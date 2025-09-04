import numpy as np
import logging
from typing import List, Dict, Optional, Tuple
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.mexc_api import MexcAPI


class CandleAnalyzer:
    def __init__(self, api: MexcAPI):
        """
        :param api: אובייקט MexcAPI קיים שממנו נשלוף את הנרות
        """
        self.api = api

    async def get_candles(self, symbol: str, interval: str = "Min1", limit: int = 30) -> Tuple[List[Dict], Dict, Dict]:
        """
        מחזירה את 30 הנרות האחרונים הסגורים, הנר הסגור האחרון והנר החי.
        """
        closed, last_closed, live = await self.api.get_candles_with_live(symbol, interval, limit)
        if not closed or not last_closed or not live:
            logging.error(f"❌ לא התקבלו נתונים תקינים מ-API עבור {symbol}")
            return [], {}, {}
        return closed, last_closed, live

    def calc_zscore(self, candles: List[Dict]) -> float:
        """
        מחשבת z-score לנר האחרון לפי ה-vol של נרות קודמים
        """
        if len(candles) < 5:  # צריך מינימום נרות
            return 0.0

        volumes = [c["vol"] for c in candles[:-1]]  # נרות קודמים
        current_vol = candles[-1]["vol"]

        mean = np.mean(volumes)
        std = np.std(volumes)

        if std == 0:
            return 0.0
        return (current_vol - mean) / std

    def calc_atr(self, candles: List[Dict], period: int = 14) -> float:
        """
        מחשבת ATR (Average True Range) על סמך נרות קודמים
        """
        if len(candles) < period + 1:
            return 0.0

        trs = []
        for i in range(1, period + 1):
            high = candles[-i]["high"]
            low = candles[-i]["low"]
            prev_close = candles[-i - 1]["close"]

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            trs.append(tr)

        return np.mean(trs)

    async def analyze(self, symbol: str, interval: str = "Min1", limit: int = 30) -> Optional[Dict]:
        """
        מביאה נרות, מחשבת z-score ו-ATR לנר החי,
        תוך שימוש בנתוני 30 נרות סגורים + נר סגור אחרון + נר חי.
        """
        closed, last_closed, live = await self.get_candles(symbol, interval, limit)
        if not closed:
            return None

        z = self.calc_zscore(closed + [live])  # מחשבים על הנרות הסגורים + הנר החי
        atr = self.calc_atr(closed)

        result = {
            "time": live["time"],
            "open": live["open"],
            "close": live["close"],
            "high": live["high"],
            "low": live["low"],
            "vol": live["vol"],
            "zscore": z,
            "atr": atr,
            "last_closed": last_closed  # נשמור גם את הנר הסגור האחרון
        }
        return result


import asyncio
import logging
from services.mexc_api import MexcAPI
from helpers.CandleAnalyzer import CandleAnalyzer


async def main():
    logging.basicConfig(level=logging.INFO)

    api = MexcAPI("API_KEY", "SECRET_KEY")
    analyzer = CandleAnalyzer(api)

    await api.start_session()
    try:
        result = await analyzer.analyze("BTC_USDT", interval="Min1", limit=30)
        if result:
            print("📊 תוצאה:")
            for k, v in result.items():
                print(f"{k}: {v}")
        else:
            print("❌ לא התקבלה תוצאה")
    finally:
        await api.close_session()


if __name__ == "__main__":
    asyncio.run(main())
