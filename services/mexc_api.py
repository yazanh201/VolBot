import time, hmac, hashlib, urllib.parse, logging, asyncio, aiohttp
from typing import Optional, Dict, Any
from decimal import Decimal, ROUND_HALF_UP
from services.rate_limiter import RateLimiter  # <-- חדש
import json
import datetime
from typing import List



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
        self._prices_cache: Dict[str, Tuple[float, float]] = {}
        self._candle_cache: Dict[Tuple[str, str], Tuple[dict, float]] = {}
        self._balance_cache = 0.0

        # rate limiting
        self._limiter = limiter or RateLimiter(rate=10.0, capacity=10)
        self._sem = asyncio.Semaphore(max_concurrency)
        self._ticker_ttl = float(ticker_ttl)
        self._candle_ttl = float(candle_ttl)

    async def start_session(self):
        """יוצר את ה-ClientSession בתוך event loop רץ"""
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=10)
            try:
                connector = aiohttp.TCPConnector(
                    force_close=True,
                    http_versions=[aiohttp.HttpVersion11]  # יעבוד בגרסאות חדשות
                )
            except TypeError:
                connector = aiohttp.TCPConnector(force_close=True)  # fallback לישן

            self.session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector
            )

    async def close_session(self):
        """סוגר את ה-ClientSession"""
        if self.session:
            await self.session.close()
            self.session = None

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
    
    def _sign_body(self, body: dict) -> dict:
        """
        יוצר חתימה מיוחדת לבקשות POST עם body (כמו stoporder/change_price)
        """
        date_now = str(int(time.time() * 1000))
        raw = json.dumps(body, separators=(",", ":"))
        g = hashlib.md5((self.api_key + date_now).encode()).hexdigest()[7:]
        sign = hashlib.md5((date_now + raw + g).encode()).hexdigest()
        return {"time": date_now, "sign": sign}

    async def update_order_tp_sl(self, order_id: int, tp: float = None, sl: float = None) -> dict:
        """
        מעדכן TP/SL להזמנה קיימת (Stop-Limit order).
        :param order_id: מזהה ההזמנה (orderId) שהוחזר בפתיחה
        :param tp: מחיר TP חדש (או None אם לא רוצים לשנות)
        :param sl: מחיר SL חדש (או None אם לא רוצים לשנות)
        """
        await self.start_session()
        url = f"{self.base_url}/api/v1/private/stoporder/change_price"

        payload = {"orderId": order_id}
        if tp is not None:
            payload["takeProfitPrice"] = round(float(tp), 3)
        if sl is not None:
            payload["stopLossPrice"] = round(float(sl), 3)

        sig = self._sign_body(payload)
        headers = {
            "Content-Type": "application/json",
            "x-mxc-sign": sig["sign"],
            "x-mxc-nonce": sig["time"],
            "Authorization": self.api_key
        }

        async with self.session.post(url, json=payload, headers=headers) as r:
            try:
                return await r.json()
            except Exception:
                return {"status": r.status, "text": await r.text()}

    async def get_stop_orders(
        self, 
        symbol: str = "", 
        is_finished: int = 0, 
        page_num: int = 1, 
        page_size: int = 20
    ) -> dict:
        """
        מחזירה את רשימת ה-Stop Orders (TP/SL).
        :param symbol: סימבול (למשל "BTC_USDT") או ריק = כל הסימבולים
        :param is_finished: 0 = פעילים, 1 = היסטוריים
        :param page_num: מספר עמוד (ברירת מחדל 1)
        :param page_size: מספר תוצאות לעמוד (ברירת מחדל 20, מקסימום 100)
        """
        path = "/api/v1/private/stoporder/list/orders"
        params = {
            "symbol": symbol,
            "is_finished": is_finished,
            "page_num": page_num,
            "page_size": page_size
        }
        return await self._send_request("GET", path, params, signed=True)


    async def get_recent_candles(self, symbol: str, interval: str = "Min1", limit: int = 30) -> Optional[List[dict]]:
        """
        מחזירה את X הנרות האחרונים של symbol נתון.
        :param symbol: סימבול (למשל "BTC_USDT")
        :param interval: טווח הנר (Min1, Min5, Min15, Day1 וכו')
        :param limit: כמה נרות להביא (מקסימום 2000 לפי ה-API)
        :return: רשימת נרות בפורמט dict
        """
        try:
            data = await self._send_request(
                "GET",
                f"/api/v1/contract/kline/{symbol}",
                {"interval": interval, "limit": limit},
                signed=False,
                weight=2
            )

            d = data["data"]
            candles = []
            for i in range(len(d["time"])):
                candles.append({
                    "time": d["time"][i],
                    "open": float(d["open"][i]),
                    "close": float(d["close"][i]),
                    "high": float(d["high"][i]),
                    "low": float(d["low"][i]),
                    "vol": float(d["vol"][i])
                })

            return candles

        except Exception as e:
            logging.error(f"❌ שגיאה בשליפת {limit} נרות אחרונים עבור {symbol}: {e}")
            return None




    async def get_candles_with_live(self, symbol: str, interval: str = "Min1", limit: int = 20):
        """
        מחזירה גם את 30 הנרות האחרונים הסגורים,
        גם את הנר הסגור האחרון,
        וגם את הנר החי (שעדיין רץ).
        """
        try:
            # נבקש limit+1 כדי לכלול גם את הנר החי
            data = await self._send_request(
                "GET",
                f"/api/v1/contract/kline/{symbol}",
                {"interval": interval, "limit": limit + 1},
                signed=False,
                weight=2
            )

            d = data["data"]
            candles = []
            for i in range(len(d["time"])):
                candles.append({
                    "time": d["time"][i],
                    "open": float(d["open"][i]),
                    "close": float(d["close"][i]),
                    "high": float(d["high"][i]),
                    "low": float(d["low"][i]),
                    "vol": float(d["vol"][i])
                })

            if len(candles) < 2:
                return None, None, None

            # נר סגור אחרון (לפני החי)
            last_closed = candles[-2]
            # נר חי (עדיין רץ)
            live = candles[-1]
            # 30 נרות סגורים אחרונים
            closed_candles = candles[:-1]

            return closed_candles, last_closed, live

        except Exception as e:
            logging.error(f"❌ שגיאה בשליפת נרות עבור {symbol}: {e}")
            return None, None, None


    async def get_fair_price(self, symbol: str) -> Optional[float]:
        """מחזיר את ה-Fair Price של החוזה"""
        symbol = self.normalize_symbol(symbol)
        data = await self._send_request("GET", f"/api/v1/contract/fair_price/{symbol}", signed=False, weight=1)
        try:
            return float(data["data"]["fairPrice"])
        except Exception:
            logging.error(f"❌ שגיאה בשליפת fair price: {data}")
            return None


    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        """מחזיר את ה-Funding Rate הנוכחי"""
        symbol = self.normalize_symbol(symbol)
        data = await self._send_request("GET", f"/api/v1/contract/funding_rate/{symbol}", signed=False, weight=1)
        try:
            return float(data["data"]["fundingRate"])
        except Exception:
            logging.error(f"❌ שגיאה בשליפת funding rate: {data}")
            return None

    async def get_order_book(self, symbol: str, limit: int = 20) -> Optional[dict]:
        """מחזיר את עומק הספר (Order Book)"""
        symbol = self.normalize_symbol(symbol)
        data = await self._send_request("GET", f"/api/v1/contract/depth/{symbol}",
                                        {"limit": limit}, signed=False, weight=1)
        try:
            ob = data["data"]  # ⬅️ ניגש ל־data["data"]
            return {
                "asks": ob["asks"],
                "bids": ob["bids"],
                "timestamp": ob.get("timestamp")
            }
        except Exception:
            logging.error(f"❌ שגיאה בשליפת order book: {data}")
            return None



# ==================== דוגמה לשימוש ====================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 🔑 הכנס את ה-API KEY וה-SECRET שלך
    api = MexcAPI("mx0vglUEoSmb5QzewG", "2d0a8e11f7b94ea689c07eddb0a29668")

    async def main():
        await api.start_session()
        try:
            symbol = "BTC_USDT"

            # --- בדיקת Fair Price ---
            fair = await api.get_fair_price(symbol)
            print(f"📈 Fair Price for {symbol}: {fair}")

            # --- בדיקת Funding Rate ---
            funding = await api.get_funding_rate(symbol)
            print(f"💸 Funding Rate for {symbol}: {funding}")

            # --- בדיקת Order Book ---
            order_book = await api.get_order_book(symbol, limit=5)
            if order_book:
                print(f"📊 Order Book for {symbol}:")
                print("Bids:", order_book["bids"][:3])  # רק שלוש הראשונות
                print("Asks:", order_book["asks"][:3])
                print("Timestamp:", order_book["timestamp"])
            else:
                print("❌ לא התקבל Order Book")

        finally:
            await api.close_session()

    asyncio.run(main())
