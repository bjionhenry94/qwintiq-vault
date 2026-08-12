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

# Thresholds tuned against real finished outputs (tests/test_leakguard.py): legitimate work
# scores at or near zero on all three; leaks clear these easily.
_VERBATIM_N = 8
_NEAR_N = 5            # 5-grams are distinctive enough that a chance collision is rare, so even
_NEAR_MIN = 2          # a couple of shared 5-word runs signals lightly-edited copy, not reuse of
                       # an example phrase (frameworks contain example outputs, which 4-grams hit)
_STRUCT_N = 3
_STRUCT_MIN = 6        # >= 6 distinct shared framework trigrams = describing the method

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


def _structural_grams(words: list[str]) -> set[tuple[str, ...]]:
    """Trigrams that carry at least two non-stopword tokens — the framework's distinctive
    phrasing, not generic English glue."""
    out = set()
    for i in range(max(0, len(words) - _STRUCT_N + 1)):
        g = tuple(words[i:i + _STRUCT_N])
        if sum(1 for w in g if w not in _STOP and len(w) > 2) >= 2:
            out.add(g)
    return out


_CACHE: dict[str, dict] = {}


def _framework_grams(name: str) -> dict:
    if name not in _CACHE:
        w = _words(load_framework(name))
        _CACHE[name] = {
            "verbatim": _grams(w, _VERBATIM_N),
            "near": _grams(w, _NEAR_N),
            "struct": _structural_grams(w),
        }
    return _CACHE[name]


def inspect(output: str, framework_name: str) -> tuple[bool, str, dict]:
    """Return (leaked, reason, scores). Deterministic; safe to run on every response."""
    fw = _framework_grams(framework_name)
    ow = _words(output)
    verbatim = len(_grams(ow, _VERBATIM_N) & fw["verbatim"])
    near = len(_grams(ow, _NEAR_N) & fw["near"])
    struct = len(_structural_grams(ow) & fw["struct"])
    scores = {"verbatim": verbatim, "near": near, "struct": struct}
    if verbatim >= 1:
        return True, "verbatim", scores
    if near >= _NEAR_MIN:
        return True, "near_verbatim", scores
    if struct >= _STRUCT_MIN:
        return True, "structural", scores
    return False, "", scores


def output_leaks(output: str, framework_name: str) -> bool:
    return inspect(output, framework_name)[0]
