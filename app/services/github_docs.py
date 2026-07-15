"""Fetch polymarket-team markdown from the GitHub contents API, with a
60-second in-memory cache. GitHub is the sync point: Kryten commits and
pushes its outputs, so no repo clone or volume is needed here."""

import time

import aiohttp

from app.config import get_settings
from app.services.docs_access import ALLOWED_TOP_DIRS

API_BASE = "https://api.github.com/repos/ebertx/polymarket-team"
CACHE_TTL = 60.0
_content_cache: dict[str, tuple[float, str]] = {}
_tree_cache: dict[str, tuple[float, list]] = {}


class DocsFetchError(Exception):
    """GitHub API unreachable or returned an unexpected status."""


class DocNotFound(Exception):
    """The requested path does not exist in the repo."""


def _cache_get(cache: dict, key: str, now: float, ttl: float):
    hit = cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(cache: dict, key: str, now: float, value) -> None:
    cache[key] = (now, value)


def _headers() -> dict:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.github_docs_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def fetch_markdown(path: str) -> str:
    now = time.monotonic()
    cached = _cache_get(_content_cache, path, now, CACHE_TTL)
    if cached is not None:
        return cached
    url = f"{API_BASE}/contents/{path}?ref=main"
    headers = {**_headers(), "Accept": "application/vnd.github.raw+json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 404:
                    raise DocNotFound(path)
                if resp.status != 200:
                    raise DocsFetchError(f"GitHub returned {resp.status} for {path}")
                text = await resp.text()
    except aiohttp.ClientError as exc:
        raise DocsFetchError(str(exc)) from exc
    _cache_put(_content_cache, path, now, text)
    return text


async def fetch_tree() -> list[str]:
    """All allowlisted .md paths in the repo (for the /docs index)."""
    now = time.monotonic()
    cached = _cache_get(_tree_cache, "tree", now, CACHE_TTL)
    if cached is not None:
        return cached
    url = f"{API_BASE}/git/trees/main?recursive=1"
    headers = {**_headers(), "Accept": "application/vnd.github+json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise DocsFetchError(f"GitHub returned {resp.status} for tree")
                data = await resp.json()
    except aiohttp.ClientError as exc:
        raise DocsFetchError(str(exc)) from exc
    paths = sorted(
        item["path"] for item in data.get("tree", [])
        if item.get("type") == "blob"
        and item["path"].endswith(".md")
        and item["path"].split("/")[0] in ALLOWED_TOP_DIRS
    )
    _cache_put(_tree_cache, "tree", now, paths)
    return paths
