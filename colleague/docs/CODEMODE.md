# Codex Code Mode：保留原生事件的训练格式

`0.1.0a2` 新增 `codemode-events-v1`。它将已采集、已脱敏并通过审核的可见事件转成字符串 `prompt/completion` JSONL，供支持这种输入的训练程序使用。它不是 OpenAI 微调上传格式，不会把 JavaScript 中的嵌套调用伪装成普通 function call，也不会执行采集的代码。

## 先运行合成示例

在 `colleague/` 或最小发行包根目录运行：

```bash
python3 hooks/nous_training.py demo --profile codemode-events-v1 --out /绝对路径/新的示例目录
python3 hooks/nous_training.py validate --dataset /绝对路径/新的示例目录
```

手写例子包含真实形状的外层 exec grammar、明确声明的 toy lookup schema、JavaScript 输入、运行时工具审计和分块工具结果。预期导出 1 条 canonical、2 条 SFT、0 条 DPO。数据和证据均明确标记为合成 fixture；这不是一次真实模型调用或模型效果评估。

`sft.jsonl` 每行只有 `prompt`、`completion` 两个训练字段。字符串内部使用规范 JSON 行：第一行声明 `nous.observed-visible-events/1`，后面是有序可见事件。内容里的换行和引号由 JSON 编码，不充当伪造边界。

- prompt 仅包含该目标之前的可见事件，包括当时已经追加的工具定义、配置和工具返回。
- completion 只包含连续的助手输出段，例如可见 commentary 与 custom call，或最终回答。工具输出、用户消息、环境状态不会成为助手训练目标。
- custom 输入保持字符串；function 输入保留其原生 JSON 参数形式；调用 ID、错误标志和分块输出保持对应关系。
- runtime MCP 审计用于来源与隐私检查，标记 `model_visible:false`，不冒充模型发出的调用或额外 prompt。
- 每个样本的 lineage 指向原记录及精确事件区间。校验器从 canonical 重新生成样本，核对其摘要及审核证据。

这里的保真指允许保存的观测内容及其顺序，不代表恢复完整的服务端模型输入。隐藏 reasoning/analysis 不采集。缺失、压缩、未知更新或未完成的轨迹继续隔离。

## 导入与审核

含 Code Mode 事件的 Codex session 自动使用 `nous.trajectory/2`，同时保留兼容的消息观察视图。完整原生事件放入 `protocol`，而不是未绑定的附加字段。摘要同时覆盖事件、工具契约、消息、配置、来源分组身份和完整性声明；任何一项变化都会使旧审核失效。采集时间本身不改变内容摘要。

```bash
python3 hooks/nous_training.py import --agent codex --project /绝对路径/项目 --transcript /绝对路径/本次会话.jsonl
python3 hooks/nous_training.py list --project /绝对路径/项目

# 检查 list 返回的本地脱敏记录后，分别审核内容和任务结果。
python3 hooks/nous_training.py review-content --id t_替换为记录编号 --reviewer 你的审核代号 --result cleared
python3 hooks/nous_training.py review --id t_替换为记录编号 --reviewer 你的审核代号 --result accepted

python3 hooks/nous_training.py export --profile codemode-events-v1 --out /绝对路径/新的导出目录
```

JavaScript 字符串本身不能证明没有访问私有文件，因此任务成功与内容审核是两个独立门槛。可观察子调用触及应排除文件、或 Git 忽略检查无法核实时，相关外层调用及审计副本会整体省略。内容审核也不能解除这些完整性缺口。审核不等于授权上传或商业销售。

工具定义必须在调用之前从有序来源事件中出现。缺失外层 exec 契约时，不能拿事后的 MCP `tools/list` 代替。当前只接受已支持的文本 custom 契约、明确识别的 exec grammar 和受支持的 function schema；未知 grammar、工具名称歧义或参数约束不支持均拒绝训练。旧格式在消息快照改变后也不会继续回填旧工具定义。

本 profile 暂不支持 DPO；传入偏好会明确拒绝，不会自动配对。`trl` / `openai` 原有格式继续处理 `trajectory/1`，不会将新的原生事件重新解释成普通工具对话。

## 真实客户端仍有的缺口

已检查的 Codex `0.169.0-alpha.11.1` 会话中，可以恢复 base instructions、原位追加的工具目录、配置、真实 JavaScript、工具输出块和运行时 MCP 审计。`0.1.0a3` 的 parser `codex-events-1.1` 仅对精确已观察版本识别合法递增的存储 ordinal，仍兼容旧 parser 的导出记录。非空采样 metadata、`world_state`、`turn_context` 和线程设置继续标记语义缺口，不会被当作可安全丢弃的无关字段。因此真实样本仍是 canonical 候选；版本依据与 hook 信任边界见 [TRAINING_CODEX_RUNTIME.md](TRAINING_CODEX_RUNTIME.md)。

工具增量和配置更新保留原位置的依据见 [OpenAI 上下文更新文档](https://developers.openai.com/api/docs/guides/preserved-reasoning#how-to-update-integrations-to-avoid-prefix-changes)。当前实现不会使用未来定义补早期调用，也不会用运行时状态的哈希引用猜测缺失正文。

实际训练加载与最小优化验证见 [TRAINING_TRAINER.md](TRAINING_TRAINER.md)。生产训练前还应使用 [TRAINING_SPLIT.md](TRAINING_SPLIT.md) 按来源项目或会话分组，避免同一任务的多个目标或快照同时进入训练和验证集。
