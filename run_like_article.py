"""Chay dung nhu muc 'Running the Bot' trong bai viet (khong dung CLI cua main.py)."""
import os

from dotenv import load_dotenv

from config import TradingConfig
from logging_setup import setup_logging
from trading_bot import TradingBot

load_dotenv()
setup_logging()

# Create and configure the bot
config = TradingConfig(
    api_key=os.getenv('ALPACA_API_KEY'),
    api_secret=os.getenv('ALPACA_API_SECRET'),
    symbols=['AAPL', 'GOOGL', 'MSFT', 'TSLA', 'NVDA']
)
bot = TradingBot(config)

# Single cycle (testing)
bot.run_trading_cycle()

# Continuous operation: bo comment dong duoi de chay lien tuc moi 30 phut
# bot.run_continuous(interval_minutes=30)
