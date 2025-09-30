import asyncio, logging, time, random
from typing import Optional, Callable, Awaitable
from helpers.CandleAnalyzer import CandleAnalyzer


class SpikeEngine:
    def __init__(self, symbol: str,
                threshold: Optional[float],   # 👈 threshold יכול להיות float או None
                interval: str,
                cooldown_seconds: int,
                alert_sink,
                mexc_api,
                ws,
                open_trades: dict,
                trade_cb: Optional[Callable[[str, float, float, float, float], Awaitable[None]]] = None,
                poll_seconds: float = 0.2):
        self.symbol = symbol.upper()
        self.interval = interval
        self.cooldown_seconds = int(cooldown_seconds)
        self.static_threshold = float(threshold) if threshold is not None else None  # 👈 ככה שומרים אותו
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
                analysis = await self.analyzer.analyze(self.symbol, self.interval, 70)
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
                    f"%B={bb_percent:.2f} | rvol={rvol:.2f} | threshold={self.static_threshold}"
                )


                # 🚀 קביעת threshold (רק סטטי מתוך config.yaml)
                threshold_to_use = self.static_threshold

                # 🧠 סינון נוסף לפי המדדים החדשים
                strong_body = body_range >= 0.40
                at_band_edge = (bb_percent >= 0.80 or bb_percent <= 0.20)
                high_rvol = rvol >= 3

                # ✅ כיוון לפי %B
                if bb_percent >= 0.80:   # קרוב ל־1 → למעלה
                    suggested_side = 1   # LONG
                elif bb_percent <= 0.20: # קרוב ל־0 → למטה
                    suggested_side = 3   # SHORT
                else:
                    suggested_side = 0   # אין כיוון ברור

                # בדיקה אם כל התנאים מתקיימים
                conditions_met = (
                    threshold_to_use is not None and
                    diff >= threshold_to_use and
                    strong_body and
                    at_band_edge and
                    high_rvol and
                    suggested_side != 0 and
                    abs(zscore) >= 3
                )



                if conditions_met and time.time() >= self._next_allowed_ts:
                    # ⏱️ שימוש בלוגיקה החדשה מ־mexc_ws
                    timing = self.ws.get_candle_timing(self.symbol, interval_sec=60)
                    if not timing:
                        await asyncio.sleep(self.poll_seconds)
                        continue

                    seconds_left = timing["left"]
                    if seconds_left <= 15:
                        logging.debug(f"⏱️ {self.symbol} פחות מ-15 שניות לנר → דילוג")
                        await asyncio.sleep(self.poll_seconds)
                        continue

                    self._next_allowed_ts = time.time() + self.cooldown_seconds

                    # פתיחת עסקה בפועל – ישיר (await)
                    if self.trade_cb:
                        logging.info(f"🚀 פותח עסקה מיידית עבור {self.symbol} | diff={diff:.2f}")
                        try:
                            await self.trade_cb(
                                self.symbol, diff, last_price, close_price,
                                analysis["last_closed"]["close"], suggested_side
                            )
                        except Exception as e:
                            logging.error(
                                f"❌ שגיאה בפתיחת עסקה עבור {self.symbol}: {e}",
                                exc_info=True
                            )

                    thr_str = f"{threshold_to_use:.4f}" if threshold_to_use is not None else "N/A"

                    msg = (
                        f"⚡ Spike Detected!\n"
                        f"Symbol: {self.symbol}\n"
                        f"Diff={diff:.2f}\n"
                        f"Zscore={zscore:.2f}\n"
                        f"ATR={atr:.2f}\n"
                        f"Threshold={thr_str}\n"
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
