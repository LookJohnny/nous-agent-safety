# 本地 AI 使用轨迹与训练 JSONL

a8 支持一次性回溯所有本机 Claude Code / Codex 项目，含压缩日志与原工作目录已缺失的保守恢复。不会开启实时采集；先计划、再导入，保留源文件和旧审核。见 [历史导入](TRAINING_HISTORY.md)。

a7 增加显式多请求工具 episode：保存完整原生上下文、并行结果与错误重试，整条成功和中间步骤分别审核。使用 `request-episode-sft-v1` 导出；[集成方式](TRAINING_EPISODE_CAPTURE.md)、[结构与标签](TRAINING_EPISODE.md)、[实机边界](TRAINING_MULTITURN_LIVE.md)。

当前为 `0.1.0a9` 本地开发预览版。发布包构建、离线自检与支持边界见 [TRAINING_RELEASE.md](TRAINING_RELEASE.md)。真实客户端验收另行记录，离线 demo 通过不代表平台 hooks 已执行。a9 历史质量修复的调用与限制见 [TRAINING_HISTORY_REPAIR.md](TRAINING_HISTORY_REPAIR.md)。

先在仓库的 `colleague/` 目录运行两条命令。输出目录必须尚不存在，父目录必须存在：

```bash
python3 hooks/nous_training.py demo --out "$PWD/training-demo"
python3 hooks/nous_training.py validate --dataset "$PWD/training-demo"
```

`demo` 使用手写的 Claude Code、Codex 合成记录和工具定义，完整演练导入、审阅、SFT/DPO 导出与校验。它使用临时数据目录，不读取真实会话、不安装 hooks、不启用持续采集。输出明确标记 `synthetic: true`；这些例子用于检查程序能否跑通，不代表真实任务结果、生产标注或模型效果。源例子见 [examples/training](../examples/training)，流程见 [nous_training_demo.py](../hooks/nous_training_demo.py)。

本模块在本机将平台会话归一为可检查的轨迹，并将通过检查的样本导出为 JSONL，没有自动上传步骤。**采集默认关闭，实际采集需要单独授权项目。** 格式和证据检查帮助筛选数据，但不保证每条回答正确，也不授予内容使用权。

新版 Codex Code Mode 使用 `trajectory/2` 原生事件和独立的 `codemode-events-v1` profile，见 [CODEMODE.md](CODEMODE.md)。常规 `trl/openai` 示例仍适用于原有 `trajectory/1`。生产训练前可以使用 [分组划分](TRAINING_SPLIT.md)；实际 TRL 加载与训练验收见 [TRAINING_TRAINER.md](TRAINING_TRAINER.md)。

请求级采集使用 `trajectory/3` 和 `request-sft-v1`；实际请求上下文与原生回答共同绑定，保留在客户端边界观察到的结构。通过任务验证和独立内容审核后才进入训练集。显式 API/CLI 接入、离线 demo 及支持范围见 [TRAINING_OBSERVER.md](TRAINING_OBSERVER.md)、[TRAINING_REQUEST.md](TRAINING_REQUEST.md) 与 [TRAINING_VERIFY.md](TRAINING_VERIFY.md)。这不是现有 Stop hook 自动获取完整请求的升级。

## 接入 Claude Code 和 Codex

以下命令仍在 `colleague/` 下执行。先把示例换成真实、已存在的项目绝对路径：

```bash
python3 hooks/nous_training.py setup --agent both --project "/绝对路径/你的项目"
python3 hooks/nous_training.py doctor --agent both --project "/绝对路径/你的项目"
```

**执行 `setup` 即安装所选平台的独立采集 hook，并启用这个项目的本地采集及导出。** `--agent` 可选 `claude`、`codex`、`both`。安装器备份已有配置，保留其他 hook 与设置，通过固定标记识别自身 handler；重复运行不会重复添加。配置损坏时停止，避免覆盖。备份与新配置文件权限为 `0600`。

独立 hook 只使用同步 `Stop` 事件，不依赖 Nous 同事后台、Lens、飞书/Slack、旧安全安装器或 `[trace] level`；`trace=off` 也不关闭这个独立入口。hook 使用安装时的 Python 和脚本绝对路径，固定本地数据目录与脱敏 ledger 目录。移动代码目录或更换 Python 后应重新运行 `setup`，并再次检查平台信任状态。

Claude Code 和 Codex 可能要求信任工作区、新的 hook 或变更后的命令；配置已写入不等于平台已授权执行。`doctor` 只读检查配置、运行文件和项目采集开关，不试跑 hook、不读取会话、不替用户接受信任。它将平台信任状态标为未知，并报告已知的禁用设置或缺失项；已有 `disableAllHooks` 不会被自动打开。请在对应平台完成其要求的信任操作，再产生一轮会话，通过下面的 `list` 确认确有记录。

项目按规范化绝对路径识别，应与 hook 的 `cwd` 一致。从项目子目录启动可能成为另一个项目标识。

采集发生在未来的 `Stop`，读取的是 **该 session 当前 transcript 的完整内容，包括启用前已发生的轮次**；它不会扫描历史会话目录，也不会回填其他 session。同一 session 后续刷新同一条缓存；内容变化后，旧审阅摘要不再适用。

```bash
python3 hooks/nous_training.py status
python3 hooks/nous_training.py health --project "/绝对路径/你的项目"
python3 hooks/nous_training.py list --project "/绝对路径/你的项目" --limit 20
```

`list` 返回记录 ID、消息数量、质量标记、缓存路径与导出授权状态，不直接打印会话正文。打开指定缓存查看已脱敏消息、工具结果及 `quality` 后，再作审阅。

当前只接入 `Stop`：中断、API 错误、空 `transcript_path`、超出处理上限或尚未落盘的尾部可能导致缺少记录或部分记录。入口会进行一次有界短暂等待后重读，缓解日志落盘延迟；仍有明确缺口时标记不完整，只使用真实文件，不拼造正文。此类会话可在文件完整后手动导入。平台 transcript 并非稳定的公共数据交换格式；本模块没有承诺覆盖所有 Claude Code/Codex 版本。

## 审阅、导出、校验

把 `list` 或导入结果中的 ID 替换到下面命令中；导出目录必须尚不存在：

```bash
python3 hooks/nous_training.py review \
  --id "t_替换为记录ID" --reviewer "reviewer-01" --result accepted

python3 hooks/nous_training.py export \
  --out "$PWD/training-export-001" --profile trl

python3 hooks/nous_training.py validate --dataset "$PWD/training-export-001"
```

审阅判断的是 **整条记录对应的任务结果和回答是否可接受**。`review` 记录判断，不代替运行测试或核实结果；不合格用 `--result rejected`。`--reviewer` 使用自选代号，不读取系统账户姓名。`accepted` 不能消除缺失工具结果、部分回复等结构缺口。

SFT 证据绑定 `record_digest(record)`，即 canonical `{messages, tools}` 的规范 JSON SHA-256；其中的消息来源标识等也是摘要的一部分。支持 `human_review` 的 `accepted`，以及 Python API 中明确针对整个任务的 `verifier`/`passed`。后者必须提供非空验收器标识和同一摘要；单次 shell 的 `exit_code=0` 不会自动成为任务通过证据。

导出先检查全部输入再发出训练样本。同一内容摘要出现否定或冲突评价时，相关候选被隔离；输入先后顺序、重复 `accepted` 都不能覆盖否定。对同一 prompt 与候选答案出现相反 DPO 偏好时，两种方向均被排除。原因保存在 `rejected.jsonl`，便于回查和重新标注。

`validate` 离线检查 manifest 版本和完成状态、每个文件的行数/字节数/SHA-256、JSONL 结构、训练字段、工具闭环、参数 schema、审阅证据、逐行来源关联、重复样本及冲突。返回 `valid: true` 时退出码为 0，失败时为 1；错误只包含原因码、文件和行号，不打印正文。校验确认导出物内部一致性，不认证标注者身份，也不独立证明事实正确或数据权利。

### 中断与写入延迟恢复

`health` 只读取本地采集计数、时间、静态错误码和记录编号，不扫描平台历史。`collected` 表示本地记录已保存，不表示平台信任已认证、任务成功或内容已达到训练标准。详细字段和保留上限见 [TRAINING_HEALTH.md](TRAINING_HEALTH.md)。

Stop 可能早于日志最后一行落盘，任务中断也可能没有 Stop。已有候选记录可在源日志写完后显式恢复：

```bash
python3 hooks/nous_training.py recover \
  --id t_替换为已有记录编号 --agent codex \
  --project /绝对路径/原项目 --transcript /绝对路径/原会话.jsonl
```

恢复仅更新这一条已授权记录，必须匹配原项目、原日志路径和已有会话身份；不会搜索其他历史、重新授予撤回的权限或自动添加审核。原文件缺失、改用另一会话、日志退回更早位置、并发撤回或其他进程修改同一记录时均拒绝覆盖。未捕获到任何记录的任务应使用显式 `import`，不要编造记录编号。

内容、质量或来源摘要改变后必须重新审核；完全相同的内容才保留旧证据。采集器先有界读取一份不可变内存快照，两种解析视图共用它，不将原始正文写入临时文件。快照读取期间文件变化会返回 `transcript_changed_during_read`，Stop 最多重试一次；读取完成后的追加不会混进同一条记录。等待日志稳定后可显式恢复。单个源文件限制为 128 MiB、单物理行 8 MiB、100 万物理行；超限整体拒绝，不截断。`recover` 不会修改原始平台日志。

### 导出后的质量报告

```bash
python3 hooks/nous_training.py audit --dataset /绝对路径/导出目录
# 在训练流水线中要求至少有一条 SFT 或 DPO：
python3 hooks/nous_training.py audit --dataset /绝对路径/导出目录 --require-training-rows
```

报告先对受限私有快照做独立校验，再汇总样本数、隔离原因、平台与格式、来源覆盖、证据类型以及显式合成数据标记；不输出正文或来源 ID 列表。默认结构有效即退出 0，允许检查只有候选的导出；加 `--require-training-rows` 后，零训练行也返回非零。报告不生成主观质量评分，也不代表模型效果或生产验收已经通过。字段和资源上限见 [TRAINING_AUDIT.md](TRAINING_AUDIT.md)。

需要定位具体候选的阻断原因时，使用只读诊断：

```sh
python3 hooks/nous_training.py diagnose --dataset /绝对路径/导出目录
python3 hooks/nous_training.py inspect-source --transcript /绝对路径/明确选择的Codex日志.jsonl
```

`diagnose` 在验证通过的私有快照上列出每条轨迹的已知缺口、审核状态与需要的材料；用物理行号、内容摘要和数据包 manifest 摘要定位，不输出正文。`inspect-source` 只读取明确提供的单份 Codex 日志，报告源事件位置、可观察上下文、采样标记和重复指令来源，不扫描会话历史。两者都不修复、导入、审核或重新放行数据。来源未知、负面或冲突证据不会通过添加一条“accepted”建议而被覆盖。完整说明见 [TRAINING_DIAGNOSE.md](TRAINING_DIAGNOSE.md) 和 [TRAINING_CONTEXT.md](TRAINING_CONTEXT.md)。

原始日志的 SHA-256 用于绑定报告对应的字节，不是匿名化或真实性证明。历史采样水位、状态指纹、工具定义存在，都不足以证明最终模型请求的完整输入；仍需请求层的实际观察及其与目标输出的关联。内容已删除或从未保存时，不能用猜测的提示词补齐训练样本。

升级与卸载见 [TRAINING_LIFECYCLE.md](TRAINING_LIFECYCLE.md)。停止某个项目使用 `disable --project ...`；`uninstall` 移除所选平台的共享采集 hook，影响使用同一数据目录的所有项目，但不会自动删除记录或撤回导出权限。

## 只导入选定文件，或补充真实工具定义

```bash
python3 hooks/nous_training.py import \
  --project "/绝对路径/你的项目" \
  --transcript "/绝对路径/所选会话.jsonl" \
  --agent codex --session "这个会话的稳定编号"
```

`--agent` 支持 `auto`、`claude`、`codex`。`--session` 可省略，届时按 transcript 路径确定会话；重新导入同一会话应沿用原编号。导入只授权指定记录，并允许这条记录导出；不会为未启用项目打开持续采集，也不会授权项目里其他缓存。平台原 transcript 不修改。

缓存默认位于 `~/.nous-colleague/training/records/<ID>.json`。可通过 `NOUS_HOME` 选择其他本地数据目录；相关命令应使用一致的环境。脱敏 ledger 默认位于该目录的 `ledger/`；若自定义 `NOUS_LEDGER_HOME`，`setup` 会把它固定到 hook 命令，各次 CLI 调用也应使用同一设置，避免脱敏标识不一致。

工具样本需要会话实际使用的定义。平台记录缺少定义时，可用 `--context` 提供 JSON 文件，顶层为 `{"tools": [...]}`。数组元素使用 function schema 形状：`type: "function"`，`function` 包含 `name` 和真实 `parameters`，参数顶层类型为 `object`：

```bash
python3 hooks/nous_training.py import \
  --project "/绝对路径/你的项目" \
  --transcript "/绝对路径/所选会话.jsonl" \
  --agent claude --session "原会话编号" \
  --context "/绝对路径/这次会话的实际工具定义.json"
```

工具名和一次参数值不能还原完整定义，因此实现不反推 schema，也不伪造系统提示。`--context` 的真实性由提供者确认，并标记 `operator_supplied_context`。此入口只补充工具定义；补充后需要重新审阅。没有定义时保留候选，不为了增加训练行数放宽要求。

## JSONL 与来源关联

五个 `.jsonl` 文件均为 UTF-8 JSONL，一行一个 JSON 对象；`manifest.json` 是独立 JSON 清单。目录权限为 `0700`，文件为 `0600`。消费数据前先运行 `validate`；失败或中断可能留下没有完整 manifest 的目录，不应投入训练。

| 文件 | 内容与用途 |
| --- | --- |
| `trajectories.jsonl` | `nous.trajectory/1` canonical 观测记录。包括已脱敏消息、工具定义、来源、质量与证据；未审阅或上下文不完整的候选也可保留在这里。 |
| `preferences.jsonl` | canonical 显式偏好记录，保留 prompt、chosen、rejected、工具、来源及偏好证据，供 DPO 来源核对。这里有记录不代表已获准进入 DPO。 |
| `sft.jsonl` | 通过检查的 conversational `messages`，存在真实工具定义时另带 `tools`；不混入审阅和来源元数据。 |
| `dpo.jsonl` | 通过检查的显式 `prompt`、`chosen`、`rejected`，必要时附 `tools`；不从重试顺序、时间或相邻失败猜测偏好。 |
| `rejected.jsonl` | 排除阶段、原因码、ID 和摘要，不复制正文。`stage=sft` 不表示 canonical 已丢弃。 |
| `manifest.json` | `nous.training.manifest/2`，包括完成状态、profile、文件统计与校验和、拒绝计数、脱敏版本和逐行 `lineage`。 |

manifest 的训练行关联包含：

- `format`/`line`：`sft.jsonl` 或 `dpo.jsonl` 中从 1 开始的行号。
- `source_file`/`source_line`：对应 `trajectories.jsonl` 或 `preferences.jsonl` 中从 1 开始的 canonical 来源行。
- `record_digest`：接受审阅的 canonical 内容摘要；SFT 为 `{messages, tools}`，DPO 为 `{prompt, chosen, rejected, tools}`。
- `sample_digest`：最终训练字段的规范 JSON SHA-256。校验器从来源重新转换样本，核对它与训练文件行一致。
- `evidence_kinds` 与 `provenance`：有效证据类型和来源信息；DPO 保留 `source_record_id` 关联。

canonical 去除完全相同的观测副本，保留内容相同但标注改变的记录；SFT/DPO 按最终训练字段去重。多个来源可以关联同一训练行。当前去重不等于跨工具调用 ID 的语义去重。`provenance.source_sha256` 是脱敏后观测消息的摘要，不是平台原文件字节的校验和。

默认 `--profile trl` 使用对话 `messages` 和显式 DPO `prompt/chosen/rejected`，函数参数为 JSON 对象，工具定义置于 `tools`。消费者仍需选择支持相应字段和工具的模型 chat template 与 TRL/datasets 版本；格式校验通过不保证任意训练器都能直接加载。

`--profile openai` 将函数参数编码为 JSON 字符串。该名字仅表示聊天消息中的参数编码；DPO 仍是显式 `prompt/chosen/rejected`，不是某个 OpenAI 微调 API 的完整上传协议，也不保证满足具体模型的限制。

## 显式 DPO 偏好

准备一份显式标注的偏好 JSONL 后导出：

```bash
python3 hooks/nous_training.py export \
  --out "$PWD/training-export-002" --profile trl \
  --preferences "/绝对路径/已审阅偏好.jsonl"
python3 hooks/nous_training.py validate --dataset "$PWD/training-export-002"
```

输入每行使用 canonical 消息块，包含 `id`、共用的 `prompt`、两个完整候选 `chosen`/`rejected`、真实 `tools` 和 `provenance.source_record_id`。来源 ID 必须指向仍被授权导出的本地缓存；完整 prompt 必须与该来源实际观测到的上下文前缀及工具定义一致。chosen 可以是人工改进答案，无需假装原模型已经产生过它。证据采用 `kind: "human_preference"`、`result: "chosen"`、非空 `reviewer`，或 `verifier_preference` 与非空 `verifier`；`record_digest` 必须等于 `preference_digest(pair)`。具体构造见 [demo 实现](../hooks/nous_training_demo.py)。

新偏好默认使用 `nous.preference/2`，摘要同时绑定 `source_record_id` 和可选的 `project_id`。必须先确定来源再签署证据；在签署后换来源或新增项目字段会使摘要失效。提供 `project_id` 时它必须等于引用来源的项目身份。已有显式 `nous.preference/1` 保持旧摘要算法，但仍接受新的来源前缀核验；不能将无关任务挂靠到一个有效来源 ID。Code Mode 来源的 DPO 当前明确不支持。

CLI 会再次脱敏候选。如果内容变化，原摘要失效，程序不会替审阅者重新签署；应先审阅脱敏后的候选，再绑定摘要。`demo` 内的偏好判断仅适用于其手写例子，不能复用为真实用户数据的证据。

## 工具质量与观测边界

- 工具样本需要真实定义、唯一调用 ID、每次调用匹配的结果和最终助手回答。连续的纯工具调用消息会在训练格式中合并，允许并行调用的结果逆序返回。
- 已明确标记失败的工具结果保留 `is_error: true`。训练消息的 `tool.content` 使用 JSON 字符串包装 `{"is_error":true,"content":原结果}`，避免把失败悄悄改成成功；manifest 用 `tool_error_content` 声明这一约定。失败工具可以是正确恢复任务的一部分，是否接受仍取决于整个任务证据。
- 工具参数按保守的 JSON Schema 子集验证：类型、properties/required/additionalProperties、items、enum/const、数字/长度/数量边界、uniqueItems、pattern，以及 anyOf/oneOf/allOf。`pattern` 使用 Python 正则语义；这不是完整 JSON Schema 实现。
- `$ref`、`format`、条件分支等未实现约束，以及未知 schema 关键字，不会被默默忽略：保留 canonical 候选，但拒绝进入 SFT/DPO。不能为了通过校验删除真实约束。支持范围以 [nous_training_schema.py](../hooks/nous_training_schema.py) 为准。
- 普通文本样本可以缺系统提示，缺失事实记录在 `quality.missing`；不会补造系统消息。观测上下文不保证等于模型当时收到的全部上下文。
- 压缩上下文、sidechain、未知记录、损坏 JSON、未完成尾行、末条助手消息明确停在 commentary/partial、缺失媒体或遮蔽敏感文件结果会影响完整性。已知缺口可以留在 canonical，但不能通过一次 `accepted` 进入严格训练集。
- thinking、reasoning、analysis 不采集。图片保留遗漏标记，不生成多模态训练样本。Codex 自定义工具原始输入可保留为观测；当前 exporter 不将它伪装成函数参数。
- 合法且不含识别出的敏感值的工具 ID 保持原样；含敏感内容或不符合标识要求的元数据会替换、标记质量缺口，并保持已识别的调用/结果映射。脱敏可能改变结构或类型，相关记录不直接进入严格训练集。

没有审阅、缺工具定义或存在质量缺口时，`sft.jsonl`、`dpo.jsonl` 可以为空；看 manifest 和拒绝原因再处理。导出后可用 `split` 按来源 project/session 分组，避免同一任务及其相邻快照跨集合泄漏；导出本身仍不自动划分。此模块没有实现生产训练任务调度、完整可执行重放或模型效果验收。

## 停止、撤回与删除

```bash
# 停止这个项目后续采集及导出，保留缓存。
python3 hooks/nous_training.py disable --project "/绝对路径/你的项目"

# 停止并删除这个项目的 training 缓存。
python3 hooks/nous_training.py disable --project "/绝对路径/你的项目" --purge

# 以下两个入口属于完整 Nous 仓库，最小训练发行包不包含 nous_consent.py。
# 全局撤回同时停止全部本地 training 采集及导出。
python3 hooks/nous_consent.py revoke

# 全局删除入口同时清除本地 training 记录。
python3 hooks/nous_consent.py delete-my-data
```

`disable` 不卸载 hook，但已禁用项目不会继续读取 transcript。`revoke` 保留缓存；`delete-my-data` 或项目级 `--purge` 清理相应缓存。已导出目录及其副本、平台自己的原 transcript 不在缓存删除范围内，需另行处理。

脱敏失败时停止收集，不以原文降级保存。模块复用密钥和已支持个人信息的脱敏规则，但无法保证覆盖所有姓名、地址、商业秘密或混合编码。`privacy.scrubbed` 说明处理器已运行，不是“完全没有敏感信息”的证明。

含明确文件路径参数的工具调用还会检查 Git 忽略规则。Git 不可用、检查超时或项目不是可检查的 Git 仓库时，训练采集保守省略该调用的输入与结果，并标记质量缺口；不会因为忽略规则无法核实而保留原始文件正文。`doctor` 的 Git 检测只确认命令是否可找到，实际文件排除检查在采集时执行。

本地 `setup`、`enable`、`import`、`review` 不授权上传、销售或向第三方提供数据。外部使用仍需核实代码、对话、工具输出的使用权，以及 完整版的数据计划与分项同意流程（未随此最小快照提供）；数据结构、脱敏和通过校验本身都不能证明商业授权。

## 与已有安全和轨迹功能并用

已有 Nous 安全 hooks 可继续使用；`enable --project` 仍只变更项目授权，不安装 hook。旧安全入口经 `nous_trace` 调用采集时，仍受旧 trace 开关约束；新增独立 hook 不受它影响。两种入口同时触发时，同一会话使用稳定记录 ID 去重，不因为配置看起来存在就跳过另一条未经执行确认的入口。

旧 `hooks/nous_trace.py export --format dpo|prm|replay` 为既有用户保留，标签和配对含启发式规则，该旧导出器及说明未随此最小快照提供。需要本文的证据、schema、manifest/2 和独立校验流程时，使用 `hooks/nous_training.py export`。
