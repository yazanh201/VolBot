import time
import logging
from typing import Dict, Any, Optional


class CacheManager:
    def __init__(self, mexc_api, ws, balance_ttl: int = 60):
        """
        מנהל מטמון עבור contract specs, balance, ומחירים.
        
        :param mexc_api: מופע של MexcAPI
        :param ws: מופע של WebSocket (למחירי live)
        :param balance_ttl: זמן חיי מטמון balance בשניות (ברירת מחדל: 60 שניות)
        """
        self.mexc_api = mexc_api
        self.ws = ws
        self.balance_ttl = balance_ttl

        # Cache dicts
        self._specs_cache: Dict[str, Dict[str, Any]] = {}
        self._balance_cache: Optional[float] = None
        self._balance_ts: float = 0.0

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



import asyncio
import logging
import os
from dotenv import load_dotenv

# יבוא המחלקה שלך
from cashe.cache_manager import CacheManager
from services.mexc_api import MexcAPI
from services.mexc_ws import MexcWebSocket


async def main():
    # טען משתני סביבה
    load_dotenv()

    # הכנת לוגים
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)s | %(message)s")

    # יצירת מופעי API ו־WS
    mexc_api = MexcAPI(os.getenv("MEXC_API_KEY_WEB2", ""), os.getenv("MEXC_API_SECRET_WEB", ""))
    ws = MexcWebSocket(["BTC_USDT", "SOL_USDT"])

    # יצירת CacheManager
    cache = CacheManager(mexc_api, ws)

    # הפעלת WebSocket ברקע
    asyncio.create_task(ws.run())

    # --- בדיקות ---
    # בדיקה 1: contract specs
    specs = await cache.get_contract_specs("BTC_USDT")
    print("\n📄 Contract Specs:", specs)

    # בדיקה 2: balance
    balance = await cache.get_balance()
    print(f"\n💰 Balance: {balance}")

    # בדיקה 3: last price מ־WS
    await asyncio.sleep(3)  # לתת זמן ל־WS להתמלא במחירים
    price = cache.get_last_price("BTC_USDT")
    print(f"\n💹 Last Price BTC_USDT: {price}")


if __name__ == "__main__":
    asyncio.run(main())
