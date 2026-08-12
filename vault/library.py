"""Framework store — server-side ONLY.

Frameworks are loaded for the execution engine's system prompt and for the leak filter.
There is deliberately no function that serialises a framework into a tool response:
the old Phase-1 `serve_skill()` (host-and-serve) is gone. Finished output only.
"""
from functools import lru_cache
from pathlib import Path

FRAMEWORKS_DIR = Path(__file__).parent / "frameworks"

FRAMEWORKS = ("copywriter", "icebreaker", "list_building", "partner_signals")


@lru_cache(maxsize=None)
def load_framework(name: str) -> str:
    if name not in FRAMEWORKS:
        raise FileNotFoundError(f"Unknown framework: {name}")
    return (FRAMEWORKS_DIR / f"{name}.md").read_text(encoding="utf-8")
