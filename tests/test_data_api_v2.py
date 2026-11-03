"""Data API v2 helpers + get_wallet_positions status merge (v1 retired 2026-11-02)."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "data_api_v2", Path(__file__).resolve().parents[1] / "app" / "services" / "data_api_v2.py")
v2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v2)

WALLET = "0xf7cc6bd64be987730dc783e6d4787b2d1b802506"


def _pos(tok, size, status="OPEN", wallet=WALLET, redeemable=False):
    return {"token_id": tok, "condition_id": "0xc" + tok, "current_size": size, "avg_price": 0.4,
            "current_price": 0.5, "proxy_wallet": wallet, "status": status,
            "redeemable": redeemable, "outcome": "No", "realized_pnl": 0.0}


def test_merge_dedupes_applies_v1_size_floor_and_aliases():
    rows = v2.merge_positions([
        [_pos("a", 44), _pos("dust", 0.92)],
        [_pos("r", 25, "REDEEMABLE", redeemable=True)],
        [_pos("r", 25, "REDEEMABLE", redeemable=True)],   # REDEEMABLE_LOST overlaps
    ])
    assert sorted(r["asset"] for r in rows) == ["a", "r"]
    a = next(r for r in rows if r["asset"] == "a")
    assert a["size"] == 44 and a["curPrice"] == 0.5 and a["avgPrice"] == 0.4
    assert a["proxyWallet"] == WALLET and a["conditionId"] == "0xca"
    assert next(r for r in rows if r["asset"] == "r")["redeemable"] is True


def test_unwrap_rejects_v1_shape():
    with pytest.raises(ValueError):
        v2.unwrap([{"asset": "x"}])
    assert v2.unwrap({"data": None}) == []


def test_get_wallet_positions_queries_each_status_and_guards_wallet():
    from app.services.polymarket import PolymarketClient
    client = PolymarketClient(WALLET)
    calls = []
    by_status = {
        "OPEN": [_pos("a", 10), _pos("foreign", 5, wallet="0xdead")],
        "REDEEMABLE": [_pos("r", 3, "REDEEMABLE", redeemable=True)],
        "REDEEMABLE_LOST": [],
    }

    async def fake_request(url, params=None):
        calls.append((url, dict(params)))
        assert url == "https://data-api.polymarket.com/v2/positions"
        return {"data": by_status[params["status"]], "pagination": {"next_cursor": None}}

    client._request = fake_request
    rows = asyncio.run(client.get_wallet_positions())
    assert [c[1]["status"] for c in calls] == list(v2.POSITION_STATUSES)
    assert sorted(r["asset"] for r in rows) == ["a", "r"]   # foreign wallet dropped


def test_get_wallet_positions_follows_cursor():
    from app.services.polymarket import PolymarketClient
    client = PolymarketClient(WALLET)
    pages = {"": ({"data": [_pos("a", 1)], "pagination": {"next_cursor": "c2"}}),
             "c2": ({"data": [_pos("b", 2)], "pagination": {"next_cursor": None}})}

    async def fake_request(url, params=None):
        if params["status"] != "OPEN":
            return {"data": [], "pagination": {}}
        return pages[params.get("cursor", "")]

    client._request = fake_request
    assert sorted(r["asset"] for r in asyncio.run(client.get_wallet_positions())) == ["a", "b"]


def test_request_retries_429_then_returns(monkeypatch):
    from app.services.polymarket import PolymarketClient
    import app.services.polymarket as pm

    class Resp:
        def __init__(self, status, body=None, headers=None):
            self.status, self._body, self.headers = status, body, headers or {}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def raise_for_status(self):
            if self.status >= 400: raise pm.aiohttp.ClientResponseError(None, (), status=self.status)
        async def json(self): return self._body

    seq = [Resp(429, headers={"Retry-After": "0"}), Resp(200, {"data": [], "pagination": {}})]

    class Sess:
        closed = False
        def get(self, url, params=None, timeout=None): return seq.pop(0)

    sleeps = []
    async def fake_sleep(s): sleeps.append(s)
    monkeypatch.setattr(pm.asyncio, "sleep", fake_sleep)
    c = PolymarketClient(WALLET)
    async def gs(): return Sess()
    c._get_session = gs
    assert asyncio.run(c._request("https://x/v2/positions")) == {"data": [], "pagination": {}}
    assert sleeps == [0.0]
