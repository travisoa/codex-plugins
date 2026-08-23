# Codex 会话清理器 · 命令行版

面向 **Codex CLI** 的本地会话管理插件。通过交互表单完成筛选、勾选和归档/删除，
并附带一套纯标准库的终端界面（TUI）。

> 使用 ChatGPT 桌面端 Codex 的用户请改装 [`codex-session-cleaner`](../codex-session-cleaner/README.md)，
> 那一版提供可视化管理页。两版共用同一份核心逻辑，但工具面互不重叠：
> 本版不提供管理页组件，管理页版不提供交互表单，因此模型不会在一种客户端上误用另一种界面。

## 功能

- 通过 `select_sessions` 进入三步交互：筛选 → 选择会话 → 选定操作，全部在表单中完成；
- 浏览未归档、已归档或全部顶层会话，统计派生会话数量，并显示被 Codex 常规列表隐藏、
  但仍会阻止源会话删除的空分叉；
- 自动添加类别标签（会话管理、自动化、插件开发、飞书协作、文档表格、图像视频、代码开发、常规任务、隐藏分叉）；
- 按最后更新时间筛选 1 天内、1 周内、1 个月内，以及 1 周前、1 个月前、3 个月前；
- 从会话记录中提取“修改文件”和“命令引用文件”线索；
- 归档和删除成功后都会通知 Codex 桌面侧边栏刷新；
- 禁止删除当前会话和临时会话；
- 另附纯终端界面（TUI），可脱离对话直接在终端里操作。

## 运行要求

- Codex CLI，且终端可执行 `codex`；
- Python 3（仅使用标准库）；
- 客户端需支持 MCP `elicitation`（Codex CLI 即支持）。

`codex` 不在默认 `PATH` 时，通过 `CODEX_CLI_PATH` 指定路径；非默认数据目录通过 `CODEX_HOME` 指定。

## 安装

本插件须通过 Codex marketplace 安装。marketplace 来源支持本地目录、Git 仓库及 HTTPS/SSH Git 地址。

### 1. 注册远程 marketplace

```bash
codex plugin marketplace add travisoa/codex-plugins --ref main
```

### 2. 安装插件

```bash
codex plugin add codex-session-cleaner-cli@codex-plugins
```

验证安装状态：

```bash
codex plugin list
```

列表中应显示 `codex-session-cleaner-cli@codex-plugins` 的状态为 `installed, enabled`。
安装后新建 Codex 任务以加载技能和 MCP 工具。

也可在 Codex CLI 中输入 `/plugins`，从已配置的 marketplace 中安装插件。

## 使用

向 Codex 提出整理、归档或删除会话的需求（例如“清理会话”“打开会话管理器”），插件会调用
`select_sessions`，并依次弹出三张表单：

1. **筛选**：会话范围（全部 / 未归档 / 已归档）、最后更新时间、类别标签（标签选项按实际会话数量生成）；
2. **选择**：筛选结果按每页 10 个编号列出，输入序号多选，支持 `1,3`、`5-7`、`all`、`clear`；
   超过一页时，输入序号后会单独再弹一张“完成选择 / 下一页 / 上一页”表单决定下一步；
3. **操作**：取消、归档所选、永久删除所选，默认取消。

每张表单只包含一个字段：Codex CLI 会将同一张表单的全部字段依次询问后统一提交，
若把序号与翻页并入同一张表单，用户选定“完成选择”后仍会被要求填写序号。

序号在整个结果集中始终有效，可以跨页选择；已选中的会话在列表中标为 `✓` 并汇总显示。
当前会话和其他受保护会话标为 `⊘`，仍然可见但不能被选中。
序号无法识别或超出范围时不会中止流程，而是在同一张表单里提示原因并重新输入。

筛选结果数量不设上限，页码如实显示总页数（例如“第 1/17 页”）。
表单等待的是用户操作而非机器应答，因此超时设为 15 分钟。

表单文案跟随宿主语言：Codex Host 提供语言时按其显示中文或英文，未提供时回退到 `LANG` 等环境变量。

### 终端界面（TUI）

需要脱离对话直接操作时，运行：

```bash
./scripts/launch_tui.sh
```

从 marketplace 安装后，脚本位于插件安装目录下，例如：

```bash
~/.codex/plugins/cache/codex-plugins/codex-session-cleaner-cli/*/scripts/launch_tui.sh
```

| 按键 | 作用 |
| --- | --- |
| `↑` `↓` / `k` `j` | 移动光标，`PgUp` `PgDn` 翻页 |
| `空格` | 勾选或取消当前会话；受保护的会话不可勾选 |
| `/` | 搜索标题、ID、标签或项目路径 |
| `F` | 打开筛选面板 |
| `I` | 查看修改文件与命令引用文件线索 |
| `A` / `D` | 归档 / 永久删除所选（删除需输入确认词） |
| `R` / `Q` | 刷新 / 退出 |

界面语言默认跟随 `LANG`，可用 `--lang zh` 或 `--lang en` 指定。

独立运行 TUI 时，进程本身不属于任何 Codex 会话，默认没有“当前会话”可保护；
需要保护某个正在运行的会话时，通过 `--current <thread-id>` 指定。

### 删除被隐藏分叉引用的会话

若某个会话标记为“被 N 个分叉引用”，需先删除这些分叉才能删除源会话。
在选择表单中同时勾选分叉与源会话即可，插件会按“分叉在前、源会话在后”的顺序执行。
仅归档分叉或源会话不会解除历史引用。

侧边栏同步失败时，刷新或重启 Codex；已删除会话不得重复提交。

### 会话被占用无法归档或删除

Codex 给每个会话加了跨进程写锁（`~/.codex/thread-writer-locks/<id>.lock`）：归档要把 rollout 文件
从 `sessions/` 搬到 `archived_sessions/`，而文件正被某个进程写着时搬动会丢数据，因此持有者之外的进程一律被拒。

持有者通常是 ChatGPT 桌面端常驻的 app-server。该进程加载过的会话不会因界面切换而释放，
目前也未观察到锁会自行超时，因此切换会话或等待重试均无效。

解除占用有两种方式：在 Codex 侧边栏中归档或删除该会话，由持有者自身完成；或退出并重启
ChatGPT 桌面端，释放全部锁。

插件会主动探测该锁，将此类会话在列表中标记为「使用中」并禁止勾选，避免选定后才操作失败。
锁文件多数为残留，因此判断依据是能否取得文件锁，而非锁文件是否存在。
## 提供的工具

| 工具 | 用途 |
| --- | --- |
| `select_sessions` | 主入口：引导用户筛选、选择并归档或删除会话 |
| `list_sessions` | 只读查看：按状态、搜索词、类别标签和最后更新时间列出会话 |
| `inspect_session_files` | 提取指定会话的修改文件和命令引用文件线索 |
| `archive_sessions` | 批量归档指定会话（通常应改用 `select_sessions`） |
| `delete_sessions` | 在确认词校验通过后永久删除会话（通常应改用 `select_sessions`） |

## 安全边界

- 永久删除通过 Codex `thread/delete` 执行，删除所选持久化会话及其派生会话，且不可撤销；
- 插件不会删除项目文件；文件列表只是从会话记录提取的线索，不代表项目的完整文件集合；
- 归档成功后，插件仅通过桌面 IPC 广播状态变化，不改动桌面本地目录项；删除成功后才按已删除的会话 ID 清理目录项，并同样通过 IPC 刷新窗口；
- 桌面数据结构不兼容时停止侧边栏同步，不影响已完成的会话删除；
- 当前会话和临时会话不可删除；归档同样只接受列表中的顶层会话，拒绝派生会话、临时会话和已不存在的 ID；
- 被其他 Codex 进程持有的会话标记为「使用中」，不可选中；
- 源会话仍被未选择的历史分叉引用时停止删除，并返回阻塞分叉 ID；
- 同批选中的分叉删除失败时，源会话一并停止删除，避免留下悬空引用；
- 交互表单只做选择与确认，最终名单以用户在表单中的选择为准，模型不能替用户扩大范围；
- 后端校验确认词、当前会话上下文及非空会话 ID，单次最多处理 100 个会话；
- 单次最多读取每种状态 5000 个会话；确实超出时列表会明确标注未列全，而不是把截断后的结果当成完整清单；
- 终端界面复用同一套后端校验，不额外放宽任何限制；独立运行时没有“当前会话”可保护，需要保护时用 `--current` 指定；
- 插件不会清理跨会话共享的插件缓存、模型缓存、认证信息或全局配置。

## 卸载

```bash
codex plugin remove codex-session-cleaner-cli@codex-plugins
```

## 本地开发

`server/core.py` 与管理页版共享同一份内容，改动后须同步到 `../codex-session-cleaner/server/core.py`；
`tests/test_core_parity.py` 会校验两者一致。

本地调试时，可将仓库目录直接注册为 marketplace：

```bash
codex plugin marketplace add /path/to/codex-plugins
codex plugin add codex-session-cleaner-cli@codex-plugins
```

测试与语法检查：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall server tui tests
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
server/core.py             与管理页版共享的核心逻辑
server/server.py           命令行版工具面
server/interactive.py      筛选 / 选择 / 操作三步交互表单
tui/session_tui.py         纯标准库 curses 终端界面
scripts/launch_server.sh   MCP 服务器启动脚本
scripts/launch_tui.sh      终端界面启动脚本
tests/test_server.py       后端与交互流程测试
tests/test_tui.py          终端界面状态、宽度与渲染测试
tests/test_core_parity.py  核心逻辑与管理页版的一致性校验
```

## 许可证

[MIT](../../LICENSE)
