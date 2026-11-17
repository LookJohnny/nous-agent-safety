# Nous Training 0.1.0a9：开发者内测发布基线

当前版本为 **macOS 开发者内测 alpha**。构建脚本只生成本地文件，不上传、创建公开 release、打 Git tag、安装 hooks 或启用真实项目采集。真实CLI对本地脚本提供商的三轮MCP恢复/采集验收已通过，真实模型完整训练数据验收尚未完成；本版本不是 GA，也不是已经验证效果的商业训练数据交付服务。

`TRAINING_VERSION` 管理训练组件版本；它与 JSONL schema 版本、数据集版本、完整 Nous 产品版本分别管理。当前发行包仅包含本地轨迹与显式请求采集、结果验证、脱敏、审阅、严格导出和离线校验所需的标准库 Python 模块及手写合成样例，不包含完整安全同事服务、数据中心或训练模型权重。

新增 `nous.trajectory/4` / `request-episode-sft-v1` 多请求函数工具 episode。完整链验证、独立内容审核和步骤审核后导出标准 prompt/completion JSONL；旧 a6 单请求入口保持兼容。见 [采集接口](TRAINING_EPISODE_CAPTURE.md) 和 [a7 历史验收](TRAINING_STATUS_PHASE7.md)。

`a9` 将上一轮已核验的历史质量修复纳入最小包：上下文凭据脱敏、精确历史原因诊断、受界限约束的 Claude 原生规范化请求解码、锚定文件的 Write 输入/结果恢复，以及结构化 Write 正文中的路径误判修复。新增两个纯内存恢复模块及其说明，源码检查要求四个相关回归模块存在。历史请求解码、文件对应关系及诊断改进不会自动产生完整性或任务成功标签；恢复接口见 [历史修复边界](TRAINING_HISTORY_REPAIR.md)。

本地候选包和本次真实模型验收分别记录。包内 `real_platform_acceptance` 继续保留 `not_completed`；本次具体 CLI 版本、任务、记录摘要及结果由包外验收报告提供，不能据此宣称任意平台/任务已全面通过。归档只包含公开形式的源代码、文档和合成 fixtures，真实历史轨迹及本机验收数据均不进入包内。

本版本还提供显式启用的真实模型受控任务验收：限定本机 MCP 工具、原生请求内存观测桥、独立代码产物检查，见 [TRAINING_REAL_MULTITURN.md](TRAINING_REAL_MULTITURN.md)。运行代码任务的独立检查依赖 macOS `sandbox-exec`；不可用时阻断该验收入口。构建及离线 smoke 只核对 CLI help，不启动真实模型。包内提供带缺陷的公开任务和冻结测试定义，不包含参考解或用户数据。

## 源码检查

在仓库根目录执行：

```bash
python3 -I -S colleague/scripts/training_release_check.py \
  --report /绝对路径/release-check.json
```

检查会创建临时 `NOUS_HOME`、`NOUS_LEDGER_HOME` 及合成项目，运行所有 `test_training*.py`、`test_codemode*.py` 和 transcript 测试、完整运行模块导入、CLI help、隔离配置的 doctor、合成 demo、离线 validate/audit/diagnose 和独立合成来源 inspect-source。模块导入及 smoke 使用 `-I -S -B`，排除启动时的工作目录、脚本目录、用户 site-packages 和 Python 环境注入；只在完成清单核验后显式加入已检查的运行模块目录。不修改操作系统 HOME 或 CODEX_HOME，不查看真实平台配置或会话历史。

`runtime_inventory()` 和发布 manifest 的 `runtime_files` 列出完整的 36 个运行模块，覆盖 schema、validate、demo、setup、Code Mode、分组划分及隐私依赖。doctor 只证明文件/配置状态；`execution_verified=false` 和真实平台验收的 `not_run` 不能被当成已运行真实 hooks。

报告将每项检查区分为 `passed`、`failed`、`not_run` 或 `not_applicable`，记录本机 Python/操作系统及测试数量、跳过数量。一次本机成功不代表整个 CI 矩阵已经成功。

## 构建两个可复现归档

```bash
python3 -I -S colleague/scripts/training_build.py \
  --out /绝对路径/全新的构建输出目录
```

输出目录必须不存在。默认会生成并解压测试：

- `nous-training-0.1.0a9.tar.gz`
- `nous-training-0.1.0a9.zip`
- `release-manifest.json`：版本、发布阶段、显式文件白名单、逐文件大小/SHA-256、运行依赖列表。
- `SHA256SUMS`：两个归档与发布 manifest 的校验和。
- `BUILD_REPORT.json`：此次解压 smoke 的实际状态；不会把未执行的源码测试、CI 或真实平台验收写成通过。

仅白名单内的源码、说明和合成 fixtures 可进入归档；不递归打包仓库，不收集 `.env`、真实轨迹、模型权重、缓存、虚拟环境或 Git 数据。源文件是符号链接、缺失或超过上限时构建失败。输出归档无需原仓库即可执行训练 CLI。

同样的源文件和 `--source-date-epoch` 在同一 Python/压缩器环境中生成相同字节；归档内部排序、权限、UID/GID 和时间固定，ZIP 使用无压缩存储减少跨压缩器差异。默认 epoch 为 0，可通过显式参数固定其他值。`BUILD_REPORT.json` 包含实际环境报告，不属于字节可复现的归档输入。SHA-256 用于完整性核对，不构成发布者身份签名。

`--no-smoke` 仅用于明确的构建调试；报告将记录 `smoke: not_run`，不能作为通过发布门槛的证据。构建命令本身不代替前面的完整源码检查。

## 解压后检查与使用

解压后进入 `nous-training-0.1.0a9/`：

```bash
python3 -I -S scripts/training_release_check.py --smoke-only
python3 hooks/nous_training.py --help
python3 hooks/nous_training.py demo --out /绝对路径/新的合成数据目录
python3 hooks/nous_training.py validate --dataset /绝对路径/新的合成数据目录
```

最小包不包含完整仓库的测试和数据计划模块；因此解压检查必须明确使用 `--smoke-only`，源码测试会标为 `not_run`。该模式强制要求 `release-manifest.json`，缺失不能降级为源码模式；运行任何包内功能前，会检查完整文件树并拒绝所有未列出的文件、目录和链接，包括 `hooks/hashlib.py`、`scripts/json.py`、根目录影子模块及 `__pycache__`。不要在发行目录内保存数据或额外代码。运行模块 import、发布文件校验和、CLI、doctor、demo、validate、audit 与新增诊断仍必须通过。真实项目的授权、setup、import、review、export 见 [TRAINING.md](TRAINING.md)。其中提及的完整 Nous 同意/撤回入口只在完整仓库中提供；最小包使用训练 CLI 的 `disable --project ... --purge` 管理本地缓存。

两个发布脚本的命令行入口要求 `-I`；未启用时在导入可被影子文件替换的标准库之前退出。推荐同时使用 `-S`，避免系统 site 初始化。源码检查模式只适用于你信任的完整开发仓库，会执行该仓库的测试。隔离启动与清单校验不能认证一个已经被替换的 checker、builder、manifest 或 Python 安装，也不能抵御核验后另一个进程篡改文件。应先从可信渠道取得发行包和预期归档哈希；当前清单没有签名，不能把内部一致性当作发布者身份认证。

核心采集、导出和划分没有第三方 **Python 包** 依赖，manifest 用 `external_python_dependencies: []` 表示这一点。Git 是单独的运行能力，登记在 `optional_runtime_capabilities`：文件工具内容的权限/排除核验需要 Git 以及可核验的仓库状态。缺少 Git、仓库状态不可核验或核验失败时，文件工具内容应被隔离并标记质量缺口，不能认为文件排除能力完整；这可能使相关样本不能进入严格训练集。doctor 会报告 `git_available`，它不等于已经核验某个文件。Python 3.11/3.12/3.13/3.14 是 CI 目标，不承诺 Windows；`fcntl` 等运行依赖决定当前是 Unix 平台实现。Linux CI 用于标准库与数据转换兼容检查，不等于 Linux 桌面 Agent 集成已经验收。

## CI 与尚待验收项

`0.1.0a9` 另带 Code Mode 合成示例、分组划分和可选训练验收脚本。核心采集/导出/划分仍只用标准库；`scripts/training_trainer_smoke.py` 的实际训练需要单独安装 `requirements-training-smoke.txt`，说明见 [TRAINING_TRAINER.md](TRAINING_TRAINER.md)。离线发布检查只核对该脚本的 CLI 与核心合成数据，不安装依赖或自动训练；实际训练报告单独保存。

`.github/workflows/training.yml` 配置 GitHub Actions 的 Linux/macOS × Python 3.11/3.12/3.13/3.14 矩阵，运行源码检查、构建及解压 smoke，保留各矩阵的测试报告与本地构建产物。它不创建公开 release，也不自动运行真实模型或连接用户账户。工作流只有在提交并被 GitHub 执行后才产生 CI 验证证据；本地创建 YAML 不代表矩阵已通过。

工作流用法依据 [checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python) 和 [upload-artifact](https://github.com/actions/upload-artifact) 的官方说明。


升级为公开稳定版前仍需完成真实平台 hooks/工具上下文验收、目标训练栈加载与训练验收、数据量边界测试、发布物来源/权限确认，以及用户范围内的回滚验证。商业数据交付还需要数据集划分与版本、来源授权、服务端交付和撤回追踪。离线合成 smoke 不替代这些结果。
