#!/usr/bin/env python3
"""
Populate clob_token_id_yes and clob_token_id_no for all markets.
Fetches from Polymarket Gamma API using market slugs.
"""

import os
import asyncio
import aiohttp
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv(Path.home() / '.claude/credentials/.env')

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


def get_connection():
    return psycopg2.connect(
        host=os.getenv('HOME_DB_HOST'),
        port=os.getenv('HOME_DB_PORT'),
        user=os.getenv('HOME_DB_USER'),
        password=os.getenv('HOME_DB_PASSWORD'),
        dbname=os.getenv('POLYBOT_DB_NAME'),
        sslmode='prefer'
    )


async def fetch_market_tokens(session: aiohttp.ClientSession, slug: str) -> dict:
    """Fetch market data from Gamma API and extract token IDs."""
    try:
        url = f"{GAMMA_API_BASE}/markets?slug={slug}"
        async with session.get(url, timeout=30) as response:
            if response.status != 200:
                print(f"    API error for {slug}: {response.status}")
                return None
            data = await response.json()

            if not data:
                print(f"    No data returned for {slug}")
                return None

            # Handle both list and single market response
            market = data[0] if isinstance(data, list) else data

            # Token IDs are in clobTokenIds array: [yes_token, no_token]
            clob_token_ids = market.get('clobTokenIds', [])
            if len(clob_token_ids) >= 2:
                return {
                    'yes': clob_token_ids[0],
                    'no': clob_token_ids[1],
                    'condition_id': market.get('conditionId'),
                }

            print(f"    No token IDs found for {slug}")
            return None

    except Exception as e:
        print(f"    Error fetching {slug}: {e}")
        return None


async def main():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    print("=" * 60)
    print("Populating clob_token_ids for all markets")
    print("=" * 60)

    # Get all markets that are missing token IDs
    cur.execute("""
        SELECT id, slug, title
        FROM markets
        WHERE clob_token_id_yes IS NULL OR clob_token_id_no IS NULL
        ORDER BY id
    """)
    markets = cur.fetchall()

    if not markets:
        print("\nAll markets already have token IDs!")
        conn.close()
        return

    print(f"\nFound {len(markets)} market(s) missing token IDs\n")

    updated = 0
    async with aiohttp.ClientSession() as session:
        for market in markets:
            print(f"Processing: {market['title'][:50]}...")
            print(f"  Slug: {market['slug']}")

            tokens = await fetch_market_tokens(session, market['slug'])

            if tokens:
                cur.execute("""
                    UPDATE markets
                    SET clob_token_id_yes = %s,
                        clob_token_id_no = %s,
                        condition_id = COALESCE(condition_id, %s),
                        updated_at = NOW()
                    WHERE id = %s
                """, (
                    tokens['yes'],
                    tokens['no'],
                    tokens.get('condition_id'),
                    market['id']
                ))
                print(f"  ✓ Updated: yes={tokens['yes'][:20]}..., no={tokens['no'][:20]}...")
                updated += 1
            else:
                print(f"  ✗ Could not fetch token IDs")

            # Small delay to be nice to the API
            await asyncio.sleep(0.2)

    conn.commit()
    cur.close()
    conn.close()

    print("\n" + "=" * 60)
    print(f"Done! Updated {updated}/{len(markets)} markets.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
