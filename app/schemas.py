from __future__ import annotations
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, EmailStr, field_validator


# ── Auth ─────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ── Folders ──────────────────────────────────────────────────
class FolderCreate(BaseModel):
    name: str
    color: Optional[str] = "#6366f1"

class FolderUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None

class FolderOut(BaseModel):
    id: int
    name: str
    color: str
    created_at: datetime
    event_count: int = 0
    model_config = {"from_attributes": True}


# ── Events ───────────────────────────────────────────────────
class EventCreate(BaseModel):
    name: str
    date: datetime
    location: Optional[str] = None
    description: Optional[str] = None
    ghl_workflow_id: Optional[str] = None
    ghl_attended_tag: Optional[str] = None
    ghl_registered_tag: Optional[str] = None
    ghl_checkout_tag: Optional[str] = None


class EventUpdate(BaseModel):
    name: Optional[str] = None
    date: Optional[datetime] = None
    location: Optional[str] = None
    description: Optional[str] = None
    ghl_workflow_id: Optional[str] = None
    ghl_attended_tag: Optional[str] = None
    ghl_registered_tag: Optional[str] = None
    ghl_checkout_tag: Optional[str] = None


class EventOut(BaseModel):
    id: int
    name: str
    date: datetime
    location: Optional[str]
    description: Optional[str]
    ghl_workflow_id: Optional[str]
    ghl_attended_tag: Optional[str]
    ghl_registered_tag: Optional[str]
    ghl_checkout_tag: Optional[str] = None
    folder_id: Optional[int] = None
    folder_name: Optional[str] = None
    folder_color: Optional[str] = None
    is_archived: bool = False
    archived_at: Optional[datetime] = None
    created_at: datetime
    total_attendees: int = 0
    checked_in_count: int = 0
    checked_out_count: int = 0
    badge_issued_count: int = 0
    tickets_sent_count: int = 0

    model_config = {"from_attributes": True}


# ── Attendees ────────────────────────────────────────────────
class AttendeeCreate(BaseModel):
    ghl_contact_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    is_vip: bool = False
    notes: Optional[str] = None


class AttendeeOut(BaseModel):
    id: int
    event_id: int
    ghl_contact_id: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    company: Optional[str] = None
    ticket_id: Optional[str]
    qr_code_path: Optional[str]
    ticket_sent: bool
    ticket_sent_at: Optional[datetime]
    checked_in: bool
    checked_in_at: Optional[datetime]
    checked_out: bool = False
    checked_out_at: Optional[datetime] = None
    is_vip: bool = False
    profile_consent: bool = False
    badge_issued: bool = False
    badge_issued_at: Optional[datetime] = None
    notes: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Check-in ─────────────────────────────────────────────────
class ScanRequest(BaseModel):
    ticket_id: str
    event_id: int
    device_info: Optional[str] = None

    @property
    def resolved_ticket_id(self) -> str:
        """
        Extract the UUID whether the scanner sent a bare UUID or a full
        profile URL (e.g. https://example.com/1f6bb91f-...).
        Handles both old and new QR code formats transparently.
        """
        import re
        UUID_RE = re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            re.IGNORECASE,
        )
        m = UUID_RE.search(self.ticket_id)
        return m.group(0) if m else self.ticket_id


class ScanResult(BaseModel):
    success: bool
    status: str          # "checked_in" | "duplicate" | "not_found"
    message: str
    attendee: Optional[AttendeeOut] = None
    is_vip: bool = False


class ManualCheckIn(BaseModel):
    attendee_id: int


# ── GHL ──────────────────────────────────────────────────────
class GHLContact(BaseModel):
    id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    tags: List[str] = []


class GHLImportRequest(BaseModel):
    tag: Optional[str] = None
    pipeline_id: Optional[str] = None
    contact_ids: Optional[List[str]] = None


class SendTicketsRequest(BaseModel):
    attendee_ids: Optional[List[int]] = None   # None = all unsent
    resend_all: bool = False                    # True = include already-sent attendees


# ── Reports ──────────────────────────────────────────────────
class AttendanceStats(BaseModel):
    event_id: int
    event_name: str
    event_date: datetime
    total: int
    checked_in: int
    not_checked_in: int
    tickets_sent: int
    percentage: float


class RecentScan(BaseModel):
    attendee_id: int
    full_name: str
    email: Optional[str]
    checked_in_at: Optional[datetime]
    ticket_id: Optional[str]
