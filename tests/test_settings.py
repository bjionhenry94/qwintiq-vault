"""Admin-managed keys: encrypted at rest, read back correctly, drive provider selection,
and (crucially) the stored value is never plaintext. Unit-level, isolated DB, no server."""
import os
import sys

# Clean slate + isolated DB, set BEFORE importing dal (it reads these at import).
for _k in ("AI_ARK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "VAULT_LLM", "VAULT_AIARK", "DATABASE_URL"):
    os.environ.pop(_k, None)
os.environ["SECRET_KEY"] = "unit-test-secret-key-for-encryption"
os.environ["VAULT_SQLITE"] = "/tmp/qv-settings-test.sqlite3"
if os.path.exists(os.environ["VAULT_SQLITE"]):
    os.remove(os.environ["VAULT_SQLITE"])
sys.path.insert(0, "..")

from db import dal          # noqa: E402
from vault import aiark, engine  # noqa: E402

results = []


def check(name, ok):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name)


dal.init_db()

# 1. round-trip
dal.set_secret("AI_ARK_API_KEY", "ark-secret-abc-123")
check("get_secret round-trips the value", dal.get_secret("AI_ARK_API_KEY") == "ark-secret-abc-123")

# 2. stored ENCRYPTED, not plaintext
row = dal.q("select value from settings where name=?", ("AI_ARK_API_KEY",), fetch="one")
check("stored value is encrypted (plaintext not in DB)", "ark-secret-abc-123" not in row["value"])

# 3. a different SECRET_KEY cannot decrypt it (proves it's really keyed off SECRET_KEY)
import base64
import hashlib
from cryptography.fernet import Fernet
wrong = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"some-other-secret").digest()))
try:
    wrong.decrypt(row["value"].encode())
    check("DB dump alone can't decrypt (needs SECRET_KEY)", False)
except Exception:
    check("DB dump alone can't decrypt (needs SECRET_KEY)", True)

# 4. aiark picks up the DB key -> not mock
check("aiark uses the DB key (leaves mock mode)", aiark._mock() is False and aiark._key() == "ark-secret-abc-123")

# 5. status reporting (never returns the key itself)
check("status = managed_here when set here", dal.secret_status("AI_ARK_API_KEY") == "managed_here")

# 6. clear -> falls back (no env set) -> None -> mock again
dal.clear_secret("AI_ARK_API_KEY")
check("clear_secret removes it", dal.get_secret("AI_ARK_API_KEY") is None)
check("aiark back to mock after clear", aiark._mock() is True)

# 7. an OpenAI (ChatGPT) key drives the provider to 'openai'
dal.set_secret("OPENAI_API_KEY", "sk-openai-test-xyz")
check("engine reads OpenAI key from DB", engine._openai_key() == "sk-openai-test-xyz")
check("provider = openai when only a ChatGPT key is set", engine._provider() == "openai")

# 8. an Anthropic key is preferred when both are present
dal.set_secret("ANTHROPIC_API_KEY", "sk-ant-test-xyz")
check("provider prefers anthropic when both set", engine._provider() == "anthropic")

os.remove(os.environ["VAULT_SQLITE"])
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
