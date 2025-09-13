import asyncio
import json
import logging
import time
import websockets

WS_URL = "wss://contract.mexc.com/edge"

class MexcWebSocket:
    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.connection = None
        self.keep_running = True
        self.prices: dict[str, float] = {}
        self.closed_candles: dict[str, float] = {}   # מחיר סגירה של נרות סגורים
        self.last_t: dict[str, int] = {}             # timestamp אחרון לכל סימבול
        self.candle_start: dict[str, float] = {}     # מתי התחיל הנר הנוכחי (epoch seconds)

    async def connect(self):
        """התחברות ל-WebSocket של MEXC"""
        self.connection = await websockets.connect(WS_URL, ping_interval=None)
        logging.info("✅ מחובר ל-MEXC WebSocket")

        for sym in self.symbols:
            # נרשמים ל-ticker
            sub_ticker = {"method": "sub.ticker", "param": {"symbol": sym}}
            await self.connection.send(json.dumps(sub_ticker))
            logging.info(f"📡 נרשמתי ל-ticker עבור {sym}")

            # נרשמים ל-kline (נרות דקה)
            sub_kline = {"method": "sub.kline", "param": {"symbol": sym, "interval": "Min1"}}
            await self.connection.send(json.dumps(sub_kline))
            logging.info(f"📡 נרשמתי ל-kline (Min1) עבור {sym}")

    async def listen(self):
        """מאזין להודעות נכנסות ומעדכן מחירים חיים ונרות"""
        try:
            async for msg in self.connection:
                data = json.loads(msg)

                # --- מחיר אחרון ---
                if data.get("channel") == "push.ticker":
                    sym = data.get("symbol")
                    if sym and "data" in data:
                        last_price = float(data["data"].get("lastPrice", 0))
                        self.prices[sym] = last_price
                        logging.debug(f"💹 {sym} → {last_price}")

                # --- נרות (kline) ---
                elif data.get("channel") == "push.kline":
                    sym = data.get("symbol")
                    kline = data.get("data", {})
                    if sym and kline:
                        current_t = kline.get("t")   # timestamp של תחילת הנר (ms)
                        close_price = float(kline.get("c", 0))

                        # אם הגיע t חדש -> הנר הקודם נסגר
                        if sym in self.last_t and self.last_t[sym] != current_t:
                            prev_close = float(kline.get("rc", close_price))
                            self.closed_candles[sym] = prev_close
                            logging.info(f"🕯️ {sym} נר סגור → close={prev_close}")

                            # נר חדש התחיל → שמור זמן התחלה מקומי
                            self.candle_start[sym] = time.time()
                            logging.info(f"⏱️ {sym} נר חדש התחיל, מאפסים טיימר")

                        # עדכון ה־t האחרון
                        self.last_t[sym] = current_t

                        # אם עדיין אין candle_start, נתחיל אותו בפעם הראשונה
                        if sym not in self.candle_start:
                            self.candle_start[sym] = time.time()

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

    def get_last_closed_price(self, symbol: str) -> float | None:
        """מחזיר את מחיר הסגירה של הנר האחרון (אם קיים)"""
        return self.closed_candles.get(symbol)

    def get_candle_timing(self, symbol: str, interval_sec: int = 60) -> dict | None:
        """מחזיר מידע על זמן הנר הנוכחי"""
        if symbol not in self.candle_start:
            return None
        now = time.time()
        start = self.candle_start[symbol]
        elapsed = int(now - start)
        left = max(0, interval_sec - elapsed)
        return {"elapsed": elapsed, "left": left, "interval": interval_sec}

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
                btc_last = ws.get_price("BTC_USDT")
                btc_close = ws.get_last_closed_price("BTC_USDT")
                btc_timing = ws.get_candle_timing("BTC_USDT")

                print(f"BTC → last={btc_last} | closed={btc_close} | timing={btc_timing}")
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logging.info("🛑 הופסק ידנית")
            await ws.close()   # סגירה נקייה
            task.cancel()

    asyncio.run(main())
