"""Lemlist proxy — QwintiQ's Lemlist key lives HERE (admin Settings, encrypted), never on a
consultant's machine. The vault adds finished leads into a Lemlist campaign on the consultant's
behalf; the consultant never sees the key, the endpoints, or the mechanics.

Real mode: LEMLIST_API_KEY set -> calls api.lemlist.com with HTTP Basic auth (blank user, the
key as the password, per Lemlist's API).
Mock mode: VAULT_LEMLIST=mock (or no key) -> deterministic campaigns/receipts for dev + tests.
"""
from __future__ import annotations

import hashlib
import logging
import os

BASE = "https://api.lemlist.com/api"

_log = logging.getLogger("qwintiq.vault")


class DataUnavailable(Exception):
    """A Lemlist call failed. Deliberately carries NO upstream detail — no provider name, URL,
    status code, or key hint — so nothing about the vault's internals can reach the consultant
    through an error. The real cause is logged server-side for the admin only."""


def _key() -> str | None:
    """The Lemlist key: admin-set (in /admin/settings, encrypted in the DB) takes precedence over
    the host env var."""
    from db import dal

    return dal.get_secret("LEMLIST_API_KEY")


def _explicit_mock() -> bool:
    """Mock ONLY when a developer/test asks for it. A missing key is NOT a reason to fake data."""
    return os.environ.get("VAULT_LEMLIST", "").lower() == "mock"


def _mock() -> bool:
    return _explicit_mock() or not _key()


NOT_CONNECTED = ("Lemlist isn't connected to QwintiQ yet, so nothing can be listed or uploaded. "
                 "Your QwintiQ admin needs to add the Lemlist key in the Control Panel → Settings "
                 "(the 'Lemlist key' row). Nothing was changed in Lemlist. Tell the user plainly and "
                 "keep the finished list ready to load once it's connected.")


def connection_status() -> str:
    """'' when Lemlist can really be used (a key is set, or a test explicitly asked for mock data);
    otherwise the plain-English reason. Callers return this INSTEAD of fake campaigns — the live
    bug was the vault answering 'campaign not recognised' against invented cam_mock… campaigns
    because no key had ever been entered, and nobody could tell."""
    if _explicit_mock() or _key():
        return ""
    return NOT_CONNECTED


def _get(path: str) -> object:
    import httpx

    try:
        r = httpx.get(f"{BASE}{path}", auth=("", _key() or ""), timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _log.warning("lemlist GET failed: %s", e)
        raise DataUnavailable from None


class LeadRejected(Exception):
    """Lemlist answered this one lead with a 4xx. Carries a short, safe reason code so the receipt
    can say WHY (already in the campaign, in another campaign, bad email…) instead of 'failed'."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _reject_reason(status: int, text: str) -> str:
    t = (text or "").lower()
    if "already_in_campaign" in t or ("already" in t and "other" not in t):
        return "already_in_campaign"
    if status == 409 or "other_campaign" in t:
        return "in_another_campaign"
    if "unsubscrib" in t:
        return "unsubscribed"
    if "email" in t:
        return "invalid_email"
    if "linkedin" in t:
        return "invalid_linkedin_url"
    if status in (401, 403):
        return "key_rejected"
    if status == 404:
        return "campaign_not_found"
    return f"rejected_{status}"


def _post(path: str, body: dict) -> dict:
    import httpx

    try:
        r = httpx.post(f"{BASE}{path}", json=body, auth=("", _key() or ""), timeout=60)
    except Exception as e:
        _log.warning("lemlist POST transport failure: %s", type(e).__name__)
        raise DataUnavailable from None
    if 400 <= r.status_code < 500:
        # Log Lemlist's own words (the 400s of 15-16 Sept were undiagnosable without them).
        _log.warning("lemlist POST %s -> %s: %s", path.split("?")[0].rsplit("/", 1)[0],
                     r.status_code, r.text[:300])
        raise LeadRejected(_reject_reason(r.status_code, r.text))
    if r.status_code >= 500:
        _log.warning("lemlist POST -> %s: %s", r.status_code, r.text[:300])
        raise DataUnavailable
    try:
        return r.json() if r.content else {}
    except Exception:
        return {}


def _mock_campaigns() -> list[dict]:
    return [
        {"id": "cam_mockAAA111", "name": "Partner & PR outreach"},
        {"id": "cam_mockBBB222", "name": "Dental clinics — UK"},
        {"id": "cam_mockCCC333", "name": "Founders — SaaS"},
    ]


def list_campaigns() -> list[dict]:
    """Return the account's Lemlist campaigns as [{'id', 'name'}]. ~free."""
    if _mock():
        return _mock_campaigns()
    raw = _get("/campaigns")
    out = []
    for c in raw if isinstance(raw, list) else []:
        cid = c.get("_id") or c.get("id")
        if cid:
            out.append({"id": cid, "name": c.get("name", cid)})
    return out


def resolve_campaign(campaign: str) -> dict | None:
    """Accept a campaign id ('cam_…') or a name; return {'id','name'} or None if no match.
    Name matching is case-insensitive and exact after trimming."""
    campaign = (campaign or "").strip()
    if not campaign:
        return None
    if campaign.startswith("cam_"):
        # Trust an explicit id; label it from the list if we can.
        for c in list_campaigns():
            if c["id"] == campaign:
                return c
        return {"id": campaign, "name": campaign}
    target = campaign.casefold()
    for c in list_campaigns():
        if c["name"].strip().casefold() == target:
            return c
    return None


# Fields Lemlist accepts in the lead body (email travels in the URL, not the body).
_LEAD_FIELDS = ("firstName", "lastName", "companyName", "phone", "linkedinUrl",
                "picture", "jobTitle", "icebreaker", "companyDomain")


# The vault's own row shape (from pulls / enrich) -> Lemlist's field names.
_ALIASES = {"title": "jobTitle", "job_title": "jobTitle", "company_name": "companyName",
            "company": "companyName", "linkedin": "linkedinUrl", "linkedin_url": "linkedinUrl",
            "website": "companyDomain", "company_domain": "companyDomain", "domain": "companyDomain",
            "first_name": "firstName", "last_name": "lastName", "mobile": "phone"}
# Bookkeeping the vault adds to rows — never a campaign variable.
_INTERNAL = {"email", "enriched", "mock", "source", "pending", "ark_error", "full_name", "name"}


def _clean_lead(lead: dict) -> dict:
    """Map a vault row onto Lemlist's fields: rename the known columns, split full_name, drop the
    vault's bookkeeping flags and empties, and send every value as TEXT (Lemlist stores variables
    as text; booleans/objects and a non-profile 'linkedinUrl' are what it answers 400 to)."""
    lead = lead or {}
    body: dict = {}
    for k, v in lead.items():
        if k in _INTERNAL or v is None or v == "" or isinstance(v, (dict, list, bool)):
            continue
        body[_ALIASES.get(k, k)] = str(v).strip()
    full = str(lead.get("full_name") or lead.get("name") or "").strip()
    if full and "firstName" not in body:
        first, _, last = full.partition(" ")
        body["firstName"] = first
        if last and "lastName" not in body:
            body["lastName"] = last.strip()
    li = body.get("linkedinUrl", "")
    if li and "linkedin.com/" not in li.lower():
        body.pop("linkedinUrl")
    elif li and not li.lower().startswith("http"):
        body["linkedinUrl"] = "https://" + li.lstrip("/")
    return body


def add_lead(campaign_id: str, email: str, lead: dict) -> dict:
    """Add one lead to a campaign. Returns the upstream result (or a mock echo)."""
    if _mock():
        return {"email": email, "_mock": True, "campaignId": campaign_id}
    # deduplicate=true so re-running never double-adds someone already in the campaign.
    return _post(f"/campaigns/{campaign_id}/leads/{email}?deduplicate=true", _clean_lead(lead))


def upload_leads(campaign_id: str, leads: list[dict]) -> dict:
    """Push a list of leads into a campaign. Each lead needs an 'email'. Returns a summary
    with added / skipped (no email) / failed counts. Individual upstream failures are counted,
    not raised, so one bad row never aborts the whole batch."""
    added, skipped, failed = 0, 0, 0
    rejected: dict[str, list[str]] = {}
    seen: set[str] = set()
    for lead in leads or []:
        email = str((lead or {}).get("email", "")).strip().lower()
        if not email or "@" not in email:
            skipped += 1
            continue
        if email in seen:  # de-dupe within this batch before we even call out
            continue
        seen.add(email)
        try:
            add_lead(campaign_id, email, lead)
            added += 1
        except LeadRejected as e:
            rejected.setdefault(e.reason, []).append(email)
        except DataUnavailable:
            failed += 1
    return {"added": added, "skipped_no_email": skipped, "failed": failed, "rejected": rejected,
            "unique_emails": len(seen), "mock": _mock()}
