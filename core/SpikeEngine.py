import asyncio, logging, time, random
from typing import Optional, Callable, Awaitable
from helpers.CandleAnalyzer import CandleAnalyzer


class SpikeEngine:
    def __init__(self, symbol: str, interval: str,
                 cooldown_seconds: int,
                 alert_sink,
                 mexc_api,
                 ws,
                 open_trades: dict,
                 trade_cb: Optional[Callable[[str, float, float, float, float], Awaitable[None]]] = None,
                 poll_seconds: float = 0.5):
        self.symbol = symbol.upper()
        self.interval = interval
        self.cooldown_seconds = int(cooldown_seconds)
        self.alert_sink = alert_sink
        self.mexc_api = mexc_api
        self.ws = ws
        self.open_trades = open_trades
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
                # 👇 אם יש עסקה פתוחה – דילוג
                if self.symbol in self.open_trades:
                    logging.debug(f"⏸️ {self.symbol} יש עסקה פתוחה → דילוג")
                    await asyncio.sleep(1)
                    continue

                # 📊 נשתמש ב־CandleAnalyzer
                analysis = await self.analyzer.analyze(self.symbol, self.interval, 50)
                if not analysis:
                    await asyncio.sleep(self.poll_seconds)
                    continue

                last_price = self.ws.get_price(self.symbol)
                if not last_price:
                    await asyncio.sleep(self.poll_seconds)
                    continue

                close_price = analysis["last_closed"]["close"]
                diff = abs(last_price - close_price)

                # שליפת מדדים שחושבו מראש
                zscore = analysis["zscore"]
                atr = analysis["atr"]
                live_vol = analysis["vol"]
                avg_vol = None  # נחשב אם צריך, אבל אפשר גם להוסיף avg_vol ל-analyze
                body_range = analysis["body_range"]
                bb_percent = analysis["bb_percent"]
                rvol = analysis["rvol"]

                logging.info(
                    f"📊 {self.symbol} | diff={diff:.2f} | vol={live_vol:.0f} | "
                    f"zscore={zscore:.2f} | atr={atr:.2f} | body/range={body_range:.2f} | "
                    f"%B={bb_percent:.2f} | rvol={rvol:.2f}"
                )

                # 🚀 קביעת threshold דינמי לפי zscore
                dynamic_threshold = None
                if zscore < 2:
                    dynamic_threshold = None   # לא פותחים עסקה
                elif 2 <= zscore < 6:
                    dynamic_threshold = atr * 1
                else:  # zscore >= 6
                    dynamic_threshold = atr * 0.5

                # 🧠 סינון נוסף לפי המדדים החדשים
                strong_body = body_range >= 0.30         # נר עם גוף משמעותי
                at_band_edge = (bb_percent >= 0.80 or bb_percent <= 0.20)  # בקצה בולינג'ר
                high_rvol = rvol >= 1.5                  # נפח גבוה מהרגיל

                # בדיקה אם כל התנאים מתקיימים
                conditions_met = (
                    dynamic_threshold is not None and
                    diff >= dynamic_threshold and
                    strong_body and
                    at_band_edge and
                    high_rvol
                )



                if conditions_met and time.time() >= self._next_allowed_ts:
                    seconds_left = self._seconds_left_in_candle(analysis["time"])
                    if seconds_left <= 10:
                        await asyncio.sleep(self.poll_seconds)
                        continue

                    self._next_allowed_ts = time.time() + self.cooldown_seconds

                    # פתיחת עסקה בפועל
                    if self.trade_cb:
                        asyncio.create_task(
                            self.trade_cb(self.symbol, diff, last_price, close_price, analysis["last_closed"]["close"])
                        )

                    dyn_str = f"{dynamic_threshold:.4f}" if dynamic_threshold else "N/A"

                    msg = (
                        f"⚡ Spike Detected!\n"
                        f"Symbol: {self.symbol}\n"
                        f"Diff={diff:.2f}\n"
                        f"Zscore={zscore:.2f}\n"
                        f"ATR={atr:.2f}\n"
                        f"DynamicThreshold={dyn_str}\n"
                        f"LiveVol={live_vol:.0f}\n"
                        f"Body/Range={body_range:.2f}\n"
                        f"%B={bb_percent:.2f}\n"
                        f"RVOL={rvol:.2f}"
                    )

                    if self.alert_sink:
                        logging.info(f"📤 שולח הודעה לטלגרם עבור {self.symbol} ...")
                        try:
                            await self.alert_sink.notify(msg)
                            logging.info("✅ ההודעה נשלחה לטלגרם")
                        except Exception as e:
                            logging.error(f"❌ שגיאה בשליחת טלגרם: {e}")

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
