# app_main.py
import asyncio, yaml, logging, os, json, time
from dotenv import load_dotenv
from collections import defaultdict

from utils.alert_sink import AlertSink
from utils.alert_sink import fmt_open_msg, fmt_close_msg  # נשאר ל-API שלך
from core.SpikeEngine import SpikeEngine
from services.mexc_api import MexcAPI
from services.mexc_order import place_order
from services.rate_limiter import RateLimiter  # אם בשימוש בקוד שלך
from services.mexc_ws import MexcWebSocket
from services.Tp_Sl_Change import MexcTPClient
from cashe.cache_manager import CacheManager

from services.monitors import start_monitors  # ← המודול שפיצלנו

# ========== גלובליים ==========
ws_client = None   # לקוח WS גלובלי לשליפת מחירים בזמן אמת
trade_counters = defaultdict(list)
open_trades: dict[str, dict] = {}   # { "BTC_USDT": {payload}, ... }

# ---------- ENV & Logging ----------
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8", mode="w"),
    ],
)

# logging.basicConfig(
#     level=logging.DEBUG,
#     format="%(asctime)s | %(levelname)s | %(message)s",
#     handlers=[
#         logging.StreamHandler(),
#         logging.FileHandler("bot.log", encoding="utf-8", mode="w"),
#     ],
# )

# ---------- API keys ----------
mexc_api_key = os.environ.get("MEXC_API_KEY_WEB2", "").strip()
mexc_secret  = os.environ.get("MEXC_API_SECRET_WEB", "").strip()
if not mexc_api_key or not mexc_secret:
    logging.error("❌ אחד או יותר מהמפתחות חסר! בדוק את קובץ .env")
    raise SystemExit(1)

mexc_api = MexcAPI(mexc_api_key, mexc_secret)

# ---------- Helpers ----------
def can_open_new_trade(symbol: str, max_trades_per_hour: int = 1) -> bool:
    now = time.time()
    one_hour_ago = now - 3600
    trade_counters[symbol] = [ts for ts in trade_counters[symbol] if ts > one_hour_ago]
    if len(trade_counters[symbol]) >= max_trades_per_hour:
        return False
    trade_counters[symbol].append(now)
    return True

def _calc_tp_price(entry: float, leverage: float, tp_pct: float, side_open: int) -> float:
    if leverage <= 0:
        leverage = 1.0
    step = (tp_pct / 100.0) / leverage
    if side_open == 1:
        return entry * (1.0 + step)
    elif side_open == 3:
        return entry * (1.0 - step)
    else:
        raise ValueError(f"unknown side_open={side_open}")

def _calc_sl_price(entry: float, sl_tolerance: float, side_open: int) -> float:
    if side_open == 1:
        return entry * (1.0 - sl_tolerance)
    elif side_open == 3:
        return entry * (1.0 + sl_tolerance)
    else:
        raise ValueError(f"unknown side_open={side_open}")

# ---------- פתיחת עסקה ----------
async def open_mexc_order(
    symbol: str,
    price_range: float,
    last_price: float,
    first_price: float,
    trade_cfg: dict,
    risk_cfg: dict,
    cache,
    alert_sink=None,
    last_closed_price: float = None,
    suggested_side: int = None
):
    norm_symbol = mexc_api.normalize_symbol(symbol)

    # מגבלת עסקאות לשעה
    if not can_open_new_trade(norm_symbol, max_trades_per_hour=6):
        logging.info("⏳ דילוג → כבר נפתחו מספיק עסקאות לשעה האחרונה עבור %s", norm_symbol)
        return {"skipped": "trade_limit_reached", "symbol": norm_symbol}

    # מניעת פתיחה כפולה
    if norm_symbol in open_trades:
        logging.info("⛔ דילוג → כבר קיימת עסקה על %s", norm_symbol)
        return {"skipped": "local_open_trade_exists", "symbol": norm_symbol}

    open_trades[norm_symbol] = {"pending": True}

    # כיוון העסקה
    if last_price == first_price:
        logging.info("⏸️ דילוג → last_price == first_price (%s)", last_price)
        open_trades.pop(norm_symbol, None)
        return {"skipped": "neutral_direction", "symbol": norm_symbol}

    side = suggested_side if suggested_side is not None else (1 if last_price > first_price else 3)

    # --- שימוש ב-CacheManager ---
    specs = await cache.get_contract_specs(norm_symbol)
    balance = await cache.get_balance()
    anchor_price = cache.get_last_price(norm_symbol) or last_price
    price_scale = cache.get_price_scale(norm_symbol)

    if not specs or not balance or not anchor_price:
        open_trades.pop(norm_symbol, None)
        return {"error": "cache_data_missing", "symbol": norm_symbol}

    percent  = risk_cfg.get("percentPerTrade", trade_cfg.get("percentPerTrade", 5))
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

    obj = {
        "symbol": norm_symbol,
        "side": side,
        "openType": trade_cfg.get("openType", 1),
        "type": trade_cfg.get("type", 5),
        "vol": vol,
        "leverage": leverage,
        "priceProtect": trade_cfg.get("priceProtect", 0),
    }

    tp_pct = risk_cfg.get("takeProfitPct")
    tp_price = None
    if tp_pct is not None and anchor_price:
        tp_price = _calc_tp_price(anchor_price, leverage, float(tp_pct), side)
        obj["takeProfitPrice"] = round(float(tp_price), price_scale)

    sl_price = None
    if last_closed_price is not None:
        sl_price = round(float(last_closed_price), price_scale)
        obj["stopLossPrice"] = sl_price
    else:
        sl_tol = float(risk_cfg.get("slTolerancePct", 0))
        if sl_tol > 0 and anchor_price:
            sl_price = _calc_sl_price(anchor_price, sl_tol, side)
            obj["stopLossPrice"] = round(float(sl_price), price_scale)

    # --- פתיחת עסקה מיידית ---
    try:
        resp = await asyncio.to_thread(place_order, obj)
    except Exception as e:
        open_trades.pop(norm_symbol, None)
        return {"error": "place_order_exception", "symbol": norm_symbol, "exception": str(e)}

    success = bool(resp.get("success", False))
    if success or resp.get("code") in (0, 200, "200"):
        # נשמור מיד ב-open_trades כדי לדעת שיש עסקה פתוחה
        open_trades[norm_symbol] = {**obj, "orderId": resp.get("data", {}).get("orderId")}
        logging.info(f"🚀 [{norm_symbol}] עסקה נפתחה ונשמרה ב-open_trades")

        # שליחת הודעה לטלגרם מייד
        if alert_sink:
            msg = (
                f"🚀 עסקה נפתחה על {norm_symbol}\n"
                f"📈 Side: {'Long' if side == 1 else 'Short'}\n"
                f"⚖️ Leverage: {leverage}x\n"
                f"🎯 TP: {obj.get('takeProfitPrice')}\n"
                f"🛑 SL: {obj.get('stopLossPrice')}\n"
            )
            try:
                await alert_sink.notify(msg)
            except Exception as e:
                logging.error(f"❌ שגיאה בשליחת הודעה לטלגרם: {e}")

        # --- עדכון פרטים נוספים ברקע ---
        async def update_details():
            try:
                pos, ws_entry = await asyncio.gather(
                    mexc_api.get_open_positions(norm_symbol),
                    asyncio.to_thread(lambda: cache.get_last_price(norm_symbol))
                )
                entry = ws_entry
                lev = leverage
                if pos and pos.get("success") and pos.get("data"):
                    p = pos["data"][0]
                    entry = float(p.get("holdAvgPrice", ws_entry))
                    lev = float(p.get("leverage", leverage))

                stop_orders = await mexc_api.get_stop_orders(symbol=norm_symbol)
                stop_plan_id = None
                if stop_orders.get("success") and stop_orders.get("data"):
                    stop_plan_id = stop_orders["data"][0]["id"]

                bar_opened = ws_client.last_t.get(norm_symbol)
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
                    "bar_opened": bar_opened,
                    "price_scale": price_scale
                }
                open_trades[norm_symbol] = obj_to_store
                logging.info(f"ℹ️ [{norm_symbol}] פרטי העסקה עודכנו בהצלחה")
            except Exception as e:
                logging.error(f"⚠️ שגיאה בעדכון פרטי העסקה עבור {norm_symbol}: {e}")

        asyncio.create_task(update_details())  # ← ירוץ ברקע

        return {"ok": True, "symbol": norm_symbol, "response": resp}

    # אם לא הצליח
    open_trades.pop(norm_symbol, None)
    return {"ok": False, "symbol": norm_symbol, "response": resp}

# ---------- Main ----------
async def run(config_path: str = "config.yaml", cache=None):
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # --- Telegram ---
    tg_cfg = cfg.get("telegram", {}) or {}
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

    try:
        if alert_sink.tg_enabled and alert_sink.bot_token and chat_ids:
            await alert_sink.notify("✅ Fast Vol bot is up – Telegram OK!")
        else:
            logging.info("🔕 Telegram disabled or missing config — continuing without Telegram.")
    except Exception as e:
        logging.warning("⚠️ Telegram notify failed: %s — continuing.", e, exc_info=True)

    mexc_trades = cfg.get("mexc_trades", {})
    risk_cfg    = cfg.get("risk", {"takeProfitPct": 50, "stopLossPct": -20})

    async def trade_cb(symbol, price_range, last_price, first_price, last_closed_price, suggested_side):
        norm_symbol = mexc_api.normalize_symbol(symbol)
        if norm_symbol in open_trades:
            logging.info("⛔ דילוג (trade_cb) → כבר קיימת עסקה על %s", norm_symbol)
            return
        trade_cfg = mexc_trades.get(norm_symbol) or mexc_trades.get(symbol)
        if not trade_cfg:
            logging.warning("⚠️ אין הגדרות מסחר עבור %s", norm_symbol)
            return
        await open_mexc_order(
            norm_symbol, price_range, last_price, first_price,
            trade_cfg, risk_cfg, cache, alert_sink=alert_sink,
            last_closed_price=last_closed_price,suggested_side=suggested_side
        )

    spike_cfg      = cfg.get("spike", {})
    symbols_cfg    = spike_cfg.get("symbols", ["BTC_USDT"])
    thresholds_map = spike_cfg.get("thresholds", {})
    interval       = spike_cfg.get("interval", "Min1")
    cooldown       = int(spike_cfg.get("cooldown_seconds", 5))

    tasks = []

    # --- WebSocket ---
    global ws_client
    ws_client = MexcWebSocket([mexc_api.normalize_symbol(s) for s in symbols_cfg])
    tasks.append(asyncio.create_task(ws_client.run()))
    cache = CacheManager(mexc_api, ws_client)

    for sym in symbols_cfg:
        norm_sym = mexc_api.normalize_symbol(sym)
        threshold_default = spike_cfg.get("threshold", 300)
        threshold = float(thresholds_map.get(norm_sym, threshold_default))
        logging.info(f"🔧 הגדרת threshold עבור {norm_sym}: {threshold}")

        engine = SpikeEngine(
            symbol=norm_sym,
            threshold=threshold,       # 👈 פה הערך מה-config.yaml
            interval=interval,
            cooldown_seconds=cooldown,
            alert_sink=alert_sink,
            mexc_api=mexc_api,
            ws=ws_client,
            open_trades=open_trades,
            trade_cb=trade_cb
        )


        tasks.append(asyncio.create_task(engine.run()))

    # --- Monitors (עברו לקובץ נפרד) ---
    tasks.extend(start_monitors(open_trades, mexc_api, ws_client, alert_sink=alert_sink))

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
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass
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
