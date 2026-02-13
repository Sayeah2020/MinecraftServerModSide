# 仅是本人学习python所用，并不具备任何专业功能
#### ModSide

一个用于《Minecraft》整合包迁移/本地存档转服务器存档时的 **模组侧别（Client/Server）快速筛选工具**。  
它会扫描本地 `mods` 目录中的 `.jar/.zip/.litemod` 文件，尽可能判定每个模组是否 **可用于服务端**、是否为 **客户端专用**，并提供 GUI 方便你快速挑选与导出。

> 目标：减少“把客户端模组塞进服务端导致崩服/起不来”的踩坑成本，让迁移更高效。

---

#### 功能特性

- **一键扫描**：递归扫描指定目录下的模组文件（`.jar/.zip/.litemod`）
- **按 side 归类展示**（Treeview 列表）  
  - `server_only`：偏向服务端专用  
  - `client_only`：客户端专用（通常不应放服务端）  
  - `both`：可能双端可用  
  - `unknown`：无法明确判断  
  - `risky`：检测到客户端特征，存在风险（建议实测/排查后再上服务端）
- **模组详情查看**：选中列表项可查看完整解析信息（JSON 格式）
- **导出服务端候选模组**：将 `server_only / unknown / risky` 的模组复制到目标目录
- **导出 Excel**：将当前扫描结果导出为 `.xlsx`
- **自动保存扫描记录**：每次扫描会把解析结果写入 `record/ModelInfo_YYYYMMDD_HHMMSS.json`
- **一键加载历史记录**：右侧列表显示历史 JSON 记录，点击即可加载并恢复模组列表

---

#### 判定逻辑（简述）

本项目尽量从模组包内的元数据与内容中推断 side：

- **Fabric**：读取 `fabric.mod.json` 的 `environment` 与 `entrypoints`（如 `client/server/main`）进行初步判断  
- **Quilt**：读取 `quilt.mod.json`（`quilt_loader.environment` 或 `metadata.environment`）进行初步判断  
- **Forge / NeoForge / Legacy Forge**：通常缺乏可靠的“侧别字段”，因此更多依赖启发式检测  
- **Rift / 特殊模组**：同样以启发式检测为主

启发式检测包含：
- 扫描 jar 内 `mixins*.json` 是否存在非空 `client` 段
- 扫描 `.class` 常量池字节串是否包含明显客户端标记（例如 `net/minecraft/client/`、LWJGL、Blaze3D 等）

> 注意：side 只能“尽量推断”，并非 100% 可靠。若结果为 `unknown` 或 `risky`，建议结合实际开服测试或查阅模组说明。

---

#### 运行环境

- Python 3.9+（推荐 3.10/3.11）
- 依赖：
  - `toml`
  - `openpyxl`（仅导出 xlsx 需要）
  - Tkinter（Windows 通常自带；Linux 可能需额外安装）

安装依赖：
```bash
pip install toml openpyxl
