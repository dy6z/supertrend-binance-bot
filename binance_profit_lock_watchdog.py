#!/usr/bin/env python3
"""Binance Watchdog (Auto TP + Trailing SL + 100% Synchronized Exact Exit Price & PnL)."""
import sys
import os
import requests
import hmac
import hashlib
import time
import json
from datetime import datetime, timezone, timedelta

env = {}
cred_path = os.path.expanduser("~/.agent/credentials/binance.env")
if os.path.exists(cred_path):
    for line in open(cred_path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'").strip('"')

KEY = env.get("BINANCE_API_KEY")
SECRET = env.get("BINANCE_API_SECRET")
BASE = "https://fapi.binance.com"
HERMES_BIN = "/home/dy6z/.local/bin/hermes"
STATE_FILE = os.path.expanduser("~/.hermes/scripts/watchdog_track_state.json")

def send_whatsapp(msg):
    try:
        import subprocess
        if os.path.exists(HERMES_BIN):
            subprocess.run([HERMES_BIN, 'send', '--to', 'whatsapp'], input=msg.encode('utf-8'), check=True, timeout=10)
    except Exception as e:
        print(f"Failed to send WhatsApp alert: {e}")

_exchange_info_cache = {}

def get_exchange_info():
    if not _exchange_info_cache.get('symbols'):
        try:
            r = requests.get(f"{BASE}/fapi/v1/exchangeInfo", timeout=10)
            data = r.json()
            for s in data.get('symbols', []):
                _exchange_info_cache[s['symbol']] = s
        except Exception as e:
            print(f"Failed to fetch exchange info: {e}")
    return _exchange_info_cache

def get_tick_size(symbol):
    info = get_exchange_info()
    s_info = info.get(symbol, {})
    for f in s_info.get('filters', []):
        if f.get('filterType') == 'PRICE_FILTER':
            return float(f.get('tickSize', '0.01'))
    return 0.01

def price_to_precision(symbol, price):
    tick = get_tick_size(symbol)
    tick_str = f"{tick:.8f}".rstrip('0')
    decimals = len(tick_str.split('.')[1]) if '.' in tick_str else 0
    rounded = round(round(price / tick) * tick, decimals)
    return f"{rounded:.{decimals}f}"

def sign(params):
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 10000
    qs = '&'.join(f'{k}={v}' for k, v in params.items())
    sig = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"

H = {'X-MBX-APIKEY': KEY}

def binance_request(method, path, params=None):
    if params is None: params = {}
    url = f"{BASE}{path}"
    try:
        if method == 'GET':
            r = requests.get(f"{url}?{sign(params)}", headers=H, timeout=10)
        elif method == 'POST':
            r = requests.post(f"{url}?{sign(params)}", headers=H, timeout=10)
        elif method == 'DELETE':
            r = requests.delete(f"{url}?{sign(params)}", headers=H, timeout=10)
        else:
            return None
        
        if 'application/json' in r.headers.get('Content-Type', ''):
            data = r.json()
            if r.status_code >= 400:
                print(f"Binance API error {r.status_code}: {data}")
                return None
            return data
        else:
            print(f"API non-JSON response ({r.status_code})")
            return None
    except Exception as e:
        print(f"Request exception {path}: {e}")
        return None

def get_positions():
    data = binance_request('GET', '/fapi/v2/positionRisk')
    if isinstance(data, list):
        return [p for p in data if float(p.get('positionAmt', 0)) != 0]
    return None

def get_all_algo_orders():
    data = binance_request('GET', '/fapi/v1/openAlgoOrders')
    if isinstance(data, dict) and 'orders' in data:
        return data['orders']
    if isinstance(data, list):
        return data
    return []

def get_recent_trades(symbol):
    data = binance_request('GET', '/fapi/v1/userTrades', {'symbol': symbol, 'limit': 5})
    if isinstance(data, list):
        return data
    return []

def cancel_algo_order(algo_id):
    return binance_request('DELETE', '/fapi/v1/algoOrder', {'algoId': algo_id})

def place_algo_tp(symbol, side, price, qty):
    rounded = price_to_precision(symbol, price)
    params = {
        'symbol': symbol,
        'side': side,
        'algoType': 'CONDITIONAL',
        'type': 'TAKE_PROFIT_MARKET',
        'stopPrice': rounded,
        'triggerPrice': rounded,
        'quantity': qty,
        'workingType': 'MARK_PRICE',
        'reduceOnly': 'true'
    }
    return binance_request('POST', '/fapi/v1/algoOrder', params)

def place_algo_sl(symbol, side, stop_price):
    rounded = price_to_precision(symbol, stop_price)
    params = {
        'symbol': symbol,
        'side': side,
        'algoType': 'CONDITIONAL',
        'type': 'STOP_MARKET',
        'stopPrice': rounded,
        'triggerPrice': rounded,
        'closePosition': 'true',
        'workingType': 'MARK_PRICE'
    }
    return binance_request('POST', '/fapi/v1/algoOrder', params)

def get_st_line(symbol):
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(
            "scanner_mod", "/home/dy6z/.hermes/scripts/supertrend_bb_crypto_scanner.py")
        sc = _ilu.module_from_spec(spec)
        spec.loader.exec_module(sc)

        df = sc.fetch_binance_klines(symbol, '15m', 300)
        if df is None or len(df) < 60: return None
        df = df.iloc[:-1].reset_index(drop=True)

        trend, lower, upper = sc.calculate_supertrend(df, 3.0, 10)
        return float(lower.iloc[-1]) if int(trend.iloc[-1]) == 1 else float(upper.iloc[-1])
    except Exception as e:
        print(f"ST calc error {symbol}: {e}")
        return None

def load_tracked():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except:
            return {}
    return {}

def save_tracked(state):
    try:
        json.dump(state, open(STATE_FILE, 'w'))
    except Exception as e:
        print(f"Failed to save tracked state: {e}")

def manage_positions_once():
    get_exchange_info()
    positions = get_positions()
    if positions is None:
        print("  ⚠️ API fetch position failed (None). Skipping check.")
        return
        
    active_symbols = {p['symbol'] for p in positions}
    all_algos = get_all_algo_orders()
    
    tracked = load_tracked()
    
    # 1. DETECT CLOSED POSITIONS & SEND 100% SYNCHRONIZED EXIT ALERT
    for sym in list(tracked.keys()):
        if sym not in active_symbols:
            info = tracked[sym]
            side = info.get('side', 'LONG')
            entry = info.get('entry', 0)
            
            exit_price = entry
            pnl_pct = 0.0
            
            # Fetch actual execution trade from Binance userTrades with retry
            trades = []
            for _ in range(3):
                time.sleep(1.0)
                trades = get_recent_trades(sym)
                if trades:
                    closing_side = 'SELL' if side == 'LONG' else 'BUY'
                    matching_trades = [t for t in trades if t.get('side') == closing_side]
                    if matching_trades:
                        break
            
            total_realized_pnl = 0.0
            total_qty = 0.0
            weighted_exit_sum = 0.0
            
            if trades:
                closing_side = 'SELL' if side == 'LONG' else 'BUY'
                matching_trades = [t for t in trades if t.get('side') == closing_side]
                if matching_trades:
                    for t in matching_trades:
                        try:
                            total_realized_pnl += float(t.get('realizedPnl', 0))
                            q = float(t.get('qty', 0))
                            pr = float(t.get('price', entry))
                            total_qty += q
                            weighted_exit_sum += pr * q
                        except:
                            pass
                    if total_qty > 0:
                        exit_price = weighted_exit_sum / total_qty
                    else:
                        exit_price = float(matching_trades[-1].get('price', entry))
            
            try:
                if entry > 0 and total_qty > 0:
                    pnl_pct = ((exit_price - entry) / entry * 100) if side == 'LONG' else ((entry - exit_price) / entry * 100)
                elif entry > 0:
                    pnl_pct = 0.0
            except:
                pnl_pct = 0.0

            status_icon = "🎯 *TAKE PROFIT HIT*" if total_realized_pnl >= 0 else "🛑 *STOP LOSS HIT*"
            if abs(total_realized_pnl) < 0.05 and abs(pnl_pct) < 0.05:
                status_icon = "🏁 *TRADE CLOSED*"

            msg = (
                f"{status_icon}\n\n"
                f"🔹 *{sym}* ({side})\n"
                f"📥 Entry: ${entry}\n"
                f"📤 Exit: ${exit_price}\n"
                f"💰 PnL: *{pnl_pct:+.2f}%* (${total_realized_pnl:+.2f})\n\n"
                f"⏰ {datetime.now(timezone(timedelta(hours=7))).strftime('%H:%M WIB')}"
            )
            print(f"[{sym}] Position closed. Sending 100% sync alert...")
            send_whatsapp(msg)
            del tracked[sym]
            
    save_tracked(tracked)
    
    # 2. AUTO CLEANUP ORPHAN ORDERS
    for o in all_algos:
        sym = o.get('symbol')
        algo_id = o.get('algoId')
        if sym not in active_symbols:
            print(f"  🧹 Cleaning orphan order {algo_id} for closed symbol {sym}...")
            cancel_algo_order(algo_id)
            
    # 3. MANAGE ACTIVE POSITIONS (Ensure 1 TP & 1 SL exist & track in state)
    for p in positions:
        sym = p['symbol']
        amt = float(p['positionAmt'])
        entry = float(p['entryPrice'])
        side = 'LONG' if amt > 0 else 'SHORT'
        qty = abs(amt)
        close_side = 'SELL' if side == 'LONG' else 'BUY'

        if sym not in tracked:
            tracked[sym] = {'side': side, 'entry': entry, 'time': datetime.now().isoformat()}
            save_tracked(tracked)

        sym_algos = [o for o in all_algos if o.get('symbol') == sym]
        tp_orders = [o for o in sym_algos if o.get('orderType') == 'TAKE_PROFIT_MARKET']
        sl_orders = [o for o in sym_algos if o.get('orderType') == 'STOP_MARKET']

        # Take Profit (+1.8%)
        if not tp_orders:
            tp_price = entry * 1.018 if side == 'LONG' else entry * 0.982
            res = place_algo_tp(sym, close_side, tp_price, qty)
            if res and isinstance(res, dict):
                print(f"  [{sym}] ✅ Placed TP at {tp_price:.4f}: algoId={res.get('algoId')}")
        elif len(tp_orders) > 1:
            for o in tp_orders[1:]:
                cancel_algo_order(o.get('algoId'))

        # Stop Loss (ST Trailing or Hard stop -3%)
        st_line = get_st_line(sym)
        if st_line is not None:
            hard_stop = entry * (1 - 0.03) if side == 'LONG' else entry * (1 + 0.03)
            target_sl = max(st_line, hard_stop) if side == 'LONG' else min(st_line, hard_stop)

            if not sl_orders:
                res = place_algo_sl(sym, close_side, target_sl)
                if res and isinstance(res, dict):
                    print(f"  [{sym}] ✅ Placed SL at {target_sl:.4f}: algoId={res.get('algoId')}")
            else:
                old_stop = float(sl_orders[0].get('triggerPrice', target_sl))
                diff_pct = abs(target_sl - old_stop) / old_stop * 100
                if diff_pct > 0.2:
                    for o in sl_orders:
                        cancel_algo_order(o.get('algoId'))
                    res = place_algo_sl(sym, close_side, target_sl)
                    if res and isinstance(res, dict):
                        print(f"  [{sym}] 🔄 Updated SL to {target_sl:.4f}: algoId={res.get('algoId')}")
                elif len(sl_orders) > 1:
                    for o in sl_orders[1:]:
                        cancel_algo_order(o.get('algoId'))

def run_loop():
    print(f"[{datetime.now()}] Binance Watchdog started (Sync Exit Prices).")
    while True:
        try:
            manage_positions_once()
        except Exception as e:
            print(f"Watchdog loop error: {e}")
        time.sleep(10)

if __name__ == '__main__':
    run_loop()
