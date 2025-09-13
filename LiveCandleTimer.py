# LiveCandleTimer.py
import asyncio
import aiohttp
import time

class LiveCandleTimer:
    def __init__(self, interval="Min1"):
        self.interval = interval
        self.interval_sec = self._interval_to_seconds(interval)

    def _interval_to_seconds(self, interval: str) -> int:
        mapping = {
            "Min1": 60,
            "Min5": 300,
            "Min15": 900,
            "Min30": 1800,
            "Hour1": 3600,
        }
        return mapping.get(interval, 60)

    async def get_last_candle(self, symbol="BTC_USDT"):
        url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}?interval={self.interval}&limit=1"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if isinstance(data, list) and len(data) > 0:
                    return data[0]  # לוקחים את הרשומה הראשונה
                return None

    def seconds_left(self, candle_open_time_ms: int) -> int:
        candle_open_sec = candle_open_time_ms // 1000
        candle_close_sec = candle_open_sec + self.interval_sec
        now = int(time.time())
        return max(0, candle_close_sec - now)


async def main():
    timer = LiveCandleTimer(interval="Min1")
    candle = await timer.get_last_candle("BTC_USDT")
    if candle:
        open_time = candle["time"]  # זמן פתיחת הנר במילישניות
        seconds_left = timer.seconds_left(open_time)
        print(f"⏳ Seconds left in current candle: {seconds_left}")
        print(f"📊 Candle data: {candle}")
    else:
        print("⚠️ Failed to fetch candle")

if __name__ == "__main__":
    asyncio.run(main())
