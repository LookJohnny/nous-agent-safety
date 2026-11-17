# 真实提供方的多轮代码任务验收

`scripts/training_real_multiturn.py` 用真实 Claude Code / Codex CLI、现有平台登录和真实上游模型完成隔离代码任务。它不同于 `training_multiturn_smoke.py` 的预编写响应 fixture；此入口可能消耗账户额度或产生费用。只有显式 `--run` 才启动模型。默认只准备新目录、固定任务、工作区基线与基线失败验收，不运行 CLI。

```sh
python3 scripts/training_real_multiturn.py --agent claude --out /private/tmp/nous-real-claude-plan
python3 scripts/training_real_multiturn.py --agent codex --out /private/tmp/nous-real-codex-plan

# 每次真实运行必须使用另一个尚不存在的绝对输出目录。
python3 scripts/training_real_multiturn.py --agent claude --run --out /private/tmp/nous-real-claude-run
python3 scripts/training_real_multiturn.py --agent codex --run --out /private/tmp/nous-real-codex-run
```

默认 fixtures 位于 `examples/live_tasks`。测试其他已审阅副本时可显式指定 `--fixtures` 与 `--source-root`；`--executable` 用于指定本机已安装 CLI。一次 invocation 不会自动重试模型任务，不修改持久平台配置或全局 hook 信任，也不启用日常项目的持续轨迹采集。平台仍可能使用其自己的认证状态和正常日志；验收工具不读取、复制或打印凭据文件。

## 两个受控代码任务

Claude 任务实现 `merge_windows`，Codex 任务实现 `event_totals`。模型只能通过本次 stdio MCP 使用三个工具：

- `read_fixture`：只读 `TASK.md`、`solution.py`、`test_public.py`、`public_cases.json` 四个公开文件之一。
- `write_solution`：只替换 `solution.py`，最多 16 KiB。先通过闭合 AST 能力检查，不允许导入、动态执行、dunder 属性、文件、网络或进程操作。
- `check_public_tests`：无命令或代码参数，由固定私有 evaluator 检查公开用例。公开测试文件的实际执行不是信任根；独立固定的任务规格和预期结果才是。

模型不具备 shell、任意文件工具或私有 evaluator 的读取入口。验收要求实际修改解决方案并调用测试，不接受仅在最终回答中自述“测试通过”。私有额外用例由流程结束后的 evaluator 检查，不回传其输入或逐项结果给模型。公开／私有用例是有限任务判据，不等于一般代码正确性证明。

候选 Python 只在子进程运行，采用固定 AST/builtin 能力集、CPU 时限、6 秒执行期限和 OS sandbox。sandbox 禁止网络、派生进程和 scratch 之外的写入；读取能力边界依靠闭合 AST/builtin 集，不宣称 OS 层完全禁止读取。256 MiB 内存限制是采样 RSS watchdog，不是内核硬内存上限。文件篡改、额外文件、符号链接、测试或 Git 基线改变会使工作区验收失败。

## 模型调用与请求观察边界

每个平台最多 8 个上游请求、180 秒 CLI 总时限；单次请求/响应 8 MiB，整段合计 16 MiB，CLI stdout/stderr 各 2 MiB。Claude 增加 `--max-budget-usd 0.75` 估计限制，Codex 设置 12,000-token rollout budget；两者都不是账单硬上限。禁止将这一入口直接加入普通无人工关注的 CI。

每次启动独立的 nonce 路由和本机 `127.0.0.1` 观察桥。两个固定官方上游及 TLS、header、正文保真边界见 [TRAINING_LIVE_BRIDGE.md](TRAINING_LIVE_BRIDGE.md)。认证由 CLI 自己加载；不使用 dummy 凭据冒充真实调用，不在桥接失败时改用其他 token 或提取钥匙串。认证、管理策略、额度或协议阻断均保留为失败结果。

请求在实际 dispatch 前绑定摘要，收到的真实响应 bytes 交给 episode observer；`synthetic=False` 只说明提供方并非 scripted fixture，不证明模型任务已成功。未知字段、隐藏推理或未支持上下文仍由原严格 observer 拒绝。不得删去这些内容后声称完整请求已还原。

只有请求数、上游响应数和完整交付数相等，全部 HTTP 状态为 200，桥没有错误，且各 capture callback 成功时，runner 才尝试保存整段 episode。已经收到上游完整响应但没有完整交付给 CLI，也不算完整采集。当前桥在有界内存中缓冲完整响应后原样发送，不能宣传为低延迟逐 token 代理。

## 看懂结果与训练门槛

`report.json` 分开记录 CLI 结束状态、真实工具调用、模型工具轮次、delivery gate、episode 采集、任务文件验收和候选 dataset 校验。`wire-observations.json` 只有摘要、大小、HTTP 状态和静态采集诊断，不含原始请求／响应。`private/` 包含本次进程命令及隔离状态，应保留本地；对外只选择安全报告和经过隐私管线的 dataset。

`artifact-oracle.json` 的成功只说明最终文件通过固定任务判据；它不证明中间每个工具决定合理。runner 不会把它自动写成人工审核或完整轨迹训练批准。候选导出使用 `request-episode-sft-v1`，仍须分别完成：

1. 原请求、工具 schema、调用结果、最终响应的完整性检查，以及实际至少两轮工具过程的核对。
2. 独立任务结果证据，绑定最终脱敏样本、解决方案、evaluator 和任务规格摘要。
3. 对准确脱敏内容及每个拟监督工具步骤的独立审查；审核者类型应如实标明，不能把 AI 审查写成人工接受。
4. 严格导出、独立 dataset validator 和目标数据加载检查。

因此 `task_result_verified=true`、`candidate_valid=true` 和 `strict_sft_rows>0` 是不同结论。此文档描述验收机制，不预先宣称任一真实平台、任务或模型已通过。本机结果只能引用各次新目录实际生成的报告。
