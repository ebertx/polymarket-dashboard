# app/routers/docs.py
"""Markdown viewer for polymarket-team analysis docs.

/docs           — browsable index, dashboard session required.
/docs/{path}    — rendered markdown; dashboard session OR a valid per-path
                  HMAC signature (?k=...) as sent in Kryten Telegram links.
Invalid or non-allowlisted paths return 404 (no allowlist oracle).
"""

import markdown as md_lib
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.auth import is_authenticated
from app.config import get_settings
from app.services.docs_access import normalize_path, verify_signature
from app.services.github_docs import DocNotFound, DocsFetchError, fetch_markdown, fetch_tree

router = APIRouter(tags=["docs"])

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
body{{background:#111;color:#ddd;font-family:-apple-system,system-ui,sans-serif;
max-width:52rem;margin:0 auto;padding:1.5rem;line-height:1.55}}
a{{color:#7ab7ff}}code,pre{{background:#1d1d1d;border-radius:4px;padding:2px 5px}}
pre{{padding:12px;overflow-x:auto}}pre code{{padding:0}}
table{{border-collapse:collapse;display:block;overflow-x:auto}}
td,th{{border:1px solid #333;padding:4px 8px}}h1,h2,h3{{color:#fff}}
blockquote{{border-left:3px solid #444;margin-left:0;padding-left:1rem;color:#aaa}}
</style></head><body><p style="color:#666;font-size:0.8rem">{path}</p>{body}</body></html>"""


@router.get("/docs", response_class=HTMLResponse)
async def docs_index(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login")
    try:
        paths = await fetch_tree()
    except DocsFetchError as exc:
        return PlainTextResponse(f"upstream error: {exc}", status_code=502)
    items = "".join(f'<li><a href="/docs/{p}">{p}</a></li>' for p in paths)
    return HTMLResponse(_PAGE.format(
        title="Docs", path="index", body=f"<h1>Docs</h1><ul>{items}</ul>"))


@router.get("/docs/{doc_path:path}")
async def docs_view(doc_path: str, request: Request, k: str = "", raw: int = 0):
    path = normalize_path(doc_path)
    if path is None:
        return PlainTextResponse("not found", status_code=404)
    settings = get_settings()
    if not (is_authenticated(request) or verify_signature(settings.docs_link_secret, path, k)):
        return RedirectResponse("/login")
    try:
        text = await fetch_markdown(path)
    except DocNotFound:
        return PlainTextResponse("not found", status_code=404)
    except DocsFetchError as exc:
        return PlainTextResponse(f"upstream error: {exc}", status_code=502)
    if raw:
        return PlainTextResponse(text)
    body = md_lib.markdown(text, extensions=["tables", "fenced_code"])
    return HTMLResponse(_PAGE.format(title=path.rsplit("/", 1)[-1], path=path, body=body))
