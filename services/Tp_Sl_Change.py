import aiohttp, asyncio, json, time, hashlib, logging
from typing import Optional
from curl_cffi import requests as curlreq
from curl_cffi.const import CurlHttpVersion


class MexcTPClient:
    def __init__(self, api_key: str, base_url: str = "https://futures.mexc.com"):
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        """פתיחת Session אסינכרוני"""
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=30, connect=5, sock_connect=10, sock_read=20)
            self._session = aiohttp.ClientSession(timeout=timeout, trust_env=True)
            logging.info("🌐 MexcTPClient Session started")

    async def close(self):
        """סגירת ה-Session"""
        if self._session:
            await self._session.close()
            self._session = None
            logging.info("🔌 MexcTPClient Session closed")

    # -------- Helpers --------
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

    async def _send_request(self, url: str, obj: dict) -> dict:
        """שליחת בקשה עם חתימה + לוגים מפורטים"""
        sig = self._sign(obj)
        headers = {
            "Content-Type": "application/json",
            "x-mxc-sign": sig["sign"],
            "x-mxc-nonce": sig["time"],
            "User-Agent": "FastVolBot/1.0",
            "Authorization": self.api_key
        }

        logging.debug(f"➡️ שולח בקשה ל־MEXC → {url}")
        logging.debug(f"📤 גוף הבקשה: {obj}")
        logging.debug(f"📤 Headers: {headers}")

        def _send_blocking():
            try:
                r = curlreq.post(
                    url,
                    headers=headers,
                    json=obj,
                    timeout=30,
                    http_version=CurlHttpVersion.V1_1
                )
                try:
                    return r.json()
                except Exception:
                    return {"status": r.status_code, "text": r.text}
            except Exception as e:
                logging.error(f"❌ שגיאה בשליחת בקשה ל־MEXC: {e}", exc_info=True)
                return {"error": str(e)}

        result = await asyncio.to_thread(_send_blocking)
        logging.debug(f"⬅️ תגובת MEXC: {result}")
        return result

    # -------- API: Update TP/SL --------
    async def update_tp_sl(self, stop_plan_order_id: int,
                           tp: float = None, sl: float = None) -> dict:
        """
        עדכון TP/SL להזמנה קיימת.
        :param stop_plan_order_id: stopPlanOrderId (מהפונקציה get_stop_orders)
        :param tp: מחיר TP חדש
        :param sl: מחיר SL חדש
        """
        if not self.api_key:
            logging.warning("⚠️ MEXC API key is empty; skipping update_tp_sl.")
            return {"error": "no_api_key"}

        assert self._session is not None, "❌ MexcTPClient.start() was not called"

        url = f"{self.base_url}/api/v1/private/stoporder/change_plan_price"
        obj = {"stopPlanOrderId": int(stop_plan_order_id)}

        if tp is not None:
            obj["takeProfitPrice"] = round(float(tp), 3)
        if sl is not None:
            obj["stopLossPrice"] = round(float(sl), 3)

        logging.info(f"🛠️ עדכון TP/SL עבור stopPlanOrderId={stop_plan_order_id}, tp={tp}, sl={sl}")
        return await self._send_request(url, obj)


# ==================== שימוש לדוגמה ====================
async def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    client = MexcTPClient(api_key="WEB7d92dd938df5fdc7718ed07373882d789094923bd2fa8947b4605a61f3278478")
    await client.start()

    # תמיד עם stopPlanOrderId אמיתי
    resp = await client.update_tp_sl(stop_plan_order_id=354649963, tp=122000.7, sl=110000.2)

    logging.info(f"✅ Update Response: {resp}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
