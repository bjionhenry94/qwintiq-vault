"""Curtain guard — no tool error may ever leak the vault's internals to a consultant.
Deterministic unit tests on the _safe wrapper (no server / network needed)."""
import sys

sys.path.insert(0, "..")
import os
os.environ.setdefault("VAULT_AIARK", "mock")

from vault.aiark import DataUnavailable  # noqa: E402
from vault.tools import _safe, _SAFE_ERROR, _DATA_ERROR  # noqa: E402

results = []


def check(name, ok):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name)


# The actual leak the consultant saw: a raw upstream error naming the provider + endpoint.
LEAKY = "Client error '401 Unauthorized' for url 'https://api.ai-ark.com/api/developer-portal/v1/companies'"


def _raise_data():
    raise DataUnavailable


def _raise_leaky():
    raise RuntimeError(LEAKY)


def _raise_llm():
    raise RuntimeError("anthropic.AuthenticationError: invalid x-api-key for claude-sonnet-5")


def _ok():
    return "real output"


forbidden = ["ai-ark", "ai_ark", "api.ai-ark", "http", "401", "unauthorized",
             "developer-portal", "anthropic", "x-api-key", "claude-", "traceback"]

r_data = _safe(_raise_data)()
check("DataUnavailable -> data message", r_data == _DATA_ERROR)

r_leak = _safe(_raise_leaky)()
check("AI-Ark 401 error -> generic safe message", r_leak == _SAFE_ERROR)
check("provider/URL never in response", not any(x in r_leak.lower() for x in forbidden))

r_llm = _safe(_raise_llm)()
check("LLM/provider error -> generic safe message", r_llm == _SAFE_ERROR)
check("model/provider never in response", not any(x in r_llm.lower() for x in forbidden))

check("happy path passes through untouched", _safe(_ok)() == "real output")
check("safe messages themselves reveal nothing",
      not any(x in (_SAFE_ERROR + _DATA_ERROR).lower() for x in forbidden))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
