# BitShares/XBTS Exchange Tentacle configuration
# Non-secret runtime parameters. Secrets are prompted at startup.

# Ordered list of BitShares WSS nodes for failover
WSS_NODES = [
    "wss://cloud.xbts.io/ws",
    "wss://dex.iobanker.com/ws",
]

# Trading account name
ACCOUNT_NAME = "ness-privateness"

# Enabled markets (BitShares asset symbols). Use exact on-chain symbols.
# Format: "BASE/QUOTE"
# Note: gateway-issued assets are prefixed (e.g., XBTSX.ASSET)
# Default: no restriction. Configure as needed, e.g.:
ENABLED_MARKETS = ["XBTSX.NCH/BTS", "XBTSX.USDC/BTS"]
#ENABLED_MARKETS = []

# Slippage guard: maximum allowed relative slippage during order placement
# Example: 0.10 means 10%
MAX_SLIPPAGE = 0.10

# Fee asset selection preferences (ordered). If first available in balance, use it.
# Use on-chain symbols (gateway-prefixed) for correct matching
FEE_ASSET_PREFERENCE = ["XBTSX.NCH", "BTS"]

# XBTS Market API base URL (read-only market data)
XBTS_API_BASE = "https://cmc.xbts.io/v2"
