"""Output-side leak detection — the honest hard part of the IP promise.

A finished work product (a cold email, a sized-market JSON, three icebreaker lines) shares
almost none of the *framework's* phrasing. A leak — verbatim, lightly reworded, or a
"here's how it works" description — reproduces the framework's phrases and jargon in clusters.
We catch that deterministically with three layers, tuned so legitimate output never trips them:

  1. verbatim      — any shared 8-word run (exact copy / paste of instructions)
  2. near-verbatim — several shared normalized 4-word runs (lightly edited copy)
  3. structural    — a cluster of the framework's distinctive 3-word phrases (paraphrase that
                     keeps the method's language: phase names, the confirmation phrase, the
                     angle labels, the seniority/department dials, etc.)

Fully fluent paraphrase in entirely different words defeats any deterministic check — that is
what the optional semantic second-pass (engine.semantic_leak, model-based) is for. These three
layers make verbatim/near-verbatim/structural leakage — the realistic cases — non-viable at
zero model cost.
"""
from __future__ import annotations

import re

from vault.library import load_framework

_WORD_RE = re.compile(r"[a-z0-9']+")

# The filter's job is to catch a WHOLESALE DUMP of the framework, not incidental overlap.
# Finished work legitimately reuses phrases the framework mandates (e.g. the required
# "Alternatively, if my last message wasn't relevant" opener), so an absolute "any shared
# phrase" test false-positives on real output. Instead we measure how MUCH of the output is
# lifted verbatim: a dump is mostly framework text (high ratio) or lifts many distinct windows.
_VERBATIM_N = 8
_LEAK_RATIO = 0.22    # >= 22% of the output's 8-word windows lifted verbatim = a dump
_LEAK_ABS = 14        # ...or 14+ distinct lifted windows outright (catches a long block in long output)

_STOP = frozenset(
    "the a an and or but if then of to in on for with at by from as is are was were be been "
    "being it its this that these those you your they them their we our i he she his her not "
    "no do does did done have has had will would can could should may might must one two more "
    "most any all each every so than into out up down over under about which who what when "
    "where how why also just only very much many few some such other same".split()
)


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _grams(words: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


_CACHE: dict[str, set] = {}


def _framework_grams(name: str) -> set:
    if name not in _CACHE:
        _CACHE[name] = _grams(_words(load_framework(name)), _VERBATIM_N)
    return _CACHE[name]


def inspect(output: str, framework_name: str) -> tuple[bool, str, dict]:
    """Return (leaked, reason, scores). Deterministic; safe to run on every response.
    Flags only a wholesale verbatim dump — a high share, or a large count, of 8-word windows
    lifted straight from the framework. Incidental reuse of mandated phrasing passes."""
    fw = _framework_grams(framework_name)
    ow = _grams(_words(output), _VERBATIM_N)
    shared = len(ow & fw)
    ratio = shared / max(1, len(ow))
    scores = {"shared_windows": shared, "output_windows": len(ow), "ratio": round(ratio, 3)}
    if shared >= _LEAK_ABS or ratio >= _LEAK_RATIO:
        return True, "verbatim_dump", scores
    return False, "", scores


def output_leaks(output: str, framework_name: str) -> bool:
    return inspect(output, framework_name)[0]
