import time
import logging
from typing import Dict, Any, Optional


class CacheManager:
    def __init__(self, mexc_api, ws,
                 balance_ttl: int = 60,
                 fair_price_ttl: int = 30,
                 funding_rate_ttl: int = 300,
                 order_book_ttl: int = 3):
        self.mexc_api = mexc_api
        self.ws = ws
        self.balance_ttl = balance_ttl
        self.fair_price_ttl = fair_price_ttl
        self.funding_rate_ttl = funding_rate_ttl
        self.order_book_ttl = order_book_ttl

        # Cache dicts
        self._specs_cache = {}
        self._balance_cache = None
        self._balance_ts = 0.0

        self._fair_price_cache = {}
        self._fair_price_ts = {}

        self._funding_rate_cache = {}
        self._funding_rate_ts = {}

        self._order_book_cache = {}
        self._order_book_ts = {}

    async def get_contract_specs(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        מחזיר contract specs מהמטמון אם קיים, אחרת טוען מה־API ושומר.
        """
        if symbol in self._specs_cache:
            return self._specs_cache[symbol]

        try:
            specs = await self.mexc_api.get_contract_specs(symbol)
            if specs and specs.get("data"):
                data = specs["data"][0] if isinstance(specs["data"], list) else specs["data"]
                self._specs_cache[symbol] = data
                return data
        except Exception as e:
            logging.error(f"❌ שגיאה בשליפת contract specs עבור {symbol}: {e}")

        return None

    async def get_balance(self) -> Optional[float]:
        """
        מחזיר balance מהמטמון אם הוא עדיין בתוקף, אחרת שולף מה־API ומעדכן.
        """
        now = time.time()
        if self._balance_cache is not None and (now - self._balance_ts) < self.balance_ttl:
            return self._balance_cache

        try:
            balance = await self.mexc_api.get_usdt_balance()
            if balance is not None:
                self._balance_cache = balance
                self._balance_ts = now
                return balance
        except Exception as e:
            logging.error(f"❌ שגיאה בשליפת balance: {e}")

        return None

    def get_last_price(self, symbol: str) -> Optional[float]:
        """
        מחזיר את המחיר האחרון מה־WebSocket (live).
        """
        try:
            return self.ws.get_price(symbol)
        except Exception as e:
            logging.error(f"❌ שגיאה בשליפת מחיר חי עבור {symbol}: {e}")
            return None

    def get_price_scale(self, symbol: str) -> int:
        """
        מחזירה את מספר הספרות אחרי הנקודה שמותר למחיר (priceScale).
        נשלף מתוך ה-contract specs ששמור במטמון.
        """
        specs = self._specs_cache.get(symbol)
        if specs:
            return int(specs.get("priceScale", 2))  # ברירת מחדל: 2
        return 2
    

    async def get_fair_price(self, symbol: str) -> Optional[float]:
        now = time.time()
        if (symbol in self._fair_price_cache and
            now - self._fair_price_ts.get(symbol, 0) < self.fair_price_ttl):
            return self._fair_price_cache[symbol]

        try:
            fair_price = await self.mexc_api.get_fair_price(symbol)
            if fair_price is not None:
                self._fair_price_cache[symbol] = fair_price
                self._fair_price_ts[symbol] = now
                return fair_price
        except Exception as e:
            logging.error(f"❌ שגיאה בשליפת fair price: {e}")
        return None

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        now = time.time()
        if (symbol in self._funding_rate_cache and
            now - self._funding_rate_ts.get(symbol, 0) < self.funding_rate_ttl):
            return self._funding_rate_cache[symbol]

        try:
            rate = await self.mexc_api.get_funding_rate(symbol)
            if rate is not None:
                self._funding_rate_cache[symbol] = rate
                self._funding_rate_ts[symbol] = now
                return rate
        except Exception as e:
            logging.error(f"❌ שגיאה בשליפת funding rate: {e}")
        return None

    async def get_order_book(self, symbol: str, limit: int = 20) -> Optional[dict]:
        now = time.time()
        if (symbol in self._order_book_cache and
            now - self._order_book_ts.get(symbol, 0) < self.order_book_ttl):
            return self._order_book_cache[symbol]

        try:
            order_book = await self.mexc_api.get_order_book(symbol, limit=limit)
            if order_book:
                self._order_book_cache[symbol] = order_book
                self._order_book_ts[symbol] = now
                return order_book
        except Exception as e:
            logging.error(f"❌ שגיאה בשליפת order book: {e}")
        return None


import asyncio
import os
from dotenv import load_dotenv
from services.mexc_api import MexcAPI
from services.mexc_ws import MexcWebSocket

if __name__ == "__main__":
    async def run():
        load_dotenv()

        mexc_api = MexcAPI(os.getenv("MEXC_API_KEY_WEB2", ""), os.getenv("MEXC_API_SECRET_WEB", ""))
        ws = MexcWebSocket(["BTC_USDT", "SOL_USDT"])
        cache = CacheManager(mexc_api, ws)

        # הפעלת WebSocket ברקע
        asyncio.create_task(ws.run())

        try:
            # --- בדיקות ---
            specs = await cache.get_contract_specs("BTC_USDT")
            print("\n📄 Contract Specs:", specs)

            balance = await cache.get_balance()
            print(f"\n💰 Balance: {balance}")

            await asyncio.sleep(3)  # לתת ל־WS זמן להתמלא במחירים
            price = cache.get_last_price("BTC_USDT")
            print(f"\n💹 Last Price BTC_USDT: {price}")

            fair_price = await cache.get_fair_price("BTC_USDT")
            print(f"\n📈 Fair Price BTC_USDT: {fair_price}")

            funding_rate = await cache.get_funding_rate("BTC_USDT")
            print(f"\n💸 Funding Rate BTC_USDT: {funding_rate}")

            order_book = await cache.get_order_book("BTC_USDT", limit=5)
            if order_book:
                print(f"\n📊 Order Book BTC_USDT:")
                print("Bids:", order_book["bids"][:3])
                print("Asks:", order_book["asks"][:3])
                print("Timestamp:", order_book["timestamp"])

        finally:
            await ws.close()
            await mexc_api.close_session()

    asyncio.run(run())
