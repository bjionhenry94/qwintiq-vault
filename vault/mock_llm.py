"""Deterministic dev-mode generator. Builds plausible finished output from the brief
alone — by construction it can never contain framework text. Lets every pipe be tested
(and the UX panel run) with no Anthropic key on the machine."""
from __future__ import annotations

import json


def _field(task: str, key: str, default=""):
    try:
        data = json.loads(task)
        value = data.get(key, default)
        return value if isinstance(value, (list, dict)) else str(value or default)
    except Exception:
        return default


def mock_generate(framework_name: str, task: str) -> str:
    if framework_name == "copywriter":
        service = _field(task, "service", "your service")
        problem = _field(task, "problem", "the problem you told me about")
        outcome = _field(task, "outcome", "the outcome you want")
        risk = _field(task, "risk_reversal", "a simple guarantee")
        return (
            "EMAIL SEQUENCE (Lemlist-ready)\n"
            f"Email 1 — {{{{icebreaker}}}}\nQuick one — most teams dealing with {problem} "
            f"end up choosing between speed and quality. {service} exists so you don't have to: "
            f"{outcome}, backed by {risk}. Worth a look?\n\n"
            f"Email 2 (day 3) — Following up in case the timing was off. One client put it "
            f"simply: '{outcome}'. If that's on your list this quarter, happy to show you how.\n\n"
            f"Email 3 (day 7) — Last note from me. If {problem} isn't a priority right now, "
            "no problem at all — reply 'later' and I'll check back next quarter.\n\n"
            "LINKEDIN SEQUENCE\n"
            f"Connect note — Saw your work and thought there's a genuine overlap with what we "
            f"do around {outcome}.\n"
            f"Message 1 — Thanks for connecting! The short version: {service}. {risk}. "
            "Open to a quick chat?\n"
        )
    if framework_name == "icebreaker":
        plist = _field(task, "prospects", [])
        if not isinstance(plist, list):
            plist = []
        lines = []
        for p in plist or [{"name": "there", "company": "your company"}]:
            comp = p.get("company", "your company")
            evidence = (p.get("page_text", "") or "").lower()
            if "partner" in evidence:
                lines.append(f"{p.get('name','')} — Saw {comp} invites partners on your site, "
                             "which is exactly why I'm reaching out.")
            elif "seo" in evidence and "pr" not in evidence:
                lines.append(f"{p.get('name','')} — Noticed {comp} covers SEO and content but "
                             "not PR — that gap is usually where we slot in for agencies.")
            else:
                lines.append(f"{p.get('name','')} — [FLAGGED: no verified hook found for {comp}; "
                             "honest plain opener used] Came across {comp} and wanted to reach "
                             "out directly.".replace("{comp}", comp))
        return "ICEBREAKERS\n" + "\n".join(lines)
    if framework_name == "list_building":
        return "LIST PLAN\n" + task
    if framework_name == "partner_signals":
        return (
            "PARTNER SIGNALS — RUN REPORT\nSignals checked per your routine; companies "
            "qualified; decision-maker counts ready. Spend remains gated on your confirmation."
        )
    return "Finished output for: " + task[:200]
