"""Tests for own_positions — the proxyWallet guard on Data API ingestion.

Regression for the 2026-06-29 corruption: the Polymarket Data API intermittently
returned a *different* account's positions (live FIFA World Cup bets) in response
to the query for our wallet. The tracker auto-discovered them as ours and later
auto-closed them, booking ~$4,994 of phantom realized P&L. Every position echoes
its true owner in `proxyWallet`, so foreign positions must be dropped before
ingestion.

Runnable two ways:
    pytest tests/test_position_filter.py
    python tests/test_position_filter.py        # no pytest dependency required
"""
import importlib.util
import os

# Load the pure module directly by file path, bypassing the `app` package
# machinery (which eagerly imports the async-DB / aiohttp stack). The function
# under test depends only on stdlib.
_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "services", "position_filter.py",
)
_spec = importlib.util.spec_from_file_location("position_filter", _MODULE_PATH)
_position_filter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_position_filter)
own_positions = _position_filter.own_positions

OURS = "0xf7cc6bd64be987730dc783e6d4787b2d1b802506"
THEM = "0x1111111111111111111111111111111111111111"


def test_drops_foreign_proxywallet():
    """The exact failure mode: a foreign account's positions returned for us."""
    positions = [
        {"title": "Greenland", "proxyWallet": OURS, "size": 9},
        {"title": "Brazil leading at halftime?", "proxyWallet": THEM, "size": 3837},
        {"title": "Will Norway win on 2026-06-30?", "proxyWallet": THEM, "size": 3605},
    ]
    ours, foreign = own_positions(positions, OURS)
    assert [p["title"] for p in ours] == ["Greenland"]
    assert len(foreign) == 2


def test_case_insensitive_match():
    """Wallet comparison must be case-insensitive (API/checksum casing varies)."""
    positions = [{"title": "ours", "proxyWallet": OURS.upper(), "size": 1}]
    ours, foreign = own_positions(positions, OURS)
    assert len(ours) == 1 and not foreign


def test_missing_proxywallet_fails_open():
    """Missing proxyWallet -> keep it. Avoids a future API shape change nuking
    every position (which would then auto-close the whole real book)."""
    positions = [{"title": "no-owner-field", "size": 1}]
    ours, foreign = own_positions(positions, OURS)
    assert len(ours) == 1 and not foreign


def test_empty_wallet_returns_all():
    """No configured wallet -> no filtering (don't silently drop everything)."""
    positions = [{"title": "a", "proxyWallet": THEM}]
    ours, foreign = own_positions(positions, "")
    assert len(ours) == 1 and not foreign


def test_all_ours_no_foreign():
    positions = [
        {"title": "a", "proxyWallet": OURS},
        {"title": "b", "proxyWallet": OURS.lower()},
    ]
    ours, foreign = own_positions(positions, OURS)
    assert len(ours) == 2 and not foreign


if __name__ == "__main__":
    test_drops_foreign_proxywallet()
    test_case_insensitive_match()
    test_missing_proxywallet_fails_open()
    test_empty_wallet_returns_all()
    test_all_ours_no_foreign()
    print("All position_filter tests passed.")
