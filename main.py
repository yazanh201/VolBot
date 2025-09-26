import asyncio
import logging
import os
from dotenv import load_dotenv

import core.fast_volatility_alert as fast_volatility_alert
from cashe.cache_manager import CacheManager
from services.mexc_api import MexcAPI
from services.mexc_ws import MexcWebSocket
from services.mexc_client import MexcClient   # 👈 ייבוא MexcClient


async def preload_cache(cache: CacheManager, symbols: list[str]):
    for sym in symbols:
        await cache.get_contract_specs(sym)
    await cache.get_balance()


async def refresh_balance_periodically(cache: CacheManager, interval: int = 60):
    while True:
        await cache.get_balance()
        await asyncio.sleep(interval)


async def main():
    logging.basicConfig(level=logging.INFO)
    load_dotenv()

    # --- יצירת API ו־WS ---
    mexc_api = MexcAPI(os.getenv("MEXC_API_KEY_WEB2", ""), os.getenv("MEXC_API_SECRET_WEB", ""))
    ws = MexcWebSocket(["BTC_USDT", "SOL_USDT"])

    # --- יצירת MexcClient (חדש) ---
    mexc_client = MexcClient(api_key=os.getenv("MEXC_API_KEY_WEB", ""))
    await mexc_client.start()

    # --- יצירת CacheManager ---
    cache = CacheManager(mexc_api, ws)

    # --- הפעלת WebSocket ברקע ---
    asyncio.create_task(ws.run())

    # --- טעינה מוקדמת של cache ---
    await preload_cache(cache, ["BTC_USDT", "SOL_USDT"])

    # --- עדכון balance כל דקה ---
    asyncio.create_task(refresh_balance_periodically(cache))

    # --- הפעלת הבוט הראשי ---
    await fast_volatility_alert.run(
        "config.yaml",
        cache=cache,
        mexc_client=mexc_client   # 👈 חשוב: שולחים ל־fast_volatility_alert
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bye 👋")
