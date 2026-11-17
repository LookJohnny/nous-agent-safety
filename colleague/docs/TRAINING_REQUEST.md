# 原生请求与响应训练入口

首版使用 `nous.trajectory/3` 保存经过授权和脱敏的单次 API exchange，训练 profile 为 `request-sft-v1`。请求条件按平台原生结构保留，不伪装成完整模型提示词，也不把 OpenAI `instructions` 猜成 system 或 developer 消息。

a7 新增显式 `nous.request-exchange/2` 以观察原生 function 工具历史和等待工具的响应。它供独立 `nous.trajectory/4` episode 入口使用，不改变上述 v3/profile 的含义。以下首版支持表仍描述 exchange/1；工具版本的边界见后文。

该层不发送网络请求、不执行工具、不生成审核证据、不创建持久状态。可信集成应在最终请求字节发送前冻结输入，在实际响应完整接收后调用采集器。导入一份自称 `complete=true` 的 JSON 或计算文件哈希，均不能证明它实际发送给了服务端。服务端内部模板和隐藏上下文不在此观测范围内。

## 闭合观察结构

```json
{
  "schema": "nous.request-exchange/1",
  "provider": "openai-responses",
  "transport": "json",
  "request": {
    "model": "synthetic-model",
    "input": [{"role": "user", "content": "Return exactly {\"answer\":4}."}]
  },
  "response": {
    "id": "synthetic-response",
    "object": "response",
    "model": "synthetic-model",
    "status": "completed",
    "output": [{
      "type": "message",
      "role": "assistant",
      "status": "completed",
      "content": [{"type": "output_text", "text": "{\"answer\":4}"}]
    }]
  }
}
```

这是手写合成示例，不是一次真实请求或任务成功的证据。所有对象采用明确字段集合；未知字段和未支持语义均静态拒绝。缺省、显式 null 和实际值不互相补写，数组保持原顺序。

支持的 provider 为 `openai-responses` 和 `anthropic-messages`。两者保留自己的请求键、内容块和工具定义，不经过同一个虚构的 chat 模板。`transport` 可为 `json` 或 `sse`；SSE 的事件闭合和顺序由独立传输适配器验证，本模块只接受已经收敛的完整原生响应。`sse` 必须对应请求中的 `stream=true`；该字符串本身不构成流完整性证据。

## 当前支持范围

| 维度 | 首版行为 |
| --- | --- |
| OpenAI 输入 | 完整显式 `input` 文本或文本消息列表；原生 system/developer/user/assistant 角色；顶层 `instructions` 原位保留。 |
| Anthropic 输入 | `messages` 中 user/assistant 文本与顶层原生 `system`；不支持 assistant prefill。更广 API 的角色/内容能力不代表本适配器已经支持。 |
| 工具定义 | 支持保守 JSON Schema 子集的 function 定义；OpenAI namespace、custom 格式定义及已知 `additional_tools`/`configuration_update` 可以作为有序请求内容保留。定义出现不表示工具已执行。 |
| 工具执行 | 所有真实 function/custom call、output、Anthropic tool_use/tool_result/server_tool_use 历史均拒绝为 `request_tool_history_not_supported`。原生文件工具的完整排除规则接入之前，不开放此路径。 |
| 最终响应 | 只支持一个助手文本结果、一个文本块。OpenAI 顶层及消息都必须 completed，且无 error/incomplete_details；Anthropic 必须 end_turn，且无额外 stop_sequence。 |
| 请求设置 | 仅已实现字段及类型；保留实际明确设置。`tool_choice` 仅 auto/none，结构化输出格式、强制工具选择等未实现配置拒绝，不静默删除。 |
| 远端状态 | 拒绝 previous_response_id、conversation、远端 prompt 引用、context management、container/MCP 等有值配置；拒绝 background=true 和 truncation=auto。 |
| 隐藏/扩展内容 | 不采集 reasoning/thinking 正文、加密 reasoning、媒体、非空引用注释、服务端工具输出和未知转换。CodeMode 的真实 custom 调用不能强塞进 v1 或本 profile。 |

请求中的 reasoning effort 枚举可以作为明确配置保留；它不授权保存隐藏思维正文。Anthropic thinking 首版仅支持显式 disabled。因格式/终态/语义不支持而拒绝的内容不会通过删除字段变成“完整”交换。

Codex 固定版本 `5cb38e79842a0e0a6dca5bb97874163e8ee50129` 的原生请求包含 `client_metadata` 字符串映射，并无条件设置 `include=["reasoning.encrypted_content"]`。此处原样支持该单一选择器，以及缺失/null/空 include；它只是返回字段请求，不是已存在的隐藏内容。实际 input/output 仍不得出现 reasoning、encrypted_content 或 summary 内容。`client_metadata` 使用独立的有界字符串映射限制并接受敏感字段脱敏；具体上限见本页末尾，公共 `metadata` 的限制保持不变。[固定请求结构](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/codex-api/src/common.rs#L303)、[固定请求构造](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/client.rs#L1228)。

顶层 reasoning.summary 可原样保留 auto/concise/detailed/none 或显式 null，不补缺省值；请求设置不等于响应摘要正文。reasoning.context 及消息内 configuration_update 的 summary 仍未支持，不删除后重试。枚举来自官方配置类型，固定版本构造仅在允许 summary 且不为 none 时发送，本机 fixture-c 也实际观察到 auto；这项观察不证明真实模型执行。[配置枚举](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/config_types.rs)、[固定 summary 构造](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/client.rs#L1110)。

本机 fixture 报告仅增加固定字段存在性、类型/数量、已知 reasoning 设置枚举桶和适配器静态错误码。未知字段名、元数据值、请求正文和任意异常文本均不写入安全报告；这些诊断不改变出站请求，也不证明模型实际运行。

OpenAI API 本身允许远端 conversation 追加输入、previous response 链和自动截断，并将 `instructions` 描述为 system 或 developer 级指令，所以本 profile 有意限制这些入口并保留原生位置。[Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)。Anthropic 的 top-level system、prefill、stop reason 和流式最终消息也有独立语义；这里只实现上表子集。[Messages API](https://platform.claude.com/docs/en/api/messages/create)、[流式消息](https://platform.claude.com/docs/en/build-with-claude/streaming)。

## Python API 与投影一致性

实现位于 `hooks/nous_training_request.py`：

- `validate_request(provider, request, *, schema=SCHEMA)`：发送前检查支持范围，返回独立副本，不插入默认值。默认仍为 exchange/1；工具入口必须明确传 `TOOL_SCHEMA`。
- `normalize_exchange(provider, request, response, *, transport='json', schema=SCHEMA)`：返回闭合、独立的 exchange 副本；不根据正文自动升级版本。
- `validate_exchange(exchange)`：只验证，不修补。
- `request_messages(exchange)`：生成旧界面的可见消息投影。顶层 instructions/system、工具目录和配置仍在原生 exchange 中，不将这些内容编造成消息角色。
- `validate_record(record, *, require_quality=True)`：验证 v3、exchange、可见投影一致性及脱敏请求摘要；构建过程的授权和 privacy envelope 由外层采集器负责。
- `sample_for(record, *, require_quality=True)`：额外要求 `privacy.scrubbed=true`，产生严格 prompt/completion 样本。
- `validate_sample(sample)`：独立重新解析规范序列化，检查请求与助手目标形状。

v3 中 `messages` 必须逐值等于 `request_messages(exchange)`。不能修改展示回答让 verifier 通过，却训练另一个 exchange 回答。`provenance.request_content_sha256` 必须等于脱敏 `exchange.request` 的规范 JSON SHA-256；规范 JSON 为 UTF-8、排序对象键、紧凑分隔符、保留 Unicode、禁止非有限数。它与实际请求字节的 `request_sha256`、实际响应字节的 `response_sha256` 含义不同，不可混用。

严格 quality 要求 complete=true、issues=[]、missing=[]。v3 不使用 v1 的缺失 system/tool 定义豁免。独立内容审核、任务结果证据、撤权和跨记录冲突仍由外层 exporter/validator 执行；本模块不从响应状态、HTTP 200、用量或一次工具返回推断任务成功。

## SFT 的监督边界

每个样本只有 `prompt`、`completion` 两个字符串。prompt 为一行规范 JSON 加换行：

```json
{"schema":"nous.request-sft/1","provider":"openai-responses","request":{"model":"synthetic-model","input":"synthetic request"}}
```

上例展示字段含义；实际输出统一排序键。completion 的结构为 `{"response": ...}`，只包含实际助手输出：OpenAI 使用 output/message/content，Anthropic 使用 role/content。响应审计 ID、status、usage 不作为训练目标；它们仍保留在 exchange 中供摘要绑定和终态检查。

历史 assistant 文本只进入 prompt，不能因它出现在请求历史中就变成额外正例。训练程序必须采用 completion-only loss，检查 token 边界且禁止截断。样本序列化不是 OpenAI 微调上传格式，也不是 Anthropic 的内部模型模板。

## 资源和失效规则

单个交换/样本最大 8 MiB，容器最多 4096 项，结构深度最多 64；错误只使用静态 `RequestError` 码。重复 JSON 键、非有限数、孤立 Unicode surrogate、未知语义和不完整终态均失败。模块不执行采集到的工具、代码、schema 中的命令或提示词指令。

脱敏必须同时覆盖 exchange 全部允许值和 UI 投影。工具契约的敏感 default/examples/enum 不能只因 schema 合法就保留；契约发生语义变化时必须保留质量缺口或拒绝采集，内容审核不能替代这一检查。脱敏后需重建投影、重算请求内容摘要；旧结果审核不会自动重新签名。

最短验收路径是：显式授权的单个最终文本请求 → 真实完整响应 → 脱敏 v3 候选 → 预先注册且绑定原请求的客观 JSON 结果检查 → 独立内容审核 → 严格导出并从 canonical 重建校验。手写 fixture 或 mock HTTP 只验证软件链路，不能标记成真实模型运行。该 API 入口也不表示已经截获现有 Claude Code 或 Codex 桌面会话的请求。

## a7：原生 function 工具交换

工具版仍使用五个顶层字段：`schema/provider/transport/request/response`，其中 schema 必须是 `nous.request-exchange/2`。完整的原生请求和响应保留原顺序、参数字符串、call ID、namespace、错误标志及明确设置；没有缺省补写。请求内的历史工具调用必须全部有结果，本次响应可以是最终文本，也可以是等待客户端执行工具的完整调用批次。这里的完整只表示观察到完整响应，不表示工具执行完毕或整个任务成功。

| 规则 | exchange/2 行为 |
| --- | --- |
| OpenAI 调用 | 支持 function_call 的 call_id/name/arguments，可保留 namespace 和审计 id/status；arguments 必须是无重复键的 JSON object，并匹配当时可见 function schema。 |
| OpenAI 结果 | 支持 function_call_output 的 string 或纯 input_text 数组，按 call_id 关联；若带 name/namespace，必须与对应调用一致。结果正文中的 error、success 或 isError 字样不被转成原生错误标志。 |
| Anthropic 调用 | 支持助手 tool_use；响应 stop_reason 必须 tool_use，最终文本仍必须 end_turn。可保留同一助手消息内的可见文本块。 |
| Anthropic 结果 | 下一条 user 消息必须先包含上一批次的全部 tool_result，然后才可有文本。保留显式布尔 is_error，允许 API 中省略 content 或空文本结果。 |
| 并行与重试 | 同一批次结果可以逆序。重复 call ID、重复/孤立结果、结果先于调用，以及未闭合批次中混入新调用均拒绝。失败后重试必须使用新的 call ID。显式禁用并行或 tool_choice=none 与响应调用矛盾时拒绝。 |
| 定义顺序 | 顶层 tools 作为本次明确目录；有序 additional_tools 必须先于使用，未定义名称和不明确的 namespace 均拒绝。相同定义重复出现可保留；冲突重定义拒绝，不拿新版 schema 回填旧调用。跨请求目录是否真的相同由 episode 链验证。 |
| 不支持内容 | custom/exec/CodeMode 实际调用、server tools、隐藏或加密内容、媒体、未知语义仍拒绝。工具定义存在不等于允许执行；本模块不会执行任何工具。 |

调用关联与错误处理参考平台原生格式，不把 OpenAI 的输出字符串当作 Anthropic 的 is_error。[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)、[Anthropic tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)。

新增纯函数接口：

- `exchange_state(exchange)`：返回 response_kind（final_text/tool_calls）、pending_call_ids、history_call_ids、error_call_ids 和 history_tool_result_count。error_call_ids 仅来自 Anthropic 显式标志。这些 ID 是绑定所需的来源值，不应进入无正文的公开诊断。
- `response_target(exchange)`：返回实际助手响应的训练投影。OpenAI 去除审计 item id/status，但保留 call_id/namespace；Anthropic exchange/2 保留 stop_reason。验证下一请求的实际历史前缀必须用原始响应，不可用这个投影替代。
- `sample_for_exchange(exchange)`：只接受 exchange/2，生成 prompt/completion 字符串。prompt 的 schema 为 `nous.request-tool-sft/1`，另含 provider 和原生 request；completion 为 `{"response": ...}`。
- `validate_exchange_sample(sample)`：重新解析规范序列化，独立校验请求、目标、工具定义和关联。它不验证跨请求来源，也不授予训练资格。

旧 `validate_record/sample_for` 继续只接受 v3 + exchange/1；把 exchange/2 装入 v3 会得到 `request_tool_exchange_requires_episode`。公开训练入口由新的 `request-episode-sft-v1` 管理，不能用纯转换函数绕过 episode 的授权、文件排除、质量、原始请求链、独立工具步骤审核及整个任务结果审核。工具结果只属于下一请求的上下文，不作为助手生成目标；一个最终 JSON 检查通过也不等于中间工具选择已被审核。

实际参数匹配使用进一步限制的 schema 子集：拒绝 pattern 和 uniqueItems=true，以避免执行任意正则或二次复杂度去重；单参数对象最多 256 KiB，每次交换最多 128 个不同调用，schema 与参数各最多 4096 个值节点、节点乘积最多 250000。超限整份拒绝，不截断为合格样本。仅声明、从未调用的 a6 工具 schema 兼容范围保持原样。以上测试使用合成数据；实平台接线、脱敏与任务链的验收由外层流程另行记录。

`client_metadata` 是 Codex 自有 string map，不套用公共 `metadata` 每值 512 字符限制。当前实现限制 64 项、key 128 字符、value 16384 字符、整个 map 128 KiB，并受总交换 8 MiB 上限约束；原样保留经脱敏的字段，不解析其值中的私有编码。公共 `metadata` 限制保持不变。
