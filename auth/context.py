"""Per-request consultant identity, set by the auth gate, read by tools."""
from contextvars import ContextVar

_consultant: ContextVar[dict | None] = ContextVar("qv_consultant", default=None)


def set_consultant(c: dict | None) -> None:
    _consultant.set(c)


def current_consultant() -> dict | None:
    return _consultant.get()
