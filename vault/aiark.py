"""AI-ARK proxy — Qwintiq's data key lives HERE (env), never on a consultant's machine.

Real mode: AI_ARK_API_KEY set -> calls api.ai-ark.com developer-portal endpoints.
Mock mode: VAULT_AIARK=mock (or no key) -> deterministic counts/rows for dev + tests.

Counting uses size=1 and reads totalElements (≈1 credit). Exports are capped hard at the
number the consultant confirmed — the server never pulls a row past the cap.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

BASE = "https://api.ai-ark.com/api/developer-portal/v1"


class DataUnavailable(Exception):
    """The proxied data lookup failed. Deliberately carries NO upstream detail — no provider
    name, URL, status code, or key hint — so nothing about the vault's internals can reach the
    consultant through an error. The real cause is logged server-side for the admin only."""


def _mock() -> bool:
    return os.environ.get("VAULT_AIARK", "").lower() == "mock" or not os.environ.get("AI_ARK_API_KEY")


def _post(path: str, body: dict) -> dict:
    import httpx

    try:
        r = httpx.post(
            f"{BASE}{path}",
            json=body,
            headers={"x-api-key": os.environ["AI_ARK_API_KEY"], "content-type": "application/json"},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        # Log the true error server-side (admin can see it in the service logs); raise a bare,
        # detail-free exception so the provider/URL never travels back to the consultant.
        logging.getLogger("qwintiq.vault").warning("data provider call failed: %s", e)
        raise DataUnavailable from None


def _mock_total(seed: str, lo: int, hi: int) -> int:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return lo + h % (hi - lo)


def count_companies(filters: dict) -> dict:
    if _mock():
        total = _mock_total("c" + json.dumps(filters, sort_keys=True), 800, 9000)
        sample = [{"company_name": "Sample Co (mock)", "website": "sample.example",
                   "country": filters.get("country", ""), "employee_count": 24,
                   "industry": filters.get("industry", "")}]
        return {"total": total, "sample": sample, "mock": True}
    body = {**_company_body(filters), "page": 0, "size": 1}
    data = _post("/companies", body)
    return {"total": data.get("totalElements", 0),
            "sample": data.get("content", [])[:1], "mock": False}


def count_people(filters: dict) -> dict:
    if _mock():
        total = _mock_total("p" + json.dumps(filters, sort_keys=True), 300, 5000)
        return {"total": total, "sample": [{"full_name": "Sam Sample (mock)",
                "title": (filters.get("titles") or ["Founder"])[0]}], "mock": True}
    body = {**_company_body(filters), **_people_body(filters), "page": 0, "size": 1}
    data = _post("/people", body)
    return {"total": data.get("totalElements", 0),
            "sample": data.get("content", [])[:1], "mock": False}


def export_rows(filters: dict, kind: str, cap: int) -> list[dict]:
    """Pull at most `cap` rows. The cap is the confirmed spend — never exceeded."""
    if _mock():
        n = min(cap, 25)
        if kind == "companies":
            return [{"company_name": f"Mock Co {i+1}", "website": f"mock{i+1}.example",
                     "country": filters.get("country", ""), "employee_count": 10 + i,
                     "industry": filters.get("industry", ""), "linkedin": ""} for i in range(n)]
        return [{"full_name": f"Mock Person {i+1}", "title": (filters.get("titles") or ["Founder"])[0],
                 "company_name": f"Mock Co {i+1}", "website": f"mock{i+1}.example",
                 "country": filters.get("country", ""), "linkedin": ""} for i in range(n)]
    rows: list[dict] = []
    page = 0
    path = "/companies" if kind == "companies" else "/people"
    base = _company_body(filters) if kind == "companies" else {**_company_body(filters), **_people_body(filters)}
    while len(rows) < cap:
        size = min(100, cap - len(rows))
        data = _post(path, {**base, "page": page, "size": size})
        got = data.get("content", [])
        rows.extend(got)
        if len(got) < size:
            break
        page += 1
    return rows[:cap]


def _company_body(f: dict) -> dict:
    body: dict = {}
    if f.get("industry"):
        body["industries"] = [f["industry"].lower()]
    if f.get("country"):
        body["locations"] = [f["country"]]
    if f.get("size_min") is not None or f.get("size_max") is not None:
        body["employeeSize"] = {"min": f.get("size_min") or 1, "max": f.get("size_max") or 100000}
    if f.get("keywords"):
        body["keywords"] = f["keywords"]
    if f.get("exclude_keywords"):
        body["excludeKeywords"] = f["exclude_keywords"]
    return body


def _people_body(f: dict) -> dict:
    body: dict = {}
    if f.get("seniorities"):
        body["seniorities"] = f["seniorities"]
    if f.get("departments"):
        body["departments"] = f["departments"]
    if f.get("titles"):
        body["titles"] = f["titles"]
    if f.get("exclude_titles"):
        body["excludeTitles"] = f["exclude_titles"]
    return body
