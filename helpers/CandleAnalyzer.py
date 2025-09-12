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

    async def get_candles(self, symbol: str, interval: str = "Min1", limit: int = 50) -> Tuple[List[Dict], Dict, Dict]:
        """
        מחזירה את הנרות האחרונים, הנר הסגור האחרון והנר החי.
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

    def calc_atr(self, candles: List[Dict], period: int = 10) -> float:
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

    def calc_body_range(self, candle: Dict) -> float:
        """
        יחס גוף/טווח: |close - open| ÷ (high - low)
        ערך גבוה → נר עם מומנטום (גוף גדול יחסית לפתיל).
        """
        body = abs(candle["close"] - candle["open"])
        rng = candle["high"] - candle["low"]
        return body / rng if rng > 0 else 0.0

    def calc_bollinger_percent(self, candles: List[Dict], price: float, period: int = 20, stddev: float = 2.0) -> float:
        """
        מחשבת %B (מיקום בתוך בולינג'ר בנדס).
        %B = (price - Lower) / (Upper - Lower)
        """
        closes = [c["close"] for c in candles[-period:]]
        if len(closes) < period:
            return 0.5  # ברירת מחדל: באמצע

        mean = np.mean(closes)
        std = np.std(closes)
        upper = mean + stddev * std
        lower = mean - stddev * std

        return (price - lower) / (upper - lower) if (upper - lower) > 0 else 0.5

    def calc_rvol(self, candles: List[Dict], live_vol: float, period: int = 30) -> float:
        """
        מחשבת Relative Volume (RVOL): live_vol ÷ mean(volume[-period:])
        """
        vols = [c["vol"] for c in candles[-period:]]
        avg_vol = np.mean(vols) if vols else 0.0
        return live_vol / avg_vol if avg_vol > 0 else 0.0

    async def analyze(self, symbol: str, interval: str = "Min1", limit: int = 50) -> Optional[Dict]:
        """
        מביאה נרות, מחשבת z-score, ATR, Body/Range, %B ו-RVOL לנר החי.
        """
        closed, last_closed, live = await self.get_candles(symbol, interval, limit)
        if not closed:
            return None

        # חישובים קיימים
        z = self.calc_zscore(closed + [live])  # מחשבים על הנרות הסגורים + הנר החי
        atr = self.calc_atr(closed)

        # חישובים חדשים
        body_range = self.calc_body_range(live)
        bb_percent = self.calc_bollinger_percent(closed, live["close"])
        rvol = self.calc_rvol(closed, live["vol"])

        result = {
            "time": live["time"],
            "open": live["open"],
            "close": live["close"],
            "high": live["high"],
            "low": live["low"],
            "vol": live["vol"],
            "zscore": z,
            "atr": atr,
            "body_range": body_range,
            "bb_percent": bb_percent,
            "rvol": rvol,
            "last_closed": last_closed  # נשמור גם את הנר הסגור האחרון
        }
        return result


import asyncio
import logging
from services.mexc_api import MexcAPI
from helpers.CandleAnalyzer import CandleAnalyzer


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    # ⚠️ שים את ה-API KEY וה-SECRET שלך כאן או בקובץ .env
    api = MexcAPI("API_KEY", "SECRET_KEY")
    analyzer = CandleAnalyzer(api)

    await api.start_session()
    try:
        result = await analyzer.analyze("SOL_USDT", interval="Min1", limit=50)
        if result:
            print("📊 תוצאה מלאה:")
            print(f"Time: {result['time']}")
            print(f"Open: {result['open']}, High: {result['high']}, Low: {result['low']}, Close: {result['close']}")
            print(f"Volume: {result['vol']}")
            print(f"Zscore: {result['zscore']:.2f}")
            print(f"ATR: {result['atr']:.2f}")
            print(f"Body/Range: {result['body_range']:.2f}")
            print(f"%B (Bollinger): {result['bb_percent']:.2f}")
            print(f"RVOL: {result['rvol']:.2f}")
            print(f"Last Closed Candle: {result['last_closed']}")
        else:
            print("❌ לא התקבלה תוצאה")
    finally:
        await api.close_session()


if __name__ == "__main__":
    asyncio.run(main())
