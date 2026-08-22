# Codex 会话清理器

面向 **ChatGPT 桌面端 Codex** 的本地会话管理插件。通过 MCP Apps 管理页和 Codex `app-server` 集中查看当前及已归档会话，按状态、类别标签和日期筛选，核对项目与文件线索，并安全地批量归档或永久删除会话。

![Codex 会话清理器管理页](assets/screenshots/session-manager-overview.png)

> 使用 Codex CLI 的用户请改装 [`codex-session-cleaner-cli`](../codex-session-cleaner-cli/README.md)，
> 那一版通过交互表单和终端界面操作。两版共用同一份核心逻辑，但工具面互不重叠。

## 功能

- 浏览当前、已归档或全部顶层会话，并统计各会话的派生会话数量；同时显示被 Codex 常规列表隐藏、但仍会阻止源会话删除的空分叉；
- 按标题、会话 ID、类别标签或项目路径搜索会话；
- 自动添加“会话管理、自动化、插件开发、飞书协作、文档表格、图像视频、代码开发、常规任务、隐藏分叉”等类别标签，并支持按标签筛选；
- 分类优先使用标题和清洗后的会话预览，忽略 `plugin://` 调用标记；仅在内容没有明确类别时才使用项目路径作为补充依据；
- 按最后更新时间筛选 1 天内、1 周内、1 个月内，以及 3 个月前、1 个月前、1 周前的会话，或使用自定义日期范围；
- 显示项目目录、Git 分支、会话状态和最后更新时间；
- 从会话记录中提取“修改文件”和“命令引用文件”线索，并支持复制路径；
- 批量归档所选会话；
- 中文界面输入确认词 `删除`（英文界面输入 `delete`）后，批量删除所选会话及其派生会话；同批选择存在历史引用关系的分叉和源会话时，插件会先删除分叉；
- 管理页根据 Codex Host 语言自动显示中文或英文，Host 未提供语言时回退到浏览器语言；也可使用标题栏中“当前管理会话受保护”旁的“中文 / EN”按钮手动切换，手动选择在当前页面内优先；
- 归档和删除成功后都会通知 Codex 桌面侧边栏刷新：归档只广播状态变化，删除还会清理桌面本地目录项，避免保留无法打开的历史条目；
- `open_session_manager` 始终只列出会话并返回管理页组件，本版不含任何交互表单工具，因此不会在管理页可用时弹出终端选择器；
- 根据 MCP `initialize` 握手判断客户端能否渲染组件：支持时返回可视化管理页，不支持时返回同等信息的文本会话列表（含会话 ID、项目、标签、状态与阻塞分叉），并提示可改用终端界面；
- 禁止删除当前管理会话和临时会话；

## 运行要求

- Codex CLI，且终端可执行 `codex`；
- Python 3；
- ChatGPT 桌面端 Codex，用于显示可视化管理页。

服务器仅使用 Python 标准库。Codex 插件支持从 ChatGPT 桌面端或 Codex CLI 安装，IDE 扩展暂不支持插件。参见 [OpenAI 插件文档](https://learn.chatgpt.com/docs/plugins)。

`codex` 不在默认 `PATH` 时，通过 `CODEX_CLI_PATH` 指定路径；非默认数据目录通过 `CODEX_HOME` 指定。

## 安装

本项目须通过 Codex marketplace 安装。marketplace 来源支持本地目录、Git 仓库及 HTTPS/SSH Git 地址。

### 1. 注册远程 marketplace

```bash
codex plugin marketplace add travisoa/codex-plugins --ref main
```

### 2. 安装插件

```bash
codex plugin add codex-session-cleaner@codex-plugins
```

验证安装状态：

```bash
codex plugin list
```

列表中应显示 `codex-session-cleaner@codex-plugins` 的状态为 `installed, enabled`，来源为 Git marketplace。安装后新建 Codex 任务以加载技能和 MCP 工具。

也可在 Codex CLI 中输入 `/plugins`，从已配置的 marketplace 中安装插件。

## 使用

### 打开管理页

在新建的 Codex 任务中使用以下指令触发插件：

- `打开 Codex 会话管理页`
- `查看本地 Codex 会话及其项目路径`
- `清理 Codex 历史会话`

插件会在 Codex 中打开管理页。标题栏右侧显示当前管理会话的保护状态，并在同一区域提供“中文 / EN”切换按钮。

### 筛选与核对

1. 使用“当前 / 已归档 / 全部”切换会话状态范围；
2. 在搜索框中输入标题、会话 ID、类别标签或项目路径；
3. 使用“标签”筛选会话类别；使用“日期”筛选 1 天内、1 周内、1 个月内或更早的会话，也可以指定自定义起止日期；
4. 核对会话卡片中的状态、类别标签、项目目录、Git 分支、最后更新时间和派生会话数量；
5. 点击“查看文件”，检查从会话记录提取的修改文件和命令引用文件线索；需要在终端中定位项目时可点击“复制项目路径”。

列表默认展示顶层会话。子代理、审查及其他普通派生会话随所属顶层会话处理，不单独显示；会阻止删除的隐藏空分叉会作为“隐藏分叉”单独显示。

### 归档或永久删除

1. 勾选目标会话；当前管理会话和临时会话受保护，不能选择；
2. 点击“归档所选”，将会话移入 Codex 已归档列表；
3. 点击“永久删除所选”，在中文界面输入 `删除`，英文界面输入 `delete`，再确认永久删除；
4. 插件会删除所选会话及其派生会话和持久化元数据，但不会删除项目文件；
5. 删除成功后，管理页自动刷新会话列表，并同步 Codex 桌面侧边栏。

![Codex 会话清理器永久删除确认](assets/screenshots/session-manager-delete-confirmation.png)

### 会话被占用无法归档或删除

Codex 给每个会话加了跨进程写锁（`~/.codex/thread-writer-locks/<id>.lock`）：归档要把 rollout 文件
从 `sessions/` 搬到 `archived_sessions/`，而文件正被某个进程写着时搬动会丢数据，因此持有者之外的进程一律被拒。

持有者通常是 ChatGPT 桌面端常驻的 app-server，**它加载过的会话不会因为界面上切走而释放**，
所以"切换到别的会话再重试"没有用。这类会话只能在 Codex 侧边栏中归档或删除，由持有者自己完成。

插件会主动探测该锁，把这类会话在列表中标为「使用中」并禁止勾选，避免勾选后才失败。
锁文件本身多数是残留，因此判断依据是能否取得文件锁，而不是锁文件是否存在。
### 删除启动会话管理页的任务

若删除用于启动会话管理页的任务失败，先在 Codex 侧边栏将该任务归档；随后新建或切换到另一个任务，重新打开会话管理页，在“已归档”或“全部”中选择原任务并执行永久删除。不要在原任务打开的管理页中删除该任务自身。

### 删除被隐藏分叉引用的任务

若源任务显示“引用分叉阻止源会话删除”，先筛选“隐藏分叉”标签，勾选对应分叉后删除，再删除源任务；也可以同时勾选分叉与源任务，插件会按“分叉在前、源任务在后”的安全顺序执行。仅归档分叉或源任务不会解除历史引用。

侧边栏同步失败时，刷新或重启 Codex；已删除会话不得重复提交。

## 提供的工具

| 工具 | 用途 |
| --- | --- |
| `open_session_manager` | 读取会话并打开可视化管理页 |
| `list_sessions` | 按状态、搜索词、类别标签和最后更新时间列出会话，并补充隐藏分叉 |
| `inspect_session_files` | 提取指定会话的修改文件和命令引用文件线索 |
| `archive_sessions` | 批量归档所选顶层会话 |
| `delete_sessions` | 在确认词与当前会话保护校验通过后永久删除会话 |

在不能渲染 MCP Apps 组件的客户端中，工具会返回结构化的文本会话列表而不是管理页，
并在开头提示改用命令行版插件 `codex-session-cleaner-cli`——那一版通过交互表单完成勾选与操作，
本版不提供交互表单，因此模型不会在管理页可用时误弹表单。

## 安全边界

- 永久删除通过 Codex `thread/delete` 执行，删除所选持久化会话及其派生会话，且不可撤销；
- 插件不会删除项目文件；页面中的文件列表只是从会话记录提取的线索，不代表项目的完整文件集合；
- 归档成功后，插件仅通过桌面 IPC 广播状态变化，不改动桌面本地目录项；删除成功后才按已删除的会话 ID 清理目录项，并同样通过 IPC 刷新窗口；
- 桌面数据结构不兼容时停止侧边栏同步，不影响已完成的会话删除；
- 当前管理会话和临时会话不可删除；归档同样只接受列表中的顶层会话，拒绝派生会话、临时会话和已不存在的 ID；
- 源会话仍被未选择的历史分叉引用时停止删除，并返回阻塞分叉 ID；
- 同批选中的分叉删除失败时，源会话一并停止删除，避免留下悬空引用；
- 管理页使用短期、不可伪造的上下文令牌维持当前会话保护；令牌失效后必须重新打开管理页；
- 后端校验确认词、管理页上下文及非空会话 ID；
- 终端界面复用同一套后端校验，不额外放宽任何限制；CLI 未绑定当前会话时没有“当前会话”可保护，需要保护时用 `--current` 指定；
- 插件不会清理跨会话共享的插件缓存、模型缓存、认证信息或全局配置。

## 卸载

```bash
codex plugin remove codex-session-cleaner@codex-plugins
```

## 本地开发

`server/core.py` 与命令行版共享同一份内容，改动后须同步到 `../codex-session-cleaner-cli/server/core.py`；
`tests/test_core_parity.py` 会校验两者一致。

本地调试时，可将仓库目录直接注册为 marketplace：

```bash
codex plugin marketplace add /path/to/codex-plugins
codex plugin add codex-session-cleaner@codex-plugins
```

测试与语法检查：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall server tests
```

启动 MCP 服务器：

```bash
./scripts/launch_server.sh
```

服务器使用逐行 JSON-RPC（stdio）。正常运行时由 Codex 根据 [`.mcp.json`](.mcp.json) 自动启动。

## 项目结构

```text
.codex-plugin/plugin.json  插件清单与默认触发语句
.mcp.json                  MCP 服务器启动配置
skills/session-manager/    会话管理技能说明
server/core.py             与命令行版共享的核心逻辑
server/server.py           管理页版工具面
web/manager.html           自包含的 MCP Apps 管理页
assets/screenshots/        README 使用的管理页与删除确认截图
tests/test_server.py       后端与页面契约测试
```

## 许可证

[MIT](../../LICENSE)
