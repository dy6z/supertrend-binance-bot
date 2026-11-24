# SuperTrend Autonomous Crypto Scalping Bot

Automated Binance USDT-M Futures trading system based on Supertrend trend-following strategy across 26 major perpetual symbols on the 15-minute timeframe.

## 📊 Backtest Baseline (30 Days, 26 Symbols)
- **Total Trades:** 428
- **Total Return:** +99.19%
- **Win Rate:** 60.51%
- **Profit Factor (PF):** 1.27
- **Methodology:** Unbiased raw Supertrend performance with standard TP/SL management (No over-fitted filters).

---

## 📂 File Structure
```text
~/.hermes/scripts/
├── supertrend_bb_crypto_scanner.py   # Core market scanner (26 symbols, 15m TF, Supertrend + BB + RSI)
├── binance_auto_executor.py          # Autonomous order execution daemon ($20 margin @ 10x)
├── binance_sl_watchdog.py            # Risk management watchdog (Trailing stops, hard SL -3%, TP +1.8%)
├── binance_auto_ownership.py         # Position ownership & trade reconciliation validator
└── README.md                         # Documentation
```

---

## 🧠 Core Logic & Strategy
1. **Trend & Retest Gate:** Evaluates 15-minute candlestick data across 26 configured perpetual symbols.
2. **Filters:**
   - Supertrend distance >= 2.0% from price.
   - RSI neutral-zone filter `[35, 65]` (avoids extreme overbought/oversold whipsaws).
   - Bollinger Bands (BB mid cross confirmation).
3. **Execution & Risk Management:**
   - Market order entry with predetermined position sizing ($20 margin, 10x leverage = $200 notional).
   - Target Take Profit: **+1.8%**.
   - Stop Loss: **-3.0%** hard stop / trailing Supertrend.
   - Watchdog safely handles order status checks, preventing duplicate triggers and suppressing immediate-trigger `-2021` error spam.

---

## 🚀 Quick Start

### 1. Prerequisites
- Python 3.10+ with `pandas`, `ccxt`, and required scientific libraries.
- Binance Futures API credentials configured securely in:
  `~/.agent/credentials/binance.env`

### 2. Run Scanner Manually
```bash
python3 ~/.hermes/scripts/supertrend_bb_crypto_scanner.py
```

### 3. Run Executor
```bash
python3 ~/.hermes/scripts/binance_auto_executor.py
```

### 4. Run Watchdog
```bash
python3 ~/.hermes/scripts/binance_sl_watchdog.py
```

---

## ⚙️ Systemd & Auto-Start
All daemons run persistently via user `systemd` services with auto-restart enabled:
- `binance-auto-executor.service`
- `binance-sl-watchdog.service`
- `supertrend-bb-loop.service`
