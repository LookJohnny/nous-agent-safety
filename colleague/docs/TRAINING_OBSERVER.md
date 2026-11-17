# 请求级采集与本地验证

开发预览 a6 提供显式的客户端请求/响应观察器。观察范围是客户端实际发送的最终 JSON bytes 和收到的完整 JSON/SSE bytes；不能证明服务端最终展开的模型输入、tokenization 或隐藏系统模板。它不会自动接管已经安装的 Claude Code/Codex，Stop 日志采集仍保持原有能力与限制。

## SDK 接入

在受信任的客户端最后发送边界调用：

```python
from nous_training_observer import observe_request

response, capture = observe_request(
    "openai-responses", final_request_bytes, send_exact_bytes,
    project=explicit_project_directory,
    session=explicit_session_identifier,
    oracle=predeclared_oracle_template,
)
```

`send_exact_bytes(bytes)` 返回 `{body: bytes, content_type: str, status: int}`。
观察器只向该函数发送一次原始 bytes；回传原始 transport response 给客户端使用，不把它写入报告。连续采集默认关闭，需先对项目执行 `enable`。独立集成也可使用 `begin_request` / `finish_request`，但必须自行保证前者发生在发送之前、后者处理真实完整响应，并自行隔离采集异常。这些本地函数不是防篡改的远程证明机制。

`manual=True` 表示明确的一次性采集，记录可以导出，但不会启用后续采集。发送期间撤销再授权、全局撤销或项目目录被替换，都会阻止这次旧请求落库。ticket 只能完成一次；每个请求独立 ID；会话标识哈希后用于分组隔离。

## 显式命令

```sh
python3 -B hooks/nous_training.py capture-request \
  --provider openai-responses --request /absolute/request.json \
  --project /absolute/project --session task-001 \
  --oracle /absolute/oracle.json --api-key-env EXPLICIT_API_KEY
```

命令会实际发送该文件中的请求，可能产生所选服务的费用；本项目 a6 验收只运行离线/localhost fixtures。只有显式指定的环境变量会被读作 API key；key/headers 不写入采集数据。Anthropic 使用 `--provider anthropic-messages`，保持原生 Messages 请求结构。官方域名固定；本地测试需显式 `--endpoint http://127.0.0.1:PORT/v1/responses --allow-loopback --synthetic`，且禁止携带凭据。

输入文件由操作者预先创建；观察器不制作原始请求或响应副本。不支持的上下文不自动删字段后再发送。CLI 输出仅为状态、ID 和静态验证摘要，不输出模型回答。SDK wrapper 在采集失败时仍保持调用方的原请求发送行为；HTTP错误/发送失败不会得到成功样本。

## 训练导出

```sh
python3 -B hooks/nous_training.py demo --profile request-sft-v1 --out /absolute/synthetic-dataset
python3 -B hooks/nous_training.py review-content --id t_RECORD_ID --reviewer reviewer-alias --result cleared
python3 -B hooks/nous_training.py export --profile request-sft-v1 --out /absolute/dataset
python3 -B hooks/nous_training.py validate --dataset /absolute/dataset
python3 -B hooks/nous_training.py split --dataset /absolute/dataset --out /absolute/split --group-by session
```

`nous.trajectory/3` 记录原生 exchange；`messages` 只是严格一致的界面投影。`sft.jsonl` 每行仅有 string `prompt` 和 `completion`，使用 `nous.request-sft/1` 原生请求/输出序列化；不是任一平台的训练 chat template。DPO 暂不支持。样本训练只计算 completion loss。数据需要接入训练框架自己的 tokenizer、长度策略和评测，不承诺对任意模型直接微调均兼容。

demo 包含 30 个手写双供应商 fixtures：24 条通过两道门槛、2 条错误答案、2 条仅格式检查、2 条缺内容审核。只有 24 条进入 SFT；所有数据标 synthetic，不代表真实用户数据、模型成功率或商业可售质量。

## 隐私与证据

- 原始 bodies 只在内存；先脱敏再保存。tool schema 的敏感属性 literal values 会删除；工具定义被改动后，记录隔离为不完整。
- 原始 request/response SHA256 是一致性关联，不是匿名化或生产者签名；脱敏请求有另一个可重算的内容 SHA256。证据摘要绑定 exchange、投影、质量和来源。
- oracle 在发送前冻结，绑定实际请求哈希。预期答案不落盘，只保留验证器、规则/结果/环境摘要与静态原因。操作者应另行保护原始 oracle 文件；摘要不能重建规则，也不能证明规则合理。
- 脱敏改变输入或最终答案时，自动验证返回无法判断，不能替新的训练内容签发成功。格式通过不等于任务完成，任务通过不等于内容获准训练。独立 `review-content` 仍必需。
- 已绑定的错误/拒绝证据继续阻止训练，后续正面标记不能覆盖。新版本不放宽旧记录门槛。

首版支持无服务端续接状态、已知工具定义、最终文本/JSON回答；执行工具历史、媒体、隐藏推理、未知语义字段和未完成响应会拒绝采集。复杂软件任务的测试运行、容器回放与完整多轮 Agent 轨迹仍待后续版本。
