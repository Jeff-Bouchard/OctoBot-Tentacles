#!/usr/bin/env python3
"""
Test script for BitShares exchange tentacle.
"""
import asyncio
import logging
from decimal import Decimal

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('BitSharesTest')

async def test_exchange():
    """Test the BitShares exchange tentacle."""
    from bitshares_exchange import BitSharesExchange
    
    # Initialize the exchange
    exchange = BitSharesExchange(config={}, exchange_manager=None)
    
    try:
        # Initialize the exchange
        logger.info("Initializing exchange...")
        await exchange.initialize_impl()
        
        # Test fetching ticker
        symbol = "XBTSX.NCH/BTS"
        logger.info(f"Fetching ticker for {symbol}...")
        ticker = exchange.get_price_ticker(symbol)
        logger.info(f"Ticker: {ticker}")
        
        # Test fetching order book
        logger.info(f"Fetching order book for {symbol}...")
        orderbook = exchange.get_order_book(symbol, limit=5)
        logger.info(f"Order book: {orderbook}")
        
        # Test fetching recent trades
        logger.info(f"Fetching recent trades for {symbol}...")
        trades = exchange.get_recent_trades(symbol, limit=5)
        logger.info(f"Recent trades: {trades}")
        
        # Test fetching balance (will prompt for WIF if not authenticated)
        logger.info("Fetching balance...")
        balance = exchange.get_balance()
        logger.info(f"Balance: {balance}")
        
    except Exception as e:
        logger.error(f"Error during test: {str(e)}", exc_info=True)
    finally:
        # Clean up
        await exchange.stop()

if __name__ == "__main__":
    asyncio.run(test_exchange())
