# Nous Agent Safety

Local-first AI safety and trustworthy training trajectories.

Nous helps people inspect AI tool activity, protect sensitive data, and turn reviewed Claude Code / Codex interactions into traceable training JSONL.

> Formerly **SoulForge**. This repository now develops Nous, focused on AI safety, user control, and auditable interaction data. Existing stars reflect the repository's full history. The [preserved SoulForge branch](https://github.com/LookJohnny/nous-agent-safety/tree/legacy/soulforge-20261118) retains the earlier implementation and license notices. See the [migration record](docs/MIGRATION.md).

## Current public component

The initial public version contains the independently runnable local trajectory and data-quality toolkit:

- Explicit project opt-in and conservative Claude Code / Codex history import.
- Privacy filtering, quality diagnostics, and separate content and tool-step review gates.
- Structured request and tool episodes, strict JSONL export and validation, and grouped train/evaluation splits.
- Handwritten synthetic examples, offline tests, and reproducible package builds.

The full Lens reader engine, private research and patent materials, model weights, and hosted data service remain outside this public source distribution.

## Status

Developer alpha **`0.1.0a9`**. Offline source tests and package smoke checks have passed locally. The [GitHub Actions runs](https://github.com/LookJohnny/nous-agent-safety/actions/workflows/training.yml) report validation for each published commit across Linux/macOS and Python 3.11–3.14.

Real-provider multi-turn acceptance remains incomplete. In the latest controlled attempts on **2026-11-16**, Claude returned a permission error, Codex produced no upstream model response, and neither task produced a strict SFT sample. See the [acceptance record](docs/STATUS.md). Unsupported hidden or encrypted reasoning remains rejected.

Collection is **off by default**. Local import, review, and export do not authorize automatic upload or commercial reuse of a conversation.

## Try the synthetic example

Use Python 3.11 or later. Python 3.14 adds standard-library zstd history support. Git is needed for repository file-exclusion checks. The controlled artifact verifier additionally requires macOS `sandbox-exec`; its absence blocks that verifier.

```bash
git clone https://github.com/LookJohnny/nous-agent-safety.git
cd nous-agent-safety/colleague
python3 hooks/nous_training.py demo --out /tmp/nous-demo-new
python3 hooks/nous_training.py validate --dataset /tmp/nous-demo-new
```

Choose an output directory that does not already exist. The demo uses handwritten synthetic fixtures, not private user conversations.

See the [training guide](colleague/docs/TRAINING.md), [quality review](colleague/docs/TRAINING_AUDIT.md), [current acceptance status](docs/STATUS.md), and [release guide](colleague/docs/TRAINING_RELEASE.md).

## Repository history and licensing

The SoulForge SDK and specifications retain their original MIT notices in the preserved historical version. Publication of the Nous product source does not grant a new open-source license. Existing per-file and third-party notices remain applicable.
