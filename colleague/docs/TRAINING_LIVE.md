# 真实客户端与自有 MCP 验收

[training_live_smoke.py](../scripts/training_live_smoke.py) 用真实 Claude Code/Codex CLI 发送一个固定合成任务。自有 stdio MCP 只提供 `lookup`：在内存表里查询 `beta`，返回 `37`。工具不读业务文件、不执行命令、不访问网络；测试脚本只把工具发现响应和固定用例结果写入专用验收目录。

这与 [离线 demo](TRAINING.md) 不同：需要平台 CLI 已自行登录、能访问模型服务，可能消耗订阅额度或 API 费用。脚本不会读取、复制或显示认证文件，也不修改全局配置或持久信任。平台 CLI 会使用自己的既有认证，并可能把本次合成会话和运行状态写入其正常目录；脚本不修改 `HOME` 或 `CODEX_HOME`。

## 运行方式

在 `colleague/` 下先生成计划，不调用模型。输出目录必须尚不存在：

```bash
python3 scripts/training_live_smoke.py --agent claude --out /绝对路径/新的claude-plan目录
python3 scripts/training_live_smoke.py --agent codex --out /绝对路径/新的codex-plan目录
```

查看 `plan.json` 中的命令后，使用另一个新目录显式开始真实调用：

```bash
python3 scripts/training_live_smoke.py --agent claude --out /绝对路径/新的claude-live目录 --run
python3 scripts/training_live_smoke.py --agent codex --out /绝对路径/新的codex-live目录 --run
```

每次 `--run` 只启动一次客户端，不自动重试。外层 90 秒超时后终止该进程组。Claude 使用 `--max-turns 3` 和估算费用上限 `--max-budget-usd 0.75`；官方说明该估算上限可能被单次请求超出，并非账单硬上限。Codex 当前没有已核实的美元限额参数，脚本设置实验性的 12,000-token rollout budget；它不等于美元保证。不要直接把真实模型调用加入普通 CI。

### Codex 独立 Stop 验收

默认 `--profile mcp` 运行上面的工具调用与显式导入链。`--profile stop` 单独检验平台自动执行 Stop 并完成本地采集，不调用 MCP，不通过 import/recover 补造成功。依次可准备计划、只读探测，或显式授权一次模型调用；每条命令都使用新的目录：

```bash
python3 scripts/training_live_smoke.py --agent codex --profile stop --out /绝对路径/新的stop-plan目录
python3 scripts/training_live_smoke.py --agent codex --profile stop --probe --out /绝对路径/新的stop-probe目录
python3 scripts/training_live_smoke.py --agent codex --profile stop --run --out /绝对路径/新的stop-live目录
```

`--probe` 仅发送 app-server `initialize` 与 `hooks/list`。它要求三个阶段都恰好有一个指定的项目 Stop hook，包含 disabled hooks 在内的总数必须为 1，无 managed/builtin、无额外命令、无错误或警告；精确定义的信任必须依次为 `untrusted → trusted → untrusted`。信任只由本次进程的参数提供。`--run` 在这个检查之后再用实际执行的覆盖参数复核一次；不匹配立即停止，只有通过后才开启这个新 toy 项目的本地采集并启动一次客户端。

这些检查共享 `-c` 参数：固定项目发现范围为当前 toy cwd，关闭 plugins、Apps、shell、搜索、记忆与多 agent，设置空 MCP 表和 12,000-token 预算。模型只收到固定的 `NOUS_STOP_OK` 回复任务。exec 另加 `--ignore-user-config`，保留既有认证；本机 app-server 不支持这个开关，因此报告明确记录 `loader_flag_difference`。inventory 是保留用户 TOML 时的来源快照，不是全部未来来源隔离的保证；空 MCP 表本身也不是对继承配置的通用清除操作。平台管理员限制继续生效。

专用 wrapper 只记录 Stop 事件是否收到、session/transcript 字段是否存在等布尔值，再把有界输入交给真正的独立采集 hook。成功判定检查本次缓存的 project、agent、session 和 transcript 来源绑定，授权必须是 `project_opt_in`，没有新增审核证据，最终回答精确匹配且没有工具调用；`read_health` 的成功计数与 `last_record_id` 也必须对应。目录存在或进程退出 0 均不算采集成功。

Stop 完成后只导出既有缓存并执行 validate/audit；CodeMode 使用 `codemode-events-v1`。`stop_passed`、`candidate_pipeline_passed`、`strict_sft_ready` 分别表示自动采集验收、候选数据集校验、是否已有严格训练行。未审核或上下文不全的候选可以得到 0 条 SFT，不能因此称训练已就绪。完整命令仅放在私有 `private/command-review.json`；本次 UUID 如存在只放在 `private/session.json`，不写入安全报告。

如果客户端已运行完，但测试脚本尚未导出数据集，可只整理已有报告和该次新会话，不再调用模型：

```bash
python3 scripts/training_live_smoke.py --finalize-existing /绝对路径/已有live目录
```

此命令保留既有 CLI 结果，只处理报告中该次 session；导出目录仍不能覆盖已有 dataset。

## 隔离与接入差别

**Claude Code** 使用 `--setting-sources ''`、`--restricted`、`--strict-mcp-config` 和专用 `--settings`/`--mcp-config`；关闭普通内置工具，只允许自有 MCP 工具免交互调用，禁止浏览器和 slash commands。专用设置的 Stop 直接调用项目的 `nous_training_hook.py`。`NOUS_HOME` 与 ledger 位于验收目录，只有 toy 项目被授权。管理策略仍然有效；脚本不会绕过管理员要求。

**Codex 的默认 MCP 模式** 使用 `exec --ignore-user-config --ignore-rules -s read-only --json` 和本次命令的 MCP 配置。关闭 shell、搜索、Apps、记忆、多 agent 和 lifecycle hooks；不使用 `--trust` 或 `--dangerously-bypass-hook-trust`。这是“真实客户端 → MCP → transcript → 显式导入”的验收，**不是 Codex Stop hook 验收**；Stop 使用上面的独立模式。

两边都保留本次会话持久化，便于核验真实 transcript。脚本仅通过本次新生成的 session UUID 查找匹配文件名，不读取其他历史会话正文。若当前客户端改为其他存储格式、没有匹配文件，报告 `transcript_found: false`；不以 stdout 拼造平台 transcript。

## 工具定义与证据

MCP server 收到客户端的 `tools/list` 后，把实际返回的结果保存为 `audit/tools-list.json`。它不是从工具调用参数反推的定义。完成本次 `tools/call` 后，`audit/mcp-call.json` 仅记录调用数量、工具名是否正确、参数是否为固定值，以及是否通过，不记录未知参数正文。

发现本次 transcript 后，如果其中是可核对的直接 function 调用，脚本以实际工具名关联 `tools/list` 的原始 `inputSchema`，写入 `context.json`，再使用 `collect(..., manual=True, context=...)` 导入。如果平台把调用包装在自定义 `exec`/Code Mode 中，则不把嵌套 lookup 的 schema 错挂到 exec；仍显式导入 canonical 候选，但不授予通过证据。因此：

- schema 来源是真实 MCP discovery，但当前通过测试专用 context 显式提供；没有证明通用工具定义自动采集已经实现。
- 不推测或支持 Claude/Codex 原生 Bash、文件、搜索工具的 schema。
- 系统提示等缺失上下文继续保留在 quality 中，不凭自定义测试提示补造完整模型上下文。
- Claude 的 `stop_captured_before_import` 在显式导入前检查本次来源绑定的独立缓存和健康成功记录。Codex 的 MCP 模式该值固定为 false，因为 hooks 明确关闭。

自动验收检查存储的 canonical 本身：恰好一个调用，参数为 `beta`；结果 ID 正确对应、未标为失败、结果实际包含 `{key: "beta", value: 37}`；末尾助手文本精确为 `NOUS_LIVE_OK beta=37`。完整性检查也须通过。只有这些条件都满足，才添加 `kind=verifier`、`result=passed` 和绑定当前 `record_digest` 的证据；不冒充用户或人工审阅。

然后导出数据集、运行独立 validator，并要求至少一条 SFT。该断言仅覆盖这个固定任务，不说明模型的一般正确率或完整平台兼容性。

CodeMode 候选导出自动选择 `codemode-events-v1`，不会使用普通 TRL profile，也不会因为合成任务成功而绕过 CodeMode 内容审核或补上通过证据。

## 如何读结果

| 字段/产物 | 含义 |
| --- | --- |
| `report.json` | 静态错误分类、退出码、运行时长、事件计数及各阶段结果。异常也保留失败报告。 |
| `model_run_attempted` | 客户端进程已尝试启动；不表示模型已接受请求或产生费用。 |
| `mcp_call.valid` | 固定工具在真实 MCP 连接上被正确调用一次。仅 tools/list 不算通过。 |
| `client_mcp_passed` | CLI 正常结束、真实 MCP 调用正确、最终输出符合固定任务；不等于训练转换通过。 |
| `stop_hook_verified` | 本次确实在显式导入前出现 Stop 采集缓存；配置存在不等于此值为 true。 |
| `schema_supplied_via_context` | schema 由本测试显式导入；不能宣传为自动接入全部工具。 |
| `canonical_verifier_passed` | 保存的 canonical 通过固定任务验收；不能只凭终端最终答案通过。 |
| `manual_import_pipeline_passed` / `passed` | 显式导入、审阅证据、SFT 导出及校验链通过；不包含 Codex hook 信任验收。 |
| `candidate_pipeline_passed` | canonical 候选已导出且整个数据集校验通过；SFT 可以为零，必须结合拒绝原因判断。 |
| `dataset/` | 如流程成功则生成常规训练导出文件，可再次使用 `nous_training.py validate --dataset` 检查。 |

原始 CLI stdout/stderr 不持久保存，因为启动事件可能包含用户配置元数据；报告只留下类型计数、摘要和预定义诊断码。诊断码表示输出匹配到相关词，不独立证明最终失败，仍需结合退出码和阶段结果。测试任务与工具内容是自有合成数据；canonical 也可能保留平台注入的指令或环境元数据，仍经过本地脱敏。报告失败时先处理明确的登录、网络、权限或适配问题，不无限重试。

## 本机首轮记录（2026-11-16）

已核对本机 CLI：Claude Code `2.1.319`，Codex `0.169.0-alpha.11.1`。

- Claude 首次进程在受限执行环境中约 0.61 秒退出 1；服务端收到真实 `tools/list`，没有 `tools/call`，没有 Stop 缓存或可定位 transcript。当时的 `auth status` 结果受该环境影响。后续正常本机权限核对确认已有登录，新的合成调用被额度/预算限制阻断，不能继续把首轮结果解释为用户未登录。
- Codex 首次进程在约 1.2 秒退出 1，无 session/工具事件；安全诊断为权限问题。该结果尚不能判断模型或 transcript 兼容性。
- 经允许使用正常本机权限后，Codex 第二次在约 43.22 秒正常退出；真实 `tools/list`、一次 `lookup(beta)` 和正确最终输出均已确认。其 native transcript 使用 `custom_tool_call(name="exec", input=JavaScript)` 包装 MCP 调用，并含 `additional_tools`、`configuration_update`、`world_state`、`token_usage_record` 等当前未归一的记录。程序保留真实 exec 代码和输出，不伪装成直接 lookup，也不静默丢弃新上下文。
- 对这一次 Codex 会话离线导入后，得到 1 条 canonical 候选、0 条 SFT，拒绝原因 `incomplete_trajectory`；独立 validator 通过。没有添加通过证据。它证明真实 MCP 通路和候选隔离可用，尚未证明该版本的训练转换或 Stop hook 接入完成。

整体不能标为“实平台验收通过”。Claude 的正常环境仍需可用额度才能完成真实工具任务；Codex 的专用事件适配器已经保留外层代码、目录与嵌套审计调用的区别，但真实会话的动态上下文仍可能阻断严格训练。真实 `tools/list` 是已经获得的定义来源，不能反推或替代外层自定义工具协议。未来复验需使用新的独立目录和报告。

## 临时 hook 信任探测修正（2026-11-16）

早期探测得到零 hooks 的原因已定位为测试参数编码错误：Codex 将 `-c` 左侧按点拆分，不会按 TOML 规则解开带引号的路径键。把项目路径或含点 hook key 写在左侧，会命中错误键；这不是已证实的平台发现缺陷。现在将特殊键放到右侧真正的 TOML inline table，例如 `projects={"/synthetic/project"={trust_level="trusted"}}`，并用同样方式指定 `hooks.state` 的精确 key。

本机 `0.169.0-alpha.11.1` 已在新专用目录实证三阶段信任变化：每次只发现一个指定 project Stop，均启用、非 managed、零错误/警告；移除进程覆盖后恢复未信任，fixture 字节不变，且无需 Git 仓库。使用完整新 Stop profile 的共享覆盖再次只读复核也通过。本次这些探测没有调用模型或写持久信任；实际 Stop 调用结果必须以独立 live 报告为准。[覆盖参数解析实现](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/config/src/overrides.rs)。

## 官方协议依据

- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference)：隔离设置、MCP、print 输出和预算参数；预算可能超出估算上限。
- [Claude MCP](https://code.claude.com/docs/en/mcp)：客户端 discovery 和完整工具名称。
- [Codex developer commands](https://learn.chatgpt.com/docs/developer-commands)：`exec --ignore-user-config` 保留既有认证，`--trust` 会持久保存信任。
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)：MCP 参数、feature 开关和实验 rollout budget。
- [Codex hooks](https://learn.chatgpt.com/docs/hooks)：多来源合并、信任与可为空的 transcript 路径。
- [MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)：`tools/list`、`inputSchema`、`tools/call` 与 `isError`。
