# Codex Plugins

面向 Codex 的插件集合。仓库本身作为远程 marketplace，每个插件位于 `plugins/` 下并维护独立的清单、技能、MCP 服务和文档。

## 插件

| 插件 | 说明 | 文档 |
| --- | --- | --- |
| `codex-session-cleaner` | **桌面客户端版**：可视化管理页，查看会话项目和文件线索，批量归档或安全删除 | [使用说明](plugins/codex-session-cleaner/README.md) |
| `codex-session-cleaner-cli` | **命令行版**：Codex CLI 中通过交互表单筛选、勾选并归档或删除，另附终端界面 | [使用说明](plugins/codex-session-cleaner-cli/README.md) |

## 安装

注册远程 marketplace：

```bash
codex plugin marketplace add travisoa/codex-plugins --ref main
```

按客户端选择其一安装——桌面客户端装管理页版，Codex CLI 装命令行版：

```bash
codex plugin add codex-session-cleaner@codex-plugins
```

```bash
codex plugin add codex-session-cleaner-cli@codex-plugins
```

两者共用同一份核心逻辑，但工具面互不重叠：管理页版不提供交互表单，命令行版不提供管理页组件，因此模型不会在一种客户端上误用另一种界面。

验证安装状态：

```bash
codex plugin list
```

安装或更新插件后，新建 Codex 任务以加载对应的技能和 MCP 工具。

## 更新

```bash
codex plugin marketplace upgrade codex-plugins
codex plugin add codex-session-cleaner@codex-plugins
```

## 仓库结构

```text
.agents/plugins/marketplace.json  marketplace 清单
plugins/                          独立插件目录
├── codex-session-cleaner/        桌面客户端版（管理页）
└── codex-session-cleaner-cli/    命令行版（交互表单 + 终端界面）
```

两个插件各自持有一份 `server/core.py`（内容完全相同，由 `tests/test_core_parity.py` 校验）。修改共享逻辑后需同步到另一个目录。

新增插件时，将完整插件目录放入 `plugins/<plugin-name>/`，并在 marketplace 清单的 `plugins` 数组中追加对应条目。

## 许可证

[MIT](LICENSE)
