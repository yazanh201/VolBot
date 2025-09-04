import asyncio, logging, time, random, numpy as np
from typing import Optional, Callable, Awaitable
from helpers.CandleAnalyzer import CandleAnalyzer


class SpikeEngine:
    def __init__(self, symbol: str, threshold: float, interval: str,
                 cooldown_seconds: int,
                 alert_sink,
                 mexc_api,
                 ws,   # 👈 WebSocket לקבלת מחיר חי
                 trade_cb: Optional[Callable[[str, float, float, float], Awaitable[None]]] = None,
                 poll_seconds: float = 1.0):
        self.symbol = symbol.upper()
        self.threshold = float(threshold)
        self.interval = interval
        self.cooldown_seconds = int(cooldown_seconds)
        self.alert_sink = alert_sink
        self.mexc_api = mexc_api
        self.ws = ws
        self.trade_cb = trade_cb
        self._next_allowed_ts: float = 0.0
        self.poll_seconds = float(poll_seconds)

        # ✅ CandleAnalyzer – לחישובי zscore ו־ATR
        self.analyzer = CandleAnalyzer(self.mexc_api)

    async def run(self):
        while True:
            try:
                # 📥 שליפת 30 נרות (כולל הנר החי)
                candles = await self.mexc_api.get_recent_candles(self.symbol, self.interval, 60)
                if not candles or len(candles) < 2:
                    logging.warning("⚠️ אין מספיק נרות זמינים עבור %s", self.symbol)
                    await asyncio.sleep(self.poll_seconds)
                    continue

                last_closed = candles[-2]   # הנר האחרון שנסגר
                live_candle = candles[-1]   # הנר החי (עדיין פתוח)
                last_price = self.ws.get_price(self.symbol)  # מחיר מה־WebSocket

                if not last_price:
                    logging.warning("⚠️ אין מחיר חי מ-WebSocket עבור %s", self.symbol)
                    await asyncio.sleep(self.poll_seconds)
                    continue

                # 🧮 חישובים
                close_price = last_closed["close"]
                diff = abs(last_price - close_price)
                zscore = self.analyzer.calc_zscore(candles)
                atr = self.analyzer.calc_atr(candles)
                live_vol = live_candle["vol"]
                avg_vol = np.mean([c["vol"] for c in candles[:-1]])  # ממוצע נפחים היסטורי

                logging.info(
                    f"📊 {self.symbol} | diff={diff:.2f} | live_vol={live_vol:.0f} | "
                    f"avg_vol={avg_vol:.0f} | zscore={zscore:.2f} | atr={atr:.2f}"
                )

                # 📌 שלושת התנאים חייבים להתקיים
                conditions_met = (
                    diff >= self.threshold and   # תנאי מחיר
                    zscore > -2 and            # תנאי Z-Score
                    diff > atr*0                 # תנאי ATR
                )

                if conditions_met and time.time() >= self._next_allowed_ts:
                    self._next_allowed_ts = time.time() + self.cooldown_seconds

                    msg = (
                        f"⚡ Spike Detected!\n"
                        f"Symbol: {self.symbol}\n"
                        f"Last closed: {close_price:.2f}\n"
                        f"Live close: {live_candle['close']:.2f}\n"
                        f"WS Price: {last_price:.2f}\n"
                        f"Diff={diff:.2f}, Zscore={zscore:.2f}, ATR={atr:.2f}, "
                        f"Volume={live_vol:.0f}, AvgVol={avg_vol:.0f}"
                    )
                    await self.alert_sink.notify(msg)
                    logging.info(msg)

                    # 📈 פתיחת עסקה (אם יש callback)
                    if self.trade_cb:
                        try:
                            await self.trade_cb(self.symbol, diff, last_price, close_price)
                        except Exception as e:
                            logging.exception("❌ שגיאה ב-trade_cb עבור %s: %s", self.symbol, e)

            except Exception as e:
                logging.error("⚠️ שגיאה ב-SpikeEngine עבור %s: %s", self.symbol, e, exc_info=True)

            # ⏱️ בדיקה כל שנייה (עם רנדום קטן למניעת עומס)
            await asyncio.sleep(self.poll_seconds + random.uniform(0.0, 0.1))


# if __name__ == "__main__":
#     import os
#     from dotenv import load_dotenv
#     from utils.alert_sink import AlertSink
#     from services.mexc_api import MexcAPI
#     from services.mexc_ws import MexcWebSocket

#     logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)s | %(message)s")

#     load_dotenv()
#     mexc_api = MexcAPI(os.getenv("MEXC_API_KEY_WEB2", ""), os.getenv("MEXC_API_SECRET_WEB", ""))
#     alert_sink = AlertSink(tg_enabled=False, bot_token="", chat_ids=[])

#     # WebSocket – נריץ ברקע
#     ws = MexcWebSocket(["BTC_USDT", "SOL_USDT"])
    
#     async def main():
#         asyncio.create_task(ws.run())
#         engine = SpikeEngine(
#             symbol="BTC_USDT",
#             threshold=100,        # 📌 סטייה נדרשת מהסגירה
#             interval="Min1",
#             cooldown_seconds=30,  # 📌 זמן המתנה בין התרעות
#             alert_sink=alert_sink,
#             mexc_api=mexc_api,
#             ws=ws
#         )
#         await engine.run()

#     asyncio.run(main())
