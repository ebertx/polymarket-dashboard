#!/usr/bin/env python3
"""
One-time migration script to mark resolved positions as closed.

Finds positions where:
- Position status is 'open'
- Market end_date has passed

And marks them as closed with appropriate realized P&L.

Usage:
    python scripts/fix_resolved_positions.py           # Dry run (show what would change)
    python scripts/fix_resolved_positions.py --apply   # Actually apply changes
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Load credentials
central_env = Path.home() / ".claude" / "credentials" / ".env"
local_env = Path(__file__).parent.parent / ".env"

if central_env.exists():
    load_dotenv(central_env)
else:
    load_dotenv(local_env)


def get_connection():
    """Get database connection."""
    return psycopg2.connect(
        host=os.getenv("HOME_DB_HOST") or os.getenv("POSTGRES_HOST"),
        port=os.getenv("HOME_DB_PORT") or os.getenv("POSTGRES_PORT", 5432),
        user=os.getenv("HOME_DB_USER") or os.getenv("POSTGRES_USER"),
        password=os.getenv("HOME_DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD"),
        dbname=os.getenv("POLYBOT_DB_NAME") or os.getenv("POSTGRES_DATABASE", "polybot"),
        sslmode=os.getenv("POSTGRES_SSLMODE", "prefer")
    )


def find_resolved_positions(conn):
    """Find positions that should be closed but aren't."""
    cur = conn.cursor(cursor_factory=RealDictCursor)

    now = datetime.now(timezone.utc)

    cur.execute("""
        SELECT
            p.id as position_id,
            p.direction,
            p.shares,
            p.entry_price,
            p.cost_basis,
            p.current_price,
            p.current_value,
            p.status,
            m.id as market_id,
            m.title as market_title,
            m.end_date,
            m.resolved_at,
            m.resolution_outcome
        FROM positions p
        JOIN markets m ON p.market_id = m.id
        WHERE p.status = 'open'
        AND (m.end_date <= %s OR m.resolved_at IS NOT NULL)
        ORDER BY m.end_date
    """, (now,))

    positions = cur.fetchall()
    cur.close()
    return positions


def calculate_pnl(position):
    """Calculate realized P&L for a position."""
    shares = Decimal(str(position['shares']))
    cost_basis = Decimal(str(position['cost_basis']))
    direction = position['direction']
    resolution_outcome = position['resolution_outcome']

    # Determine payout
    if resolution_outcome is not None:
        # Case-insensitive comparison
        outcome_lower = resolution_outcome.lower() if resolution_outcome else None
        direction_lower = direction.lower() if direction else None
        won = outcome_lower == direction_lower
        payout = shares * Decimal("1.0") if won else Decimal("0")
        exit_price = Decimal("1.0") if won else Decimal("0")
    else:
        # Outcome unknown - use last known price or assume loss
        last_price = Decimal(str(position['current_price'] or 0))
        payout = shares * last_price
        exit_price = last_price

    realized_pnl = payout - cost_basis

    return {
        'exit_price': exit_price,
        'realized_pnl': realized_pnl,
        'payout': payout,
    }


def apply_fixes(conn, positions):
    """Apply fixes to mark positions as closed."""
    cur = conn.cursor()
    now = datetime.now(timezone.utc)

    for pos in positions:
        pnl_info = calculate_pnl(pos)
        exit_date = pos['resolved_at'] or pos['end_date'] or now

        cur.execute("""
            UPDATE positions
            SET
                status = 'closed',
                exit_date = %s,
                exit_price = %s,
                realized_pnl = %s,
                current_value = 0,
                unrealized_pnl = 0,
                updated_at = %s
            WHERE id = %s
        """, (
            exit_date,
            pnl_info['exit_price'],
            pnl_info['realized_pnl'],
            now,
            pos['position_id']
        ))

        print(f"  Fixed position {pos['position_id']}: {pos['market_title'][:50]}")
        print(f"    Realized P&L: ${pnl_info['realized_pnl']:.2f}")

    conn.commit()
    cur.close()


def main():
    parser = argparse.ArgumentParser(description="Fix resolved positions in database")
    parser.add_argument("--apply", action="store_true", help="Actually apply changes (default is dry run)")
    args = parser.parse_args()

    conn = get_connection()

    print("=" * 60)
    print("Finding resolved positions that are still marked as 'open'...")
    print("=" * 60)

    positions = find_resolved_positions(conn)

    if not positions:
        print("\nNo positions need fixing. All resolved markets are properly closed.")
        conn.close()
        return

    print(f"\nFound {len(positions)} position(s) to fix:\n")

    total_realized_pnl = Decimal("0")

    for pos in positions:
        pnl_info = calculate_pnl(pos)
        total_realized_pnl += pnl_info['realized_pnl']

        print(f"Position {pos['position_id']}: {pos['market_title'][:50]}...")
        print(f"  Direction: {pos['direction'].upper()}")
        print(f"  Shares: {pos['shares']}")
        print(f"  Entry: ${pos['entry_price']:.4f}")
        print(f"  Cost Basis: ${pos['cost_basis']:.2f}")
        print(f"  Market End Date: {pos['end_date']}")
        print(f"  Resolution Outcome: {pos['resolution_outcome'] or 'Unknown'}")
        print(f"  Calculated Exit Price: ${pnl_info['exit_price']:.4f}")
        print(f"  Realized P&L: ${pnl_info['realized_pnl']:.2f}")
        print()

    print("-" * 60)
    print(f"Total Realized P&L from these positions: ${total_realized_pnl:.2f}")
    print("-" * 60)

    if args.apply:
        print("\nApplying fixes...")
        apply_fixes(conn, positions)
        print("\nDone! Positions have been marked as closed.")
    else:
        print("\nThis was a DRY RUN. No changes were made.")
        print("Run with --apply to actually fix these positions.")

    conn.close()


if __name__ == "__main__":
    main()
