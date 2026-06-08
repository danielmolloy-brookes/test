from datetime import datetime, timedelta
from typing import Optional
import uuid

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
import warnings
import bcrypt as _bcrypt
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, SessionLocal
from app.models import User, RevokedToken

ALGORITHM = "HS256"

# Use bcrypt directly to avoid passlib/bcrypt version conflicts
def get_password_hash(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ── JWT helpers ──────────────────────────────────────────────
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire, "jti": str(uuid.uuid4())})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)


def create_pending_token(username: str) -> str:
    """Short-lived token issued after password check, before 2FA is verified."""
    expire = datetime.utcnow() + timedelta(minutes=10)
    return jwt.encode(
        {"sub": username, "type": "pending_2fa", "exp": expire},
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )


def decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        # Reject pending tokens — they must not grant full access
        if payload.get("type") == "pending_2fa":
            return None
        # Check revocation list
        jti = payload.get("jti")
        if jti:
            db = SessionLocal()
            try:
                if db.query(RevokedToken).filter(RevokedToken.jti == jti).first():
                    return None
            finally:
                db.close()
        return payload.get("sub")
    except JWTError:
        return None


def revoke_token(token: str) -> None:
    """Add a token to the revocation list so it cannot be used again."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        jti = payload.get("jti")
        exp = payload.get("exp")
        if not jti or not exp:
            return
        expires_at = datetime.utcfromtimestamp(exp)
        db = SessionLocal()
        try:
            # Avoid duplicate if already revoked
            if not db.query(RevokedToken).filter(RevokedToken.jti == jti).first():
                db.add(RevokedToken(jti=jti, expires_at=expires_at))
                db.commit()
            # Clean up expired tokens while we're here
            db.query(RevokedToken).filter(RevokedToken.expires_at < datetime.utcnow()).delete()
            db.commit()
        finally:
            db.close()
    except JWTError:
        pass


def decode_pending_token(token: str) -> Optional[str]:
    """Decode a pending_2fa token — returns username or None."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "pending_2fa":
            return None
        return payload.get("sub")
    except JWTError:
        return None


# ── FastAPI dependencies ─────────────────────────────────────
def get_current_user_from_cookie(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    username = decode_token(token)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


def get_current_user_api(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """For JSON API endpoints — returns 401 instead of redirect."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = decode_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def _login_redirect() -> RedirectResponse:
    """Redirect to /login and clear any stale token cookie to prevent redirect loops."""
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("access_token")
    return resp


def _2fa_setup_redirect() -> RedirectResponse:
    """Redirect to 2FA setup when user hasn't configured it yet."""
    return RedirectResponse(url="/account/2fa-gate", status_code=303)


def require_login(request: Request, db: Session = Depends(get_db)) -> User:
    """Page dependency — redirects to /login on failure, /account/2fa-gate if 2FA not set up."""
    try:
        user = get_current_user_from_cookie(request, db)
    except HTTPException:
        return _login_redirect()
    if not user.totp_enabled:
        return _2fa_setup_redirect()
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    """Page dependency — admin or developer role required. Redirects others to check-in home."""
    try:
        user = get_current_user_from_cookie(request, db)
    except HTTPException:
        return _login_redirect()
    if not user.totp_enabled:
        return _2fa_setup_redirect()
    if not user.is_admin:
        return RedirectResponse(url="/checkin-home", status_code=303)
    return user


def require_developer(request: Request, db: Session = Depends(get_db)) -> User:
    """Page dependency — developer role only. Redirects non-developers to /dashboard."""
    try:
        user = get_current_user_from_cookie(request, db)
    except HTTPException:
        return _login_redirect()
    if not user.totp_enabled:
        return _2fa_setup_redirect()
    if not user.is_developer:
        return RedirectResponse(url="/dashboard", status_code=303)
    return user


def require_admin_api(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """API dependency — admin or developer role required. Returns 403 for others."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = decode_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.totp_enabled:
        raise HTTPException(status_code=403, detail="2FA setup required")
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_developer_api(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """API dependency — developer role only. Returns 403 for non-developers."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = decode_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.totp_enabled:
        raise HTTPException(status_code=403, detail="2FA setup required")
    if not user.is_developer:
        raise HTTPException(status_code=403, detail="Developer access required")
    return user


def check_event_access(user: User, event_id: int, db: Session) -> bool:
    """Returns True if the user may scan/check-in for this event."""
    if user.is_developer:
        return True
    from app.models import Event, EventPermission
    if user.is_org_admin:
        # Org admin can access any event in their org
        return db.query(Event).filter(
            Event.id == event_id,
            Event.org_id == user.org_id,
        ).first() is not None
    # checkin: must have explicit EventPermission AND event must be in same org
    return (
        db.query(EventPermission)
        .join(Event, Event.id == EventPermission.event_id)
        .filter(
            EventPermission.user_id == user.id,
            EventPermission.event_id == event_id,
            Event.org_id == user.org_id,
        )
        .first()
    ) is not None


def org_filter(query, user, model):
    """Apply org scoping to a query. Developers see everything; others see only their org."""
    if user.is_developer:
        return query
    return query.filter(model.org_id == user.org_id)
