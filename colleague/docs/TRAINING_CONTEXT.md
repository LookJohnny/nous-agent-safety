# Codex 原来源上下文诊断

`inspect-source` 只读检查用户明确指定的一个 Codex JSONL 文件。它回答“这个文件实际记录了哪些上下文线索，以及哪些信息仍不能证明”，不会修复轨迹、填充提示词或生成训练样本。

```bash
python3 hooks/nous_training.py inspect-source --transcript /绝对路径/本次会话.jsonl
```

指定路径即授权本次读取这个文件；命令不会搜索历史会话，不启用项目采集，不创建 Nous 或 ledger 状态，不写回来源。文件必须直接指向普通文件；直接符号链接、管道和目录会被拒绝。报告输出到 stdout。实现只调用一次受限稳定读取，后续分析使用同一份不可变 bytes，不将原始正文写入临时文件。

退出成功仅表示已完整扫描当前支持版本的来源，并生成结构有效的诊断。已观察到的上下文缺口不会单独导致命令失败。未知或缺失版本、混合会话、非法数值/存储顺序、损坏 JSON、资源超限会使 `valid=false`。当前已观察版本是 `0.169.0-alpha.11.1`；文件自报版本不证明实际生产程序身份。

## 报告字段

报告 schema 为 `nous.training.context/1`。所有文字字段来自固定集合；不回显正文、路径、会话 ID、工具名、未知版本值或来源中的 world-state 哈希。

| 字段 | 含义 |
| --- | --- |
| `valid` / `status` / `scan_complete` | 本次诊断的有效性、结果和完整扫描状态；不表示数据可训练。完整扫描但版本不支持时，`scan_complete=true`、`valid=false`。 |
| `source_sha256` / `source_bytes` / `source_lines` | 整份读取字节的 SHA-256、字节数与物理行数，绑定本次输入快照。它不是 canonical `provenance.source_sha256` 所用的脱敏消息摘要，也不是匿名化。 |
| `runtime` | 版本是否落在已观察 profile、版本与会话身份是否一致。身份值只在内存中比较，不输出。`producer_identity` 始终为 `unverified`。 |
| `ordering` | 存储 ordinal、history sequence、采样 checkpoint 的数值及一致性观察。数值必须是非 bool 的 `u64`；可选字段的 null 按缺失处理。 |
| `observed_context` | base 指令、原生 system/developer 消息、工具目录、配置更新、full/patch 状态、turn context、线程设置及未支持事件的计数。 |
| `metadata_presence` | 固定 history metadata 字段出现次数，包含显式 null；不复制字段的任意正文。 |
| `reference_observations` | 原生文本/工具对象与安全引用的同文件匹配计数。语义固定为 `whole_source_cooccurrence_no_temporal_effectiveness`。 |
| `targets` | 最多 128 个可见助手输出定位点：从 1 开始的物理源行、固定 native 类型、合法 history/checkpoint 数值及观察关系。不是请求边界或导出的训练 target 区间。 |
| `errors` / `gaps` / `actions` | 静态诊断码及下一步类别：定位已有观察、重新读取原来源、捕获请求级证据、实现版本化语义、进行内容和结果审核。 |

所有报告都固定保留：

```json
{
  "request_context_verified": false,
  "request_replay": "missing_evidence",
  "training_eligibility": "not_evaluated"
}
```

因此，即使检查得到连续 sequence、所有引用都有相同内容、存在完整工具目录和最终回答，也不会解除任何 canonical quality、内容审阅或任务结果验证要求。诊断不修改这些字段或证据。报告的整源指纹可以关联同一原文件，保存或共享报告时仍应按来源元数据处理。

## 采样序号的边界

已核查的公开源码将 `history_sequence` 与 `sampled_through_sequence` 定义为可选 `u64`。前者是线程内部历史序号，后者是已产生输出的普通请求曾包含的最高输入 checkpoint；压缩可保留旧的历史身份。报告因此将 history 的重复、非单调和 checkpoint 未在此前观察中出现，作为计数而非“日志损坏”的判定。存储 ordinal 的类型与严格递增检查独立进行。

`observed_before` 只表示相同序号在此前物理行出现过；`ahead_of_observed_history` 只比较此前观察到的最大值。它们不能证明完整输入成员关系。空缺、重放、压缩和导出截取都可能影响这种观察。零 checkpoint 单独标记，不推断它引用一条具体输入。

公开实现先保存采样 checkpoint，再执行 `history.for_prompt`、工具调用关联、prompt 构造、guardian 处理和工具 fallback 预留。因此，即使全部序号关系都正常，也不能直接把“小于等于 checkpoint 的全部日志”当成实际请求。[history 定义](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/history/src/lib.rs#L58-L175)、[采样维护](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/context_manager/sampling.rs)、[请求构造](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/session/turn.rs#L2002-L2041)。该固定源码提交不是本机二进制构建来源的证明。

## 引用匹配的边界

`base_native_exact_alias_count` 统计 base 文本与原生 developer 单文本块的字面相等次数。它不去重，不把 base 自动转成 system，也不改变模型消息角色。

`model_instructions.exact_match` 只比较已经观察到的原生 system/developer 单文本块，metadata base 本身不会被拿来证明原生片段存在。匹配使用已核查的 fragment 指纹算法：固定域分隔符、role/text 的 CRLF 规范化和长度编码后 SHA-1。[指纹实现](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/context/world_state/mod.rs#L427-L451)。

工具引用只比较允许确定复现的 JSON 指纹：排序对象键、紧凑 UTF-8 JSON；namespace 拆为去掉 `tools` 的 header 和原始 child 定义。浮点数、超出 serde 整数范围或嵌套 namespace 不猜测序列化，计入 `uncomparable_tool_definitions`。[工具引用构造](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/context/world_state/top_level_tools.rs#L71-L97)。

这些匹配扫描整份来源，可能命中后续才出现的文本或旧版工具定义；它们没有时间生效含义，也不证明某一 target 使用了该目录。没有匹配也不证明正文一定缺失：某些指纹可表示未渲染的默认状态。`model_catalog` 当前仅统计为 opaque 引用，不生成提示词，不把空目录状态误写成模型已接收的撤销指令。full/patch 只计数，不应用 merge patch 或解析完整状态。

## 资源与失败行为

- 复用来源读取上限：128 MiB、每物理行 8 MiB、100 万物理行；拒绝读取期间的增长、截断或替换。
- 最多 128 个可见助手定位点、10 万个跟踪观察、256 KiB 报告。超过任一上限，整份诊断无效；不返回看似完整的前缀定位点。
- 重复 JSON 键、NaN/Infinity、指数溢出为非有限值、无效 UTF-8/孤立 surrogate、未完成尾行均拒绝。失败只输出静态原因。
- 格式或上限失败会清空部分观察及 targets；若已经得到稳定源快照，仍可保留整源摘要与字节/行数。读取失败则这些绑定字段为 null。
- 隐藏 reasoning/analysis 及 UI 镜像不会成为助手定位点。未知可见事件只产生固定计数或缺口，不能提升为支持的请求内容。

## 下一阶段需要的证据

本功能给下一步请求级采集提供来源摘要、物理位置、可见类型和 checkpoint 线索。它不尝试用现时文件、工具清单或事后人工描述修补历史请求。要实现严格请求重放，仍需在实际请求构造完成的位置记录：该次请求真实选中的输入、有效工具契约、动态上下文的 materialization/过滤结果，以及 target 与该请求的可信关联。新证据还需经过独立授权、脱敏、版本化校验和摘要绑定。

仅从旧 canonical 做解释仍有价值，但它已经丢弃部分 history metadata 与线程设置正文，无法补回这些证据。来源诊断不会改写旧 canonical 的缺口，也不保证列出所有未来训练适配问题。
