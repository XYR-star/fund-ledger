from typing import Optional

from fastapi import Request
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware

from .config import settings


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def add_session_middleware(app) -> None:
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=False,
        max_age=60 * 60 * 12,
    )


def verify_login(username: str, password: str) -> bool:
    if username != settings.username:
        return False
    return pwd_context.verify(password, settings.password_hash)


def current_user(request: Request) -> Optional[str]:
    return request.session.get("user")


def login_user(request: Request, username: str) -> None:
    request.session["user"] = username


def logout_user(request: Request) -> None:
    request.session.clear()
