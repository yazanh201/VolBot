import asyncio, logging, time, random, numpy as np
from typing import Optional, Callable, Awaitable
from helpers.CandleAnalyzer import CandleAnalyzer


class SpikeEngine:
    def __init__(self, symbol: str, interval: str,
                 cooldown_seconds: int,
                 alert_sink,
                 mexc_api,
                 ws,
                 open_trades: dict,   # 👈 נוסיף את ה־dict כאן
                 trade_cb: Optional[Callable[[str, float, float, float, float], Awaitable[None]]] = None,
                 poll_seconds: float = 0.5):
        self.symbol = symbol.upper()
        self.interval = interval
        self.cooldown_seconds = int(cooldown_seconds)
        self.alert_sink = alert_sink
        self.mexc_api = mexc_api
        self.ws = ws
        self.open_trades = open_trades   # 👈 שמירה לשימוש פנימי
        self.trade_cb = trade_cb
        self._next_allowed_ts: float = 0.0
        self.poll_seconds = float(poll_seconds)

        self.analyzer = CandleAnalyzer(self.mexc_api)

    def _seconds_left_in_candle(self, candle_ts: int) -> int:
        now = int(time.time())
        bar_opened = candle_ts // 1000
        elapsed = now - bar_opened
        return 60 - (elapsed % 60)

    async def run(self):
        while True:
            try:
                # 👇 בדיקה אם יש עסקה פתוחה → לא נבדוק את הסימבול
                if self.symbol in self.open_trades:
                    logging.debug(f"⏸️ {self.symbol} יש עסקה פתוחה → דילוג על בדיקות")
                    await asyncio.sleep(1)
                    continue

                candles = await self.mexc_api.get_recent_candles(self.symbol, self.interval, 30)
                if not candles or len(candles) < 2:
                    await asyncio.sleep(self.poll_seconds)
                    continue

                last_closed = candles[-2]
                live_candle = candles[-1]
                last_price = self.ws.get_price(self.symbol)

                if not last_price:
                    await asyncio.sleep(self.poll_seconds)
                    continue

                close_price = last_closed["close"]
                diff = abs(last_price - close_price)
                zscore = self.analyzer.calc_zscore(candles)
                atr = self.analyzer.calc_atr(candles)
                live_vol = live_candle["vol"]
                avg_vol = np.mean([c["vol"] for c in candles[:-1]])

                logging.info(
                    f"📊 {self.symbol} | diff={diff:.2f} | vol={live_vol:.0f} | "
                    f"avgVol={avg_vol:.0f} | zscore={zscore:.2f} | atr={atr:.2f}"
                )

                # 🚀 קביעת threshold דינמי לפי zscore
                dynamic_threshold = None
                if zscore < 2.5:
                    dynamic_threshold = None   # 👈 לא נפתח עסקה בכלל
                elif 2.5 <= zscore < 6:
                    dynamic_threshold = atr * 1.5
                else:  # zscore >= 6
                    dynamic_threshold = atr * 1.0

                # בדיקה אם התנאים מתקיימים
                conditions_met = (
                    dynamic_threshold is not None and
                    diff >= dynamic_threshold
                )


                if conditions_met and time.time() >= self._next_allowed_ts:
                    seconds_left = self._seconds_left_in_candle(live_candle["time"])
                    if seconds_left <= 10:
                        await asyncio.sleep(self.poll_seconds)
                        continue

                    self._next_allowed_ts = time.time() + self.cooldown_seconds

                    if self.trade_cb:
                        asyncio.create_task(
                            self.trade_cb(self.symbol, diff, last_price, close_price, last_closed["close"])
                        )

                    msg = (
                        f"⚡ Spike Detected!\nSymbol: {self.symbol}\n"
                        f"Diff={diff:.2f}, Zscore={zscore:.2f}, ATR={atr:.2f}"
                    )
                    if self.alert_sink:
                        asyncio.create_task(self.alert_sink.notify(msg))
                    logging.info(msg)

            except Exception as e:
                logging.error("⚠️ שגיאה ב-SpikeEngine עבור %s: %s", self.symbol, e, exc_info=True)

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
