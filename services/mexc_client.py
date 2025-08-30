import aiohttp, asyncio, json, time, hashlib, logging
from typing import Optional
from curl_cffi import requests as curlreq
from curl_cffi.const import CurlHttpVersion

class MexcClient:
    def __init__(self, api_key: str, base_url: str = "https://futures.mexc.com"):
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        """פתיחת Session אסינכרוני (למשאבים אחרים אם נדרש)"""
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=30, connect=5, sock_connect=10, sock_read=20)
            self._session = aiohttp.ClientSession(timeout=timeout, trust_env=True)

    async def close(self):
        """סגירת ה-Session"""
        if self._session:
            await self._session.close()
            self._session = None

    @staticmethod
    def _md5_hex(s: str) -> str:
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    def _sign(self, payload: dict) -> dict:
        """יצירת חתימה לבקשה"""
        date_now = str(int(time.time() * 1000))
        g = self._md5_hex(self.api_key + date_now)[7:]
        s = json.dumps(payload, separators=(",", ":"))
        sign = self._md5_hex(date_now + s + g)
        return {"time": date_now, "sign": sign}

    async def place_order(self, obj: dict) -> dict:
        """שליחת הזמנה ל-MEXC"""
        if not self.api_key:
            logging.warning("MEXC API key is empty; skipping order.")
            return {"error": "no_api_key"}

        assert self._session is not None, "MexcClient.start() was not called"
        url = f"{self.base_url}/api/v1/private/order/create"
        sig = self._sign(obj)
        headers = {
            "Content-Type": "application/json",
            "x-mxc-sign": sig["sign"],
            "x-mxc-nonce": sig["time"],
            "User-Agent": "FastVolBot/1.0",
            "Authorization": self.api_key
        }

        def _send_blocking():
            r = curlreq.post(url, headers=headers, json=obj, timeout=30,http_version=CurlHttpVersion.V1_1)
            try:
                return r.json()
            except Exception:
                return {"status": r.status_code, "text": r.text}

        retries, delay = 3, 0.7
        last_exc = None
        for i in range(retries):
            try:
                data = await asyncio.to_thread(_send_blocking)
                if isinstance(data, dict) and data.get("status") not in (None, 200):
                    logging.warning("MEXC HTTP (curl) %s: %s", data.get("status"), data)
                return data
            except Exception as e:
                last_exc = e
                logging.warning(
                    "MEXC post error (curl, try %s/%s): %s. Retrying in %.1fs...",
                    i + 1, retries, e, delay
                )
                await asyncio.sleep(delay)
                delay *= 1.7
        raise last_exc or RuntimeError("MEXC request failed")

    async def open_directional_order(self, trade_cfg: dict, last_price: float, first_price: float) -> dict:
        """
        פותח עסקה לפי כיוון המחיר:
        - אם last_price > first_price → Long (side=1)
        - אחרת → Short (side=3)
        """
        side = 1 if last_price > first_price else 3
        obj = {
            "symbol": trade_cfg["symbol"],          # למשל "SOL_USDT"
            "side": side,
            "openType": trade_cfg.get("openType", 1),
            "type": trade_cfg.get("type", 5),     # Market
            "vol": trade_cfg.get("vol", 1),
            "leverage": trade_cfg.get("leverage", 20),
            "priceProtect": trade_cfg.get("priceProtect", 0),
        }
        logging.info("📈 Directional order for %s → side=%s (last=%.4f vs first=%.4f)",
                     trade_cfg["symbol"], side, last_price, first_price)
        return await self.place_order(obj)
    

    async def close_position(self, trade_cfg: dict, last_price: float, first_price: float, vol: Optional[float] = None) -> dict:
        """
        סוגרת עסקה לפי אותו היגיון כיוון:
        - אם last_price > first_price → מניחה שהיה LONG → side=2 (סגירת לונג)
        - אחרת → מניחה שהיה SHORT → side=4 (סגירת שורט)

        vol:
        אם לא הועבר – יילקח מ-trade_cfg.get("vol", 1). אם תרצה לסגור את כל הפוזיציה,
        העבר כאן את ה-holdVol מה-API של MEXC.
        """
        side_close = 2 if last_price > first_price else 4
        obj = {
            "symbol": trade_cfg["symbol"],                 # לדוגמה: "SOL_USDT"
            "side": side_close,                            # 2=סגור LONG, 4=סגור SHORT
            "openType": trade_cfg.get("openType", 1),
            "type": trade_cfg.get("type", 5),            # Market
            "vol": (vol if vol is not None else trade_cfg.get("vol", 1)),
            "leverage": trade_cfg.get("leverage", 20),     # לא נדרש לסגירה אבל לא מזיק
            "priceProtect": trade_cfg.get("priceProtect", 0),
        }
        logging.info("🔻 Close order for %s → side=%s (last=%.4f vs first=%.4f)",
                    trade_cfg["symbol"], side_close, last_price, first_price)
        return await self.place_order(obj)

