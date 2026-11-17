# 候选数据只读诊断

`diagnose_dataset(path)` 在已导出数据集的一个有界私有快照上，列出每条 canonical 轨迹的已知质量缺口、内容/任务结果证据状态、已有拒绝记录，以及需要核实或补采的材料。它不更改 `quality`、`evidence` 或轨迹内容，不生成标注，也不重新导出样本。

```sh
python3 hooks/nous_training.py diagnose --dataset /absolute/path/to/dataset
python3 hooks/nous_training.py diagnose --dataset /absolute/path/to/dataset --require-training-rows
```

默认命令允许诊断零训练行的候选数据；需要结构验证通过且 `diagnosis_available=true` 才返回 0。`--require-training-rows` 另要求现有 SFT/DPO 文件至少有一行。失败、资源超限或不能完整诊断时返回非零。命令不会为了达到非零产出而接受候选或启动平台会话。

也可从 `colleague` 目录调用 Python API：

```sh
PYTHONPATH=hooks python3 -c '
import json, sys
from nous_training_diagnose import diagnose_dataset
print(json.dumps(diagnose_dataset(sys.argv[1]), ensure_ascii=False, indent=2))
' /absolute/path/to/dataset
```

## 报告范围

报告 schema 为 `nous.training.diagnosis/1`：

| 字段 | 含义 |
| --- | --- |
| `structural_valid` | 本次复制出的快照是否实际完成并通过严格验证 |
| `diagnosis_available` | 是否完整生成报告；超限不会返回前 1,000 条假装完成 |
| `dataset_manifest_sha256` | 本次快照 manifest 的 SHA-256；标识本报告所对应的完整数据版本 |
| `training_rows_present`、`counts`、`existing_export_view` | 已经存在且通过验证的训练文件和 lineage 视图 |
| `training_eligibility_not_reassessed` | 始终为 true；诊断没有重新作出训练资格决定 |
| `aggregate` | 与 audit 相同口径的安全聚合统计，使用同一个快照，不再复制或验证原目录 |
| `records` | canonical 轨迹逐条诊断，不是逐条 preference 诊断 |

“证据满足某项规则”和“质量声明完整”都不能使诊断器宣布新的 ready 状态。只有已有验证后 lineage 进入 `existing_export`；是否最终用于训练、达到真实任务要求或拥有交付授权，都不是本报告新证明的事实。

每条轨迹用 `trajectory_ordinal`（`trajectories.jsonl` 中从 1 开始的物理行号）和 `record_digest` 标识，不输出任意记录 ID、来源项目/会话 ID、文件路径、正文、工具名或标注者。这些 ordinal 只在该 manifest 版本内有意义，不是 collector 的 `t_…` ID，不能直接传给 `recover` 或审核命令。用户需在有权访问的本地记录中自行定位准确来源。

历史 `nous.trajectory/1` 的 `record_digest` 只绑定 messages 与 tools，不绑定 quality；保持这个算法是为了兼容旧审核。`nous.trajectory/2` 还绑定 protocol、quality 和特定来源身份。报告顶层的 manifest SHA-256 则通过各文件摘要绑定本次输入的完整内容：判断两个诊断是否针对同一数据版本，应同时使用 manifest 摘要，不能只比较 v1 record digest。

## 已知事实与所需证据

`quality` 分开呈现 canonical 声明与 v2 protocol 声明，并枚举两者的静态 issues/missing。`known_blocker_codes` 是可从这些声明及当前证据规则确认的缺口；未知字符串统一为 `unknown`，引导人工检查，不原样输出。历史导入器已经定义的工作区、工具正文省略、父链/分支和非 legacy 回放缺口，以及原生适配器的未知字段代码，均保留具体静态代码并映射到相应材料清单；不是从待审数据中学习或放行任意标签。

v1 沿用 exporter 的例外：缺少 `system_prompt` 仅记录覆盖范围；没有 tool_use 的纯文本记录可以把 `tool_definitions` 作为信息性缺失。它们进入 `informational_missing`，不会被误列为补齐训练前提。v2 ordered-event 训练要求两个质量视图都完整且没有缺失，这些例外不适用。

`evidence.outcome` 直接使用 exporter 的 `_evidence` 规则。`evidence.content` 采用 `training_evidence` 同样的摘要、kind/result 及非空 reviewer/verifier 条件，并在 outcome 可以验证时交叉核对组合规则。由于现有组合函数在 outcome 缺失时提前返回，诊断会独立检查内容维度，避免只显示第一个错误；过程中不会插入临时的虚构通过标注。

证据状态为 `satisfied`、`missing`、`invalid`、`blocked_negative_or_conflicting`，v1 内容维度另可为 `not_required`。`satisfied` 只表示已有标注满足当前匹配规则，不证明标注者判断真实。过期摘要计入 `stale_entries`，不会被当作针对当前内容的审核；也不会自动重新签署。

`reported_rejections` / `reported_blocker_codes` 明确表示导出文件中报告的理由，与 `known_blocker_codes` 分开。拒绝记录通过内部的安全 ID 和 digest 联结；若多个 canonical 来源具有相同 ID 和 digest，它们可能共享这份关联，不能据此推断是某个独有来源造成。v1 的同摘要尤其不意味着 quality 声明相同。`duplicate_sft` 等去重说明不会被视为训练阻断；所有平铺 lineage 来源都被检查，因此共享一个 SFT 行的多个来源会各自显示 `included`。

“全部已知缺口”表示当前版本能够识别并观察到的质量声明、证据维度和拒绝原因全部列出，不承诺穷尽尚未实现的适配语义、业务验收或隐藏上下文。例如一个真实 v2 候选同时有四个 runtime 缺口，且缺少内容审核和任务结果证据，报告会一次展示这些维度；解决其中一项不意味着剩余项自动通过。

## 动作清单

`required_actions` 仅是静态材料清单，每项的 `result` 都为 `reinspection_required_no_automatic_eligibility`。

| action | 需要的真实材料与边界 |
| --- | --- |
| `recover_original_source` | 明确授权的原始 transcript 中实际保留的尾部回复、工具正文或原生事件；历史脱敏省略项需先重新核验隐私上下文，不能从占位符补造正文或成功状态 |
| `verify_historical_workspace_state` | 会话当时的工作区快照、已跟踪/未跟踪文件及排除规则状态和摘要绑定的原日志；当前迁移目录通过隐私检查不会消除历史状态缺口 |
| `recover_historical_workspace` | 获授权的旧工作区快照或有证据的迁移对应关系、当时的文件排除与隐私上下文，以及摘要绑定的原日志；同名当前目录不等于原工作区已恢复 |
| `recover_history_ancestry` | 获授权的父分支或 base history、经核实的原生父链及顺序，以及回放语义；不能按文件名或时间直接拼接日志 |
| `capture_request_context` | 原始顺序、模型可见上下文、实际采样/运行时配置或原始工具目录；不是从结果倒推上下文 |
| `implement_runtime_adapter` | 有依据的原生协议语义及适配回归测试；仅采到某个字段并不代表已经理解其作用 |
| `supply_observed_tool_contracts` | 仅针对 v1，通过现有 import/recover 的 `--context` 提供同会话真实函数 schema；不得从调用参数猜造 schema |
| `review_exact_scrubbed_content` | 检查准确的脱敏上下文与 custom code，留下绑定该 digest 的真实内容判断 |
| `verify_task_outcome` | 独立检查完整任务实际结果，记录真实判断；缺少正向证据不等于任务成功，也不意味着应提交 accepted |
| `resolve_negative_or_conflicting_evidence` | 保留既有负评，独立纠正任务或采集真正发生的新轨迹；不能靠删除冲突或追加一条通过审核来消除负评 |
| `manual_inspection` | 检查授权范围内的原始材料和规则，处理未知原因或格式问题 |

v2 的工具目录必须来自原始有序事件；本报告不会建议用 v1 `--context` 给 v2 注入目录。采样和 runtime 语义缺口可能同时需要补采与实现适配器，不能只通过人工审核跨过。

若存在当前摘要绑定的负评，或有已关联的跨记录冲突报告，`blocked_by_negative_or_reported_conflict=true`。此时不会给出 `verify_task_outcome` 或 `review_exact_scrubbed_content` 作为通过该旧记录的建议，而是保持阻断并要求独立修正。真实负评不会变成自动选择 DPO chosen 的依据。

## 隐私、资源与验证

诊断复用 audit 的受限快照：manifest 16 MiB、全部六个输入文件合计 256 MiB、单 JSONL 物理行 16 MiB、五个 JSONL 合计 1,000,000 行。拒绝符号链接、非普通文件、复制过程中变化及摘要不符；严格验证器只读取私有快照。临时目录为 `0700`、文件为 `0600`，结束后清理。

逐轨迹报告额外限制为最多 1,000 条 canonical 轨迹、序列化报告最多 8 MiB。超限返回 `diagnosis_record_limit_exceeded` 或 `diagnosis_report_limit_exceeded`，`diagnosis_available=false`、`records=[]`，不静默截断。若在快照验证前因声明条数超限退出，`structural_valid=false` 表示尚未验证；若验证完成后报告超限，可保留 `structural_valid=true`，但仍不是成功诊断。

所有输出错误与行动代码为固定值，未知错误统一处理，异常正文不回显。测试使用合成数据，覆盖 v1/v2、零训练行、未知值隐私、摘要过期、负评/冲突、双审核缺失、多来源去重、篡改、来源变动和硬上限。测试不读取用户历史会话，也不表示已完成真实平台或生产训练验收。
