import asyncio, logging, time, random
from typing import Optional, Callable, Awaitable
from helpers.CandleAnalyzer import CandleAnalyzer

# =========================
# ⚙️ פרמטרים פר־סימבול (כוונון)
# =========================
SYMBOL_PARAMS = {
    "BTC_USDT": {
        "atr_floor": 100.0,   # אם ATR קטן מכאן → נקשיח משמעותית
        "abs_diff_floor": 60,  # מינימום דולר לשינוי אמיתי שלא נתרגש מרעש
        "min_z": 3.0
    },
    "SOL_USDT": {
        "atr_floor": 0.65,
        "abs_diff_floor": 0.35,
        "min_z": 3.0
    }
}
DEFAULT_PARAMS = {"atr_floor": 1e9, "abs_diff_floor": 0.0, "min_z": 3.0}

def _get_sym_params(sym: str):
    return SYMBOL_PARAMS.get(sym, DEFAULT_PARAMS)

def _compute_dynamic_threshold(symbol: str, atr: float, zscore: float):
    """
    מחזיר סף דינמי מוקשח:
    - אם ATR נמוך → מכפילים גדולים יותר (לחתוך רעש).
    - אם ATR גבוה → טיפה מרפים (לא לפספס תנועה אמיתית).
      Z ב-[3..6): ATR*2 (נמוך) / ATR*1.25 (גבוה)
      Z ≥ 6:      ATR*1.25 (נמוך) / ATR*0.75 (גבוה)
    """
    p = _get_sym_params(symbol)
    if atr is None or atr <= 0:
        return None
    if zscore < p["min_z"]:
        return None

    low_atr = atr < p["atr_floor"]

    if 3.0 <= zscore < 6.0:
        return atr * (2.5 if low_atr else 1.5)
    else:  # zscore >= 6.0
        return atr * (1.5 if low_atr else 1.0)


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
                diff_raw = last_price - close_price
                diff_abs = abs(diff_raw)

                # שליפת מדדים שחושבו מראש
                zscore = analysis["zscore"]
                atr = analysis["atr"]
                live_vol = analysis["vol"]
                body_range = analysis["body_range"]
                bb_percent = analysis["bb_percent"]
                rvol = analysis["rvol"]

                logging.info(
                    f"📊 {self.symbol} | diff={diff_abs:.2f} | vol={live_vol:.0f} | "
                    f"zscore={zscore:.2f} | atr={atr:.2f} | body/range={body_range:.2f} | "
                    f"%B={bb_percent:.2f} | rvol={rvol:.2f}"
                )

                # ==============================
                # 🚀 סף דינמי מוקשח לפי Z ו-ATR
                # ==============================
                dynamic_threshold = _compute_dynamic_threshold(self.symbol, atr, zscore)

                # חיתוך רעשים קטנים: אם diff האבסולוטי קטן מרף המינימום—אל תתריע
                abs_floor = _get_sym_params(self.symbol)["abs_diff_floor"]
                if diff_abs < abs_floor:
                    dynamic_threshold = None
                    logging.debug(f"🧹 {self.symbol} diff_abs<{abs_floor} → ביטול טריגר קטן")

                # ==================================
                # 🧠 סינון נוסף – הקשחת תנאי איכות
                # ==================================
                strong_body = body_range >= 0.50               # היה 0.40
                at_band_edge = (bb_percent >= 0.90 or          # היה 0.80/0.20
                                bb_percent <= 0.10)
                high_rvol = rvol >= 3.0                        # היה 2

                # ✅ כיוון לפי %B (קשיח יותר)
                if bb_percent >= 0.90:
                    suggested_side = 1   # LONG
                elif bb_percent <= 0.10:
                    suggested_side = 3   # SHORT
                else:
                    suggested_side = 0   # אין כיוון ברור

                # השוואה בכיוון נכון (לונג → עלייה, שורט → ירידה)
                if suggested_side == 1:      # LONG
                    signed_diff = diff_raw      # מצופה חיובי
                elif suggested_side == 3:     # SHORT
                    signed_diff = -diff_raw    # מצופה חיובי אחרי היפוך סימן
                else:
                    signed_diff = 0

                # ==============================
                # ✅ בדיקת תנאים קשיחה
                # ==============================
                min_z = _get_sym_params(self.symbol)["min_z"]
                conditions_met = (
                    dynamic_threshold is not None and
                    signed_diff >= dynamic_threshold and   # נדרשת תנועה בכיוון
                    zscore >= min_z and
                    strong_body and
                    at_band_edge and
                    high_rvol and
                    suggested_side != 0
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
                            self.trade_cb(self.symbol, diff_abs, last_price, close_price,
                                          analysis["last_closed"]["close"], suggested_side)
                        )

                    dyn_str = f"{dynamic_threshold:.4f}" if dynamic_threshold else "N/A"

                    msg = (
                        f"⚡ Spike Detected!\n"
                        f"Symbol: {self.symbol}\n"
                        f"Diff={diff_abs:.2f}\n"
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
#
#     logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)s | %(message)s")
#
#     load_dotenv()
#     mexc_api = MexcAPI(os.getenv("MEXC_API_KEY_WEB2", ""), os.getenv("MEXC_API_SECRET_WEB", ""))
#     alert_sink = AlertSink(tg_enabled=False, bot_token="", chat_ids=[])
#
#     # WebSocket – נריץ ברקע
#     ws = MexcWebSocket(["BTC_USDT", "SOL_USDT"])
#     
#     async def main():
#         asyncio.create_task(ws.run())
#         engine = SpikeEngine(
#             symbol="BTC_USDT",
#             interval="Min1",
#             cooldown_seconds=30,
#             alert_sink=alert_sink,
#             mexc_api=mexc_api,
#             ws=ws,
#             open_trades={}
#         )
#         await engine.run()
#
#     asyncio.run(main())
