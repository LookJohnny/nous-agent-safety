# 训练采集钩子的升级、诊断与卸载

安装配置、项目授权和本地数据是三个独立状态。升级或卸载钩子不会自动删除轨迹、审核、导出、密钥或健康状态，也不会修改 Claude Code / Codex 的平台信任配置。

## 项目停用和平台卸载的区别

只停止一个项目的自动采集，使用：

```sh
python3 hooks/nous_training.py disable --project /绝对路径/项目
```

移除某个平台的独立训练 Stop hook，使用：

```sh
python3 hooks/nous_training.py uninstall --agent claude --project /绝对路径/项目
python3 hooks/nous_training.py uninstall --agent codex --project /绝对路径/项目
```

`--agent both` 可同时处理两种平台。

**`uninstall --project` 中的项目仅用于返回该项目的诊断和授权状态。Stop hook 存在于平台设置中，同一个 `NOUS_HOME` 下的全部已启用项目共用它。卸载某个 agent 的钩子会影响这些项目在该 agent 上的自动采集，不是只卸载所写的一个项目。** 项目级停止应使用 `disable --project`。

卸载成功后，`capture_enabled` 仍可能为 `true`：这表示项目授权保持不变，平台钩子已移除。以后再次安装钩子，原有授权项目可能恢复自动采集。若不希望恢复，另行显式停用相应项目。

独立钩子卸载不会移除旧的 `nous_hook.py` 安全钩子或其他采集入口。`legacy_safety_hook_may_also_collect` 仍出现时，应按旧入口的配置单独检查，不能把本功能的 `uninstalled` 当成关闭电脑上所有采集功能的证明。

## 升级到新的本地运行目录

保留新的运行目录和完整发行文件，然后使用该目录中的 CLI：

```sh
python3 hooks/nous_training.py doctor --agent both --project /绝对路径/项目
python3 hooks/nous_training.py setup --agent both --project /绝对路径/项目
python3 hooks/nous_training.py doctor --agent both --project /绝对路径/项目
```

应沿用原有 `NOUS_HOME` 和 `NOUS_LEDGER_HOME`，以保持数据与脱敏密钥归属。`setup` 会将未修改的托管 handler 原位指向当前 Python 和新目录的 `nous_training_hook.py`，保留所在事件、matcher、组元数据及同组其他钩子的顺序。旧路径已经不存在也可以识别和升级。

CLI 的 `setup` 是项目安装/启用入口，保留原有项目授权流程。单独调用下面的 `nous_training_setup.setup` Python API 只更新平台配置，不启用项目授权。

运行目录缺少依赖时，安装会以 `runtime_files_missing` 失败并保留原配置。不会先写入一个已经知道缺文件的入口。仅文件存在不证明运行环境或平台执行成功；实际发行检查与运行验收见 [TRAINING_RELEASE.md](TRAINING_RELEASE.md)。

## 哪些内容由安装器管理

可替换或移除的 handler 必须完整符合生成格式：

- `type` 为 `command`，只有 `type`、`command`、`timeout` 三个字段，`timeout` 是整数 20。
- 命令符合安装器生成的规范 shell 引用格式，包括 `/usr/bin/env`、绝对 `NOUS_HOME` / `NOUS_LEDGER_HOME`、绝对 Python / hook 路径、agent 和 `nous-training-v1` 标记。
- agent、数据目录和 ledger 目录与这次操作一致。旧 Python / hook 路径允许不同，供迁移识别。

用户修改过超时、添加 `async` 或其他字段、增加命令参数或修改命令写法的 handler 会保留，doctor 报告 `modified_managed_handler_preserved`。不会为同一数据目录再叠加一个无条件 Stop hook。仅含相似文件名或 marker 字样的其他命令也不会被删除。

同一数据目录使用不同 ledger 时会报告存储范围冲突，保留现有入口；使用其实际 ledger 参数明确操作。其他数据目录或其他 agent 的 handler 均保留。

升级不重置用户的事件或 matcher。改到非 Stop 事件、带过滤器或带额外组元数据时，doctor 会报告问题，不能假称已恢复正常自动采集。重复的托管入口也会报告；卸载会删除所有精确匹配的目标入口，但不会删除修改后的版本。

## 配置备份与卸载行为

每次实际修改前，原始配置字节保存到同目录的 `*.bak-nous-training-*` 文件，再用临时文件和原子替换更新设置。新文件和备份权限为 `0600`。其他顶层设置、权限设置、环境设置和用户钩子保持不变。

卸载只移除精确匹配的 handler。空的默认生成组可移除；原本就为空的组以及含自定义 matcher 或元数据的组会保留，即使卸载后其 hooks 为空。不会恢复整个旧备份覆盖后续用户修改。

重复卸载是幂等操作：没有对应入口时不重写配置、不增加备份；平台配置尚不存在时不创建空配置或平台目录。卸载不要求旧 hook、旧 Python 或项目目录仍然存在。

损坏 JSON、重复 JSON 键、非法 hooks 结构或配置符号链接会被拒绝，原文件保留。操作会预检两种平台配置；每个配置文件独立原子更新，不是跨文件事务。写前检测到配置自读取后变化时，以 `config_changed_during_update` 退出。平台编辑器不一定使用相同锁，因此升级期间仍应避免同时编辑该配置。

## 如何阅读 doctor

```sh
python3 hooks/nous_training.py doctor --agent both --project /绝对路径/项目
```

诊断只读取配置、运行文件是否存在和该项目的本地健康状态；不扫描轨迹正文、不启动平台、不访问网络、不授予信任。

- `installed`：目标配置中存在精确的当前入口，所需运行文件存在。
- `configuration_ready`：在上述基础上，平台没有 `disableAllHooks=true`，且目标范围没有重复入口。这仍不是平台信任或实际执行的证明。
- `execution_verified` 始终为 `false`，各平台 `trust` 为 `unknown`。
- `collection_health` 来自本地 `read_health` 的项目范围结果；它是诊断记录，不把平台执行认证为真。读取失败使用静态错误状态，不输出异常正文。
- `capture_enabled` 单独反映所选项目的授权状态。

常见配置诊断：

| 诊断 | 含义 |
| --- | --- |
| `outdated_hook_script_path` | handler 指向其他安装目录，可检查后用新目录执行 setup。 |
| `configured_hook_script_missing` | 配置中指定的脚本已经不存在。 |
| `configured_runtime_incomplete` | 配置目标目录缺少所需模块；缺少文件名单单独返回。 |
| `outdated_python_path` / `configured_python_unavailable` | Python 路径与当前运行环境不同，或原可执行文件不存在/不可执行。 |
| `modified_managed_handler_preserved` | 检测到用户修改，已保留原 handler，不能自动按完整托管入口处理。 |
| `managed_hook_event_mismatch` / `managed_hook_filter_preserved` | 入口位于其他事件或有用户过滤条件；升级保留该行为。 |
| `managed_hook_group_metadata_unverified` | 存在额外组元数据，不能据此声称平台会执行。 |
| `managed_hook_storage_mismatch` | 数据或 ledger 目录与本次诊断目标不同。 |
| `duplicate_managed_hooks` | 同范围存在多个托管或已修改入口。 |
| `platform_hooks_disabled` | 平台禁用了全部 hooks；安装器不会擅自打开。 |

## Python API 与隔离测试

```python
from nous_training_setup import setup, doctor, uninstall

report = uninstall(
    "both",
    "/absolute/project/used-for-diagnostics",
    home="/absolute/nous-home",
    ledger_home="/absolute/nous-home/ledger",
    config_path={
        "claude": "/absolute/isolated/claude/settings.json",
        "codex": "/absolute/isolated/codex/hooks.json",
    },
)
```

`setup`、`doctor` 和 `uninstall` 使用相同的参数布局。`config_path` 仅供明确选择的配置位置或隔离测试使用；`both` 必须提供完整映射，不会在映射缺项时退回真实用户设置。

卸载报告包含 `changed`、`removed_handlers`、`uninstalled`、各配置的 `backup_path`，以及固定为 `false` 的 `authorization_changed`、`data_deleted`、`trust_changed`。`uninstalled=false` 表示目标范围仍留有已修改入口或同数据目录的 ledger 冲突，需要用户检查；它不会自动清除这些内容。

测试均使用临时平台配置和合成数据，覆盖升级原位保留、精确备份、卸载幂等、用户修改与其他入口保护、无配置时无创建、授权/数据/密钥不变、损坏配置拒绝、旧路径和缺依赖诊断，以及健康状态读取失败不泄露正文。

升级前应先停止相关项目采集，并等待已启动的旧版 hook 结束，再切换源码与重新运行 setup；setup 不会终止已经运行的进程。跨版本同时运行的旧采集进程不在新版本锁协议的保证范围内。
