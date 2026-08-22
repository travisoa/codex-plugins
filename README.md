# Codex Plugins

面向 Codex 的插件集合。仓库本身作为远程 marketplace，每个插件位于 `plugins/` 下并维护独立的清单、技能、MCP 服务和文档。

## 插件

| 插件 | 说明 | 文档 |
| --- | --- | --- |
| `codex-session-cleaner` | 查看会话项目和文件线索，批量归档或安全删除 Codex 会话 | [使用说明](plugins/codex-session-cleaner/README.md) |

## 安装

注册远程 marketplace：

```bash
codex plugin marketplace add travisoa/codex-plugins --ref main
```

安装所需插件：

```bash
codex plugin add codex-session-cleaner@codex-plugins
```

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
└── codex-session-cleaner/        Codex 会话清理器
```

新增插件时，将完整插件目录放入 `plugins/<plugin-name>/`，并在 marketplace 清单的 `plugins` 数组中追加对应条目。

## 许可证

[MIT](LICENSE)
