import time, hmac, hashlib, urllib.parse, logging, asyncio, aiohttp
from typing import Optional, Dict, Any
from decimal import Decimal, ROUND_HALF_UP
from services.rate_limiter import RateLimiter  # <-- חדש


class MexcAPI:
    def __init__(self, api_key: str, secret_key: str, base_url: str = "https://contract.mexc.com",
                 limiter: Optional[RateLimiter] = None, max_concurrency: int = 8,
                 ticker_ttl: float = 0.8, candle_ttl: float = 1.5):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.session: Optional[aiohttp.ClientSession] = None
        self.rate_limit_wait = 1

        # caches
        self._contracts_cache = {}
        # {symbol: (price, ts)}
        self._prices_cache: Dict[str, Tuple[float, float]] = {}
        # {(symbol, interval): (candle_dict, ts)}
        self._candle_cache: Dict[Tuple[str, str], Tuple[dict, float]] = {}
        self._balance_cache = 0.0

        # rate limiting
        self._limiter = limiter or RateLimiter(rate=10.0, capacity=10)
        self._sem = asyncio.Semaphore(max_concurrency)
        self._ticker_ttl = float(ticker_ttl)
        self._candle_ttl = float(candle_ttl)

    def attach_rate_limiter(self, limiter: RateLimiter, max_concurrency: int = 8):
        """מאפשר לחבר לימיטר מבחוץ, אם רוצים ליצור אותו לפי config."""
        self._limiter = limiter
        self._sem = asyncio.Semaphore(max_concurrency)

    def _guess_weight(self, path: str) -> int:
        # משקל שמרני: public 1-2, private 2-3
        if "/contract/ticker" in path:
            return 1
        if "/contract/kline" in path:
            return 2
        if "/private/" in path:
            return 2
        return 1

    async def start_session(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=10)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def _send_request(self, method: str, path: str, params: dict = None, signed: bool = True,
                            max_retries: int = 5, weight: Optional[int] = None) -> dict:
        """פונקציה מרכזית לשליחת בקשות ל-MEXC עם ניהול Rate Limit, חתימה ושגיאות"""
        await self.start_session()
        params = params or {}

        # --- Rate limit (tokens + concurrency) ---
        w = weight if weight is not None else self._guess_weight(path)
        await self._limiter.acquire(w)
        async with self._sem:
            # --- חתימה אם נדרש ---
            if signed:
                timestamp = str(int(time.time() * 1000))
                sorted_params = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in sorted(params.items()))
                to_sign = self.api_key + timestamp + sorted_params
                signature = hmac.new(self.secret_key.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
                headers = {
                    "ApiKey": self.api_key,
                    "Request-Time": timestamp,
                    "Signature": signature,
                    "Content-Type": "application/json"
                }
                url = f"{self.base_url}{path}?{sorted_params}" if sorted_params else f"{self.base_url}{path}"
            else:
                headers = {"Content-Type": "application/json"}
                url = f"{self.base_url}{path}"
                if params:
                    qs = urllib.parse.urlencode(params)
                    url += f"?{qs}"

            logging.debug(f"🌐 שולח בקשה → {method} {url} | params={params}")

            # --- לולאת נסיונות ---
            for attempt in range(1, max_retries + 1):
                try:
                    async with self.session.request(method, url, headers=headers) as resp:
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:
                            text = await resp.text()
                            logging.error(f"❌ לא ניתן לפענח JSON ({resp.status}): {text}")
                            return {"success": False, "msg": "Invalid JSON response"}

                        if resp.status == 429:
                            # backup מקומי (נשמר למורשת) – בפועל ה-RateLimiter מונע להגיע לזה
                            wait_time = min(self.rate_limit_wait * 2, 10)
                            logging.warning(f"🚨 Rate Limit! ניסיון {attempt}/{max_retries}, מחכה {wait_time}s...")
                            self.rate_limit_wait = wait_time
                            await asyncio.sleep(wait_time)
                            continue

                        if resp.status == 200 and data.get("success", True):
                            self.rate_limit_wait = 1
                            return data

                        logging.warning(f"⚠️ API Error {resp.status}: {data}")
                        return data
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logging.error(f"❌ שגיאת רשת (ניסיון {attempt}/{max_retries}): {e}")
                    await asyncio.sleep(2)

            logging.critical("❌ כל הניסיונות נכשלו – לא ניתן להתחבר ל-API")
            return {"success": False, "msg": "API connection failed"}
    # ==================== פונקציות API ====================

    async def get_open_positions(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        params = {}
        if symbol:
            params["symbol"] = self.normalize_symbol(symbol)
        return await self._send_request("GET", "/api/v1/private/position/open_positions", params, signed=True)

    async def has_open_position(self, symbol: str) -> bool:
        if "_" not in symbol and symbol.endswith("USDT"):
            symbol = symbol.replace("USDT", "_USDT")
        data = await self.get_open_positions(symbol)
        if not data.get("success"):
            return False
        positions = data.get("data", [])
        return any(pos.get("holdVol", 0) > 0 and pos.get("state", 0) == 1 for pos in positions)

    def normalize_symbol(self, symbol: str) -> str:
        return symbol.replace("USDT", "_USDT") if "_" not in symbol and symbol.endswith("USDT") else symbol

    
    async def get_current_price(self, symbol: str) -> Optional[float]:
        now = time.monotonic()
        cached = self._prices_cache.get(symbol)
        if cached and (now - cached[1]) < self._ticker_ttl:
            return cached[0]

        data = await self._send_request("GET", "/api/v1/contract/ticker",
                                        {"symbol": symbol}, signed=False, weight=1)
        try:
            price = float(data["data"]["lastPrice"])
            self._prices_cache[symbol] = (price, now)
            return price
        except Exception:
            return None

    async def get_unrealized_pnl(self, symbol: str) -> Optional[float]:
        symbol = self.normalize_symbol(symbol)
        pos_data = await self.get_open_positions(symbol)
        if not pos_data.get("success") or not pos_data.get("data"):
            return None
        position = pos_data["data"][0]
        entry = float(position["holdAvgPrice"])
        side = position["positionType"]  # 1=LONG, 2=SHORT
        lev = float(position.get("leverage", 1))
        current_price = await self.get_current_price(symbol)
        if not current_price:
            return None
        return ((current_price - entry) / entry if side == 1 else (entry - current_price) / entry) * lev * 100

    async def get_account_assets(self) -> dict:
        return await self._send_request("GET", "/api/v1/private/account/assets", {}, signed=True)

    async def get_usdt_balance(self) -> float:
        data = await self.get_account_assets()
        if not data or not data.get("data"):
            return 0.0
        for asset in data["data"]:
            if asset.get("currency") == "USDT":
                return float(asset.get("availableBalance", 0))
        return 0.0

    async def get_contract_specs(self, symbol: str) -> dict:
        return await self._send_request("GET", "/api/v1/contract/detail", {"symbol": symbol}, signed=False)

    async def calc_order_volume(self, symbol: str, percent: float, leverage: int) -> int:
        specs = await self.get_contract_specs(symbol)
        if not specs or not specs.get("data"):
            raise ValueError(f"❌ לא נמצאו נתוני חוזה עבור {symbol}")

        specs_data = specs["data"][0] if isinstance(specs["data"], list) else specs["data"]
        contract_size = float(specs_data["contractSize"])
        min_vol = int(specs_data["minVol"])

        balance = await self.get_usdt_balance()
        if balance <= 0:
            raise ValueError("❌ אין יתרה זמינה ב-USDT")

        price = await self.get_current_price(symbol)
        if not price:
            raise ValueError(f"❌ מחיר לא תקין ל-{symbol}")

        margin_amount = balance * (percent / 100.0)
        raw_contracts_value = margin_amount * leverage
        raw_vol = raw_contracts_value / (price * contract_size)
        vol = int(Decimal(str(raw_vol)).to_integral_value(rounding=ROUND_HALF_UP))

        cost_for_min_vol = (price * contract_size) / leverage
        if vol < min_vol:
            vol = min_vol if margin_amount >= cost_for_min_vol else 0

        logging.info(f"💰 Balance={balance:.4f} | %={percent}% | Lev={leverage}x | Price={price} "
                     f"| ContractSize={contract_size} | RawVol={raw_vol:.8f} → Vol={vol}")
        return vol

    async def get_last_closed_candle(self, symbol: str, interval: str = "Min1") -> Optional[dict]:
        now = time.monotonic()
        key = (symbol, interval)
        cached = self._candle_cache.get(key)
        if cached and (now - cached[1]) < self._candle_ttl:
            return cached[0]

        data = await self._send_request("GET", f"/api/v1/contract/kline/{symbol}",
                                        {"interval": interval, "limit": 2}, signed=False, weight=2)
        try:
            d = data["data"]
            candle = {
                "time": d["time"][-2],
                "open": float(d["open"][-2]),
                "close": float(d["close"][-2]),
                "high": float(d["high"][-2]),
                "low": float(d["low"][-2]),
            }
            self._candle_cache[key] = (candle, now)
            return candle
        except Exception:
            return None

    async def can_open_trade(self, symbol: str, side: int, interval: str = "Min1") -> bool:
        candle = await self.get_last_closed_candle(symbol, interval)
        if not candle:
            return False
        close_price = candle["close"]
        last_price = await self.get_current_price(symbol)
        if not last_price:
            return False
        tolerance = close_price * 0.0005
        if side == 1 and last_price >= close_price - tolerance:
            return True
        elif side == 3 and last_price <= close_price + tolerance:
            return True
        return False

# ==================== MAIN TEST ====================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    api = MexcAPI("MEXC_API_KEY", "MEXC_SECRET_KEY")

    # # נבדוק נר אחרון של BTC ו-SOL
    # for sym in ["BTC_USDT", "SOL_USDT"]:
    #     candle = api.get_last_closed_candle(sym, interval="Min1")
    #     if candle:
    #         print(f"🕯️ נר אחרון עבור {sym}: {candle}")
    #     else:
    #         print(f"❌ לא התקבל נר עבור {sym}")

    print(api.get_current_price("BTC_USDT"))
    print(api.get_last_closed_candle("BTC_USDT"))


# ==================== MAIN TEST ====================

# if __name__ == "__main__":
#     logging.basicConfig(level=logging.INFO)

#     api = MexcAPI("mx0vglLPkGS8iQAnTV", "20ffb7c8ea614faf95cf40f631b3f249")

#     percent = 20
#     leverage = 20

#     try:
#         vol_btc = api.calc_order_volume("SOL_USDT", percent, leverage)
#         print(f"➡️  Vol ({percent}% @ {leverage}x):", vol_btc)


#     except Exception as e:
#         print("❌ שגיאה בחישוב:", e)
