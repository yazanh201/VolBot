import asyncio
import json
import logging
import websockets

WS_URL = "wss://contract.mexc.com/edge"

class MexcWebSocket:
    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.connection = None
        self.keep_running = True
        self.prices: dict[str, float] = {}

    async def connect(self):
        """התחברות ל-WebSocket של MEXC"""
        self.connection = await websockets.connect(WS_URL, ping_interval=None)
        logging.info("✅ מחובר ל-MEXC WebSocket")

        # נרשמים ל-ticker בלבד
        for sym in self.symbols:
            sub_ticker = {
                "method": "sub.ticker",
                "param": {"symbol": sym}
            }
            await self.connection.send(json.dumps(sub_ticker))
            logging.info(f"📡 נרשמתי ל-ticker עבור {sym}")

    async def listen(self):
        """מאזין להודעות נכנסות ומעדכן מחירים חיים"""
        try:
            async for msg in self.connection:
                data = json.loads(msg)

                if data.get("channel") == "push.ticker":
                    sym = data.get("symbol")
                    if sym and "data" in data:
                        last_price = float(data["data"].get("lastPrice", 0))
                        self.prices[sym] = last_price
                        logging.debug(f"💹 {sym} → {last_price}")

                elif data.get("channel") == "pong":
                    logging.debug("📡 התקבל Pong")

        except Exception as e:
            logging.error(f"❌ שגיאה ב-WebSocket: {e}")

    async def heartbeat(self):
        """שולח Ping כל כמה שניות כדי לשמור על החיבור"""
        while self.keep_running and self.connection:
            try:
                await self.connection.send(json.dumps({"method": "ping"}))
                await asyncio.sleep(15)
            except Exception as e:
                logging.error(f"❌ שגיאה ב-Heartbeat: {e}")
                break

    async def run(self):
        """מריץ את ההתחברות וההאזנה עם reconnect אוטומטי"""
        while self.keep_running:
            try:
                await self.connect()
                await asyncio.gather(
                    self.listen(),
                    self.heartbeat()
                )
            except Exception as e:
                if self.keep_running:  # רק אם לא עצרנו ידנית
                    logging.error(f"⚠️ חיבור נפל, מנסה להתחבר מחדש... ({e})")
                    await asyncio.sleep(5)

    def get_price(self, symbol: str) -> float | None:
        """מחזיר את המחיר האחרון של הסימבול או None אם עדיין לא הגיע"""
        return self.prices.get(symbol)

    async def close(self):
        """סגירה מסודרת של החיבור"""
        self.keep_running = False
        if self.connection:
            await self.connection.close()
            self.connection = None
        logging.info("🔌 WebSocket נסגר נקי")

# --- בדיקה מקומית ---
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    symbols = ["BTC_USDT", "SOL_USDT"]
    ws = MexcWebSocket(symbols)

    async def main():
        task = asyncio.create_task(ws.run())

        try:
            while True:
                btc = ws.get_price("BTC_USDT")
                sol = ws.get_price("SOL_USDT")
                print(f"BTC: {btc} | SOL: {sol}")
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logging.info("🛑 הופסק ידנית")
            await ws.close()   # סגירה נקייה
            task.cancel()

    asyncio.run(main())
