import asyncio, logging, time, random
from typing import Optional, Callable, Awaitable
from helpers.CandleAnalyzer import CandleAnalyzer
from helpers.MarketAnalyzer import MarketAnalyzer  # 👈 נוסיף גם את זה


class SpikeEngine:
    # --- thresholds פר סימבול ---
    SYMBOL_THRESHOLDS = {
        "BTC_USDT": {"spread": 0.3, "imbalance": 0.05, "deviation": 0.00015},
        "SOL_USDT": {"spread": 0.5, "imbalance": 0.08, "deviation": 0.0002},
    }

    def __init__(self, symbol: str, interval: str,
                 cooldown_seconds: int,
                 alert_sink,
                 mexc_api,
                 ws,
                 cache,   # 👈 נוסיף כאן
                 open_trades: dict,
                 trade_cb: Optional[Callable[[str, float, float, float, float], Awaitable[None]]] = None,
                 poll_seconds: float = 0.5):

        self.symbol = symbol.upper()
        self.interval = interval
        self.cooldown_seconds = int(cooldown_seconds)
        self.alert_sink = alert_sink
        self.mexc_api = mexc_api
        self.ws = ws
        self.cache = cache    # 👈 לשמור
        self.open_trades = open_trades
        self.trade_cb = trade_cb
        self._next_allowed_ts: float = 0.0
        self.poll_seconds = float(poll_seconds)

        self.analyzer = CandleAnalyzer(self.mexc_api)
        self.market_analyzer = MarketAnalyzer(self.cache)
        
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
                avg_vol = None
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
                if zscore < 2.5:
                    dynamic_threshold = None   # לא פותחים עסקה
                elif 2.5 <= zscore < 6:
                    dynamic_threshold = atr * 1
                else:  # zscore >= 6
                    dynamic_threshold = atr * 0.5

                # 🧠 סינון נוסף לפי המדדים החדשים
                strong_body = body_range >= 0.40
                at_band_edge = (bb_percent >= 0.80 or bb_percent <= 0.20)
                high_rvol = rvol >= 2

                # ✅ כיוון לפי %B
                if bb_percent >= 0.80:
                    suggested_side = 1   # LONG
                elif bb_percent <= 0.20:
                    suggested_side = 3   # SHORT
                else:
                    suggested_side = 0   # אין כיוון ברור

                # --- 🔥 MarketAnalyzer integration ---
                extra_conditions = True
                market_analysis = await self.market_analyzer.analyze_market(self.symbol)
                if market_analysis:
                    imbalance = market_analysis["imbalance"]
                    deviation = market_analysis["deviation"]
                    spread = market_analysis["spread"]

                    # thresholds מותאמים לפי הסימבול
                    t = self.SYMBOL_THRESHOLDS.get(
                        self.symbol,
                        {"spread": 0.5, "imbalance": 0.05, "deviation": 0.0002}
                    )

                    extra_conditions = (
                        spread is not None and spread < t["spread"] and
                        abs(imbalance) > t["imbalance"] and
                        abs(deviation) > t["deviation"]
                    )

                    logging.info(
                        f"📊 MarketAnalysis {self.symbol} | "
                        f"spread={spread:.4f} (thr={t['spread']}) | "
                        f"imbalance={imbalance:.4f} (thr={t['imbalance']}) | "
                        f"deviation={deviation:.6f} (thr={t['deviation']})"
                    )
                # -----------------------------------

                # בדיקה אם כל התנאים מתקיימים
                conditions_met = (
                    dynamic_threshold is not None and
                    diff >= dynamic_threshold and
                    strong_body and
                    at_band_edge and
                    high_rvol and
                    suggested_side != 0 and
                    extra_conditions
                )

                if conditions_met and time.time() >= self._next_allowed_ts:
                    # ⏱️ שימוש בלוגיקה החדשה מ־mexc_ws
                    timing = self.ws.get_candle_timing(self.symbol, interval_sec=60)
                    if not timing:
                        await asyncio.sleep(self.poll_seconds)
                        continue

                    seconds_left = timing["left"]
                    if seconds_left <= 11:
                        logging.debug(f"⏱️ {self.symbol} פחות מ-11 שניות לנר → דילוג")
                        await asyncio.sleep(self.poll_seconds)
                        continue

                    self._next_allowed_ts = time.time() + self.cooldown_seconds

                    # פתיחת עסקה בפועל
                    if self.trade_cb:
                        asyncio.create_task(
                            self.trade_cb(self.symbol, diff, last_price, close_price,
                                          analysis["last_closed"]["close"], suggested_side)
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
                        f"RVOL={rvol:.2f}\n"
                        f"Spread={spread:.4f}\n"
                        f"Imbalance={imbalance:.4f}\n"
                        f"Deviation={deviation:.6f}"
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
