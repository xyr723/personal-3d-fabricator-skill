---
name: personal-3d-fabricator
description: 根据描述创建 3D 打印模型、修改已有模型，或把超出打印机成型空间的模型拆成可装配的部件。适用于模型预览、打印适配与拆件规划。
---

# 个人 3D 打印建模

先确认预期用途。涉及承重、防护、儿童用品、食品接触、电气、高温或用途不明确时，阅读[安全边界](references/safety-boundaries.md)。根据请求选择对应工作流。

- 从描述或尺寸新建模型时，阅读[创建模型](references/create-model.md)。收集用途、关键尺寸和打印机成型空间，并提供整体预览。
- 编辑用户提供的模型时，阅读[修改模型](references/modify-model.md)。保留原模型，确认修改范围，再展示修改结果。
- 模型超出打印机成型空间或用户要求拆件时，阅读[拆分大型模型](references/split-large-model.md)。展示整体模型并取得用户确认后，将项目状态推进到 `overall_approved`，再执行拆件。展示拆件与装配预览并再次取得用户确认后，将项目状态推进到 `split_approved`，再生成最终交付物。

如用户指定耗材，在规划前填写项目 `project.json` 的 `material`，并核对设备配置中的 `supported_materials`。材料名称匹配时忽略大小写。展示拆件预览时一并确认计划中的材料，更改材料后建立修订项目并重新规划。保留已批准的源模型归档和校验文件，以供拆件审批、交付和重试时核验。

按[安装说明](README.md)准备本地 Python 环境。运行 `personal-3d-fabricator check-environment --json` 检查可选工具。项目输出存入独立目录，原模型以归档副本保留。只有取得用户明确确认后才执行相应的审批命令。
