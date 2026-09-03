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


def _post(path: str, body: dict) -> dict:
    import httpx

    try:
        r = httpx.post(f"{BASE}{path}", json=body, auth=("", _key() or ""), timeout=60)
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as e:
        # Log the true error server-side (admin can see it in the service logs); raise a bare,
        # detail-free exception so the provider/URL never travels back to the consultant.
        _log.warning("lemlist POST failed: %s", e)
        raise DataUnavailable from None


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


def _clean_lead(lead: dict) -> dict:
    """Keep the known Lemlist fields plus any extra custom variables, drop empties and email."""
    body = {}
    for k, v in (lead or {}).items():
        if k == "email" or v in (None, ""):
            continue
        body[k] = v
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
        except DataUnavailable:
            failed += 1
    return {"added": added, "skipped_no_email": skipped, "failed": failed,
            "unique_emails": len(seen), "mock": _mock()}
