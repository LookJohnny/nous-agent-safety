# Nous Agent Safety

**Agent safety through emotion-related monitoring, protective controls, and readable risk signals.**

English · [简体中文](README.zh-CN.md)

Nous is an Agent safety project focused on helping people **observe AI behavior, recognize risk, and retain control over AI-assisted work**. Its core functions are emotion and behavioral-risk monitoring, safety controls for supported interactions, and human-readable feedback about what the system has observed.

Trajectory collection supports this work through audit records and reviewed training-data export. It is an optional supporting component of the project.

## Core focus

The broader project contains development prototypes for three complementary functions.

### Monitor emotion-related and behavioral signals

Observe the tone and emotional expression of AI responses, and surface signals associated with hostility, demeaning language, excessive agreement, or possible fabrication. These readouts help users identify interactions that deserve closer inspection.

For supported models with accessible internal states, the project includes direct model-state monitoring prototypes. For closed-model APIs, a reader model can assess the output that is available to it. These two modes have different visibility and timing: reading a closed model's reply does not reveal that model's hidden state or provide a warning before the reply exists.

Here, “emotion” refers to estimated expression and model-state signals. A readout does not establish that an AI has subjective feelings. Fabrication and excessive-agreement indicators remain experimental.

### Apply safety controls

The safety prototypes include agent hooks and policy checks for recognized secret exposure, sensitive file access, selected high-impact tool actions, and signs of prompt injection in external content. Depending on the supported integration and active policy, an event can be recorded, sensitive values masked, an operation blocked, or human approval requested.

These controls operate at the integration points they cover. Their behavior depends on configuration, available dependencies, and the host agent's permission system; they are not a guarantee that every unsafe action will be detected or prevented.

### Make AI behavior easier to understand

Risk labels, relevant response segments, action records, and HUD or console views make the available observations easier to inspect. The aim is to help a person understand **what was flagged, what action was taken, and what evidence is available**.

Readable signals and explanations of safety decisions support oversight. They do not amount to a complete causal explanation of a model's reasoning.

## Components and availability

The project's product scope is broader than the code currently published here.

| Component | Role in Nous | Current availability |
| --- | --- | --- |
| Emotion and risk monitoring | Model-state or reader-based signals for supported models and interactions | Research and development prototypes; not included in this public snapshot |
| Agent protection | Safety hooks, policy decisions, sensitive-data handling, and human approval paths | Development prototypes; full runtime and dependencies not included here |
| Readable interfaces | Risk annotations, HUD views, and monitoring consoles | Development prototypes; not included here |
| Trajectory and training-data tools | Optional collection, review, audit, and JSONL export | Included under [`colleague/`](colleague/); component version **`0.1.0a9`**, developer alpha |

**The current checkout provides the trajectory component.** It does not install the full monitoring engine, protection runtime, or HUD. Private research, patent materials, model weights, and real user datasets remain outside this public distribution. The trajectory component's version and test results do not describe a stable release of the complete safety product.

## Supporting feature: trajectories and training JSONL

The published tools support explicit project opt-in, supported Claude Code and Codex session imports, privacy filtering, content-bound review evidence, and profile-specific JSONL export. Historical import and collection are separate operations; collection is off by default.

Records first become candidates. Only eligible samples enter training files, and rejected samples receive diagnostic reasons. Exported datasets include source links, file hashes, and validation metadata. These checks support inspection and consistency; they do not establish factual correctness or permission to reuse the content.

See the [trajectory guide](docs/TRAJECTORIES.md) for requirements, integration steps, export profiles, validation, and grouped dataset splits.

### Try the published component

This example exercises the **auxiliary trajectory pipeline**, using synthetic data. Core commands use the Python standard library on macOS/Linux with Python 3.11 or later; the configured CI matrix covers 3.11–3.14. Git is needed for cloning and repository file-exclusion checks. Additional capabilities and their requirements are described in the [guide](docs/TRAJECTORIES.md#requirements).

```bash
git clone https://github.com/LookJohnny/nous-agent-safety.git
cd nous-agent-safety/colleague

nous_demo_root="$(mktemp -d)"
python3 -B hooks/nous_training.py demo --out "$nous_demo_root/dataset"
python3 -B hooks/nous_training.py validate --dataset "$nous_demo_root/dataset"
python3 -B hooks/nous_training.py audit --dataset "$nous_demo_root/dataset" --require-training-rows
```

The demo does not read real sessions, install safety or collection hooks, or call a model provider. Its generated records are marked `synthetic: true`.

## Development and validation

Nous remains under development. Monitoring readouts depend on the model, available context, and operating conditions. Alerts can be wrong or miss a risk; deployment decisions require evaluation in the intended setting.

The public [GitHub Actions workflow](https://github.com/LookJohnny/nous-agent-safety/actions/workflows/training.yml) checks the **trajectory component** across Ubuntu/macOS and Python 3.11–3.14. It runs offline tests and package checks. It does not measure emotion-monitoring accuracy or validate the complete Agent safety product.

Real-provider multi-turn acceptance for the published trajectory component is also incomplete. Its [acceptance record](docs/STATUS.md) distinguishes offline checks, controlled attempts, and outstanding work. [Build and verification instructions](docs/TRAJECTORIES.md#verification) are available separately.

## Repository history and licensing

This repository was previously **SoulForge**, a portable AI identity and companion-runtime project. The earlier implementation remains in the [legacy branch](https://github.com/LookJohnny/nous-agent-safety/tree/legacy/soulforge-20261118) and at the [pre-migration commit](https://github.com/LookJohnny/nous-agent-safety/tree/614645cf72277b39196441d91b9fe5e2931f9324). Existing stars reflect the repository's full history. See the [migration record](docs/MIGRATION.md).

The historical SoulForge SDK and specifications retain their original MIT notices. Publication of the current Nous source does not grant a new open-source license. Existing per-file and third-party notices remain applicable.
