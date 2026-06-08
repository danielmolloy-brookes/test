"""
CSRF protection — Double Submit Cookie pattern.

On GET /login:  generate a token, set it as a cookie AND embed it in the form.
On POST /login: verify the form field value matches the cookie value.

The cookie is HttpOnly so JS cannot read it, but the server compares the two
values it placed: one in the signed cookie, one in the hidden form field.
A cross-origin attacker can POST the form but cannot read or set our
SameSite=Strict cookie, so the values will never match.
"""
import secrets
import hashlib
import hmac

from fastapi import Request, HTTPException

_TOKEN_BYTES = 32
COOKIE_NAME  = "csrf_token"


def generate_csrf_token() -> str:
    """Return a cryptographically random hex token."""
    return secrets.token_hex(_TOKEN_BYTES)


def get_csrf_token(request: Request) -> str:
    """
    Return the CSRF token from the cookie, or create one if absent.
    Call this in GET handlers; pass the returned value to the template.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) != _TOKEN_BYTES * 2:
        token = generate_csrf_token()
    return token


def validate_csrf(request: Request, form_token: str | None) -> None:
    """
    Validate that the form-submitted token matches the cookie token.
    Raises HTTP 403 on failure.
    """
    cookie_token = request.cookies.get(COOKIE_NAME)
    if not cookie_token or not form_token:
        raise HTTPException(status_code=403, detail="CSRF token missing")
    # Constant-time comparison
    if not hmac.compare_digest(cookie_token, form_token):
        raise HTTPException(status_code=403, detail="CSRF token invalid")
