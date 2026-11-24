#!/usr/bin/env python3
"""
Binance Futures Auto Executor
Listen to scanner alerts, auto-execute market orders with TP

Safety:
- Position size: $10 margin @ 20x leverage ($200 notional)
- TP: +/-1.8%
- SL: Managed by autonomous-only Supertrend watchdog
- Limits: 2 autonomous positions, 4 total account positions
- Ownership: stauto-* client IDs plus a persistent per-symbol registry
- Safety check: fresh Grade A signal only; never share a symbol with manual exposure
"""

import sys
import os
import json
import requests
import hmac
import hashlib
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import urlencode
from dotenv import load_dotenv

from binance_auto_ownership import (
    ENTRY_PREFIX,
    REGISTRY_FILE,
    capacity,
    load_registry,
    make_client_id,
    reconcile_registry,
    register_entry,
    save_registry,
    valid_st_distance,
    signal_is_fresh,
    file_lock,
    REGISTRY_LOCK,
)

# Load credentials
load_dotenv(os.path.expanduser('~/.agent/credentials/binance.env'))
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
BASE_URL = 'https://fapi.binance.com'

# Config
MARGIN_USD = 10  # $10 per trade
LEVERAGE = 20    # 20x leverage
MAX_TP_PCT = 0.018  # +/-1.8%
MAX_ST_DISTANCE_PCT = 5.0  # Execute only signals with ST distance <= 5%
MAX_AUTO_POSITIONS = 2   # Autonomous positions owned by this executor
MAX_TOTAL_POSITIONS = 4  # Preserve room for up to two manual positions
STATE_FILE = '/home/dy6z/.hermes/scripts/auto_executor_state.json'
SCANNER_STATE_FILE = '/home/dy6z/.hermes/scripts/supertrend_bb_state.json'

# WhatsApp config
HERMES_API_URL = 'http://localhost:8177'
WA_CHAT_ID = os.getenv('WHATSAPP_HOME_CHAT_ID', '120363349919489485@g.us')

# === WHATSAPP ALERT ===

def send_whatsapp(message):
    """Send WhatsApp alert via Hermes send command targeting explicit chat ID or whatsapp channel"""
    try:
        import subprocess
        result = subprocess.run(
            ['hermes', 'send', '--to', 'whatsapp', message],
            capture_output=True,
            timeout=10,
            text=True
        )
        return result.returncode == 0
    except Exception as e:
        print(f"  ⚠️  WhatsApp alert error: {e}")
        return False

# === BINANCE API ===

def sign_request(params):
    """Sign Binance request using the exact encoded query string sent on wire."""
    query_string = urlencode([(k, str(v)) for k, v in params.items()])
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()


def binance_request(method, endpoint, params=None, signed=True):
    """Make Binance request, rejecting HTTP and API-body errors with built-in retry on IP-whitelist/connection glitches (-2015) and timestamp/signature drift (-1022)."""
    params = dict(params or {})
    headers = {'X-MBX-APIKEY': API_KEY}
    
    for attempt in range(3):
        if signed:
            try:
                response = requests.get(f'{BASE_URL}/fapi/v1/time', timeout=5)
                response.raise_for_status()
                params['timestamp'] = response.json()['serverTime']
            except Exception:
                params['timestamp'] = int(time.time() * 1000)
            params['recvWindow'] = 10000
            params['signature'] = sign_request(params)

        url = f"{BASE_URL}{endpoint}"
        try:
            if method == 'GET':
                response = requests.get(url, params=params, headers=headers, timeout=10)
            elif method == 'POST':
                response = requests.post(url, params=params, headers=headers, timeout=10)
            elif method == 'DELETE':
                response = requests.delete(url, params=params, headers=headers, timeout=10)
            else:
                raise ValueError(f"Unknown method: {method}")
            
            payload = response.json()
            if response.status_code >= 400:
                code = int(payload.get('code', 0) or 0) if isinstance(payload, dict) else 0
                if (response.status_code == 401 and code == -2015) or (response.status_code == 400 and code == -1022):
                    if attempt < 2:
                        time.sleep(0.5)
                        continue
                raise RuntimeError(f"Binance HTTP {response.status_code}: {payload}")
            if isinstance(payload, dict) and int(payload.get('code', 0) or 0) < 0:
                code = int(payload.get('code'))
                if code in (-2015, -1022) and attempt < 2:
                    time.sleep(0.5)
                    continue
                raise RuntimeError(f"Binance API error: {payload}")
            return payload
        except (requests.RequestException, ValueError) as exc:
            if attempt == 2:
                raise
            time.sleep(0.5)
    raise RuntimeError("Binance request failed after retries")

# === HELPER FUNCTIONS ===

def get_symbol_info(symbol):
    """Get cached exchange filters needed for exact order sizing and prices."""
    global _EXCHANGE_INFO_CACHE
    if _EXCHANGE_INFO_CACHE is None:
        data = binance_request('GET', '/fapi/v1/exchangeInfo', signed=False)
        _EXCHANGE_INFO_CACHE = {row['symbol']: row for row in data.get('symbols', [])}

    row = _EXCHANGE_INFO_CACHE.get(symbol)
    if not row or row.get('status') != 'TRADING':
        return None
    filters = {item['filterType']: item for item in row.get('filters', [])}
    lot = filters.get('LOT_SIZE')
    price_filter = filters.get('PRICE_FILTER')
    min_filter = filters.get('MIN_NOTIONAL', {})
    if not lot or not price_filter:
        return None
    return {
        'qty_precision': int(row['quantityPrecision']),
        'price_precision': int(row['pricePrecision']),
        'qty_step': str(lot['stepSize']),
        'tick_size': str(price_filter['tickSize']),
        'min_notional': float(min_filter.get('notional', 5.0)),
    }


def quantize_quantity(quantity, precision):
    step = Decimal(str(precision['qty_step']))
    raw = Decimal(str(quantity))
    rounded = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
    return rounded.quantize(Decimal(1).scaleb(-precision['qty_precision']))


def quantize_price(price, precision):
    tick = Decimal(str(precision['tick_size']))
    raw = Decimal(str(price))
    rounded = (raw / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    return rounded.quantize(Decimal(1).scaleb(-precision['price_precision']))


_EXCHANGE_INFO_CACHE = None

def get_current_price(symbol):
    """Get current mark price"""
    try:
        data = binance_request('GET', '/fapi/v1/premiumIndex', {'symbol': symbol}, signed=False)
        return float(data['markPrice'])
    except Exception as e:
        print(f"  ❌ Error fetching price: {e}")
        return None

def get_open_positions():
    """Get normalized live positions; API failure is a hard safety stop."""
    data = binance_request('GET', '/fapi/v2/positionRisk')
    if isinstance(data, dict) and 'code' in data:
        raise RuntimeError(f"positionRisk API error: {data}")
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected positionRisk response: {data}")
    positions = []
    for pos in data:
        amount = float(pos.get('positionAmt', 0))
        if amount == 0:
            continue
        positions.append({
            'symbol': pos['symbol'],
            'side': 'LONG' if amount > 0 else 'SHORT',
            'quantity': abs(amount),
            'entry_price': float(pos.get('entryPrice', 0)),
        })
    return positions


def get_recent_orders(symbol, limit=100):
    """Read recent orders so ownership can recover after state loss/restart."""
    data = binance_request('GET', '/fapi/v1/allOrders', {'symbol': symbol, 'limit': limit})
    if isinstance(data, dict) and 'code' in data:
        # Fallback to order endpoint or userTrades if allOrders fails/empty
        try:
            trades = binance_request('GET', '/fapi/v1/userTrades', {'symbol': symbol, 'limit': limit})
            if isinstance(trades, list):
                # Convert trades into synthetic order dicts for recovery
                orders_map = {}
                for t in trades:
                    oid = t.get('orderId')
                    if oid not in orders_map:
                        orders_map[oid] = {
                            'orderId': oid,
                            'clientOrderId': 'stauto-e-recovered',
                            'symbol': symbol,
                            'side': t.get('side'),
                            'status': 'FILLED',
                            'executedQty': t.get('qty'),
                            'origQty': t.get('qty'),
                            'avgPrice': t.get('price'),
                            'price': t.get('price'),
                            'time': t.get('time'),
                            'updateTime': t.get('time'),
                            'reduceOnly': False
                        }
                return list(orders_map.values())
        except Exception:
            pass
        return []
    return data if isinstance(data, list) else []

from decimal import Decimal


def tp_matches(order, symbol, side, quantity, trigger, client_id=None):
    """Require the complete reduce-only protection contract on read-back."""
    try:
        return (
            bool(order.get('algoId'))
            and order.get('symbol') == symbol
            and order.get('side') == side
            and str(order.get('clientAlgoId', '')).startswith('stauto-tp-')
            and (client_id is None or order.get('clientAlgoId') == client_id)
            and str(order.get('reduceOnly', '')).lower() == 'true'
            and order.get('workingType') == 'MARK_PRICE'
            and order.get('orderType', order.get('type')) in {'TAKE_PROFIT', 'TAKE_PROFIT_MARKET'}
            and str(order.get('algoStatus', order.get('status', 'NEW'))).upper() == 'NEW'
            and Decimal(str(order.get('quantity', order.get('origQty', 0)))) == Decimal(str(quantity))
            and Decimal(str(order.get('triggerPrice', 0))) == Decimal(str(trigger))
        )
    except Exception:
        return False


def set_leverage(symbol, leverage):
    """Set leverage and require the exchange-confirmed value to match."""
    try:
        result = binance_request('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage})
        return int(result.get('leverage', -1)) == leverage
    except Exception as e:
        print(f"  ⚠️  Leverage setting failed: {e}")
        return False


def execute_market_order(symbol, side, quantity, precision):
    """Execute entry and protect it with a verified conditional TP."""
    quantity = quantize_quantity(quantity, precision)
    if quantity <= 0:
        print(f"  ❌ Quantity rounds to zero for {symbol}")
        return None

    try:
        leverage_ok = set_leverage(symbol, LEVERAGE)
    except Exception as exc:
        print(f"  ❌ Cannot confirm leverage for {symbol}: {exc}")
        return None
    if not leverage_ok:
        print(f"  ❌ Requested leverage {LEVERAGE}x was not confirmed for {symbol}; entry cancelled")
        return None

    entry_client_id = make_client_id('entry')
    params = {
        'symbol': symbol,
        'side': side,
        'type': 'MARKET',
        'quantity': format(quantity, 'f'),
        'newClientOrderId': entry_client_id,
        'newOrderRespType': 'RESULT',
    }
    # Persist the entry intent before the network call so an uncertain fill
    # cannot disappear between placement and ownership registration.
    intent_registry = load_registry()
    intent = register_entry(
        intent_registry, symbol=symbol, side='LONG' if side == 'BUY' else 'SHORT',
        quantity=float(quantity), entry_price=0, order_id='', client_order_id=entry_client_id,
    )
    intent['status'] = 'pending'
    save_registry(intent_registry)
    try:
        result = binance_request('POST', '/fapi/v1/order', params)
    except Exception as exc:
        # Resolve ambiguous transport errors by client id before deciding retry.
        try:
            result = binance_request('GET', '/fapi/v1/order', {
                'symbol': symbol,
                'origClientOrderId': entry_client_id,
            })
        except Exception:
            print(f"  ❌ Entry result ambiguous for {symbol}; pending ownership retained; reconcile before retry")
            return None

    if 'orderId' not in result:
        pending_registry = load_registry()
        pending_registry.get('positions', {}).pop(symbol, None)
        save_registry(pending_registry)
        print(f"  ❌ Market order failed: {result}")
        return None

    order_id = result['orderId']
    if result.get('status') != 'FILLED':
        result = binance_request('GET', '/fapi/v1/order', {'symbol': symbol, 'orderId': order_id})
    if result.get('status') != 'FILLED':
        print(f"Entry {order_id} is not confirmed filled; pending ownership retained")
        return None
    executed_qty = Decimal(str(result.get('executedQty') or 0))
    avg_price = float(result.get('avgPrice') or 0)
    if avg_price <= 0:
        try:
            trades = binance_request('GET', '/fapi/v1/userTrades', {
                'symbol': symbol, 'orderId': order_id, 'limit': 1000,
            })
            fills = [t for t in trades if int(t.get('orderId', -1)) == int(order_id)]
            fill_qty = sum(float(t.get('qty', 0)) for t in fills)
            if fill_qty > 0:
                avg_price = sum(float(t['price']) * float(t['qty']) for t in fills) / fill_qty
        except Exception:
            avg_price = 0
    if avg_price <= 0:
        try:
            live = get_open_positions()
            avg_price = next(p['entry_price'] for p in live if p['symbol'] == symbol)
        except Exception:
            avg_price = 0
    if avg_price <= 0 or executed_qty <= 0:
        print(f"❌ Filled entry {order_id} but fill details are unavailable; ownership recovery required")
        return {
            'order_id': order_id,
            'entry_client_order_id': entry_client_id,
            'avg_price': avg_price,
            'quantity': float(executed_qty),
        }

    print(f"  ✅ Market order filled: #{order_id} @ ${avg_price:.8f} qty={executed_qty}")
    return {
        'order_id': order_id,
        'entry_client_order_id': entry_client_id,
        'avg_price': avg_price,
        'quantity': float(quantized_qty if 'quantized_qty' in locals() else executed_qty),
    }

# === STATE MANAGEMENT ===

def load_state():
    """Load executor state"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            return {'executed_signals': [], 'last_check': None}
    return {'executed_signals': [], 'last_check': None}

def save_state(state):
    """Save executor state"""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def load_scanner_state():
    """Load scanner state (executables)"""
    if os.path.exists(SCANNER_STATE_FILE):
        try:
            with open(SCANNER_STATE_FILE) as f:
                return json.load(f)
        except:
            return None
    return None

# === MAIN EXECUTOR ===

def execute_pending_signals(dry_run=False):
    """Serialize the complete cycle with the autonomous watchdog."""
    with file_lock('/home/dy6z/.hermes/scripts/binance_auto_cycle.lock'):
        return _execute_pending_signals(dry_run)


def _execute_pending_signals(dry_run=False):
    """Check scanner state and execute new signals."""
    print(f"{'='*70}")
    print(f"BINANCE AUTO EXECUTOR - Supertrend Strategy")
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} (WIB: {datetime.now(timezone.utc).hour + 7:02d}:{datetime.now(timezone.utc).minute:02d})")
    print(f"{'='*70}\n")
    
    # Load states and always reconcile ownership, even with no pending signal.
    state = load_state()
    scanner_state = load_scanner_state()
    registry = load_registry()
    try:
        live_positions = get_open_positions()
        registry, ownership = reconcile_registry(registry, live_positions, get_recent_orders)
        if not dry_run:
            save_registry(registry)
    except Exception as e:
        print(f"❌ Safety stop: cannot reconcile live positions: {e}\n")
        return

    limits = capacity(
        registry,
        live_positions,
        max_auto=MAX_AUTO_POSITIONS,
        max_total=MAX_TOTAL_POSITIONS,
    )
    live_symbols = set(ownership['live'])
    print(
        f"📊 Positions: auto {limits['auto']}/{MAX_AUTO_POSITIONS} | "
        f"manual {limits['manual']} | total {limits['total']}/{MAX_TOTAL_POSITIONS}"
    )
    if ownership['frozen']:
        print(f"⚠️  Frozen ownership conflict: {', '.join(ownership['frozen'])}")

    if not scanner_state or not signal_is_fresh(scanner_state):
        print("⚠️  Stale or unverified scanner state; refusing autonomous entry\n")
        return

    executables = scanner_state.get('executables', [])
    
    if len(executables) == 0:
        print("✅ No pending signals\n")
        return

    if limits['slots'] == 0:
        print("⚠️  No autonomous slot available; skipping execution\n")
        return
    
    # Filter NEW signals (not executed yet)
    executed_keys = set(state.get('executed_signals', []))
    new_signals = []
    
    for sig in executables:
        if sig.get('is_new_flip'):  # Only NEW flips, not retests
            # ONLY execute Grade A signals
            grade = sig.get('grade', 'B')
            if 'A' not in grade:
                continue

            # Executor-only risk filter; scanner output remains unchanged.
            if not valid_st_distance(sig.get('st_distance_pct'), MAX_ST_DISTANCE_PCT):
                print(
                    f"⏭️  Skip {sig.get('symbol', 'UNKNOWN')}: invalid or out-of-range "
                    f"ST distance (max {MAX_ST_DISTANCE_PCT:.2f}%)"
                )
                continue
                
            sig_key = f"{sig['symbol']}_{sig['direction']}_{sig['entry_price']}"
            if sig_key not in executed_keys:
                new_signals.append(sig)
    
    if len(new_signals) == 0:
        print("✅ No new signals to execute\n")
        return
    
    print(f"🔔 Found {len(new_signals)} new signal(s):\n")
    
    # Execute signals
    for sig in new_signals:
        symbol = sig['symbol']
        direction = sig['direction']
        entry_price = sig['entry_price']
        
        sig_key = f"{symbol}_{direction}_{entry_price}"
        
        print(f"{'─'*70}")
        print(f"Signal: {symbol} {direction} @ ${entry_price:.4f}")
        
        # Re-read exchange state immediately before each entry, not the scan snapshot.
        live_positions = get_open_positions()
        registry, ownership = reconcile_registry(registry, live_positions, get_recent_orders)
        live_symbols = set(ownership['live'])
        limits = capacity(
            registry,
            live_positions,
            max_auto=MAX_AUTO_POSITIONS,
            max_total=MAX_TOTAL_POSITIONS,
        )
        if limits['slots'] == 0:
            print("  ⏭️  Skipped: no autonomous/total slot available\n")
            break
        if symbol in live_symbols or symbol in registry.get('positions', {}):
            print("  ⏭️  Skipped: symbol already has manual or autonomous exposure\n")
            executed_keys.add(sig_key)
            continue
        
        # Get symbol info
        precision = get_symbol_info(symbol)
        if not precision:
            print(f"  ❌ Skipped: can't get symbol info\n")
            executed_keys.add(sig_key)  # Mark as attempted
            continue
        
        # Calculate position size
        current_price = get_current_price(symbol)
        if not current_price:
            print(f"  ❌ Skipped: can't get current price\n")
            executed_keys.add(sig_key)
            continue
        
        notional = MARGIN_USD * LEVERAGE  # $5 margin x 10 = $50 notional
        quantity = notional / current_price
        
        print(f"  Current price: ${current_price:.4f}")
        print(f"  Position: ${MARGIN_USD} margin @ {LEVERAGE}× = ${notional} notional")
        print(f"  Quantity: {quantity:.4f}")
        
        # Check actual rounded notional, not the target before rounding.
        quantity = quantize_quantity(quantity, precision)
        if quantity <= 0 or float(quantity) * current_price < precision['min_notional']:
            print(f"  Skipped {symbol}: rounded quantity is below exchange minimum")
            executed_keys.add(sig_key)
            continue
        if dry_run:
            print(f"DRY RUN: would enter {symbol} {direction}, quantity={quantity}")
            continue

        # Execute
        side = 'BUY' if direction == 'LONG' else 'SELL'
        
        result = execute_market_order(symbol, side, quantity, precision)

        if result:
            register_entry(
                registry,
                symbol=symbol,
                side=direction,
                quantity=result['quantity'],
                entry_price=result['avg_price'],
                order_id=result['order_id'],
                client_order_id=result['entry_client_order_id'],
                tp_algo_id=result.get('tp_algo_id'),
                tp_client_algo_id=result.get('tp_client_algo_id'),
            )
            save_registry(registry)
            live_positions.append({
                'symbol': symbol,
                'side': direction,
                'quantity': result['quantity'],
                'entry_price': result['avg_price'],
            })
            live_symbols.add(symbol)

            print("  Execution confirmed; ST Watchdog handles protection")
            wib_time = datetime.now(timezone(timedelta(hours=7)))
            send_whatsapp(
                f"🚀 *SUPERTREND AUTO EXECUTION*\n\n"
                f"🚀 *{symbol}* — *{direction}*\n"
                f"🏷️ Entry: ${result['avg_price']:.4f} (Market)\n"
                f"🎯 Protection: Managed by ST Watchdog\n"
                f"💰 Position: ${MARGIN_USD} margin @ {LEVERAGE}x (${notional} notional)\n\n"
                f"⏰ {wib_time.strftime('%H:%M')} WIB"
            )

            executed_keys.add(sig_key)
            continue

        print("  Entry not confirmed; reconcile pending intent before retry\n")
        send_whatsapp(f"AUTO ENTRY NOT CONFIRMED: {symbol} {direction}. Check pending intent/order status.")
        registry = load_registry()
        executed_keys.add(sig_key)  # Avoid retrying an already attempted signal
    
    # Save state
    if not dry_run:
        state['executed_signals'] = sorted(executed_keys)[-5000:]
        state['last_check'] = datetime.now(timezone.utc).isoformat()
        save_state(state)
    
    print(f"{'='*70}")
    print(f"Execution cycle complete")
    print(f"{'='*70}")

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='GET-only; no orders, alerts, or state writes')
    execute_pending_signals(dry_run=parser.parse_args().dry_run)
