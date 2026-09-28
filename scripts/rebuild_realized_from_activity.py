#!/usr/bin/env python3
"""Rebuild realized P&L from the Polymarket Data API activity feed and reconcile
it against the tracker DB (beads pm-rfz.3 / pm-rfz.4).

Why: the tracker books realized P&L at a CLOB midpoint when a position vanishes
from the Data API, never books partial sells, and is missing many early-2026
closes entirely. The Data API activity feed (TRADE / REDEEM / YIELD / REWARD
rows) is the authoritative fill ledger, so per-position realized P&L is rebuilt
from it with average-cost accounting and diffed against `positions.realized_pnl`.

Modes
    (default)   report: per-asset fills-vs-DB table, totals, identities
    --check     one-line RECON OK / RECON FAIL summary, exit 1 on FAIL (cron use)
    --apply     write corrections to the DB in one transaction (asks to confirm
                unless --yes)
    --json P    dump the per-asset ledger to P

Usage
    python scripts/rebuild_realized_from_activity.py --year 2026
    python scripts/rebuild_realized_from_activity.py --from-file activity.json --year 2026
    python scripts/rebuild_realized_from_activity.py --check --tolerance 2
    python scripts/rebuild_realized_from_activity.py --apply            # dry-run table + confirm
    python scripts/rebuild_realized_from_activity.py --apply --yes

Ledger rules (per token id, rows in timestamp order, average cost):
    BUY     shares += size; cost += usdcSize
    SELL    realized += usdcSize - size*avg; shares -= size; cost -= size*avg
    REDEEM  matched by (conditionId, outcomeIndex) -- the feed's `asset` is
            usually empty on REDEEM rows. usdcSize > 0 -> winner, realized +=
            usdcSize - cost; usdcSize == 0 -> loser, realized -= cost. Shares
            go to 0 either way (the loser row's `size` is meaningless).
    after   shares > 0 and no REDEEM -> ask Gamma. Resolved -> book at $1/$0
            (status resolved_unredeemed); not resolved -> open, unrealized.
    Realized is attributed to the year of the sell / redeem / resolution.
    YIELD and REWARD rows are summed separately as yield income.

The pure parts (build_ledgers, join_ledgers, plan_corrections, identities) have
no DB / HTTP imports and are unit-tested in tests/test_rebuild_realized.py.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DEFAULT_WALLET = "0xf7cc6bd64be987730dc783e6d4787b2d1b802506"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
PAGE_SIZE = 100
PAGE_SLEEP_S = 1.5
GAMMA_SLEEP_S = 0.4

ZERO = Decimal("0")
Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")
Q8 = Decimal("0.00000001")
SHARE_EPS = Decimal("0.000001")     # float-noise threshold for "no shares left"
DUST_SHARES = Decimal("0.01")       # remainder at or below this is written off, not a position
MONEY_EPS = Decimal("0.005")        # below this a money diff is "zero"
REDEEM_MISMATCH_TOL = Decimal("0.01")

ResolverFn = Callable[[str], Optional[dict]]


def dec(value: Any) -> Decimal:
    """Decimal from a feed float/str without binary-float artefacts."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def q(value: Decimal, places: Decimal = Q8) -> Decimal:
    return value.quantize(places, rounding=ROUND_HALF_UP)


def ts_to_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def year_of(ts: int) -> int:
    return ts_to_dt(ts).year


# =========================================================================== #
# Ledger (pure)
# =========================================================================== #

@dataclass
class Fill:
    ts: int
    size: Decimal
    price: Decimal
    usdc: Decimal
    cost_out: Decimal = ZERO        # cost basis released by this exit (0 for buys)

    def as_dict(self) -> dict:
        return {"ts": self.ts, "date": ts_to_dt(self.ts).isoformat(), "size": str(self.size),
                "price": str(self.price), "usdc": str(self.usdc), "cost_out": str(self.cost_out)}


@dataclass
class AssetLedger:
    asset: str
    condition_id: str
    outcome_index: int
    title: str = ""
    slug: str = ""
    event_slug: str = ""
    shares: Decimal = ZERO
    cost: Decimal = ZERO
    total_bought: Decimal = ZERO
    total_bought_cost: Decimal = ZERO
    buys: List[Fill] = field(default_factory=list)
    sells: List[Fill] = field(default_factory=list)
    redeems: List[Fill] = field(default_factory=list)   # winner payouts (cash in)
    exits: List[Fill] = field(default_factory=list)     # sells + redeem/resolution, for exit VWAP
    realized_by_year: Dict[int, Decimal] = field(default_factory=dict)
    status: str = "open"            # open | closed | resolved_unredeemed
    redeemed: bool = False
    resolution: Optional[dict] = None
    mark_price: Optional[Decimal] = None
    flags: List[str] = field(default_factory=list)
    first_buy_ts: Optional[int] = None
    last_exit_ts: Optional[int] = None

    # -- derived -----------------------------------------------------------
    @property
    def direction(self) -> str:
        return "yes" if int(self.outcome_index) == 0 else "no"

    @property
    def avg_cost(self) -> Decimal:
        if self.shares > SHARE_EPS:
            return self.cost / self.shares
        if self.total_bought > ZERO:
            return self.total_bought_cost / self.total_bought
        return ZERO

    @property
    def entry_avg(self) -> Decimal:
        """Average price over every buy (what a DB row's entry_price means)."""
        return self.total_bought_cost / self.total_bought if self.total_bought > ZERO else ZERO

    def realized(self, year: Optional[int] = None) -> Decimal:
        if year is None:
            return sum(self.realized_by_year.values(), ZERO)
        return self.realized_by_year.get(year, ZERO)

    def exit_vwap(self) -> Optional[Decimal]:
        total = sum((f.size for f in self.exits), ZERO)
        if total <= ZERO:
            return None
        return sum((f.size * f.price for f in self.exits), ZERO) / total

    def unrealized(self) -> Optional[Decimal]:
        if self.status != "open" or self.shares <= SHARE_EPS or self.mark_price is None:
            return None
        return self.shares * (self.mark_price - self.avg_cost)

    def active_in(self, year: int) -> bool:
        if self.status == "open" and self.shares > SHARE_EPS:
            return True
        if any(year_of(f.ts) == year for f in self.buys):
            return True
        if any(year_of(f.ts) == year for f in self.exits):
            return True
        return abs(self.realized(year)) > ZERO

    def years_with_exits(self) -> List[int]:
        return sorted({year_of(f.ts) for f in self.exits})

    def holdings_at(self, ts: int) -> Tuple[Decimal, Decimal]:
        """(shares, cost) held strictly before unix time ``ts``."""
        shares = sum((f.size for f in self.buys if f.ts < ts), ZERO) - sum((f.size for f in self.exits if f.ts < ts), ZERO)
        cost = sum((f.usdc for f in self.buys if f.ts < ts), ZERO) - sum((f.cost_out for f in self.exits if f.ts < ts), ZERO)
        if shares <= SHARE_EPS:
            return ZERO, ZERO
        return shares, cost

    # -- mutation ----------------------------------------------------------
    def _book(self, ts: int, amount: Decimal) -> None:
        y = year_of(ts)
        self.realized_by_year[y] = self.realized_by_year.get(y, ZERO) + q(amount)

    def buy(self, ts: int, size: Decimal, price: Decimal, usdc: Decimal) -> None:
        self.shares += size
        self.cost += usdc
        self.total_bought += size
        self.total_bought_cost += usdc
        self.buys.append(Fill(ts, size, price, usdc))
        if self.status == "closed" and not self.redeemed:
            self.status = "open"                  # re-entry after a full sell-out
        if self.first_buy_ts is None or ts < self.first_buy_ts:
            self.first_buy_ts = ts

    def sell(self, ts: int, size: Decimal, price: Decimal, usdc: Decimal) -> None:
        if size > self.shares + SHARE_EPS:
            self.flags.append(f"oversell: sold {size} with {self.shares} held at {ts_to_dt(ts).date()}")
            size = self.shares
        avg = self.avg_cost
        cost_out = q(size * avg, Q8) if self.shares > ZERO else ZERO
        self._book(ts, usdc - cost_out)
        self.shares -= size
        self.cost -= cost_out
        if self.shares <= SHARE_EPS:
            self.shares = ZERO
            self.cost = ZERO
        self.sells.append(Fill(ts, size, price, usdc))
        self.exits.append(Fill(ts, size, price, usdc, cost_out))
        self.last_exit_ts = ts
        if self.shares == ZERO and self.status == "open":
            self.status = "closed"

    def redeem(self, ts: int, usdc: Decimal, reported_size: Decimal) -> None:
        held = self.shares
        cost_out = self.cost
        if usdc > ZERO:
            if held <= SHARE_EPS:
                self.flags.append(f"redeem payout {usdc} with no shares held")
            elif abs(usdc - held) > REDEEM_MISMATCH_TOL:
                self.flags.append(f"redeem payout mismatch: usdc {usdc} vs {held} shares held")
            self._book(ts, usdc - self.cost)
            price = (usdc / held) if held > SHARE_EPS else Decimal("1")
            self.exits.append(Fill(ts, held if held > SHARE_EPS else usdc, price, usdc, cost_out))
            self.redeems.append(Fill(ts, held, price, usdc, cost_out))
        else:
            if held <= SHARE_EPS:
                self.flags.append("loser redeem with no shares held")
            self._book(ts, -self.cost)
            self.exits.append(Fill(ts, held, ZERO, ZERO, cost_out))
        self.shares = ZERO
        self.cost = ZERO
        self.redeemed = True
        self.status = "closed"
        self.last_exit_ts = ts

    def write_off_dust(self, ts: int) -> None:
        """A sub-DUST_SHARES remainder is float noise from a partial sell, not a position."""
        self.flags.append(f"dust {self.shares} shares written off at cost {q(self.cost, Q4)}")
        self._book(ts, -self.cost)
        self.exits.append(Fill(ts, self.shares, ZERO, ZERO, self.cost))
        self.shares = ZERO
        self.cost = ZERO
        self.status = "closed"
        if self.last_exit_ts is None:
            self.last_exit_ts = ts

    def resolve(self, res: dict, now: datetime) -> None:
        """Apply a Gamma resolution to an unredeemed remainder."""
        self.resolution = res
        if not res.get("resolved"):
            prices = res.get("prices")
            if prices and len(prices) > int(self.outcome_index):
                self.mark_price = dec(prices[int(self.outcome_index)])
            return
        outcome = res.get("outcome")
        if outcome not in ("yes", "no"):
            self.flags.append("gamma says resolved but winner unknown; left open")
            return
        when = res.get("resolved_at") or now
        ts = int(when.timestamp())
        won = outcome == self.direction
        if won:
            self._book(ts, self.shares - self.cost)
            self.exits.append(Fill(ts, self.shares, Decimal("1"), self.shares, self.cost))
        else:
            self._book(ts, -self.cost)
            self.exits.append(Fill(ts, self.shares, ZERO, ZERO, self.cost))
        self.shares = ZERO
        self.cost = ZERO
        self.status = "resolved_unredeemed"
        self.last_exit_ts = ts

    def as_dict(self) -> dict:
        vw = self.exit_vwap()
        return {
            "asset": self.asset, "condition_id": self.condition_id, "outcome_index": self.outcome_index,
            "direction": self.direction, "title": self.title, "slug": self.slug, "event_slug": self.event_slug,
            "status": self.status, "redeemed": self.redeemed,
            "net_shares": str(q(self.shares)), "cost": str(q(self.cost)), "avg_cost": str(q(self.avg_cost, Q4)),
            "total_bought": str(q(self.total_bought)), "total_bought_cost": str(q(self.total_bought_cost)),
            "entry_avg": str(q(self.entry_avg, Q4)),
            "exit_vwap": str(q(vw, Q4)) if vw is not None else None,
            "realized_by_year": {str(y): str(q(v, Q2)) for y, v in sorted(self.realized_by_year.items())},
            "mark_price": str(self.mark_price) if self.mark_price is not None else None,
            "unrealized": str(q(self.unrealized(), Q2)) if self.unrealized() is not None else None,
            "first_buy": ts_to_dt(self.first_buy_ts).isoformat() if self.first_buy_ts else None,
            "last_exit": ts_to_dt(self.last_exit_ts).isoformat() if self.last_exit_ts else None,
            "buys": [f.as_dict() for f in self.buys], "sells": [f.as_dict() for f in self.sells],
            "exits": [f.as_dict() for f in self.exits],
            "resolution": {k: (v.isoformat() if isinstance(v, datetime) else
                               [str(p) for p in v] if isinstance(v, list) else v)
                           for k, v in (self.resolution or {}).items() if k != "gamma"} or None,
            "flags": list(self.flags),
        }


@dataclass
class LedgerResult:
    assets: Dict[str, AssetLedger]
    yield_by_year: Dict[int, Decimal]
    yield_events: List[Fill]
    yield_count: int
    unmatched_redeems: List[dict]
    rows_processed: int
    gamma_lookups: int = 0
    noop_redeems: int = 0

    def yield_income(self, year: Optional[int] = None) -> Decimal:
        if year is None:
            return sum(self.yield_by_year.values(), ZERO)
        return self.yield_by_year.get(year, ZERO)


def _row_rank(row: dict) -> Tuple[int, int]:
    """Stable within-timestamp order: buys, then sells, then redeems, then income."""
    kind = row.get("type")
    if kind == "TRADE":
        return (0 if row.get("side") == "BUY" else 1, 0)
    if kind == "REDEEM":
        return (2, 0)
    return (3, 0)


def build_ledgers(rows: List[dict], *, resolver: Optional[ResolverFn] = None,
                  now: Optional[datetime] = None) -> LedgerResult:
    """Replay the activity feed into per-asset average-cost ledgers."""
    now = now or datetime.now(timezone.utc)
    ordered = sorted(enumerate(rows), key=lambda ir: (int(ir[1].get("timestamp") or 0), _row_rank(ir[1]), ir[0]))

    assets: Dict[str, AssetLedger] = {}
    by_cond: Dict[str, List[str]] = {}
    yield_by_year: Dict[int, Decimal] = {}
    yield_events: List[Fill] = []
    yield_count = 0
    unmatched: List[dict] = []
    noop_redeems = 0

    # Pre-index conditionId -> our assets from TRADE rows so a REDEEM can be
    # matched even when its own `asset` field is empty (it usually is).
    for _, row in ordered:
        if row.get("type") == "TRADE" and row.get("asset"):
            lst = by_cond.setdefault(row.get("conditionId") or "", [])
            if row["asset"] not in lst:
                lst.append(row["asset"])

    for _, row in ordered:
        kind = row.get("type")
        ts = int(row.get("timestamp") or 0)
        if kind == "TRADE":
            asset = row.get("asset") or ""
            if not asset:
                unmatched.append(row)
                continue
            led = assets.get(asset)
            if led is None:
                led = AssetLedger(asset=asset, condition_id=row.get("conditionId") or "",
                                  outcome_index=int(row.get("outcomeIndex") or 0),
                                  title=row.get("title") or "", slug=row.get("slug") or "",
                                  event_slug=row.get("eventSlug") or "")
                assets[asset] = led
            size, price, usdc = dec(row.get("size")), dec(row.get("price")), dec(row.get("usdcSize"))
            if row.get("side") == "BUY":
                led.buy(ts, size, price, usdc)
            elif row.get("side") == "SELL":
                led.sell(ts, size, price, usdc)
            else:
                led.flags.append(f"unknown trade side {row.get('side')!r} at {ts}")
        elif kind == "REDEEM":
            # One REDEEM row per redemption call on a condition. Observed
            # semantics (2023-2026 feed): outcomeIndex is the side that *paid*
            # (the winner) and usdcSize our payout. Post-V2 rows sometimes carry
            # our own side + asset instead. Both are handled: match our asset on
            # (condition, index) if we have one; a zero payout whose index is not
            # ours means everything we still hold on the condition lost.
            cond = row.get("conditionId") or ""
            idx = int(row.get("outcomeIndex") or 0)
            usdc = dec(row.get("usdcSize"))
            holders = [assets[a] for a in by_cond.get(cond, []) if a in assets]
            target = next((l for l in holders if int(l.outcome_index) == idx), None)
            if target is None and row.get("asset") and row["asset"] in assets:
                target = assets[row["asset"]]
                holders = holders or [target]
            if usdc > ZERO:
                if target is None:
                    unmatched.append(row)
                    continue
                target.redeem(ts, usdc, dec(row.get("size")))
                for other in holders:
                    if other is not target and other.shares > SHARE_EPS:
                        other.redeem(ts, ZERO, ZERO)
            else:
                if target is not None:
                    losers = [target] if target.shares > SHARE_EPS else []
                else:
                    losers = [l for l in holders if l.shares > SHARE_EPS]
                if not losers:
                    if holders:
                        noop_redeems += 1
                    else:
                        unmatched.append(row)
                    continue
                for l in losers:
                    l.redeem(ts, ZERO, ZERO)
        elif kind in ("YIELD", "REWARD"):
            usdc = dec(row.get("usdcSize"))
            y = year_of(ts)
            yield_by_year[y] = yield_by_year.get(y, ZERO) + usdc
            yield_events.append(Fill(ts, ZERO, ZERO, usdc))
            yield_count += 1
        # any other row type is ignored (none observed in the feed)

    gamma_lookups = 0
    now_ts = int(now.timestamp())
    for led in assets.values():
        if led.status == "open" and SHARE_EPS < led.shares <= DUST_SHARES:
            led.write_off_dust(led.last_exit_ts or now_ts)
        if led.status != "open" or led.shares <= SHARE_EPS:
            if led.shares <= SHARE_EPS and led.status == "open":
                led.status = "closed"
            continue
        if resolver is None:
            continue
        res = resolver(led.condition_id)
        gamma_lookups += 1
        if res is None:
            led.flags.append("gamma lookup returned nothing; left open, unpriced")
            continue
        led.resolve(res, now)

    return LedgerResult(assets=assets, yield_by_year=yield_by_year, yield_events=yield_events,
                        yield_count=yield_count, unmatched_redeems=unmatched,
                        rows_processed=len(rows), gamma_lookups=gamma_lookups, noop_redeems=noop_redeems)


@dataclass
class CarryPosition:
    """Shares held across the Jan-1 boundary of the report year, at cost."""
    asset: str
    ledger: AssetLedger
    shares: Decimal
    cost: Decimal
    mark: Optional[Decimal] = None

    @property
    def gain(self) -> Optional[Decimal]:
        """Unrealized gain sitting in the position at year start (mark - cost)."""
        if self.mark is None:
            return None
        return self.shares * self.mark - self.cost


def carry_mark_from_resolution(res: Optional[dict], direction: str, boundary: datetime) -> Optional[Decimal]:
    """Jan-1 mark for a position whose market had already resolved before the
    boundary: exactly the payout, $1 if our side won, else $0. None otherwise."""
    if not res or not res.get("resolved") or res.get("outcome") not in ("yes", "no"):
        return None
    resolved_at = res.get("resolved_at")
    if resolved_at is None or resolved_at >= boundary:
        return None
    return Decimal("1") if res["outcome"] == direction else ZERO


def carry_positions(ledgers: LedgerResult, year: int) -> List[CarryPosition]:
    boundary = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    out: List[CarryPosition] = []
    for led in ledgers.assets.values():
        shares, cost = led.holdings_at(boundary)
        if shares > DUST_SHARES:
            out.append(CarryPosition(asset=led.asset, ledger=led, shares=shares, cost=cost))
    return out


# =========================================================================== #
# Join against DB rows (pure)
# =========================================================================== #

@dataclass
class AssetReport:
    asset: str
    title: str
    direction: str
    status: str
    net_shares: Decimal
    avg_cost: Decimal
    fills_realized: Decimal
    db_realized: Decimal
    diff: Decimal
    db_ids: List[int]
    db_statuses: List[str]
    join_kind: str            # token | condition+direction | heuristic | none
    flags: List[str]
    db_rows: List[dict]
    ledger: AssetLedger


@dataclass
class JoinResult:
    year: int
    per_asset: List[AssetReport]
    backfill_candidates: List[AssetReport]
    suspect_db_rows: List[dict]
    open_partial: List[AssetReport]
    rows_outside_year: List[dict]
    total_fills_realized: Decimal
    total_db_realized: Decimal
    max_abs_diff: Decimal
    heuristic_links: List[Tuple[str, int]]


def _row_token(row: dict) -> Optional[str]:
    tok = row.get("token_yes") if (row.get("direction") or "").lower() == "yes" else row.get("token_no")
    return str(tok) if tok else None


def _row_in_scope(row: dict, year: int) -> bool:
    if (row.get("status") or "").lower() == "open":
        return True
    exit_date = row.get("exit_date")
    if exit_date is None:
        return True          # closed with no exit date: can't exclude it, report it
    return exit_date.year == year


def _latest_row(rows: List[dict]) -> dict:
    opens = [r for r in rows if (r.get("status") or "").lower() == "open"]
    pool = opens or rows
    return max(pool, key=lambda r: (r.get("exit_date") or r.get("entry_date") or datetime.min.replace(tzinfo=timezone.utc), r["id"]))


def resolve_asset_prefix(ledgers: LedgerResult, prefix: str) -> str:
    matches = [a for a in ledgers.assets if a.startswith(prefix)]
    if len(matches) != 1:
        raise ValueError(f"asset prefix {prefix!r} matches {len(matches)} assets")
    return matches[0]


def join_ledgers(ledgers: LedgerResult, db_rows: List[dict], *, year: int,
                 manual_links: Optional[Dict[str, int]] = None) -> JoinResult:
    active = [led for led in ledgers.assets.values() if led.active_in(year)]
    forced: Dict[str, int] = {resolve_asset_prefix(ledgers, p): pid for p, pid in (manual_links or {}).items()}
    in_scope = [r for r in db_rows if _row_in_scope(r, year)]
    outside = [r for r in db_rows if not _row_in_scope(r, year)]

    by_token: Dict[str, List[dict]] = {}
    by_cond: Dict[Tuple[str, str], List[dict]] = {}
    for r in in_scope:
        tok = _row_token(r)
        if tok:
            by_token.setdefault(tok, []).append(r)
        elif r.get("condition_id"):
            by_cond.setdefault((r["condition_id"], (r.get("direction") or "").lower()), []).append(r)

    joined_ids: set = set()
    reports: Dict[str, AssetReport] = {}
    unjoined_assets: List[AssetLedger] = []

    def make_report(led: AssetLedger, rows: List[dict], kind: str) -> AssetReport:
        db_realized = sum((dec(r.get("realized_pnl")) for r in rows), ZERO)
        fills = q(led.realized(year), Q2)
        return AssetReport(
            asset=led.asset, title=led.title, direction=led.direction, status=led.status,
            net_shares=q(led.shares, Q4), avg_cost=q(led.avg_cost, Q4),
            fills_realized=fills, db_realized=db_realized, diff=q(fills - db_realized, Q2),
            db_ids=[r["id"] for r in rows], db_statuses=[r.get("status") or "" for r in rows],
            join_kind=kind, flags=list(led.flags), db_rows=rows, ledger=led,
        )

    row_by_id_all = {r["id"]: r for r in in_scope}
    heuristic_links: List[Tuple[str, int]] = []
    for led in active:
        if led.asset in forced:
            pid = forced[led.asset]
            if pid not in row_by_id_all:
                raise ValueError(f"--link {led.asset[:8]}={pid}: position {pid} is not an in-scope DB row")
            rows = [row_by_id_all[pid]]
            kind = "manual"
            heuristic_links.append((led.asset, pid))
        else:
            rows = by_token.get(led.asset)
            kind = "token"
            if not rows:
                rows = by_cond.get((led.condition_id, led.direction))
                kind = "condition+direction"
        if rows:
            joined_ids.update(r["id"] for r in rows)
            reports[led.asset] = make_report(led, rows, kind)
        else:
            unjoined_assets.append(led)

    # Heuristic link for early manual DB rows that carry neither token nor
    # condition id: same direction, same realized (±0.05) and same share count
    # (±0.5), unique in both directions.
    orphan_rows = [r for r in in_scope if r["id"] not in joined_ids and not _row_token(r) and not r.get("condition_id")]
    if orphan_rows and unjoined_assets:
        pairs: Dict[int, List[AssetLedger]] = {}
        rev: Dict[str, List[int]] = {}
        for r in orphan_rows:
            for led in unjoined_assets:
                if led.direction != (r.get("direction") or "").lower():
                    continue
                if abs(dec(r.get("realized_pnl")) - led.realized(year)) > Decimal("0.05"):
                    continue
                if abs(dec(r.get("shares")) - led.total_bought) > Decimal("0.5"):
                    continue
                pairs.setdefault(r["id"], []).append(led)
                rev.setdefault(led.asset, []).append(r["id"])
        row_by_id = {r["id"]: r for r in orphan_rows}
        for rid, leds in pairs.items():
            if len(leds) != 1 or len(rev.get(leds[0].asset, [])) != 1:
                continue
            led = leds[0]
            reports[led.asset] = make_report(led, [row_by_id[rid]], "heuristic")
            joined_ids.add(rid)
            heuristic_links.append((led.asset, rid))
            unjoined_assets = [x for x in unjoined_assets if x.asset != led.asset]

    backfill: List[AssetReport] = [make_report(led, [], "none") for led in unjoined_assets]
    per_asset: List[AssetReport] = [reports[led.asset] for led in active if led.asset in reports] + backfill
    suspect = [r for r in in_scope if r["id"] not in joined_ids]

    open_partial = [
        rep for rep in per_asset
        if rep.ledger.status == "open"
        and any((s.lower() == "open") for s in rep.db_statuses)
        and any(year_of(f.ts) == year for f in rep.ledger.sells)
        and abs(rep.diff) > MONEY_EPS
    ]

    total_fills = sum((rep.fills_realized for rep in per_asset), ZERO)
    total_db = sum((dec(r.get("realized_pnl")) for r in in_scope), ZERO)
    diffs = [abs(rep.diff) for rep in per_asset] + [abs(dec(r.get("realized_pnl"))) for r in suspect]
    max_abs = max(diffs) if diffs else ZERO

    return JoinResult(year=year, per_asset=per_asset, backfill_candidates=backfill, suspect_db_rows=suspect,
                      open_partial=open_partial, rows_outside_year=outside,
                      total_fills_realized=q(total_fills, Q2), total_db_realized=q(total_db, Q2),
                      max_abs_diff=q(max_abs, Q2), heuristic_links=heuristic_links)


# =========================================================================== #
# Identities (pure)
# =========================================================================== #

def cash_identity(ledgers: LedgerResult, *, start_cash: Decimal, start_ts: datetime, end_ts: datetime,
                  end_cash: Decimal) -> dict:
    """start_cash - buys + sells + redeems + yield over (start_ts, end_ts] vs end_cash."""
    lo, hi = int(start_ts.timestamp()), int(end_ts.timestamp())

    def in_window(f: Fill) -> bool:
        return lo < f.ts <= hi

    buys = sells = redeems = ZERO
    for led in ledgers.assets.values():
        buys += sum((f.usdc for f in led.buys if in_window(f)), ZERO)
        sells += sum((f.usdc for f in led.sells if in_window(f)), ZERO)
        redeems += sum((f.usdc for f in led.redeems if in_window(f)), ZERO)
    yield_income = sum((f.usdc for f in ledgers.yield_events if in_window(f)), ZERO)
    expected = start_cash - buys + sells + redeems + yield_income
    return {
        "start_cash": start_cash, "start_ts": start_ts, "end_ts": end_ts, "end_cash": end_cash,
        "buys": q(buys, Q2), "sells": q(sells, Q2), "redeems": q(redeems, Q2), "yield": q(yield_income, Q4),
        "expected_end_cash": q(expected, Q2), "residual": q(end_cash - expected, Q2),
    }


def equity_identity(*, start_capital: Decimal, fills_realized: Decimal, yield_income: Decimal,
                    unrealized: Decimal, latest_total_value: Decimal, carry_gain: Decimal = ZERO) -> dict:
    """start_capital + realized - carry_gain + yield + unrealized vs latest total_value.

    ``carry_gain`` is the unrealized gain already embedded in start_capital for
    positions carried across the year boundary (their in-year realized is
    measured from cost, so that part would otherwise be double counted).
    """
    expected = start_capital + fills_realized - carry_gain + yield_income + unrealized
    return {
        "start_capital": start_capital, "fills_realized": q(fills_realized, Q2), "yield": q(yield_income, Q4),
        "unrealized": q(unrealized, Q2), "carry_gain": q(carry_gain, Q2), "latest_total_value": latest_total_value,
        "expected_total_value": q(expected, Q2), "residual": q(latest_total_value - expected, Q2),
    }


# =========================================================================== #
# Apply plan (pure)
# =========================================================================== #

@dataclass
class Action:
    kind: str                       # update_closed | update_open | insert | link_market
    asset: str
    title: str = ""
    position_id: Optional[int] = None
    old_realized: Optional[Decimal] = None
    new_realized: Optional[Decimal] = None
    exit_price: Optional[Decimal] = None
    exit_reasoning_suffix: str = ""
    market: Optional[dict] = None
    market_id: Optional[int] = None
    position: Optional[dict] = None


def plan_corrections(joined: JoinResult, ledgers: LedgerResult, *, year: int, today: str,
                     existing_market_ids_by_condition: Dict[str, int]) -> List[Action]:
    plan: List[Action] = []

    # A manual DB market row can be shared by positions that are really
    # different Polymarket conditions (e.g. two SpaceX markets on market 16).
    # Linking such a row to either condition would corrupt it, so skip those.
    conds_by_market: Dict[int, set] = {}
    for rep in joined.per_asset:
        if rep.join_kind in ("heuristic", "manual"):
            for r in rep.db_rows:
                if r.get("market_id") is not None:
                    conds_by_market.setdefault(r["market_id"], set()).add(rep.ledger.condition_id)

    for rep in joined.per_asset:
        led = rep.ledger
        rows = rep.db_rows
        if not rows:
            continue
        fills = rep.fills_realized
        vwap = led.exit_vwap()
        exit_price = q(vwap, Q4) if vwap is not None else None

        if rep.join_kind in ("heuristic", "manual") and not (_row_token(rows[0]) and rows[0].get("condition_id")):
            r = rows[0]
            shared = conds_by_market.get(r.get("market_id"), set())
            if len(shared) > 1:
                led.flags.append(f"market {r.get('market_id')} shared by {len(shared)} conditions; not linked")
            else:
                plan.append(Action(kind="link_market", asset=led.asset, title=led.title, position_id=r["id"],
                                   market_id=r.get("market_id"),
                                   market={"condition_id": led.condition_id,
                                           "clob_token_id_yes": led.asset if led.direction == "yes" else None,
                                           "clob_token_id_no": led.asset if led.direction == "no" else None}))

        if len(rows) == 1:
            r = rows[0]
            old = dec(r.get("realized_pnl"))
            status = (r.get("status") or "").lower()
            if status == "closed":
                if abs(fills - old) >= Q2:
                    plan.append(Action(kind="update_closed", asset=led.asset, title=led.title, position_id=r["id"],
                                       old_realized=old, new_realized=fills, exit_price=exit_price,
                                       exit_reasoning_suffix=f" | fills-reconciled {today} (was {old:.2f})"))
            elif status == "open":
                if led.status != "open":
                    led.flags.append(f"DB row {r['id']} open but fills say {led.status}; left to tracker")
                    continue
                partial = q(led.realized(year), Q2)
                if abs(partial - old) >= Q2:
                    plan.append(Action(kind="update_open", asset=led.asset, title=led.title, position_id=r["id"],
                                       old_realized=old, new_realized=partial, exit_price=None,
                                       exit_reasoning_suffix=""))
            continue

        # several DB rows for one token: fix the token total on the latest row only
        residual = q(fills - rep.db_realized, Q2)
        if abs(residual) < Q2:
            continue
        target = _latest_row(rows)
        old = dec(target.get("realized_pnl"))
        status = (target.get("status") or "").lower()
        if status == "open" and led.status != "open":
            led.flags.append(f"DB row {target['id']} open but fills say {led.status}; left to tracker")
            continue
        plan.append(Action(
            kind="update_closed" if status == "closed" else "update_open", asset=led.asset, title=led.title,
            position_id=target["id"], old_realized=old, new_realized=q(old + residual, Q2),
            exit_price=exit_price if status == "closed" else None,
            exit_reasoning_suffix=(f" | fills-reconciled {today} (token total across {len(rows)} rows; was {old:.2f})"
                                   if status == "closed" else ""),
        ))

    for rep in joined.backfill_candidates:
        led = rep.ledger
        if led.status == "open":
            continue                    # the tracker owns open rows
        realized = q(led.realized(year), Q2)
        if abs(realized) < MONEY_EPS:
            continue
        vwap = led.exit_vwap()
        market_id = existing_market_ids_by_condition.get(led.condition_id)
        market = None
        if market_id is None:
            market = {
                "slug": led.slug or f"backfill-{led.asset[:16]}", "title": led.title or led.slug or led.asset,
                "condition_id": led.condition_id,
                "clob_token_id_yes": led.asset if led.direction == "yes" else None,
                "clob_token_id_no": led.asset if led.direction == "no" else None,
                "end_date": None,
            }
        exit_ts = led.last_exit_ts or led.first_buy_ts
        exit_price = q(vwap, Q4) if vwap is not None else None
        position = {
            "direction": led.direction,
            "shares": q(led.total_bought, Q8),
            "entry_price": q(led.entry_avg, Q4),
            "cost_basis": q(led.total_bought_cost, Q2),
            "entry_date": ts_to_dt(led.first_buy_ts) if led.first_buy_ts else ts_to_dt(exit_ts),
            "exit_date": ts_to_dt(exit_ts) if exit_ts else None,
            "exit_price": exit_price,
            "current_price": exit_price,
            "current_value": ZERO,
            "unrealized_pnl": ZERO,
            "realized_pnl": realized,
            "status": "closed",
            "exit_reasoning": f"backfill from Data API activity feed {today}",
            "api_miss_count": 0,
        }
        plan.append(Action(kind="insert", asset=led.asset, title=led.title, market=market, market_id=market_id,
                           position=position, new_realized=realized, exit_price=exit_price))
    return plan


# =========================================================================== #
# HTTP: Data API activity feed + Gamma resolution
# =========================================================================== #

def http_json(url: str, *, retries: int = 6, timeout: int = 30) -> Any:
    delay = 5.0
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        req = urlrequest.Request(url, headers=HTTP_HEADERS)
        try:
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as e:
            last_err = e
            if e.code == 429 or e.code >= 500:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise
        except (urlerror.URLError, socket.timeout, TimeoutError) as e:
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(f"giving up on {url}: {last_err}")


def fetch_year_start_mark(token_id: str, year: int) -> Optional[Decimal]:
    """CLOB price closest to Jan 1 of ``year`` for a token (±3 days), or None."""
    boundary = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    qs = urlparse.urlencode({"market": token_id, "startTs": boundary - 3 * 86400,
                             "endTs": boundary + 3 * 86400, "fidelity": 60})
    try:
        data = http_json(f"{CLOB_API}/prices-history?{qs}", retries=3)
    except Exception:
        return None
    history = data.get("history") if isinstance(data, dict) else None
    if not history:
        return None
    best = min(history, key=lambda p: abs(int(p.get("t", 0)) - boundary))
    return dec(best.get("p"))


def fetch_activity(wallet: str, *, page_size: int = PAGE_SIZE, sleep_s: float = PAGE_SLEEP_S,
                   max_pages: int = 500, log=print) -> List[dict]:
    rows: List[dict] = []
    seen: set = set()
    dups = 0
    offset = 0
    for _ in range(max_pages):
        qs = urlparse.urlencode({"user": wallet, "limit": page_size, "offset": offset})
        page = http_json(f"{DATA_API}/activity?{qs}")
        if not isinstance(page, list) or not page:
            break
        for row in page:
            key = json.dumps(row, sort_keys=True)
            if key in seen:
                dups += 1
                continue
            seen.add(key)
            rows.append(row)
        log(f"  activity page offset={offset}: {len(page)} rows")
        if len(page) < page_size:
            break
        offset += page_size
        time.sleep(sleep_s)
    if dups:
        log(f"  dropped {dups} exact-duplicate rows (page shift during paging)")
    return rows


def _load_resolution_module():
    path = REPO_ROOT / "app" / "services" / "resolution.py"
    spec = importlib.util.spec_from_file_location("_recon_resolution", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class GammaResolver:
    """condition_id -> {resolved, outcome, resolved_at, prices, gamma}; cached.

    Gamma hides closed markets unless closed=true is passed, and hides open
    markets when it is, so a lookup tries closed=true first and then plain.
    """

    def __init__(self, sleep_s: float = GAMMA_SLEEP_S, log=print):
        self.cache: Dict[str, Optional[dict]] = {}
        self.sleep_s = sleep_s
        self.log = log
        self._res = _load_resolution_module()

    def _get(self, condition_id: str, closed: Optional[bool]) -> List[dict]:
        params = {"condition_ids": condition_id}
        if closed is not None:
            params["closed"] = "true" if closed else "false"
        data = http_json(f"{GAMMA_API}/markets?{urlparse.urlencode(params)}")
        return data if isinstance(data, list) else []

    def __call__(self, condition_id: str) -> Optional[dict]:
        if condition_id in self.cache:
            return self.cache[condition_id]
        if not condition_id:
            self.cache[condition_id] = None
            return None
        data = self._get(condition_id, True)
        if not data:
            time.sleep(self.sleep_s)
            data = self._get(condition_id, None)
        time.sleep(self.sleep_s)
        if not data:
            self.log(f"  gamma: no market for condition {condition_id[:12]}…")
            self.cache[condition_id] = None
            return None
        m = data[0]
        state = self._res.parse_resolution({
            "closed": m.get("closed"), "uma_status": m.get("umaResolutionStatus"),
            "outcome_prices": m.get("outcomePrices"), "closed_time": m.get("closedTime"),
        })
        resolved_at = None
        if state.resolved:
            resolved_at = state.resolved_at or self._res.parse_gamma_timestamp(m.get("endDate"))
        prices = self._res._coerce_prices(m.get("outcomePrices"))
        clob_ids = m.get("clobTokenIds")
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except ValueError:
                clob_ids = None
        result = {
            "resolved": state.resolved, "outcome": state.outcome, "resolved_at": resolved_at, "prices": prices,
            "gamma": {"slug": m.get("slug"), "question": m.get("question"), "closed": m.get("closed"),
                      "uma_status": m.get("umaResolutionStatus"), "end_date": m.get("endDate"),
                      "closed_time": m.get("closedTime"), "clob_token_ids": clob_ids},
        }
        self.cache[condition_id] = result
        return result


# =========================================================================== #
# DB
# =========================================================================== #

def load_env(explicit: Optional[str] = None) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:            # env may already be exported (cron)
        return
    candidates = [explicit, os.getenv("RECON_ENV_FILE"), Path.home() / ".claude" / "credentials" / ".env",
                  REPO_ROOT / ".env", Path.home() / "ai" / "polymarket-team" / "data" / "credentials" / ".env"]
    for c in candidates:
        if c and Path(c).exists():
            load_dotenv(Path(c))


def resolve_db_host() -> str:
    """Local LAN, then Tailscale, then DNS — first host that accepts a TCP connect."""
    explicit = os.getenv("HOME_DB_HOST") or os.getenv("POSTGRES_HOST")
    candidates = [c for c in [explicit, "192.168.0.166", os.getenv("HOME_DB_HOST_TAILSCALE", "100.92.27.16"),
                              os.getenv("HOME_DB_HOST_DNS", "ebertx.duckdns.org")] if c]
    port = int(os.getenv("HOME_DB_PORT") or os.getenv("POSTGRES_PORT") or 5432)
    for host in candidates:
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
            return host
        except OSError:
            continue
    return candidates[0]


def get_connection():
    import psycopg2
    return psycopg2.connect(
        host=resolve_db_host(),
        port=os.getenv("HOME_DB_PORT") or os.getenv("POSTGRES_PORT") or 5432,
        user=os.getenv("HOME_DB_USER") or os.getenv("POSTGRES_USER"),
        password=os.getenv("HOME_DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD"),
        dbname=os.getenv("POLYBOT_DB_NAME") or os.getenv("POSTGRES_DATABASE") or "polybot",
        sslmode=os.getenv("POSTGRES_SSLMODE", "prefer"),
        connect_timeout=15,
    )


def load_db_positions(conn) -> List[dict]:
    from psycopg2.extras import RealDictCursor
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT p.id, p.market_id, p.direction::text AS direction, p.shares, p.entry_price, p.entry_date,
               p.exit_price, p.exit_date, p.current_price, p.current_value, p.unrealized_pnl, p.realized_pnl,
               p.cost_basis, p.status::text AS status, p.exit_reasoning,
               m.slug, m.title, m.condition_id, m.clob_token_id_yes AS token_yes, m.clob_token_id_no AS token_no
        FROM positions p LEFT JOIN markets m ON m.id = p.market_id
        ORDER BY p.id
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def load_snapshots(conn, year: int) -> Tuple[Optional[dict], Optional[dict]]:
    from psycopg2.extras import RealDictCursor
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""SELECT timestamp, cash_balance, position_value, total_value FROM portfolio_snapshots
                   WHERE timestamp >= %s ORDER BY timestamp ASC LIMIT 1""",
                (datetime(year, 1, 1, tzinfo=timezone.utc),))
    first = cur.fetchone()
    cur.execute("""SELECT timestamp, cash_balance, position_value, total_value FROM portfolio_snapshots
                   ORDER BY timestamp DESC LIMIT 3""")
    latest_rows = cur.fetchall()
    cur.close()
    latest = None
    # skip the tracker's transient "cash=0" hiccup snapshot (see fetch_portfolio_db.py)
    for r in latest_rows:
        if dec(r["cash_balance"]) > Q2 or dec(r["total_value"]) <= ZERO:
            latest = r
            break
    if latest is None and latest_rows:
        latest = latest_rows[0]
    return (dict(first) if first else None, dict(latest) if latest else None)


def load_market_ids_by_condition(conn) -> Dict[str, int]:
    cur = conn.cursor()
    cur.execute("SELECT condition_id, id FROM markets WHERE condition_id IS NOT NULL")
    out = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    return out


def execute_plan(conn, plan: List[Action], log=print) -> None:
    """Apply every action inside one transaction; raise (and roll back) on any error."""
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    try:
        for a in plan:
            if a.kind in ("update_closed", "update_open"):
                if a.kind == "update_closed":
                    cur.execute("""UPDATE positions
                                   SET realized_pnl = %s, exit_price = COALESCE(%s, exit_price),
                                       exit_reasoning = COALESCE(exit_reasoning, '') || %s, updated_at = %s
                                   WHERE id = %s AND status = 'closed'""",
                                (a.new_realized, a.exit_price, a.exit_reasoning_suffix, now, a.position_id))
                else:
                    cur.execute("""UPDATE positions SET realized_pnl = %s, updated_at = %s
                                   WHERE id = %s AND status = 'open'""",
                                (a.new_realized, now, a.position_id))
                if cur.rowcount != 1:
                    raise RuntimeError(f"{a.kind} for position {a.position_id} touched {cur.rowcount} rows; aborting")
                log(f"  {a.kind}: position {a.position_id} realized {a.old_realized} -> {a.new_realized}")
            elif a.kind == "link_market":
                m = a.market or {}
                cur.execute("""UPDATE markets
                               SET condition_id = COALESCE(condition_id, %s),
                                   clob_token_id_yes = COALESCE(clob_token_id_yes, %s),
                                   clob_token_id_no = COALESCE(clob_token_id_no, %s), updated_at = %s
                               WHERE id = %s""",
                            (m.get("condition_id") or None, m.get("clob_token_id_yes"), m.get("clob_token_id_no"),
                             now, a.market_id))
                log(f"  link_market: market {a.market_id} <- condition {str(m.get('condition_id'))[:12]}… (position {a.position_id})")
            elif a.kind == "insert":
                market_id = a.market_id
                if market_id is None:
                    m = a.market
                    cur.execute("""INSERT INTO markets (slug, title, condition_id, clob_token_id_yes, clob_token_id_no, end_date)
                                   VALUES (%s, %s, %s, %s, %s, NULL)
                                   ON CONFLICT (slug) DO UPDATE SET
                                       condition_id = COALESCE(markets.condition_id, EXCLUDED.condition_id),
                                       clob_token_id_yes = COALESCE(markets.clob_token_id_yes, EXCLUDED.clob_token_id_yes),
                                       clob_token_id_no = COALESCE(markets.clob_token_id_no, EXCLUDED.clob_token_id_no),
                                       updated_at = now()
                                   RETURNING id""",
                                (m["slug"], m["title"], m["condition_id"], m["clob_token_id_yes"], m["clob_token_id_no"]))
                    market_id = cur.fetchone()[0]
                    log(f"  insert market {market_id}: {m['slug'][:60]}")
                p = a.position
                cur.execute("""INSERT INTO positions
                               (market_id, direction, shares, entry_price, entry_date, exit_price, exit_date,
                                current_price, current_value, unrealized_pnl, realized_pnl, cost_basis, status,
                                exit_reasoning, api_miss_count, created_at, updated_at)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                               RETURNING id""",
                            (market_id, p["direction"], p["shares"], p["entry_price"], p["entry_date"], p["exit_price"],
                             p["exit_date"], p["current_price"], p["current_value"], p["unrealized_pnl"],
                             p["realized_pnl"], p["cost_basis"], p["status"], p["exit_reasoning"], p["api_miss_count"],
                             now, now))
                pid = cur.fetchone()[0]
                log(f"  insert position {pid}: {a.title[:50]} {p['direction']} realized {p['realized_pnl']}")
            else:
                raise RuntimeError(f"unknown action kind {a.kind}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


# =========================================================================== #
# Report
# =========================================================================== #

def money(v: Optional[Decimal]) -> str:
    return "     -" if v is None else f"{v:>9.2f}"


def print_report(ledgers: LedgerResult, joined: Optional[JoinResult], year: int, *, source: str,
                 identities: Dict[str, Optional[dict]], unpriced: List[AssetLedger],
                 carried: Optional[List[CarryPosition]] = None, log=print) -> None:
    assets = ledgers.assets
    statuses = {}
    for led in assets.values():
        statuses[led.status] = statuses.get(led.status, 0) + 1
    log("=" * 100)
    log(f"REBUILD REALIZED FROM ACTIVITY FEED — year {year}")
    log("=" * 100)
    log(f"Feed: {ledgers.rows_processed} rows from {source}")
    log(f"Ledger: {len(assets)} assets ({', '.join(f'{k}={v}' for k, v in sorted(statuses.items()))}); "
        f"gamma lookups={ledgers.gamma_lookups}; unmatched redeems={len(ledgers.unmatched_redeems)}; "
        f"no-op redeems={ledgers.noop_redeems}")
    log(f"Yield/reward income {year}: ${ledgers.yield_income(year):.4f} "
        f"({sum(1 for f in ledgers.yield_events if year_of(f.ts) == year)} rows); all years ${ledgers.yield_income():.4f}")
    all_years = sorted({y for led in assets.values() for y in led.realized_by_year})
    log("Fills realized by year: " + ", ".join(
        f"{y}: ${sum((led.realized(y) for led in assets.values()), ZERO):.2f}" for y in all_years))

    if joined is None:
        log("\n(no DB join — --no-db)")
        active = [led for led in assets.values() if led.active_in(year)]
        log(f"\n== Assets active in {year} ({len(active)}) ==")
        log(f"{'asset':>8}  {'title':<46} {'dir':<3} {'net_sh':>8} {'fills':>9}  status")
        for led in sorted(active, key=lambda l: -abs(l.realized(year))):
            log(f"{led.asset[:8]:>8}  {led.title[:46]:<46} {led.direction:<3} {led.shares:>8.2f} "
                f"{money(led.realized(year))}  {led.status}")
        return

    log(f"\n== Per-asset: fills realized {year} vs DB realized ({len(joined.per_asset)} assets, sorted by |diff|) ==")
    log(f"{'asset':>8}  {'title':<44} {'dir':<3} {'net_sh':>8} {'fills':>9} {'db':>9} {'diff':>8}  db_ids/status  join  flags")
    for rep in sorted(joined.per_asset, key=lambda r: (-abs(r.diff), r.title)):
        ids = ",".join(f"{i}{s[:1]}" for i, s in zip(rep.db_ids, rep.db_statuses)) or "-"
        flags = ("; ".join(rep.flags))[:60]
        log(f"{rep.asset[:8]:>8}  {rep.title[:44]:<44} {rep.direction:<3} {rep.net_shares:>8.2f} "
            f"{money(rep.fills_realized)} {money(rep.db_realized)} {rep.diff:>8.2f}  {ids:<13} {rep.join_kind[:9]:<9} {flags}")

    log(f"\n== Totals {year} ==")
    log(f"  fills realized: ${joined.total_fills_realized:>9.2f}")
    log(f"  DB realized   : ${joined.total_db_realized:>9.2f}   (rows in scope: open + closed with exit_date in {year})")
    log(f"  diff          : ${joined.total_fills_realized - joined.total_db_realized:>9.2f}")
    log(f"  max |diff| on any asset/row: ${joined.max_abs_diff:.2f}")

    log(f"\n== Backfill candidates: feed assets with {year} activity and no DB row ({len(joined.backfill_candidates)}) ==")
    for rep in joined.backfill_candidates:
        led = rep.ledger
        log(f"  {rep.asset[:8]}  {rep.title[:60]:<60} {rep.direction:<3} bought {led.total_bought:.2f} @ {led.entry_avg:.4f} "
            f"net {rep.net_shares:.2f} realized {rep.fills_realized:.2f} status={led.status} "
            f"{'(insertable)' if led.status != 'open' and abs(rep.fills_realized) >= MONEY_EPS else '(not inserted)'}")

    log(f"\n== Suspect DB rows: in-scope rows with no feed activity ({len(joined.suspect_db_rows)}) ==")
    for r in joined.suspect_db_rows:
        log(f"  id={r['id']:<4} {(r.get('title') or r.get('slug') or '')[:55]:<55} {r.get('direction')} "
            f"status={r.get('status')} shares={dec(r.get('shares')):.2f} realized={dec(r.get('realized_pnl')):.2f} "
            f"token={'y' if _row_token(r) else 'n'} cond={'y' if r.get('condition_id') else 'n'}")
        if not _row_token(r) and not r.get("condition_id"):
            same_dir = [c for c in joined.backfill_candidates
                        if c.direction == (r.get("direction") or "").lower() and c.ledger.status != "open"]
            hints = sorted(same_dir, key=lambda c: abs(c.fills_realized - dec(r.get("realized_pnl"))))[:3]
            if hints:
                log("        possible feed match (use --link ASSET_PREFIX=" + str(r["id"]) + "): "
                    + "; ".join(f"{c.asset[:8]} '{c.title[:38]}' realized {c.fills_realized:.2f}" for c in hints))

    log(f"\n== Heuristic links (DB rows without ids matched by direction/realized/shares) ({len(joined.heuristic_links)}) ==")
    for asset, rid in joined.heuristic_links:
        log(f"  asset {asset[:8]} <-> position {rid}")

    log(f"\n== Open DB rows carrying unbooked partial realized ({len(joined.open_partial)}) ==")
    for rep in joined.open_partial:
        log(f"  {rep.asset[:8]}  {rep.title[:55]:<55} db_ids={rep.db_ids} fills={rep.fills_realized:.2f} db={rep.db_realized:.2f}")

    open_leds = [led for led in ledgers.assets.values() if led.status == "open" and led.shares > SHARE_EPS]
    log(f"\n== Open positions from fills ({len(open_leds)}) ==")
    log(f"{'asset':>8}  {'title':<50} {'dir':<3} {'shares':>8} {'avg':>7} {'mark':>7} {'unreal':>8}")
    for led in sorted(open_leds, key=lambda l: l.title):
        u = led.unrealized()
        log(f"{led.asset[:8]:>8}  {led.title[:50]:<50} {led.direction:<3} {led.shares:>8.2f} {led.avg_cost:>7.4f} "
            f"{(f'{led.mark_price:.4f}' if led.mark_price is not None else '   -   '):>7} {money(u)}")
    if unpriced:
        log("  unpriced (excluded from unrealized): " + ", ".join(l.asset[:8] for l in unpriced))

    log(f"\n== Rows outside {year} (ignored) : {len(joined.rows_outside_year)} ==")

    flagged = [led for led in ledgers.assets.values() if led.flags and led.active_in(year)]
    log(f"\n== Ledger flags ({len(flagged)} assets) ==")
    for led in flagged:
        for f in led.flags:
            log(f"  {led.asset[:8]} {led.title[:40]:<40} {f}")
    for r in ledgers.unmatched_redeems:
        log(f"  unmatched REDEEM: cond={str(r.get('conditionId'))[:12]} idx={r.get('outcomeIndex')} "
            f"usdc={r.get('usdcSize')} {str(r.get('title'))[:40]}")

    log(f"\n== Positions carried into {year} (held on Jan 1; realized in-year is measured from cost) ({len(carried or [])}) ==")
    for c in carried or []:
        g = c.gain
        log(f"  {c.asset[:8]}  {c.ledger.title[:50]:<50} {c.ledger.direction:<3} {c.shares:>8.2f}sh cost {c.cost:.2f} "
            f"Jan-1 mark {(f'{c.mark:.4f}' if c.mark is not None else 'n/a (assumed at cost)')} "
            f"embedded gain {(f'{g:+.2f}' if g is not None else 'n/a')}")

    log("\n== Identities ==")
    c = identities.get("cash")
    if c:
        log(f"  cash: {c['start_ts']:%Y-%m-%d %H:%M} cash {c['start_cash']:.2f} - buys {c['buys']:.2f} + sells {c['sells']:.2f} "
            f"+ redeems {c['redeems']:.2f} + yield {c['yield']:.4f} = {c['expected_end_cash']:.2f}; "
            f"snapshot {c['end_ts']:%Y-%m-%d %H:%M} cash {c['end_cash']:.2f}; residual {c['residual']:+.2f}")
    else:
        log("  cash: n/a (no snapshots)")
    e = identities.get("equity")
    if e:
        log(f"  equity: start {e['start_capital']:.2f} + fills realized {e['fills_realized']:.2f} "
            f"- carry gain {e['carry_gain']:.2f} + yield {e['yield']:.4f} + unrealized {e['unrealized']:.2f} "
            f"= {e['expected_total_value']:.2f}; latest total_value {e['latest_total_value']:.2f}; "
            f"residual {e['residual']:+.2f}")
        log(f"  economic {year} P&L (fills realized - carry gain, i.e. measured from Jan-1 marks): "
            f"${e['fills_realized'] - e['carry_gain']:.2f}")
        log("  (a cash residual means flows the feed does not show — fees, deposits/withdrawals, USDC locked in "
            "resting BUY orders — or the window-start snapshot lagging a fill; snapshot cash is free CLOB collateral)")
    else:
        log("  equity: n/a (no snapshots)")


def print_plan(plan: List[Action], log=print) -> None:
    log("\n== APPLY PLAN (before -> after) ==")
    if not plan:
        log("  nothing to change")
        return
    for a in plan:
        if a.kind in ("update_closed", "update_open"):
            log(f"  {a.kind:<13} position {a.position_id:<4} {a.title[:45]:<45} realized {a.old_realized:>8.2f} -> {a.new_realized:>8.2f}"
                + (f"  exit_price -> {a.exit_price}" if a.exit_price is not None else "")
                + (f"  reasoning += '{a.exit_reasoning_suffix.strip()}'" if a.exit_reasoning_suffix else ""))
        elif a.kind == "link_market":
            log(f"  {a.kind:<13} market {a.market_id:<4} (position {a.position_id}) {a.title[:45]:<45} "
                f"set condition_id/token ids where NULL")
        elif a.kind == "insert":
            p = a.position
            mk = f"new market '{a.market['slug'][:40]}'" if a.market else f"market {a.market_id}"
            log(f"  {a.kind:<13} {mk}: {a.title[:45]:<45} {p['direction']} {p['shares']:.2f}sh @ {p['entry_price']} "
                f"exit {p['exit_price']} realized {p['realized_pnl']:.2f} {p['entry_date']:%Y-%m-%d} -> "
                f"{p['exit_date']:%Y-%m-%d}")
    log(f"  {len(plan)} action(s)")


def dump_json(path: str, ledgers: LedgerResult, joined: Optional[JoinResult], year: int,
              identities: Dict[str, Optional[dict]]) -> None:
    def conv(o):
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError(str(type(o)))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "year": year,
        "assets": {a: led.as_dict() for a, led in ledgers.assets.items()},
        "yield_by_year": {str(y): str(v) for y, v in sorted(ledgers.yield_by_year.items())},
        "unmatched_redeems": ledgers.unmatched_redeems,
        "identities": identities,
    }
    if joined:
        payload["join"] = {
            "per_asset": [{"asset": r.asset, "title": r.title, "direction": r.direction, "status": r.status,
                           "net_shares": r.net_shares, "fills_realized": r.fills_realized, "db_realized": r.db_realized,
                           "diff": r.diff, "db_ids": r.db_ids, "db_statuses": r.db_statuses, "join_kind": r.join_kind,
                           "flags": r.flags} for r in joined.per_asset],
            "backfill_candidates": [r.asset for r in joined.backfill_candidates],
            "suspect_db_rows": [{k: v for k, v in r.items()} for r in joined.suspect_db_rows],
            "open_partial": [r.asset for r in joined.open_partial],
            "totals": {"fills_realized": joined.total_fills_realized, "db_realized": joined.total_db_realized,
                       "max_abs_diff": joined.max_abs_diff},
        }
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1, default=conv)


# =========================================================================== #
# main
# =========================================================================== #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from-file", help="read the activity feed from a saved JSON list instead of the Data API")
    p.add_argument("--wallet", default=os.getenv("POLYMARKET_WALLET", DEFAULT_WALLET))
    p.add_argument("--year", type=int, default=datetime.now(timezone.utc).year)
    p.add_argument("--check", action="store_true", help="one-line RECON OK/FAIL, exit 1 on FAIL")
    p.add_argument("--tolerance", type=Decimal, default=Decimal("2.00"),
                   help="max |equity residual| and max per-row |diff| for RECON OK (default 2.00)")
    p.add_argument("--apply", action="store_true", help="write corrections to the DB (one transaction)")
    p.add_argument("--yes", action="store_true", help="skip the interactive confirmation for --apply")
    p.add_argument("--json", dest="json_path", help="dump the per-asset ledger + join to this path")
    p.add_argument("--start-capital", type=Decimal, default=Decimal("229.13"),
                   help="starting capital on Jan 1 of --year for the equity identity")
    p.add_argument("--no-gamma", action="store_true",
                   help="no Polymarket lookups: skip Gamma resolution of unredeemed remainders and CLOB year-start marks")
    p.add_argument("--link", action="append", default=[], metavar="ASSET_PREFIX=POSITION_ID",
                   help="force-join a feed asset (token id prefix) to a DB position row; repeatable")
    p.add_argument("--no-db", action="store_true", help="skip the DB entirely (ledger only)")
    p.add_argument("--env-file", help="explicit .env with HOME_DB_* / POSTGRES_* credentials")
    p.add_argument("--save-feed", help="write the fetched activity feed to this JSON path")
    p.add_argument("--quiet", action="store_true", help="suppress progress lines (report still prints)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and args.check:
        print("--apply and --check are mutually exclusive", file=sys.stderr)
        return 2
    progress = (lambda *a, **k: None) if args.quiet or args.check else print
    load_env(args.env_file)
    year = args.year
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manual_links: Dict[str, int] = {}
    for spec in args.link:
        try:
            prefix, pid = spec.split("=", 1)
            manual_links[prefix.strip()] = int(pid)
        except ValueError:
            print(f"bad --link {spec!r}; expected ASSET_PREFIX=POSITION_ID", file=sys.stderr)
            return 2

    # 1. feed
    if args.from_file:
        with open(args.from_file) as fh:
            rows = json.load(fh)
        source = args.from_file
    else:
        progress(f"Fetching activity feed for {args.wallet} …")
        rows = fetch_activity(args.wallet, log=progress)
        source = f"Data API ({args.wallet[:10]}…)"
        if args.save_feed:
            with open(args.save_feed, "w") as fh:
                json.dump(rows, fh)
    rows = [r for r in rows if (r.get("proxyWallet") or args.wallet).lower() == args.wallet.lower()]

    # 2. ledger
    resolver = None if args.no_gamma else GammaResolver(log=progress)
    ledgers = build_ledgers(rows, resolver=resolver)

    # 3. DB join + identities
    joined: Optional[JoinResult] = None
    identities: Dict[str, Optional[dict]] = {"cash": None, "equity": None}
    unpriced: List[AssetLedger] = []
    carried = carry_positions(ledgers, year)
    if carried and not args.no_gamma:
        boundary = datetime(year, 1, 1, tzinfo=timezone.utc)
        for c in carried:
            c.mark = fetch_year_start_mark(c.asset, year)
            time.sleep(GAMMA_SLEEP_S)
            if c.mark is None and resolver is not None:
                # market resolved before the boundary: the mark is the payout
                c.mark = carry_mark_from_resolution(resolver(c.ledger.condition_id), c.ledger.direction, boundary)
    carry_gain = sum((c.gain for c in carried if c.gain is not None), ZERO)
    conn = None
    if not args.no_db:
        conn = get_connection()
        db_rows = load_db_positions(conn)
        first_snap, latest_snap = load_snapshots(conn, year)
        joined = join_ledgers(ledgers, db_rows, year=year, manual_links=manual_links)

        # mark open remainders with the DB's current_price when we have it
        price_by_token: Dict[str, Decimal] = {}
        for r in db_rows:
            tok = _row_token(r)
            if tok and (r.get("status") or "").lower() == "open" and r.get("current_price") is not None:
                price_by_token[tok] = dec(r["current_price"])
        unrealized_total = ZERO
        for led in ledgers.assets.values():
            if led.status == "open" and led.shares > SHARE_EPS:
                if led.asset in price_by_token:
                    led.mark_price = price_by_token[led.asset]
                u = led.unrealized()
                if u is None:
                    unpriced.append(led)
                else:
                    unrealized_total += u

        if first_snap and latest_snap:
            identities["cash"] = cash_identity(
                ledgers, start_cash=dec(first_snap["cash_balance"]), start_ts=first_snap["timestamp"],
                end_ts=latest_snap["timestamp"], end_cash=dec(latest_snap["cash_balance"]))
            identities["equity"] = equity_identity(
                start_capital=args.start_capital, fills_realized=joined.total_fills_realized,
                yield_income=ledgers.yield_income(year), unrealized=unrealized_total,
                latest_total_value=dec(latest_snap["total_value"]), carry_gain=carry_gain)

    # 4. outputs
    if args.json_path:
        dump_json(args.json_path, ledgers, joined, year, identities)

    if args.check:
        if joined is None or identities["equity"] is None:
            print(f"RECON FAIL {year}: no DB join / snapshots available")
            return 1
        eq = identities["equity"]["residual"]
        worst = max(joined.per_asset + [], key=lambda r: abs(r.diff), default=None)
        problems = []
        if abs(eq) > args.tolerance:
            problems.append(f"equity residual {eq:+.2f}")
        if joined.max_abs_diff > args.tolerance:
            who = f"{worst.title[:40]} ({worst.diff:+.2f})" if worst and abs(worst.diff) == joined.max_abs_diff else "suspect DB row"
            problems.append(f"max row diff {joined.max_abs_diff:.2f} on {who}")
        summary = (f"fills={joined.total_fills_realized:.2f} db={joined.total_db_realized:.2f} "
                   f"equity_resid={eq:+.2f} max_diff={joined.max_abs_diff:.2f} "
                   f"backfill={len(joined.backfill_candidates)} suspect={len(joined.suspect_db_rows)} "
                   f"open_partial={len(joined.open_partial)} tol={args.tolerance}")
        if problems:
            print(f"RECON FAIL {year}: {'; '.join(problems)} | {summary}")
            return 1
        print(f"RECON OK {year}: {summary}")
        return 0

    print_report(ledgers, joined, year, source=source, identities=identities, unpriced=unpriced, carried=carried)

    if args.apply:
        if joined is None or conn is None:
            print("--apply needs the DB (drop --no-db)", file=sys.stderr)
            return 2
        if joined.suspect_db_rows:
            print(f"\nWARNING: {len(joined.suspect_db_rows)} suspect DB row(s) are not joined to any feed asset; "
                  f"if any of them is really one of the backfill candidates, abort and re-run with --link "
                  f"ASSET_PREFIX=POSITION_ID so no duplicate row is inserted.")
        plan = plan_corrections(joined, ledgers, year=year, today=today,
                                existing_market_ids_by_condition=load_market_ids_by_condition(conn))
        print_plan(plan)
        if not plan:
            return 0
        if not args.yes:
            answer = input("\nType 'apply' to write these changes to the DB: ").strip()
            if answer != "apply":
                print("aborted; nothing written")
                return 1
        execute_plan(conn, plan)
        print(f"\nApplied {len(plan)} action(s). Re-run without --apply to verify.")
    elif joined is not None:
        plan = plan_corrections(joined, ledgers, year=year, today=today, existing_market_ids_by_condition={})
        print(f"\n(dry run) --apply would perform {len(plan)} action(s): "
              + ", ".join(f"{k}={sum(1 for a in plan if a.kind == k)}" for k in
                          ("update_closed", "update_open", "insert", "link_market")))

    if conn is not None:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
