import aiohttp
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

DATA_API_BASE = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"


class PolymarketClient:
    def __init__(self, wallet_address: str):
        self.wallet_address = wallet_address.lower()
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

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
                current_price = Decimal(str(pos.get("currentPrice", 0)))
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

        # Note: USDC balance would require a Web3 call or separate API
        # For now, we'll track positions only and set USDC to 0
        # Can be enhanced later with Web3 integration
        return {
            "usdc_balance": Decimal("0"),
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
                best_bid = Decimal(str(bids[0].get("price", 0)))
                best_ask = Decimal(str(asks[0].get("price", 0)))
                return (best_bid + best_ask) / 2
            elif bids:
                return Decimal(str(bids[0].get("price", 0)))
            elif asks:
                return Decimal(str(asks[0].get("price", 0)))
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
