# rate_limiter.py
import asyncio, time

class RateLimiter:
    """
    Token-bucket פשוט:
    - rate: כמה אסימונים נוספים בכל שנייה
    - capacity: מקסימום אסימונים (burst)
    acquire(weight): ממתין עד שיש מספיק אסימונים, ואז "צורך" אותם.
    """
    def __init__(self, rate: float = 10.0, capacity: int = 10):
        self.rate = float(rate)
        self.capacity = int(capacity)
        self._tokens = float(capacity)
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int = 1):
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated_at
                if elapsed > 0:
                    self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                    self._updated_at = now

                if self._tokens >= weight:
                    self._tokens -= weight
                    return

                need = max(0.0, weight - self._tokens)
                wait_for = max(need / self.rate, 0.01)
            # נשחרר את ה-lock לפני השינה כדי לאפשר לאחרים להתקדם
            await asyncio.sleep(wait_for)
