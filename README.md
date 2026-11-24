# SuperTrend Autonomous Crypto Scalping Bot

Automated Binance USDT-M Futures trading system based on Supertrend trend-following strategy across 26 major perpetual symbols on the 15-minute timeframe.

## 📊 Backtest Baseline (30 Days, 26 Symbols)
- **Total Trades:** 428
- **Total Return:** +99.19%
- **Win Rate:** 60.51%
- **Profit Factor (PF):** 1.27
- **Methodology:** Unbiased raw Supertrend performance with standard TP/SL management (No over-fitted filters).

---

## 📂 File Structure & Detailed Component Responsibilities
```text
~/.hermes/scripts/
├── supertrend_bb_crypto_scanner.py      # Core market scanner (26 symbols, 15m TF, Supertrend + BB + RSI)
├── binance_auto_executor.py             # Autonomous order execution daemon ($20 margin @ 10x)
├── binance_auto_executor_loop.sh        # Shell loop wrapper running executor every 30 seconds
├── binance_sl_watchdog.py               # Risk management watchdog (Trailing stops, hard SL -3%, TP +1.8%)
├── binance_profit_lock_watchdog.py      # Profit lock & reconciliation watchdog (Sends exact PnL % & USD alerts)
├── binance_auto_ownership.py            # Position ownership & trade reconciliation validator
└── README.md                            # Documentation
```

### Detailed File Functions:
1. **`supertrend_bb_crypto_scanner.py`**
   - Fetches 15m candlestick data for 26 symbols from Binance Futures.
   - Computes Supertrend lines, Bollinger Band mid-cross validation, and RSI neutral zone filter `[35, 65]`.
   - Generates active signals and logs them to `supertrend_bb_signals.log`.

2. **`binance_auto_executor.py`**
   - Reads scanner signals and autonomously opens market positions on Binance Futures.
   - Enforces strict risk sizing ($20 margin @ 10x leverage = $200 notional).
   - Prevents duplicate orders and handles entry identity verification.

3. **`binance_auto_executor_loop.sh`**
   - Shell loop wrapper that runs `binance_auto_executor.py` continuously at 30-second intervals.

4. **`binance_sl_watchdog.py`**
   - Monitors open positions and manages Stop Loss orders in real-time.
   - Implements trailing Supertrend stops and hard Stop Loss (`-3%`).
   - Suppresses API error spam (such as `-2021` immediate trigger errors).

5. **`binance_profit_lock_watchdog.py`**
   - Tracks open and closed positions to provide 100% synchronized trade closure alerts.
   - Fetches actual execution trades (`userTrades`) from Binance with retry logic.
   - Computes exact weighted exit price, percentage return, and realized USD profit/loss.
   - Sends formatted WhatsApp alerts (`🎯 TAKE PROFIT HIT` / `🛑 STOP LOSS HIT`) with exact PnL figures.

6. **`binance_auto_ownership.py`**
   - Validates position ownership and ensures all trades align with authorized strategy parameters before letting watchdogs touch them.

---

## 🚀 Quick Start

### 1. Prerequisites & Credentials
- Python 3.10+ with `pandas`, `ccxt`, and required scientific libraries.
- Create your Binance API credential file at `~/.agent/credentials/binance.env`:
  ```env
  BINANCE_API_KEY=your_binance_api_key_here
  BINANCE_API_SECRET=your_binance_api_secret_here
  WHATSAPP_HOME_CHAT_ID=your_whatsapp_chat_id_here
  ```

### 2. Manual Test Runs
```bash
# Run Market Scanner
python3 ~/.hermes/scripts/supertrend_bb_crypto_scanner.py

# Run Executor Loop
bash ~/.hermes/scripts/binance_auto_executor_loop.sh

# Run SL Watchdog
python3 ~/.hermes/scripts/binance_sl_watchdog.py
```

---

## 💎 Key Unique Features
1. **Signal Grading (Grade A & B):**
   - **Grade A:** Fresh Supertrend flip within <= 1 candle, strong distance ($\ge 2.0\%$), RSI in safe neutral zone `[35, 65]`, and aligned with Bollinger Band mid-cross.
   - **Grade B:** Valid setup meeting core Supertrend and RSI volatility gates across 2-3 bars.
2. **Dynamic Trailing Stop Loss:**
   - Stop Loss is not static; it dynamically trails the Supertrend line in real-time as price moves in favor, protecting open profits while cutting losses automatically at `-3.0%` hard stop.
3. **Precision PnL Reconciliation & Alerts:**
   - Real-time WhatsApp notifications featuring exact entry, exit, percentage return, and realized USD profit/loss (`userTrades` execution truth).
4. **Autonomous Risk Guardrails:**
   - Strict position sizing ($20 margin @ 10x leverage = $200 notional) with anti-duplicate execution locks and watchdog error suppression.

---

## 🗂️ Local State Files
- `supertrend_bb_state.json`: Latest scanner state and signal cache.
- `auto_executor_state.json`: Executed orders and active symbol cache.
- `watchdog_track_state.json`: Position state tracked by watchdogs for PnL reconciliation.

---

## 🛠️ Troubleshooting & Common Binance Error Codes
- **`-2021` (Order would immediately trigger):** Trigger price is too close to market price (handled gracefully by watchdog).
- **`-1021` (Timestamp outside recvWindow):** System time desync with Binance servers. Run NTP sync (`timedatectl set-ntp true`).
- **`-4120` (Algo order rejection):** Precision/tick size mismatch (handled via CCXT price rounding).

---

## 📄 License
This project is open-sourced under the [MIT License](LICENSE).
Persistent user `systemd` services with auto-restart enabled:

### Example Unit File (`~/.config/systemd/user/binance-auto-executor.service`):
```ini
[Unit]
Description=Binance Auto Executor (Supertrend Strategy)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/dy6z/.hermes/scripts/binance_auto_executor.py
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
```

### Useful Commands:
```bash
# Enable and start services
systemctl --user daemon-reload
systemctl --user enable --now binance-auto-executor binance-sl-watchdog binance-profit-lock supertrend-bb-loop

# View live logs
journalctl -n 50 --user -u binance-auto-executor -f
journalctl -n 50 --user -u binance-sl-watchdog -f
journalctl -n 50 --user -u binance-profit-lock -f
```

---

## 📄 License
This project is open-sourced under the [MIT License](LICENSE).
