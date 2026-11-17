# Claude 本地原生请求回放证据

`hooks/nous_training_claude_replay.py` 对 Claude Code 2.1.318 / 2.1.319 日志中的 `api-request-shape`、`api-request-blob` 和 `api-request` 提供**纯内存、有界、摘要核验**解码。它不读写文件、不执行原日志命令、不发模型请求、不新增成功或人工审核标签。

```python
from nous_training_claude_replay import iter_replay
for result in iter_replay(immutable_authorized_bytes, session_id=expected_session):
    if result['kind'] == 'request' and result['status'] == 'recovered':
        # result['normalized_request'] 含原始敏感内容，只能在内存中交给脱敏器。
        # 不可直接打印、写 JSONL，或当作可训练样本。
        pass
```

调用前仍需完成来源授权、文件身份/快照校验；调用后需要脱敏、内容审核、训练资格审核与授权复查。返回的请求在原日志使用的规范化摘要口径下核验，不是原始 HTTP 字节。

## 本机实现依据

在本机已安装的 Claude Code 2.1.319 程序中检查了实际写入器 `rLt`、`appendRequestReplay`、`V7.noteLine`、`eLt` 和规范化函数 `m7e` / `Z0t` / `QDt`。它们记录：

- `shape={system,tools}`，递归移除 `cache_control`，按排序 JSON 求 SHA-256；
- 消息 blob 或 transcript 中同一消息的有序内容块，字符串 content 规范化为单 text block；
- `base` + `keep` 前缀和 `tail` 消息摘要；
- 另外保存的 `cacheControl` 容器路径和值；
- 其余保留的 `params` 和最终规范化 `requestDigest`。

解码器恢复路径后重新计算完整的规范化摘要，必须精确相等。原生 `h()` 逐键输出 `Object.keys(...).sort()` 的结果，按 UTF-16 排序；因此 `"10"` 也排在 `"2"` 前面，不经过普通 JS 对象再次枚举。解码器遵循这个规则。浮点数、超出 JS 安全范围的整数及孤立 UTF-16 surrogate 暂不支持，直接拒绝，不近似其序列化。

本机 4 个来源中的 78 个请求已在内存中重建并通过原生摘要检查。75 个有不歧义的 `requestRef` 响应消息关联；1 个请求对应多个 message ID，2 个无关联响应。停止原因统计是**原始响应片段数量**，不等于请求数或成功任务数。没有保存原始重建请求、消息或工具 schema。

## 不能据此宣布完整

原生写入器本来就排除了部分 billing system block，以及 `metadata`、`stream`、`thread`、`diagnostics`、`safeguards` 顶层字段。因此 `original_wire_complete` 始终为 false。`cut`、`cutRequestDigest`、`withheld` 不会被忽略；fork-context / content-replacement 等未实现上下文编辑也会阻断后续回放。

`requestRef` 证明原生日志中的引用关系；其响应检查只统计消息 ID、来源行号、停止原因等元数据，不还原完整响应字节，也不把 `end_turn` / `tool_use` 当作任务结果成功。`task_success_verified` 始终为 false。

实际请求可能含 role=system/developer 或其他扩展字段。解码会保留在摘要中绑定的字段，但不把这些扩展自动当成通用 Anthropic API 或训练格式兼容。恢复原始工具目录是具体证据改进，不会让整段 transcript 质量自动变为完整。附件、原有历史分支缺口、脱敏变化和任务验证仍须分别处理。

错误仅返回或抛出静态 `ReplayError` 码；调用者必须处理预扫描失败。第二遍达到资源限制时会产生一条阻断记录并停止，不能把停止当成其余来源已检查。有效请求 body 可含原日志中的敏感正文或推理字段，必须由调用者按既有隐私政策处理；不能把解码成功与脱敏成功混为一谈。模块没有持久化功能。

## 复制之前的资源限制

原始 snapshot 上限沿用 transcript 限制。规范化序列化按最多 4096 字符一段处理字符串，边输出边计数，不先生成一个可能超限的大字符串。每个 body 上限为 16 MiB、500000 个 JSON 节点和 64 层；单个键最多 65536 字符。请求先用只含引用的视图计算展开后的大小、节点数、缓存值和路径，全部通过后才复制消息。重复引用一个大 blob 也会按每次出现收费。

统一的保留预算同时涵盖 shape、blob、base 前缀引用、累计消息引用、请求/响应元数据索引与无效摘要隔离集合：128 MiB 的规范化 payload 加身份索引收费、2000000 节点、200000 引用、50000 索引项。另有最多 10000 个 blob、10000 个 shape、10000 个请求、100000 个消息/响应片段。这里的字节与节点是保守的计费单位，不是 Python 堆内存的精确测量。累计消息通过保留 blob 引用，不复制一份长期累计内容。

调用者应逐个消费生成器并及时释放正文；把所有返回 body 收集成列表的内存不由解码器管理。合成回归测试覆盖重复引用展开、深层嵌套、缓存值注入、shape/base 累积、消息前缀、无效索引增长和分块序列化；Python 3.11 与 3.14 均验证。
