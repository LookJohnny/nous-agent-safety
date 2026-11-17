# 实际多轮请求的 episode 与步骤监督

`nous.trajectory/4` 把一次任务中连续观察到的原生请求和响应保存为一个 episode。公开导出 profile 为 `request-episode-sft-v1`，底层 exchange 必须为 `nous.request-exchange/2`。a6 的 `nous.trajectory/3` 和 `request-sft-v1` 保持原来的单次最终文本范围；不会因为安装 a7 而悄悄接受工具过程。

本模块不执行工具、不补造缺失上下文，也不根据最终答案正确推断中间工具选择正确。默认只把最终 assistant 回复作为可候选的监督目标。每个中间工具决策还需要绑定该 episode 和步骤的独立审核；整个任务的结果审核与独立内容审核仍是导出的前置条件。

## 保留的结构

顶层 record 包含现有 `id`、`agent`、`messages`、`provenance`、`quality`、`privacy` 和 `evidence`，并新增：

- `schema: "nous.trajectory/4"`；
- `supervision_policy: "accepted_task_reviewed_steps"`，必须显式声明；
- `episode: {schema: "nous.request-episode/1", provider, steps}`；
- `step_evidence: []`，与整项任务的 `evidence` 分开。

每个 step 是闭合对象：

```text
{
  index: 从 0 开始的连续整数,
  exchange: 完整的 nous.request-exchange/2 实际快照,
  provenance: {
    project_id, session_id, session_group_id,
    request_sha256, response_sha256,
    request_content_sha256, response_content_sha256
  },
  quality: {complete, missing, issues},
  privacy: {scrubbed, scrubber_version, tool_history_checked, ...}
}
```

原始请求/响应字节摘要与保留的原生 JSON 内容摘要是不同绑定。`*_content_sha256` 由排序键、UTF-8、无多余空白、禁止非有限数字的规范 JSON 重算；`request_sha256`、`response_sha256` 由采集层在实际字节边界计算，不能从脱敏后的对象反推。纯模块检查内容摘要与来源一致性，但不声称密码学证明这些字节来自某个远程服务。

所有步骤必须属于相同 provider、project、session 和 session group。顶层 `provenance.request_sha256` 必须等于首步实际请求摘要。顶层 `messages` 只是最后一个 exchange 的 UI 投影，必须与 `request_messages(last_exchange)` 完全一致；训练字符串使用原生请求，不能以 UI 投影替代原生上下文。

## 连续性要求

首步不得带有未被本 episode 观察到的历史工具调用。每个非末响应必须是工具调用批次，末响应必须是完整最终文本。调用 ID、响应 ID、步骤编号及重复快照均检查；重复、跨来源、缺步和重排不会被自动修补。

下一次实际 request 必须包含上次 request 的完整历史前缀，随后是刚观察到的 assistant 输出和该批次全部原生工具结果。并行结果可以反序，但不得缺失、重复或混入下一项任务的用户指令。OpenAI 多步历史需要原生列表；不会把最初的字符串 input 猜改成消息列表。Anthropic 按实际 `{role, content}` assistant 消息以及紧随的 user 工具结果消息关联。

比较刚重新插入的 OpenAI response item 时，仅允许已知审计字段 `id`、`status` 的差异。`call_id`、namespace、名称、参数字符串、正文和 phase 不会忽略或重写；连参数 JSON 字符串的空白改写也不会猜测为相同原始调用。已经观察到的 request 历史前缀仍要求完整 JSON 一致，包括其审计字段。所有原始差异都留在实际快照中，比较不会修改快照。

Anthropic 的历史前缀和刚复制的 assistant 输出仅有以下比较兼容：在 `messages[].content` 位置，将字符串视为单个 `{type: "text", text: 原字符串}`；在该数组的直接 `text`、`tool_result` 块上，比较时忽略已由原生 schema 验证的 `cache_control` 缓存断点。官方 [Messages API](https://platform.claude.com/docs/en/api/http/beta/messages/create) 明确说明字符串与单 text 块的等价表示，并定义内容块的缓存断点。此处只改变连续性比较视图：实际 exchange、原始字节摘要、内容摘要、episode/step 摘要和训练样本均保留各次观察到的表示。移动缓存标记也会改变绑定实际快照的摘要，不能复用旧审核。

这不是递归删除字段或合并文本。文本内容、空白、多块边界、citations、工具 ID/名称/参数和错误标记仍精确比较；`tool_use.input` 中名为 `cache_control` 的用户键、工具结果的 JSON 文本及 `tool_result.content` 内部结构不会被改写或忽略。顶层 request 的 cache/settings、system 和 tools 仍受下述完整上下文要求约束。未知原生字段或无效缓存标记会先被 request adapter 拒绝。

request 中历史字段之外的设置也必须完全一致；模型、instructions、工具契约或其他设置变化会被视为上下文切换。工具结果的错误标记保留为原生上下文，不会根据退出码推断任务成功。首版只支持 function 调用和结果；custom CodeMode、隐藏推理、远程隐含状态等不因 episode 接口而开放。

## 审核规则与摘要

整项任务的 `human_review/accepted` 或 `verifier/passed` 只表示整个 episode 的结果证据，并绑定 `episode_digest(record)`。独立 `content_review/cleared` 或 `content_verifier/passed` 也必须绑定这个摘要。两者都具备，仍不会自动标注任何中间工具决策为正确。

中间步骤审核放在独立的 `step_evidence`：

```text
{
  kind: "step_review",
  result: "accepted" 或 "rejected",
  reviewer: 实际审核者选择的化名,
  episode_digest: episode_digest(record),
  step_digest: step_digest(record, index),
  step: index,
  at: 可选时间
}
```

只有实际完成步骤审核后才可以写入 accepted。最终 JSON oracle 不会替这些记录填写结果。一个步骤同时有正负审核时，负面审核阻断该步骤；它不会自动把其他步骤标成正确。对最终步骤的显式负面审核同样阻断最终行。整项任务的负面或冲突证据仍阻断整个 episode。

步骤证据不放入普通 `evidence`，避免把“任务最终完成但某个中间选择不合适”错误合并成同一种标注。旧审核、其他步骤、其他会话的摘要不能复用。导出器还会按最终训练样本内容检查跨来源冲突：某来源对步骤给出有效负评时，同一训练行不能借另一个来源的正评重新进入数据集。

`episode_digest` 绑定所有步骤的 index、exchange、quality、provenance、隐私门槛 `scrubbed/tool_history_checked`，以及顶层 UI 投影、质量、监督策略和来源身份。`step_digest` 绑定整个 episode 摘要和步骤索引。审核列表不参与自身摘要计算。隐私计数、scrubber 版本和顶层采集时间是观测元数据，不因二次脱敏刷新而使审核过期；实际正文、上下文、质量、身份或隐私门槛变化会使原审核失效。

## API 与训练行

```python
validate_episode(record, *, require_quality=True)  # 无返回值；失败抛静态 EpisodeError
episode_digest(record) -> str
step_digest(record, index) -> str
samples(record, *, require_quality=True) -> list[tuple[dict, dict]]
sample_for(record, {"step": index}, *, require_quality=True) -> dict
validate_sample(sample)
final_text(record) -> str
```

`samples` 输出 `(sample, {"step": index})`。sample 仅含 `prompt` 和 `completion` 字符串，使用 `nous.request-tool-sft/1` 序列化。prompt 是该步骤完整的实际原生 request；completion 是该步实际 assistant response 的原生目标投影，去除协议明确规定的审计元数据。工具结果只存在于后续 prompt，不作为 completion 标签。训练器必须屏蔽整个 prompt 的 loss，不能把它当成需要学习生成的文本。

纯模块的样本转换**不代替整项任务和内容证据检查**；应使用严格 exporter 的 `request-episode-sft-v1`。严格导出要求顶层及每一步质量完整、工具内容隐私检查完成、whole-task 和内容证据齐全；中间行还要求对应 step review。manifest 的 target 指向原 canonical episode 的步骤，离线 validator 会重新检查样本、审核和所有去重来源，不能裁掉不方便的来源。

`require_quality=False` 只给负面证据隔离路径使用：仍严格验证原生结构与连续链，允许没有最终文本的合法工具调用尾部，并返回已观察到的步骤目标。它不是训练不完整或未审核记录的开关。没有最终响应的合法 episode 可以保留为候选；缺失的原生内容不被补造。DPO 不支持此 profile，旧 profile 不能把 episode 扁平化后绕过这些门槛。

## 边界

每个 episode 最多 64 个步骤，完整输入最多 16 MiB、300000 个遍历节点和 64 层；对象键也计入遍历。超限直接拒绝，不截断。原生 exchange 另受请求适配器的契约、参数和资源限制约束。

采集层仍负责发送前冻结任务 oracle、授权、撤回 generation、原始字节观察、脱敏和落盘前复查。该模块不会访问文件、启动 CLI 或模型、执行工具、联网或写证据。结构通过不代表工具执行可回放、任意任务验证完成、模型效果提升或生产验收完成。
