"""Login for the web app: one account from the environment and an HMAC-signed session cookie.

    APP_LOGIN_EMAIL, APP_LOGIN_PASSWORD   the account (required)
    APP_SECRET_KEY                        signs session cookies; set it so logins survive restarts
"""

import base64
import hashlib
import hmac
import html
import os
import secrets
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional
from urllib.parse import quote

COOKIE = "sg_session"
SESSION_S = 12 * 3600          # stay signed in for 12 hours
MAX_ATTEMPTS, WINDOW_S = 10, 300  # at most 10 login attempts per 5 minutes per client


class Auth:
    def __init__(self, email: str, password: str, secret: str):
        self.email = email.strip().lower()
        self.password = password
        self.secret = secret.encode()
        self.attempts: Dict[str, Deque[float]] = defaultdict(deque)

    @classmethod
    def from_env(cls) -> "Auth":
        email, password = os.environ.get("APP_LOGIN_EMAIL"), os.environ.get("APP_LOGIN_PASSWORD")
        if not email or not password:
            raise RuntimeError("Set APP_LOGIN_EMAIL and APP_LOGIN_PASSWORD (e.g. in .env)")
        # Without a fixed key every restart signs everyone out, which is safe but inconvenient.
        return cls(email, password, os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32))

    def check_credentials(self, email: str, password: str) -> bool:
        # compare both in constant time so a wrong email and a wrong password look the same
        ok_email = hmac.compare_digest(email.strip().lower().encode(), self.email.encode())
        ok_password = hmac.compare_digest(password.encode(), self.password.encode())
        return ok_email and ok_password

    def allow_attempt(self, client: str) -> bool:
        now, window = time.time(), self.attempts[client]
        while window and window[0] < now - WINDOW_S:
            window.popleft()
        if len(window) >= MAX_ATTEMPTS:
            return False
        window.append(now)
        return True

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, email: str) -> str:
        payload = f"{email.strip().lower()}|{int(time.time()) + SESSION_S}"
        encoded = base64.urlsafe_b64encode(payload.encode()).decode()
        return f"{encoded}.{self._sign(payload)}"

    def verify(self, token: Optional[str]) -> Optional[str]:
        """The signed-in email, or None for a missing, forged or expired session."""
        if not token or "." not in token:
            return None
        encoded, signature = token.rsplit(".", 1)
        try:
            payload = base64.urlsafe_b64decode(encoded.encode()).decode()
            email, expiry = payload.rsplit("|", 1)
            if not hmac.compare_digest(signature, self._sign(payload)) or int(expiry) < time.time():
                return None
        except (ValueError, UnicodeDecodeError):
            return None
        return email if hmac.compare_digest(email.encode(), self.email.encode()) else None


def safe_next(target: Optional[str]) -> str:
    """Only redirect back to a path on this site (no //evil.example or https://…)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def login_url(path: str) -> str:
    return "/login" if path in ("", "/") else f"/login?next={quote(path)}"


def login_page(error: str = "", next_path: str = "/", email: str = "") -> str:
    """Self-contained (static files are behind the login too)."""
    alert = f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · Stream Graph</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📡</text></svg>">
<style>
  :root {{ --bg:#f4f4f6; --panel:#fff; --text:#1f2330; --muted:#6b7080; --border:#dcdce2; --accent:#ff6d5a; --down:#dc2626; color-scheme: light; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#121318; --panel:#22242d; --text:#e8e9ee; --muted:#9a9fb0; --border:#353846; color-scheme: dark; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:16px; background:var(--bg); color:var(--text);
         font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
         background-image: radial-gradient(var(--border) 1.2px, transparent 1.2px); background-size: 22px 22px; }}
  main {{ width:min(380px, 100%); background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:28px 26px;
          box-shadow: 0 1px 3px rgba(20,20,40,.08), 0 12px 32px rgba(20,20,40,.10); }}
  .brand {{ display:flex; align-items:center; gap:10px; font-weight:700; font-size:18px; margin-bottom:4px; }}
  p.sub {{ color:var(--muted); margin:0 0 20px; }}
  label {{ display:block; font-weight:600; font-size:12.5px; margin:14px 0 6px; }}
  input {{ width:100%; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:var(--bg); color:var(--text); font:inherit; }}
  input:focus {{ outline:2px solid var(--accent); outline-offset:1px; border-color:transparent; }}
  button {{ width:100%; margin-top:22px; padding:11px; border:0; border-radius:8px; background:var(--accent); color:#fff; font:inherit; font-weight:700; cursor:pointer; }}
  button:hover {{ filter:brightness(1.05); }}
  .error {{ margin-top:14px; padding:9px 12px; border-radius:8px; color:var(--down); background:color-mix(in srgb, var(--down) 12%, transparent); font-size:13px; }}
</style></head>
<body><main>
  <div class="brand"><span>📡</span>Stream Graph</div>
  <p class="sub">Sign in to monitor your streams.</p>
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{html.escape(next_path)}">
    <label for="email">Email</label>
    <input id="email" name="email" type="email" autocomplete="username" required autofocus value="{html.escape(email)}">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    {alert}
    <button type="submit">Sign in</button>
  </form>
</main></body></html>"""
