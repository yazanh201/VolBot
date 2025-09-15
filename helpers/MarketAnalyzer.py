import logging
from typing import Optional, Dict, Any
import statistics


class MarketAnalyzer:
    def __init__(self, cache):
        """
        מחלקת ניתוח שמבצעת חישובים משולבים על בסיס נתוני CacheManager
        :param cache: מופע של CacheManager
        """
        self.cache = cache

    async def analyze_market(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        שולף נתונים חיוניים (fair price, funding rate, order book)
        ומבצע חישובים להערכת מצב השוק.
        """
        try:
            # --- שליפת נתונים מהמטמון ---
            fair_price = await self.cache.get_fair_price(symbol)
            funding_rate = await self.cache.get_funding_rate(symbol)
            order_book = await self.cache.get_order_book(symbol, limit=20)
            last_price = self.cache.get_last_price(symbol)

            if not fair_price or not order_book or not last_price:
                logging.warning(f"⚠️ לא ניתן לחשב ניתוח עבור {symbol}")
                return None

            bids = order_book["bids"]
            asks = order_book["asks"]

            # --- חישובים פשוטים ---
            best_bid = bids[0][0] if bids else None
            best_ask = asks[0][0] if asks else None
            spread = best_ask - best_bid if (best_bid and best_ask) else None

            # עומק (Volume) בצדדים
            total_bid_vol = sum([b[1] for b in bids[:5]]) if bids else 0
            total_ask_vol = sum([a[1] for a in asks[:5]]) if asks else 0
            imbalance = (total_bid_vol - total_ask_vol) / (total_bid_vol + total_ask_vol + 1e-9)

            # סטייה מה־Fair Price
            deviation = (last_price - fair_price) / fair_price

            # החבילה שתוחזר
            return {
                "symbol": symbol,
                "last_price": last_price,
                "fair_price": fair_price,
                "funding_rate": funding_rate,
                "spread": spread,
                "imbalance": imbalance,       # בין קונים למוכרים
                "deviation": deviation        # עד כמה המחיר חורג מה-fair price
            }

        except Exception as e:
            logging.error(f"❌ שגיאה בניתוח שוק עבור {symbol}: {e}", exc_info=True)
            return None

import asyncio
import logging
import os
from dotenv import load_dotenv

from cashe.cache_manager import CacheManager
from services.mexc_api import MexcAPI
from services.mexc_ws import MexcWebSocket
from helpers.MarketAnalyzer import MarketAnalyzer


async def main():
    logging.basicConfig(level=logging.INFO)
    load_dotenv()

    mexc_api = MexcAPI(os.getenv("MEXC_API_KEY_WEB2", ""), os.getenv("MEXC_API_SECRET_WEB", ""))
    await mexc_api.start_session()  # 👈 פתח חיבור API

    ws = MexcWebSocket(["BTC_USDT", "SOL_USDT"])
    asyncio.create_task(ws.run())

    cache = CacheManager(mexc_api, ws)
    analyzer = MarketAnalyzer(cache)

    # לתת קצת זמן ל־WS
    await asyncio.sleep(3)

    result = await analyzer.analyze_market("BTC_USDT")
    print("\n📊 Market Analysis BTC_USDT:")
    for k, v in result.items():
        print(f"{k}: {v}")

    # 👇 סגירה מסודרת של משאבים
    await mexc_api.close_session()
    await ws.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bye 👋")
