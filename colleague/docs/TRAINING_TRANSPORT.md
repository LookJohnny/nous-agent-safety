# 请求级传输与本机 fixture

此入口观察客户端实际发出的 API 请求字节和实际收到的响应字节。它补充会话日志的上下文缺口，但不声称恢复提供方内部隐藏模板、内部推理或未观察到的前置请求。第一版使用显式 SDK observer；不会代理、拦截或自动修改 Claude Code / Codex 的全部网络流量。

## 显式 observer 的顺序

`hooks/nous_training_observer.py` 提供 `begin_request(provider, request_bytes, *, project, session, oracle=None, synthetic=False, manual=False, oracle_registration="before_dispatch")` 和 `finish_request(ticket, response_bytes, *, content_type="application/json")`。provider 只能为 `openai-responses` 或 `anthropic-messages`。

先用最终即将发送的 `bytes` 调用 `begin_request`，冻结请求摘要、项目授权状态与可选任务 oracle；发送完全相同的字节，收到完整响应后再 `finish_request`。持续采集未授权时 begin 返回空 ticket；`manual=True` 是显式单次导入，不开启后续采集。撤权会使尚未完成的观察失效。observer 脱敏后存储 `nous.trajectory/3` 候选，并把客观验证证据绑定到请求、结果、oracle 和 record 摘要。

已知答案只来自提前注册的 oracle，例如 `nous.oracle/1` 的 `final_json_task` 与 `exact_json` 断言。验证得到的“任务结果匹配”不能替代独立内容审查，也不等同于用户接受、商业使用授权或模型能力测评。需要内容审查的候选仍不会自动进入 SFT。

## 传输 API

`hooks/nous_training_transport.py` 的 `post_json(endpoint, request_bytes, *, headers=None, allow_loopback=False)` 返回 `{body: bytes, content_type: str, status: int}`。它发送输入字节，不重新编码请求；请求和响应各限 8 MiB，不解压响应。HTTP 错误状态由调用者处理，重定向直接拒绝。

默认目的地只允许 `https://api.openai.com/v1/responses` 和 `https://api.anthropic.com/v1/messages`，使用系统 TLS 验证。URL 不允许账号、密码、查询或 fragment。实现使用标准库 HTTP 连接，不读取代理环境变量，不跟随重定向，不自动获取凭据。headers 仅接受明确提供的 Authorization、x-api-key、Content-Type、Accept 与提供方版本字段，不记录或回显 header。此阶段没有向真实提供方发送请求，也没有获得使用真实 API key 的授权。

测试显式设置 `allow_loopback=True` 才接受 HTTP 的 `127.0.0.1` / `::1`、显式端口及两个固定 API 路径；不接受 localhost DNS 名称或任意内网地址，且禁止向测试 HTTP 发送鉴权 header。此能力是固定目的地客户端，不是通用转发网关。

`decode_response(provider, body, content_type)` 返回 `(native_response, "json" | "sse")`。返回对象和 bytes 都是**尚未脱敏的内存值**，调用者必须先经过授权和隐私处理，不能直接写文件。JSON 重复键、非有限数字、非法编码、过深结构和超限数据均拒绝。

SSE 支持严格的完整 Responses 和 Messages 子集：文本及 function call / tool use、文本或参数增量，以及最终终止事件。OpenAI 的响应 ID、item ID、索引、增量与最终内容必须一致；Anthropic 的 content block 必须正确关闭并收到最终停止事件。缺终止、截断 frame、重复终止、额外未知事件、错误响应和不支持的内容均失败。隐藏 reasoning / thinking 事件直接拒绝，不把它们写入候选，也不通过删除它们宣称上下文完整。传输支持某种工具块不表示当前训练 normalizer 已接受包含未完成工具调用的样本。

所有传输异常为静态 `TransportError` 代码，不附提供方错误正文、URL、路径或凭据。该模块本身没有文件写入。

## 真实客户端、合成本机提供方

先查看计划，不启动客户端，不创建输出：

```sh
python3 scripts/training_request_smoke.py
```

经审阅后显式运行一次，`--out` 必须是全新绝对目录：

```sh
python3 scripts/training_request_smoke.py --run --out /private/tmp/nous-request-fixture-new
```

也可用 `--codex /absolute/path/to/codex` 指定安装版本。脚本默认路径是当前核验的 macOS Codex 内置 CLI；并未承诺所有版本或操作系统都兼容。

此脚本启动真实 Codex CLI，但提供方仅为监听 `127.0.0.1` 的自有 fixture；不运行真实模型、不调用付费模型。fixture 只接受一次 `POST /v1/responses`，拒绝鉴权、压缩、其他方法或路径、重复长度及超过 8 MiB 的正文，并检查固定的合成任务和模型标识。原始请求和 CLI stdout/stderr 只留内存，报告仅含闭合字段名、计数、布尔值和静态诊断；未知字段、工具名、header、用户路径、session ID 与正文不进入报告。

`answer=4` 的 oracle 模板在客户端启动前已冻结为内存快照。请求到达 fixture 后，begin 才绑定实际请求摘要，因此这里的观察点是本机 fixture 服务端、注册时点是 `before_response`，不能称为客户端发送前的 `before_dispatch`。随后向连接发送固定的三个 SSE 事件，成功发送后把相同 bytes 交给 finish。输出包含实际的 id/object/model/status、完整 output 及 message.status；不会为满足 normalizer 而补造客户端缺失的字段。若请求包含当前未支持的原生字段，仍返回合成响应使客户端结束，但报告 `request_capture_unsupported`，不伪造成功记录或审核证据。

CLI 使用 `--ignore-user-config --ignore-rules --skip-git-repo-check --ephemeral`、静态模型 catalog 和进程级自定义 `model_provider`。该 provider 设置 `wire_api="responses"`、`requires_openai_auth=false`、关闭 WebSocket 和重试；同时关闭 hooks/apps/plugins/skills 指令、shell、memories、多 agent、自定义 MCP 和请求压缩，使用只读 sandbox。命令不改 `HOME` / `CODEX_HOME`、不持久化 trust、不写用户全局配置。标准输出和错误各有大小上限，外层 90 秒终止，客户端 token 预算设为 12000。保留平台管理员限制；这些配置不构成对客户端所有后台网络活动的全面证明。

输出 `report.json` 区分 `fixture_transport_passed`、`request_capture_passed`、客观结果匹配和 `strict_sft_ready`。候选通过 `request-sft-v1` 导出并运行离线 validator；没有内容审查时不宣称 SFT 就绪。`private/` 是该 fixture 的隔离状态，不属于可分享验收材料；分享前仅选择安全报告与通过隐私管线生成的数据集。

本模块的单元测试只用合成 bytes、mock socket 与 mock handler，不运行 CLI、外部 HTTP 或模型。执行：

```sh
python3 -m unittest tests.test_training_transport
```

## 接口依据与验证边界

本机已核验 CLI 版本为 `0.169.0-alpha.11.1`。配置与 SSE 形状参考固定公开源码 commit `5cb38e79842a0e0a6dca5bb97874163e8ee50129` 的 [ModelProviderInfo](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/model-provider-info/src/lib.rs)、[模型描述](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/protocol/src/openai_models.rs) 和 [Responses 测试 fixture](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/tests/common/responses.rs)。固定源码可解释已知协议，不是本机 binary 与该 commit 完全一致的证明；实际请求 schema 和客户端结束状态仍需运行本机 fixture 核验。

本阶段准备和离线测试完成不代表实机验收已经执行。具体执行与结果以独立生成的安全报告为准。
