from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shlex
import subprocess
from urllib.parse import unquote, urlsplit

import pytest
import yaml
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]


def read_skill():
    skill = ROOT / "SKILL.md"
    assert skill.is_file()
    opening, frontmatter, body = skill.read_text(encoding="utf-8").split("---\n", 2)
    assert opening == ""
    return yaml.safe_load(frontmatter), body


def test_skill_discovery_metadata():
    metadata, _ = read_skill()
    assert metadata["name"] == "personal-3d-fabricator"
    assert metadata["description"].strip()


def test_codex_interface_metadata():
    config = ROOT / "agents" / "openai.yaml"
    assert config.is_file()
    interface = yaml.safe_load(config.read_text(encoding="utf-8"))["interface"]
    assert interface["display_name"].strip()


def test_skill_routes_resolve_to_workflow_references():
    _, body = read_skill()
    routes = {}
    for token in MarkdownIt().parse(body):
        if token.type != "inline":
            continue
        approvals = {child.content for child in token.children if child.type == "code_inline"}
        for child in token.children:
            if child.type == "link_open":
                target = PurePosixPath(child.attrGet("href"))
                assert not target.is_absolute()
                routes[(ROOT / target).resolve()] = approvals

    references = ROOT / "references"
    split = references / "split-large-model.md"
    expected = {
        references / "create-model.md",
        references / "modify-model.md",
        split,
        references / "safety-boundaries.md",
    }
    assert expected <= routes.keys()
    assert {"overall_approved", "split_approved"} <= routes[split]
    assert all(path.is_file() for path in routes)

LOCAL_HOME = re.compile(
    r"(?<![\w/])/(?:Users|home|Volumes)/[^/\s`]+/"
    r"|[A-Za-z]:[\\/]Users[\\/][^\\/\s`]+[\\/]",
    re.IGNORECASE,
)


def readme_tokens():
    document = ROOT / "README.md"
    assert document.is_file(), "Release README is missing"
    return MarkdownIt().parse(document.read_text(encoding="utf-8"))


def installs_local_package(arguments):
    if arguments[:1] == ["&"]:
        arguments = arguments[1:]
    if not arguments:
        return False

    launcher, *options = (argument.strip("\"'") for argument in arguments)
    name = PureWindowsPath(launcher).name.lower().removesuffix(".exe")
    if name == "py" and options and re.fullmatch(r"-(?:v:)?3(?:\.\d+)?", options[0], re.IGNORECASE):
        options = options[1:]
    if re.fullmatch(r"py|python(?:3(?:\.\d+)?)?", name):
        if options[:2] != ["-m", "pip"]:
            return False
        options = options[2:]
    elif name not in {"pip", "pip3"}:
        return False
    return options[:1] == ["install"] and options[-1:] in (["."], ["./"], [".\\"])


def validate_installation(tokens, platform_name):
    headings = (
        (index, token) for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.markup.startswith("#") and token.level == 0
    )
    for index, heading in headings:
        if tokens[index + 1].content.strip().casefold() == platform_name.casefold():
            break
    else:
        raise AssertionError(f"Missing {platform_name} installation heading")

    windows = platform_name == "Windows"
    shells = {"", "shell", "powershell", "pwsh", "ps1", "cmd", "bat"} if windows else {
        "", "shell", "bash", "sh", "zsh",
    }
    for token in tokens[index + 3:]:
        if token.type == "heading_open" and token.tag <= heading.tag:
            break
        if token.type != "fence" or token.info.strip().lower() not in shells:
            continue
        for line in token.content.splitlines():
            arguments = shlex.split(line, comments=True, posix=not windows)
            if installs_local_package(arguments):
                return
    raise AssertionError(f"Missing {platform_name} local package install")


@pytest.mark.parametrize("platform_name", ["Windows", "macOS", "Linux"])
def test_readme_documents_local_install_for_each_platform(platform_name):
    validate_installation(readme_tokens(), platform_name)


WINDOWS_INSTALLATION = r"""### Windows

Use PowerShell in the repository folder.

```powershell
# Create and use an isolated environment.
py -3.11 -m venv my-env
& ".\my-env\Scripts\python.exe" -m pip install --upgrade pip
& ".\my-env\Scripts\python.exe" -m pip install -e "." # Install from the checkout.
```

Check the installed CLI.

```powershell
& ".\my-env\Scripts\personal-3d-fabricator.exe" check-environment --json
```
"""


def test_installation_check_accepts_prose_comments_and_equivalent_launcher():
    validate_installation(MarkdownIt().parse(WINDOWS_INSTALLATION), "Windows")


@pytest.mark.parametrize(
    ("before", "after", "message"),
    [
        ("### Windows", "Windows", "Missing Windows installation heading"),
        ('-m pip install -e "."', '-m pip install build', "Windows local package install"),
    ],
)
def test_installation_check_reports_missing_platform_or_local_install(before, after, message):
    with pytest.raises(AssertionError, match=message):
        validate_installation(MarkdownIt().parse(WINDOWS_INSTALLATION.replace(before, after)), "Windows")


def test_git_checks_out_versioned_schemas_with_lf():
    schemas = sorted(path.relative_to(ROOT).as_posix() for path in ROOT.glob("schemas/*.json"))
    assert schemas
    result = subprocess.check_output(
        ["git", "check-attr", "-z", "text", "eol", "--", *schemas], cwd=ROOT,
    )
    paths = result.decode().split(chr(0))[:-1]
    fields = zip(paths[::3], paths[1::3], paths[2::3], strict=True)
    actual = {(path, attribute): value for path, attribute, value in fields}
    for name in schemas:
        assert actual[name, "text"] == "set", name
        assert actual[name, "eol"] == "lf", name


def test_readme_links_resolve_to_shipped_files():
    targets = set()
    for token in readme_tokens():
        for child in token.children or ():
            if child.type != "link_open":
                continue
            url = urlsplit(child.attrGet("href"))
            if url.scheme or url.netloc or not url.path:
                continue
            target = (ROOT / unquote(url.path)).resolve()
            assert target.is_relative_to(ROOT)
            assert target.is_file(), f"Broken README link: {url.path}"
            targets.add(target.relative_to(ROOT).as_posix())
    assert {"SKILL.md", "LICENSE", "references/create-model.md",
            "references/modify-model.md", "references/split-large-model.md"} <= targets


def test_mit_license_has_grant_and_disclaimer():
    path = ROOT / "LICENSE"
    assert path.is_file(), "MIT license is missing"
    paragraphs = path.read_text(encoding="utf-8").strip().split("\n\n")
    title, copyright_notice, grant, condition, disclaimer = [
        " ".join(paragraph.split()) for paragraph in paragraphs
    ]
    assert title == "MIT License"
    assert re.fullmatch(r"Copyright \(c\) \d{4} .+", copyright_notice)
    assert all(right in grant for right in ("use, copy, modify", "sublicense", "sell"))
    assert "copyright notice" in condition and "all copies" in condition
    assert '"AS IS"' in disclaimer and "WITHOUT WARRANTY" in disclaimer


def test_publishable_documents_exclude_developer_paths():
    tracked = subprocess.check_output(["git", "ls-files", "-z", "--", "*.md"], cwd=ROOT)
    documents = {Path(name.decode()) for name in tracked.split(b"\0") if name}
    documents.update((Path("README.md"), Path("SKILL.md")))
    for relative in sorted(documents):
        path = ROOT / relative
        if path.is_file():
            assert not LOCAL_HOME.search(path.read_text(encoding="utf-8")), relative


def test_sample_projects_and_diagnostics_are_ignored_and_untracked():
    ignored = (
        "projects/example/project.json",
        "projects/example/source/model.stl",
        "projects/example/printable/part-001.stl",
        "test-results/pytest.xml",
        ".superpowers/sdd/progress.md",
    )
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=ROOT, input=("\n".join(ignored) + "\n").encode(), capture_output=True, check=True,
    )
    assert set(result.stdout.decode().splitlines()) == set(ignored)
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "projects", "test-results", ".superpowers", "dist", "build"],
        cwd=ROOT,
    )
    assert not tracked


def test_workflow_covers_core_validation_matrix_and_failure_diagnostics():
    path = ROOT / ".github" / "workflows" / "test.yml"
    assert path.is_file(), "Cross-platform test workflow is missing"
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert {"push", "pull_request"} <= set(workflow["on"])
    job = workflow["jobs"]["test"]
    matrix = job["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "macos-latest", "windows-latest"}
    assert set(matrix["python-version"]) == {"3.11", "3.12"}
    assert job["runs-on"] == "${{ matrix.os }}"
    steps = job["steps"]
    setup, = (step for step in steps if step.get("uses", "").startswith("actions/setup-python@"))
    assert setup["with"]["python-version"] == "${{ matrix.python-version }}"
    commands = [shlex.split(line) for step in steps for line in step.get("run", "").splitlines()]
    assert ["python", "-m", "pip", "install", ".[dev]"] in commands
    assert ["python", "scripts/quick_validate.py", "."] in commands
    test_command, = (command for command in commands if command[:4] == ["python", "-m", "pytest", "-q"])
    upload, = (step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["if"] == "failure()"
    report, = (arg.split("=", 1)[1] for arg in test_command if arg.startswith("--junitxml="))
    assert PurePosixPath(report).is_relative_to(PurePosixPath(upload["with"]["path"]))


def test_cadquery_job_runs_the_cad_regression_suites():
    path = ROOT / ".github" / "workflows" / "test.yml"
    job = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]["cadquery"]
    commands = [shlex.split(line) for step in job["steps"] for line in step.get("run", "").splitlines()]
    backend = ["personal-3d-fabricator", "check-environment", "--require", "cadquery", "--json"]
    tests, = (command for command in commands if command[:3] == ["python", "-m", "pytest"])
    assert commands.index(["python", "-m", "pip", "install", ".[dev,cadquery]"]) < commands.index(backend)
    assert commands.index(backend) < commands.index(tests)
    targets = {arg for arg in tests[3:] if arg != "-q"}
    cad_suites = {
        "tests/test_connectors.py",
        "tests/test_end_to_end.py",
        "tests/test_geometry_workflow.py",
        "tests/test_project_consistency.py",
        "tests/test_split_evidence.py",
        "tests/test_tooling.py",
    }
    if targets and targets != {"tests"}:
        assert cad_suites <= targets
