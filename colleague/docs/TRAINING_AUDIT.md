# 离线交付质量报告

`nous_training_audit.audit_dataset(path)` 对已导出的数据集生成聚合报告。它先进行有界复制和完整性检查，再对私有快照执行现有严格验证；只有验证通过才统计。验证器与统计都不直接读取可变的原目录。没有模型调用、训练、上传、历史会话扫描，也不会改变原数据集或补造标注。

从 `colleague` 目录使用主 CLI：

```sh
python3 hooks/nous_training.py audit --dataset /absolute/path/to/dataset
python3 hooks/nous_training.py audit --dataset /absolute/path/to/dataset --require-training-rows
```

默认命令在结构有效时返回 0，允许检查零训练行的候选数据集；`--require-training-rows` 另外要求至少一条 SFT 或 DPO，否则返回 1。无论是否使用该参数，报告都会区分 `structural_valid` 与 `training_rows_present`。

从 `colleague` 目录运行 Python API：

```sh
PYTHONPATH=hooks python3 -c '
import json, sys
from nous_training_audit import audit_dataset
print(json.dumps(audit_dataset(sys.argv[1]), ensure_ascii=False, indent=2))
' /absolute/path/to/dataset
```

输入目录须包含 `nous.training.manifest/2` 的 `manifest.json` 以及五个 JSONL 文件：`trajectories.jsonl`、`preferences.jsonl`、`sft.jsonl`、`dpo.jsonl`、`rejected.jsonl`。支持已实现的 `trl`、`openai` 和 `codemode-events-v1` profile；这不扩大它们各自的训练器或 API 兼容范围。

## 如何理解结果

报告 schema 为 `nous.training.audit/1`：

| 字段 | 含义 |
| --- | --- |
| `status` | `structurally_valid_with_training_rows`、`structurally_valid_without_training_rows` 或 `invalid` |
| `structural_valid` | 对实际统计快照的完整严格验证是否通过 |
| `statistics_available` | 是否已经从通过验证的快照生成汇总；失败时不输出统计块 |
| `training_rows_present` | 通过验证的快照中是否至少有一行 SFT 或 DPO；`statistics_available=false` 时不能据此推断原目录为空 |
| `validation_errors` | 固定错误码及出现次数，不含原始错误文本、文件路径或记录 ID |
| `reasons` | 静态处理建议或失败原因 |
| `production_readiness`、`model_effectiveness`、`rights_and_consent` | 一律为 `unverified` |

结构通过表示当前验证器的文件完整性、样本字段、工具闭环、证据和 lineage 规则通过。报告不检查标注者是否诚实，不证明实际模型效果、真实任务完成率、生产稳定性、商业交付权利或数据授权。报告不计算混合这些概念的“质量分”。

零训练行仍可以是有效的候选数据集，此时 `structural_valid=true`、`training_rows_present=false`。`reasons` 包含 `no_training_rows` 与 `inspect_rejections_and_bound_evidence`：先查看拒绝原因、补齐真实上下文或进行针对准确内容的审核，再重新导出。canonical 和 preference 都为空时，另提示 `collect_or_import_explicitly_authorized_sources`。报告不会自动接受候选、推断偏好或启用持续采集。

## 统计口径

- `counts`：快照中实际 canonical 轨迹、canonical 偏好、SFT、DPO 和拒绝记录行数。不会用 manifest 的输入计数代替实际文件行数。SFT 的行数已经经过 exporter 去重；CodeMode 一条轨迹可以产生多条 assistant target。
- `distributions`：轨迹的 `agent.id`、两类 canonical 的 schema 与来源类别。只输出固定类别；未知来源值一律合并为 `unknown`。模型名、版本字符串及来源路径不进入分布。
- `quality`：轨迹声明的 complete/incomplete/unknown，以及具有各类 issue、missing 字段的轨迹数。同一轨迹重复同一原因只计一次；未知原因合并为 `unknown`。canonical 可以保留尚不合格的候选，所以声明 complete 与实际训练行数须分开看。
- `rejections`：拒绝记录按静态原因及阶段计数。一次输入可以因重复、证据或格式被记录多次；拒绝行数不能直接当作“失败任务数”，也不能直接当作产出率分母。
- `evidence`：所有 canonical 标注类型、摘要绑定的标注类型、静态结果类型，以及最终训练行的合格证据类型。一个训练行可以同时具有人工与 verifier 证据，因此分项和可能大于训练行数。`digest_bound` 只表示内容摘要相同；是否满足严格导出条件由验证器与训练 lineage 核对，不能把任意声明当作有效验收。

`coverage` 按最终训练行统计实际引用的来源：

- SFT 使用所有 lineage origin，保留去重后合并的多个来源；DPO 通过 canonical 偏好的 `source_record_id` 关联轨迹。同一 ID 有多个 canonical 来源时全部计入已声明来源。报告不会输出这些 ID。
- `distinct_source_projects` 与 `distinct_source_session_groups` 是已知来源身份的去重数。会话组优先使用 `session_group_id`，否则使用 `session_id`；同项目内已声明的 `session_group_id`/`source_transcript_id` 别名连接为一组。不同项目的同名会话不合并。
- 历史低层 API 允许不绑定轨迹的 DPO。这样的训练行可通过结构验证，但报告将其计入 `training_rows_with_unresolved_sources`，不会凭偏好记录中的任意项目或会话声明推断真实来源。缺少项目/会话身份的行单列为 `training_rows_with_incomplete_source_identity`。
- `distinct_origin_links` 是去重后的训练行到 canonical 来源及 target 的链接数。`additional_origin_links_after_deduplication` 等于该数减去训练行数，表示同一训练行额外保留的来源；`duplicate_lineage_entries` 单独统计完全重复的 lineage 条目，不让它们放大其他计数。
- `training_rows_sharing_source_session` 是与其他训练行共享至少一个已知会话组的行数；`repeated_session_row_links` 对每组累加 `max(行数-1, 0)`。CodeMode 的多个 target 或同一来源的 SFT/DPO 都可能贡献重复。这是来源集中程度的描述，不表示文本重复，也不能替代 split 的泄漏检查。

## 合成标记

只认可 canonical `provenance.synthetic` 的布尔 `true` 或 `false`。字符串 `"true"`、模型名、文件名、提示词和 manifest 顶层 `synthetic` 不被当作逐来源事实。未标记或格式不明确的来源一律为 `unknown`。

`canonical_by_explicit_marker` 分别统计轨迹与偏好。训练行检查所有 lineage origin；DPO 还检查其关联轨迹的标记，分为：

- `all_origins_explicit_synthetic`：全部已关联来源都显式为 true。
- `all_origins_explicit_non_synthetic`：全部显式为 false。
- `mixed_explicit`：同时出现 true 和 false，且没有未知来源。
- `unknown_or_partially_marked`：有缺失、非布尔标记或未解析的来源。

`all_origins_explicit_synthetic_fraction` 以实际 SFT+DPO 行数为分母，只统计第一类；零训练行时为 `null`。另有 `training_rows_with_any_explicit_synthetic_origin` 表示至少一个来源显式为 true 的行数。两者描述已声明标记，不验证这些声明是否真实，也不能将 unknown 自动视为真实用户数据。

## 快照、隐私和失败处理

报告先固定 manifest 字节，闭合校验复制需要的文件清单、非负字节/行数与 SHA-256 字段；复制时逐文件检查大小、摘要和读前/读后 stat，再核对源 manifest 未变化。之后仅把私有快照交给验证器，避免“验证旧目录、统计新内容”，也避免验证器在原目录 stat 后打开被替换的 FIFO 或过大文件。数据在完成快照后发生变化，不影响已经固定的本次统计；报告不保证原目录之后一直不变。

硬上限为 manifest 16 MiB、manifest 加五个 JSONL 文件合计 256 MiB、单条 JSONL 物理行 16 MiB（含换行字节），以及五个 JSONL 文件合计 1,000,000 行。空行也计入物理行数，防止大量空行在严格验证阶段消耗无界 CPU。声明的大小/行数和实际读取大小/行数都检查；逐行读取最多上限加一个字节以识别越界。上限只决定此版本审计器接受的输入规模，不表示训练质量阈值；超限分别返回 `audit_manifest_too_large`、`audit_dataset_too_large`、`audit_line_too_large` 或 `audit_too_many_records`，不截断、不送入验证器、不生成部分统计。

临时快照目录权限为 `0700`，文件为 `0600`，结束时清理。只复制 manifest 声明的五个固定 JSONL 文件，不复制目录中其他文件；读取拒绝符号链接与非普通文件。报告不包含正文、工具参数、标注者、私密路径、项目 ID 列表或会话 ID 列表。

验证原因、质量缺口和拒绝原因都经过固定白名单。未知验证错误变为 `validation_error`；未知统计类别变为 `unknown`。即使输入提供看似错误码的私密字符串，也不会原样输出。白名单需要随新协议错误码显式维护。

验证失败时返回 `invalid` 和安全的 `validation_errors`；复制阶段发现非法 manifest 使用 `dataset_validation_failed`，快照严格验证失败使用 `snapshot_validation_failed`。快照读到变化或字节/摘要不匹配时返回 `dataset_changed_during_audit`；不安全或不可读源文件返回 `audit_source_unavailable`；其他 I/O 或解析意外只返回静态 `audit_failed`。这些情况不输出可能混合不同版本的统计。

实现会在临时目录保存快照，内存中保存 manifest lineage 和来源摘要元数据；它不是常量内存的超大数据扫描器。测试只覆盖合成数据中的篡改、零产出、多来源去重、证据分类、CodeMode 多 target、未知字段隐私和文件变动，不能据此宣称完成生产或商业数据验收。

## 历史导入类别

`historical_transcript`、`historical_repair` 和 `derived_standalone_text_task` 是已知的来源声明类别；统计这些类别不证明来源真实、训练资格或授权。固定 gap 白名单包含历史工作区不可核验、`historical_workspace_state_unverified` / `historical_workspace_state`（当时的文件与排除状态仍未核验）、工具正文已省略、原生协议未恢复、父链/分支以及非 legacy 回放语义等应用代码，不再把它们合并成 `unknown`。仍不认识的任意字符串继续归入 `unknown`，不会把来源正文、路径或私密标签带入报告。该修复只改善诊断可读性，不改变导出器质量门槛、不补写内容或审核证据。
