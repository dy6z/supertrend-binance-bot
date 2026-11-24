#!/usr/bin/env python3
"""
Supertrend + Bollinger Band MID Gate - Crypto Scanner
Binance Futures 15m ONLY (single timeframe)

SIGNAL LOGIC (owner spec, 2026-11-07):
  Bollinger Band MID (SMA20) is a DIRECTIONAL CONFIRMATION:
    - price closes ABOVE bb_mid -> bullish context
    - price closes BELOW bb_mid -> bearish context
  Supertrend flip is the TRIGGER.
  A valid signal = BB mid break AND ST flip, SAME DIRECTION, close in time.

  GRADE A ("barengan"): BB mid cross and ST flip within <= 1 bar
  GRADE B ("disusul") : BB mid cross then ST flip 2-3 bars later
  SKIP                : ST flip with NO same-direction BB mid cross in window
                        (option 1: mid-cross is MANDATORY, not optional)

Entry: BB mid cross (same dir) + fresh Supertrend flip
Exit : Supertrend trailing SL + Max TP 2.0%
NO BEP logic (proven: BEP kills profit)
"""

import sys
import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

# === PARAMETERS ===
PARAMS = {
    'symbols': [
        'ZECUSDT', 'SOLUSDT', 'PEOPLEUSDT', 'TIAUSDT', 'TAOUSDT', 'JUPUSDT', 'GRTUSDT',
        'FETUSDT', 'OPUSDT', 'ARBUSDT', 'INJUSDT', 'XRPUSDT', '1000PEPEUSDT', 'ENAUSDT',
        'UNIUSDT', 'ADAUSDT', 'LINKUSDT', 'SUIUSDT', 'WLDUSDT', 'DOGEUSDT', 'NEARUSDT',
        'ETHUSDT', 'SNDKUSDT', 'INITUSDT', 'SNXXUSDT', 'SOXSUSDT'
    ],
    'timeframe': '15m',
    'lookback': 500,          # bars
    'st_period': 10,
    'st_multiplier': 3.0,
    'bb_period': 20,          # SMA20 = mid band
    'bb_std': 2.0,
    'min_st_distance_pct': 2.0,   # 2.0% threshold paired with RSI [35, 65] (optimal return +70.0%)
    'max_tp_percent': 0.018,      # +1.8% (sweep winner: +96.0%, PF 1.13 vs +70.0% @2.0%)
    'hard_stop_pct': 0.03,        # -3% catastrophe stop (replaces profit lock: +113.3%, PF 1.15)
    # signal timing windows (in bars between BB mid cross and ST flip)
    'gap_grade_a_max': 1,     # "barengan"  : gap 0-1 bar
    'gap_grade_b_max': 3,     # "disusul"   : gap 2-3 bars
}

STATE_FILE = '/home/dy6z/.hermes/scripts/supertrend_bb_state.json'

# --- health tracking -------------------------------------------------------
# fetch_binance_klines() records here when it cannot get data for a symbol.
# scan_all_symbols() clears it at the start of every scan and reads it after.
FAILED_SYMBOLS = set()

# Test hook: when set to a set of labels ("ALL" = every symbol, or an int N =
# the first N symbols), fetch is forced to fail so the BLIND/DEGRADED alert
# path can be verified end-to-end without touching the network.
FORCE_FETCH_FAIL = None

# === SUPERTREND CALCULATION ===

def calculate_rma(series, period):
    """RMA = Exponentially Weighted Moving Average with alpha = 1/period"""
    return series.ewm(alpha=1/period, adjust=False).mean()

def calculate_atr(df, period):
    """Calculate ATR"""
    high = df['high']
    low = df['low']
    close = df['close']

    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return calculate_rma(tr, period)

def get_price_basis(df):
    """hlcc4 price source"""
    return (df['high'] + df['low'] + df['close'] + df['close']) / 4

def calculate_supertrend(df, mult, atr_len):
    """TradingView standard Supertrend implementation"""
    hl2 = (df['high'] + df['low']) / 2
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - df['close'].shift(1)).abs()
    tr3 = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Wilder's smoothing (RMA) for ATR
    atr = calculate_atr(df, atr_len)
    
    upper_basic = hl2 + (mult * atr)
    lower_basic = hl2 - (mult * atr)
    
    upper_band = upper_basic.copy()
    lower_band = lower_basic.copy()
    
    for i in range(1, len(df)):
        if df['close'].iloc[i-1] < upper_band.iloc[i-1]:
            upper_band.iloc[i] = min(upper_basic.iloc[i], upper_band.iloc[i-1])
        else:
            upper_band.iloc[i] = upper_basic.iloc[i]
            
        if df['close'].iloc[i-1] > lower_band.iloc[i-1]:
            lower_band.iloc[i] = max(lower_basic.iloc[i], lower_band.iloc[i-1])
        else:
            lower_band.iloc[i] = lower_basic.iloc[i]
            
    trend = pd.Series(1, index=df.index)
    for i in range(1, len(df)):
        if trend.iloc[i-1] == 1:
            if df['close'].iloc[i] < lower_band.iloc[i]:
                trend.iloc[i] = -1
            else:
                trend.iloc[i] = 1
        else:
            if df['close'].iloc[i] > upper_band.iloc[i]:
                trend.iloc[i] = 1
            else:
                trend.iloc[i] = -1
                
    return trend, lower_band, upper_band

# === BB CALCULATION ===

def calculate_bb(df, period, stdev):
    """Calculate Bollinger Bands (mid = SMA of close)"""
    close = df['close']

    bb_mid = close.rolling(period).mean()
    bb_std = close.rolling(period).std()
    bb_upper = bb_mid + stdev * bb_std
    bb_lower = bb_mid - stdev * bb_std

    return bb_mid, bb_upper, bb_lower

# === DATA FETCH ===

def fetch_binance_klines(symbol, interval, limit):
    """Fetch klines from Binance Futures API with 1 retry on transient failure."""
    url = 'https://fapi.binance.com/fapi/v1/klines'
    params = {
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }

    # Test hook (never active in normal runs)
    force = FORCE_FETCH_FAIL
    if force is not None and (force == "ALL" or symbol in force):
        FAILED_SYMBOLS.add(symbol)
        print(f"FETCH_FAIL {symbol} {interval}: (forced)")
        return None

    last_err = None
    # 3 attempts with progressive backoff. Some symbols (1000PEPEUSDT, XRPUSDT)
    # intermittently time out on Binance's side; extra retries recover them.
    for attempt in (1, 2, 3):
        try:
            r = requests.get(url, params=params, timeout=20)
            data = r.json()

            if not isinstance(data, list):
                last_err = f"bad payload: {str(data)[:80]}"
            else:
                df = pd.DataFrame(data, columns=[
                    'ts', 'open', 'high', 'low', 'close', 'volume',
                    'ct', 'qv', 'n', 'tbv', 'tqv', 'x'
                ])

                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = df[col].astype(float)

                df['ts'] = pd.to_datetime(df['ts'].astype(int), unit='ms', utc=True)

                return df[['ts', 'open', 'high', 'low', 'close', 'volume']]

        except Exception as e:
            last_err = str(e)

        if attempt < 3:
            time.sleep(1.5 * attempt)   # 1.5s, then 3.0s backoff

    print(f"FETCH_FAIL {symbol} {interval}: {last_err}")
    FAILED_SYMBOLS.add(symbol)
    return None
# === SIGNAL DETECTION ===

def find_crosses(close, bb_mid):
    """
    Return list of (index, direction) for BB mid crosses.
      bullish cross : close[i] > mid[i] and close[i-1] <= mid[i-1]
      bearish cross : close[i] < mid[i] and close[i-1] >= mid[i-1]
    """
    crosses = []
    for i in range(1, len(close)):
        c, p = close.iloc[i], close.iloc[i-1]
        m, pm = bb_mid.iloc[i], bb_mid.iloc[i-1]
        if pd.isna(m) or pd.isna(pm):
            continue
        if c > m and p <= pm:
            crosses.append((i, 1))
        elif c < m and p >= pm:
            crosses.append((i, -1))
    return crosses


def find_flips(trend):
    """Return list of (index, direction) where trend flipped at bar i."""
    flips = []
    for i in range(1, len(trend)):
        if trend.iloc[i] != trend.iloc[i-1]:
            flips.append((i, int(trend.iloc[i])))
    return flips


def check_entry_signal(symbol):
    """
    Entry requires ALL of:
      1. fresh Supertrend flip on the last CLOSED bar
      2. same-direction BB mid cross within [-1, +3] bars of that flip
           gap = flip_idx - cross_idx
           gap in [-1, 1] -> GRADE A ("barengan")
           gap in [ 2, 3] -> GRADE B ("disusul")
      3. Supertrend distance >= min_st_distance_pct (whipsaw guard)
    No same-direction BB mid cross  -> SKIP entirely (option 1).
    """
    df = fetch_binance_klines(symbol, PARAMS['timeframe'], PARAMS['lookback'])
    if df is None or len(df) < 100:
        return None

    # --- closed-bar guard: Binance's final kline is still forming (repaints).
    # Drop it so every decision uses only fully closed 15m candles.
    df = df.iloc[:-1].reset_index(drop=True)
    if len(df) < 60:
        return None

    trend, lower, upper = calculate_supertrend(
        df, PARAMS['st_multiplier'], PARAMS['st_period']
    )
    bb_mid, bb_upper, bb_lower = calculate_bb(
        df, PARAMS['bb_period'], PARAMS['bb_std']
    )

    last = len(df) - 1
    price = float(df.iloc[last]['close'])
    current_trend = int(trend.iloc[last])
    prev_trend = int(trend.iloc[last - 1])

    # 1. fresh flip only (flip exactly at the last closed bar)
    if current_trend == prev_trend:
        return None

    # ST line = support for LONG, resistance for SHORT
    st_line = float(lower.iloc[last] if current_trend == 1 else upper.iloc[last])

    # 3. ST distance filter (disabled when min_st_distance_pct == 0.0)
    if current_trend == 1:
        st_distance_pct = (price - st_line) / price * 100
    else:
        st_distance_pct = (st_line - price) / price * 100

    if PARAMS['min_st_distance_pct'] > 0 and st_distance_pct < PARAMS['min_st_distance_pct']:
        return None

    # 4. Asymmetric RSI Momentum filter (LONG: 55-65, SHORT: 35-45)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi_series = 100 - (100 / (1 + rs))
    current_rsi = float(rsi_series.iloc[last])
    
    if current_trend == 1 and not (55 <= current_rsi <= 65):
        return None
    if current_trend == -1 and not (35 <= current_rsi <= 45):
        return None

    # 2. BB mid gate: same-direction cross within window
    crosses = find_crosses(df['close'], bb_mid)
    same_dir = [ix for ix, d in crosses if d == current_trend]

    grade = None
    gap = None
    cross_idx = None
    for ix in reversed(same_dir):
        g = last - ix                      # bars between cross and flip
        if -1 <= g <= PARAMS['gap_grade_a_max']:
            grade, gap, cross_idx = 'A', g, ix
            break
        if PARAMS['gap_grade_a_max'] < g <= PARAMS['gap_grade_b_max']:
            grade, gap, cross_idx = 'B', g, ix
            break

    if grade is None:
        return None                        # no same-dir BB mid cross -> SKIP

    mid_now = float(bb_mid.iloc[last])
    direction = 'LONG' if current_trend == 1 else 'SHORT'
    tp_price = price * (1 + PARAMS['max_tp_percent']) if current_trend == 1 \
        else price * (1 - PARAMS['max_tp_percent'])

    # Asymmetric RSI Momentum filter (LONG: 55-65, SHORT: 35-45) - active
    grade = grade

    return {
        'symbol': symbol,
        'direction': direction,
        'entry_price': price,
        'sl_price': st_line,
        'max_tp_price': tp_price,
        'st_distance_pct': round(st_distance_pct, 3),
        'rsi': round(current_rsi, 2),
        'bb_mid': round(mid_now, 6),
        'bb_cross_gap': gap,
        'grade': grade,
        'trend': current_trend,
        'is_new_flip': True,
    }
# === MAIN SCAN ===

def fmt_price(p):
    if p >= 1000: return f"{p:.2f}"
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.5f}"
    if p >= 0.0001: return f"{p:.7f}"
    return f"{p:.8f}"

def scan_all_symbols():
    """Scan all symbols for graded entry signals"""
    print(f"{'='*70}")
    print(f"SUPERTREND + BB MID GATE SCANNER")
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} (WIB: {(datetime.now(timezone.utc) + timedelta(hours=7)).strftime('%H:%M')})")
    print(f"{'='*70}\n")

    state = load_state()
    previous_alerted = set(state.get('alerted_signals', []))
    current_alerted = set()

    signals = []
    FAILED_SYMBOLS.clear()          # fresh health tally for this scan

    for symbol in PARAMS['symbols']:
        try:
            signal = check_entry_signal(symbol)

            if signal:
                signal_key = f"{signal['symbol']}_{signal['direction']}"
                current_alerted.add(signal_key)

                if signal_key not in previous_alerted:
                    signals.append(signal)
                    print(f"✅ {symbol:15} | {signal['direction']:5} | GRADE {signal['grade']} "
                          f"| gap {signal['bb_cross_gap']} | @ ${signal['entry_price']:.4f} "
                          f"| SL ${signal['sl_price']:.4f} | TP ${signal['max_tp_price']:.4f}")
                else:
                    print(f"   {symbol:15} | {signal['direction']:5} (continuing, skip)")
            else:
                print(f"   {symbol:15} | no signal")

        except Exception as e:
            FAILED_SYMBOLS.add(symbol)
            print(f"❌ {symbol:15} | ERROR: {str(e)}")

    # Sort priority: Grade AA-3 first, then A, then B
    signals.sort(key=lambda s: (0 if 'AA-3' in s['grade'] else (1 if 'A' in s['grade'] else 2)))

    total = len(PARAMS['symbols'])
    failed = sorted(FAILED_SYMBOLS)
    ok_count = total - len(failed)
    print(f"\n{'='*70}")
    print(f"NEW SIGNALS: {len(signals)}  (A={sum(1 for s in signals if s['grade']=='A')}, "
          f"B={sum(1 for s in signals if s['grade']=='B')})")
    print(f"HEALTH: ok={ok_count}/{total}  failed={len(failed)}"
          + (f"  ({', '.join(failed)})" if failed else ""))
    # Machine-readable health line the loop greps. BLIND = total blackout.
    if ok_count == 0:
        print(f"SCANNER_HEALTH: BLIND  ok={ok_count}/{total} failed={len(failed)}")
    elif failed:
        print(f"SCANNER_HEALTH: DEGRADED  ok={ok_count}/{total} failed={len(failed)}")
    else:
        print(f"SCANNER_HEALTH: OK  ok={ok_count}/{total} failed=0")
    print(f"{'='*70}")

    for sig in signals:
        sig['is_new_flip'] = True

    state['executables'] = signals
    state['alerted_signals'] = list(current_alerted)
    state['last_scan'] = datetime.now(timezone.utc).isoformat()
    if ok_count > 0:
        state['last_success_scan'] = datetime.now(timezone.utc).isoformat()
    state['last_ok_count'] = ok_count
    state['last_failed'] = failed
    save_state(state)

    return signals


# === STATE MANAGEMENT ===

def load_state():
    """Load previous state"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {'executables': [], 'last_scan': None, 'alerted_signals': []}
    return {'executables': [], 'last_scan': None, 'alerted_signals': []}

def save_state(state):
    """Save current state"""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


# === MAIN ===

GRADE_LABEL = {
    'AA-3': 'AA-3 - HIGH MOMENTUM 🚀 (Win Rate ~60%)',
    'A': 'A - BARENGAN (BB mid cross & ST flip within 1 bar)',
    'B': 'B - DISUSUL (ST flip 2-3 bars after BB mid cross)'
}

def main():
    signals = scan_all_symbols()

    if len(signals) > 0:
        print("\n" + "="*70)
        print(f"EXECUTABLE SIGNALS: {len(signals)}")
        print("="*70)
        print("\nEXECUTION INSTRUCTIONS:")
        print("="*70)

    for sig in signals:
            p_str = fmt_price(sig['entry_price'])
            sl_str = fmt_price(sig['sl_price'])
            tp_str = fmt_price(sig['max_tp_price'])
            arrow = "🚀" if sig['direction'] == 'LONG' else "📉"
            print(f"\n{arrow} *{sig['symbol']}* — *{sig['direction']}*")
            print(f"🏷️ Entry: ${p_str} (Market)")
            print(f"🎯 Target TP: ${tp_str} (+1.8%)")
            print(f"🛑 Stop Loss: ${sl_str} (Trailing ST / Hard Stop -3%)")
            print(f"📊 Grade: {sig['grade']} | ST Dist: {sig['st_distance_pct']:.2f}% | RSI: {sig.get('rsi', 'N/A')}")
            print(f"💰 Position: $20 margin @ 10x ($200 notional)")
    else:
        print("\n✅ No signals — no fresh flip with same-direction BB mid cross")

    print(f"\n{'='*70}")
    print(f"Next scan: 1 minute")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
