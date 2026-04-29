import asyncio
import aiohttp
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Any

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

logger = logging.getLogger(__name__)

DATA_API_BASE = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"

# Process-wide cache for the authenticated CLOB client. The L1→L2 derive
# call (~500ms) only needs to happen once per process; subsequent balance
# fetches reuse the cached creds and take ~150ms.
_clob_client_cache: Optional[ClobClient] = None


class PolymarketClient:
    def __init__(
        self,
        wallet_address: str,
        private_key: Optional[str] = None,
        signature_type: int = 1,
    ):
        self.wallet_address = wallet_address.lower()
        self.private_key = private_key or None  # treat empty string as None
        self.signature_type = signature_type
        self._session: Optional[aiohttp.ClientSession] = None

    @classmethod
    def from_settings(cls, settings) -> "PolymarketClient":
        return cls(
            wallet_address=settings.polymarket_wallet,
            private_key=settings.polymarket_private_key or None,
            signature_type=settings.polymarket_signature_type,
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _build_clob_client(self) -> ClobClient:
        global _clob_client_cache
        if _clob_client_cache is not None:
            return _clob_client_cache
        client = ClobClient(
            CLOB_API_BASE,
            key=self.private_key,
            chain_id=POLYGON,
            signature_type=self.signature_type,
            funder=self.wallet_address,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        _clob_client_cache = client
        logger.info("Derived Polymarket CLOB API credentials (cached for process lifetime)")
        return client

    def _fetch_collateral_balance_sync(self) -> Decimal:
        client = self._build_clob_client()
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        # CLOB returns the collateral balance as a string in 6-decimal USDC units
        # (e.g. "179174679" for $179.17). Polymarket's "major upgrade" moved
        # collateral off the Polygon USDC.e ERC20, so reading balanceOf() on
        # 0x2791... returns 0 for funded wallets — the CLOB API is now the
        # only authoritative source for cash balance.
        raw = result.get("balance", "0")
        return Decimal(raw) / Decimal(10 ** 6)

    async def get_usdc_balance(self) -> Decimal:
        """Fetch USDC collateral balance from the Polymarket CLOB API.

        Returns Decimal("0") and logs a warning if no private key is configured
        (tracker can run without trading creds, but cash balance will be unavailable).
        """
        if not self.private_key:
            logger.warning(
                "POLYMARKET_PRIVATE_KEY not configured — cash balance will report 0. "
                "Set it in the environment to enable accurate portfolio totals."
            )
            return Decimal("0")
        try:
            return await asyncio.to_thread(self._fetch_collateral_balance_sync)
        except Exception as e:
            logger.error(f"Failed to fetch USDC balance from CLOB API: {e}")
            return Decimal("0")

    async def _request(self, url: str, params: Optional[Dict] = None) -> Any:
        session = await self._get_session()
        try:
            async with session.get(url, params=params, timeout=30) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            logger.error(f"Request failed for {url}: {e}")
            raise

    async def get_wallet_positions(self) -> List[Dict]:
        """Fetch all positions for the wallet from the data API."""
        url = f"{DATA_API_BASE}/positions"
        params = {"user": self.wallet_address}

        try:
            data = await self._request(url, params)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

    async def get_wallet_balance(self) -> Dict[str, Any]:
        """
        Fetch wallet balance and positions.
        Returns dict with usdc_balance and positions list.
        """
        positions = await self.get_wallet_positions()

        # Calculate total position value from positions
        total_value = Decimal("0")
        processed_positions = []

        for pos in positions:
            try:
                size = Decimal(str(pos.get("size", 0)))
                # API returns 'curPrice' not 'currentPrice'
                current_price = Decimal(str(pos.get("curPrice", pos.get("currentPrice", 0))))
                avg_price = Decimal(str(pos.get("avgPrice", 0)))
                value = size * current_price

                processed_positions.append({
                    "token_id": pos.get("asset"),
                    "condition_id": pos.get("conditionId"),
                    "outcome": pos.get("outcome", "Unknown"),
                    "size": size,
                    "avg_price": avg_price,
                    "current_price": current_price,
                    "value": value,
                    "unrealized_pnl": (current_price - avg_price) * size,
                    "realized_pnl": Decimal(str(pos.get("realizedPnl", 0))),
                })
                total_value += value
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to process position: {pos}, error: {e}")
                continue

        # Fetch USDC collateral balance from the CLOB API
        usdc_balance = await self.get_usdc_balance()

        return {
            "usdc_balance": usdc_balance,
            "total_position_value": total_value,
            "positions": processed_positions,
        }

    async def get_market_price(self, token_id: str) -> Optional[Decimal]:
        """Fetch current midpoint price for a token from CLOB API."""
        url = f"{CLOB_API_BASE}/book"
        params = {"token_id": token_id}

        try:
            data = await self._request(url, params)
            bids = data.get("bids", [])
            asks = data.get("asks", [])

            if bids and asks:
                # CLOB returns bids ascending and asks descending (worst-first),
                # so use max/min to get the best bid/ask regardless of sort order
                best_bid = max(Decimal(str(b.get("price", 0))) for b in bids)
                best_ask = min(Decimal(str(a.get("price", 0))) for a in asks)
                return (best_bid + best_ask) / 2
            elif bids:
                return max(Decimal(str(b.get("price", 0))) for b in bids)
            elif asks:
                return min(Decimal(str(a.get("price", 0))) for a in asks)
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch price for {token_id}: {e}")
            return None

    async def get_market_prices(self, token_ids: List[str]) -> Dict[str, Decimal]:
        """Fetch prices for multiple tokens."""
        prices = {}
        for token_id in token_ids:
            price = await self.get_market_price(token_id)
            if price is not None:
                prices[token_id] = price
        return prices

    async def get_market_metadata(self, condition_id: str) -> Optional[Dict]:
        """Fetch market metadata from Gamma API."""
        url = f"{GAMMA_API_BASE}/markets/{condition_id}"

        try:
            return await self._request(url)
        except Exception as e:
            logger.warning(f"Failed to fetch market metadata for {condition_id}: {e}")
            return None

    async def search_markets(self, query: str, limit: int = 10) -> List[Dict]:
        """Search markets by query string."""
        url = f"{GAMMA_API_BASE}/markets"
        params = {"_q": query, "_limit": limit}

        try:
            data = await self._request(url, params)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"Market search failed: {e}")
            return []

    async def lookup_market_by_token_id(self, token_id: str) -> Optional[Dict]:
        """Look up market metadata from Gamma API by CLOB token ID.

        Uses a User-Agent header because the Gamma API blocks default Python user-agents.
        """
        url = f"{GAMMA_API_BASE}/markets"
        params = {"clob_token_ids": token_id}
        headers = {"User-Agent": "Mozilla/5.0"}

        session = await self._get_session()
        try:
            async with session.get(url, params=params, headers=headers, timeout=30) as response:
                response.raise_for_status()
                data = await response.json()
                if isinstance(data, list) and len(data) > 0:
                    return data[0]
                return None
        except Exception as e:
            logger.warning(f"Gamma API lookup failed for token {token_id}: {e}")
            return None
