# Synthetic training fixtures

These files are handwritten **synthetic test data**, not captured user sessions or production training data. Agent versions, models, message IDs, tool calls and outputs are invented. They exercise supported transcript shapes; they do not certify compatibility with every Claude Code or Codex version.

Both transcripts contain two turns. The first looks up `alpha` and returns `2`. The second first requests an absent key, records an explicit tool failure, then recovers by looking up `beta` and returning `3`. `tools.json` defines the exact fictional `fixture_lookup` tool used here. No tool is executed or installed.

Run this from `colleague/` to try an isolated import, review and export. The temporary directories keep the demo separate from real Nous records; no hooks or continuous capture are enabled.

```bash
(
set -eu
export NOUS_HOME="$(mktemp -d "${TMPDIR:-/tmp}/nous-synthetic-demo.XXXXXX")"
export NOUS_LEDGER_HOME="$NOUS_HOME/ledger"
mkdir "$NOUS_HOME/project"
for fixture_agent in claude codex; do
  fixture_record_id="$(python3 hooks/nous_training.py import \
    --transcript "examples/training/$fixture_agent.jsonl" \
    --project "$NOUS_HOME/project" --agent "$fixture_agent" \
    --session "synthetic-$fixture_agent" --context examples/training/tools.json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
  # This accepts only the known handwritten fixture behavior described above.
  python3 hooks/nous_training.py review \
    --id "$fixture_record_id" --reviewer synthetic-demo-reviewer --result accepted
done
python3 hooks/nous_training.py export --out "$NOUS_HOME/export" --profile trl
printf 'Synthetic fixture export: %s\n' "$NOUS_HOME/export"
)
```

The two providers intentionally describe equivalent behavior. Training output may deduplicate equivalent examples. Do not mix these demo records into a production dataset or treat this manual fixture acceptance as validation of real user outcomes.
