import hashlib
import json
import time
from curl_cffi import requests
import os
from dotenv import load_dotenv

# ===== עזר: הדפסת מפתח מוחשך =====
def _mask(s: str, show=4) -> str:
    if not s:
        return "<EMPTY>"
    if len(s) <= show:
        return "*" * len(s)
    return "*" * (len(s) - show) + s[-show:]

# טוען .env מתוך התיקייה הנוכחית של הסקריפט
load_dotenv()

print("📂 CWD:", os.getcwd())
print("📄 .env expected in:", os.path.dirname(__file__))

# שליפת המפתח מה־.env
MEXC_API_KEY_WEB = os.getenv("MEXC_API_KEY_WEB", "").strip()
print("🔑 MEXC_API_KEY_WEB (masked):", _mask(MEXC_API_KEY_WEB))

def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def _sign(key: str, obj: dict) -> dict:
    """יוצר חתימה לבקשה"""
    date_now = str(int(time.time() * 1000))
    g = _md5(key + date_now)[7:]
    s = json.dumps(obj, separators=(',', ':'))
    sign = _md5(date_now + s + g)
    print("📝 Signature Debug:")
    print("   ⏱ time =", date_now)
    print("   🔑 g =", g)
    print("   📦 obj (for sign) =", s)
    print("   ✍ sign =", sign)
    return {"time": date_now, "sign": sign}

def place_order(obj: dict) -> dict:
    """שליחת הזמנה ל-MEXC (פתיחה או סגירה)"""
    if not MEXC_API_KEY_WEB:
        print("❌ Missing MEXC_API_KEY_WEB in .env (or empty)")
        return {"error": "Missing MEXC_API_KEY_WEB in .env"}

    signature = _sign(MEXC_API_KEY_WEB, obj)
    headers = {
        "Content-Type": "application/json",
        "x-mxc-sign": signature["sign"],
        "x-mxc-nonce": signature["time"],
        "User-Agent": "FastVolBot/1.0",
        "Authorization": MEXC_API_KEY_WEB
    }

    url = "https://futures.mexc.com/api/v1/private/order/create"

    print("➡️ POST", url)
    print("🧾 Headers:", {k: (v if k != "Authorization" else _mask(v)) for k,v in headers.items()})
    print("📦 Payload (raw):", obj)

    try:
        r = requests.post(url, headers=headers, json=obj, timeout=30)
        print("📥 Raw response:", r.status_code, r.text)
        try:
            return r.json()
        except Exception:
            return {"status": r.status_code, "text": r.text}
    except Exception as e:
        print("❌ Request failed:", e)
        return {"error": str(e)}

if __name__ == "__main__":
    # דוגמה לבדיקה (לא פותח עסקה אמיתית אלא רק בודק מבנה):
    test_payload = {
        "symbol": "BTC_USDT",
        "side": 1,
        "openType": 1,
        "type": "5",
        "vol": 1,
        "leverage": 20,
        "priceProtect": "0"
    }
    print("=== בדיקת place_order עם payload לדוגמה ===")
    resp = place_order(test_payload)
    print("📊 Final parsed response:", resp)
