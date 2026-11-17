# 显式多请求工具轨迹采集

`nous_training_episode_capture` 在发送客户端最终请求字节的集成点调用。默认关闭；项目 opt-in 或 `manual=True` 只授权这一条 episode。不会扫描既有历史、修改平台配置、代理真实 API 或自动安装 hooks。

```python
from nous_training_episode_capture import begin_episode, begin_step, finish_step, finish_episode
h = begin_episode("openai-responses", project=project_path, session=session_id,
                  oracle=expected_json_oracle, manual=True)
ticket = begin_step(h, actual_request_bytes)  # 在发送前冻结实际请求
actual_response_bytes = send_exactly(actual_request_bytes)
finish_step(h, ticket, actual_response_bytes, content_type="application/json")
# 每次实际工具结果写入下一次原生请求后，重复 begin_step / finish_step。
report = finish_episode(h)
```

这是观察 API，不执行工具，不替调用方构造上下文；调用方管理网络、SDK异常和业务调用。与 a6 单请求兼容 API 并存。SSE 可用 `content_type="text/event-stream"`。`begin_episode` 只冻结 oracle 模板，第一次 `begin_step` 将它绑定实际请求 SHA256；本地模拟提供商在收到客户端字节后注册时，必须明确 `oracle_registration="before_response"`，不得声称发送前注册。

每条最多64步、单次请求/响应最多8MiB、整条最多16MiB；一条只有一个进行中的 ticket，不能跨 handle、复用或并发推进。验证保存每步原生请求/响应及脱敏后内容摘要，再验证下一请求的完整前缀、上一助手输出、全部工具结果和相同项目/会话。缺步、改写或混入新任务会拒绝整条；不会猜测补齐历史。字段/结构不支持时静态报错，原始正文和oracle期望值不落盘。

原始请求只在内存；脱敏步骤暂存内存，只有 `finish_episode` 原子保存。进程中断前尚未完成的 episode 会丢失；本版不提供崩溃恢复或后台代理。最后仍待工具结果时，可显式结束为不完整候选，不能进入严格训练集。撤回、撤回后重新授权、全局清除、项目路径被替换或数据根变更，均不能让原在途采集重新获得写入权限。

函数工具参数/JSON结果按结构脱敏；隐私变换改变工具内容则质量不完整。命令/脚本/任意执行输入拒绝。文件工具只接受可核实项目内现存普通文件，检查原路径和解析目标的排除名、大小、有限8KiB二进制探测及Git忽略；特殊文件、项目外文件、Git不可验证时拒绝。探测内容不保存。已检查的不可变快照导出时重脱敏，不重新计算内容摘要；未检查标记不会自动升级。仍要求独立内容审核，这些检查不构成任意MCP工具行为证明。

整条精确最终JSON oracle只验证最终结果，不证明每个中间决定正确。严格导出同时需要整条结果证据、内容审核；每个中间助手工具目标还需要单独 `review-step`。步骤负评优先于正评，保留冲突记录，并跨重复样本隔离。工具返回仅在 prompt 中，永远不作为 assistant completion。

```bash
python3 hooks/nous_training.py review-content --id t_... --reviewer reviewer_alias --result cleared
python3 hooks/nous_training.py review-step --id t_... --step 0 --reviewer reviewer_alias --result accepted
python3 hooks/nous_training.py export --profile request-episode-sft-v1 --out /新的数据集目录
python3 hooks/nous_training.py demo --profile request-episode-sft-v1 --out /新的合成验收目录
python3 hooks/nous_training.py validate --dataset /新的合成验收目录/dataset
```

合成演示包含两平台、并行逆序结果、错误/重试、未审步骤、拒绝步骤、错误最终任务和未做内容审核。其10条episode产生12条SFT和4条整条拒绝记录；无模型调用。详见 [数据结构](TRAINING_EPISODE.md) 与 [实机验收](TRAINING_MULTITURN_LIVE.md)。
