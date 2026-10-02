"""Pages routes (split out of server.py). Shared state and helpers are read from the server
module at call time as `srv.<name>`, so there is one copy of each and tests can patch them there."""

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from urllib.parse import parse_qs
import re

import server as srv

router = APIRouter()


@router.get("/healthz")
def healthz():
    """For Docker's health check; says nothing about the data."""
    return {"ok": True}


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    if srv.auth.verify(request.cookies.get(srv.COOKIE)):
        return RedirectResponse(srv.safe_next(next), status_code=303)
    return HTMLResponse(srv.login_page(next_path=srv.safe_next(next)), headers={"Cache-Control": "no-store"})


@router.post("/login")
async def login_submit(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items()}
    email, password, next_path = form.get("email", ""), form.get("password", ""), srv.safe_next(form.get("next"))
    client = request.client.host if request.client else "?"
    if not srv.auth.allow_attempt(client):
        return HTMLResponse(srv.login_page("Too many attempts. Wait a few minutes and try again.", next_path, email), status_code=429)
    if not srv.auth.check_credentials(email, password):
        return HTMLResponse(srv.login_page("Wrong email or password.", next_path, email), status_code=401)
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(srv.COOKIE, srv.auth.issue(email), max_age=srv.SESSION_S, httponly=True, samesite="lax", path="/")
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(srv.COOKIE, path="/")
    return response


@router.get("/")
@router.get("/servers")
@router.get("/spiders")
@router.get("/links")
@router.get("/relationships")
@router.get("/agent")
@router.get("/agent/tools")
@router.get("/agent/skills")
@router.get("/learning")
@router.get("/notifications")
@router.get("/notifications/email")
@router.get("/notifications/whatsapp")
@router.get("/notifications/sms")
def index():
    """index.html with each asset URL stamped by its modification time, so browsers never run stale CSS/JS."""
    html = (srv.WEB / "index.html").read_text()
    html = re.sub(
        r'/static/([\w.-]+\.(?:css|js))(\?v=[^"]*)?',
        lambda m: f"/static/{m.group(1)}?v={int((srv.WEB / m.group(1)).stat().st_mtime)}",
        html,
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
