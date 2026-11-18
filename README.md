# Personal 3D Fabricator

用 Codex 从尺寸和参考资料创建个人 3D 打印模型、修改已有模型，或将超出打印机成型空间的模型拆成可装配的部件。Python 命令行负责几何检查、预览、拆件规划与交付。

## 安装

从 GitHub Releases 下载 `Source code` 压缩包、从仓库页面用 `Code → Download ZIP` 下载，或用 `git clone` 获取仓库。将解压或克隆后的完整目录放入项目的 `.agents/skills/personal-3d-fabricator` 或用户主目录下的 `.agents/skills/personal-3d-fabricator`。保留 [SKILL.md](SKILL.md)、`references/`、`profiles/` 和软件包源文件。进入该目录，按操作系统安装 Python 3.11 或 3.12，再运行下列命令。若安装的是 Python 3.11，Windows 命令中的 `-3.12` 改为 `-3.11`。macOS 和 Linux 的 `python3` 应指向已安装的对应版本。Linux 发行版若单独提供 `venv` 模块，请同时安装对应软件包。

Python 的 sdist 和 wheel 用于安装 CLI 与 Python 库。Codex Skill 的完整资源随上述仓库源码提供。

### Windows

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m personal_3d_fabricator.cli check-environment --json
```

### macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m personal_3d_fabricator.cli check-environment --json
```

### Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m personal_3d_fabricator.cli check-environment --json
```

在 Codex 中输入 `$personal-3d-fabricator`，说明要制作什么、关键尺寸和打印机型号。技能未出现在选择器时，重启 Codex。[Codex 技能文档](https://developers.openai.com/codex/skills)

安装后可以使用虚拟环境里的 `personal-3d-fabricator` 命令。后文的命令示例假设该环境已激活，也可以用对应平台的虚拟环境 Python 执行 `-m personal_3d_fabricator.cli`。

## 可选工具

- **CadQuery** 用于参数化实体建模、STEP 读写和连接件设计。在仓库目录中用同一个虚拟环境 Python，将安装命令改为 `-m pip install ".[cadquery]"`。用 `personal-3d-fabricator check-environment --require cadquery --json` 验证。
- **Blender** 用于复杂外形、网格编辑及后台导出。安装 [Blender](https://www.blender.org/download/) 后，可以用下面的变量指定可执行文件。用 `personal-3d-fabricator check-environment --require blender --json` 验证。后台适配器运行可信的本地 Python 脚本。脚本收到的第一个 `--` 后参数是输出的暂存路径。
- 普通 `check-environment --json` 会列出可选工具的可用状态和检测到的版本。缺少可选工具时，仍可进行网格检查、基础拆分和打印规划。

工具查找顺序为显式路径、`PATH`、常见安装目录。显式变量需指向实际可执行文件，路径无效时该工具会被报告为不可用。Windows 使用 PowerShell 的 `$env:变量名 = "可执行文件路径"`，macOS 和 Linux 使用 `export 变量名="可执行文件路径"`。

| 环境变量 | 值 |
| --- | --- |
| `PERSONAL_3D_FABRICATOR_BLENDER` | Blender 可执行文件 |
| `PERSONAL_3D_FABRICATOR_FREECAD` | FreeCAD 可执行文件 |
| `PERSONAL_3D_FABRICATOR_BAMBU_STUDIO` | Bambu Studio 可执行文件 |
| `PERSONAL_3D_FABRICATOR_ORCASLICER` | OrcaSlicer 可执行文件 |
| `PERSONAL_3D_FABRICATOR_PRUSASLICER` | PrusaSlicer 可执行文件 |
| `FABRICATOR_PREVIEW_FONT` | 覆盖预览文字所需字符的字体；字体集合可用 `#索引` 选择字面，从 0 开始 |

中文预览标签需要有中文字符覆盖的字体，例如 Noto Sans CJK。各工具的常见安装位置见[创建模型](references/create-model.md)。

## 输入与限制

- [创建模型](references/create-model.md)接受用途、尺寸、草图和参考照片。比例需要至少一项可信的实测或已确认的设计尺寸。单张照片可辅助判断外形，无法提供任意物体的精确三维模型。
- [修改模型](references/modify-model.md)支持 STL、OBJ、网格 3MF 和 STEP。STL、OBJ 要提供确认过的单位。CLI `inspect`、`plan-split` 使用 STL、OBJ 或 3MF 网格，STEP 由 CadQuery 适配器处理，拆件前从实体导出网格。原模型归档到项目，修改以新版本保存。
- 使用与实机配置一致的成型空间和喷嘴参数。可参考 [通用单喷嘴](profiles/generic-single-nozzle.json)、[通用双喷嘴](profiles/generic-dual-nozzle.json)和 [Bambu Lab X2D](profiles/bambu-x2d.json)配置。
- 当前流程面向熔融挤出打印准备。薄壁、碰撞和成型空间检查用于辅助判断，最终切片设置、材料和实物配合由用户验证。切片与启动打印由用户在本机操作。涉及承重、防护或其他安全关键用途时，按[安全边界](references/safety-boundaries.md)处理。

## 拆件与两次确认

在仓库之外创建独立项目目录。若使用源码目录下的样例位置，将生成内容放入已忽略的 `projects/`。下列命令中的 `项目父目录`、`项目目录`、`模型.stl` 和 `实际尺寸毫米数` 需替换为实际值。

| 阶段 | CLI 操作 |
| --- | --- |
| 创建项目 | `init "项目父目录" "项目名称" --scale-reference-mm 实际尺寸毫米数` |
| 归档模型并预览整体 | `inspect "模型.stl" --assumed-unit mm --project "项目目录"` |
| 用户确认整体外形、比例和装配基准 | `approve-overall "项目目录"`，状态变为 `overall_approved` |
| 展示编号零件、切缝、摆放与装配顺序 | `plan-split "项目目录" --profile profiles/generic-single-nozzle.json` |
| 用户确认拆分和装配方案 | `approve-split "项目目录"`，状态变为 `split_approved` |
| 生成最终打印文件和装配说明 | `deliver "项目目录"` |

如已选好耗材，在规划前将项目 `project.json` 的 `material` 设为材料名称，例如 `PETG`。`plan-split` 忽略大小写，并按打印机配置的 `supported_materials` 检查与记录选择。材料未指定时可以继续规划。拆件预览和交付的 `print-plan.json` 均保留已选材料。更换材料时，请建立修订项目并重新规划、确认。

超出多个轴的模型可递归拆件。默认使用平切面，安装 CadQuery 后可选 `--connector pin`，并指定销径、长度和径向间隙。拆件预览和装配说明使用实际打印件的销孔几何。

使用 Python 接口时，`split_project_mesh(project_dir, cut)` 从已确认的项目归档读取模型，切割位置采用原模型的毫米坐标。内存网格可传给 `split_mesh`。审批、交付和重复交付均检查归档文件、校验和与整体审批记录。

只有首次确认后才能规划拆分，只有第二次确认后才能发布正式打印文件。整体几何、比例或拆分方案有实质变化时，建立修订项目并重新确认。详见[拆分大型模型](references/split-large-model.md)。

## 卸载

保留需要的模型及项目文件，移除 Codex 技能目录中的 `personal-3d-fabricator` 文件夹或符号链接。若将 Python 包安装到了独立于技能目录的环境，在该环境中执行 `python -m pip uninstall personal-3d-fabricator`。从终端或系统设置中清除本次配置的环境变量。

## 许可证

[MIT](LICENSE)
