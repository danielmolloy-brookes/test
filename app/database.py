import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings


# SQLite WAL mode for better concurrency
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)


# Enable WAL mode + foreign keys for SQLite
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables, run migrations, and seed the admin user."""
    from app.models import User, Event, Attendee, ScanLog, EventPermission, EventFolder, Organisation  # noqa: F401
    Base.metadata.create_all(bind=engine)

    # ── Migrations for existing databases ────────────────────
    _run_migrations()
    _run_mobile_migrations()
    _run_booking_migrations()
    _run_branding_migrations()
    _run_consent_migrations()
    _run_totp_migrations()

    # Ensure QR code directory exists
    os.makedirs(settings.QR_CODE_DIR, exist_ok=True)

    # Seed bootstrap developer user if missing
    db = SessionLocal()
    try:
        from app.auth import get_password_hash
        admin = db.query(User).filter(User.username == settings.ADMIN_USERNAME).first()
        if not admin:
            admin_user = User(
                username=settings.ADMIN_USERNAME,
                password_hash=get_password_hash(settings.ADMIN_PASSWORD),
                role="developer",
                display_name="Admin",
                org_id=None,
            )
            db.add(admin_user)
            db.commit()
        else:
            # Ensure existing bootstrap user has a valid role
            if not admin.role:
                admin.role = "developer"
                db.commit()
    finally:
        db.close()


def _run_migrations():
    """Safe ALTER TABLE migrations for existing SQLite databases."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    with engine.connect() as conn:
        # Add 'role' column to users if missing
        user_cols = [c["name"] for c in insp.get_columns("users")]
        if "role" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'admin'"))
            conn.commit()
        if "display_name" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN display_name VARCHAR(100)"))
            conn.commit()

        # Create event_folders table for existing databases
        if "event_folders" not in insp.get_table_names():
            conn.execute(text("""
                CREATE TABLE event_folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(255) NOT NULL UNIQUE,
                    color VARCHAR(7) DEFAULT '#6366f1',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    created_by INTEGER REFERENCES users(id)
                )
            """))
            conn.commit()

        # Add folder / archive columns to events for existing databases
        event_cols = [c["name"] for c in insp.get_columns("events")]
        if "folder_id" not in event_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN folder_id INTEGER REFERENCES event_folders(id) ON DELETE SET NULL"))
            conn.commit()
        if "is_archived" not in event_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        if "archived_at" not in event_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN archived_at DATETIME"))
            conn.commit()

        # Create event_permissions table for existing databases
        if "event_permissions" not in insp.get_table_names():
            conn.execute(text("""
                CREATE TABLE event_permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    granted_by INTEGER REFERENCES users(id),
                    granted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, event_id)
                )
            """))
            conn.commit()

        # ── Multi-tenant migration ────────────────────────────────

        # 1. Create organisations table
        if "organisations" not in insp.get_table_names():
            conn.execute(text("""
                CREATE TABLE organisations (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       VARCHAR(255) NOT NULL UNIQUE,
                    slug       VARCHAR(100) NOT NULL UNIQUE,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.commit()

        # 2. Seed Default Organisation (idempotent)
        default_row = conn.execute(
            text("SELECT id FROM organisations WHERE slug = 'default'")
        ).fetchone()
        if not default_row:
            conn.execute(text(
                "INSERT INTO organisations (name, slug) VALUES ('Default Organisation', 'default')"
            ))
            conn.commit()
            default_row = conn.execute(
                text("SELECT id FROM organisations WHERE slug = 'default'")
            ).fetchone()
        default_org_id = default_row[0]

        # 3. Add org_id to users; promote existing admins to developer
        user_cols = [c["name"] for c in insp.get_columns("users")]
        if "org_id" not in user_cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN org_id INTEGER REFERENCES organisations(id) ON DELETE SET NULL"
            ))
            conn.commit()
            # Existing admins become developers (no org)
            conn.execute(text("UPDATE users SET role = 'developer' WHERE role = 'admin'"))
            # Existing checkin users join Default Organisation
            conn.execute(text(
                f"UPDATE users SET org_id = {default_org_id} WHERE role = 'checkin'"
            ))
            conn.commit()

        # 4. Add org_id to events
        event_cols = [c["name"] for c in insp.get_columns("events")]
        if "org_id" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN org_id INTEGER REFERENCES organisations(id) ON DELETE CASCADE"
            ))
            conn.commit()
            conn.execute(text(f"UPDATE events SET org_id = {default_org_id}"))
            conn.commit()

        # 5. Add ghl_api_key to events (may have been added in an earlier session)
        event_cols2 = [c["name"] for c in insp.get_columns("events")]
        if "ghl_api_key" not in event_cols2:
            conn.execute(text("ALTER TABLE events ADD COLUMN ghl_api_key VARCHAR(500)"))
            conn.commit()
        if "ghl_location_id" not in event_cols2:
            conn.execute(text("ALTER TABLE events ADD COLUMN ghl_location_id VARCHAR(255)"))
            conn.commit()

        # 6. Recreate event_folders with per-org unique name constraint
        #    (replaces the global UNIQUE(name) with UNIQUE(name, org_id))
        folder_indexes = [idx["name"] for idx in insp.get_indexes("event_folders")]
        has_per_org_unique = any("org" in idx.lower() for idx in folder_indexes)
        if not has_per_org_unique:
            conn.execute(text("""
                CREATE TABLE event_folders_new (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       VARCHAR(255) NOT NULL,
                    color      VARCHAR(7) DEFAULT '#6366f1',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    created_by INTEGER REFERENCES users(id),
                    org_id     INTEGER REFERENCES organisations(id) ON DELETE CASCADE,
                    UNIQUE(name, org_id)
                )
            """))
            # Copy existing rows; add org_id if column exists
            folder_cols = [c["name"] for c in insp.get_columns("event_folders")]
            if "org_id" in folder_cols:
                conn.execute(text(
                    "INSERT INTO event_folders_new (id, name, color, created_at, created_by, org_id) "
                    "SELECT id, name, color, created_at, created_by, org_id FROM event_folders"
                ))
            else:
                conn.execute(text(
                    f"INSERT INTO event_folders_new (id, name, color, created_at, created_by, org_id) "
                    f"SELECT id, name, color, created_at, created_by, {default_org_id} FROM event_folders"
                ))
            conn.execute(text("DROP TABLE event_folders"))
            conn.execute(text("ALTER TABLE event_folders_new RENAME TO event_folders"))
            conn.commit()
        else:
            # Table already has per-org unique; just ensure org_id column is populated
            folder_cols = [c["name"] for c in insp.get_columns("event_folders")]
            if "org_id" in folder_cols:
                conn.execute(text(
                    f"UPDATE event_folders SET org_id = {default_org_id} WHERE org_id IS NULL"
                ))
                conn.commit()

        # ── scan_logs: make attendee_id nullable, add event_id + scanned_ticket_id ──
        scan_cols = [c["name"] for c in insp.get_columns("scan_logs")]
        if "scanned_ticket_id" not in scan_cols:
            # Recreate table with nullable attendee_id and new columns
            conn.execute(text("""
                CREATE TABLE scan_logs_new (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    attendee_id       INTEGER REFERENCES attendees(id) ON DELETE CASCADE,
                    event_id          INTEGER REFERENCES events(id) ON DELETE CASCADE,
                    scanned_ticket_id VARCHAR(36),
                    scanned_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
                    device_info       VARCHAR(500),
                    result            VARCHAR(50)
                )
            """))
            conn.execute(text(
                "INSERT INTO scan_logs_new (id, attendee_id, scanned_at, device_info, result) "
                "SELECT id, attendee_id, scanned_at, device_info, result FROM scan_logs"
            ))
            conn.execute(text("DROP TABLE scan_logs"))
            conn.execute(text("ALTER TABLE scan_logs_new RENAME TO scan_logs"))
            conn.commit()

        # ── Import jobs table ─────────────────────────────────────
        if "import_jobs" not in insp.get_table_names():
            conn.execute(text("""
                CREATE TABLE import_jobs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id     INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    status       VARCHAR(20) NOT NULL DEFAULT 'pending',
                    tag          VARCHAR(255),
                    total        INTEGER NOT NULL DEFAULT 0,
                    fetched      INTEGER NOT NULL DEFAULT 0,
                    added        INTEGER NOT NULL DEFAULT 0,
                    skipped      INTEGER NOT NULL DEFAULT 0,
                    errors_json  TEXT,
                    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME
                )
            """))
            conn.commit()

        # ── Ticket jobs table ─────────────────────────────────────
        if "ticket_jobs" not in insp.get_table_names():
            conn.execute(text("""
                CREATE TABLE ticket_jobs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id     INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    status       VARCHAR(20) NOT NULL DEFAULT 'pending',
                    total        INTEGER NOT NULL DEFAULT 0,
                    sent         INTEGER NOT NULL DEFAULT 0,
                    failed       INTEGER NOT NULL DEFAULT 0,
                    skipped      INTEGER NOT NULL DEFAULT 0,
                    errors_json  TEXT,
                    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME
                )
            """))
            conn.commit()

        # ── Attendee extra fields migration ───────────────────────
        attendee_cols = [c["name"] for c in insp.get_columns("attendees")]
        if "is_vip" not in attendee_cols:
            conn.execute(text("ALTER TABLE attendees ADD COLUMN is_vip BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        if "notes" not in attendee_cols:
            conn.execute(text("ALTER TABLE attendees ADD COLUMN notes TEXT"))
            conn.commit()
        if "company" not in attendee_cols:
            conn.execute(text("ALTER TABLE attendees ADD COLUMN company VARCHAR(255)"))
            conn.commit()
        if "badge_issued" not in attendee_cols:
            conn.execute(text("ALTER TABLE attendees ADD COLUMN badge_issued BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        if "badge_issued_at" not in attendee_cols:
            conn.execute(text("ALTER TABLE attendees ADD COLUMN badge_issued_at DATETIME"))
            conn.commit()
        if "checked_out" not in attendee_cols:
            conn.execute(text("ALTER TABLE attendees ADD COLUMN checked_out BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        if "checked_out_at" not in attendee_cols:
            conn.execute(text("ALTER TABLE attendees ADD COLUMN checked_out_at DATETIME"))
            conn.commit()

        # ── ghl_checkout_tag on events ────────────────────────────
        event_cols_v2 = [c["name"] for c in insp.get_columns("events")]
        if "ghl_checkout_tag" not in event_cols_v2:
            conn.execute(text("ALTER TABLE events ADD COLUMN ghl_checkout_tag VARCHAR(255)"))
            conn.commit()


def _run_mobile_migrations():
    """Mobile-app additions — safe to run on existing databases."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    with engine.connect() as conn:
        # attendee_users table
        if "attendee_users" not in insp.get_table_names():
            conn.execute(text("""
                CREATE TABLE attendee_users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    email         VARCHAR(255) NOT NULL UNIQUE,
                    password_hash VARCHAR(255),
                    device_token  VARCHAR(500),
                    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_login_at DATETIME
                )
            """))
            conn.execute(text("CREATE INDEX ix_attendee_users_email ON attendee_users (email)"))
            conn.commit()

        event_cols = [c["name"] for c in insp.get_columns("events")]
        if "push_notifications_enabled" not in event_cols:
            conn.execute(text(
                "ALTER TABLE events ADD COLUMN push_notifications_enabled BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()
        if "map_image_path" not in event_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN map_image_path VARCHAR(500)"))
            conn.commit()

        attendee_cols = [c["name"] for c in insp.get_columns("attendees")]
        if "mobile_booked" not in attendee_cols:
            conn.execute(text(
                "ALTER TABLE attendees ADD COLUMN mobile_booked BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()


def _run_booking_migrations():
    """Public slot-booking system migrations."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    with engine.connect() as conn:
        # booking columns on events
        event_cols = [c["name"] for c in insp.get_columns("events")]
        if "booking_enabled" not in event_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN booking_enabled BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        if "slot_duration_mins" not in event_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN slot_duration_mins INTEGER NOT NULL DEFAULT 30"))
            conn.commit()
        if "slot_capacity" not in event_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN slot_capacity INTEGER NOT NULL DEFAULT 1"))
            conn.commit()

        # event_slots table
        if "event_slots" not in insp.get_table_names():
            conn.execute(text("""
                CREATE TABLE event_slots (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id     INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    start_time   DATETIME NOT NULL,
                    end_time     DATETIME NOT NULL,
                    capacity     INTEGER NOT NULL DEFAULT 1,
                    booked_count INTEGER NOT NULL DEFAULT 0,
                    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text("CREATE INDEX ix_event_slots_event_id ON event_slots (event_id)"))
            conn.commit()

        # slot_bookings table
        if "slot_bookings" not in insp.get_table_names():
            conn.execute(text("""
                CREATE TABLE slot_bookings (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_id    INTEGER NOT NULL REFERENCES event_slots(id) ON DELETE CASCADE,
                    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    first_name VARCHAR(255) NOT NULL,
                    last_name  VARCHAR(255),
                    email      VARCHAR(255) NOT NULL,
                    phone      VARCHAR(50),
                    ticket_id  VARCHAR(36) UNIQUE,
                    notes      TEXT,
                    cancelled  BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text("CREATE INDEX ix_slot_bookings_slot_id  ON slot_bookings (slot_id)"))
            conn.execute(text("CREATE INDEX ix_slot_bookings_email     ON slot_bookings (email)"))
            conn.execute(text("CREATE INDEX ix_slot_bookings_ticket_id ON slot_bookings (ticket_id)"))
            conn.commit()


def _run_branding_migrations():
    """Event branding columns."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    with engine.connect() as conn:
        event_cols = [c["name"] for c in insp.get_columns("events")]
        for col, default in [
            ("brand_primary",   "'#6366f1'"),
            ("brand_secondary", "'#4f46e5'"),
            ("brand_tertiary",  "'#818cf8'"),
            ("brand_backdrop_path", "NULL"),
            ("brand_logo_path",     "NULL"),
        ]:
            if col not in event_cols:
                dtype = "VARCHAR(7)" if "path" not in col else "VARCHAR(500)"
                conn.execute(text(f"ALTER TABLE events ADD COLUMN {col} {dtype} DEFAULT {default}"))
                conn.commit()


def _run_consent_migrations():
    """Profile sharing consent feature migrations."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    with engine.connect() as conn:
        event_cols = [c["name"] for c in insp.get_columns("events")]
        if "profiles_disabled" not in event_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN profiles_disabled BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        if "profile_consent_enabled" not in event_cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN profile_consent_enabled BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        attendee_cols = [c["name"] for c in insp.get_columns("attendees")]
        if "profile_consent" not in attendee_cols:
            conn.execute(text("ALTER TABLE attendees ADD COLUMN profile_consent BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()


def _run_totp_migrations():
    """2FA TOTP columns on users table."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    with engine.connect() as conn:
        user_cols = [c["name"] for c in insp.get_columns("users")]
        if "totp_secret" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN totp_secret VARCHAR(64)"))
            conn.commit()
        if "totp_enabled" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        if "totp_backup_codes" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN totp_backup_codes TEXT"))
            conn.commit()
