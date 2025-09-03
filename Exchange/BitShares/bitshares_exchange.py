from __future__ import annotations

import datetime
import decimal
import json
import logging
import time
from decimal import Decimal
from getpass import getpass
from typing import Optional, Dict, Any, List, Tuple, Union

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

    # Mark as non-CCXT exchange
    IS_CCXT_EXCHANGE = False
    
    # Required exchange info for OctoBot
    EXCHANGE_INFO = {
        'id': 'bitshares',
        'name': 'BitShares',
        'enabled': True,
        'rateLimit': 300,  # ms between requests
        'timeout': 30000,  # request timeout in ms
        'has': {
            'fetchMarkets': True,
            'fetchTicker': True,
            'fetchTickers': True,
            'fetchOrderBook': True,
            'fetchTrades': True,
            'fetchBalance': True,
            'createOrder': True,
            'cancelOrder': True,
            'fetchOrder': True,
            'fetchOpenOrders': True,
            'fetchClosedOrders': True,
            'fetchMyTrades': False,
            'withdraw': False,
        },
        'urls': {
            'logo': 'https://bitshares.org/assets/img/logo.png',
            'api': 'https://cmc.xbts.io/v2',
            'www': 'https://bitshares.org',
            'doc': 'https://docs.bitshares.org/api/',
        },
        'api': {
            'public': {
                'get': [
                    'tickers/{pair}',
                    'orderbook/{pair}',
                    'trades/{pair}',
                ],
            },
        },
    }

    def __init__(self, config, exchange_manager, exchange_config_by_exchange: Optional[dict[str, dict]] = None):
        super().__init__(config, exchange_manager, exchange_config_by_exchange)
        
        # Initialize exchange info
        self.exchange_info = self.EXCHANGE_INFO.copy()
        
        # Initialize session and configuration
        self._session: Optional[requests.Session] = None
        self._wss_nodes: List[str] = list(bts_config.WSS_NODES)
        self._account_name: str = bts_config.ACCOUNT_NAME
        self._enabled_markets: List[str] = list(bts_config.ENABLED_MARKETS)
        self._max_slippage: Decimal = Decimal(str(bts_config.MAX_SLIPPAGE))
        self._fee_preference: List[str] = list(bts_config.FEE_ASSET_PREFERENCE)
        self._api_base: str = bts_config.XBTS_API_BASE.rstrip("/")
        
        # Initialize BitShares client and account
        self.bitshares: Optional[BitShares] = None
        self.account: Optional[Account] = None
        self.connector = _Connector()
        
        # Initialize markets cache
        self._markets = {}
        self._markets_by_id = {}
        self._markets_loaded = False

    @classmethod
    def get_name(cls) -> str:
        return "BitShares"

    async def initialize_impl(self):
        # HTTP session for XBTS API
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "OctoBot-BitShares/1.0",
            "Accept": "application/json",
            "Content-Type": "application/json"
        })

        try:
            # Setup BitShares connection without keys (no prompt at init)
            self.bitshares = BitShares(
                self._wss_nodes,
                num_retries=3,
                retry_overrides=["wss"],
                timeout=30,
                expiration=30
            )
            
            # Load markets data
            await self.load_markets()
            
            # Instantiate account (public reads don't require WIF)
            try:
                self.account = Account(self._account_name, blockchain_instance=self.bitshares)
                self.logger.info(f"Connected to BitShares account: {self._account_name}")
            except Exception as e:
                self.logger.warning(f"Could not load account {self._account_name}: {str(e)}")
                self.account = None
            
            # Set symbols handled
            self.symbols = set(self._enabled_markets)
            
            # Not authenticated yet (no keys provided)
            self.connector.is_authenticated = False
            
            self.logger.info("BitShares exchange initialized successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize BitShares exchange: {str(e)}")
            raise

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
    # Market Methods
    # -------------------------
    async def load_markets(self, reload: bool = False) -> dict:
        """Load available markets from the exchange."""
        if not reload and self._markets_loaded:
            return self._markets
            
        self._markets = {}
        self._markets_by_id = {}
        
        try:
            # Get all tickers to find available markets
            tickers = self._request("tickers")
            
            for pair, ticker in tickers.items():
                try:
                    base, quote = pair.split('_')
                    symbol = f"{base}/{quote}"
                    
                    market = {
                        'id': pair,
                        'symbol': symbol,
                        'base': base,
                        'quote': quote,
                        'baseId': base,
                        'quoteId': quote,
                        'active': True,
                        'precision': {
                            'price': 8,  # Default precision, adjust based on actual market
                            'amount': 8,  # Default precision, adjust based on actual market
                        },
                        'limits': {
                            'amount': {
                                'min': Decimal('0.001'),  # Adjust based on actual market
                                'max': None,
                            },
                            'price': {
                                'min': Decimal('0.00000001'),
                                'max': None,
                            },
                            'cost': {
                                'min': Decimal('0.001'),  # Minimum order value
                                'max': None,
                            },
                        },
                        'info': ticker,
                    }
                    
                    self._markets[symbol] = market
                    self._markets_by_id[pair] = market
                    
                except Exception as e:
                    self.logger.warning(f"Error processing market {pair}: {str(e)}")
                    continue
            
            self._markets_loaded = True
            self.logger.info(f"Loaded {len(self._markets)} markets")
            
        except Exception as e:
            self.logger.error(f"Failed to load markets: {str(e)}")
            raise
            
        return self._markets
        
    def get_market(self, symbol: str) -> dict:
        """Get market info for a symbol."""
        if not self._markets_loaded:
            raise RuntimeError("Markets not loaded. Call load_markets() first.")
        if symbol not in self._markets:
            raise ValueError(f"Market {symbol} not found")
        return self._markets[symbol]
        
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
        """Send an HTTP request to the XBTS API."""
        try:
            if self._session is None:
                self._session = requests.Session()
                self._session.headers.update({
                    "User-Agent": "OctoBot-BitShares/1.0",
                    "Accept": "application/json"
                })
                
            url = f"{self._api_base}/{path.lstrip('/')}"
            self.logger.debug(f"Requesting {url} with params: {params}")
            
            response = self._session.get(url, params=params, timeout=15)
            response.raise_for_status()
            
            try:
                return response.json()
            except ValueError as e:
                self.logger.error(f"Failed to parse JSON response: {response.text}")
                raise ValueError(f"Invalid JSON response: {str(e)}")
                
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Request failed: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                self.logger.error(f"Response status: {e.response.status_code}")
                self.logger.error(f"Response text: {e.response.text}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error in _request: {str(e)}")
            raise

    def _top_of_book(self, symbol: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """Return (best_bid, best_ask) as Decimals or (None, None)."""
        try:
            orderbook = self.get_order_book(symbol, limit=1)
            if not orderbook or 'bids' not in orderbook or 'asks' not in orderbook:
                return None, None
                
            best_bid = Decimal(str(orderbook['bids'][0][0])) if orderbook['bids'] else None
            best_ask = Decimal(str(orderbook['asks'][0][0])) if orderbook['asks'] else None
            
            return best_bid, best_ask
            
        except Exception as e:
            self.logger.warning(f"Error getting top of book for {symbol}: {str(e)}")
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
    async def fetch_order_book(self, symbol: str, limit: int = 5, **kwargs: dict) -> dict:
        """Fetch order book for a symbol."""
        try:
            api_sym = self._bts_pair_to_api(symbol)
            depth = max(1, min(100, int(limit)))
            data = self._request(f"orderbook/{api_sym}", params={"depth": depth})
            
            # Transform to CCXT format
            orderbook = {
                'bids': [],
                'asks': [],
                'symbol': symbol,
                'timestamp': int(time.time() * 1000),
                'datetime': self.iso8601(time.time() * 1000),
                'nonce': int(time.time() * 1000),
            }
            
            if 'bids' in data and isinstance(data['bids'], list):
                orderbook['bids'] = [[Decimal(str(b[0])), Decimal(str(b[1]))] for b in data['bids'][:limit]]
            if 'asks' in data and isinstance(data['asks'], list):
                orderbook['asks'] = [[Decimal(str(a[0])), Decimal(str(a[1]))] for a in data['asks'][:limit]]
                
            return orderbook
            
        except Exception as e:
            self.logger.error(f"Error fetching order book for {symbol}: {str(e)}")
            raise
            
    def get_order_book(self, symbol: str, limit: int = 5, **kwargs: dict) -> Optional[dict]:
        """Get order book (synchronous wrapper for fetch_order_book)."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self.fetch_order_book(symbol, limit, **kwargs)
        )

    async def fetch_ticker(self, symbol: str, **kwargs: dict) -> dict:
        """Fetch ticker for a symbol."""
        try:
            api_sym = self._bts_pair_to_api(symbol)
            data = self._request(f"tickers/{api_sym}")
            
            # Get order book for bid/ask
            orderbook = await self.fetch_order_book(symbol, 1)
            
            ticker = {
                'symbol': symbol,
                'timestamp': int(time.time() * 1000),
                'datetime': self.iso8601(time.time() * 1000),
                'high': Decimal(str(data.get('high', 0))),
                'low': Decimal(str(data.get('low', 0))),
                'bid': Decimal(str(data.get('bid', 0))),
                'bidVolume': None,  # Not provided by XBTS API
                'ask': Decimal(str(data.get('ask', 0))),
                'askVolume': None,  # Not provided by XBTS API
                'vwap': None,  # Not provided by XBTS API
                'open': Decimal(str(data.get('open', 0))),
                'close': Decimal(str(data.get('close', 0))),
                'last': Decimal(str(data.get('last', 0))),
                'previousClose': None,  # Not provided by XBTS API
                'change': Decimal(str(data.get('change', 0))),
                'percentage': Decimal(str(data.get('percentage', 0))),
                'average': None,  # Calculate if needed
                'baseVolume': Decimal(str(data.get('volume', 0))),
                'quoteVolume': Decimal(str(data.get('quote_volume', 0))),
                'info': data,
            }
            
            # Update bid/ask from orderbook if available
            if orderbook['bids']:
                ticker['bid'] = Decimal(str(orderbook['bids'][0][0]))
            if orderbook['asks']:
                ticker['ask'] = Decimal(str(orderbook['asks'][0][0]))
                
            return ticker
            
        except Exception as e:
            self.logger.error(f"Error fetching ticker for {symbol}: {str(e)}")
            raise
            
    def get_price_ticker(self, symbol: str, **kwargs: dict) -> Optional[dict]:
        """Get ticker (synchronous wrapper for fetch_ticker)."""
        import asyncio
        try:
            return asyncio.get_event_loop().run_until_complete(
                self.fetch_ticker(symbol, **kwargs)
            )
        except Exception as e:
            self.logger.error(f"Error in get_price_ticker: {str(e)}")
            return None

    async def fetch_trades(self, symbol: str, since: Optional[int] = None, limit: int = 50, **kwargs: dict) -> list:
        """Fetch recent trades for a symbol."""
        try:
            api_sym = self._bts_pair_to_api(symbol)
            limit = max(1, min(300, int(limit)))
            params = {"limit": limit}
            
            if since is not None:
                params['since'] = int(since / 1000)  # Convert to seconds
                
            trades = self._request(f"trades/{api_sym}", params=params)
            
            # Transform to CCXT format
            result = []
            for trade in trades:
                result.append({
                    'id': str(trade.get('id', '')),
                    'order': None,  # Not provided by XBTS API
                    'info': trade,
                    'timestamp': int(trade.get('time', 0)) * 1000,  # Convert to ms
                    'datetime': self.iso8601(int(trade.get('time', 0)) * 1000),
                    'symbol': symbol,
                    'type': None,  # Not provided by XBTS API
                    'side': trade.get('side', '').lower(),
                    'takerOrMaker': None,  # Not provided by XBTS API
                    'price': Decimal(str(trade.get('price', 0))),
                    'amount': Decimal(str(trade.get('amount', 0))),
                    'cost': Decimal(str(trade.get('total', 0))),
                    'fee': None,  # Not provided by XBTS API
                })
                
            return result
            
        except Exception as e:
            self.logger.error(f"Error fetching trades for {symbol}: {str(e)}")
            raise
            
    def get_recent_trades(self, symbol: str, limit: int = 50, **kwargs: dict) -> Optional[list]:
        """Get recent trades (synchronous wrapper for fetch_trades)."""
        import asyncio
        try:
            return asyncio.get_event_loop().run_until_complete(
                self.fetch_trades(symbol, limit=limit, **kwargs)
            )
        except Exception as e:
            self.logger.error(f"Error in get_recent_trades: {str(e)}")
            return None

    # -------------------------
    # Account & Balance Methods
    # -------------------------
    async def fetch_balance(self, params=None):
        """Fetch account balance."""
        self._ensure_auth()
        
        try:
            result = {
                'info': {},
                'timestamp': int(time.time() * 1000),
                'datetime': self.iso8601(time.time() * 1000),
                'free': {},
                'used': {},
                'total': {},
            }
            
            if self.account is None:
                self.logger.warning("Account not initialized, returning empty balance")
                return result
                
            # Get account balances
            balances = self.account.balances
            
            for asset in balances:
                symbol = asset['symbol']
                free = Decimal(str(asset['amount']))
                
                # In BitShares, the amount is the available balance
                result['free'][symbol] = free
                result['total'][symbol] = free
                result['used'][symbol] = Decimal('0')
                
                # Add to info
                result['info'][symbol] = {
                    'free': float(free),
                    'used': 0.0,
                    'total': float(free)
                }
                
            return result
            
        except Exception as e:
            self.logger.error(f"Error fetching balance: {str(e)}")
            raise
            
    def get_balance(self, **kwargs: dict):
        """Get balance (synchronous wrapper for fetch_balance)."""
        import asyncio
        try:
            balance = asyncio.get_event_loop().run_until_complete(
                self.fetch_balance(**kwargs)
            )
            
            # Transform to the format expected by OctoBot
            result = {}
            for symbol, amount in balance.get('total', {}).items():
                if amount > 0:
                    result[symbol] = amount
                    
            return result
            
        except Exception as e:
            self.logger.error(f"Error in get_balance: {str(e)}")
            return {}

    def get_default_balance(self):
        """Get default balance (all assets)."""
        return self.get_balance()

    # -------------------------
    # Utility Methods
    # -------------------------
    def iso8601(self, timestamp: Optional[int] = None) -> str:
        """Convert timestamp to ISO8601 format."""
        if timestamp is None:
            timestamp = int(time.time() * 1000)
        return datetime.datetime.utcfromtimestamp(timestamp / 1000).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        
    def amount_to_precision(self, symbol: str, amount: float) -> Decimal:
        """Convert amount to exchange precision."""
        try:
            market = self.get_market(symbol)
            precision = market.get('precision', {}).get('amount', 8)
            return Decimal(str(amount)).quantize(
                Decimal('1') / (10 ** precision),
                rounding=decimal.ROUND_DOWN
            )
        except Exception as e:
            self.logger.warning(f"Error in amount_to_precision: {str(e)}")
            return Decimal(str(amount))
            
    def price_to_precision(self, symbol: str, price: float) -> Decimal:
        """Convert price to exchange precision."""
        try:
            market = self.get_market(symbol)
            precision = market.get('precision', {}).get('price', 8)
            return Decimal(str(price)).quantize(
                Decimal('1') / (10 ** precision),
                rounding=decimal.ROUND_DOWN
            )
        except Exception as e:
            self.logger.warning(f"Error in price_to_precision: {str(e)}")
            return Decimal(str(price))
            
    def currency_to_precision(self, currency: str, value: float) -> Decimal:
        """Convert value to currency precision."""
        # Default to 8 decimal places for most assets
        precision = 8
        
        # Special cases for known assets
        if currency.upper() in ['BTC', 'XBTC']:
            precision = 8
        elif currency.upper() in ['USDT', 'USDC']:
            precision = 6
            
        return Decimal(str(value)).quantize(
            Decimal('1') / (10 ** precision),
            rounding=decimal.ROUND_DOWN
        )
        
    def parse_timeframe(self, timeframe: str) -> int:
        """Convert timeframe string to seconds."""
        amount = int(timeframe[:-1])
        unit = timeframe[-1].lower()
        
        if unit == 'm':
            return amount * 60
        elif unit == 'h':
            return amount * 3600
        elif unit == 'd':
            return amount * 86400
        elif unit == 'w':
            return amount * 604800
        elif unit == 'M':
            return amount * 2592000
        else:
            return int(timeframe)  # Default to seconds

    # -------------------------
    # Orders
    # -------------------------
    def _enforce_slippage(self, side: trading_enums.TradeOrderSide, symbol: str, price: Decimal) -> None:
        """Enforce maximum allowed slippage for an order."""
        try:
            best_bid, best_ask = self._top_of_book(symbol)
            if best_bid is None or best_ask is None:
                self.logger.warning("Could not get market data for slippage check")
                return  # can't compute
                
            if side == trading_enums.TradeOrderSide.BUY:
                # price must not exceed ask by more than MAX_SLIPPAGE
                allowed = best_ask * (Decimal("1") + self._max_slippage)
                if price > allowed:
                    raise ValueError(
                        f"Slippage > {self._max_slippage*100}%: "
                        f"price {price} > allowed {allowed} (best ask: {best_ask})"
                    )
            elif side == trading_enums.TradeOrderSide.SELL:
                allowed = best_bid * (Decimal("1") - self._max_slippage)
                if price < allowed:
                    raise ValueError(
                        f"Slippage > {self._max_slippage*100}%: "
                        f"price {price} < allowed {allowed} (best bid: {best_bid})"
                    )
                    
        except Exception as e:
            self.logger.error(f"Error in slippage check: {str(e)}")
            raise

    async def create_order_async(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        params: dict = None,
    ) -> dict:
        """Create a new order asynchronously."""
        try:
            self._ensure_auth()
            
            # Convert parameters to appropriate types
            amount = Decimal(str(amount))
            price = Decimal(str(price)) if price is not None else None
            
            # Validate parameters
            if not symbol or not order_type or not side or amount <= 0:
                raise ValueError("Invalid order parameters")
                
            # Only limit orders are supported
            if order_type.upper() not in ['LIMIT', 'LIMIT_MAKER']:
                raise NotImplementedError(f"Order type {order_type} is not supported. Only LIMIT orders are supported.")
                
            # Convert side to TradeOrderSide
            side_enum = trading_enums.TradeOrderSide.BUY if side.upper() == 'BUY' else trading_enums.TradeOrderSide.SELL
            
            # Slippage check for market orders (not applicable for limit orders)
            if order_type.upper() == 'MARKET' and price is not None:
                self._enforce_slippage(side_enum, symbol, Decimal(str(price)))
                
            # Get market info
            market = self.get_market(symbol)
            
            # Round amount and price to market precision
            amount = self.amount_to_precision(symbol, amount)
            if price is not None:
                price = self.price_to_precision(symbol, price)
                
            # Select fee asset
            fee_asset = self._select_fee_asset()
            
            # Create market object
            market_obj = Market(symbol, blockchain_instance=self.bitshares)
            
            # Place the order
            try:
                if side_enum == trading_enums.TradeOrderSide.BUY:
                    tx = market_obj.buy(
                        price=float(price),
                        amount=float(amount),
                        account=self.account,
                        fee_asset=fee_asset,
                        **params or {}
                    )
                else:
                    tx = market_obj.sell(
                        price=float(price),
                        amount=float(amount),
                        account=self.account,
                        fee_asset=fee_asset,
                        **params or {}
                    )
                    
                # Extract order ID from transaction
                order_id = None
                try:
                    order_id = tx.get("operation_results", [[None, [None]]])[0][1]
                except (IndexError, AttributeError):
                    self.logger.warning("Could not extract order ID from transaction")
                    
                # Return order info in CCXT format
                return {
                    'id': str(order_id) if order_id else None,
                    'info': tx,
                    'timestamp': int(time.time() * 1000),
                    'datetime': self.iso8601(int(time.time() * 1000)),
                    'lastTradeTimestamp': None,
                    'symbol': symbol,
                    'type': order_type.lower(),
                    'side': side.lower(),
                    'price': float(price) if price is not None else None,
                    'amount': float(amount),
                    'cost': float(amount * price) if price is not None else None,
                    'average': None,
                    'filled': 0.0,
                    'remaining': float(amount),
                    'status': 'open',
                    'fee': None,
                    'trades': None,
                }
                
            except Exception as e:
                self.logger.error(f"Error placing {side} order: {str(e)}")
                raise
                
        except Exception as e:
            self.logger.error(f"Error in create_order_async: {str(e)}")
            raise
            
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
        """Create a new order (synchronous wrapper for create_order_async)."""
        import asyncio
        
        try:
            # Convert order type and side to strings expected by create_order_async
            order_type_str = "LIMIT"  # Default to LIMIT
            if hasattr(order_type, 'name'):
                if 'MARKET' in order_type.name:
                    order_type_str = 'MARKET'
                    
            side_str = str(side).split('.')[-1] if hasattr(side, 'name') else str(side)
            
            # Call the async version
            result = asyncio.get_event_loop().run_until_complete(
                self.create_order_async(
                    symbol=symbol,
                    order_type=order_type_str,
                    side=side_str,
                    amount=float(quantity),
                    price=float(price) if price is not None else None,
                    params=params or {}
                )
            )
            
            # Convert back to the format expected by OctoBot
            return {
                "exchange_order_id": result.get('id'),
                "transaction": result.get('info', {}),
                "symbol": symbol,
                "side": side_str.upper(),
                "price": str(price) if price is not None else None,
                "quantity": str(quantity),
                "fee_asset": None,  # Will be set by the exchange
            }
            
        except Exception as e:
            self.logger.error(f"Error in create_order: {str(e)}")
            return None

    async def cancel_order_async(self, order_id: str, symbol: Optional[str] = None, **kwargs: dict) -> dict:
        """Cancel an order asynchronously."""
        try:
            self._ensure_auth()
            
            if not order_id:
                raise ValueError("Order ID is required")
                
            # Cancel the order
            result = self.bitshares.cancel(order_id)
            
            # Return result in CCXT format
            return {
                'info': result,
                'id': order_id,
                'symbol': symbol,
                'status': 'canceled' if result else 'error',
            }
            
        except Exception as e:
            self.logger.error(f"Error canceling order {order_id}: {str(e)}")
            raise
            
    def cancel_order(self, exchange_order_id: str, symbol: str, order_type: trading_enums.TraderOrderType, **kwargs: dict):
        """Cancel an order (synchronous wrapper for cancel_order_async)."""
        import asyncio
        
        try:
            if not self.bitshares:
                raise RuntimeError("BitShares client not initialized")
                
            result = asyncio.get_event_loop().run_until_complete(
                self.cancel_order_async(exchange_order_id, symbol, **kwargs)
            )
            
            # Convert to OctoBot status
            return trading_enums.OrderStatus.CANCELED if result.get('status') == 'canceled' else trading_enums.OrderStatus.ERROR
            
        except Exception as e:
            self.logger.error(f"Error in cancel_order: {str(e)}")
            return trading_enums.OrderStatus.ERROR

    async def fetch_order(self, order_id: str, symbol: Optional[str] = None, **kwargs: dict) -> dict:
        """Fetch an order by ID asynchronously."""
        try:
            # First try to find in open orders
            orders = await self.fetch_open_orders(symbol=symbol)
            for order in orders:
                if str(order.get('id')) == str(order_id):
                    return order
                    
            # If not found in open orders, try closed orders
            closed_orders = await self.fetch_closed_orders(symbol=symbol)
            for order in closed_orders:
                if str(order.get('id')) == str(order_id):
                    return order
                    
            # Order not found
            raise ValueError(f"Order {order_id} not found")
            
        except Exception as e:
            self.logger.error(f"Error fetching order {order_id}: {str(e)}")
            raise
            
    def get_order(self, exchange_order_id: str, symbol: str = None, **kwargs: dict) -> dict:
        """Get order by ID (synchronous wrapper for fetch_order)."""
        import asyncio
        
        try:
            return asyncio.get_event_loop().run_until_complete(
                self.fetch_order(exchange_order_id, symbol, **kwargs)
            )
        except Exception as e:
            self.logger.error(f"Error in get_order: {str(e)}")
            return {}

    async def fetch_open_orders(self, symbol: Optional[str] = None, since: Optional[int] = None, 
                              limit: Optional[int] = None, **kwargs: dict) -> list:
        """Fetch open orders asynchronously."""
        try:
            self._ensure_auth()
            
            if self.account is None:
                raise RuntimeError("Account not initialized")
                
            # Get open orders from the account
            raw_orders = self.account.openorders
            
            # Convert to CCXT format
            orders = []
            for order in raw_orders:
                try:
                    # Skip if symbol doesn't match
                    if symbol and not self._order_matches_symbol(order, symbol):
                        continue
                        
                    # Parse order data
                    order_data = self._parse_order(order, symbol)
                    if order_data:
                        orders.append(order_data)
                        
                except Exception as e:
                    self.logger.warning(f"Error parsing order {order.get('id')}: {str(e)}")
                    continue
            
            # Apply limit if specified
            if limit is not None and orders:
                orders = orders[:int(limit)]
                
            return orders
            
        except Exception as e:
            self.logger.error(f"Error fetching open orders: {str(e)}")
            raise
            
    async def fetch_closed_orders(self, symbol: Optional[str] = None, since: Optional[int] = None, 
                                 limit: Optional[int] = None, **kwargs: dict) -> list:
        """Fetch closed orders asynchronously."""
        # In BitShares, we can only get closed orders from the account history
        # This is a simplified implementation that returns an empty list
        return []
        
    def _order_matches_symbol(self, order: dict, symbol: str) -> bool:
        """Check if an order matches the given symbol."""
        try:
            base, quote = self.get_split_pair_from_exchange(symbol)
            order_str = json.dumps(order).lower()
            return base.lower() in order_str and quote.lower() in order_str
        except Exception:
            return False
            
    def _parse_order(self, order: dict, symbol: Optional[str] = None) -> Optional[dict]:
        """Parse order data to CCXT format."""
        try:
            # Extract order details
            order_id = str(order.get('id', ''))
            
            # Determine order side (buy or sell)
            side = 'buy' if 'buy' in str(order).lower() else 'sell'
            
            # Extract price and amount
            price = Decimal(str(order.get('price', 0)))
            amount = Decimal(str(order.get('amount', 0)))
            
            # Determine order status
            status = 'open'  # Default status for open orders
            
            # Extract timestamp
            timestamp = int(time.time() * 1000)  # Default to current time
            
            # Create order in CCXT format
            return {
                'id': order_id,
                'info': order,
                'timestamp': timestamp,
                'datetime': self.iso8601(timestamp),
                'lastTradeTimestamp': None,
                'symbol': symbol or 'UNKNOWN',
                'type': 'limit',  # Only limit orders are supported
                'side': side,
                'price': float(price),
                'amount': float(amount),
                'cost': float(price * amount),
                'average': None,
                'filled': 0.0,  # Not provided by BitShares
                'remaining': float(amount),  # Not provided by BitShares
                'status': status,
                'fee': None,
                'trades': None,
            }
            
        except Exception as e:
            self.logger.warning(f"Error parsing order: {str(e)}")
            return None
            
    def get_open_orders(self, symbol: str = None, since: int = None, limit: int = None, **kwargs: dict) -> list:
        """Get open orders (synchronous wrapper for fetch_open_orders)."""
        import asyncio
        
        try:
            return asyncio.get_event_loop().run_until_complete(
                self.fetch_open_orders(symbol=symbol, since=since, limit=limit, **kwargs)
            )
        except Exception as e:
            self.logger.error(f"Error in get_open_orders: {str(e)}")
            return []
