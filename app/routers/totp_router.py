"""
Two-factor authentication (TOTP) router.

GET  /account/2fa          — 2FA status + setup page
POST /account/2fa/setup    — generate secret + show QR
POST /account/2fa/verify   — confirm code and enable 2FA
POST /account/2fa/disable  — disable 2FA (requires password)
POST /account/2fa/validate — called during login to verify code
"""

import io
import json
import os
import secrets

import pyotp
import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

limiter = Limiter(key_func=get_remote_address)

from app.auth import decode_token, decode_pending_token, create_access_token, verify_password, get_password_hash
from app.config import settings
from app.database import get_db, SessionLocal
from app.models import User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

APP_NAME = "Smart Event Check-In"


# ── Change password ───────────────────────────────────────────

@router.get("/account/password", response_class=HTMLResponse)
def change_password_page(request: Request, db: Session = Depends(get_db)):
    user = _get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("account/password.html", {"request": request, "user": user})


@router.post("/api/account/change-password")
def change_password(
    request: Request,
    payload: dict,
    db: Session = Depends(get_db),
):
    user = _get_current_user(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated")

    current  = payload.get("current_password", "")
    new_pass = payload.get("new_password", "")

    if not verify_password(current, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    if len(new_pass) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")

    user.password_hash = get_password_hash(new_pass)
    db.commit()
    return {"status": "ok"}


def _get_current_user(request: Request, db: Session) -> User | None:
    """Resolve user from access_token (full session) cookie."""
    token = request.cookies.get("access_token")
    if not token:
        return None
    username = decode_token(token)
    if not username:
        return None
    return db.query(User).filter(User.username == username).first()


def _get_pending_user(request: Request, db: Session) -> User | None:
    """Resolve user from pending_2fa cookie (post-password, pre-2FA)."""
    token = request.cookies.get("pending_2fa")
    if not token:
        return None
    username = decode_pending_token(token)
    if not username:
        return None
    return db.query(User).filter(User.username == username).first()


def _generate_backup_codes() -> list[str]:
    """Generate 8 plain-text backup codes."""
    return [secrets.token_hex(4).upper() for _ in range(8)]


def _hash_backup_codes(codes: list[str]) -> list[str]:
    """Hash backup codes for storage."""
    import hashlib
    return [hashlib.sha256(c.encode()).hexdigest() for c in codes]


def _check_backup_code(plain: str, hashed_list: list[str]) -> bool:
    import hashlib
    h = hashlib.sha256(plain.strip().upper().encode()).hexdigest()
    return h in hashed_list


def _remove_backup_code(plain: str, hashed_list: list[str]) -> list[str]:
    import hashlib
    h = hashlib.sha256(plain.strip().upper().encode()).hexdigest()
    return [c for c in hashed_list if c != h]


# ── 2FA gate — shown after every password login ───────────────

@router.get("/account/2fa-gate", response_class=HTMLResponse)
def twofa_gate(request: Request, db: Session = Depends(get_db)):
    """
    Entry point after password login.
    - If user has 2FA set up → show code entry
    - If not → show forced setup
    """
    user = _get_pending_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("account/2fa_gate.html", {
        "request": request,
        "totp_enabled": user.totp_enabled,
        "username": user.username,
    })


# ── 2FA settings page (for already-logged-in users) ───────────

@router.get("/account/2fa", response_class=HTMLResponse)
def twofa_page(request: Request, db: Session = Depends(get_db)):
    user = _get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("account/2fa.html", {
        "request": request,
        "user": user,
        "totp_enabled": user.totp_enabled,
    })


# ── Generate secret + QR ─────────────────────────────────────

@router.post("/account/2fa/setup")
def twofa_setup(request: Request, db: Session = Depends(get_db)):
    user = _get_current_user(request, db) or _get_pending_user(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated")

    # Generate a new secret (don't save yet — user must verify first)
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=user.username, issuer_name=APP_NAME)

    # Generate QR code as base64 PNG
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    import base64
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return JSONResponse({
        "secret": secret,
        "qr_data_url": f"data:image/png;base64,{qr_b64}",
    })


# ── Verify code and enable ────────────────────────────────────

@router.post("/account/2fa/verify")
def twofa_verify(
    request: Request,
    payload: dict,
    db: Session = Depends(get_db),
):
    user = _get_current_user(request, db) or _get_pending_user(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated")

    secret = payload.get("secret", "")
    code   = payload.get("code", "").strip().replace(" ", "")

    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=1):
        raise HTTPException(400, "Invalid code — please try again")

    # Generate backup codes
    plain_codes = _generate_backup_codes()
    hashed      = _hash_backup_codes(plain_codes)

    user.totp_secret       = secret
    user.totp_enabled      = True
    user.totp_backup_codes = json.dumps(hashed)
    db.commit()

    # If coming from the gate flow (pending token), also issue the real session token
    pending_user = _get_pending_user(request, db)
    if pending_user and pending_user.id == user.id:
        token = create_access_token({"sub": user.username})
        resp  = JSONResponse({"status": "enabled", "backup_codes": plain_codes, "redirect": "/"})
        resp.set_cookie("access_token", token, httponly=True, samesite="lax",
                        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
        resp.delete_cookie("pending_2fa")
        return resp

    return JSONResponse({"status": "enabled", "backup_codes": plain_codes})


# ── Disable 2FA ───────────────────────────────────────────────

@router.post("/account/2fa/disable")
def twofa_disable(
    request: Request,
    payload: dict,
    db: Session = Depends(get_db),
):
    user = _get_current_user(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated")

    password = payload.get("password", "")
    if not verify_password(password, user.password_hash):
        raise HTTPException(400, "Incorrect password")

    user.totp_enabled     = False
    user.totp_secret      = None
    user.totp_backup_codes = None
    db.commit()

    return JSONResponse({"status": "disabled"})


# ── Validate code during login ────────────────────────────────

@router.post("/account/2fa/validate")
@limiter.limit("10/minute")
def twofa_validate(
    request: Request,
    payload: dict,
    db: Session = Depends(get_db),
):
    """
    Called from the gate page after password login.
    Reads username from the pending_2fa cookie (not from payload).
    On success: issues real access_token, clears pending_2fa.
    """
    user = _get_pending_user(request, db)
    if not user:
        raise HTTPException(401, "Session expired — please log in again")
    if not user.totp_enabled:
        raise HTTPException(400, "2FA not set up")

    code = payload.get("code", "").strip().replace(" ", "")

    totp  = pyotp.TOTP(user.totp_secret)
    valid = totp.verify(code, valid_window=1)

    # Check backup codes if TOTP fails
    if not valid and user.totp_backup_codes:
        hashed_list = json.loads(user.totp_backup_codes)
        if _check_backup_code(code, hashed_list):
            remaining = _remove_backup_code(code, hashed_list)
            user.totp_backup_codes = json.dumps(remaining)
            db.commit()
            valid = True

    if not valid:
        raise HTTPException(400, "Invalid code")

    token = create_access_token({"sub": user.username})
    resp  = JSONResponse({"status": "ok"})
    resp.set_cookie("access_token", token, httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 30)
    resp.delete_cookie("pending_2fa")
    return resp
