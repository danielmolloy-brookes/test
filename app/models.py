import uuid
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import relationship
from app.database import Base


def _uuid():
    return str(uuid.uuid4())


# ── Organisations ─────────────────────────────────────────────

class Organisation(Base):
    __tablename__ = "organisations"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(255), nullable=False, unique=True)
    slug       = Column(String(100), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    users   = relationship("User",        back_populates="org")
    events  = relationship("Event",       back_populates="org")
    folders = relationship("EventFolder", back_populates="org")


# ── Users ─────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    username      = Column(String(100), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role          = Column(String(20), default="admin", nullable=False)  # "developer" | "admin" | "checkin"
    display_name  = Column(String(100))
    org_id        = Column(Integer, ForeignKey("organisations.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    # 2FA
    totp_secret   = Column(String(64), nullable=True)
    totp_enabled  = Column(Boolean, default=False, nullable=False)
    totp_backup_codes = Column(Text, nullable=True)  # JSON list of hashed backup codes

    org = relationship("Organisation", back_populates="users")

    @property
    def is_admin(self):
        """True for both developer and org admin — passes all require_admin gates."""
        return self.role in ("admin", "developer")

    @property
    def is_developer(self):
        return self.role == "developer"

    @property
    def is_org_admin(self):
        return self.role == "admin"

    @property
    def is_checkin(self):
        return self.role == "checkin"


# ── Event Folders ─────────────────────────────────────────────

class EventFolder(Base):
    """A named group that events can be organised into."""
    __tablename__ = "event_folders"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(255), nullable=False)
    color      = Column(String(7), default="#6366f1")
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    org_id     = Column(Integer, ForeignKey("organisations.id", ondelete="CASCADE"), nullable=True, index=True)

    events = relationship("Event", back_populates="folder")
    org    = relationship("Organisation", back_populates="folders")


# ── Events ────────────────────────────────────────────────────

class Event(Base):
    __tablename__ = "events"

    id                = Column(Integer, primary_key=True, index=True)
    name              = Column(String(255), nullable=False)
    date              = Column(DateTime, nullable=False)
    location          = Column(String(500))
    description       = Column(Text)
    ghl_api_key       = Column(String(500))
    ghl_workflow_id   = Column(String(255))
    ghl_location_id   = Column(String(255))
    ghl_attended_tag  = Column(String(255))
    ghl_registered_tag = Column(String(255))
    ghl_checkout_tag  = Column(String(255))
    folder_id         = Column(Integer, ForeignKey("event_folders.id", ondelete="SET NULL"), nullable=True)
    org_id            = Column(Integer, ForeignKey("organisations.id", ondelete="CASCADE"), nullable=True, index=True)
    is_archived                = Column(Boolean, default=False, nullable=False)
    archived_at                = Column(DateTime, nullable=True)
    push_notifications_enabled = Column(Boolean, default=False, nullable=False)
    map_image_path             = Column(String(500), nullable=True)
    # Public slot-booking system
    booking_enabled            = Column(Boolean, default=False, nullable=False)
    slot_duration_mins         = Column(Integer, default=30, nullable=False)
    slot_capacity              = Column(Integer, default=1, nullable=False)
    # Profile sharing consent feature
    profiles_disabled          = Column(Boolean, default=False, nullable=False)
    profile_consent_enabled    = Column(Boolean, default=False, nullable=False)
    created_at                 = Column(DateTime, default=datetime.utcnow)

    folder    = relationship("EventFolder", back_populates="events")
    org       = relationship("Organisation", back_populates="events")
    attendees = relationship("Attendee", back_populates="event", cascade="all, delete-orphan")
    slots     = relationship("EventSlot", back_populates="event", cascade="all, delete-orphan")


# ── Attendees ─────────────────────────────────────────────────

class Attendee(Base):
    __tablename__ = "attendees"

    id             = Column(Integer, primary_key=True, index=True)
    event_id       = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    ghl_contact_id = Column(String(255), index=True)
    first_name     = Column(String(255))
    last_name      = Column(String(255))
    email          = Column(String(255), index=True)
    phone          = Column(String(50))
    ticket_id      = Column(String(36), unique=True, index=True, default=_uuid)
    qr_code_path   = Column(String(500))
    company        = Column(String(255), nullable=True)
    ticket_sent    = Column(Boolean, default=False)
    ticket_sent_at = Column(DateTime)
    checked_in     = Column(Boolean, default=False)
    checked_in_at  = Column(DateTime)
    checked_out    = Column(Boolean, default=False, nullable=False)
    checked_out_at = Column(DateTime, nullable=True)
    is_vip         = Column(Boolean, default=False, nullable=False)
    profile_consent = Column(Boolean, default=False, nullable=False)
    badge_issued   = Column(Boolean, default=False, nullable=False)
    badge_issued_at = Column(DateTime, nullable=True)
    notes          = Column(Text, nullable=True)
    mobile_booked  = Column(Boolean, default=False, nullable=False)
    created_at     = Column(DateTime, default=datetime.utcnow)

    event     = relationship("Event", back_populates="attendees")
    scan_logs = relationship("ScanLog", back_populates="attendee", cascade="all, delete-orphan")

    @property
    def full_name(self):
        parts = [self.first_name or "", self.last_name or ""]
        return " ".join(p for p in parts if p).strip() or self.email or "Unknown"

    @property
    def qr_url(self):
        from app.config import settings
        if self.ticket_id:
            return f"{settings.BASE_URL}/static/qr_codes/{self.ticket_id}.png"
        return None


# ── Scan Logs ─────────────────────────────────────────────────

class ScanLog(Base):
    __tablename__ = "scan_logs"

    id               = Column(Integer, primary_key=True, index=True)
    attendee_id      = Column(Integer, ForeignKey("attendees.id", ondelete="CASCADE"), nullable=True)
    event_id         = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=True, index=True)
    scanned_ticket_id = Column(String(36), nullable=True)   # raw ticket_id that was scanned
    scanned_at       = Column(DateTime, default=datetime.utcnow)
    device_info      = Column(String(500))
    result           = Column(String(50))   # "success" | "duplicate" | "not_found" | "wrong_event"

    attendee = relationship("Attendee", back_populates="scan_logs")


# ── Import Jobs ──────────────────────────────────────────────

class ImportJob(Base):
    """Background GHL import job — processes contacts page by page."""
    __tablename__ = "import_jobs"

    id           = Column(Integer, primary_key=True, index=True)
    event_id     = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    status       = Column(String(20), default="pending", nullable=False)  # pending | running | done | error
    tag          = Column(String(255), nullable=True)
    total        = Column(Integer, default=0)   # estimated from first GHL page (updated as we go)
    fetched      = Column(Integer, default=0)   # contacts fetched from GHL so far
    added        = Column(Integer, default=0)   # new attendees added to DB
    skipped      = Column(Integer, default=0)   # duplicates skipped
    errors_json  = Column(Text, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


# ── Ticket Jobs ──────────────────────────────────────────────

class TicketJob(Base):
    """Background ticket-sending job — created immediately, processed async."""
    __tablename__ = "ticket_jobs"

    id           = Column(Integer, primary_key=True, index=True)
    event_id     = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    status       = Column(String(20), default="pending", nullable=False)  # pending | running | done | error
    total        = Column(Integer, default=0)
    sent         = Column(Integer, default=0)
    failed       = Column(Integer, default=0)
    skipped      = Column(Integer, default=0)   # no GHL contact ID
    errors_json  = Column(Text, nullable=True)  # JSON list of error strings
    created_at   = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


# ── Event Permissions ─────────────────────────────────────────

class EventPermission(Base):
    """Grants a check-in user access to scan at a specific event."""
    __tablename__ = "event_permissions"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_id   = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    granted_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "event_id", name="uq_event_permission"),)


# ── Attendee self-service accounts (mobile app) ───────────────

class AttendeeUser(Base):
    """Mobile-app account linked to an attendee email address."""
    __tablename__ = "attendee_users"

    id            = Column(Integer, primary_key=True, index=True)
    email         = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=True)
    device_token  = Column(String(500), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)


# ── Event Slots ───────────────────────────────────────────────

class EventSlot(Base):
    """One bookable time window within a public event."""
    __tablename__ = "event_slots"

    id           = Column(Integer, primary_key=True, index=True)
    event_id     = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    start_time   = Column(DateTime, nullable=False)
    end_time     = Column(DateTime, nullable=False)
    capacity     = Column(Integer, default=1, nullable=False)
    booked_count = Column(Integer, default=0, nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow)

    event    = relationship("Event", back_populates="slots")
    bookings = relationship("SlotBooking", back_populates="slot", cascade="all, delete-orphan")

    @property
    def is_available(self):
        return self.booked_count < self.capacity

    @property
    def spots_left(self):
        return max(0, self.capacity - self.booked_count)


# ── Slot Bookings ─────────────────────────────────────────────

class SlotBooking(Base):
    """A public booking against a specific event slot."""
    __tablename__ = "slot_bookings"

    id         = Column(Integer, primary_key=True, index=True)
    slot_id    = Column(Integer, ForeignKey("event_slots.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id   = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    first_name = Column(String(255), nullable=False)
    last_name  = Column(String(255), nullable=True)
    email      = Column(String(255), nullable=False, index=True)
    phone      = Column(String(50), nullable=True)
    ticket_id  = Column(String(36), unique=True, index=True, default=_uuid)
    notes      = Column(Text, nullable=True)
    cancelled  = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    slot = relationship("EventSlot", back_populates="bookings")

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name or ''}".strip()
