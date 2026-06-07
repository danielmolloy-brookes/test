"""
Password policy enforcement.

Rules
-----
- Minimum 12 characters
- At least one uppercase letter
- At least one lowercase letter
- At least one digit
- At least one special character

Breach check
------------
Uses the HaveIBeenPwned Pwned Passwords API with k-anonymity:
  1. SHA-1 hash the password locally
  2. Send only the first 5 characters to the API
  3. Check if the remainder appears in the returned list
  Never sends the full password or hash to a third party.
"""

import hashlib
import re
import httpx
from typing import Optional


# ── Complexity rules ─────────────────────────────────────────

MIN_LENGTH    = 12
_RE_UPPER     = re.compile(r"[A-Z]")
_RE_LOWER     = re.compile(r"[a-z]")
_RE_DIGIT     = re.compile(r"\d")
_RE_SPECIAL   = re.compile(r"[^A-Za-z0-9]")


def check_complexity(password: str) -> Optional[str]:
    """
    Returns an error message string if the password fails policy,
    or None if it passes.
    """
    if len(password) < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters long."
    if not _RE_UPPER.search(password):
        return "Password must contain at least one uppercase letter."
    if not _RE_LOWER.search(password):
        return "Password must contain at least one lowercase letter."
    if not _RE_DIGIT.search(password):
        return "Password must contain at least one number."
    if not _RE_SPECIAL.search(password):
        return "Password must contain at least one special character (e.g. !@#$%^&*)."
    return None


# ── HaveIBeenPwned breach check ───────────────────────────────

HIBP_API = "https://api.pwnedpasswords.com/range/"
HIBP_TIMEOUT = 5  # seconds — fail open if the API is slow


def check_pwned(password: str) -> int:
    """
    Returns the number of times this password has appeared in known data
    breaches, or 0 if it hasn't (or if the API is unreachable).

    Uses k-anonymity: only the first 5 hex chars of the SHA-1 hash are
    sent to the API. The full hash never leaves this server.
    """
    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]

    try:
        resp = httpx.get(
            f"{HIBP_API}{prefix}",
            headers={"Add-Padding": "true"},
            timeout=HIBP_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception:
        # Fail open — don't block the user if HIBP is unreachable
        return 0

    for line in resp.text.splitlines():
        if ":" not in line:
            continue
        hash_suffix, count = line.split(":", 1)
        if hash_suffix.strip() == suffix:
            return int(count.strip())
    return 0


# ── Combined validator ────────────────────────────────────────

def validate_password(password: str) -> Optional[str]:
    """
    Run complexity check then breach check.
    Returns an error string if the password should be rejected, else None.

    Use this at every point a password is set (creation, reset, self-service change).
    """
    error = check_complexity(password)
    if error:
        return error

    pwned_count = check_pwned(password)
    if pwned_count > 0:
        return (
            f"This password has appeared in {pwned_count:,} known data breaches and cannot be used. "
            "Please choose a different password."
        )

    return None
