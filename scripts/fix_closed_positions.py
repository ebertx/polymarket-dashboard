#!/usr/bin/env python3
"""
Fix positions that were incorrectly auto-closed on 2026-02-10 due to
API sync failure in _sync_positions().

Also adds the missing "Inflation >3% 2026" position.

Run with --apply to actually make changes. Without it, shows what would change.
"""
import argparse
import asyncio
from decimal import Decimal
from datetime import datetime, timezone

# Must run from /app directory with PYTHONPATH set
from app.database import AsyncSessionLocal
from sqlalchemy import text


# Positions to reopen (id, current_shares, current_entry_price)
# Share counts from live API as of 2026-03-13
POSITIONS_TO_REOPEN = [
    # (db_id, api_shares, entry_price_per_share)
    (1, 18, Decimal("0.830")),        # Greenland NO
    (3, 18, Decimal("0.270")),        # Unemployment YES (was 27, sold 9)
    (6, 22, Decimal("0.450")),        # 2026 Temp 4th Place YES
    (8, 140, Decimal("0.044")),       # Venezuela Cabello YES
    (11, 65, Decimal("0.046")),       # Oscar Moura YES  (id 12 in markets table)
    (19, 24, Decimal("0.118")),       # Fed 1 Cut YES (was 62, sold 38)
    (20, 16, Decimal("0.740")),       # Fed No Change Apr YES
]

# New position to add: Inflation >3% 2026
INFLATION_MARKET = {
    "title": "Inflation >3% 2026",
    "condition_id": "0xcd01256212f10704b528e9c86dba7fb9e55a5eeab4a7d7051e25512bb2636b2d",
    "clob_token_id_yes": "6478474356831359523032969262014082686922886129187075898439624325202312555669",
    "clob_token_id_no": "36378311265709846060859255962129667010780913173065483696749977329500251096213",
    "slug": "will-inflation-reach-more-than-3-in-2026",
    "end_date": "2026-12-31",
}

INFLATION_POSITION = {
    "direction": "yes",
    "shares": Decimal("25"),
    "entry_price": Decimal("0.400"),
    "cost_basis": Decimal("10.00"),  # 25 * 0.40
}


async def main(apply: bool):
    async with AsyncSessionLocal() as db:
        print("=" * 70)
        print("Position Recovery Script")
        print("=" * 70)

        # 1. Reopen incorrectly closed positions
        print("\n--- Reopening incorrectly closed positions ---\n")
        for pos_id, api_shares, entry_price in POSITIONS_TO_REOPEN:
            r = await db.execute(text(
                "SELECT p.id, m.title, p.status, p.shares, p.entry_price, p.cost_basis "
                "FROM positions p LEFT JOIN markets m ON p.market_id = m.id "
                "WHERE p.id = :pid"
            ), {"pid": pos_id})
            row = r.fetchone()
            if not row:
                print(f"  [SKIP] Position {pos_id} not found in DB")
                continue

            db_id, title, status, db_shares, db_entry, db_cost = row
            new_cost_basis = api_shares * entry_price

            print(f"  Position {db_id}: {title}")
            print(f"    Status: {status} -> open")
            print(f"    Shares: {db_shares} -> {api_shares}")
            print(f"    Cost basis: {db_cost} -> {new_cost_basis}")

            if apply:
                await db.execute(text("""
                    UPDATE positions SET
                        status = 'open',
                        shares = :shares,
                        cost_basis = :cost_basis,
                        exit_date = NULL,
                        exit_price = NULL,
                        exit_reasoning = NULL,
                        realized_pnl = 0,
                        current_value = 0,
                        unrealized_pnl = 0
                    WHERE id = :pid
                """), {
                    "pid": pos_id,
                    "shares": float(api_shares),
                    "cost_basis": float(new_cost_basis),
                })
                print(f"    [APPLIED]")
            else:
                print(f"    [DRY RUN]")

        # 2. Add missing Inflation >3% 2026 market and position
        print("\n--- Adding missing Inflation >3% 2026 ---\n")

        # Check if market already exists
        r = await db.execute(text(
            "SELECT id FROM markets WHERE condition_id = :cid"
        ), {"cid": INFLATION_MARKET["condition_id"]})
        existing = r.fetchone()

        if existing:
            market_id = existing[0]
            print(f"  Market already exists (id={market_id})")
        else:
            print(f"  Creating market: {INFLATION_MARKET['title']}")
            if apply:
                r = await db.execute(text("""
                    INSERT INTO markets (title, slug, condition_id, clob_token_id_yes, clob_token_id_no,
                                        end_date, created_at)
                    VALUES (:title, :slug, :cid, :token_yes, :token_no, :end_date, NOW())
                    RETURNING id
                """), {
                    "title": INFLATION_MARKET["title"],
                    "slug": INFLATION_MARKET["slug"],
                    "cid": INFLATION_MARKET["condition_id"],
                    "token_yes": INFLATION_MARKET["clob_token_id_yes"],
                    "token_no": INFLATION_MARKET["clob_token_id_no"],
                    "end_date": INFLATION_MARKET["end_date"],
                })
                market_id = r.fetchone()[0]
                print(f"  [APPLIED] Market created (id={market_id})")
            else:
                market_id = None
                print(f"  [DRY RUN]")

        # Check if position already exists
        if market_id:
            r = await db.execute(text(
                "SELECT id FROM positions WHERE market_id = :mid AND status = 'open'"
            ), {"mid": market_id})
            existing_pos = r.fetchone()
        else:
            existing_pos = None

        if existing_pos:
            print(f"  Position already exists (id={existing_pos[0]})")
        else:
            print(f"  Creating position: {INFLATION_POSITION['shares']} YES @ {INFLATION_POSITION['entry_price']}")
            if apply and market_id:
                await db.execute(text("""
                    INSERT INTO positions (market_id, direction, shares, entry_price, cost_basis,
                                          status, entry_date, current_price, current_value, unrealized_pnl)
                    VALUES (:mid, :direction, :shares, :entry_price, :cost_basis,
                            'open', '2026-03-06', 0.815, :current_value, :unrealized_pnl)
                """), {
                    "mid": market_id,
                    "direction": INFLATION_POSITION["direction"],
                    "shares": float(INFLATION_POSITION["shares"]),
                    "entry_price": float(INFLATION_POSITION["entry_price"]),
                    "cost_basis": float(INFLATION_POSITION["cost_basis"]),
                    "current_value": float(Decimal("25") * Decimal("0.815")),
                    "unrealized_pnl": float(Decimal("25") * Decimal("0.815") - Decimal("10.00")),
                })
                print(f"  [APPLIED]")
            else:
                print(f"  [DRY RUN]")

        if apply:
            await db.commit()
            print("\n[COMMITTED] All changes saved to database.")
        else:
            print("\n[DRY RUN] No changes made. Run with --apply to execute.")

        # 3. Show final state
        print("\n--- Current position status ---\n")
        r = await db.execute(text(
            "SELECT p.id, substr(m.title, 1, 40), p.status, p.shares, p.entry_price "
            "FROM positions p LEFT JOIN markets m ON p.market_id = m.id "
            "ORDER BY p.status, p.id"
        ))
        for row in r.fetchall():
            print(f"  id={row[0]:3} {row[1] or '?':40} status={row[2]:6} shares={row[3]:10} entry={row[4]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually apply changes")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
