from PIL import Image
import pytest

from personal_3d_fabricator.adapters.blender_adapter import run_blender_script
from personal_3d_fabricator.tooling import discover_tool


def test_real_blender_save_as_preserves_relative_texture(tmp_path):
    blender = discover_tool("blender")
    if not blender.available:
        pytest.skip("Blender executable unavailable for relative texture integration test")

    texture = tmp_path / "textures" / "checker.png"
    texture.parent.mkdir()
    Image.new("RGB", (2, 2), "white").save(texture)
    original = tmp_path / "original.blend"
    source_script = tmp_path / "create.py"
    source_script.write_text(
        "import bpy, sys\n"
        f"image = bpy.data.images.load({str(texture)!r})\n"
        "image.name = 'Relative texture'\n"
        "image.use_fake_user = True\n"
        "image.filepath = '//textures/checker.png'\n"
        "output = sys.argv[sys.argv.index('--') + 1]\n"
        "bpy.ops.wm.save_as_mainfile(filepath=output, check_existing=False, relative_remap=False)\n",
        encoding="utf-8",
    )
    run_blender_script(None, source_script, original)
    original_content = original.read_bytes()

    resave_script = tmp_path / "save_as.py"
    resave_script.write_text(
        "import bpy, sys\n"
        "output = sys.argv[sys.argv.index('--') + 1]\n"
        "bpy.ops.wm.save_as_mainfile(filepath=output, check_existing=False, relative_remap=True)\n",
        encoding="utf-8",
    )
    resaved = tmp_path / "resaved.blend"
    run_blender_script(original, resave_script, resaved)

    verify_script = tmp_path / "verify.py"
    verify_script.write_text(
        "import bpy, sys\n"
        "from pathlib import Path\n"
        "image = bpy.data.images['Relative texture']\n"
        "assert image.filepath == '//textures/checker.png', image.filepath\n"
        f"assert Path(bpy.path.abspath(image.filepath)).resolve() == Path({str(texture)!r}).resolve()\n"
        "Path(sys.argv[sys.argv.index('--') + 1]).write_text('texture resolved')\n",
        encoding="utf-8",
    )
    verified = tmp_path / "verified.txt"
    run_blender_script(resaved, verify_script, verified)

    assert verified.read_text() == "texture resolved"
    assert original.read_bytes() == original_content
