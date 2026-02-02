#!/usr/bin/env python3
"""
Fix Fed Chair market data issues:
1. Wrong end_date (was Jan 30, should be Dec 31, 2026)
2. Prematurely closed positions
3. Missing markets (Hassett, rate cuts, no change)
"""

import os
import sys
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
    print("Fixing Fed Chair market data issues")
    print("=" * 60)

    # 1. Fix the Fed Chair: Waller market end_date
    print("\n1. Fixing Fed Chair: Waller market end_date...")
    cur.execute("""
        UPDATE markets
        SET end_date = '2026-12-31T00:00:00Z',
            updated_at = NOW()
        WHERE title = 'Fed Chair: Waller'
    """)
    print(f"   Updated {cur.rowcount} market(s)")

    # 2. Reopen positions 14 and 15
    print("\n2. Reopening Fed Chair positions...")
    cur.execute("""
        UPDATE positions
        SET status = 'open',
            exit_date = NULL,
            exit_price = NULL,
            realized_pnl = NULL,
            updated_at = NOW()
        WHERE id IN (14, 15)
    """)
    print(f"   Reopened {cur.rowcount} position(s)")

    # 3. Check if Hassett market exists, if not create it
    print("\n3. Checking for Fed Chair: Hassett market...")
    cur.execute("SELECT id FROM markets WHERE slug LIKE '%hassett%'")
    hassett_market = cur.fetchone()

    if not hassett_market:
        print("   Creating Fed Chair: Hassett market...")
        cur.execute("""
            INSERT INTO markets (slug, title, description, end_date, created_at, updated_at)
            VALUES (
                'who-will-trump-nominate-as-fed-chair-hassett',
                'Fed Chair: Hassett',
                'Will Trump nominate Kevin Hassett as the next Fed chair?',
                '2026-12-31T00:00:00Z',
                NOW(),
                NOW()
            )
            RETURNING id
        """)
        hassett_market_id = cur.fetchone()['id']
        print(f"   Created market id={hassett_market_id}")

        # Update position 15 to point to Hassett market
        print("   Updating position 15 to link to Hassett market...")
        cur.execute("""
            UPDATE positions
            SET market_id = %s,
                updated_at = NOW()
            WHERE id = 15
        """, (hassett_market_id,))
        print(f"   Updated {cur.rowcount} position(s)")
    else:
        print(f"   Hassett market already exists (id={hassett_market['id']})")

    # 4. Add missing Fed rate markets if they don't exist
    print("\n4. Checking for missing Fed rate cut markets...")

    fed_markets_to_add = [
        {
            'slug': 'will-1-fed-rate-cut-happen-2026',
            'title': 'Fed: 1 Rate Cut 2026',
            'description': 'Will 1 Fed rate cut happen in 2026?',
        },
        {
            'slug': 'will-no-change-fed-rates-jan-2026',
            'title': 'Fed: No Change Jan 2026',
            'description': 'Will there be no change in Fed interest rates after the January 2026 meeting?',
        },
    ]

    for market_data in fed_markets_to_add:
        cur.execute("SELECT id FROM markets WHERE slug = %s", (market_data['slug'],))
        if not cur.fetchone():
            print(f"   Creating market: {market_data['title']}...")
            cur.execute("""
                INSERT INTO markets (slug, title, description, end_date, created_at, updated_at)
                VALUES (%s, %s, %s, '2026-12-31T00:00:00Z', NOW(), NOW())
                RETURNING id
            """, (market_data['slug'], market_data['title'], market_data['description']))
            new_id = cur.fetchone()['id']
            print(f"   Created market id={new_id}")
        else:
            print(f"   Market '{market_data['title']}' already exists")

    # 5. Verify the fixes
    print("\n5. Verifying fixes...")
    cur.execute("""
        SELECT p.id, p.status, p.shares, p.direction, m.title, m.end_date
        FROM positions p
        JOIN markets m ON p.market_id = m.id
        WHERE m.title ILIKE '%fed%' OR m.title ILIKE '%waller%' OR m.title ILIKE '%hassett%'
        ORDER BY p.id
    """)
    print("\n   Fed-related positions after fix:")
    for row in cur.fetchall():
        print(f"   Position {row['id']}: {row['shares']} {row['direction']} {row['status']} - {row['title']} (ends {row['end_date']})")

    conn.commit()
    cur.close()
    conn.close()

    print("\n" + "=" * 60)
    print("Done! Fed Chair market data fixed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
