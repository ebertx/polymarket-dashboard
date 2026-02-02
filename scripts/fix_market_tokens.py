#!/usr/bin/env python3
"""
Fix market token IDs by matching API positions to DB positions.
Uses entry_price and shares as matching keys since they're unique enough.
"""

import os
import asyncio
import aiohttp
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv(Path.home() / '.claude/credentials/.env')

DATA_API_BASE = 'https://data-api.polymarket.com'
WALLET = os.getenv('POLYMARKET_WALLET_ADDRESS', '0xf7cc6bd64be987730dc783e6d4787b2d1b802506')


def get_connection():
    return psycopg2.connect(
        host=os.getenv('HOME_DB_HOST'),
        port=os.getenv('HOME_DB_PORT'),
        user=os.getenv('HOME_DB_USER'),
        password=os.getenv('HOME_DB_PASSWORD'),
        dbname=os.getenv('POLYBOT_DB_NAME'),
        sslmode='prefer'
    )


async def fetch_api_positions():
    """Fetch current positions from Polymarket API."""
    async with aiohttp.ClientSession() as session:
        url = f'{DATA_API_BASE}/positions'
        params = {'user': WALLET.lower()}
        async with session.get(url, params=params, timeout=30) as response:
            return await response.json()


def match_positions(api_positions, db_positions):
    """Match API positions to DB positions by entry price and shares."""
    matches = []

    for api_pos in api_positions:
        api_size = round(float(api_pos.get('size', 0)), 2)
        api_price = round(float(api_pos.get('avgPrice', 0)), 4)
        token_id = api_pos.get('asset')
        condition_id = api_pos.get('conditionId')
        outcome = api_pos.get('outcome', '').lower()
        title = api_pos.get('title', api_pos.get('marketDescription', ''))[:50]

        # Find matching DB position
        for db_pos in db_positions:
            db_size = round(float(db_pos['shares']), 2)
            db_price = round(float(db_pos['entry_price']), 4)

            # Match by shares and entry price (with tolerance)
            if abs(api_size - db_size) < 0.5 and abs(api_price - db_price) < 0.01:
                matches.append({
                    'db_position_id': db_pos['position_id'],
                    'db_market_id': db_pos['market_id'],
                    'db_title': db_pos['market_title'],
                    'db_direction': db_pos['direction'],
                    'api_title': title,
                    'api_outcome': outcome,
                    'token_id': token_id,
                    'condition_id': condition_id,
                })
                break

    return matches


async def main():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    print("=" * 60)
    print("Fixing market token IDs by matching positions")
    print("=" * 60)

    # Get all open DB positions with market info
    cur.execute("""
        SELECT
            p.id as position_id,
            p.market_id,
            p.direction,
            p.shares,
            p.entry_price,
            m.title as market_title,
            m.clob_token_id_yes,
            m.clob_token_id_no
        FROM positions p
        JOIN markets m ON p.market_id = m.id
        WHERE p.status = 'open'
        ORDER BY p.id
    """)
    db_positions = cur.fetchall()
    print(f"\nFound {len(db_positions)} open DB positions")

    # Fetch API positions
    api_positions = await fetch_api_positions()
    print(f"Found {len(api_positions)} API positions")

    # Match them up
    matches = match_positions(api_positions, db_positions)
    print(f"\nMatched {len(matches)} positions")

    # Update markets with token IDs
    print("\nUpdating markets:")
    updated = 0
    for match in matches:
        direction = match['db_direction']
        token_field = 'clob_token_id_yes' if direction == 'yes' else 'clob_token_id_no'

        print(f"\n  Position {match['db_position_id']}: {match['db_title'][:40]}")
        print(f"    Direction: {direction.upper()}")
        print(f"    Matched to: {match['api_title'][:40]}")
        print(f"    Token: {match['token_id'][:30]}...")

        # Update the market
        cur.execute(f"""
            UPDATE markets
            SET {token_field} = %s,
                condition_id = COALESCE(condition_id, %s),
                updated_at = NOW()
            WHERE id = %s
        """, (match['token_id'], match['condition_id'], match['db_market_id']))

        if cur.rowcount > 0:
            print(f"    ✓ Updated market {match['db_market_id']}")
            updated += 1

    conn.commit()
    cur.close()
    conn.close()

    print("\n" + "=" * 60)
    print(f"Done! Updated {updated} markets with token IDs.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
