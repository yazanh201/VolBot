# services/monitors.py
import asyncio
import logging
import os
import json
from services.Tp_Sl_Change import MexcTPClient

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


async def monitor_tp_sl(open_trades, ws_client, alert_sink=None):
    CHECK_INTERVAL = 0.5

    web_token = os.getenv("MEXC_API_KEY_WEB")
    if not web_token:
        logging.error("❌ לא נמצא MEXC_API_KEY_WEB ב-.env")
        return

    tp_client = MexcTPClient(api_key=web_token)
    await tp_client.start()
    logging.info("🚀 monitor_tp_sl התחיל לעבוד עם API key תקין")

    # רמות “נעילת רווח” באחוזי PnL (על המרג'ין)
        # רמות “נעילת רווח” באחוזי PnL (על המרג'ין)
    LOCK_LEVELS = [50, 100, 200, 300, 400]  # 👈 לניסוי – רמות נמוכות מאוד

    def calc_upnl_pct(side, entry, curr, lev):
        """
        אחוז רווח/הפסד על המרג'ין (PnL%) ≈ שינוי מחיר * מינוף.
        side: 1=Long, 3=Short
        """
        if not entry or not curr or not lev:
            return None
        pct_move = (curr - entry) / entry if side == 1 else (entry - curr) / entry
        return pct_move * float(lev) * 100.0

    def price_for_lock_pct(side, entry, lev, lock_pct):
        """
        מחיר SL שינעל lock_pct% רווח על המרג'ין.
        Δ%_price ≈ lock_pct/lev  → Long: entry*(1+Δ), Short: entry*(1-Δ).
        """
        step = (lock_pct / 100.0) / float(lev)
        return round(entry * (1.0 + step) if side == 1 else entry * (1.0 - step), 1)

    while True:
        if not open_trades:
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        for sym_key, trade_obj in list(open_trades.items()):
            try:
                stop_plan_id = trade_obj.get("stopPlanOrderId")
                sl_tol       = float(trade_obj.get("sl_tol", 0.0))
                side         = int(trade_obj.get("side", 0))     # 1=Long, 3=Short
                entry        = trade_obj.get("entry")
                tp_price     = trade_obj.get("tp_price")
                sl_price     = trade_obj.get("sl_price")
                updates_sl   = int(trade_obj.get("updates_sl", 0))
                bar_opened   = trade_obj.get("bar_opened")
                lev          = float(trade_obj.get("lev") or trade_obj.get("leverage") or 1.0)
                locked_pct   = float(trade_obj.get("locked_pct", 0.0))  # 👈 חדש
                price_scale  = int(trade_obj.get("price_scale", 2))    # 👈 חדש

                if not stop_plan_id or not entry:
                    logging.warning(f"⚠️ [{sym_key}] דילוג → stop_plan_id={stop_plan_id}, entry={entry}")
                    continue

                current_price = ws_client.get_price(sym_key) if ws_client else None
                closed_price  = ws_client.get_last_closed_price(sym_key) if ws_client else None
                current_bar   = ws_client.last_t.get(sym_key) if ws_client else None

                # ===== עדכון TP (ללא שינוי) =====
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

                # ===== לוגיקת SL =====
                if sl_tol <= 0:
                    continue

                # --- שלב 1: אחרי סגירת הנר שבו נפתחה העסקה – עדכון ל-Entry רק אם לא בהפסד
                if updates_sl == 0:
                    if current_bar and bar_opened and current_bar > bar_opened:
                        # בדיקת הפסד רגעי – אם בהפסד, אל תעלה ל-Entry
                        in_profit_or_flat = (
                            (side == 1 and current_price is not None and current_price >= entry) or
                            (side == 3 and current_price is not None and current_price <= entry)
                        )
                        if in_profit_or_flat:
                            new_sl = round(entry, price_scale)
                            resp = await tp_client.update_tp_sl(stop_plan_order_id=stop_plan_id,
                                                                tp=round(tp_price, price_scale) if tp_price else None,
                                                                sl=new_sl)
                            if resp.get("success") or str(resp.get("code")) in ("0", "200"):
                                trade_obj["sl_price"] = new_sl
                                trade_obj["updates_sl"] = -1  # חכה לנר הבא
                                logging.info(f"✅ [{sym_key}] SL הוגדר ל-Entry ({new_sl}); ממתין לנר הבא")
                            else:
                                logging.warning(f"⚠️ [{sym_key}] עדכון SL ל-Entry נכשל → {resp}")
                            # בכל מקרה לא נמשיך לעדכונים נוספים באותו סבב
                            continue
                    # אם עדיין באותו נר – כלום
                    # אם בהפסד – דילגנו ולא שינינו

                # --- שלב 2: דילוג על נר אחד אחרי Entry
                elif updates_sl == -1:
                    if current_bar and bar_opened and current_bar > bar_opened + 1:
                        trade_obj["updates_sl"] = 1  # מתחילים רגיל מהנר הבא
                        logging.info(f"⏭️ [{sym_key}] הנר הבא אחרי Entry נסגר → מתחילים עדכוני SL רגילים")
                    continue

                # --- שלב 3: עדכונים רגילים + טראיילינג לפי רמות רווח
                new_sl_to_send = None

                # 3a) “SL לפי נר סגור” (המודל הקיים)
                if updates_sl >= 1 and closed_price:
                    candidate = closed_price * (1 - sl_tol) if side == 1 else closed_price * (1 + sl_tol)
                    candidate = round(candidate, price_scale)
                    # שלח רק אם משפר:
                    improve = (side == 1 and (sl_price is None or candidate > sl_price)) or \
                              (side == 3 and (sl_price is None or candidate < sl_price))
                    if improve:
                        new_sl_to_send = candidate

                # 3b) “נעילת רווח” לפי רמות (50%, 100%, ...)
                upnl_pct = calc_upnl_pct(side, entry, current_price, lev) if current_price else None
                if upnl_pct is not None:
                    # מצא רמה הבאה שעברנו ועדיין לא ננעלה
                    next_level = None
                    for lvl in LOCK_LEVELS:
                        if lvl > locked_pct and upnl_pct >= lvl:
                            next_level = lvl
                            break
                    if next_level is not None:
                        lock_sl = price_for_lock_pct(side, entry, lev, next_level)
                        # שלח רק אם משפר מול ה-SL הנוכחי וגם מול מועמד נר-סגור אם קיים
                        def better(a, b):
                            if a is None: return True
                            return (side == 1 and b > a) or (side == 3 and b < a)
                        if better(sl_price, lock_sl) and better(new_sl_to_send, lock_sl):
                            new_sl_to_send = lock_sl
                            trade_obj["locked_pct"] = next_level  # עדכנו רמת נעילה

                # שליחה
                if new_sl_to_send and new_sl_to_send != sl_price:
                    resp = await tp_client.update_tp_sl(stop_plan_order_id=stop_plan_id,
                                                        tp=round(tp_price, price_scale) if tp_price else None,
                                                        sl=new_sl_to_send)
                    if resp.get("success") or str(resp.get("code")) in ("0", "200"):
                        trade_obj["sl_price"] = new_sl_to_send
                        trade_obj["updates_sl"] = max(1, updates_sl + 1)
                        logging.info(f"✅ [{sym_key}] SL עודכן ל-{new_sl_to_send} (locked_pct={trade_obj.get('locked_pct', 0)})")
                    else:
                        logging.warning(f"⚠️ [{sym_key}] עדכון SL נכשל → {resp}")

            except Exception as e:
                logging.error(f"💥 [{sym_key}] שגיאה ב-monitor_tp_sl: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)


def start_monitors(open_trades, mexc_api, ws_client, alert_sink=None):
    """
    מפעיל את שני המוניטורים כתהליכי asyncio ומחזיר את רשימת המשימות.
    """
    tasks = []
    tasks.append(asyncio.create_task(monitor_positions(open_trades, mexc_api, alert_sink)))
    tasks.append(asyncio.create_task(monitor_tp_sl(open_trades, ws_client, alert_sink)))
    return tasks
