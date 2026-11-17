# 本地采集健康状态

`nous_training_health.py` 记录已授权项目的 Stop 采集是否被尝试、是否存下本地记录，以及最近的静态失败原因。它不读取平台历史目录，不联网，也不自动重试或补齐轨迹。

运行以下命令只读取现有健康状态，不启用采集、不创建健康目录：

```sh
python3 hooks/nous_training.py health
python3 hooks/nous_training.py health --project /absolute/path/to/project
```

`doctor` 报告也可显示项目的健康快照。安装配置存在、最近采集成功和数据可用于训练是三个独立条件；健康快照本身不能证明当前平台会执行 Hook。

## 字段含义

状态按脱敏后的 `project_id` 和 `claude`、`codex`、`unknown` 平台分开记录：

| 字段 | 含义 |
| --- | --- |
| `attempt_count` / `last_attempt_at` | 已授权 Stop 采集尝试数及最近尝试时间 |
| `captured_count` / `last_success_at` | 本地采集返回 `collected` 的次数及最近时间 |
| `failed_count` / `last_failure_at` | 采集失败次数及最近时间；不表示 AI 任务失败 |
| `last_record_id` | 最近一次成功采集的 `t_…` 本地记录标识 |
| `last_error_code` | 最近记录的失败或放弃原因，只允许固定代码 |
| `last_status` | 最近时间点的 `attempted`、`collected` 或 `failed` |
| `pending_count` | 开始但尚未收到结束回报的尝试数；不能据此判断进程仍存活 |
| `abandoned_count` / `last_abandoned_at` | 待结束记录达到容量上限时被移除的旧尝试数及时间 |
| `counters_saturated` | 任一计数已达到上限，后续保持上限 |

时间为 UTC Unix 秒。`last_error_code` 会在后续成功后保留，须结合 `last_status` 和相应时间理解。成功采集可以得到存在上下文缺口的候选记录：这里不统计审核通过率，不代表严格 SFT 导出合格，也不会生成正向训练证据。

同一会话的多次 Stop 可更新同一条 canonical 记录；`captured_count` 是采集完成次数，并非去重后的轨迹数量。一次 Stop 内部的有界重读仍计作一次尝试。手工 `import` 不会借此启用持续采集。

## 边界与存储

数据位于 `NOUS_HOME/training/health/state.json`。新建目录使用 `0700`，状态与锁文件使用 `0600`。公开快照不含内部随机完成令牌；持久状态也不包含原始正文、文件路径或平台原始会话 ID。项目和记录 ID 必须符合已有脱敏标识格式，任意异常字符串会降为 `collection_failed`，不会写入磁盘。

容量固定为最多 128 个项目、每项目每平台最多 8 个待结束尝试，状态文件最多 1 MiB，每个计数最多 `2**31 - 1`。新项目超过上限时移除最近活动时间最早的项目，并增加 `evicted_projects`；这些计数因此是有界诊断快照，不是永久审计账本。待结束尝试超限只增加 `abandoned_count` 并记录 `collection_abandoned`，不增加 `failed_count`。

写入通过共享文件锁、完整 JSON 校验、同目录临时文件、`fsync` 和原子替换完成。锁等待最多 0.5 秒，超时返回 `health_busy`。多个正常进程的成功更新不会互相覆盖。读取有界原子快照，不取得写锁，不创建路径。符号链接、非普通状态文件和不符要求的私有目录/文件权限会被拒绝；模块不会静默扩大或修复已有路径权限。它不是对恶意同权限进程篡改目录的防护边界。

损坏或超大状态返回 `corrupt` 和静态诊断码，不回显文件内容，也不静默清空后继续计数。`purge_health(home)` 可在现有安全文件路径下移除损坏状态；按项目清理需要状态仍能解析。清理保留锁文件，以维持并发进程使用同一把锁。清理失败返回静态错误，不能阻止主流程删除训练记录。项目 `disable --purge` 会在尝试其他清理后以静态错误报告未完成；全局撤回返回清理报告，数据计划继续执行后续撤回动作后再报告失败。

## 集成契约

```python
record_attempt(home, project_id, agent, *, authorized=False, now=None)
record_result(home, project_id, agent, attempt_id, *, authorized=False,
              status, record_id=None, error_code=None, now=None)
read_health(home, project_id=None)
purge_health(home, project_id=None)
```

`record_attempt` 成功返回 `status="recorded"`、随机 `attempt_id` 和安全 `state`。`record_result` 的 `status` 参数只能是 `collected` 或 `failed`；成功结果必须携带格式正确的本地 `record_id`。完成令牌只能消费一次，已清理或已移除的令牌返回 `missing` 或 `ignored`，不会重建状态。写入失败返回 `unavailable` 及静态 `error`。

`authorized` 必须显式为布尔 `True`；其他值在检查路径前直接返回 `disabled`。这个参数是调用方责任的声明，不是模块自行验证授权。collector 必须在自己的授权/撤回锁中检查项目持续采集权限与撤回版本，再调用 attempt/result。撤回并清理后，旧尝试不得继续回报结果。`purge_health` 由外层明确的撤回或删除操作授权；不要用 health 模块替代该授权控制。

允许持久化的采集失败码为：

```text
collection_failed
invalid_hook_input
incomplete_hook_input
transcript_missing
transcript_unreadable
transcript_too_large
no_supported_messages
record_too_large
privacy_failed
invalid_transcript
stop_reply_not_flushed
partial_tail
collection_cancelled
transcript_changed_during_read
transcript_must_be_regular_file
transcript_line_too_large
transcript_too_many_lines
transcript_session_mismatch
project_changed_during_collection
project_unavailable
```

上述 allowlist 表示模块可接收的代码，不保证每个 collector 分支都单独映射这些原因。未知代码统一为 `collection_failed`。状态存储自己的错误使用 `health_*` 静态代码返回，不保存异常详情。

恢复采集需要用户明确提供原 transcript，走 collector 的 `recover` 流程与现有授权检查。健康模块不扫描磁盘寻找会话，不自动启动后台恢复。撤回停止持续采集；删除训练缓存时同时清理对应健康信息。已手动导出的目录仍需另行处理。

## 验证范围

`tests/test_training_health.py` 使用临时目录和合成 ID，覆盖独立进程与线程并发、重复完成、清理后迟到回报、原子替换失败、锁超时、损坏/超大 JSON、权限、符号链接、FIFO，以及任意错误文本不落盘。测试没有读取真实用户轨迹，也不证明任何平台生产环境中的 Hook 触发可靠性。
