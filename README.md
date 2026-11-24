# SuperTrend Autonomous Crypto Scalping Bot

Automated Binance USDT-M Futures trading system based on Supertrend trend-following strategy across 26 major perpetual symbols on 15-minute timeframe.

## Features
- **Autonomous Execution:** Listens to real-time Supertrend scanner signals, placing market entries with structured risk limits ($20 margin @ 10x / $200 notional).
- **Automated Watchdog (TP/SL):** Manages open positions, trailing stops, and market-close execution cleanly.
- **Robust Error Handling:** Prevents duplicate orders, guards against immediate trigger errors (`-2021`), and enforces strict ownership checks (`stauto-*`).
- **Honest Backtest Baseline:** Validated across 26 symbols with un-optimized raw performance (`428 trades`, `+99.19% return`, `60.51% WR`, `1.27 PF`).

## Architecture & Scripts
- `supertrend_bb_crypto_scanner.py`: Core scanner evaluating 26 symbols on 15m timeframe.
- `binance_auto_executor.py`: Autonomous trade execution daemon.
- `binance_sl_watchdog.py`: Risk management watchdog handling trailing stops and profit targets.
- `binance_auto_ownership.py`: Position reconciliation and ownership verification.

## Setup & Systemd Integration
Managed via persistent user systemd services with auto-restart enabled.
