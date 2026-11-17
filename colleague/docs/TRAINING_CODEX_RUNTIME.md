# Codex 运行时兼容边界

2026-11-16 核查结论：当前 `codex-events-1.1` 能识别一部分已知存储元数据，但没有把真实 CodeMode 轨迹提升为可训练正例。已授权的单次合成会话仍保留 19 个事件，质量报告包含 `unresolved_history_metadata`、`unresolved_world_state`、`unresolved_turn_context`、`unresolved_thread_settings`。它可以作为脱敏后的 canonical 候选保存；`codemode-events-v1` 继续拒绝生成 SFT。

本机 CLI 的 `--version` 和随包 `codex-package.json` 都报告 `0.169.0-alpha.11.1`，目标平台为 `aarch64-apple-darwin`。核查没有读取其他会话、凭据或用户配置正文，也没有调用模型。公开源码依据固定在 [`openai/codex` 的 `5cb38e79842a0e0a6dca5bb97874163e8ee50129`](https://github.com/openai/codex/tree/5cb38e79842a0e0a6dca5bb97874163e8ee50129)；随包元数据没有提供对应提交，因此不能把这个提交当成本机二进制的已证实构建来源。

| 观察位置 | 已有依据 | 当前处理 |
| --- | --- | --- |
| JSONL 外层 `ordinal` | 公开 `RolloutLine` 定义为可选 `u64` 存储序号 | 只有会话声明精确版本 `0.169.0-alpha.11.1` 时识别；检查整数范围和严格递增，不把它加入模型输入 |
| JSONL 外层 `metadata` | history sequence、采样边界、保留来源、MCP 归因均由运行时维护 | 非空 metadata 始终产生 `unresolved_history_metadata`；未知键另报静态缺口，不复制正文 |
| `world_state` | 模型可见上下文的比较状态，包含内容指纹和 full/patch 基线 | 只保留已允许的安全引用字段；继续阻断训练，不把哈希当成 system/developer 正文 |
| `turn_context` | 在用户轮次及压缩后持久化，帮助恢复上下文基线 | 当前只保留 model/effort 提示；权限、路由和恢复语义未完整处理，继续阻断 |
| `thread_settings_applied` | 线程设置快照，包括模型、权限、协作模式及插件选择 | 仍记录静态未解析缺口；不能把它当成纯 UI 计数或替代模型输入 |

`metadata` 不是一类可以整体忽略的数据。`sampled_through_sequence` 表示一次已产生模型输出的请求实际包含到哪一个输入；`retained_source` 涉及被保留内容的完整性；`fallback_token_limit_override` 会影响历史重放预算。虽然这些字段本身不是模型输入，它们关系到如何判断轨迹覆盖了什么。`mcp_attribution` 是运行时归因，也不等于模型直接发出了对应的 function call。[history 元数据定义](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/history/src/lib.rs#L58-L175)、[采样边界维护](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/context_manager/sampling.rs)。

`world_state.full=true` 的意思是建立比较基线，并不表示记录里已经包含全部提示词。公开实现用 SHA-1 内容指纹追踪部分片段；后续状态差异使用 merge patch。即使知道这一机制，也还需要证明每个引用对应的模型可见片段、有效工具目录和变更位置，才能宣称完整重放。[world-state 比较与指纹实现](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/context/world_state/mod.rs#L422-L566)。

`TurnContextItem` 和 `ThreadSettingsSnapshot` 的用途与字段可从公开协议中核对，但当前适配器没有实现该运行时的全部恢复、权限和插件选择规则。线程设置还带有逻辑任务归属，不能只凭物理文件的位置推断来源。[持久化协议定义](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/protocol/src/protocol.rs#L2522-L2566)、[线程设置更新实现](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/session/thread_settings.rs)。

解析兼容说明：事件 envelope 仍为 `nous.codex.events/1`，没有增加可绕过脱敏的新 payload 字段。新版解析器输出 `adapter_version=codex-events-1.1`；结构验证同时接受旧 `codex-events-1`。这是格式兼容，不是对旧数据重新授予完整性或审核标签。未声明版本、其他版本、非法或逆序 ordinal，以及未知 metadata，均不会被新规则放行。`parse_protocol` 仍返回未脱敏的内存对象，只能由已授权 collector 脱敏后持久化。

Stop hook 的接入条件也应分开验证：配置存在、平台允许执行、实际收到事件，是三个不同结果。官方说明 `features.hooks` 默认开启；`codex_hooks` 是旧别名。项目层还受项目可信状态约束；非托管 hook 要对当前精确定义哈希给予信任。`/hooks` 是交互管理入口，本机 CLI 没有单独的 `codex hooks` 管理子命令。[Hooks 官方说明](https://learn.chatgpt.com/docs/hooks)。

`--ignore-user-config` 不能作为全部 hook 来源已隔离的证明。公开 loader 仍保留一个空的 User 配置层，hook discovery 会按该层的目录寻找 `hooks.json`。多个来源会合并；已有 managed/builtin hooks 也不能靠普通单项 `enabled=false` 禁用。[配置加载](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/config/src/loader/mod.rs#L550-L565)、[来源发现与执行条件](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/hooks/src/engine/discovery.rs#L145-L210)。

交互信任操作会写入 `hooks.state`。测试没有调用该写接口，没有设置全局 trust，也没有使用绕过 hook 信任的参数。第四轮已在专用目录验证单个 fixture 的临时精确哈希信任和真实 Stop；过程及边界见下文。[TUI 信任写入实现](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/tui/src/hooks_rpc.rs#L64-L98)。

只读 `hooks/list` 核查不创建线程、不开始 turn、不调用模型、不写配置。首次受限环境尝试在初始化前以权限错误退出，不能据此推断安装或信任状态。正常权限复核按本机 `app-server generate-json-schema --experimental` 导出的契约严格检查：结果须含 `data` 数组，每个条目须含 `cwd`、`hooks`、`warnings`、`errors`；不能把字段缺失默认成空 hook 列表。本次正常权限查询得到一个 cwd 匹配的条目，warnings/errors 均为零，total/enabled/managed hooks 均为零。这只是原合成项目在本次查询时的清单，不证明后续来源隔离或 Stop 已经执行。

随后在全新专用测试目录放置一个已审阅的独立 Stop hook，仅通过进程参数临时声明该测试项目可信，未启用该项目的轨迹采集。正常权限的只读查询仍返回 cwd 匹配、零 warnings/errors、零 hooks：清单可读，但指定 fixture 未被发现。唯一一次修正是在该测试目录用空模板初始化空 Git 仓库；保持 hook 定义字节不变后再次查询，结果仍相同。公开源码的默认项目标记是 `.git`，但没有标记时会回退 cwd 并尝试加载 `.codex`，所以不能把失败原因断言为缺少 Git 仓库。[项目标记默认值](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/config/src/project_root_markers.rs#L5)、[无标记时的回退与项目层发现](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/config/src/loader/mod.rs#L1485-L1487)。

第三轮在此停止，当时单 hook 临时精确信任和真实 Stop 均未验证。第四轮定位到测试脚本错误：CLI 覆盖参数的左侧按字面点号分割，不会对各段做 TOML 键反引号或双引号解码。旧 `projects."/path".trust_level` 编码无法匹配项目，含点的 hook key 也会被拆开。修复为右侧 inline table，例如 `projects={"/path"={trust_level="trusted"}}` 和 `hooks.state={"exact.key"={trusted_hash="hash"}}`。此前零 hooks 的结果不能用作平台不兼容的证据。

第四轮只读探测验证唯一、非托管、已启用的测试 Stop，信任依次为 untrusted → trusted → untrusted，warnings/errors 均为零。启动客户端前再次检查相同 fixture 哈希和来源清单，再用同一组运行时覆盖参数启动一次有界合成任务。真实 Stop 收到事件并调用正式 collector，自动保存 1 条 `nous.trajectory/2`；项目、原生会话、来源文件、最终回复、健康状态均匹配。没有手动 import/recover，没有添加审核证据，没有持久化信任或修改全局配置。客户端退出 0，但报告保留了过程中观察到的网络/额度类静态诊断，不把最终成功表述为无错误运行。

这一结果证明本机该版本、该配置下的单次自动采集。清单检查是快照；app-server 不支持 exec 使用的 `--ignore-user-config`，报告明确保留此加载差异，不能推导出任意安装环境、来源变更或所有 CLI 版本均兼容。该样本仍缺上述四项运行时语义，候选数据结构验证与审计通过，严格 SFT 为 0。实际 Stop 发生在最后一项项目身份竞态修复前；最终发行代码另有完整离线回归，未重复模型请求。详见 a4 发行目录 `reports/live/codex-stop/report.json` 和 `ACCEPTANCE_REPORT.json`。

Stop 成功时空 stdout 在已核查的公开实现里被接受；非空内容才进入 Stop JSON 输出解析。因而目前独立采集 hook 的空成功输出不需要仅为文档措辞而改成控制指令。实际客户端行为仍需有界实测记录。[Stop 输出处理](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/hooks/src/events/stop.rs)。

进一步放行需要新增证据，而不是删除质量标签：固定并验证运行时版本；证明所选 assistant 目标的实际采样前缀；将 state 指纹及更新对应到已观察片段；覆盖压缩、工具目录变化和线程归属；最后再做独立的内容审核和结果验证。当前合成 fixture 训练正例只验证可理解的显式事件协议，不代表所有实际 Codex 会话已经支持。

## 第五轮：只读采样上下文核查

本轮只检查第四轮新产生的 Stop 合成会话，没有重新调用模型，也没有读取其他会话正文、凭据或用户配置。该源文件有 **20 个物理 JSONL 行**，其中 10 个 native response items、8 个 message、1 个 assistant 目标。此前 MCP 合成会话的“19”是适配后的 protocol 事件数，与这里的物理行数不是同一口径；本轮没有重新读取旧 MCP 会话。以下判断仍依据本机声明的精确 CLI 版本与上文固定公开提交，不能把公开提交等同于已证明的二进制构建来源。

| 本次观察 | 可以确认的事实 | 不能据此断言 |
| --- | --- | --- |
| 原生 `history_sequence` 连续为 1–10；唯一 assistant 的序号为 10，`sampled_through_sequence=9` | 目标输出携带截至输入序号 9 的累计采样边界 | 最终请求逐项等于全部序号 1–9 |
| `session_meta.base_instructions.text` 与一个 native developer block 逐字相等 | 两个来源指向同一段基础指令；对应 model 指纹也匹配 | 额外增加一份 system/developer 输入 |
| 15 个 top-level namespace/member 指纹全部匹配 `additional_tools` | 此时已记录工具定义与比较快照相符，包含 3 个 namespace、12 个成员 | 未来更新、运行时嵌套 callable 或请求最终工具配置均已完整覆盖 |
| 6 个文本片段引用中，5 个匹配实际可见片段 | model、权限、协作模式和两处多 agent 提示引用可以关联既有文本 | 其余运行时状态都可忽略 |
| `model_catalog` 没有匹配可见片段，但匹配源码已知的空默认状态 | 比较快照能表示尚未渲染的空状态 | 将该默认撤销提示补进模型输入 |

`history_sequence` 与 `sampled_through_sequence` 的公开类型均为 `Option<u64>`。前者是线程历史身份，压缩会保留被保留输入的身份；后者是已记录模型输出的 regular 请求包含的最高输入序号，压缩携带该值但不推进。记录新 envelope 时运行时按累计最大值维护水位，因此不能把每行水位误作一张完整请求成员表。[字段定义：`codex-rs/history/src/lib.rs` 70–78 行](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/history/src/lib.rs#L70-L78)、[累计水位维护：`codex-rs/core/src/context_manager/sampling.rs` 13–23 行](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/context_manager/sampling.rs#L13-L23)。

请求构造还存在明确的后续步骤：先从 history 取得采样水位，之后才执行 `for_prompt`、附加工具调用信息、构建 prompt、可能的 Guardian 处理，以及搜索 fallback 的工具调整。仅凭历史水位无法证明这些步骤后的内容；压缩、媒体恢复、过滤与重试同样需要单独证据。[请求构造：`codex-rs/core/src/session/turn.rs` 2002–2041 行](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/session/turn.rs#L2002-L2041)。

基础指令的来源别名尤其需要保真。公开 `BaseInstructionsFragment` 只把带 `model.base_instructions` 分类的单 block developer 消息识别为 dedicated base item，本次日志有这一分类且原文完全相等。在 incremental-tools 模式下，`build_prompt` 又把独立 `base_instructions` 字段置空，基础指令已经在历史里。当前观察可以支持“记录别名关系”，尚不能直接改变 canonical；未来训练投影必须避免把 session metadata 再作为第二份输入。[专用基础片段：`codex-rs/core/src/context/base_instructions.rs`](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/context/base_instructions.rs)、[incremental prompt 构造：`turn.rs` 1899–1928 行](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/session/turn.rs#L1899-L1928)。

本轮指纹比对使用源码给出的算法：文本片段按固定版本前缀、规范化换行后的 role/text 的 UTF-8 字节长度与内容计算 SHA-1；工具对象递归排序键后计算紧凑 JSON 的 SHA-1。namespace header 先移除 `tools`，成员各自按完整定义计算。本次纯字符串、整数与布尔结构的 15 项全部匹配；不能将这一结果泛化到未经核对的浮点数序列化。SHA-1 在这里是源码比较指纹，不替代本项目对来源和训练证据的 SHA-256 绑定。[指纹算法：`world_state/mod.rs` 427–458 行](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/context/world_state/mod.rs#L427-L458)、[工具拆分：`world_state/top_level_tools.rs` 71–97 行](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/context/world_state/top_level_tools.rs#L71-L97)。

`model_catalog` 的空默认状态是一个具体反例：源码能计算该状态的指纹，但在空 catalog 且没有先前状态时不产生模型片段。本次指纹正好匹配这种默认片段，而不匹配“没有 picker-visible model”的另一种文字；源日志中没有对应可见片段。因此诊断可区分“已知空状态引用”和“未解析引用”，但不能把算法里的默认文字当成真实输入。[状态渲染与省略：`world_state/model_catalog.rs` 119–160 行](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/core/src/context/world_state/model_catalog.rs#L119-L160)、[片段标记拼接：`codex-rs/context-fragments/src/fragment.rs` 94–104 行](https://github.com/openai/codex/blob/5cb38e79842a0e0a6dca5bb97874163e8ee50129/codex-rs/context-fragments/src/fragment.rs#L94-L104)。

运行参数也不能替代观测：虽然测试显式设置了 `features.multi_agent=false` 等开关，本次 native catalog 仍包含 collaboration 以及其他运行时工具声明。本次任务没有调用工具，Stop 验收仍成立；关于模型能看到哪些工具，应以实际目录及其时序为准，不能写成所有工具均不可见。

本轮已提供绑定源快照的只读诊断（`inspect-source`）：序号与采样水位、基础指令精确文本匹配、已观察片段引用及未知字段分别计数，保持 canonical、隐私规则和质量标签不变。通用诊断仍将 `model_catalog` 作为 opaque 引用计数；上面的已知空默认状态匹配属于本次固定源码研究，尚未作为通用自动判定。`turn_context`、`thread_settings_applied` 继续作为运行时状态核查，不把设置对象转成补造的提示词。若要证明完整请求，还需要在最终请求构造完成后的显式本地观测点取得请求成员与配置来源，并覆盖压缩、媒体和动态工具更新；不能通过删除未解析项让数据“通过”。本轮没有新增审核证据，真实候选仍为 1 条 canonical、0 条严格 SFT。
