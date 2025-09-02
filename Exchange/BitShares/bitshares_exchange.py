from __future__ import annotations

import json
import time
from decimal import Decimal
from getpass import getpass
from typing import Optional, Dict, Any, List, Tuple

import requests
from bitshares import BitShares
from bitshares.account import Account
from bitshares.market import Market

from octobot_trading.exchanges.abstract_exchange import AbstractExchange
from octobot_trading import enums as trading_enums

from . import config as bts_config


class _Connector:
    """Minimal connector facade expected by AbstractExchange.authenticated()."""

    def __init__(self):
        self.is_authenticated: bool = False


class BitSharesExchange(AbstractExchange):
    """OctoBot tentacle for BitShares/XBTS.

    - Market data: XBTS REST API (cmc.xbts.io/v2)
    - Trading: python-bitshares over WSS
    - Secrets: prompted via getpass at initialize time
    - Slippage guard: bts_config.MAX_SLIPPAGE
    - Fee asset selection: bts_config.FEE_ASSET_PREFERENCE
    """

    def __init__(self, config, exchange_manager, exchange_config_by_exchange: Optional[dict[str, dict]] = None):
        super().__init__(config, exchange_manager, exchange_config_by_exchange)
        self._session: Optional[requests.Session] = None
        self._wss_nodes: List[str] = list(bts_config.WSS_NODES)
        self._account_name: str = bts_config.ACCOUNT_NAME
        self._enabled_markets: List[str] = list(bts_config.ENABLED_MARKETS)
        self._max_slippage: Decimal = Decimal(str(bts_config.MAX_SLIPPAGE))
        self._fee_preference: List[str] = list(bts_config.FEE_ASSET_PREFERENCE)
        self._api_base: str = bts_config.XBTS_API_BASE.rstrip("/")

        self.bitshares: Optional[BitShares] = None
        self.account: Optional[Account] = None
        self.connector = _Connector()

    @classmethod
    def get_name(cls) -> str:
        return "BitShares"

    async def initialize_impl(self):
        # HTTP session for XBTS API
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "OctoBot-BitShares/1.0"})

        # Setup BitShares connection without keys (no prompt at init)
        self.bitshares = BitShares(self._wss_nodes)
        # Instantiate account (public reads don't require WIF)
        self.account = Account(self._account_name, blockchain_instance=self.bitshares)

        # Set symbols handled
        self.symbols = set(self._enabled_markets)

        # Not authenticated yet (no keys provided)
        self.connector.is_authenticated = False

    async def stop(self) -> None:
        # BitShares lib has no explicit close; cleanup references
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None
        self.account = None
        self.bitshares = None
        self.connector.is_authenticated = False

    # -------------------------
    # Helpers
    # -------------------------
    def _bts_pair_to_api(self, symbol: str) -> str:
        """Convert BASE/QUOTE -> BASE_QUOTE for XBTS API."""
        base, quote = self.get_split_pair_from_exchange(symbol)
        return f"{base}_{quote}"

    def get_split_pair_from_exchange(self, pair) -> (str, str):
        # AbstractExchange declares this helper; ensure consistent splitting
        if isinstance(pair, str) and "/" in pair:
            b, q = pair.split("/", 1)
            return b.strip(), q.strip()
        raise ValueError(f"Unexpected pair format: {pair}")

    def _ensure_auth(self):
        """Prompt for ACTIVE WIF once when a trading action is requested."""
        if self.connector.is_authenticated:
            return
        # Prompt secrets (ACTIVE WIF) securely
        active_wif = getpass("Enter BitShares ACTIVE WIF for account %s: " % self._account_name)
        # Recreate BitShares instance with keys or add to wallet
        try:
            # If wallet exists, add key; if not, recreate instance with keys
            if self.bitshares is not None and getattr(self.bitshares, "wallet", None) is not None:
                try:
                    self.bitshares.wallet.addPrivateKey(active_wif)
                except Exception:
                    # fallback to recreating instance with keys
                    self.bitshares = BitShares(self._wss_nodes, keys=[active_wif])
            else:
                self.bitshares = BitShares(self._wss_nodes, keys=[active_wif])
            # Rebind account to ensure signed ops use the new instance
            self.account = Account(self._account_name, blockchain_instance=self.bitshares)
            self.connector.is_authenticated = True
        except Exception as e:
            raise RuntimeError(f"Authentication failed: {e}")

    def _request(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        assert self._session is not None, "HTTP session not initialized"
        url = f"{self._api_base}/{path.lstrip('/') }"
        r = self._session.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def _top_of_book(self, symbol: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """Return (best_bid, best_ask) as Decimals or (None, None)."""
        api_sym = self._bts_pair_to_api(symbol)
        try:
            data = self._request(f"orderbook/{api_sym}", params={"depth": 1})
            bid = Decimal(str(data["bids"][0]["price"])) if data.get("bids") else None
            ask = Decimal(str(data["asks"][0]["price"])) if data.get("asks") else None
            return bid, ask
        except Exception:
            return None, None

    def _select_fee_asset(self) -> str:
        """Pick a fee asset according to preference with robust symbol handling.

        Strategy:
        1) Exact match on preferred symbols (case-insensitive).
        2) Suffix match: if preference is e.g. 'NCH', match balances like 'XBTSX.NCH'.
           When several prefixed variants exist, choose the one with the highest balance.
        3) Fallback to 'BTS' if available, else any positive-balance asset, else 'BTS'.
        """
        balances = self.get_balance() or {}
        if not balances:
            return "BTS"

        # Normalize a quick lookup for exact matches (case-insensitive)
        upper_map = {sym.upper(): sym for sym in balances.keys()}

        # 1) Exact match
        for pref in self._fee_preference:
            pref_up = pref.upper()
            canon = upper_map.get(pref_up)
            if canon and balances.get(canon, Decimal("0")) > 0:
                return canon

        # 2) Suffix/prefix-aware match: prefer symbols whose last token matches preference
        for pref in self._fee_preference:
            pref_up = pref.upper()
            candidates = [
                (sym, amt)
                for sym, amt in balances.items()
                if amt > 0 and sym.upper().split(".")[-1] == pref_up
            ]
            if candidates:
                # Choose the largest available balance for stability
                best = max(candidates, key=lambda x: x[1])
                return best[0]

        # 3) Fallbacks
        if balances.get("BTS", Decimal("0")) > 0:
            return "BTS"
        # Any positive-balance asset as last resort
        for sym, amt in balances.items():
            if amt > 0:
                return sym
        return "BTS"

    # -------------------------
    # Market data
    # -------------------------
    def get_order_book(self, symbol: str, limit: int = 5, **kwargs: dict) -> Optional[dict]:
        api_sym = self._bts_pair_to_api(symbol)
        depth = max(1, int(limit))
        data = self._request(f"orderbook/{api_sym}", params={"depth": depth})
        # Return as-is; OctoBot has parse_order_book
        return data

    def get_price_ticker(self, symbol: str, **kwargs: dict) -> Optional[dict]:
        api_sym = self._bts_pair_to_api(symbol)
        try:
            return self._request(f"tickers/{api_sym}")
        except Exception:
            return None

    def get_recent_trades(self, symbol: str, limit: int = 50, **kwargs: dict) -> Optional[list]:
        api_sym = self._bts_pair_to_api(symbol)
        limit = max(1, min(300, int(limit)))
        try:
            return self._request(f"trades/{api_sym}", params={"limit": limit})
        except Exception:
            return None

    # -------------------------
    # Balances
    # -------------------------
    def get_balance(self, **kwargs: dict):
        assert self.account is not None, "Account not initialized"
        # Returns a dict {symbol: Decimal}
        result: Dict[str, Decimal] = {}
        for bal in self.account.balances:
            sym = bal["symbol"]
            amount = Decimal(str(bal["amount"]))
            result[sym] = result.get(sym, Decimal("0")) + amount
        return result

    def get_default_balance(self):
        return self.get_balance()

    # -------------------------
    # Orders
    # -------------------------
    def _enforce_slippage(self, side: trading_enums.TradeOrderSide, symbol: str, price: Decimal) -> None:
        best_bid, best_ask = self._top_of_book(symbol)
        if best_bid is None or best_ask is None:
            return  # can't compute
        if side == trading_enums.TradeOrderSide.BUY:
            # price must not exceed ask by more than MAX_SLIPPAGE
            allowed = best_ask * (Decimal("1") + self._max_slippage)
            if price > allowed:
                raise ValueError(f"Slippage > {self._max_slippage*100}%: price {price} > allowed {allowed}")
        elif side == trading_enums.TradeOrderSide.SELL:
            allowed = best_bid * (Decimal("1") - self._max_slippage)
            if price < allowed:
                raise ValueError(f"Slippage > {self._max_slippage*100}%: price {price} < allowed {allowed}")

    def create_order(
        self,
        order_type: trading_enums.TraderOrderType,
        symbol: str,
        quantity: Decimal,
        price: Decimal = None,
        stop_price: Decimal = None,
        side: trading_enums.TradeOrderSide = None,
        current_price: Decimal = None,
        reduce_only: bool = False,
        params: dict = None,
    ) -> Optional[dict]:
        assert self.bitshares is not None and self.account is not None, "BitShares not initialized"

        # Accept BUY_LIMIT and SELL_LIMIT variants (no generic LIMIT in this OctoBot-Trading version)
        if order_type not in (
            getattr(trading_enums.TraderOrderType, "BUY_LIMIT", None),
            getattr(trading_enums.TraderOrderType, "SELL_LIMIT", None),
        ):
            raise NotImplementedError(
                f"Only BUY_LIMIT/SELL_LIMIT orders are supported on BitShares tentacle, got {order_type}"
            )
        if side is None:
            raise ValueError("Order side is required")
        if price is None:
            raise ValueError("Limit price is required")

        # Slippage guard
        self._enforce_slippage(side, symbol, Decimal(str(price)))

        # Ensure authenticated (will prompt once)
        self._ensure_auth()

        market = Market(symbol, blockchain_instance=self.bitshares)
        fee_symbol = self._select_fee_asset()

        # Place order
        if side == trading_enums.TradeOrderSide.BUY:
            tx = market.buy(price=float(price), amount=float(quantity), account=self.account, fee_asset=fee_symbol)
        else:
            tx = market.sell(price=float(price), amount=float(quantity), account=self.account, fee_asset=fee_symbol)

        # tx contains transaction details; try to extract order id
        order_id = None
        try:
            # typical structure: {'expiration': ..., 'operations': [[1, {'seller': '1.2.x', 'amount_to_sell': {...}, ...}]], ...}
            order_id = tx.get("operation_results", [[None, [None]]])[0][1]
        except Exception:
            pass

        return {
            "exchange_order_id": order_id,
            "transaction": tx,
            "symbol": symbol,
            "side": side.value if hasattr(side, "value") else str(side),
            "price": str(price),
            "quantity": str(quantity),
            "fee_asset": fee_symbol,
        }

    def cancel_order(self, exchange_order_id: str, symbol: str, order_type: trading_enums.TraderOrderType, **kwargs: dict):
        assert self.bitshares is not None, "BitShares not initialized"
        self._ensure_auth()
        # python-bitshares exposes cancel on BitShares instance
        res = self.bitshares.cancel(exchange_order_id)
        return trading_enums.OrderStatus.CANCELED if res else trading_enums.OrderStatus.ERROR

    def get_order(self, exchange_order_id: str, symbol: str = None, **kwargs: dict) -> dict:
        # For now, query open orders and match id
        orders = self.get_open_orders(symbol=symbol)
        for o in orders:
            if str(o.get("id")) == str(exchange_order_id):
                return o
        return {}

    def get_open_orders(self, symbol: str = None, since: int = None, limit: int = None, **kwargs: dict) -> list:
        assert self.account is not None, "Account not initialized"
        open_orders = []
        for order in self.account.openorders:
            od = dict(order)
            if symbol:
                try:
                    base, quote = self.get_split_pair_from_exchange(symbol)
                    if base not in json.dumps(od) and quote not in json.dumps(od):
                        continue
                except Exception:
                    pass
            open_orders.append(od)
        if limit is not None:
            open_orders = open_orders[: int(limit)]
        return open_orders
