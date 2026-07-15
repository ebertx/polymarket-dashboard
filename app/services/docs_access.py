"""Path validation and HMAC link signatures for the /docs markdown viewer.

The signature scheme must match ops/kryten/hermes-scripts/doc_link.py in the
polymarket-team repo exactly (shared test vector in both test suites).
"""

import hashlib
import hmac

ALLOWED_TOP_DIRS = {"markets", "portfolio", "briefings", "research", "retrospectives", "docs"}


def normalize_path(path: str) -> str | None:
    """Return the normalized repo-relative path, or None if not allowed."""
    if not path or path.startswith("/") or "\\" in path:
        return None
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if parts[0] not in ALLOWED_TOP_DIRS:
        return None
    if not parts[-1].endswith(".md"):
        return None
    return "/".join(parts)


def sign_path(secret: str, path: str) -> str:
    return hmac.new(secret.encode(), path.encode(), hashlib.sha256).hexdigest()[:32]


def verify_signature(secret: str, path: str, sig: str) -> bool:
    if not secret or not sig:
        return False
    return hmac.compare_digest(sign_path(secret, path), sig)
