# 本地最终 JSON 结果验证

`hooks/nous_training_verify.py` 是一个无文件读写、无网络调用、无命令执行的纯验证器。它检查**预先明确的最终 JSON 交付物**，返回报告和证据候选；不会修改轨迹、审核、隐私状态或训练资格。首版仅支持完整的 `nous.trajectory/3` 请求级记录。

它不运行对话里的命令，不通过一次工具退出码推断整个任务成功，也不能用模型自己声称“测试通过”的 JSON 来证明代码、文件、网页或外部系统实际正确。要使用 `final_json_task`，任务的完整交付要求必须就是这个独立给定的最终 JSON 值。其他效果还需要独立验证。

## 两种验证范围

| `scope` | 必需规则 | 能产生的证据候选 |
| --- | --- | --- |
| `final_json_task` | 至少一条完整 `exact_json`，可再加 `json_schema`；规则之间必须一致 | 完整匹配为 `verifier/passed`，可判定的不匹配为 `verifier/failed`；不可判定不产生任务证据 |
| `final_json_format` | 至少一条 `exact_json` 或 `json_schema` | 仅 `result_check/matched`、`mismatched` 或 `indeterminate`，不能代替任务结果证据 |

格式不匹配使用 `mismatched`，不会误用现有导出器会视为负面任务标注的 `failed`。验证器也不会产生 `content_verifier`：内容可以被导出、用于训练的审核与结果是否满足 oracle 是独立问题。

## Oracle 契约与 API

```json
{
  "schema": "nous.oracle/1",
  "scope": "final_json_task",
  "request_sha256": "实际发出请求字节的64位小写SHA256",
  "assertions": [
    {"kind": "exact_json", "expected": {"answer": 4}},
    {"kind": "json_schema", "schema": {
      "type": "object",
      "properties": {"answer": {"type": "integer"}},
      "required": ["answer"],
      "additionalProperties": false
    }}
  ]
}
```

上例中的摘要说明文字是占位符，实际调用必须替换为真实的小写十六进制摘要。所有顶层和 assertion 字段闭合：未知字段被拒绝。没有“忽略额外字段”或“只比较答案的某一部分”的模式；`exact_json` 比较整个 JSON 值，对象键顺序和 JSON 外部空白不影响比较。

```python
validate_oracle(spec, *, require_binding=True) -> dict
oracle_digest(spec, *, require_binding=True) -> str
evaluate_record(record, spec, *, request_sha256, response_unchanged=True,
                request_unchanged=True) -> dict
```

`validate_oracle` 返回与输入独立的规范化 oracle。无效输入抛出 `VerificationError`，异常文字只有静态错误码。`require_binding=False` 允许尚无 `request_sha256` 的模板；如果提供该字段，仍必须是合法摘要。验证器不会替模板填写请求绑定或注册时间。`oracle_digest` 返回规范化 oracle 的 SHA256；给模板填入请求摘要会形成新的 oracle 和新的摘要。

调用方必须把原始请求字节摘要可信地绑定到 `record.provenance.request_sha256`。`evaluate_record` 要求参数、oracle 和记录中的三份摘要完全相等，同时调用请求适配器核验完整原生响应、`messages` 镜像和可重算的 `provenance.request_content_sha256`。后者是保留的 `exchange.request` 规范化 JSON 摘要，与原始请求字节摘要分开。脱敏后的 exchange 不能还原实际发送的原始字节，因此这个纯模块不能自行证明原始字节绑定。

如果响应内容曾被脱敏改变，调用方应传 `response_unchanged=False`，结果将是 `indeterminate`。默认值不能代替调用方对原始响应和保留响应的一致性检查。不能把脱敏占位符与 expected 的巧合匹配视为真实任务通过。

如果请求内容曾被脱敏改变，调用方也必须传 `request_unchanged=False`，结果为 `indeterminate`，静态码 `request_redacted_or_changed`。原始请求成功的结果不能被重新签署为另一个脱敏后任务的成功；保留原始请求摘要也不能替代这项内容一致性检查。采集层负责同时检查请求与响应，纯函数的两个默认参数都不是一致性证明。

报告的 `schema` 为 `nous.verification/1`，含：

- `status`: `matched`、`mismatched` 或 `indeterminate`，以及静态 `codes`；
- `record_digest`、`oracle_sha256`、`request_sha256`、`result_digest`、`environment_digest`；尚不能安全计算的摘要为 `null`；
- `scope`、固定验证器标识和 `evidence_candidate`。任务不可判定时候选为 `null`。

`result_digest` 是严格解析后整个 JSON 值的规范化摘要，不是原始 HTTP 响应字节摘要。`environment_digest` 绑定验证器版本、Schema 子集版本、Python 实现/版本和整数规则，不证明机器或环境可信。报告不会包含 expected、实际答案、正文、路径、模型账户信息或原始异常。

## 严格解析与资源边界

最终响应必须是唯一、完整的 assistant 文本，直接构成 JSON；不从 Markdown 代码块、解释段落、工具输出或多个候选片段中抽取答案。截断响应、拒绝响应、未知请求协议、不完整质量状态、镜像不一致、重复对象键和非法 JSON 都不能通过。

首版仅接受有符号 64 位整数，明确区分 `true` 与 `1`；浮点、指数写法、NaN 和 Infinity 返回拒绝或不可判定，避免隐式数字精度和类型折算。字符串、布尔、null、数组和对象按标准 JSON 值处理。

Schema 只支持单一 `type`、`properties`、`required`、`additionalProperties`、`items`、`enum`、`const`、整数 `minimum`/`maximum`、`minLength`/`maxLength`、`minItems`/`maxItems`。Schema 本身也做类型校验。`pattern`、引用、组合分支、`uniqueItems`、`multipleOf`、注释扩展和其他未知关键词全部拒绝，不静默忽略。首版没有通用 JSON Schema 兼容承诺。

资源上限是安全限制，不是质量评分：

| 项目 | 上限 |
| --- | --- |
| oracle | 256 KiB、4096 个遍历节点、32 层、16 条 assertions |
| 单个 Schema | 16 层；每层 properties/required 各 64 项；enum 32 项 |
| 最终 JSON | 256 KiB、16384 个遍历节点、32 层 |
| 输入 record | 16 MiB、200000 个遍历节点、64 层 |

对象键也计入节点，长度上限按 UTF-8 字节和保守遍历预算检查；字符串/数组的 Schema 长度界不得超过 262144。超过任何上限均拒绝，绝不截断后判定通过。调用方从文件或网络读取时仍须先做自己的字节上限；本模块接收的是已在内存中的对象。

## 最小合成调用

在 `colleague` 目录执行下面的 Python 片段。这里只构造自有任务，不访问模型、文件系统或真实用户轨迹：

```python
import hashlib
import json
import sys
sys.path.insert(0, "hooks")
from nous_training_request import request_messages
from nous_training_verify import evaluate_record

request = {"model": "synthetic-model", "input": "Return the JSON answer to 2 + 2."}
wire = json.dumps(request, separators=(",", ":")).encode()
request_sha = hashlib.sha256(wire).hexdigest()
request_content_sha = hashlib.sha256(json.dumps(
    request, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
).encode()).hexdigest()
exchange = {
    "schema": "nous.request-exchange/1", "provider": "openai-responses", "transport": "json",
    "request": request,
    "response": {
        "id": "synthetic-response", "object": "response", "status": "completed", "model": "synthetic-model",
        "output": [{"type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": '{"answer":4}'}]}]
    }
}
record = {
    "schema": "nous.trajectory/3", "id": "synthetic-only",
    "exchange": exchange, "messages": request_messages(exchange),
    "quality": {"complete": True, "missing": [], "issues": []},
    "provenance": {"request_sha256": request_sha, "request_content_sha256": request_content_sha}, "evidence": []
}
oracle = {
    "schema": "nous.oracle/1", "scope": "final_json_task", "request_sha256": request_sha,
    "assertions": [{"kind": "exact_json", "expected": {"answer": 4}}]
}
report = evaluate_record(record, oracle, request_sha256=request_sha)
assert report["status"] == "matched", report
assert record["evidence"] == []  # 纯验证器没有落盘或改标注
print(json.dumps(report, ensure_ascii=False, indent=2))
```

这是手写 oracle 的合成单元验收，不是对真实模型能力的测量，也不证明训练收益、商业授权或生产验收完成。

## 证据提交由采集层负责

调用方须在发送请求前冻结 oracle，才能把本次观察记为发送前注册；事后 `verify` 必须标识为采集后检查，不能倒填或伪造预注册时间。纯函数不生成任何注册时间证明。

真正提交证据前，采集层必须在撤回锁内重新核对授权、撤回 generation、记录原始字节/摘要以及 oracle 快照是否变化。若评估期间发生撤回、重采集、审核或 oracle 修改，应拒绝旧候选而不是重新签署。已有负面证据必须保留，新的匹配候选不能自动覆盖、删除或绕过冲突。只有在这些检查完成后，候选才可能成为记录里的证据。

本模块不提供命令 runner、沙箱、自动标注任务成功或自动训练。验证报告成功与训练导出是否满足上下文、隐私、内容审核和其他质量要求是不同结论。
