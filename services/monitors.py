# services/monitors.py
import asyncio
import logging
import os
import json
from services.Tp_Sl_Change import MexcTPClient
import time
from services.mexc_client import MexcClient


async def monitor_positions(open_trades, mexc_api, alert_sink=None):
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

                # 🔄 הרחבות עתידיות (Trailing וכו') אפשר להוסיף כאן

            except Exception as e:
                logging.error(
                    f"⚠️ שגיאה בבדיקת פוזיציות עבור {trade_obj.get('symbol', sym_key)}: {e}",
                    exc_info=True,
                )

        await asyncio.sleep(FAST_SLEEP)


async def monitor_tp_sl(open_trades, ws_client,mexc_client, alert_sink=None):
    CHECK_INTERVAL = 0.5

    web_token = os.getenv("MEXC_API_KEY_WEB")
    if not web_token:
        logging.error("❌ לא נמצא MEXC_API_KEY_WEB ב-.env")
        return

    tp_client = MexcTPClient(api_key=web_token)
    await tp_client.start()
    logging.info("🚀 monitor_tp_sl התחיל לעבוד עם API key תקין")
    mexc_client = MexcClient(api_key=web_token)
    await mexc_client.start()


    # רמות נעילת רווח באחוזי PnL (על המרג'ין)
    LOCK_LEVELS = [50, 150, 250, 350, 500, 700, 800, 900, 1000, 1200]

    def calc_upnl_pct(side, entry, curr, lev):
        """ חישוב אחוז רווח/הפסד על המרג'ין (PnL%) """
        if not entry or not curr or not lev:
            return None
        pct_move = (curr - entry) / entry if side == 1 else (entry - curr) / entry
        return pct_move * float(lev) * 100.0

    def price_for_lock_pct(side, entry, lev, lock_pct):
        """ מחיר SL שינעל lock_pct% רווח על המרג'ין """
        step = (lock_pct / 100.0) / float(lev)
        return round(entry * (1.0 + step) if side == 1 else entry * (1.0 - step), 1)

    while True:
        if not open_trades:
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        for sym_key, trade_obj in list(open_trades.items()):
            try:
                stop_plan_id = trade_obj.get("stopPlanOrderId")
                side         = int(trade_obj.get("side", 0))     # 1=Long, 3=Short
                entry        = trade_obj.get("entry")
                tp_price     = trade_obj.get("tp_price")
                sl_price     = trade_obj.get("sl_price")
                vol          = trade_obj.get("vol", 1)
                lev          = float(trade_obj.get("lev") or trade_obj.get("leverage") or 1.0)
                locked_pct   = float(trade_obj.get("locked_pct", 0.0))
                price_scale  = int(trade_obj.get("price_scale", 2))
                start_time   = trade_obj.get("start_time")

                if not entry:
                    logging.warning(f"⚠️ [{sym_key}] דילוג → entry={entry}")
                    continue

                current_price = ws_client.get_price(sym_key) if ws_client else None

                # ===== בדיקת timeout (10 שניות, 100% רווח) =====
                if start_time:
                    elapsed = time.time() - start_time
                    logging.debug(f"⏱️ [{sym_key}] חלפו {elapsed:.2f} שניות מאז פתיחת העסקה")

                    if elapsed >= 13:
                        upnl_pct = calc_upnl_pct(side, entry, current_price, lev) if current_price else None
                        logging.debug(
                            f"📊 [{sym_key}] בדיקת Timeout → upnl_pct={upnl_pct}, lev={lev}, entry={entry}, curr={current_price}"
                        )

                        if upnl_pct is not None and upnl_pct < 40:
                            # קביעת side לסגירה
                            close_side = 2 if side == 3 else 4 if side == 1 else None
                            if not close_side:
                                logging.error(f"⚠️ [{sym_key}] side לא תקין לסגירה: {side}")
                                continue

                            vol = trade_obj.get("vol", 1)  # נשמר בעת פתיחת העסקה

                            logging.warning(
                                f"⏱️ [{sym_key}] Timeout → שולח סגירה עם symbol={sym_key}, side={close_side}, type=5, vol={vol}"
                            )

                            try:
                                # קריאה לאובייקט של MexcClient (שנוצר בתחילת הקוד)
                                resp = await mexc_client.close_position(
                                    symbol=sym_key,
                                    side=close_side,
                                    vol=vol,
                                    type_=5   # תמיד Market
                                )
                                logging.info(f"📥 [{sym_key}] תגובת close_position: {resp}")
                                if resp.get("success") or str(resp.get("code")) in ("0", "200"):
                                    open_trades.pop(sym_key, None)
                                    logging.info(f"✅ [{sym_key}] נסגרה בהצלחה אחרי timeout")
                                else:
                                    logging.warning(f"⚠️ [{sym_key}] ניסיון סגירה נכשל → {resp}")
                            except Exception as e:
                                logging.error(f"💥 [{sym_key}] שגיאה בקריאת close_position: {e}", exc_info=True)

                            continue  # דילוג לשאר העסקאות


                # ===== לוגיקת TP =====
                if current_price and tp_price:
                    updates_done = trade_obj.get("updates_count", 0)
                    tp_trigger   = entry + (tp_price - entry) * 0.8 if side == 1 else entry - (entry - tp_price) * 0.8
                    if (side == 1 and current_price >= tp_trigger) or (side == 3 and current_price <= tp_trigger):
                        new_tp = round(tp_price + (tp_price - entry), price_scale) if side == 1 else round(tp_price - (entry - tp_price), price_scale)
                        if (side == 1 and new_tp > tp_price) or (side == 3 and new_tp < tp_price):
                            resp = await tp_client.update_tp_sl(stop_plan_order_id=stop_plan_id, tp=new_tp,
                                                                sl=round(sl_price, price_scale) if sl_price else None)
                            if resp.get("success") or str(resp.get("code")) in ("0", "200"):
                                trade_obj["tp_price"] = new_tp
                                trade_obj["updates_count"] = updates_done + 1
                                logging.info(f"✅ [{sym_key}] TP עודכן ל-{new_tp}")

                # ===== לוגיקת SL (לפי LOCK_LEVELS) =====
                new_sl_to_send = None
                upnl_pct = calc_upnl_pct(side, entry, current_price, lev) if current_price else None

                if upnl_pct is not None:
                    next_level = None
                    for lvl in LOCK_LEVELS:
                        if lvl > locked_pct and upnl_pct >= lvl:
                            next_level = lvl
                            break

                    if next_level is not None:
                        lock_sl = price_for_lock_pct(side, entry, lev, next_level)

                        def better(a, b):
                            if a is None:
                                return True
                            return (side == 1 and b > a) or (side == 3 and b < a)

                        if better(sl_price, lock_sl):
                            new_sl_to_send = lock_sl
                            trade_obj["locked_pct"] = next_level

                if new_sl_to_send and new_sl_to_send != sl_price:
                    resp = await tp_client.update_tp_sl(stop_plan_order_id=stop_plan_id,
                                                        tp=round(tp_price, price_scale) if tp_price else None,
                                                        sl=new_sl_to_send)
                    if resp.get("success") or str(resp.get("code")) in ("0", "200"):
                        trade_obj["sl_price"] = new_sl_to_send
                        logging.info(f"✅ [{sym_key}] SL עודכן ל-{new_sl_to_send} (locked_pct={trade_obj.get('locked_pct', 0)})")
                    else:
                        logging.warning(f"⚠️ [{sym_key}] עדכון SL נכשל → {resp}")

            except Exception as e:
                logging.error(f"💥 [{sym_key}] שגיאה ב-monitor_tp_sl: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)



def start_monitors(open_trades, mexc_api, ws_client,mexc_client, alert_sink=None):
    """
    מפעיל את שני המוניטורים כתהליכי asyncio ומחזיר את רשימת המשימות.
    """
    tasks = []
    tasks.append(asyncio.create_task(monitor_positions(open_trades, mexc_api, alert_sink)))
    tasks.append(asyncio.create_task(monitor_tp_sl(open_trades, ws_client,mexc_client, alert_sink)))
    return tasks
