import aiohttp
import logging

class AlertSink:
    def __init__(self, tg_enabled: bool, bot_token: str, chat_ids: list[str]):
        self.tg_enabled = tg_enabled
        self.bot_token = bot_token
        if isinstance(chat_ids, str):
            chat_ids = [chat_ids]
        self.chat_ids = chat_ids

    async def notify(self, msg: str):
        if not (self.tg_enabled and self.bot_token and self.chat_ids):
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for chat_id in self.chat_ids:
                payload = {"chat_id": chat_id, "text": msg}
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status != 200:
                            logging.warning("Telegram send failed for %s: %s", chat_id, await resp.text())
                except Exception as e:
                    logging.warning("Telegram exception for %s: %s", chat_id, e)



    # === Helpers for trade messages ===
def fmt_open_msg(symbol: str, side: str, qty, lev, entry=None, tp_pct=None, tp_price=None):
    lines = [
        "✅ OPEN TRADE",
        f"• Symbol: {symbol}",
        f"• Side: {side}",
        f"• Qty: {qty}",
        f"• Leverage: x{lev}",
    ]
    if entry is not None:
        lines.append(f"• Entry: {entry}")
    if tp_pct is not None:
        lines.append(f"• TP%: {tp_pct}")
    if tp_price is not None:
        lines.append(f"• TP Price: {tp_price}")
    return "\n".join(lines)


def fmt_close_msg(symbol: str, side: str, qty, pnl=None):
    lines = [
        "✅ CLOSE TRADE",
        f"• Symbol: {symbol}",
        f"• Side: {side}",
        f"• Qty: {qty}",
    ]
    if pnl is not None:
        lines.append(f"• PnL: {pnl}%")
    return "\n".join(lines)

