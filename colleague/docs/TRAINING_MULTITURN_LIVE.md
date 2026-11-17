# 多轮客户端与真实 MCP 验收

`scripts/training_multiturn_smoke.py` 准备两条独立链路：Codex CLI → 本机 Responses fixture → stdio MCP，以及 Claude Code CLI → 本机 Messages fixture → stdio MCP。客户端和 MCP 进程是真实运行的；提供方输出是预先编写的合成响应，没有真实模型、付费推理或上游转发。这验证客户端工具协议和采集完整性，不能证明模型会正确选择工具或恢复错误。

## 任务与验收条件

唯一工具 `lookup` 只接受 `{"key":"beta"}`。第一次实际 MCP 调用返回 `isError:true` 及固定的临时错误 JSON；第二次返回 `{"key":"beta","value":37}`。工具不读业务文件、不运行命令、不访问网络；仅测试框架向自有 audit 目录写固定 schema 的计数和实际返回的 `tools/list` 契约。

提供方最多接受三个请求：首次返回工具调用；第二次必须看到第一次调用与错误结果的原生历史，才返回重试调用；第三次必须同时看到错误和成功结果，才返回 `{"answer":37}`。实际请求 catalog 必须包含且只包含一个匹配自有 MCP 契约的函数。Codex 自带其他原生函数，因此校验并完整保留实际 native function catalog 的名称/namespace/参数对象形状与唯一身份，不删工具目录。scripted provider 只能从唯一 fixture 契约选择调用名，所有后续请求中的实际调用也必须逐项匹配该 fixture；它不会调用其他函数。报告准确标记 `declared_context_preserved` 和 `fixture_calls_only`，不宣称整个目录只有一个工具。Claude 仍要求目录只有自有函数。不会凭配置文件认定 discovery 成功，也不会凭最终答案推断工具执行成功。

Codex 的工具输出在线上不一定含 MCP 的 `isError`，并会附加原生 wall-time/Output 说明。验收分别检查自有 MCP 的安全计数，以及 wire 中完整固定 JSON；只额外接受源码明确的有限耗时说明＋一个 JSON 文本块（或对应完整字符串），不接受任意前后缀或 substring 命中。此说明保持在实际请求中，不向原请求补造错误标志。Claude 原生 `tool_result.is_error` 必须保持正确。缺少结果、调用 ID/名称/namespace/参数错配、契约变更、非支持的工具类型或未完整回放历史都会停止 fixture 推进。

## 先准备计划，再显式运行

默认只打印静态计划，不创建目录或启动进程：

```sh
python3 scripts/training_multiturn_smoke.py --agent codex
python3 scripts/training_multiturn_smoke.py --agent claude
```

审阅后，每个平台使用独立进程和全新绝对输出目录：

```sh
python3 scripts/training_multiturn_smoke.py --agent codex --run --out /private/tmp/nous-multiturn-codex-new
python3 scripts/training_multiturn_smoke.py --agent claude --run --out /private/tmp/nous-multiturn-claude-new
```

可通过 `--executable /absolute/path/to/cli` 指定可执行文件。当前已核验本机 Codex `0.169.0-alpha.11.1` 与 Claude Code `2.1.319`，并不承诺所有版本兼容。具体结果以各次独立报告为准。

每个进程外层最多 90 秒、3 个提供方请求、2 次受允许的 MCP 调用；stdout/stderr 各限 2 MiB，HTTP 正文限 8 MiB，并限制读取时间。服务只监听 `127.0.0.1`，只接受对应 API 路径；Claude 兼容 SDK 的固定 `?beta=true` 选择器，不接受其他 query。任何重复、鉴权异常、压缩、转发编码、未知路径或非法请求长度都会拒绝。

## 进程隔离与凭据

Codex 沿用单轮 fixture 的静态 catalog、自定义 unauthenticated Responses provider、禁用 WebSocket/重试和请求压缩，使用 `--ignore-user-config --ignore-rules --skip-git-repo-check --ephemeral`。只读 sandbox 下只配置自有 stdio MCP；进程级 `projects` 覆盖只信任新建的 fixture 项目，不写 `--trust` 或持久配置。hooks、apps、plugins、shell、memory、多 agent 和其他 MCP 保持关闭。token 预算为 12000。

Claude 使用 `--bare --restricted`、空 setting sources、显式自有 settings/MCP 文件、`--strict-mcp-config`、空内置 tools、仅允许自有 MCP lookup、`dontAsk` 与 `--permission-prompts none`。不保存会话、不加载技能命令，不连接 Chrome。其新的 `CLAUDE_CONFIG_DIR` 仅位于 fixture 的 private 目录。`ANTHROPIC_BASE_URL` 指向本机服务，`ANTHROPIC_API_KEY` 只使用公开的非秘密 dummy 值；服务严格校验这个 dummy，并拒绝 Authorization 和其他 API key。

子进程环境采用最小 allowlist，保留既有 `HOME` / `CODEX_HOME`，不继承真实 auth token、API key、其他云提供方配置、代理或额外请求正文变量。Claude `--bare` 的本机帮助明确不读取 OAuth/钥匙串；非必要流量、实验 beta、工具搜索和 thinking 参数另显式关闭。保留管理员限制，不通过 bypass 绕开策略。这些配置不是对客户端所有后台网络活动的全面审计证明；出现管理策略或版本阻断时记录失败，不改变真实配置追求通过。

Claude 的 `--max-budget-usd 0.75` 是额外客户端限制；本机 fixture 本身没有真实计费模型。脚本没有真实服务模式，不会把失败请求转发给官方 API。

## 请求级采集与报告

通过 `nous_training_episode_capture` 的 `begin_episode`、`begin_step`、`finish_step`、`finish_episode` 观察同一 episode。oracle 模板在客户端启动前冻结；实际请求摘要只能在 fixture 收到请求后绑定，所以注册时点如实为 `before_response`。每轮使用原始实际请求字节及实际发出的相同 SSE 响应字节，不重写请求来适配 schema。未知语义由适配器拒绝，仍可报告客户端链路的独立结果。

最终以 `nous.trajectory/4` 保存脱敏候选，用 `request-episode-sft-v1` 导出和离线验证，序列化契约为 `nous.request-tool-sft/1`。工具步骤保持未审核；任务答案匹配并不自动认可中间决策，也不替代内容审查。脚本不添加人工通过证据，`strict_sft_ready` 不会因工具执行成功就自动变成 true。

`report.json` 分别给出：

- `fixture_transport_passed`：三个实际请求/响应及客户端最终成功结束。
- `mcp_recovery_passed`：真实 discovery、两次调用、一错一成功，以及后续请求的实际结果回放均匹配。
- `episode_capture_passed`：完整 episode 已存储，导出的一个候选通过 validator。

原请求、CLI 输出、header 和原生用户身份不进入报告。报告只保存固定字段、计数、布尔值和静态诊断；候选正文只能由授权的隐私管线输出。`private/` 内状态及 `command-review.json` 供本机核对，不能当安全分享报告。`audit/` 的内容仅为自有工具固定契约和执行计数。

`capture_errors` 只输出精确 `EpisodeError`、`RequestError`、`ObserverError` 类且命中硬编码 allowlist 的错误码，其他异常统一隐藏。工具目录诊断只给固定类型计数、已知 builtin 布尔和 fixture 数量，不复制实际工具名称或描述。`context_changes` 给固定顶层字段变化、metadata 变化计数/布尔及历史 prefix 对比；去除 cache 或特定 transport 元数据的对比**仅用于诊断**，不会修改任何实际请求、绕过 episode 校验或放行训练。工具调用语义 ID 从不作为可忽略 metadata 删除。

2026-11-16 的最终本机 d 轮结果：

| 客户端 | 提供方请求 / 已采集 steps | 实际 MCP 调用 | canonical / SFT | validator |
| --- | --- | --- | --- | --- |
| Codex | 3 / 3 | 2：临时错误 1、成功 1 | 1 / 0 | valid |
| Claude Code | 3 / 3 | 2：临时错误 1、成功 1 | 1 / 0 | valid |

两者均 `fixture_transport_passed`、`mcp_recovery_passed`、`episode_capture_passed=true`，episode 为三步且完整；各有 1 条 rejected 记录，未审核内容/工具步骤不会进入 SFT。没有新增人工审核标签。Codex 实际 catalog 有 9 个 function，均保留在请求中，只有 fixture 被调用；真实输出使用已核验的 wall-time 数组包装。Claude 的早期失败经闭合诊断确认是相同文本的单 text block 与 string 别名，以及合法 cache 标记位置变化；只在 episode 比较视图处理这两种表示，全部实际 snapshot 和摘要仍保留原貌。这里只验证 scripted provider 的客户端链路，真实模型能力与真实提供方可用性是独立结论。

## 真提供方验收的独立计划

真实模型对工具的选择能力需要另一个实验。本轮不会自动运行它，可在授权和预算审阅后，单独使用既有 `training_live_smoke.py` 的 Claude MCP profile：

```sh
python3 scripts/training_live_smoke.py --agent claude --profile mcp --out /private/tmp/nous-real-claude-mcp-new
```

此命令先生成计划；显式增加 `--run` 才启动真实服务调用。它使用既有 CLI 登录、仅固定合成 lookup 任务、一次进程、外层 90 秒、Claude 预算上限 0.75 美元，结果与本机 scripted provider 报告分开。不能用此脚本的本机通过结果替代真实模型验收，也不能为了认证/额度/策略失败扩大授权范围。

## 官方依据

Claude 的 [环境变量文档](https://code.claude.com/docs/en/env-vars) 说明 API endpoint、API key、独立配置目录和关闭非必要流量；[CLI reference](https://code.claude.com/docs/en/cli-reference) 给出 bare、显式 MCP 和禁用持久会话等入口。[网关连接](https://code.claude.com/docs/en/llm-gateway-connect) 与 [网关兼容协议](https://code.claude.com/docs/en/llm-gateway-protocol) 说明请求格式和 beta 能力是协议的一部分，不能通过删除未知字段伪造上下文完整。

Codex 原生 `FunctionCall` 的 `name` / `namespace` 和工具结果 body 来自固定公开 [protocol models.rs](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/protocol/src/models.rs)；其 failure serialization 测试说明内部成功标志不一定进入 wire body。[spec_plan.rs 的资源工具注册](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/tools/spec_plan.rs#L1322-L1342) 在存在 MCP server 时加入三个资源 handler，因此静态 model catalog 的空 experimental tools 列表不是关闭它们的证明。[McpToolOutput 的响应包装](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/tools/context.rs#L234-L264) 定义 wall-time header 的准确形状。测试保留完整真实上下文，只限定自身输出的调用。公开固定 commit 不证明本机 binary 与它完全一致，因此本机 fixture 仍独立核对实际请求。

Claude [Messages API](https://platform.claude.com/docs/en/api/http/beta/messages/create) 明确 `content` 字符串是单 text block 数组的简写；这不授权合并多个文本块、改写工具参数或忽略未知 metadata。

离线测试命令：`python3 -m unittest tests.test_training_multiturn_smoke`。这些测试使用合成字节和 mock，不启动平台 CLI、socket 或模型。
