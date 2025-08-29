import asyncio, yaml, logging, os, json
from dotenv import load_dotenv

from utils.alert_sink import AlertSink
from core.SpikeEngine import SpikeEngine
from services.mexc_api import MexcAPI        # בדיקת פוזיציות / PnL
from services.mexc_order import place_order  # שליחת הזמנות (פתיחה/סגירה) בפועל
from services.rate_limiter import RateLimiter  # לעתיד (כרגע לא בשימוש כאן)
from services.mexc_ws import MexcWebSocket   # WebSocket לשליפת מחיר נוכחי
import time
from collections import defaultdict

ws_client = None   # לקוח WS גלובלי לשליפת מחירים בזמן אמת



# מונה עסקאות לפי סימבול
trade_counters = defaultdict(list)

def can_open_new_trade(symbol: str, max_trades_per_hour: int = 2) -> bool:
    """
    מחזיר True אם מותר לפתוח עסקה חדשה עבור symbol, אחרת False.
    שומר עד מקסימום X עסקאות בשעה.
    """
    now = time.time()
    one_hour_ago = now - 3600

    # ננקה עסקאות ישנות מהרשימה
    trade_counters[symbol] = [ts for ts in trade_counters[symbol] if ts > one_hour_ago]

    if len(trade_counters[symbol]) >= max_trades_per_hour:
        return False  # כבר פתחנו מקסימום עסקאות בשעה

    # אחרת נעדכן שהולכים לפתוח עכשיו
    trade_counters[symbol].append(now)
    return True

# ---------- Pretty print helper ----------
def _pp_open_trades(open_trades: dict) -> str:
    try:
        return json.dumps(open_trades, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as e:
        return f"<cannot json-dump open_trades: {e}>"
    

def _calc_tp_price(entry: float, leverage: float, tp_pct: float, side_open: int) -> float:
    """
    מחשב מחיר TP מהיר לפי יעד PnL באחוזים.
    side_open: 1=Open Long, 3=Open Short
    tp_pct: למשל 20 -> 20% PnL
    """
    if leverage <= 0:
        leverage = 1.0
    step = (tp_pct / 100.0) / leverage  # כמה יחסית ל-entry
    if side_open == 1:   # Long
        return entry * (1.0 + step)
    elif side_open == 3: # Short
        return entry * (1.0 - step)
    else:
        raise ValueError(f"unknown side_open={side_open}")


# ---------- ENV & Logging ----------
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8", mode="w"),
    ],
)

# ---------- API keys for read-only MexcAPI ----------
mexc_api_key = os.environ.get("MEXC_API_KEY_WEB2", "").strip()
mexc_secret  = os.environ.get("MEXC_API_SECRET_WEB", "").strip()
if not mexc_api_key or not mexc_secret:
    logging.error("❌ אחד או יותר מהמפתחות חסר! בדוק את קובץ .env")
    raise SystemExit(1)

mexc_api = MexcAPI(mexc_api_key, mexc_secret)

# ---------- State ----------
# נשמור את ה-payload שנשלח בפתיחה לכל סימבול (עם side=1/3 וכו')
open_trades: dict[str, dict] = {}   # { "BTC_USDT": {payload}, "SOL_USDT": {payload}, ... }


# ---------- Open order ----------
async def open_mexc_order(
    symbol: str,
    price_range: float,
    last_price: float,
    first_price: float,
    trade_cfg: dict,
    risk_cfg: dict
):
    """
    פותח עסקה על בסיס Spike:
    - מונע כפילות (dict)
    - מחשב Vol לפי אחוז מהיתרה ולברג'
    - חוסם שליחה אם vol==0
    - מחשב ושומר tp_price (מחיר יעד ל-TP) לפי entry+leverage+TP%
    - שומר את ה-payload שנשלח ב-open_trades רק לאחר הצלחה
    - מגביל מקסימום עסקאות לשעה (ברירת מחדל 2 לסימבול)
    """
    norm_symbol = mexc_api.normalize_symbol(symbol)

    # 🟢 שלב 0: מגבלת עסקאות לשעה
    if not can_open_new_trade(norm_symbol, max_trades_per_hour=2):
        logging.info("⏳ דילוג → כבר נפתחו 2 עסקאות בשעה האחרונה עבור %s", norm_symbol)
        return {"skipped": "trade_limit_reached", "symbol": norm_symbol}

    # 🟢 שלב 1: בדיקה מקומית (מניעת פתיחה כפולה)
    if norm_symbol in open_trades:
        logging.info("⛔ דילוג (dict) → כבר קיימת עסקה על %s", norm_symbol)
        return {"skipped": "local_open_trade_exists", "symbol": norm_symbol}

    # סימון "pending" כדי למנוע פתיחה כפולה בזמן אמת
    open_trades[norm_symbol] = {"pending": True}

    # 🟢 שלב 2: קביעת כיוון העסקה
    if last_price == first_price:
        logging.info("⏸️ דילוג → last_price == first_price (%s), לא פותח עסקה עבור %s",
                     last_price, norm_symbol)
        open_trades.pop(norm_symbol, None)  # ביטול pending
        return {"skipped": "neutral_direction", "symbol": norm_symbol}

    side = 1 if last_price > first_price else 3
    logging.info("🧭 כיוון פתיחה עבור %s → side=%s (last=%.6f, close=%.6f)",
                 norm_symbol, side, last_price, first_price)

    # 🟢 שלב 3: חישוב Vol
    percent = risk_cfg.get("percentPerTrade", trade_cfg.get("percentPerTrade", 5))
    leverage = trade_cfg.get("leverage", 20)
    logging.info("📊 חישוב Vol → symbol=%s | percent=%s | leverage=%s", norm_symbol, percent, leverage)

    try:
        vol = await mexc_api.calc_order_volume(norm_symbol, percent=percent, leverage=leverage)
        logging.info("📊 Vol שחושב עבור %s: %s חוזים", norm_symbol, vol)

        if vol <= 0:
            logging.warning("⛔ Vol==0 עבור %s → לא שולח הזמנה", norm_symbol)
            open_trades.pop(norm_symbol, None)  # ביטול pending
            return {"skipped": "zero_volume", "symbol": norm_symbol,
                    "percent": percent, "leverage": leverage}
    except Exception as e:
        logging.error("❌ שגיאה בחישוב vol עבור %s: %s", norm_symbol, e, exc_info=True)
        open_trades.pop(norm_symbol, None)  # ביטול pending
        return {"error": "calc_volume_failed", "symbol": norm_symbol, "exception": str(e)}

    # 🟢 שלב 4: בניית payload ושליחה
    obj = {
        "symbol": norm_symbol,
        "side": side,
        "openType": trade_cfg.get("openType", 1),
        "type": trade_cfg.get("type", "5"),   # Market
        "vol": vol,
        "leverage": leverage,
        "priceProtect": trade_cfg.get("priceProtect", "0"),
    }

    logging.info("📤 שולח עסקה על %s (side=%s, vol=%s)", norm_symbol, side, vol)
    logging.debug("Payload → %s", obj)

    try:
        resp = await asyncio.to_thread(place_order, obj)
    except Exception as e:
        logging.error("❌ כשל בשליחת ההזמנה עבור %s: %s", norm_symbol, e, exc_info=True)
        open_trades.pop(norm_symbol, None)  # ביטול pending
        return {"error": "place_order_exception", "symbol": norm_symbol, "exception": str(e)}

    logging.info("📩 תגובת MEXC עבור %s: %s", norm_symbol, resp)

    # 🟢 שלב 5: ולידציית הצלחה ושמירת מצב
    success = bool(resp.get("success", False))
    code = resp.get("code")

    if success or code in (0, 200, "200"):
        try:
            entry = None
            lev = leverage

            # ✅ במקביל: API + WS
            pos, ws_entry = await asyncio.gather(
                mexc_api.get_open_positions(norm_symbol),
                asyncio.to_thread(lambda: ws_client.get_price(norm_symbol) if ws_client else None)
            )

            if pos and pos.get("success") and pos.get("data"):
                p = pos["data"][0]
                entry_val = p.get("holdAvgPrice")
                if entry_val is not None:
                    entry = float(entry_val)
                lev_val = p.get("leverage")
                if lev_val is not None:
                    lev = float(lev_val)

            if entry is None:
                entry = ws_entry

            tp_pct = risk_cfg.get("takeProfitPct")
            tp_price = None
            if tp_pct is not None and entry is not None:
                tp_price = _calc_tp_price(entry=entry,
                                          leverage=lev if lev and lev > 0 else 1.0,
                                          tp_pct=float(tp_pct),
                                          side_open=side)

            obj_to_store = {**obj, "entry": entry, "lev": lev,
                            "tp_pct": tp_pct, "tp_price": tp_price}
            open_trades[norm_symbol] = obj_to_store  # ✅ עדכון מלא במקום pending
            logging.info("🗂️ open_trades עודכן (אחרי פתיחה):\n%s",
                         _pp_open_trades(open_trades))
        except Exception as e:
            open_trades[norm_symbol] = obj
            logging.warning("⚠️ לא הצלחתי להביא entry/lev/TP עבור %s: %s", norm_symbol, e)

        return {"ok": True, "symbol": norm_symbol, "response": resp}

    # כישלון → ביטול ה־pending
    open_trades.pop(norm_symbol, None)
    logging.warning("⚠️ פתיחה נכשלה עבור %s: %s", norm_symbol, resp)
    return {"ok": False, "symbol": norm_symbol, "response": resp}

# ---------- TP/SL close ----------

async def check_and_close_if_needed(trade_obj: dict, pnl: float, risk_cfg: dict):
    """
    בודקת TP קבוע מתוך config ו-SL דינמי לפי מחיר הסגירה של הנר האחרון עם tolerance.
    """
    symbol = trade_obj["symbol"]
    tp = risk_cfg.get("takeProfitPct")          # TP באחוזים
    tol = risk_cfg.get("slTolerancePct", 0.0)   # טולרנס ל-SL דינמי (ברירת מחדל 0)

    logging.info("📊 %s: PnL=%.2f%% | TP=%s | tol=%.4f (SL דינמי)", symbol, pnl, tp, tol)

    # מיפוי נכון של side
    if trade_obj["side"] == 1:       # Open Long
        close_side = 4              # Close Long
    elif trade_obj["side"] == 3:     # Open Short
        close_side = 2              # Close Short
    else:
        logging.warning("⚠️ side לא מוכר לפתיחה (%s) עבור %s – דילוג", trade_obj["side"], symbol)
        return None

    # הכנה לפקודת סגירה
    close_obj = {**trade_obj, "side": close_side, "reduceOnly": True}

    # --- בדיקת TP ---
    if tp is not None and pnl >= tp:
        logging.info("🎯 TP הושג על %s (PnL=%.2f%%) → סוגר עסקה", symbol, pnl)
    else:
        # --- בדיקת SL דינמי ---
        try:
            candle = await mexc_api.get_last_closed_candle(symbol, interval="Min1")

            # מחיר נוכחי דרך WS תחילה, אח"כ API כ-fallback
            global ws_client
            last_price = ws_client.get_price(symbol) if ws_client else None
            # if last_price is None:
            #     last_price = await mexc_api.get_current_price(symbol)

            if not candle or not last_price:
                logging.warning("⚠️ לא הצלחנו להביא candle/last_price עבור %s", symbol)
                return None

            close_price = candle["close"]

            # בדיקה עם טולרנס
            if trade_obj["side"] == 1:  # Long
                trigger_price = close_price * (1 - tol)
                if last_price <= trigger_price:
                    logging.info("🛑 SL דינמי הופעל על %s (Long): last=%.2f <= trigger=%.2f (close=%.2f)", 
                                 symbol, last_price, trigger_price, close_price)
                else:
                    logging.info("⏳ עדיין לא הגיע ל-SL (Long) → last=%.2f > trigger=%.2f", last_price, trigger_price)
                    return None

            elif trade_obj["side"] == 3:  # Short
                trigger_price = close_price * (1 + tol)
                if last_price >= trigger_price:
                    logging.info("🛑 SL דינמי הופעל על %s (Short): last=%.2f >= trigger=%.2f (close=%.2f)", 
                                 symbol, last_price, trigger_price, close_price)
                else:
                    logging.info("⏳ עדיין לא הגיע ל-SL (Short) → last=%.2f < trigger=%.2f", last_price, trigger_price)
                    return None

        except Exception as e:
            logging.error("⚠️ שגיאה בבדיקת SL דינמי עבור %s: %s", symbol, e, exc_info=True)
            return None

    # --- שליחת ההזמנה בפועל ---
    resp = await asyncio.to_thread(place_order, close_obj)
    logging.info("📩 תגובת MEXC לסגירה על %s: %s", symbol, resp)

    if resp.get("success") or resp.get("code") == 2009:  # גם "already closed"
        open_trades.pop(symbol, None)
        logging.info("🗂️ open_trades אחרי סגירה:\n%s", json.dumps(open_trades, ensure_ascii=False, indent=2))

    return resp


# ---------- Main orchestration ----------
async def run(config_path: str = "config.yaml"):
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # --- טלגרם ---
    tg_cfg = cfg.get("telegram", {}) or {}

    # נורמליזציה של chat_ids
    chat_ids_raw = tg_cfg.get("chat_id", [])
    if isinstance(chat_ids_raw, (str, int)):
        chat_ids = [str(chat_ids_raw)]
    elif isinstance(chat_ids_raw, list):
        chat_ids = [str(x) for x in chat_ids_raw]
    else:
        chat_ids = []

    alert_sink = AlertSink(
        tg_enabled=bool(tg_cfg.get("enabled")),
        bot_token=str(tg_cfg.get("bot_token", "")),
        chat_ids=chat_ids,
    )

    # אל תתן לשגיאת טלגרם להפיל את הריצה
    try:
        if alert_sink.tg_enabled and alert_sink.bot_token and chat_ids:
            await alert_sink.notify("✅ Fast Vol bot is up – Telegram OK!")
        else:
            logging.info("🔕 Telegram disabled or missing config — continuing without Telegram.")
    except Exception as e:
        logging.warning("⚠️ Telegram notify failed: %s — continuing.", e, exc_info=True)

    # --- הגדרות מסחר + סיכון ---
    mexc_trades = cfg.get("mexc_trades", {})
    risk_cfg    = cfg.get("risk", {"takeProfitPct": 50, "stopLossPct": -20})

    # callback לפתיחה – יוזן ל-motor
    async def trade_cb(symbol, price_range, last_price, first_price):
        norm_symbol = mexc_api.normalize_symbol(symbol)

        # 🟢 אם כבר יש עסקה ב־dict → אל תנסה לפתוח בכלל
        if norm_symbol in open_trades:
            logging.info("⛔ דילוג (trade_cb) → כבר קיימת עסקה על %s", norm_symbol)
            return

        # ⚙️ קונפיג לפי הסימבול המנורמל (עם fallback)
        trade_cfg = mexc_trades.get(norm_symbol) or mexc_trades.get(symbol)
        if not trade_cfg:
            logging.warning("⚠️ אין הגדרות מסחר עבור %s", norm_symbol)
            return

        await open_mexc_order(norm_symbol, price_range, last_price, first_price, trade_cfg, risk_cfg)

    # --- Spike Engine (תמיכה ב-threshold אישי לכל סימבול) ---
    spike_cfg      = cfg.get("spike", {})
    symbols_cfg    = spike_cfg.get("symbols", ["BTC_USDT"])
    thresholds_map = spike_cfg.get("thresholds", {})  # מיפוי thresholds מהקונפיג
    interval       = spike_cfg.get("interval", "Min1")
    cooldown       = int(spike_cfg.get("cooldown_seconds", 20))

    tasks = []

    # --- WebSocket מחירים חי ---
    global ws_client
    ws_client = MexcWebSocket([mexc_api.normalize_symbol(s) for s in symbols_cfg])
    tasks.append(asyncio.create_task(ws_client.run()))

    for sym in symbols_cfg:
        # נוודא שהסימבול בפורמט נכון, ונשתמש בו לכל האופרציות
        norm_sym = mexc_api.normalize_symbol(sym)

        # threshold ייחודי מהקובץ, אחרת ברירת מחדל כללית
        threshold_default = spike_cfg.get("threshold", 300)
        threshold = float(thresholds_map.get(norm_sym, threshold_default))

        logging.info(f"🔧 הגדרת threshold עבור {norm_sym}: {threshold}")

        # יצירת מנוע Spike עבור כל סימבול
        engine = SpikeEngine(
            norm_sym, threshold, interval, cooldown,
            alert_sink, mexc_api, ws_client, trade_cb=trade_cb
        )

        tasks.append(asyncio.create_task(engine.run()))

    # --- מוניטור TP/SL על העסקאות השמורות ---
    async def monitor_positions():
        """
        מנטרת את כל העסקאות ב-open_trades:
        - TP מהיר לפי מחיר (ticker) על בסיס tp_price ששמור ב-open_trades
        - SL דינמי כמו שהיה (דרך check_and_close_if_needed)
        - סנכרון אם נסגרה ידנית (מחיקה מהמילון)
        """
        FAST_SLEEP = 0.1  # נשאר ללא שינוי בלוגיקה (אפשר לקצר בהמשך אם תרצה)

        while True:
            # עותק כדי לא לקרוס אם dict משתנה תוך כדי איטרציה
            for sym_key, trade_obj in list(open_trades.items()):
                try:
                    # 🟢 בדיקת קיום פוזיציה אמיתית
                    positions_api = await mexc_api.get_open_positions(sym_key)
                    if (not positions_api
                        or not positions_api.get("success", False)
                        or not positions_api.get("data")):
                        # נסגרה ידנית / אין נתונים → ניקוי
                        open_trades.pop(sym_key, None)
                        logging.info("🧹 נמחק %s מ-open_trades (נסגר ידנית/אין נתונים מה-API)", sym_key)
                        continue

                    # ---- TP מהיר לפי מחיר ----
                    tp_pct    = trade_obj.get("tp_pct")
                    tp_price  = trade_obj.get("tp_price")
                    side_open = trade_obj.get("side")  # 1=Long, 3=Short

                    # מחיר נוכחי דרך WS תחילה, אח"כ API כ-fallback
                    global ws_client
                    last = ws_client.get_price(sym_key) if ws_client else None
                    # if last is None:
                    #     last = await mexc_api.get_current_price(sym_key)
                    # if last is None:
                    #     continue

                    if tp_pct is not None and tp_price is not None and side_open in (1, 3):
                        triggered_tp = (
                            (side_open == 1 and last >= tp_price) or
                            (side_open == 3 and last <= tp_price)
                        )
                        if triggered_tp:
                            logging.info(
                                "🎯 TP מחיר הופעל על %s: last=%.6f, tp=%.6f, side=%s",
                                sym_key, last, tp_price, side_open
                            )
                            try:
                                # שימוש בפונקציה הקיימת לסגירה
                                await check_and_close_if_needed(trade_obj, pnl=float(tp_pct), risk_cfg=risk_cfg)
                                open_trades.pop(sym_key, None)
                            except Exception as e:
                                logging.error("❌ שגיאה בסגירת TP מהירה עבור %s: %s", sym_key, e, exc_info=True)
                            continue  # לסימבול הבא

                    # ---- לא נסגר ב-TP: בדיקת SL דינמי/אחרים ----
                    pnl = await mexc_api.get_unrealized_pnl(sym_key)
                    if pnl is not None:
                        try:
                            await check_and_close_if_needed(trade_obj, pnl, risk_cfg)
                        except Exception as e:
                            logging.error("❌ שגיאה בסגירה לפי TP/SL עבור %s: %s", sym_key, e, exc_info=True)
                    else:
                        logging.debug("ℹ️ אין PnL עבור %s (ייתכן שאין פוזיציה בפועל/שגיאת נתונים)", sym_key)

                except Exception as e:
                    logging.error(
                        "⚠️ שגיאה בבדיקת TP/SL עבור %s: %s",
                        trade_obj.get("symbol", sym_key), e, exc_info=True
                    )

            await asyncio.sleep(FAST_SLEEP)  # קצב בדיקה

    tasks.append(asyncio.create_task(monitor_positions()))

    # --- הרצה/כיבוי מסודר ---
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.error("💥 תקלה כללית בלולאת הריצה: %s", e, exc_info=True)
    finally:
        logging.info("🛑 סיום ריצה — מבטל משימות וסוגר סשנים...")
        for t in tasks:
            try:
                t.cancel()
            except Exception:
                pass
        # חכה לביטול המשימות (לא קריטי אם כבר נסגרו)
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass
        # סגירה נקייה של WS + session
        try:
            if ws_client:
                ws_client.keep_running = False
        except Exception:
            pass
        try:
            await mexc_api.close_session()
        except Exception as e:
            logging.error("⚠️ שגיאה בסגירת session: %s", e, exc_info=True)


if __name__ == "__main__":
    try:
        asyncio.run(run("config.yaml"))
    except KeyboardInterrupt:
        print("Bye 👋")
