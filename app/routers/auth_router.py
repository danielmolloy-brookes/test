from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import create_access_token, decode_token, verify_password
from app.config import settings
from app.database import get_db
from app.models import User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("access_token")
    if token:
        username = decode_token(token)
        if username:
            user = db.query(User).filter(User.username == username).first()
            if user:
                # Valid session — send to the right landing page
                if user.is_developer:
                    return RedirectResponse(url="/admin/organisations", status_code=303)
                elif user.is_org_admin:
                    return RedirectResponse(url="/dashboard", status_code=303)
                else:
                    return RedirectResponse(url="/checkin-home", status_code=303)
        # Token present but invalid/expired — clear it and show login form
        resp = templates.TemplateResponse("login.html", {"request": request, "error": None})
        resp.delete_cookie("access_token")
        return resp
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid username or password"},
            status_code=401,
        )

    token = create_access_token(
        data={"sub": user.username},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    if user.is_developer:
        landing = "/admin/organisations"
    elif user.is_org_admin:
        landing = "/dashboard"
    else:
        landing = "/checkin-home"
    resp = RedirectResponse(url=landing, status_code=303)
    resp.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
    )
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("access_token")
    return resp


# ── API endpoint for token (for fetch() calls) ────────────────
@router.post("/api/auth/login")
async def api_login(
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(data={"sub": user.username})
    return {"access_token": token, "token_type": "bearer"}
