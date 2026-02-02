"""
Authentication module for the Polymarket Dashboard.

Uses JWT tokens stored in HTTP-only cookies for session management.
Password is verified against a bcrypt hash stored in environment variable.

To generate a password hash:
    python -c "from passlib.hash import bcrypt; print(bcrypt.hash('your-password'))"
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request, HTTPException, status, Depends
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from passlib.hash import bcrypt

from app.config import get_settings

# JWT settings
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24 * 7  # 1 week


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a bcrypt hash."""
    try:
        return bcrypt.verify(plain_password, hashed_password)
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    settings = get_settings()
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.jwt_secret_key, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def get_token_from_cookie(request: Request) -> Optional[str]:
    """Extract JWT token from cookie."""
    return request.cookies.get("access_token")


async def get_current_user(request: Request) -> Optional[str]:
    """
    Dependency that returns the current authenticated user.
    Returns None if not authenticated.
    """
    token = get_token_from_cookie(request)
    if not token:
        return None

    payload = decode_token(token)
    if not payload:
        return None

    username = payload.get("sub")
    return username


async def require_auth(request: Request) -> str:
    """
    Dependency that requires authentication.
    Raises 401 if not authenticated.
    """
    user = await get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def is_authenticated(request: Request) -> bool:
    """Check if the request has a valid auth token."""
    token = get_token_from_cookie(request)
    if not token:
        return False

    payload = decode_token(token)
    return payload is not None


class AuthMiddleware:
    """
    Middleware that redirects unauthenticated requests to login page.
    Excludes certain paths from auth requirement.
    """

    EXCLUDED_PATHS = {
        "/login",
        "/auth/login",
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
    }

    EXCLUDED_PREFIXES = (
        "/static/",
    )

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        # Check excluded paths
        if path in self.EXCLUDED_PATHS:
            await self.app(scope, receive, send)
            return

        # Check excluded prefixes
        if any(path.startswith(prefix) for prefix in self.EXCLUDED_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Check for valid token in cookies
        request = Request(scope, receive)
        if is_authenticated(request):
            await self.app(scope, receive, send)
            return

        # API requests get 401, browser requests get redirect
        accept = request.headers.get("accept", "")
        if "application/json" in accept or path.startswith("/api"):
            response = HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )
            # Return 401 JSON response
            from starlette.responses import JSONResponse
            response = JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"}
            )
            await response(scope, receive, send)
        else:
            # Redirect to login
            response = RedirectResponse(url="/login", status_code=302)
            await response(scope, receive, send)
