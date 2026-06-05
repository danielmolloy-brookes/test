from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import get_current_user_api
from app.database import get_db
from app.models import User
from app.services import ghl_service

router = APIRouter(prefix="/api/ghl")


@router.get("/test")
async def test_ghl_connection(
    api_key: str = Query(..., description="GHL API Key"),
    location_id: str = Query(..., description="GHL Location ID"),
    current_user: User = Depends(get_current_user_api),
):
    """Test GHL API connectivity using event credentials."""
    return await ghl_service.test_connection(api_key=api_key, location_id=location_id)


@router.get("/count")
async def count_contacts(
    api_key: str = Query(..., description="GHL API Key"),
    location_id: str = Query(..., description="GHL Location ID"),
    tag: Optional[str] = Query(None, description="Filter by GHL tag"),
    current_user: User = Depends(get_current_user_api),
):
    """Return the total contact count (optionally filtered by tag) without fetching all contacts."""
    return await ghl_service.count_contacts(api_key=api_key, location_id=location_id, tag=tag)


@router.get("/contacts")
async def get_contacts(
    api_key: str = Query(..., description="GHL API Key"),
    location_id: str = Query(..., description="GHL Location ID"),
    tag: Optional[str] = Query(None, description="Filter by GHL tag"),
    query: Optional[str] = Query(None, description="Search query"),
    limit: int = Query(500, le=2000),
    current_user: User = Depends(get_current_user_api),
):
    """Fetch contacts from GHL with optional tag/search filter."""
    return await ghl_service.fetch_contacts(
        api_key=api_key, location_id=location_id,
        limit=limit, tag=tag, query=query,
    )


@router.get("/contacts/{contact_id}")
async def get_contact(
    contact_id: str,
    api_key: str = Query(..., description="GHL API Key"),
    current_user: User = Depends(get_current_user_api),
):
    """Fetch a single GHL contact."""
    contact = await ghl_service.fetch_contact(api_key=api_key, contact_id=contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found in GHL")
    return contact
