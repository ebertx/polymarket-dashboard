#!/usr/bin/env python3
"""
Add missing positions that exist in Polymarket API but not in database.
"""

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv(Path.home() / '.claude/credentials/.env')


def get_connection():
    return psycopg2.connect(
        host=os.getenv('HOME_DB_HOST'),
        port=os.getenv('HOME_DB_PORT'),
        user=os.getenv('HOME_DB_USER'),
        password=os.getenv('HOME_DB_PASSWORD'),
        dbname=os.getenv('POLYBOT_DB_NAME'),
        sslmode='prefer'
    )


def main():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    print("=" * 60)
    print("Adding missing positions from Polymarket API")
    print("=" * 60)

    # Positions from API that are missing in DB
    # Based on polymarket_trading.py balance output
    missing_positions = [
        {
            'market_slug': 'will-1-fed-rate-cut-happen-2026',
            'market_title': 'Fed: 1 Rate Cut 2026',
            'direction': 'yes',
            'shares': 62.0,
            'entry_price': 0.118,
            'current_price': 0.140,
        },
        {
            'market_slug': 'will-no-change-fed-rates-jan-2026',
            'market_title': 'Fed: No Change Jan 2026',
            'direction': 'yes',
            'shares': 16.0,
            'entry_price': 0.740,
            'current_price': 0.760,
        },
    ]

    for pos in missing_positions:
        print(f"\nProcessing: {pos['market_title']}...")

        # Find the market
        cur.execute("SELECT id FROM markets WHERE slug = %s", (pos['market_slug'],))
        market = cur.fetchone()

        if not market:
            print(f"  ERROR: Market not found for slug '{pos['market_slug']}'")
            continue

        market_id = market['id']

        # Check if position already exists
        cur.execute("""
            SELECT id FROM positions
            WHERE market_id = %s AND direction = %s AND status = 'open'
        """, (market_id, pos['direction']))

        existing = cur.fetchone()
        if existing:
            print(f"  Position already exists (id={existing['id']})")
            continue

        # Calculate values
        shares = Decimal(str(pos['shares']))
        entry_price = Decimal(str(pos['entry_price']))
        current_price = Decimal(str(pos['current_price']))
        cost_basis = shares * entry_price
        current_value = shares * current_price
        unrealized_pnl = current_value - cost_basis

        # Insert position
        cur.execute("""
            INSERT INTO positions (
                market_id, direction, shares, entry_price, entry_date,
                current_price, current_value, cost_basis, unrealized_pnl,
                status, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, NOW(),
                %s, %s, %s, %s,
                'open', NOW(), NOW()
            )
            RETURNING id
        """, (
            market_id, pos['direction'], shares, entry_price,
            current_price, current_value, cost_basis, unrealized_pnl
        ))

        new_id = cur.fetchone()['id']
        print(f"  Created position id={new_id}")
        print(f"    {shares} {pos['direction'].upper()} @ {entry_price} = ${cost_basis:.2f}")
        print(f"    Current: ${current_value:.2f} (PnL: ${unrealized_pnl:+.2f})")

    conn.commit()
    cur.close()
    conn.close()

    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)


if __name__ == "__main__":
    main()
