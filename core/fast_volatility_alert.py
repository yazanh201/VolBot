import asyncio, yaml, logging, os, json
from dotenv import load_dotenv

from utils.alert_sink import AlertSink
from utils.alert_sink import fmt_open_msg, fmt_close_msg
from core.SpikeEngine import SpikeEngine
from services.mexc_api import MexcAPI
from services.mexc_order import place_order
from services.rate_limiter import RateLimiter
from services.mexc_ws import MexcWebSocket
import time
from collections import defaultdict
from services.Tp_Sl_Change import MexcTPClient
from cashe.cache_manager import CacheManager


ws_client = None   # לקוח WS גלובלי לשליפת מחירים בזמן אמת



# מונה עסקאות לפי סימבול
trade_counters = defaultdict(list)

def can_open_new_trade(symbol: str, max_trades_per_hour: int = 1) -> bool:
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

def _calc_sl_price(entry: float, sl_tolerance: float, side_open: int) -> float:
    """
    מחשב מחיר Stop Loss לפי כניסה + טולרנס.
    side_open: 1=Open Long, 3=Open Short
    sl_tolerance: אחוז טולרנס, למשל 0.001 (0.1%)
    """
    if side_open == 1:   # Long
        return entry * (1.0 - sl_tolerance)
    elif side_open == 3: # Short
        return entry * (1.0 + sl_tolerance)
    else:
        raise ValueError(f"unknown side_open={side_open}")


# ---------- ENV & Logging ----------
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# logging.basicConfig(
#     level=logging.DEBUG,
#     format="%(asctime)s | %(levelname)s | %(message)s",
#     handlers=[
#         logging.StreamHandler(),
#         logging.FileHandler("bot.log", encoding="utf-8", mode="w"),
#     ],
# )


logging.basicConfig(
    level=logging.WARNING,   # כאן לשנות מ-DEBUG ל-WARNING
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
    risk_cfg: dict,
    cache,              # ✅ משתמשים ב-CacheManager
    alert_sink=None,
    last_closed_price: float = None   # 👈 חדש: מחיר הסגירה של הנר האחרון
):
    norm_symbol = mexc_api.normalize_symbol(symbol)

    # מגבלת עסקאות לשעה
    if not can_open_new_trade(norm_symbol, max_trades_per_hour=2):
        logging.info("⏳ דילוג → כבר נפתחו 2 עסקאות בשעה האחרונה עבור %s", norm_symbol)
        return {"skipped": "trade_limit_reached", "symbol": norm_symbol}

    # מניעת פתיחה כפולה
    if norm_symbol in open_trades:
        logging.info("⛔ דילוג (dict) → כבר קיימת עסקה על %s", norm_symbol)
        return {"skipped": "local_open_trade_exists", "symbol": norm_symbol}

    open_trades[norm_symbol] = {"pending": True}

    # כיוון העסקה
    if last_price == first_price:
        logging.info("⏸️ דילוג → last_price == first_price (%s)", last_price)
        open_trades.pop(norm_symbol, None)
        return {"skipped": "neutral_direction", "symbol": norm_symbol}

    side = 1 if last_price > first_price else 3

    # --- ✅ שימוש ב-CacheManager ---
    specs = await cache.get_contract_specs(norm_symbol)
    balance = await cache.get_balance()
    anchor_price = cache.get_last_price(norm_symbol) or last_price

    if not specs or not balance or not anchor_price:
        open_trades.pop(norm_symbol, None)
        return {"error": "cache_data_missing", "symbol": norm_symbol}

    percent = risk_cfg.get("percentPerTrade", trade_cfg.get("percentPerTrade", 5))
    leverage = trade_cfg.get("leverage", 20)

    try:
        contract_size = float(specs["contractSize"])
        min_vol = int(specs["minVol"])
        margin_amount = balance * (percent / 100.0)
        raw_contracts_value = margin_amount * leverage
        raw_vol = raw_contracts_value / (anchor_price * contract_size)
        vol = max(int(raw_vol), min_vol)

        if vol <= 0:
            open_trades.pop(norm_symbol, None)
            return {"skipped": "zero_volume", "symbol": norm_symbol}
    except Exception as e:
        open_trades.pop(norm_symbol, None)
        return {"error": "calc_volume_failed", "symbol": norm_symbol, "exception": str(e)}

    # Payload
    obj = {
        "symbol": norm_symbol,
        "side": side,
        "openType": trade_cfg.get("openType", 1),
        "type": trade_cfg.get("type", 5),
        "vol": vol,
        "leverage": leverage,
        "priceProtect": trade_cfg.get("priceProtect", 0),
    }

    # TakeProfit
    tp_pct = risk_cfg.get("takeProfitPct")
    tp_price = None
    if tp_pct is not None and anchor_price:
        tp_price = _calc_tp_price(anchor_price, leverage, float(tp_pct), side)
        obj["takeProfitPrice"] = round(float(tp_price), 1)

    # StopLoss → הפעם לפי הנר האחרון שנסגר
    sl_price = None
    if last_closed_price is not None:
        sl_price = round(float(last_closed_price), 1)
        obj["stopLossPrice"] = sl_price
    else:
        # fallback לטולרנס אם לא נשלח last_closed_price
        sl_tol = float(risk_cfg.get("slTolerancePct", 0))
        if sl_tol > 0 and anchor_price:
            sl_price = _calc_sl_price(anchor_price, sl_tol, side)
            obj["stopLossPrice"] = round(float(sl_price), 1)

    # שליחה
    try:
        resp = await asyncio.to_thread(place_order, obj)
    except Exception as e:
        open_trades.pop(norm_symbol, None)
        return {"error": "place_order_exception", "symbol": norm_symbol, "exception": str(e)}

    # שמירת מצב
    success = bool(resp.get("success", False))
    if success or resp.get("code") in (0, 200, "200"):
        try:
            entry, lev = None, leverage
            pos, ws_entry = await asyncio.gather(
                mexc_api.get_open_positions(norm_symbol),
                asyncio.to_thread(lambda: cache.get_last_price(norm_symbol))
            )
            if pos and pos.get("success") and pos.get("data"):
                p = pos["data"][0]
                entry = float(p.get("holdAvgPrice", ws_entry))
                lev = float(p.get("leverage", leverage))
            else:
                entry = ws_entry

            # אחרי שקיבלת resp מ-place_order
            stop_orders = await mexc_api.get_stop_orders(symbol=norm_symbol)
            stop_plan_id = None
            if stop_orders.get("success") and stop_orders.get("data"):
                stop_plan_id = stop_orders["data"][0]["id"]

            bar_opened = ws_client.last_t.get(norm_symbol)  # 👈 זמן תחילת הנר הנוכחי
            obj_to_store = {
                **obj,
                "orderId": resp.get("data", {}).get("orderId"),
                "stopPlanOrderId": stop_plan_id,
                "entry": entry,
                "lev": lev,
                "tp_pct": tp_pct,
                "tp_price": obj.get("takeProfitPrice"),
                "sl_price": obj.get("stopLossPrice"),
                "original_tp_price": tp_price,
                "original_sl_price": sl_price,
                "updates_count": 0,
                "updates_sl": 0,
                "sl_tol": risk_cfg.get("slTolerancePct", 0.0),
                "bar_opened": bar_opened
            }
            open_trades[norm_symbol] = obj_to_store
            logging.info(f"➕ [{norm_symbol}] נוספה עסקה ל-open_trades:\n{json.dumps(obj_to_store, indent=2, ensure_ascii=False)}")

            if alert_sink:
                msg = (
                    f"🚀 עסקה נפתחה על {norm_symbol}\n"
                    f"📈 Side: {'Long' if side == 1 else 'Short'}\n"
                    f"⚖️ Leverage: {lev}x\n"
                    f"🎯 TP: {obj_to_store.get('tp_price')}\n"
                    f"🛑 SL: {obj_to_store.get('sl_price')}\n"
                )
                for i in range(3):
                    await alert_sink.notify(msg)
                    await asyncio.sleep(1)
                await alert_sink.notify("⚠️⚠️⚠️ התרעה חוזרת: נפתחה עסקה חדשה! ⚠️⚠️⚠️")

        except Exception as e:
            open_trades[norm_symbol] = obj
        return {"ok": True, "symbol": norm_symbol, "response": resp}

    open_trades.pop(norm_symbol, None)
    return {"ok": False, "symbol": norm_symbol, "response": resp}

# ---------- Main orchestration ----------
async def run(config_path: str = "config.yaml", cache=None):
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
    async def trade_cb(symbol, price_range, last_price, first_price, last_closed_price):
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

        # 👇 שולחים גם את last_closed_price ל-open_mexc_order
        await open_mexc_order(
            norm_symbol,
            price_range,
            last_price,
            first_price,
            trade_cfg,
            risk_cfg,
            cache,
            alert_sink=alert_sink,
            last_closed_price=last_closed_price
        )

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
    cache = CacheManager(mexc_api, ws_client)

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

    # --- מוניטור פשוט על העסקאות השמורות ---
    async def monitor_positions(alert_sink=None):
        """
        מנטרת את כל העסקאות ב-open_trades:
        - מנקה עסקאות שנסגרו ידנית או בשרת (TP/SL)
        - שולחת הודעה לטלגרם כשנסגרת עסקה
        """
        FAST_SLEEP = 7  # זמן השהייה בין בדיקות

        while True:
            for sym_key, trade_obj in list(open_trades.items()):
                try:
                    # 🟢 בדיקת קיום פוזיציה אמיתית דרך ה-API
                    positions_api = await mexc_api.get_open_positions(sym_key)

                    # אם אין פוזיציה פעילה → נסגרה ידנית / TP/SL הופעל
                    if (
                        not positions_api
                        or not positions_api.get("success", False)
                        or not positions_api.get("data")
                    ):
                        open_trades.pop(sym_key, None)
                        logging.info(f"🧹 נמחק {sym_key} מ-open_trades (נסגר בשרת/ידנית)")

                        # 🟢 שליחת הודעה לטלגרם על סגירה
                        if alert_sink:
                            try:
                                side_txt = "Long" if trade_obj.get("side") == 1 else "Short"
                                tp = trade_obj.get("tp_price")
                                sl = trade_obj.get("sl_price")
                                msg = (
                                    f"✅ עסקה על {sym_key} נסגרה (שרת/ידני)\n"
                                    f"📈 Side: {side_txt}\n"
                                    f"🎯 TP: {tp}\n"
                                    f"🛑 SL: {sl}"
                                )
                                await alert_sink.notify(msg)
                            except Exception as e:
                                logging.warning(f"⚠️ כשל בשליחת הודעת סגירה לטלגרם: {e}")

                        continue

                    # 🔄 אפשר להוסיף הרחבות כאן (למשל Trailing Stop בעתיד)

                except Exception as e:
                    logging.error(
                        f"⚠️ שגיאה בבדיקת פוזיציות עבור {trade_obj.get('symbol', sym_key)}: {e}",
                        exc_info=True,
                    )

            await asyncio.sleep(FAST_SLEEP)

    tasks.append(asyncio.create_task(monitor_positions(alert_sink=alert_sink)))

    # --- מוניטור TP/SL על העסקאות השמורות ---
    async def monitor_tp_sl():
        """
        מנטר עסקאות פתוחות ומבצע עדכון דינמי ל-TP/SL:
        - TP מתעדכן ברגע שהמחיר מתקרב ל־80% מהיעד, ורק אם זה משפר את המיקום.
        - SL מתעדכן:
            • בפעם הראשונה → entry price (אבל רק אחרי שהנר שבו העסקה נפתחה נסגר)
            • לאחר מכן → מחיר סגירה של נר (close candle) ± tolerance, ורק מהנר הבא.
        - משתמשים אך ורק בנתונים מתוך open_trades (ללא קריאות חוזרות לשרת).
        """
        CHECK_INTERVAL = 0.5  

        web_token = os.getenv("MEXC_API_KEY_WEB")
        if not web_token:
            logging.error("❌ לא נמצא MEXC_API_KEY_WEB ב-.env")
            return

        tp_client = MexcTPClient(api_key=web_token)
        await tp_client.start()
        logging.info("🚀 monitor_tp_sl התחיל לעבוד עם API key תקין")

        while True:
            if not open_trades:
                logging.debug("⏸️ אין עסקאות ב-open_trades → הולך לישון")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            #logging.debug(f"📊 open_trades כרגע: {json.dumps(open_trades, indent=2, ensure_ascii=False)}")

            for sym_key, trade_obj in list(open_trades.items()):
                try:
                    stop_plan_id = trade_obj.get("stopPlanOrderId")
                    sl_tol       = trade_obj.get("sl_tol", 0.0)
                    side         = trade_obj.get("side")   # 1 = Long, 3 = Short
                    entry        = trade_obj.get("entry")
                    tp_price     = trade_obj.get("tp_price")
                    sl_price     = trade_obj.get("sl_price")
                    updates_sl   = trade_obj.get("updates_sl", 0)
                    bar_opened   = trade_obj.get("bar_opened")  # 👈 נשמר בזמן פתיחת העסקה
                    current_bar  = ws_client.last_t.get(sym_key) if ws_client else None

                    logging.debug(
                        f"🔎 [{sym_key}] stop_plan_id={stop_plan_id}, sl_tol={sl_tol}, side={side}, "
                        f"entry={entry}, tp_price={tp_price}, sl_price={sl_price}, "
                        f"updates_sl={updates_sl}, bar_opened={bar_opened}, current_bar={current_bar}"
                    )

                    if not stop_plan_id or not entry:
                        logging.warning(f"⚠️ [{sym_key}] דילוג → stop_plan_id={stop_plan_id}, entry={entry}")
                        continue  

                    current_price = ws_client.get_price(sym_key) if ws_client else None
                    closed_price  = ws_client.get_last_closed_price(sym_key) if ws_client else None
                    logging.debug(f"💹 [{sym_key}] current_price={current_price}, closed_price={closed_price}")

                    # ===== עדכון TP =====
                    if current_price and tp_price:
                        updates_done = trade_obj.get("updates_count", 0)
                        tp_trigger   = entry + (tp_price - entry) * 0.8 if side == 1 else entry - (entry - tp_price) * 0.8
                        logging.debug(f"🎯 [{sym_key}] tp_trigger={tp_trigger}, tp_price={tp_price}, updates_done={updates_done}")

                        if (side == 1 and current_price >= tp_trigger) or (side == 3 and current_price <= tp_trigger):
                            new_tp = tp_price + (tp_price - entry) if side == 1 else tp_price - (entry - tp_price)
                            new_tp = round(new_tp, 1)
                            logging.info(f"📈 [{sym_key}] תנאי TP הופעל → new_tp={new_tp}")

                            if (side == 1 and new_tp <= tp_price) or (side == 3 and new_tp >= tp_price):
                                logging.debug(f"⏭️ [{sym_key}] דילוג TP → new_tp={new_tp}, tp_price={tp_price}")
                            elif new_tp == tp_price:
                                logging.debug(f"⏭️ [{sym_key}] דילוג TP → נשאר זהה ({tp_price})")
                            else:
                                logging.info(f"➡️ [{sym_key}] שולח עדכון TP → {new_tp}")
                                resp = await tp_client.update_tp_sl(
                                    stop_plan_order_id=stop_plan_id,
                                    tp=new_tp,
                                    sl=round(sl_price, 1) if sl_price else None
                                )
                                logging.debug(f"⬅️ [{sym_key}] תגובת API לעדכון TP: {resp}")
                                if resp.get("success") or str(resp.get("code")) in ("0", "200"):
                                    trade_obj["tp_price"] = new_tp
                                    trade_obj["updates_count"] = updates_done + 1
                                    logging.info(f"✅ [{sym_key}] TP עודכן בהצלחה ל-{new_tp}")
                                else:
                                    logging.warning(f"⚠️ [{sym_key}] עדכון TP נכשל → {resp}")

                    # ===== עדכון SL =====
                    if sl_tol > 0:
                        new_sl = None  # ברירת מחדל

                        # ---- שלב 1: עדכון ראשוני ל-entry אחרי סגירת הנר ----
                        if updates_sl == 0:
                            if current_bar and bar_opened and current_bar > bar_opened:
                                new_sl = round(entry, 1)
                                logging.info(f"🛡️ [{sym_key}] הנר שבו העסקה נפתחה נסגר → מגדיר SL ל-entry: {new_sl}")
                                resp = await tp_client.update_tp_sl(
                                    stop_plan_order_id=stop_plan_id,
                                    tp=round(tp_price, 1) if tp_price else None,
                                    sl=new_sl
                                )
                                logging.debug(f"⬅️ [{sym_key}] תגובת API ל-SL ראשוני: {resp}")
                                if resp.get("success") or str(resp.get("code")) in ("0", "200"):
                                    trade_obj["sl_price"] = new_sl
                                    trade_obj["updates_sl"] = -1   # 👈 מצב מיוחד: חכה לנר הבא
                                    logging.info(f"✅ [{sym_key}] SL הוגדר ל-entry ({new_sl}), ממתין לנר הבא")
                                else:
                                    logging.warning(f"⚠️ [{sym_key}] עדכון SL ראשון נכשל → {resp}")
                                continue   

                            else:
                                logging.debug(f"⏸️ [{sym_key}] עדיין באותו נר → לא מגדיר SL ראשוני")

                        # ---- שלב 2: דילוג על נר אחד אחרי entry ----
                        elif updates_sl == -1:
                            if current_bar and bar_opened and current_bar > bar_opened + 1:
                                trade_obj["updates_sl"] = 1   # 👈 מהנר הזה והלאה עדכונים רגילים
                                logging.info(f"⏭️ [{sym_key}] הנר הבא אחרי entry נסגר → מתחילים עדכוני SL רגילים")
                            else:
                                logging.debug(f"⏸️ [{sym_key}] עדיין מחכה לנר הבא אחרי entry")
                            continue

                        # ---- שלב 3: עדכונים רגילים מנרות סגורים ----
                        elif updates_sl >= 1 and closed_price:
                            new_sl = closed_price * (1 - sl_tol) if side == 1 else closed_price * (1 + sl_tol)
                            new_sl = round(new_sl, 1)
                            logging.debug(f"🔄 [{sym_key}] חישוב SL חדש מנר סגור → {new_sl}")

                        # ---- שליחה בפועל אם יש new_sl ----
                        if new_sl:
                            if (side == 1 and sl_price and new_sl < sl_price) or (side == 3 and sl_price and new_sl > sl_price):
                                logging.debug(f"⏭️ [{sym_key}] דילוג SL → new_sl={new_sl}, sl_price={sl_price}")
                            elif sl_price and new_sl == sl_price:
                                logging.debug(f"⏭️ [{sym_key}] דילוג SL → נשאר זהה ({sl_price})")
                            else:
                                logging.info(f"➡️ [{sym_key}] שולח עדכון SL → {new_sl}")
                                resp = await tp_client.update_tp_sl(
                                    stop_plan_order_id=stop_plan_id,
                                    tp=round(tp_price, 1) if tp_price else None,
                                    sl=new_sl
                                )
                                logging.debug(f"⬅️ [{sym_key}] תגובת API לעדכון SL: {resp}")
                                if resp.get("success") or str(resp.get("code")) in ("0", "200"):
                                    trade_obj["sl_price"] = new_sl
                                    trade_obj["updates_sl"] += 1
                                    logging.info(f"✅ [{sym_key}] SL עודכן בהצלחה ל-{new_sl}")
                                else:
                                    logging.warning(f"⚠️ [{sym_key}] עדכון SL נכשל → {resp}")

                except Exception as e:
                    logging.error(f"💥 [{sym_key}] שגיאה ב-monitor_tp_sl: {e}", exc_info=True)

            await asyncio.sleep(CHECK_INTERVAL)

    tasks.append(asyncio.create_task(monitor_tp_sl()))

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
