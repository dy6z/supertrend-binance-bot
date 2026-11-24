#!/usr/bin/env python3
"""
Binance Futures SL Watchdog (MARKET CLOSE MODE)
Monitor open positions, close via MARKET order when price hits Supertrend line or on trend mismatch.
"""

import sys
import os
import requests
import time
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

# Load local scripts path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from binance_auto_ownership import (
    TP_PREFIX,
    load_registry,
    make_client_id,
    reconcile_registry,
    save_registry,
    file_lock,
)
from binance_auto_executor import (
    binance_request,
    get_symbol_info,
    get_recent_orders,
    quantize_quantity,
    quantize_price,
    get_open_positions,
    tp_matches,
)
from supertrend_bb_crypto_scanner import calculate_supertrend

ST_PERIOD = 10
ST_MULTIPLIER = 3.0
LOOKBACK = 500  # 500 closed bars

CYCLE_LOCK_PATH = "/home/dy6z/.hermes/scripts/binance_auto_cycle.lock"

def send_ownership_alert(message, dry_run=False):
    """Best-effort alert; suppressed in dry-run mode."""
    if dry_run:
        print(f"  [DRY-RUN] Suppressed alert: {message}")
        return
    try:
        import subprocess
        subprocess.run(
            ['hermes', 'send', '--to', 'whatsapp', message],
            capture_output=True,
            timeout=10,
            text=True,
        )
    except Exception as exc:
        print(f"  ⚠️ Ownership alert error: {exc}")

def get_supertrend_sl(symbol):
    """Get current Supertrend SL level for symbol using 500 closed bars"""
    url = 'https://fapi.binance.com/fapi/v1/klines'
    params = {'symbol': symbol, 'interval': '15m', 'limit': LOOKBACK}
    
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        
        if not isinstance(data, list) or len(data) < ST_PERIOD + 2:
            return None
        
        df = pd.DataFrame(data, columns=[
            'ts', 'open', 'high', 'low', 'close', 'volume',
            'ct', 'qv', 'n', 'tbv', 'tqv', 'x'
        ])
        
        for col in ['open', 'high', 'low', 'close']:
            df[col] = df[col].astype(float)
        
        # Exclude current unclosed bar (use closed bars)
        df = df.iloc[:-1].reset_index(drop=True)
        
        trend, lower, upper = calculate_supertrend(df, ST_MULTIPLIER, ST_PERIOD)
        
        current_trend = trend.iloc[-1]
        st_line = lower.iloc[-1] if current_trend == 1 else upper.iloc[-1]
        
        return {
            'trend': 'LONG' if current_trend == 1 else 'SHORT',
            'st_line': float(st_line)
        }
    except Exception as e:
        print(f"    ⚠️ Supertrend error for {symbol}: {e}")
        return None

def place_verified_tp(symbol, side, entry, quantity, precision, dry_run=False):
    """Verify existing TP or place new TP order verifying id/qty/side/trigger and ownership prefix."""
    try:
        # Check existing open algo orders
        res = binance_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol})
        algos = res if isinstance(res, list) else res.get('orders', [])
        
        target_pct = 0.018
        tp_price = entry * (1 - target_pct) if side == 'SHORT' else entry * (1 + target_pct)
        order_side = 'BUY' if side == 'SHORT' else 'SELL'
        
        quantized_price = quantize_price(tp_price, precision)
        quantized_qty = quantize_quantity(quantity, precision)
        
        for a in algos:
            if tp_matches(a, symbol, order_side, quantized_qty, quantized_price):
                return {'success': True, 'algoId': str(a['algoId']), 'clientAlgoId': a['clientAlgoId']}
        # Do not stack a replacement over an unverified autonomous TP.
        if any(str(a.get('clientAlgoId', '')).startswith(TP_PREFIX) for a in algos):
            return {'success': False, 'error': 'Existing autonomous TP contract mismatch'}

        if dry_run:
            print(f"  [DRY-RUN] Would place TP {order_side} @ ${quantized_price} qty {quantized_qty}")
            return {'success': True, 'algoId': 'dry_run_tp', 'clientAlgoId': 'stauto-tp-dryrun'}
        
        tp_client_id = make_client_id('tp')
        params = {
            'symbol': symbol,
            'side': order_side,
            'algoType': 'CONDITIONAL',
            'type': 'TAKE_PROFIT_MARKET',
            'triggerPrice': float(quantized_price),
            'quantity': float(quantized_qty),
            'reduceOnly': 'true',
            'workingType': 'MARK_PRICE',
            'clientAlgoId': tp_client_id,
        }
        
        # Get current mark price to check if TP would immediately trigger
        # Get current mark price to check if TP would immediately trigger
        try:
            pr_res = requests.get(f'https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}', timeout=5)
            mark_price = float(pr_res.json()['markPrice'])
            if side == 'SHORT' and quantized_price >= mark_price:
                return {'success': False, 'error': f'TP price {quantized_price} >= mark price {mark_price} (would immediately trigger)'}
            if side == 'LONG' and quantized_price <= mark_price:
                return {'success': False, 'error': f'TP price {quantized_price} <= mark price {mark_price} (would immediately trigger)'}
        except Exception:
            pass

        result = binance_request('POST', '/fapi/v1/algoOrder', params)
        algo_id = result.get('algoId')
        if algo_id:
            time.sleep(0.3)
            check = binance_request('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol})
            orders = check if isinstance(check, list) else check.get('orders', [])
            exists = any(str(o.get('algoId')) == str(algo_id) and tp_matches(o, symbol, order_side, quantized_qty, quantized_price, tp_client_id) for o in orders)
            if exists:
                print(f"    📈 TP verified and active: {order_side} @ ${quantized_price} | algoId={algo_id}")
                return {'success': True, 'algoId': str(algo_id), 'clientAlgoId': tp_client_id}
        
        return {'success': False, 'algoId': None, 'error': str(result)}
    except Exception as e:
        print(f"    ⚠️ TP placement/verification error: {e}")
        return {'success': False, 'algoId': None, 'error': str(e)}

def check_and_close_if_sl_hit(symbol, side, current_price, st_line, quantity, precision, force_close=False, dry_run=False):
    """Check if price hit ST line or force close (trend mismatch), close via MARKET order."""
    try:
        sl_hit = force_close
        if not sl_hit:
            if side == 'LONG' and current_price <= st_line:
                sl_hit = True
                print(f"    🛡️ SL HIT: Price ${current_price:.4f} <= ST ${st_line:.4f}")
            elif side == 'SHORT' and current_price >= st_line:
                sl_hit = True
                print(f"    🛡️ SL HIT: Price ${current_price:.4f} >= ST ${st_line:.4f}")
        
        if not sl_hit:
            return False
        
        quantized_qty = quantize_quantity(quantity, precision)
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        
        if dry_run:
            print(f"  [DRY-RUN] Would close position {symbol} {close_side} MARKET qty {quantized_qty}")
            return True
        
        params = {
            'symbol': symbol,
            'side': close_side,
            'type': 'MARKET',
            'quantity': float(quantized_qty),
            'reduceOnly': 'true',
            'newOrderRespType': 'RESULT',
            'newClientOrderId': make_client_id('sl'),
        }
        
        result = binance_request('POST', '/fapi/v1/order', params)
        if 'orderId' in result:
            if result.get('status') != 'FILLED':
                result = binance_request('GET', '/fapi/v1/order', {'symbol': symbol, 'orderId': result['orderId']})
            remaining = [p for p in get_open_positions() if p['symbol'] == symbol]
            if result.get('status') != 'FILLED' or remaining:
                return False
            print(f"    Position closure confirmed: orderId {result['orderId']}")
            return True
        else:
            print(f"    ❌ Failed to close: {result}")
            return False
    except Exception as e:
        print(f"    ❌ Error closing position: {e}")
        return False

def cleanup_orphans(open_symbols, live_positions=None, dry_run=False):
    """Cancel only stauto-prefixed algos whose autonomous position is gone (no LIVE position on exchange)."""
    try:
        print("\n🧹 Autonomous orphan cleanup...")
        data = binance_request('GET', '/fapi/v1/openAlgoOrders')
        algo_list = data if isinstance(data, list) else data.get('orders', [])
        open_symbols = set(open_symbols)
        live_symbols = set(p['symbol'] for p in (live_positions or get_open_positions()))
        
        orphans = []
        for algo in algo_list:
            client_id = str(algo.get('clientAlgoId') or algo.get('clientOrderId') or '')
            symbol = algo.get('symbol')
            if client_id.startswith('stauto-') and symbol and symbol not in open_symbols and symbol not in live_symbols:
                orphans.append(algo)
        
        if not orphans:
            print("  ✅ No autonomous orphan orders")
            return
        
        print(f"  Found {len(orphans)} autonomous orphan algo order(s)")
        for orphan in orphans:
            if dry_run:
                print(f"  [DRY-RUN] Would delete orphan algoId {orphan.get('algoId')} for {orphan.get('symbol')}")
                continue
            try:
                binance_request('DELETE', '/fapi/v1/algoOrder', {
                    'symbol': orphan['symbol'],
                    'algoId': orphan.get('algoId'),
                })
                print(f"  ✅ Deleted orphan algo {orphan.get('symbol')} | algoId {orphan.get('algoId')}")
            except Exception as exc:
                print(f"  ❌ Failed to delete algoId {orphan.get('algoId')}: {exc}")
    except Exception as exc:
        print(f"  ⚠️ Autonomous orphan cleanup error: {exc}")

def watchdog_scan(dry_run=False):
    """Main watchdog scan wrapped with shared cycle lock."""
    with file_lock(CYCLE_LOCK_PATH):
        print(f"{'='*70}")
        print(f"BINANCE SL WATCHDOG - Supertrend Trailing (MARKET CLOSE) {'' if not dry_run else '[DRY-RUN]'}")
        wib_time = datetime.now(timezone(timedelta(hours=7)))
        print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC (WIB: {wib_time.strftime('%H:%M')})")
        print(f"{'='*70}\n")
        
        try:
            all_positions = get_open_positions()
        except Exception as exc:
            print(f"❌ Safety stop: cannot read live positions: {exc}\n")
            return
        
        registry, ownership = reconcile_registry(load_registry(), all_positions, order_lookup=get_recent_orders)
        if not dry_run:
            save_registry(registry)
        
        cleanup_orphans(ownership['owned'], all_positions, dry_run=dry_run)
        
        owned_records = registry.get('positions', {})
        positions = [
            pos for pos in all_positions
            if owned_records.get(pos['symbol'], {}).get('status') == 'active'
        ]
        manual_positions = [pos for pos in all_positions if pos['symbol'] in ownership['manual']]
        
        if manual_positions:
            print(f"🔒 Manual positions ignored: {', '.join(p['symbol'] for p in manual_positions)}")
        
        for symbol in ownership['frozen']:
            record = owned_records[symbol]
            print(f"🚨 FROZEN {symbol}: {record.get('freeze_reason')}")
            if not record.get('conflict_alerted'):
                send_ownership_alert(
                    f"*AUTO EXECUTOR FROZEN*\n\n{symbol}\n{record.get('freeze_reason')}\n"
                    "TP/ST watchdog will not touch this symbol.",
                    dry_run=dry_run
                )
                record['conflict_alerted'] = True
                if not dry_run:
                    save_registry(registry)
        
        if len(positions) == 0:
            print("✅ No active autonomous positions\n")
            return
        
        print(f"📊 Found {len(positions)} autonomous position(s):\n")
        
        for pos in positions:
            symbol = pos['symbol']
            side = pos['side']
            entry = pos['entry_price']
            qty = pos['quantity']
            
            print(f"  {symbol:15} | {side:5} @ ${entry:.4f} | Qty {qty:.3f}")
            
            precision = get_symbol_info(symbol)
            if not precision:
                print(f"    ⚠️ Skip: can't get symbol info\n")
                continue
            
            # Place / verify TP
            tp_res = place_verified_tp(symbol, side, entry, qty, precision, dry_run=dry_run)
            if tp_res.get('success') and tp_res.get('algoId') and not dry_run:
                owned_records[symbol]['tp_algo_id'] = tp_res['algoId']
                owned_records[symbol]['tp_client_algo_id'] = tp_res.get('clientAlgoId')
                save_registry(registry)
            
            if not tp_res.get('success'):
                err_msg = tp_res.get('error', 'Unknown error')
                if 'would immediately trigger' in err_msg:
                    print(f"    ℹ️ TP already reached/exceeded for {symbol}: {err_msg}")
                else:
                    send_ownership_alert(f"AUTO TP NOT CONFIRMED: {symbol}. {err_msg}", dry_run=dry_run)

            # Get current price
            try:
                r = requests.get(f'https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}', timeout=5)
                current_price = float(r.json()['price'])
            except Exception as e:
                print(f"    ⚠️ Skip: can't get price\n")
                continue
            
            # Get Supertrend SL
            st = get_supertrend_sl(symbol)
            if not st:
                print(f"    ⚠️ Skip: can't calculate Supertrend\n")
                continue
            
            st_sl = st['st_line']
            st_trend = st['trend']
            
            print(f"    💰 Current: ${current_price:.4f}")
            print(f"    🛡️ ST line: ${st_sl:.4f} ({st_trend})")
            
            # Trend mismatch check: close reduceOnly instead skip
            trend_mismatch = (side == 'LONG' and st_trend == 'SHORT') or (side == 'SHORT' and st_trend == 'LONG')
            if trend_mismatch:
                print(f"    ⚠️ TREND MISMATCH: Position {side} but ST trend {st_trend} -> Closing via MARKET (reduceOnly)")
            
            closed = check_and_close_if_sl_hit(symbol, side, current_price, st_sl, qty, precision, force_close=trend_mismatch, dry_run=dry_run)
            
            if closed:
                print(f"    Position closed (SL hit or trend mismatch)\n")
                if not dry_run:
                    closed_record = registry['positions'].pop(symbol, None)
                    if closed_record:
                        closed_record['closed_at'] = datetime.now(timezone.utc).isoformat()
                        closed_record['close_reason'] = 'trend mismatch' if trend_mismatch else 'supertrend watchdog'
                        registry.setdefault('history', []).append(closed_record)
                        registry['history'] = registry['history'][-200:]
                        save_registry(registry)
            else:
                if side == 'LONG':
                    dist_pct = ((current_price - st_sl) / current_price) * 100
                    print(f"    ✅ SL active: {dist_pct:.2f}% below\n")
                else:
                    dist_pct = ((st_sl - current_price) / current_price) * 100
                    print(f"    ✅ SL active: {dist_pct:.2f}% above\n")

if __name__ == '__main__':
    dry_run = '--dry-run' in sys.argv
    watchdog_scan(dry_run=dry_run)
