from datetime import timedelta

from fastapi import APIRouter, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.auth import verify_password, create_access_token, ACCESS_TOKEN_EXPIRE_HOURS

router = APIRouter(tags=["auth"])

LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Polymarket Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen flex items-center justify-center">
    <div class="bg-white p-8 rounded-lg shadow-md w-full max-w-md">
        <div class="text-center mb-8">
            <h1 class="text-2xl font-bold text-gray-800">Polymarket Dashboard</h1>
            <p class="text-gray-600">Sign in to continue</p>
        </div>

        {error_message}

        <form method="post" action="/auth/login" class="space-y-6">
            <div>
                <label for="username" class="block text-sm font-medium text-gray-700">Username</label>
                <input type="text" id="username" name="username" required
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500">
            </div>

            <div>
                <label for="password" class="block text-sm font-medium text-gray-700">Password</label>
                <input type="password" id="password" name="password" required
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500">
            </div>

            <button type="submit"
                class="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500">
                Sign in
            </button>
        </form>
    </div>
</body>
</html>
"""


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    """Render the login page."""
    error_html = ""
    if error:
        error_html = f"""
        <div class="mb-4 p-4 bg-red-50 border border-red-200 rounded-md">
            <p class="text-sm text-red-600">{error}</p>
        </div>
        """
    return LOGIN_PAGE.format(error_message=error_html)


@router.post("/auth/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Process login form submission."""
    settings = get_settings()

    # Verify credentials
    if username != settings.auth_username:
        return RedirectResponse(
            url="/login?error=Invalid+username+or+password",
            status_code=302,
        )

    if not verify_password(password, settings.auth_password_hash):
        return RedirectResponse(
            url="/login?error=Invalid+username+or+password",
            status_code=302,
        )

    # Create access token
    access_token = create_access_token(
        data={"sub": username},
        expires_delta=timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
    )

    # Set cookie and redirect to dashboard
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=True,  # Requires HTTPS in production
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
    )
    return response


@router.get("/auth/logout")
async def logout():
    """Log out by clearing the auth cookie."""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key="access_token")
    return response


@router.get("/auth/status")
async def auth_status(request: Request):
    """Check current authentication status (for API use)."""
    from app.auth import get_current_user
    user = await get_current_user(request)
    return {
        "authenticated": user is not None,
        "username": user,
    }
