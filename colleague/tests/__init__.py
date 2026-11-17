"""Every colleague test runs against a throwaway home, set before any test module imports a hook module.

Several hook modules (nous_spool, nous_consent, nous_undo, nous_facts, ...) read their data folder once, at import. A
test that restores the environment and then reaches one of them in-process used to write into the person's real
~/.nous-colleague (2026-11-05: test sessions landed in the real spool). With the defaults below, a "restored"
environment still points here."""
import os, tempfile

SANDBOX = os.environ.get("NOUS_TEST_SANDBOX")
if not SANDBOX:          # the first import in a test run; worker processes inherit it and keep their parent's settings
    SANDBOX = os.environ["NOUS_TEST_SANDBOX"] = tempfile.mkdtemp(prefix="nous-tests-")
    os.environ["NOUS_HOME"] = SANDBOX
    os.environ["NOUS_LEDGER_HOME"] = os.path.join(SANDBOX, "ledger")
    os.environ["NOUS_MANAGED"] = os.path.join(SANDBOX, "managed.json")
    os.environ["NOUS_LOCAL_API"] = "http://127.0.0.1:9"
    os.environ["NOUS_VAULT_SERVICE"] = "center.nous.vault.test"
    os.environ["NOUS_VAULT_INDEX"] = os.path.join(SANDBOX, "vault.json")
    os.environ["NOUS_NO_CLIPBOARD"] = "1"
    os.environ["NOUS_LOCK_URL"] = "http://127.0.0.1:9/lock"     # never flash frames on the screen of whoever runs the tests
    os.environ["NOUS_LOCK_LAUNCH"] = "0"                          # ...nor start the real lock-on app
    os.environ.pop("NOUS_DATA_URL", None)
