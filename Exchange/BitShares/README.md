# BitShares Exchange Tentacle for OctoBot

This tentacle integrates the BitShares DEX into OctoBot.

- Market data: XBTS REST API (`https://cmc.xbts.io/v2`)
- Trading: python-bitshares over WSS nodes
  - Primary: `wss://cloud.xbts.io/ws`
  - Failover: `wss://dex.iobanker.com/ws`
- Secure ACTIVE WIF prompt only at first trading action (not stored)
- Order types: BUY_LIMIT and SELL_LIMIT
- Slippage guard: 10% max relative slippage
- Fee asset preference: on-chain symbols (e.g., `XBTSX.NCH`, fallback `BTS`)

## Paths

- Code: `tentacles/Exchange/BitShares/bitshares_exchange.py`
- Config: `tentacles/Exchange/BitShares/config.py`

## On-chain symbols (critical)

Gateway-issued assets use on-chain prefixed symbols. For XBTS, symbols are like `XBTSX.NCH`, `XBTSX.USDC`.

`config.py` defaults to no market restriction. Example configuration:

- `XBTSX.NCH/BTS`
- `XBTSX.USDC/BTS`

Market data endpoints may display unprefixed names (e.g., `NCH/BTS`), but order placement MUST use the exact on-chain symbols.

## Security and keys

- ACTIVE WIF is prompted via the terminal at first trading call using a secure prompt.
- WIF is never stored in files or env vars by this tentacle.
- If a WIF is ever exposed, rotate the ACTIVE key on your BitShares account immediately.

## Installation

This project uses `uv` for dependency management. Dependencies are pinned for compatibility with python-bitshares signing.

```bash
uv sync
```

Important pin: `ecdsa==0.13.3` to resolve signing incompatibilities with graphenebase.

## Quick non-trading smoke tests

```bash
uv run python - <<'PY'
from tentacles.Exchange.BitShares.bitshares_exchange import BitSharesExchange
import asyncio

class EM:  # minimal exchange_manager shim
    exchange_class_string = 'BitShares'
    tentacles_setup_config = None

async def main():
    exch = BitSharesExchange(config={}, exchange_manager=EM(), exchange_config_by_exchange=None)
    await exch.initialize(force=True)
    print('Ticker NCH/BTS:', exch.get_price_ticker('NCH/BTS'))
    print('Orderbook NCH/BTS (top 3):', {k: v[:3] for k, v in exch.get_order_book('NCH/BTS', limit=3).items()})
    print('Recent trades NCH/BTS (5):', exch.get_recent_trades('NCH/BTS', limit=5))
    await exch.stop()

asyncio.run(main())
PY
```

## Live tiny test (BUY_LIMIT)
This places a tiny BUY for an example market `XBTSX.NCH/BTS` at best ask (+invert to BTS/BASE internally). You will be prompted for ACTIVE WIF.

```bash
uv run python - <<'PY'
from tentacles.Exchange.BitShares.bitshares_exchange import BitSharesExchange
from octobot_trading import enums as E
from decimal import Decimal
import asyncio

class EM:
    exchange_class_string = 'BitShares'
    tentacles_setup_config = None

SYMBOL_ONCHAIN = 'XBTSX.NCH/BTS'
SYMBOL_API     = 'NCH/BTS'
AMOUNT = Decimal('2')

async def main():
    exch = BitSharesExchange(config={}, exchange_manager=EM(), exchange_config_by_exchange=None)
    await exch.initialize(force=True)
    ob = exch.get_order_book(SYMBOL_API, limit=5)
    a0 = ob.get('asks', [])
    if not a0:
        print('No asks'); await exch.stop(); return
    api_price = Decimal(str(a0[0][0] if isinstance(a0[0], (list, tuple)) else a0[0].get('price')))
    price = Decimal('1')/api_price  # convert to BTS/BASE for BitShares
    order = exch.create_order(
        order_type=E.TraderOrderType.BUY_LIMIT,
        symbol=SYMBOL_ONCHAIN,
        quantity=AMOUNT,
        price=price,
        side=E.TradeOrderSide.BUY,
    )
    print('Order response:', order)
    await exch.stop()

asyncio.run(main())
PY
```

## Design notes

- `BitSharesExchange` subclasses OctoBot `AbstractExchange` and implements required methods: market data, balances, create/cancel/get order(s).
- Slippage guard: compares intended limit price vs top-of-book to ensure deviation ≤ 10%.
- Fee asset selection scans balances using `FEE_ASSET_PREFERENCE` in `config.py`.

## Troubleshooting

- AssetDoesNotExistsException: ensure you are using the on-chain symbol (e.g., `XBTSX.NCH`, not `NCH`).
- UnhandledRPCError: Insufficient balance — size the order to your available BTS (or fee asset) after fees.
- Signing error (`PointJacobi`): ensure `ecdsa==0.13.3` is installed (`uv sync`).

## License

Follow OctoBot’s contribution guidelines and license terms.
