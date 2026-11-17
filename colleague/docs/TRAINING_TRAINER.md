# CodeMode 事件数据与本地训练验收

`codemode-events-v1` 是 Nous 定义的训练序列化。它把已观测、已脱敏的 Codex 可见事件转换为 TRL 标准的字符串 `prompt` / `completion`；不是 Codex 的模型内部模板，不是 OpenAI 微调 API 上传协议，也不代表模型已经学会使用工具。

## 数据契约

canonical 使用 `nous.trajectory/2`，保留原有 `messages` 视图及 `protocol`。协议是 `nous.codex.events/1`，带 adapter 版本、按源文件物理行排列的事件、完整性状态。证据摘要绑定完整 canonical 内容，包括运行审计；任何内容变化都应重新审阅。

每一行 SFT 只有两个字符串字段：

```json
{"prompt":"{\"representation\":\"observed-visible-events\",\"schema\":\"nous.observed-visible-events/1\"}\n……此前可见事件的 JSON 行……\n","completion":"……下一段连续助手事件的 JSON 行……\n"}
```

这只是说明结构的示意，不能作为训练输入。真实可运行的合成来源是 `examples/training/codemode.jsonl`。序列化采用 UTF-8、排序键、紧凑 JSON，每个事件末尾一个换行；内容中的换行按 JSON 转义。header 明确声明它是观测事件表示，不会虚构 system 提示。`base_instructions` 用显式事件类型表示真实采集到的基础指令。

- `completion` 仅包含连续的助手消息、原生 `custom_tool_call` / `function_call`。相邻助手 commentary 与调用属于同一个目标段。
- 工具输出只出现在后续样本的 `prompt` 中。原始 custom JS、调用 ID、namespace 契约、消息 phase/channel、结构化输出文本及显式错误内容按已脱敏值保留。
- `runtime_tool_execution`、token usage 不进入模型输入或目标，它们保留在 canonical 供审计。物理行 `seq` 及目标索引属于 canonical/lineage，不训练模型生成这些来源元数据。
- manifest lineage 的 `target.start` / `target.end` 是 `protocol.events` 的从零开始、左闭右开区间。校验器从来源重算对应训练字符串与摘要。
- 当前只有 SFT；此 profile 不接受 DPO。没有时序推断的偏好，也不会把一次工具 exit=0 当作全任务成功。

“无损”仅表示允许采集的、已脱敏的可见事件字段能反解，不表示原始文件字节、隐藏思考、媒体或模型全部上下文已经恢复。

## 严格接受条件

导出需要完整基础上下文、在调用之前实际出现的工具 catalog、唯一可解析的工具名、成对调用/结果和最终助手回答。catalog 按追加顺序生效，重复定义或歧义短名拒绝。函数参数执行已有保守 JSON Schema 校验；custom 工具必须保持原类型。除显式自由文本格式外，grammar 只支持协议模块中已知 exec 的 Lark grammar 精确摘要，不会执行输入 JS 或假装已经验证 JS 的运行语义。

完整性缺口、world_state/turn_context 语义未解析、未知内容、缺失契约均拒绝进入训练。真实运行审计不能倒推成当时的模型契约。CodeMode 任务还需绑定摘要的全任务 outcome 证据，以及独立的内容审阅/校验证据；内容审阅不是放行已知质量缺口的开关。这些授权和证据门槛由 collector/exporter 执行，纯序列化 API 不自行授予训练权限。

为限制前缀展开规模，当前硬上限为：4096 个事件、协议 JSON 8 MiB、128 个目标、每个 prompt+completion 2 MiB、每条轨迹展开总量 32 MiB。超限给出静态拒绝码，不静默截断。canonical 候选仍按导出器规则保留。

## 本地训练验收

训练依赖是可选验收工具，采集/导出功能仍只依赖 Python 标准库。使用独立 Python 3.11 环境；不要改已有项目环境：

```bash
python3.11 -m venv /tmp/nous-training-trainer-env
/tmp/nous-training-trainer-env/bin/python -m pip install \
  --index-url https://pypi.org/simple -r requirements-training-smoke.txt

# 在 colleague 目录中，从手写 fixture 生成已标记 synthetic 的数据集。
python3 hooks/nous_training.py demo --profile codemode-events-v1 \
  --out /tmp/nous-codemode-demo-001
python3 hooks/nous_training.py validate --dataset /tmp/nous-codemode-demo-001
DATASET=/tmp/nous-codemode-demo-001
/tmp/nous-training-trainer-env/bin/python -I scripts/training_trainer_smoke.py \
  --dataset "$DATASET" --out /tmp/nous-trainer-run-001 --steps 2
```

`--out` 必须不存在；报告写为 `report.json`。如果例子已运行过，请换新目录名。输入 manifest 必须显式标记 `synthetic: true`；这个标记用于防止误用真实数据，不是数据真实性证明。依赖安装需连接官方 PyPI；运行训练时强制 Hugging Face 离线、禁用遥测、拒绝 socket 连接，不下载权重，不调用模型 API，不上传数据。

脚本创建字节级 tokenizer 和随机初始化的两层 GPT-2（隐藏维度 64），只在 CPU float32 执行 1–2 个优化步骤。数据由 `datasets.load_dataset("json", ...)` 实际读取，再交给 TRL `SFTTrainer`。使用 `completion_only_loss=True`、关闭 packing、关闭自动长度截断；超过 `--max-tokens`（默认 4096）直接失败。脚本检查：

1. JSONL 与 canonical/lineage 校验通过，tokenizer 能还原全部训练字符串。
2. prompt/completion 分界精确，预处理没有截断或改写 token。
3. prompt（包括工具输出）和 padding 的 labels 全为 `-100`，助手目标有有效 token。
4. loss、每一步梯度有限且梯度非零；实际 step 数正确、模型权重摘要发生变化。

`report.json` 记录实测库版本、数据 manifest 摘要、样本与 token 数和上述检查。它不保存模型权重。训练器可能打印 loss 等数值；它不打印轨迹正文。正常数据集超过 64 行时，此小型验收脚本会拒绝，避免误把它当成批量训练服务。

这项验收只证明加载、mask 和优化链路可运行。随机模型没有效果指标；不能用两步 loss 或权重变化宣称安全能力改善、生产训练通过、真实平台所有轨迹均可训练。实际平台中尚未解释的上下文仍会阻断严格导出。

## 实现与官方依据

`hooks/nous_codemode_training.py` 提供 `samples(record, require_quality=True)`、`sample_for(record, target, require_quality=True)`、`validate_sample(sample)`。错误仅为静态 `CodeModeError`；无用户正文。开发验收脚本应从可信 checkout 用 `python -I` 启动；它不是代码签名校验器，不能安全运行被替换的本地源码或解释器。

- [TRL 0.26.2 SFT 输入与 completion loss](https://huggingface.co/docs/trl/v0.26.2/en/sft_trainer)：标准字符串 prompt/completion，无需伪造工具聊天模板。
- [TRL 0.26.2 SFTConfig 源码](https://raw.githubusercontent.com/huggingface/trl/v0.26.2/trl/trainer/sft_config.py)：默认长度存在截断，因此本脚本显式关闭并自行检查长度。
- [Datasets 本地 JSON 加载](https://huggingface.co/docs/datasets/loading#json)：逐行 JSON 对象可直接作为本地数据源。
- [Transformers GPT-2 配置初始化](https://huggingface.co/docs/transformers/en/model_doc/gpt2)：可从配置创建随机权重模型，不需要下载预训练权重。

具体是否实跑成功，以对应运行的 `report.json` 为准；依赖锁文件不是通过证明。

2026-11-16 已在 macOS arm64 / Python 3.11.14 上用手写 fixture 实跑通过：TRL 0.26.2、Transformers 4.57.6、Torch 2.13.0、Datasets 4.4.1；两条样本、521 个有效目标 token、两步优化、378816 个随机初始化参数。mask、无截断、有限梯度与权重变化检查通过。这条记录不延伸为其他系统、版本或真实平台轨迹的验收。


a6 同时支持 `request-sft-v1` 的标准字符串 prompt/completion 数据。训练脚本依据 manifest 选择对应的严格 validator，其余 synthetic-only、无网络、不截断、completion-only-loss 检查一致。可先运行 `demo --profile request-sft-v1` 生成 24 条可训练的手写样本，再传给上述验收脚本；这不证明模型效果或真实用户数据质量。

## 多轮函数工具数据

a7 支持 `request-episode-sft-v1`，序列化为 `nous.request-tool-sft/1`。通过 `demo --profile request-episode-sft-v1 --out /新的目录` 生成演示，传 `/新的目录/dataset` 给 trainer。10条合成episode筛出12条目标样本；整个请求与工具输出属于prompt，损失只作用于实际助手completion。该随机小模型测试只证明装载、边界和优化流程，不证明模型效果。
