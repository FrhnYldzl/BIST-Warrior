# Meridian TR - Broker Selector
# BROKER env var ile seçim yap: "ibkr" (Interactive Brokers) veya "yfinance" (paper simulator)

import os
import logging

BROKER_TYPE = os.getenv("BROKER", "yfinance").lower()

log = logging.getLogger("broker")

if BROKER_TYPE == "ibkr":
    try:
        from .ibkr import IBKRBroker as EquityBroker
        log.info("✅ Broker: Interactive Brokers (IBKR) — IB Gateway üzerinden")
    except ImportError as e:
        log.warning(f"⚠️  ib_insync import başarısız ({e}) — yfinance paper'a düşüyorum")
        from .equity import EquityBroker
else:
    from .equity import EquityBroker
    log.info("✅ Broker: yfinance paper simulator (local SQLite)")

__all__ = ["EquityBroker"]
