from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from app.database.connection import files_col, users_col, logs_col, settings
from app.admin.auth import (
    clear_session_cookie,
    create_session_token,
    is_admin,
    password_is_set,
    set_session_cookie,
    verify_password,
)
import psutil
import time

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")

_login_attempts: dict[str, list[float]] = {}
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 300  # seconds


def _too_many_attempts(ip: str) -> bool:
    now = time.time()
    tries = [t for t in _login_attempts.get(ip, []) if now - t < ATTEMPT_WINDOW]
    _login_attempts[ip] = tries
    return len(tries) >= MAX_ATTEMPTS


def _record_attempt(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())


def _guard(request: Request):
    """Redirect browsers to the login page when not authenticated."""
    if not is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


@router.get("/login")
async def admin_login_page(request: Request):
    if is_admin(request):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(
        "admin/login.html",
        {"request": request, "error": None, "configured": password_is_set()},
    )


@router.post("/login")
async def admin_login(request: Request, password: str = Form(...)):
    ip = request.client.host if request.client else "unknown"

    if not password_is_set():
        return templates.TemplateResponse(
            "admin/login.html",
            {
                "request": request,
                "error": "ADMIN_PASSWORD is not set in the environment.",
                "configured": False,
            },
            status_code=503,
        )

    if _too_many_attempts(ip):
        return templates.TemplateResponse(
            "admin/login.html",
            {
                "request": request,
                "error": "Too many attempts. Try again in a few minutes.",
                "configured": True,
            },
            status_code=429,
        )

    if not verify_password(password):
        _record_attempt(ip)
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Incorrect password.", "configured": True},
            status_code=401,
        )

    _login_attempts.pop(ip, None)
    response = RedirectResponse(url="/admin", status_code=303)
    set_session_cookie(response, create_session_token())
    return response


@router.get("/logout")
@router.post("/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    clear_session_cookie(response)
    return response


@router.get("/")
async def admin_dashboard(request: Request):
    redirect = _guard(request)
    if redirect:
        return redirect

    total_users = await users_col.count_documents({})
    total_files = await files_col.count_documents({})

    # System Stats
    cpu_usage = psutil.cpu_percent()
    ram_usage = psutil.virtual_memory().percent

    return templates.TemplateResponse("admin/dashboard.html", {
        "request": request,
        "total_users": total_users,
        "total_files": total_files,
        "cpu": cpu_usage,
        "ram": ram_usage
    })


@router.get("/users")
async def admin_users(request: Request):
    redirect = _guard(request)
    if redirect:
        return redirect

    users = await users_col.find().to_list(100)
    return templates.TemplateResponse("admin/users.html", {"request": request, "users": users})


@router.get("/files")
async def admin_files(request: Request, q: str = None):
    redirect = _guard(request)
    if redirect:
        return redirect

    query = {}
    if q:
        query = {"filename": {"$regex": q, "$options": "i"}}

    files = await files_col.find(query).sort("created_at", -1).to_list(100)
    return templates.TemplateResponse("admin/files.html", {"request": request, "files": files, "query": q})


@router.post("/files/delete/{short_code}")
async def delete_file(request: Request, short_code: str):
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="Admin login required")

    await files_col.delete_one({"short_code": short_code})
    return {"status": "success"}
