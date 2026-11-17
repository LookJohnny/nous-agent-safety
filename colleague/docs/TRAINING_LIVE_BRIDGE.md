# 限定实机任务的本地请求观察

`hooks/nous_training_live_bridge.py` 供显式授权的小规模实机验收使用。它只监听 `127.0.0.1` 的随机端口，路由包含每次新生成的随机 nonce；不安装全局代理、不修改平台配置、不读取认证文件，也不获取或刷新认证。正常 CLI 自行加载既有认证，bridge 仅在内存中把这次请求所携带的允许 header 交给固定 HTTPS 上游。

固定目标只有：

- Codex：`https://chatgpt.com/backend-api/codex/responses`，用于已登录 ChatGPT 的本地验收。它不是通用 OpenAI API key 网关。
- Claude：`https://api.anthropic.com/v1/messages`；仅额外支持 SDK 的固定 `?beta=true` 选择器。

不得用此模块绕过平台的认证、管理策略或配额要求。若 CLI 拒绝向该本地观察点发送凭据，应记录阻断；不能提取钥匙串、替换 credential 或关闭 TLS 检查追求通过。

```python
from nous_training_live_bridge import LiveBridge

# callbacks 接收尚未脱敏的内存 bytes，必须接现有授权/脱敏 observer。
with LiveBridge("codex", before=begin_observed_step,
                after=finish_observed_step,
                max_requests=8, total_timeout=180, io_timeout=30) as bridge:
    # Codex 的进程级自定义 provider.base_url = bridge.base_url + "/v1"
    # Claude 的进程环境 ANTHROPIC_BASE_URL = bridge.base_url
    # 只启动已经审阅的隔离 CLI 命令；正常 CLI 认证由平台自身处理。
    run_one_reviewed_cli(bridge.base_url)
    metadata = bridge.report()
```

`before(raw_request_bytes)` 在发上游前执行，返回 opaque ticket；`after(ticket, raw_response_bytes, content_type, status)` 在读完真实上游响应后执行。bridge 不重编码请求、不解析或删改 SSE 内容。响应有界缓冲完整后原样交回 CLI，因此这不是低延迟逐 token 流式代理。单次请求/响应上限 8 MiB，整段累计请求与响应上限 16 MiB；默认最多 6 个请求，可明确设置为 1–16。总时限最多 300 秒，I/O 时限最多 60 秒。没有重试、重定向、自动解压、任意上游 URL 或证书验证关闭开关。

callback 异常仅产生固定 `capture_before_failed` / `capture_after_failed` 计数，不显示异常文本。此时真实上游 bytes 仍给 CLI，但 `capture_callbacks_passed=false`，整条数据不得宣称完整采集或进入严格训练。上游 200、CLI 退出 0、或者这个 callback 标记本身，都不是任务结果成功证据。隐藏推理、不支持的工具或上下文仍由 observer 拒绝；禁止为了让样本通过而删除事件。

安全报告只包含静态错误码、状态码计数、字节量及布尔值。URL nonce、header 值、会话标识、原始正文均不写入报告。拒绝原因分为 host、content type、content length、认证缺失/格式/冲突和未知 header 等闭集代码。未知 header 只报告本机二进制已确认名称的布尔命中与其他未知名称的数量；诊断词表不会自动扩大转发白名单。调用方负责限制 CLI 输出、禁止打印原始 callback 数据、独立核验实际工具过程与最终任务结果，并给最终脱敏版本绑定审核证据。

JSON 错误响应仅在内存中识别有限 `error.type` 和固定消息词组，报告 `provider_error_type` 及布尔 `message_signals`；未知类型统一记为 `other_or_unknown`。不保存 message、请求标识、正文或正文 hash。`quota_terms=true` 只是服务端错误中出现相关词组，不能单独证明配额不足；CLI 输出的关键词匹配更不能代替服务端状态与明确错误类型。超过 64 KiB、非 JSON、重复 key 或畸形错误正文不做内容分类。分类不修改交还 CLI 的上游 bytes。

Claude 额外报告固定 OAuth beta 标记是否存在，以及含该标记的 CLI 原 header 是否未经改写交给上游发送方法：`oauth_beta_present` / `oauth_beta_forwarded_exact`。完整 beta 值不存储；probe 或未执行发送时后者恒为 false。它们不证明服务端接受了认证。桥不会手动补 beta、替换认证或改变模型配置来绕过策略。

排查本地拒绝可使用 `scripts/training_header_probe.py`，默认只打印计划。显式 `--run-probe` 才创建隔离项目并启动 CLI；需给 `--source-root` 指向包含完整 hooks 和真实 runner 的代码根，`--fixtures` 指向已审核 fixture，`--out` 必须是尚不存在的绝对目录：

```sh
python3 -B scripts/training_header_probe.py --agent codex \
  --source-root /absolute/reviewed/code-root \
  --fixtures /absolute/reviewed/fixtures \
  --out /private/tmp/nous-header-probe-new --run-probe
```

probe 强制 `probe_only=True`、最多 1 个有效路径请求和 45 秒 CLI 时限。在路径、header 和正文长度检查后返回固定 503；不会调用 `_connect`、observer callback 或上游错误分类，也不会写训练标签。拒绝时仍给出闭集本地原因。CLI 自身的正常认证与后台行为不在此承诺内；此模式证明的是 bridge 不转发任何模型推理请求，不能视为真实多轮通过。未知 header 必须先查固定源码或本机二进制，再明确修改并测试白名单，不能根据 probe 自动放行。

Codex 的 `session-id`、`thread-id`、`x-client-request-id` 已在固定提交的[会话 header 构造](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/codex-api/src/requests/headers.rs)和[Responses 请求](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/codex-api/src/endpoint/responses.rs)中核对，并经一次不转发的本机 CLI probe 精确匹配后，明确加入转发白名单。它们的值仍只在内存中原样传递，不写报告。二进制匹配与公开提交不能替代真实任务验收。

本地合成测试会 mock 所有上游 I/O，仅启用本机回环端口。25 项测试覆盖请求/响应字节保真、两个固定上游、认证缺失、重复 header、未知路径、nonce、错误方法、并发、次数/大小/时间限制、重定向、压缩响应、错误状态、callback 失败、TLS 验证配置、诊断脱敏、OAuth beta 和已验证 SDK header 原样转发，以及 probe 零上游/零 callback。它们不证明真实平台已验收通过。

Codex 认证路径依据固定的[提供方配置源码](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/model-provider-info/src/lib.rs)和[认证路由源码](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/model-provider/src/provider.rs)：自定义 provider 可显式配置 `requires_openai_auth=true`，保留现有 first-party auth 管理，不另提供 `env_key` 或 bearer override。该公开提交不等同于已证明的本机二进制构建来源。Claude 2.1.319 的本机推理工厂代码显示已有 OAuth 与显式 `ANTHROPIC_BASE_URL` 的组合路径；真实是否允许仍以本次有界实测和管理策略为准。
