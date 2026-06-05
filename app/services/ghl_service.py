"""
GoHighLevel API v2 integration service.

Docs: https://highlevel.stoplight.io/docs/integrations/
"""
import logging
from typing import Optional, List, Dict, Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GHL_BASE = settings.GHL_API_BASE_URL
GHL_VERSION = settings.GHL_API_VERSION


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Version": GHL_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _check_credentials(api_key: Optional[str], location_id: Optional[str]) -> None:
    """Raise ValueError if either credential is missing."""
    if not api_key:
        raise ValueError("No GHL API Key set for this event. Edit the event to add one.")
    if not location_id:
        raise ValueError("No GHL Location ID set for this event. Edit the event to add one.")


# ── Contacts ─────────────────────────────────────────────────

async def fetch_contacts(
    api_key: str,
    location_id: str,
    limit: int = 500,
    tag: Optional[str] = None,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch contacts from GHL. Paginates automatically up to `limit` contacts."""
    try:
        _check_credentials(api_key, location_id)
    except ValueError as e:
        return {"contacts": [], "total": 0, "error": str(e)}

    all_contacts: List[Dict[str, Any]] = []
    hdrs = _headers(api_key)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if tag:
                search_after: Optional[list] = None
                while len(all_contacts) < limit:
                    page_size = min(100, limit - len(all_contacts))
                    body: Dict[str, Any] = {
                        "locationId": location_id,
                        "filters": [{"field": "tags", "operator": "contains", "value": tag}],
                        "pageLimit": page_size,
                    }
                    if search_after:
                        body["searchAfter"] = search_after
                    response = await client.post(f"{GHL_BASE}/contacts/search", headers=hdrs, json=body)
                    response.raise_for_status()
                    data = response.json()
                    page_contacts = data.get("contacts", [])
                    all_contacts.extend([_parse_contact(c) for c in page_contacts])
                    if not page_contacts or len(page_contacts) < page_size:
                        break
                    search_after = page_contacts[-1].get("searchAfter")
                    if not search_after:
                        break
            else:
                next_page_id: Optional[str] = None
                while len(all_contacts) < limit:
                    page_size = min(100, limit - len(all_contacts))
                    params: Dict[str, Any] = {"locationId": location_id, "limit": page_size}
                    if query:
                        params["query"] = query
                    if next_page_id:
                        params["startAfterId"] = next_page_id
                    response = await client.get(f"{GHL_BASE}/contacts/", headers=hdrs, params=params)
                    response.raise_for_status()
                    data = response.json()
                    page_contacts = data.get("contacts", [])
                    all_contacts.extend([_parse_contact(c) for c in page_contacts])
                    if not page_contacts or len(page_contacts) < page_size:
                        break
                    next_page_id = data.get("meta", {}).get("startAfterId")
                    if not next_page_id:
                        break
        return {"contacts": all_contacts, "total": len(all_contacts), "count": len(all_contacts)}
    except httpx.HTTPStatusError as e:
        logger.error(f"GHL contacts fetch failed: {e.response.status_code} {e.response.text}")
        return {"contacts": [], "total": 0, "error": f"GHL API error: {e.response.status_code}"}
    except Exception as e:
        logger.error(f"GHL contacts fetch error: {e}")
        return {"contacts": [], "total": 0, "error": str(e)}


async def count_contacts(
    api_key: str,
    location_id: str,
    tag: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the total number of contacts (optionally filtered by tag) without fetching all pages."""
    try:
        _check_credentials(api_key, location_id)
    except ValueError as e:
        return {"count": 0, "error": str(e)}

    hdrs = _headers(api_key)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if tag:
                body = {
                    "locationId": location_id,
                    "filters": [{"field": "tags", "operator": "contains", "value": tag}],
                    "pageLimit": 1,
                }
                response = await client.post(f"{GHL_BASE}/contacts/search", headers=hdrs, json=body)
                response.raise_for_status()
                data = response.json()
                count = data.get("total", len(data.get("contacts", [])))
            else:
                response = await client.get(
                    f"{GHL_BASE}/contacts/",
                    headers=hdrs,
                    params={"locationId": location_id, "limit": 1},
                )
                response.raise_for_status()
                data = response.json()
                count = data.get("meta", {}).get("total", len(data.get("contacts", [])))
        return {"count": count}
    except httpx.HTTPStatusError as e:
        logger.error(f"GHL count contacts failed: {e.response.status_code} {e.response.text}")
        return {"count": 0, "error": f"GHL API error: {e.response.status_code}"}
    except Exception as e:
        logger.error(f"GHL count contacts error: {e}")
        return {"count": 0, "error": str(e)}


async def search_contacts_by_email(api_key: str, location_id: str, email: str) -> Optional[Dict[str, Any]]:
    """Look up a single contact by email."""
    result = await fetch_contacts(api_key=api_key, location_id=location_id, query=email, limit=5)
    contacts = result.get("contacts", [])
    for c in contacts:
        if c.get("email", "").lower() == email.lower():
            return c
    return contacts[0] if contacts else None


async def fetch_contact(api_key: str, contact_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single contact by ID."""
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{GHL_BASE}/contacts/{contact_id}",
                headers=_headers(api_key),
            )
            response.raise_for_status()
            data = response.json()
            return _parse_contact(data.get("contact", data))
    except Exception as e:
        logger.error(f"GHL fetch contact {contact_id} error: {e}")
        return None


# ── Tags ─────────────────────────────────────────────────────

async def add_tags(api_key: str, contact_id: str, tags: List[str]) -> bool:
    """Add tags to a GHL contact."""
    if not api_key or not tags:
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{GHL_BASE}/contacts/{contact_id}/tags",
                headers=_headers(api_key),
                json={"tags": tags},
            )
            response.raise_for_status()
            logger.info(f"Added tags {tags} to contact {contact_id}")
            return True
    except httpx.HTTPStatusError as e:
        logger.error(f"GHL add tags failed for {contact_id}: {e.response.status_code} {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"GHL add tags error: {e}")
        return False


async def remove_tags(api_key: str, contact_id: str, tags: List[str]) -> bool:
    """Remove tags from a GHL contact."""
    if not api_key or not tags:
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.delete(
                f"{GHL_BASE}/contacts/{contact_id}/tags",
                headers=_headers(api_key),
                json={"tags": tags},
            )
            response.raise_for_status()
            logger.info(f"Removed tags {tags} from contact {contact_id}")
            return True
    except httpx.HTTPStatusError as e:
        logger.error(f"GHL remove tags failed for {contact_id}: {e.response.status_code} {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"GHL remove tags error: {e}")
        return False


# ── Contact Update ───────────────────────────────────────────

async def update_contact(api_key: str, contact_id: str, fields: Dict[str, Any]) -> bool:
    """Update contact fields (including custom fields)."""
    if not api_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.put(
                f"{GHL_BASE}/contacts/{contact_id}",
                headers=_headers(api_key),
                json=fields,
            )
            response.raise_for_status()
            return True
    except httpx.HTTPStatusError as e:
        logger.error(f"GHL update contact {contact_id} failed: {e.response.status_code} {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"GHL update contact error: {e}")
        return False


# ── Workflow Trigger ─────────────────────────────────────────

async def trigger_workflow(api_key: str, contact_id: str, workflow_id: str) -> Optional[str]:
    """Returns None on success, or an error string on failure."""
    if not api_key:
        return "No GHL API Key set for this event."
    if not workflow_id:
        return "No workflow ID provided"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{GHL_BASE}/contacts/{contact_id}/workflow/{workflow_id}",
                headers=_headers(api_key),
                json={},
            )
            response.raise_for_status()
            logger.info(f"Triggered workflow {workflow_id} for contact {contact_id}")
            return None
    except httpx.HTTPStatusError as e:
        msg = f"GHL {e.response.status_code}: {e.response.text[:200]}"
        logger.error(f"Workflow trigger failed for contact {contact_id}: {msg}")
        return msg
    except Exception as e:
        logger.error(f"GHL workflow trigger error: {e}")
        return str(e)


# ── Full check-in action ─────────────────────────────────────

async def process_checkin_in_ghl(
    api_key: str,
    contact_id: str,
    attended_tag: Optional[str],
    registered_tag: Optional[str],
) -> Dict[str, bool]:
    """Add attended tag and remove registered tag after check-in."""
    results = {}
    if contact_id and attended_tag:
        results["tag_added"] = await add_tags(api_key, contact_id, [attended_tag])
    if contact_id and registered_tag:
        results["tag_removed"] = await remove_tags(api_key, contact_id, [registered_tag])
    return results


async def process_checkout_in_ghl(
    api_key: str,
    contact_id: str,
    checkout_tag: Optional[str],
) -> Dict[str, bool]:
    """Add checkout tag when attendee checks out."""
    results = {}
    if contact_id and checkout_tag:
        results["tag_added"] = await add_tags(api_key, contact_id, [checkout_tag])
    return results


# ── Ticket sending ───────────────────────────────────────────

async def send_ticket_via_workflow(
    api_key: str,
    contact_id: str,
    workflow_id: str,
    ticket_data: Dict[str, Any],
) -> Optional[str]:
    """
    Update the contact's custom fields with ticket info,
    then trigger the workflow that sends the email.
    Returns None on success, or an error string on failure.
    """
    # Update custom fields first (best-effort — don't block on failure)
    custom_fields = [
        {"key": "ticket_id", "field_value": ticket_data.get("ticket_id", "")},
        {"key": "ticket_url", "field_value": ticket_data.get("qr_url", "")},
        {"key": "profile_url", "field_value": ticket_data.get("profile_url", "")},
        {"key": "event_name", "field_value": ticket_data.get("event_name", "")},
        {"key": "event_date", "field_value": ticket_data.get("event_date", "")},
        {"key": "event_location", "field_value": ticket_data.get("event_location", "")},
    ]
    update_ok = await update_contact(api_key, contact_id, {"customFields": custom_fields})
    if not update_ok:
        logger.warning(f"Custom field update failed for {contact_id}, proceeding with workflow trigger anyway")

    return await trigger_workflow(api_key, contact_id, workflow_id)


# ── Helpers ──────────────────────────────────────────────────

def _parse_contact(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a GHL contact response to our internal format."""
    return {
        "id": raw.get("id", ""),
        "first_name": raw.get("firstName") or raw.get("first_name") or "",
        "last_name": raw.get("lastName") or raw.get("last_name") or "",
        "email": raw.get("email", ""),
        "phone": raw.get("phone", ""),
        "company": raw.get("companyName") or raw.get("businessName") or raw.get("company") or "",
        "tags": raw.get("tags", []),
    }


async def test_connection(api_key: str, location_id: str) -> Dict[str, Any]:
    """Test GHL API connectivity using the event's credentials."""
    if not api_key:
        return {"ok": False, "message": "No API Key set for this event."}
    if not location_id:
        return {"ok": False, "message": "No Location ID set for this event."}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{GHL_BASE}/contacts/",
                headers=_headers(api_key),
                params={"locationId": location_id, "limit": 1},
            )
            if response.status_code == 200:
                return {"ok": True, "message": "GHL connection successful"}
            return {"ok": False, "message": f"HTTP {response.status_code}: {response.text[:200]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
