#!/usr/bin/env python3
"""Independent, bounded-input, local Stop collector; never controls the AI agent."""
import json
import sys

MAX_INPUT_BYTES = 1024 * 1024
MARKER = "nous-training-v1"


def _agent(argv):
    if len(argv) != 4 or argv[0] != "--agent" or argv[2:] != ["--managed-marker", MARKER]:
        raise ValueError()
    if argv[1] not in ("claude", "codex"):
        raise ValueError()
    return argv[1]


def main(argv=None, stream=None, error_stream=None):
    """Success is silent; failure reports a static code and still exits zero."""
    argv = sys.argv[1:] if argv is None else argv
    stream = getattr(sys.stdin, "buffer", sys.stdin) if stream is None else stream
    error_stream = sys.stderr if error_stream is None else error_stream
    try:
        agent = _agent(argv)
        raw = stream.read(MAX_INPUT_BYTES + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not raw or len(raw) > MAX_INPUT_BYTES:
            raise ValueError()
        data = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(data, dict):
            raise ValueError()
    except (ValueError, UnicodeError, OSError, RecursionError):
        error_stream.write("nous-training: invalid_hook_input\n")
        return 0
    if data.get("hook_event_name") != "Stop":
        return 0
    if any(not isinstance(data.get(key), str) or not data[key].strip()
           for key in ("cwd", "transcript_path", "session_id")):
        error_stream.write("nous-training: incomplete_hook_input\n")
        return 0
    try:
        import nous_training
        result = nous_training.capture_stop({"event": "Stop", "agent": agent, "collector": "independent"}, data)
        if isinstance(result, dict) and result.get("status") == "collection_failed":
            error_stream.write("nous-training: collection_failed\n")
    except Exception:
        # Exceptions may embed prompt text, filenames or tokens; never echo them.
        error_stream.write("nous-training: collection_failed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
