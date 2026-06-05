# Smart Event Check-In

A lightweight internal event check-in system with GoHighLevel (GHL) integration.

Built with **FastAPI + SQLite + Python QR generation + Jinja2 templates**.

---

## Features

- ✅ Pull contacts directly from GoHighLevel
- ✅ Import attendees via CSV (auto-matched to GHL contacts)
- ✅ Generate unique QR code tickets
- ✅ Send tickets via GHL workflow (email with QR code)
- ✅ Live QR scanner (phone camera + webcam)
- ✅ Automatic GHL tag update on check-in ("Attended - Event Name")
- ✅ Manual check-in search
- ✅ Live attendance dashboard
- ✅ Attendance reports + CSV export
- ✅ Docker-ready deployment

---

## Quick Start (Local)

### 1. Clone & configure

```bash
cd smart-event-checkin
cp .env.example .env
```

Edit `.env` with your values:

```env
SECRET_KEY=your-random-64-char-secret
GHL_API_KEY=your-ghl-api-key
GHL_LOCATION_ID=your-ghl-location-id
BASE_URL=http://localhost:8000
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-secure-password
```

### 2. Install & run (without Docker)

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000** — login with your admin credentials.

### 3. Run with Docker

```bash
docker-compose up -d
```

App runs at **http://localhost:8000**.

---

## GoHighLevel Setup

### API Key

1. Go to **GHL → Settings → API Keys**
2. Create a new key with read/write access to Contacts
3. Copy to `GHL_API_KEY` in `.env`

### Location ID

1. In GHL, go to **Settings → Business Profile**
2. Copy the Location ID (sub-account ID)
3. Paste into `GHL_LOCATION_ID` in `.env`

### Workflow for Sending Tickets

Create a GHL Automation/Workflow that:
1. Trigger: **Contact enters workflow** (or a tag is added)
2. Action: **Send Email**
3. Email template uses custom fields:

```
Subject: Your ticket for {{event_name}}

Hi {{contact.first_name}},

You're registered for {{event_name}}!

📅 Date: {{event_date}}
📍 Location: {{event_location}}
🎟️ Ticket ID: {{ticket_id}}

Please bring your QR code (attached) or show this email at the door:
{{ticket_url}}

See you there!
```

Copy the Workflow ID from the URL (e.g. `https://app.gohighlevel.com/v2/location/xxx/workflows/WORKFLOW_ID_HERE`)

Paste it into the event settings in the app.

### GHL Custom Fields (optional but recommended)

Create these custom fields in GHL for ticket data:
- `ticket_id` (Text)
- `ticket_url` (Text / URL)
- `event_name` (Text)
- `event_date` (Text)
- `event_location` (Text)

The app will populate these automatically before triggering the workflow.

---

## User Guide

### Create an Event
1. Login → **New Event**
2. Fill in event name, date, location
3. Add the **GHL Workflow ID** (for sending emails)
4. Set tags:
   - **Attended tag**: e.g. `Attended - Job Fair June 2024` (auto-applied on scan)
   - **Registered tag**: e.g. `Registered - Job Fair June 2024` (auto-removed on scan)

### Import Attendees

**From GHL:**
1. Event → **Attendees** → "From GHL" tab
2. Filter by tag (optional) → Preview → Select → Import

**From CSV:**
1. Prepare CSV with columns: `email`, `first_name`, `last_name`, `phone`
2. Upload under "Upload CSV" tab
3. Emails are automatically matched to GHL contacts

### Generate & Send Tickets
1. Event → **Tickets**
2. Click **Generate QR Codes** (creates PNG images)
3. Click **Send Tickets** (triggers GHL workflow for each attendee)

### Event Day Check-In
1. Event → **Check-In Scanner** (open on phone or laptop at the door)
2. Click **Start Camera**
3. Point at QR code — check-in happens instantly
4. GHL contact is tagged automatically: `Attended - [Event Name]`
5. Use **Manual Search** if QR scan fails

### Attendance Report
1. Event → **Report**
2. View breakdown, timeline chart
3. **Export CSV** for records

---

## CSV Format

```csv
email,first_name,last_name,phone
jane@example.com,Jane,Smith,+44 7700 900000
john@example.com,John,Doe,
```

---

## Project Structure

```
smart-event-checkin/
├── app/
│   ├── main.py              # FastAPI app
│   ├── config.py            # Settings from .env
│   ├── database.py          # SQLite + SQLAlchemy setup
│   ├── models.py            # DB models
│   ├── schemas.py           # Pydantic schemas
│   ├── auth.py              # JWT authentication
│   ├── routers/             # API + page routes
│   │   ├── auth_router.py
│   │   ├── events_router.py
│   │   ├── attendees_router.py
│   │   ├── tickets_router.py
│   │   ├── checkin_router.py
│   │   ├── ghl_router.py
│   │   └── reports_router.py
│   ├── services/
│   │   ├── ghl_service.py   # GoHighLevel API client
│   │   └── qr_service.py    # QR code generation
│   └── templates/           # Jinja2 HTML templates
├── static/
│   └── qr_codes/            # Generated QR PNG files
├── requirements.txt
├── .env.example
├── Dockerfile
└── docker-compose.yml
```

---

## API Reference

All API endpoints require authentication (cookie-based session).

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/events` | List all events |
| POST | `/api/events` | Create event |
| GET | `/api/events/{id}/attendees` | List attendees |
| POST | `/api/events/{id}/import-ghl` | Import from GHL |
| POST | `/api/events/{id}/import-csv` | Import from CSV |
| POST | `/api/events/{id}/generate-tickets` | Generate QR codes |
| POST | `/api/events/{id}/send-tickets` | Send via GHL |
| POST | `/api/checkin/scan` | Process QR scan |
| POST | `/api/checkin/manual` | Manual check-in |
| GET | `/api/events/{id}/checkin/recent` | Recent check-ins |
| GET | `/api/events/{id}/stats` | Live stats |
| GET | `/api/events/{id}/export-csv` | Export attendance |
| GET | `/api/ghl/contacts` | Fetch GHL contacts |
| GET | `/api/ghl/test` | Test GHL connection |

Full interactive docs: **http://localhost:8000/api/docs**

---

## VPS Deployment

For a VPS/cloud deployment:

1. Set `BASE_URL=https://your-domain.com` in `.env`
2. Use a reverse proxy (nginx/Caddy) in front of the app
3. Enable HTTPS (required for camera access on mobile)

**nginx example:**
```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;
    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

> ⚠️ **HTTPS is required** for camera access (`getUserMedia`) on mobile browsers.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Camera won't start | Ensure HTTPS (not HTTP) on mobile; allow camera permission |
| GHL contacts not loading | Check `GHL_API_KEY` and `GHL_LOCATION_ID` in `.env` |
| Tickets not sending | Verify `ghl_workflow_id` is set on the event |
| QR codes not in email | Ensure `BASE_URL` is publicly accessible |
| Database locked | Stop other instances; use one worker (`--workers 1`) |

---

## Security Notes

- Change `SECRET_KEY` and `ADMIN_PASSWORD` before deploying
- Run behind HTTPS in production
- QR codes are served publicly (required for email images) — the ticket IDs are UUIDs and not guessable
- JWT tokens expire after 8 hours by default

---

*Built for rapid deployment — MVP ready in hours, not days.*
