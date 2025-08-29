import asyncio
import logging
import core.fast_volatility_alert as fast_volatility_alert

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(fast_volatility_alert.run("config.yaml"))
    except KeyboardInterrupt:
        print("Bye 👋")
