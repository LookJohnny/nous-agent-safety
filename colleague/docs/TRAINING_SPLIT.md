# 按来源分组划分训练集与评估集

`nous_training_split.split_dataset` 将已经通过独立校验的 Nous 导出数据划分为 `train` 和 `eval`。它复制现有 SFT / DPO 行，不重新转换消息、不训练模型、不连接网络，也不修改输入数据集。

默认按项目分组。同一项目的会话、快照和多个助手训练目标全部进入同一侧。可以显式选择按会话分组，但必须确保来源中的会话身份代表真实会话，而不是每次导入或快照新建的身份。

## 调用

命令行入口：

```sh
python3 hooks/nous_training.py split --dataset /绝对路径/已校验的数据集 --out /绝对路径/新的划分目录 --group-by project --eval-ratio 0.1 --seed nous-v1
```

新采集记录会独立生成稳定分组身份，不受 hook 是否传入 `session_id` 或手动导入是否填写 `--session` 影响。原生会话 ID 可用时优先使用它；否则使用规范化 transcript 路径的摘要，同时保留同源文件标识以连接快照。旧数据缺少这些字段时优先选择默认 `project`，不能从不可靠的旧会话标识推断安全划分。

在 `colleague` 目录下可以直接使用 Python API：

```sh
PYTHONPATH=hooks python3 -B - <<'PY'
from nous_training_split import split_dataset

report = split_dataset(
    "/absolute/path/to/validated-export",
    "/absolute/path/to/new-split",
    group_by="project",
    eval_ratio=0.1,
    seed="nous-v1",
)
print({"counts": report["counts"], "warnings": report["warnings"]})
PY
```

参数：

- `dataset`：含完整 `nous.training.manifest/2`、canonical 来源、lineage、SFT / DPO 文件的导出目录。支持原有 `trl`、`openai` 和 `codemode-events-v1` profile。
- `out`：尚不存在的新目录；不能位于输入数据集内部。已有目录、文件或符号链接均拒绝覆盖。
- `group_by`：`project` 或 `session`。默认 `project`。
- `eval_ratio`：0 到 1 之间的有限数值，默认 0.1。0 / 1 可用于显式分到单侧，但会报告空分区。
- `seed`：非空字符串，默认 `nous-v1`。同一输入、参数和种子生成相同划分及报告。

函数返回与 `split-manifest.json` 相同的报告。失败抛出 `SplitError`，消息是静态原因码，不含样本正文或用户路径。

## 分组规则

归属从 canonical 记录和已经验证的 lineage 解析，不从 SFT / DPO 行号猜测。

1. `project` 使用来源轨迹的 `provenance.project_id`。
2. `session` 使用项目身份和会话身份的组合。优先取 `session_group_id`；仅在该字段不存在时回退到 `session_id`。存在但为空或类型错误不会触发回退。同项目内，具有相同 `session_group_id` 或相同 `source_transcript_id` 的来源进一步连接；这样，后续快照才出现原生会话元数据、用户改变导入别名或同会话换了日志路径时，可以通过已观察到的身份联系保持同组。两个字段是独立命名空间，只有同字段、同项目的相同值构成联系。
3. 一个 canonical 轨迹生成的所有目标始终使用同一来源组，包括 CodeMode 一条轨迹生成的多个 prompt/completion 行。
4. 同一最终样本可能经过导出去重而只剩一行，但 lineage 保留多个来源。所有这些组通过并查集合并；连接关系具有传递性。相关组的其他样本也必须跟随这个连通分量。
5. DPO 必须提供 `provenance.source_record_id`，绑定 canonical trajectory 的来源组。普通导出允许的独立偏好记录，如果没有来源关联，在分组时会失败。偏好记录自己的项目字段不能代替来源轨迹。
6. 如果一个 DPO 来源 ID 对应多条、分属不同组的轨迹，所有匹配组保守合并，并报告 `preference_source_id_spans_groups`，不会任取其中一条。
7. 除显式来源外，DPO 的完整 prompt 会与所有 V1 轨迹的实际起始前缀比较，使用与导出器相同的消息归一化忽略消息 ID、时间戳，并要求 tools 上下文完全一致。所有精确匹配的来源组一并连接，防止 A/B 做同一任务但答案不同、DPO 引用 A 却采用 B 答案时与 B 的 SFT 分到两侧。这里只匹配完整前缀，不以最后一条用户消息、模糊相似度或相同答案猜测来源。匹配增加来源组时报告 `cross_source_prompt_groups_connected`。

未获得训练审核的 canonical 快照也可提供会话身份的桥接证据，但不会因此生成训练行。只有包含实际输出样本的连通分量进入报告。V2 原生事件轨迹目前不支持 DPO 来源或这种消息前缀匹配。

缺少实际使用的项目或会话身份时，划分失败；不会降级为逐行随机划分。未经训练导出的候选记录不会仅因存在于 canonical 文件中而自动成为训练样本。

## 确定性分区与比例

每个来源组的身份组合先通过规范 JSON 和 SHA-256 得到组 ID。一个连通分量的组 ID 排序后生成分量 ID，然后计算：

```text
SHA256(canonical_json({
  "algorithm": "sha256-component-threshold-v1",
  "seed": seed,
  "component_id": component_id
}))
```

将这个 256 位整数与 `floor(eval_ratio × 2^256)` 比较，低于阈值进入 `eval`，其余进入 `train`。比例基于独立分量的哈希分配，不保证样本行数恰好达到目标比例。一个大项目或大量跨项目重复样本可能形成一个大分量。

不会为了凑比例把一个分量拆到两侧。增加来源或新发现的重复关系可能合并分量，从而改变新版数据集的划分；固定种子并不保证不同数据集版本中的分量永远留在原侧。发布时应固定输入 manifest 和划分 manifest。

## 输出与可追溯信息

```text
new-split/
  train/sft.jsonl
  train/dpo.jsonl
  eval/sft.jsonl
  eval/dpo.jsonl
  split-manifest.json
```

训练文件保留原始 JSONL 行的字节、数据类型和 profile；每个分区内部保持输入顺序。空文件也会创建并记录哈希。

报告 schema 为 `nous.training.split-manifest/1`，包含：

- 原始输入 `manifest.json` 的 SHA-256、各输入文件摘要，以及原 profile。
- 分组模式、比例、种子、算法版本。
- 各输出文件的行数、字节数和 SHA-256。
- 每个连通分量的组 ID 及其分区。
- 每个输出行对应的输入行、样本摘要、canonical 来源行、已有安全 provenance（包括来源日志 ID），以及 CodeMode 目标边界。DPO 额外记录显式关联的来源轨迹行和所有完整 prompt 前缀匹配来源。
- 各分区的 SFT / DPO 数量、分量数、组数和警告。

报告不复制样本正文，组身份以摘要记录；来源标识沿用导出 lineage 的安全形式。它是划分产物，不冒充完整的 dataset manifest/2。不要把 `train`、`eval` 或整个划分目录作为普通 `validate_dataset` 的输入：它们不包含完整 canonical 文件和原导出 manifest。

## 校验、权限与失败行为

开始时调用独立 `validate_dataset`。随后只把 manifest 声明的五个 JSONL 文件和 manifest 复制到私有临时快照，核对 SHA-256、行数和字节数，并再次独立校验这个实际用于划分的快照。输入文件在首次校验后发生变化、被替换为符号链接或变成不一致的数据时，不会直接进入输出。临时快照在结束时删除。

新输出目录及其子目录权限为 `0700`，文件为 `0600`。输出文件写完并同步后，最后写 `split-manifest.json`。写入中断可能留下没有完整 manifest 的目录；重试不会覆盖它，应选新路径或先人工检查这个未完成目录。

常见失败码包括 `invalid_dataset`、`input_changed_or_hash_mismatch`、`invalid_snapshot_dataset`、`project_identity_missing`、`session_identity_missing`、`preference_source_identity_missing`、`output_already_exists` 和 `output_inside_input_dataset`。

## 如何理解警告和评估边界

- `empty_train_split` / `empty_eval_split`：对应分区没有训练行。一个来源组只有一个归属，因此小数据集出现空侧是正常、明确可见的结果。
- `no_training_samples`：输入没有任何可划分 SFT / DPO 行。
- `partition_nonempty` 只说明两侧各有至少一行。
- `evaluation_validated` 始终为 `false`：此工具验证数据归属与划分，不运行模型评估，也不证明评估设计、标签或指标有效。

默认项目分组比会话分组更保守。会话分组仍可能让同一项目的相似任务、文件或知识同时出现在两侧。本模块能连接已声明的来源身份、已观察到的会话别名、去重 lineage，以及当前数据集中 DPO 的完整精确任务前缀；不能证明来源身份本身真实，也不能自动发现不同导出版本中未记录的共同前缀、近似重复或外部训练数据重叠。

当前测试使用纯合成导出，覆盖多项目/会话、去重多来源与传递合并、CodeMode 多目标、DPO 来源绑定与跨格式精确前缀联系、不同上下文不误合并、会话元数据较晚出现、来源别名桥接、缺少身份、确定性、空分区、源内容变化、已有输出、符号链接和安全权限。
