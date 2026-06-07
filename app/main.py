"""
Smart Event Check-In — FastAPI Application Entry Point
"""
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.database import init_db
from app.routers import (
    auth_router,
    events_router,
    attendees_router,
    tickets_router,
    checkin_router,
    ghl_router,
    reports_router,
    users_router,
    folders_router,
    orgs_router,
    mobile_router,
    mobile_admin_router,
    profile_router,
    booking_router,
)
from app.routers import totp_router

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Rate limiter ─────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

# ── FastAPI app ──────────────────────────────────────────────
app = FastAPI(
    title="Smart Event Check-In",
    description="Internal event check-in system with GoHighLevel integration",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

# ── Rate limit middleware ─────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── Security headers middleware ───────────────────────────────
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=()"
    # Allow CDN resources used in templates (Tailwind, Font Awesome, Alpine.js)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com https://unpkg.com; "
        "font-src 'self' https://cdnjs.cloudflare.com https://unpkg.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response


# ── CORS (mobile PWA) ────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:4173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files ─────────────────────────────────────────────
os.makedirs("static/qr_codes", exist_ok=True)
os.makedirs("static/maps", exist_ok=True)
os.makedirs("static/brand", exist_ok=True)
os.makedirs(settings.QR_CODE_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# If QR codes are stored outside /static (e.g. on a persistent volume),
# serve them at /qr_codes/ so URLs still work
if not settings.QR_CODE_DIR.startswith("static"):
    app.mount("/qr_codes", StaticFiles(directory=settings.QR_CODE_DIR), name="qr_codes")

# ── Templates ────────────────────────────────────────────────
templates = Jinja2Templates(directory="app/templates")

# ── Routers ──────────────────────────────────────────────────
app.include_router(auth_router.router)
app.include_router(users_router.router)
app.include_router(events_router.router)
app.include_router(attendees_router.router)
app.include_router(tickets_router.router)
app.include_router(checkin_router.router)
app.include_router(ghl_router.router)
app.include_router(reports_router.router)
app.include_router(folders_router.router)
app.include_router(orgs_router.router)
app.include_router(mobile_router.router)
app.include_router(mobile_admin_router.router)
app.include_router(booking_router.router)
app.include_router(totp_router.router)
# ⚠️  Must be last — /{ticket_id} is a catch-all path
app.include_router(profile_router.router)


# ── Root redirect ────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    from app.auth import decode_token
    from app.database import SessionLocal
    from app.models import User as UserModel
    token = request.cookies.get("access_token")
    if token:
        username = decode_token(token)
        if username:
            db = SessionLocal()
            try:
                user = db.query(UserModel).filter(UserModel.username == username).first()
                if user:
                    if user.is_developer:
                        landing = "/admin/organisations"
                    elif user.is_org_admin:
                        landing = "/dashboard"
                    else:
                        landing = "/checkin-home"
                    return RedirectResponse(url=landing, status_code=302)
            finally:
                db.close()
    return RedirectResponse(url="/login", status_code=302)


# ── Health check ─────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "app": "Smart Event Check-In"}


# ── Startup ──────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Smart Event Check-In starting up…")
    init_db()
    logger.info(f"📁 QR codes directory: {settings.QR_CODE_DIR}")
    logger.info(f"🌐 Base URL: {settings.BASE_URL}")
    ghl_configured = bool(settings.GHL_API_KEY and settings.GHL_LOCATION_ID)
    if ghl_configured:
        logger.info("✅ GHL API configured")
    else:
        logger.warning("⚠️  GHL_API_KEY or GHL_LOCATION_ID not set — GHL features disabled")
    logger.info(f"🔐 Admin user: {settings.ADMIN_USERNAME}")
    logger.info("✅ Application ready!")
