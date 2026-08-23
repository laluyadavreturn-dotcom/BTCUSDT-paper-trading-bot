"""
CoinDCX BTCUSDT Futures - PAPER TRADING Bot (GitHub Actions version)
------------------------------------------------------------------------------
Runs ONCE per invocation - GitHub Actions triggers it every 15 minutes,
24/7 (crypto doesn't close on weekends). State persists via state.json,
committed back to the repo after each run.

Strategy: CCI(20, HLC/3) vs its own 14 EMA, with 20 EMA price trend filter
  Entry Long  : CCI(20) crosses ABOVE its 14 EMA, AND price > 20 EMA
  Entry Short : CCI(20) crosses BELOW its 14 EMA, AND price < 20 EMA
  Exit        : Stop-loss (0.25%) OR take-profit (0.5%) OR CCI crosses its
                14 EMA the opposite way - whichever happens first
  Timeframe   : 15 minute candles
  Pair        : B-BTC_USDT

Position sizing (risk-based, NOT a flat % of capital as margin):
  Starting capital : 1000 (unit = quote currency, USDT)
  Risk per trade    : 1% of current capital (this is what you lose if SL hits)
  Stop-loss distance: 0.25% of entry price
  Take-profit dist. : 0.50% of entry price (2:1 reward:risk)
  Leverage          : 10x

  Position size is derived so that a 0.25% adverse move = exactly 1% of
  capital lost:
      risk_amount = capital * 1%
      notional    = risk_amount / stop_loss_pct
      margin      = notional / leverage
      quantity    = notional / entry_price

100% PAPER TRADING - only public endpoints, no API key needed, no real orders.
"""

import time
import csv
import os
import json
from datetime import datetime, timezone

import requests

# ---------------- CONFIG ----------------
PAIR = "B-BTC_USDT"
RESOLUTION = "15"
CANDLE_DURATION_MS = 15 * 60 * 1000

STARTING_CAPITAL = 1000.0
RISK_PCT = 0.01            # 1% of capital risked per trade
SL_PCT = 0.0025            # 0.25% stop-loss distance
TP_PCT = 0.005              # 0.50% take-profit distance
LEVERAGE = 10
FEE_RATE = 0.00075          # CoinDCX standard crypto futures taker fee: 0.075%

HISTORY_HOURS = 240  # 10 days lookback - warms up CCI(20)+EMA(14) and price EMA(20)

CCI_PERIOD = 20
CCI_EMA_PERIOD = 14
PRICE_EMA_PERIOD = 20

STATE_FILE = "state.json"
TRADE_LOG_FILE = "btcusdt_paper_trades.csv"
CANDLES_URL = "https://public.coindcx.com/market_data/candlesticks"


# ---------------- DATA FETCH ----------------
def fetch_candles(lookback_hours):
    now = int(time.time())
    frm = now - lookback_hours * 3600
    params = {
        "pair": PAIR,
        "from": frm,
        "to": now,
        "resolution": RESOLUTION,
        "pcode": "f",
    }
    resp = requests.get(CANDLES_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    dedup = {c["time"]: c for c in data}
    return [dedup[t] for t in sorted(dedup)]


def only_closed_candles(candles):
    now_ms = int(time.time() * 1000)
    return [c for c in candles if c["time"] + CANDLE_DURATION_MS <= now_ms]


# ---------------- INDICATORS ----------------
def compute_ema(values, period):
    """EMA that tolerates leading None values (e.g. CCI isn't defined until
    enough candles exist). Starts once the first non-None value appears."""
    out = [None] * len(values)
    k = 2 / (period + 1)
    ema = None
    for i, v in enumerate(values):
        if v is None:
            continue
        ema = v if ema is None else v * k + ema * (1 - k)
        out[i] = ema
    return out


def compute_cci(candles, period):
    tp = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles]
    cci = [None] * len(tp)
    for i in range(period - 1, len(tp)):
        window = tp[i - period + 1:i + 1]
        sma = sum(window) / period
        mean_dev = sum(abs(x - sma) for x in window) / period
        cci[i] = 0.0 if mean_dev == 0 else (tp[i] - sma) / (0.015 * mean_dev)
    return cci


def add_indicators(candles):
    cci = compute_cci(candles, CCI_PERIOD)
    cci_ema = compute_ema(cci, CCI_EMA_PERIOD)
    price_ema = compute_ema([c["close"] for c in candles], PRICE_EMA_PERIOD)
    for i, c in enumerate(candles):
        c["cci"] = cci[i]
        c["cci_ema"] = cci_ema[i]
        c["price_ema"] = price_ema[i]
    return candles


# ---------------- STATE (persisted across runs) ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"capital": STARTING_CAPITAL, "position": None, "last_processed_time": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def init_trade_log():
    if not os.path.exists(TRADE_LOG_FILE):
        with open(TRADE_LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "entry_time", "exit_time", "side", "entry_price", "exit_price",
                "quantity", "pnl", "reason", "capital_after"
            ])


def log_trade(row):
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(row)


# ---------------- TRADE LOGIC ----------------
def open_position(state, side, price, ts):
    risk_amount = state["capital"] * RISK_PCT
    notional = risk_amount / SL_PCT
    margin = min(notional / LEVERAGE, state["capital"])  # safety clamp
    quantity = notional / price

    if side == "long":
        sl_price = price * (1 - SL_PCT)
        tp_price = price * (1 + TP_PCT)
        liq_price = price * (1 - 1 / LEVERAGE)
    else:
        sl_price = price * (1 + SL_PCT)
        tp_price = price * (1 - TP_PCT)
        liq_price = price * (1 + 1 / LEVERAGE)

    state["position"] = {
        "side": side,
        "entry_price": price,
        "entry_time": ts,
        "quantity": quantity,
        "margin": margin,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "liq_price": liq_price,
    }
    print(f"[{ts}] OPEN {side.upper()} @ {price:.2f} | qty={quantity:.6f} "
          f"| margin={margin:.2f} | sl={sl_price:.2f} | tp={tp_price:.2f}")


def close_position(state, price, ts, reason):
    pos = state["position"]
    direction = 1 if pos["side"] == "long" else -1
    gross_pnl = (price - pos["entry_price"]) * pos["quantity"] * direction

    entry_notional = pos["entry_price"] * pos["quantity"]
    exit_notional = price * pos["quantity"]
    fees = (entry_notional + exit_notional) * FEE_RATE

    pnl = gross_pnl - fees
    if reason == "liquidated":
        pnl = -pos["margin"]

    state["capital"] += pnl
    print(f"[{ts}] CLOSE {pos['side'].upper()} @ {price:.2f} | pnl={pnl:.2f} "
          f"| reason={reason} | capital={state['capital']:.2f}")

    log_trade([
        pos["entry_time"], ts, pos["side"], pos["entry_price"], price,
        pos["quantity"], round(pnl, 4), reason, round(state["capital"], 4)
    ])
    state["position"] = None


def check_stop_loss(state, current_price, ts):
    pos = state.get("position")
    if not pos:
        return
    if pos["side"] == "long" and current_price <= pos["sl_price"]:
        close_position(state, pos["sl_price"], ts, "stop_loss")
    elif pos["side"] == "short" and current_price >= pos["sl_price"]:
        close_position(state, pos["sl_price"], ts, "stop_loss")


def check_take_profit(state, current_price, ts):
    pos = state.get("position")
    if not pos:
        return
    if pos["side"] == "long" and current_price >= pos["tp_price"]:
        close_position(state, pos["tp_price"], ts, "take_profit")
    elif pos["side"] == "short" and current_price <= pos["tp_price"]:
        close_position(state, pos["tp_price"], ts, "take_profit")


def check_liquidation(state, current_price, ts):
    """Backstop only - SL (0.25%) is far tighter than liquidation (~10%),
    so this should essentially never fire in practice."""
    pos = state.get("position")
    if not pos:
        return
    if pos["side"] == "long" and current_price <= pos["liq_price"]:
        close_position(state, pos["liq_price"], ts, "liquidated")
    elif pos["side"] == "short" and current_price >= pos["liq_price"]:
        close_position(state, pos["liq_price"], ts, "liquidated")


def check_signals(prev_c, curr_c):
    if prev_c["cci"] is None or prev_c["cci_ema"] is None:
        return None
    if curr_c["cci"] is None or curr_c["cci_ema"] is None or curr_c["price_ema"] is None:
        return None

    bullish_cross = prev_c["cci"] <= prev_c["cci_ema"] and curr_c["cci"] > curr_c["cci_ema"]
    bearish_cross = prev_c["cci"] >= prev_c["cci_ema"] and curr_c["cci"] < curr_c["cci_ema"]

    price_above = curr_c["close"] > curr_c["price_ema"]
    price_below = curr_c["close"] < curr_c["price_ema"]

    if bullish_cross and price_above:
        return "enter_long"
    if bearish_cross and price_below:
        return "enter_short"
    if bullish_cross or bearish_cross:
        return "exit"
    return None


# ---------------- MAIN (one run) ----------------
def main():
    init_trade_log()
    state = load_state()
    is_first_run = state["last_processed_time"] == 0

    print(f"Loaded state: capital={state['capital']:.2f}, "
          f"position={'flat' if not state['position'] else state['position']['side']}")

    raw = fetch_candles(HISTORY_HOURS)
    if not raw:
        print("No candle data returned this run, skipping.")
        save_state(state)
        return

    current_price = float(raw[-1]["close"])
    ts_now = datetime.now(timezone.utc).isoformat()
    check_stop_loss(state, current_price, ts_now)
    check_take_profit(state, current_price, ts_now)
    check_liquidation(state, current_price, ts_now)

    closed = only_closed_candles(raw)
    if not closed:
        print("No closed candles yet, skipping.")
        save_state(state)
        return

    closed = add_indicators(closed)

    if is_first_run:
        state["last_processed_time"] = closed[-1]["time"]
        print("First run: indicators warmed up, no historical signals executed. "
              "Bot will start reacting to the next new candle.")
    else:
        new_candles = [c for c in closed if c["time"] > state["last_processed_time"]]
        start_idx = len(closed) - len(new_candles)

        for i in range(max(start_idx, 1), len(closed)):
            prev_c = closed[i - 1]
            curr_c = closed[i]
            ts = datetime.fromtimestamp(curr_c["time"] / 1000, tz=timezone.utc).isoformat()
            price = float(curr_c["close"])

            signal = check_signals(prev_c, curr_c)

            if state["position"] and signal == "exit":
                close_position(state, price, ts, "signal_exit")
                signal = check_signals(prev_c, curr_c)

            if not state["position"]:
                if signal == "enter_long":
                    open_position(state, "long", price, ts)
                elif signal == "enter_short":
                    open_position(state, "short", price, ts)

        state["last_processed_time"] = closed[-1]["time"]

    save_state(state)
    print(f"Run complete. Capital: {state['capital']:.2f} | "
          f"Position: {'flat' if not state['position'] else state['position']['side']}")


if __name__ == "__main__":
    main()
