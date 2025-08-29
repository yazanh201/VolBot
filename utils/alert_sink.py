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
