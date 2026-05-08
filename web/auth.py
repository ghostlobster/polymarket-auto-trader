"""
OAuth 2.0 integration for the web UI.

Providers: Google (OpenID Connect) and GitHub.
Sessions are stored in signed cookies via Starlette's SessionMiddleware.
Access control is governed by the OAUTH_ALLOWED_EMAILS setting — only emails
listed there are approved on first login. Manually approved rows (is_allowed=1
set directly in the DB) are preserved across re-logins by the MAX() upsert.
"""
from pathlib import Path

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"


class NeedsLogin(Exception):
    """Raised by require_auth; caught by a registered exception handler."""
    def __init__(self, url: str = "/login"):
        self.url = url


def _allowed_email_set(settings) -> set[str]:
    raw = (settings.oauth_allowed_emails or "").strip()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def configure_oauth(settings) -> OAuth:
    oauth = OAuth()
    if settings.google_client_id:
        oauth.register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url=(
                "https://accounts.google.com/.well-known/openid-configuration"
            ),
            client_kwargs={"scope": "openid email profile"},
        )
    if settings.github_client_id:
        oauth.register(
            name="github",
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret,
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user user:email"},
        )
    return oauth


def make_auth_router(db, settings, oauth: OAuth) -> APIRouter:
    router = APIRouter()
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    allowed = _allowed_email_set(settings)

    @router.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login(request: Request):
        denied = request.query_params.get("denied")
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "has_google": bool(settings.google_client_id),
                "has_github": bool(settings.github_client_id),
                "denied": denied,
                "current_user": None,
            },
        )

    @router.get("/auth/google", include_in_schema=False)
    async def auth_google(request: Request):
        redirect_uri = settings.oauth_base_url.rstrip("/") + "/auth/google/callback"
        return await oauth.google.authorize_redirect(request, redirect_uri)

    @router.get("/auth/google/callback", include_in_schema=False)
    async def auth_google_callback(request: Request):
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get("userinfo") or await oauth.google.userinfo(token=token)
        email = (user_info.get("email") or "").lower()
        is_allowed = 1 if email in allowed else 0
        user_id, effective_allowed = await db.upsert_web_user(
            "google",
            user_info["sub"],
            email,
            user_info.get("name"),
            user_info.get("picture"),
            is_allowed,
        )
        if not effective_allowed:
            return RedirectResponse("/login?denied=1", status_code=303)
        request.session["user_id"] = user_id
        return RedirectResponse("/profiles", status_code=303)

    @router.get("/auth/github", include_in_schema=False)
    async def auth_github(request: Request):
        redirect_uri = settings.oauth_base_url.rstrip("/") + "/auth/github/callback"
        return await oauth.github.authorize_redirect(request, redirect_uri)

    @router.get("/auth/github/callback", include_in_schema=False)
    async def auth_github_callback(request: Request):
        token = await oauth.github.authorize_access_token(request)
        resp = await oauth.github.get("user", token=token)
        resp.raise_for_status()
        profile = resp.json()
        email = profile.get("email")
        if not email:
            emails_resp = await oauth.github.get("user/emails", token=token)
            for entry in emails_resp.json():
                if entry.get("primary") and entry.get("verified"):
                    email = entry["email"]
                    break
        email = (email or "").lower()
        is_allowed = 1 if email in allowed else 0
        user_id, effective_allowed = await db.upsert_web_user(
            "github",
            str(profile["id"]),
            email,
            profile.get("name") or profile.get("login"),
            profile.get("avatar_url"),
            is_allowed,
        )
        if not effective_allowed:
            return RedirectResponse("/login?denied=1", status_code=303)
        request.session["user_id"] = user_id
        return RedirectResponse("/profiles", status_code=303)

    @router.get("/logout", include_in_schema=False)
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    return router


async def require_auth(request: Request):
    """FastAPI dependency — raises NeedsLogin if the session has no valid allowed user."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise NeedsLogin()
    db = request.app.state.db
    user = await db.get_web_user_by_id(user_id)
    if not user or not user["is_allowed"]:
        request.session.clear()
        raise NeedsLogin("/login?denied=1")
    return user
