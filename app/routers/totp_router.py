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
from sqlalchemy.orm import Session

from app.auth import decode_token, verify_password, get_password_hash
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
    token = request.cookies.get("access_token")
    if not token:
        return None
    username = decode_token(token)
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


# ── 2FA settings page ─────────────────────────────────────────

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
    user = _get_current_user(request, db)
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
    user = _get_current_user(request, db)
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

    user.totp_secret      = secret
    user.totp_enabled     = True
    user.totp_backup_codes = json.dumps(hashed)
    db.commit()

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
def twofa_validate(
    request: Request,
    payload: dict,
    db: Session = Depends(get_db),
):
    """
    Called from the login flow when 2FA is required.
    Expects: { "username": "...", "code": "..." }
    On success sets the access_token cookie.
    """
    from app.auth import create_access_token
    username = payload.get("username", "")
    code     = payload.get("code", "").strip().replace(" ", "")

    user = db.query(User).filter(User.username == username).first()
    if not user or not user.totp_enabled:
        raise HTTPException(400, "2FA not set up for this user")

    totp = pyotp.TOTP(user.totp_secret)
    valid = totp.verify(code, valid_window=1)

    # Check backup codes if TOTP fails
    if not valid and user.totp_backup_codes:
        hashed_list = json.loads(user.totp_backup_codes)
        if _check_backup_code(code, hashed_list):
            # Consume the backup code
            remaining = _remove_backup_code(code, hashed_list)
            user.totp_backup_codes = json.dumps(remaining)
            db.commit()
            valid = True

    if not valid:
        raise HTTPException(400, "Invalid code")

    token = create_access_token({"sub": username})
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        "access_token", token,
        httponly=True, samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response
