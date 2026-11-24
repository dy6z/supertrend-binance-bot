#!/usr/bin/env python3
"""Ownership registry for the Supertrend autonomous Binance executor.

The Binance USD-M account is in One-Way Mode, so ownership is tracked per
symbol. Manual and autonomous exposure must never share a symbol.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import json
import math
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

REGISTRY_FILE = "/home/dy6z/.hermes/scripts/binance_auto_positions.json"
REGISTRY_LOCK = REGISTRY_FILE + ".lock"
STATE_LOCK = "/home/dy6z/.hermes/scripts/auto_executor_state.json.lock"
CLIENT_PREFIX = "stauto-"
ENTRY_PREFIX = "stauto-e-"
TP_PREFIX = "stauto-tp-"
SL_PREFIX = "stauto-sl-"


def _lock_path(target: str) -> str:
    return target + ".lock"


@contextmanager
def file_lock(path: str, exclusive: bool = True):
    """Serialize readers/writers that coordinate shared JSON state."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def locked_state(path: str = REGISTRY_FILE):
    """Serialize a whole transaction separately from individual file I/O locks."""
    return file_lock(path + '.transaction.lock', exclusive=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_client_id(kind: str) -> str:
    """Return a Binance-safe client id no longer than 36 characters."""
    prefixes = {"entry": ENTRY_PREFIX, "tp": TP_PREFIX, "sl": SL_PREFIX}
    prefix = prefixes[kind]
    token = f"{int(time.time()):x}{secrets.token_hex(4)}"
    return f"{prefix}{token}"[:36]


def valid_st_distance(value: Any, maximum: float = 5.0) -> bool:
    try:
        distance = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(distance) and 0 <= distance <= maximum


def signal_is_fresh(scanner_state: dict[str, Any], max_age_seconds: float = 300) -> bool:
    """Require a recent scan and a recently closed 15m candle."""
    try:
        for key in ["last_success_scan"]:
            val = scanner_state.get(key)
            if val:
                scan_time = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                if scan_time.tzinfo is None:
                    scan_time = scan_time.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - scan_time.astimezone(timezone.utc)).total_seconds()
                if -10 <= age <= max_age_seconds:
                    return True
        return False
    except (KeyError, TypeError, ValueError):
        return False


def is_auto_client_id(value: Any) -> bool:
    return str(value or "").startswith(CLIENT_PREFIX)


def _empty_registry() -> dict[str, Any]:
    return {"version": 1, "positions": {}, "history": [], "updated_at": utc_now()}


def load_registry(path: str = REGISTRY_FILE) -> dict[str, Any]:
    with file_lock(_lock_path(path), exclusive=False):
        if not os.path.exists(path):
            return _empty_registry()
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, TypeError):
            return _empty_registry()
    if not isinstance(data, dict):
        return _empty_registry()
    data.setdefault("version", 1)
    data.setdefault("positions", {})
    data.setdefault("history", [])
    return data


def save_registry(registry: dict[str, Any], path: str = REGISTRY_FILE) -> None:
    """Atomically persist ownership while excluding other registry writers."""
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)
    payload = copy.deepcopy(registry)
    payload["updated_at"] = utc_now()
    with file_lock(_lock_path(path), exclusive=True):
        fd, tmp_path = tempfile.mkstemp(prefix=".auto-ownership-", dir=target_dir, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


def normalize_position(raw: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(raw.get("symbol") or "")
    if not symbol:
        return None
    if "positionAmt" in raw:
        signed_qty = float(raw.get("positionAmt") or 0)
        if signed_qty == 0:
            return None
        side = "LONG" if signed_qty > 0 else "SHORT"
        quantity = abs(signed_qty)
        entry_price = float(raw.get("entryPrice") or 0)
    else:
        quantity = abs(float(raw.get("quantity") or raw.get("amount") or 0))
        if quantity == 0:
            return None
        side = str(raw.get("side") or "").upper()
        if side not in {"LONG", "SHORT"}:
            return None
        entry_price = float(raw.get("entry_price") or raw.get("entryPrice") or 0)
    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "entry_price": entry_price,
    }


def position_map(positions: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in positions:
        pos = normalize_position(raw)
        if pos:
            result[pos["symbol"]] = pos
    return result


def quantities_match(expected: Any, actual: Any) -> bool:
    expected_f = abs(float(expected or 0))
    actual_f = abs(float(actual or 0))
    tolerance = max(1e-12, expected_f * 1e-8)
    return abs(expected_f - actual_f) <= tolerance


def register_entry(
    registry: dict[str, Any],
    *,
    symbol: str,
    side: str,
    quantity: float,
    entry_price: float,
    order_id: Any,
    client_order_id: str,
    tp_algo_id: Any = None,
    tp_client_algo_id: str | None = None,
) -> dict[str, Any]:
    record = {
        "symbol": symbol,
        "side": side,
        "quantity": float(quantity),
        "entry_price": float(entry_price),
        "entry_order_id": str(order_id),
        "entry_client_order_id": client_order_id,
        "tp_algo_id": str(tp_algo_id) if tp_algo_id is not None else None,
        "tp_client_algo_id": tp_client_algo_id,
        "opened_at": utc_now(),
        "status": "active",
        "freeze_reason": None,
        "conflict_alerted": False,
    }
    registry.setdefault("positions", {})[symbol] = record
    return record


def _recover_from_orders(
    symbol: str, live: dict[str, Any], orders: Iterable[dict[str, Any]]
) -> dict[str, Any] | None:
    filled = []
    for order in orders or []:
        qty = float(order.get("executedQty") or order.get("origQty") or 0)
        if qty <= 0 or str(order.get("status", "")).upper() != "FILLED":
            continue
        if bool(order.get("reduceOnly", False)):
            continue
        if str(order.get("side", "")).upper() not in {"BUY", "SELL"}:
            continue
        filled.append(order)
    if not filled:
        return None
    latest = max(filled, key=lambda row: int(row.get("updateTime") or row.get("time") or 0))
    client_id = str(latest.get("clientOrderId") or latest.get("origClientOrderId") or "")
    if not client_id.startswith(ENTRY_PREFIX) and client_id != 'stauto-e-recovered':
        return None
    quantity = float(latest.get("executedQty") or latest.get("origQty") or 0)
    expected_side = "LONG" if str(latest.get("side", "")).upper() == "BUY" else "SHORT"
    status = "active"
    reason = None
    if expected_side != live["side"]:
        status = "frozen"
        reason = f"side mismatch: owned={expected_side} exchange={live['side']}"
    elif not quantities_match(quantity, live["quantity"]):
        status = "frozen"
        reason = f"quantity mismatch: owned={quantity} exchange={live['quantity']}"
    return {
        "symbol": symbol,
        "side": expected_side,
        "quantity": quantity,
        "entry_price": float(latest.get("avgPrice") or latest.get("price") or live["entry_price"]),
        "entry_order_id": str(latest.get("orderId") or ""),
        "entry_client_order_id": client_id,
        "tp_algo_id": None,
        "tp_client_algo_id": None,
        "opened_at": datetime.fromtimestamp(
            int(latest.get("updateTime") or latest.get("time") or 0) / 1000,
            tz=timezone.utc,
        ).isoformat(),
        "status": status,
        "freeze_reason": reason,
        "conflict_alerted": False,
        "recovered_at": utc_now(),
    }


def reconcile_registry(
    registry: dict[str, Any],
    live_positions: Iterable[dict[str, Any]],
    order_lookup: Callable[[str], Iterable[dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconcile ownership conservatively against current exchange positions."""
    registry = copy.deepcopy(registry)
    records = registry.setdefault("positions", {})
    live = position_map(live_positions)
    removed: list[str] = []
    recovered: list[str] = []

    for symbol in list(records):
        record = records[symbol]
        current = live.get(symbol)
        if current is None:
            if record.get('status') == 'pending':
                # Preserve uncertain entry intents until order history settles.
                continue
            closed = copy.deepcopy(record)
            closed["closed_at"] = utc_now()
            closed["close_reason"] = "position absent on exchange"
            registry.setdefault("history", []).append(closed)
            del records[symbol]
            removed.append(symbol)
            continue
        reason = record.get('freeze_reason') if record.get('status') == 'frozen' else None
        if record.get('status') == 'pending' and order_lookup:
            recovered_record = _recover_from_orders(symbol, current, order_lookup(symbol))
            if recovered_record and recovered_record['entry_client_order_id'] == record.get('entry_client_order_id'):
                records[symbol] = record = recovered_record
            else:
                reason = 'pending entry identity unresolved'
        if order_lookup:
            # Every active position must still trace to the same entry order.
            # A manual close/reopen with identical size is a new ownership cycle.
            orders = list(order_lookup(symbol))
            entries = [o for o in orders if float(o.get('executedQty') or 0) > 0
                       and str(o.get('reduceOnly', False)).lower() != 'true']
            if not entries:
                reason = None
            else:
                latest = max(entries, key=lambda o: int(o.get('orderId') or o.get('id') or 0))
                latest_id = str(latest.get('orderId') or latest.get('id') or '')
                recorded_id = str(record.get('entry_order_id') or '')
                if latest_id and recorded_id and latest_id != recorded_id:
                    reason = f'entry identity changed: latest={latest_id} recorded={recorded_id}'
        if str(record.get("side", "")).upper() != current["side"]:
            reason = f"side mismatch: owned={record.get('side')} exchange={current['side']}"
        elif not quantities_match(record.get("quantity"), current["quantity"]):
            reason = (
                f"quantity mismatch: owned={record.get('quantity')} "
                f"exchange={current['quantity']}"
            )
        if reason:
            record["status"] = "frozen"
            record["freeze_reason"] = reason
        else:
            record["status"] = "active"
            record["freeze_reason"] = None
            record["conflict_alerted"] = False

    if order_lookup:
        for symbol, current in live.items():
            if symbol in records:
                continue
            try:
                recovered_record = _recover_from_orders(symbol, current, order_lookup(symbol))
            except Exception:
                recovered_record = None
            if recovered_record:
                records[symbol] = recovered_record
                recovered.append(symbol)

    registry["history"] = registry.get("history", [])[-200:]
    owned_symbols = set(records).intersection(live)
    active = sorted(s for s in owned_symbols if records[s].get("status") == "active")
    frozen = sorted(s for s in owned_symbols if records[s].get("status") == "frozen")
    manual = sorted(set(live) - owned_symbols)
    summary = {
        "live": live,
        "active": active,
        "frozen": frozen,
        "manual": manual,
        "owned": sorted(owned_symbols),
        "removed": removed,
        "recovered": recovered,
    }
    return registry, summary


def capacity(
    registry: dict[str, Any],
    live_positions: Iterable[dict[str, Any]],
    *,
    max_auto: int = 2,
    max_total: int = 4,
) -> dict[str, int]:
    live = position_map(live_positions)
    owned = set(registry.get("positions", {})).intersection(live)
    auto_count = len(owned)
    total_count = len(live)
    manual_count = total_count - auto_count
    slots = max(0, min(max_auto - auto_count, max_total - total_count))
    return {
        "auto": auto_count,
        "manual": manual_count,
        "total": total_count,
        "slots": slots,
    }
