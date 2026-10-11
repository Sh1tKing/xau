"""Explicit offline placeholders. Never read actual .env or initialize a broker."""
import os
for key in list(os.environ):
    if key.startswith(("FIB_PA_", "MT5_", "AI1_", "AI2_", "AI3_", "GPTSTORE_", "FEISHU_", "CRYPTO_")):
        os.environ.pop(key, None)
os.environ.update(CRYPTO_RUN_AUTHORIZED="1", CRYPTO_MODE="dry-run", CRYPTO_ASSET="BTC",
                  MT5_LOGIN="1", MT5_PASSWORD="offline-test", MT5_SERVER="offline-test",
                  FIB_PA_SYMBOL="BTCUSD.mock", AI1_KEY="offline-test", AI2_KEY="offline-test",
                  AI3_KEY="offline-test", FEISHU_NOTIFY_ENABLED="false")
