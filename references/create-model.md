# 从描述创建模型

1. 收集用途、关键尺寸、装配界面、外形参考、材料与打印机成型空间。至少取得一个可信的物理测量或用户确认的设计尺寸，将单位统一为毫米。照片可辅助判断外形。缺少可信比例或配合所需尺寸时，先请用户补充，再确定绝对比例。
2. 孔距、壁厚和公差主导的功能件使用 CadQuery，保存带参数的 Python 源文件。需要与其他 CAD 软件交换实体时，同时导出 STEP。复杂曲面、雕塑和外观道具优先使用 Blender，保存可编辑的 Blender 工程。
3. 预览整体轮廓、安装面和关键尺寸，记录比例基准与尚待确认的装配条件。用适合该模型的几何检查确认成型空间、封闭性、实际壁厚和配合间隙。
4. 如果需要拆件，按照[拆分大型模型](split-large-model.md)取得整体批准，再执行拆件。

## 查找本地可选工具

工具发现先查显式环境变量，再查 `PATH`，最后查下表的常见安装位置。显式变量填写本机可执行文件的完整路径。配置的路径不存在、不可执行，或探测命令执行失败时，返回不可用状态和该路径。安装在其他位置时，用环境变量指向实际程序。

| 工具 | 环境变量 | PATH 候选名 | macOS 应用内路径 | Windows 安装路径 |
| --- | --- | --- | --- | --- |
| Blender | `PERSONAL_3D_FABRICATOR_BLENDER` | `blender` | `Blender.app/Contents/MacOS/Blender` | `Blender Foundation/Blender/blender.exe` 或 `Blender Foundation/Blender */blender.exe` |
| FreeCAD | `PERSONAL_3D_FABRICATOR_FREECAD` | `freecad`、`FreeCAD`、`freecadcmd` | `FreeCAD.app/Contents/MacOS/FreeCAD` | `FreeCAD/bin/FreeCAD.exe` 或 `FreeCAD */bin/FreeCAD.exe` |
| Bambu Studio | `PERSONAL_3D_FABRICATOR_BAMBU_STUDIO` | `bambu-studio`、`bambustudio` | `BambuStudio.app/Contents/MacOS/BambuStudio` | `Bambu Studio/bambu-studio.exe` |
| OrcaSlicer | `PERSONAL_3D_FABRICATOR_ORCASLICER` | `orcaslicer`、`orca-slicer` | `OrcaSlicer.app/Contents/MacOS/OrcaSlicer` | `OrcaSlicer/orca-slicer.exe` |
| PrusaSlicer | `PERSONAL_3D_FABRICATOR_PRUSASLICER` | `prusa-slicer`、`prusaslicer` | `PrusaSlicer.app/Contents/MacOS/PrusaSlicer` | `Prusa3D/PrusaSlicer/prusa-slicer-console.exe` |

macOS 在 `/Applications` 和 `~/Applications` 下查找。Windows 在 `%ProgramFiles%` 和 `%ProgramFiles(x86)%` 下查找原生 `.exe`。Windows 的 PATH 查找也仅使用原生 `.exe`，并在查找 PrusaSlicer 时优先尝试目录内的 `prusa-slicer-console.exe`。Linux 在 `/usr/bin` 和 `/usr/local/bin` 下查找 PATH 候选名。

CadQuery 和 STEP 读写使用项目的 `cadquery` 可选依赖。Blender 后台适配器仅运行可信的本地 Python 脚本，把与最终文件同目录的隐藏暂存路径作为 `--` 后的第一个脚本参数，以维持 Blender 相对资源路径。Blender 和 STEP 导出在确认暂存文件有效后原子发布，冲突时保留已有产物。脚本失败时查看执行诊断。发布失败时检查空间和目录权限，或换到支持原子排他重命名的本地卷和未占用的文件名后重新运行。相关工具不可用时，继续可完成的尺寸核对和方案工作，并标明待生成的文件。
