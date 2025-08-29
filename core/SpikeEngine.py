import asyncio, logging, time, random
from typing import Optional, Callable, Awaitable


class SpikeEngine:
    def __init__(self, symbol: str, threshold: float, interval: str,
                 cooldown_seconds: int,
                 alert_sink,
                 mexc_api,
                 ws,   # 👈 נוסיף WebSocket
                 trade_cb: Optional[Callable[[str, float, float, float], Awaitable[None]]] = None,
                 poll_seconds: float = 1.0):
        self.symbol = symbol.upper()
        self.threshold = float(threshold)
        self.interval = interval
        self.cooldown_seconds = int(cooldown_seconds)
        self.alert_sink = alert_sink
        self.mexc_api = mexc_api
        self.ws = ws   # 👈 שמור רפרנס ל־WebSocket
        self.trade_cb = trade_cb
        self._next_allowed_ts: float = 0.0
        self.poll_seconds = float(poll_seconds)

    async def run(self):
        while True:
            try:
                # קנדל מה־API
                candle = await self.mexc_api.get_last_closed_candle(self.symbol, self.interval)
                # מחיר נוכחי מה־WebSocket
                last_price = self.ws.get_price(self.symbol)

                logging.debug(f"🔍 {self.symbol} | candle={candle} | last_price={last_price}")

                if not candle or not last_price:
                    logging.warning("⚠️ אין נתוני candle/price עבור %s", self.symbol)
                    await asyncio.sleep(self.poll_seconds)
                    continue

                close_price = candle["close"]
                diff = abs(last_price - close_price)

                if diff >= self.threshold and time.time() >= self._next_allowed_ts:
                    self._next_allowed_ts = time.time() + self.cooldown_seconds

                    msg = (
                        f"⚡ Spike Detected\n"
                        f"Symbol: {self.symbol}\n"
                        f"Close price: {close_price:.2f}\n"
                        f"Current price: {last_price:.2f}\n"
                        f"Diff: {diff:.2f} (Threshold: {self.threshold:.2f})"
                    )
                    await self.alert_sink.notify(msg)
                    logging.info(msg)

                    if self.trade_cb:
                        try:
                            await self.trade_cb(self.symbol, diff, last_price, close_price)
                        except Exception as e:
                            logging.exception("❌ שגיאה ב-trade_cb עבור %s: %s", self.symbol, e)

            except Exception as e:
                logging.error("⚠️ שגיאה ב-SpikeEngine עבור %s: %s", self.symbol, e, exc_info=True)

            await asyncio.sleep(self.poll_seconds + random.uniform(0.0, 0.2))




if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    from utils.alert_sink import AlertSink
    from services.mexc_api import MexcAPI
    from services.mexc_ws import MexcWebSocket

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)s | %(message)s")

    load_dotenv()
    mexc_api = MexcAPI(os.getenv("MEXC_API_KEY_WEB2", ""), os.getenv("MEXC_API_SECRET_WEB", ""))
    alert_sink = AlertSink(tg_enabled=False, bot_token="", chat_ids=[])

    # WebSocket – נריץ אותו ברקע
    ws = MexcWebSocket(["BTC_USDT"])
    
    async def main():
        asyncio.create_task(ws.run())
        engine = SpikeEngine(
            symbol="BTC_USDT",
            threshold=100,
            interval="Min1",
            cooldown_seconds=30,
            alert_sink=alert_sink,
            mexc_api=mexc_api,
            ws=ws
        )
        await engine.run()

    asyncio.run(main())
