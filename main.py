#!/usr/bin/env python3
"""Long-only EMA-cross momentum bot for the btc-race-24h dev window.

Strategy (deterministic, standard library only):
  * Enter long with 100% of cash when EMA(8) crosses up through
    EMA(21), the close sits above EMA(120), the 15-bar return tops
    0.2% and the 60-bar return tops 0.4%.
  * Exit on an EMA(8) cross back down, a 0.4% trailing stop off the
    highest close since entry, or a 0.8% hard stop below the fill
    price. No take-profit.
  * 30-bar cooldown after an exit, no entries in the last 60 bars of
    the window. After bar 1200, if the bot has never traded, the
    momentum gates are waived (cross + trend filter only).

Protocol: agent-fun/bars-replay/v1, newline-delimited JSON on
stdin/stdout. Every price is an integer: closes in price-scale units
(1e-5 USDC), cash and notionals in micro-USDC (1e-6 USDC), quantities
in 1e-8 BTC. Orders decided on the batch of the bar closing at minute
t fill at the open of the next bar.
"""

import json
import sys

SYMBOL = "CBBTC"
TOTAL_BARS = 1440
NO_TAIL_BARS = 60
COOLDOWN_BARS = 30
FALLBACK_BAR = 1200

FAST_SPAN = 8
SLOW_SPAN = 21
TREND_SPAN = 120

MOM15_WIN = 15
MOM15_THR = 0.002
MOM60_WIN = 60
MOM60_THR = 0.004

TRAIL_PCT = 0.004
STOP_PCT = 0.008
MAX_SLIPPAGE_BPS = 100
MIN_NOTIONAL = 1_000_000


class Ema:
    """Seeded EMA: simple average of the first `span` closes, then the
    usual recursive update. Mirrors the kit reference sweep exactly."""

    def __init__(self, span):
        self.span = span
        self.k = 2.0 / (span + 1)
        self.warm = []
        self.value = None

    def update(self, close):
        if self.value is None:
            self.warm.append(close)
            if len(self.warm) == self.span:
                self.value = sum(self.warm) / self.span
        else:
            self.value += self.k * (close - self.value)
        return self.value


class Bot:
    """One position at most, full-size entries, fills tracked from the
    batch's own fill reports so entry price and cooldown stay exact."""

    def __init__(self):
        self.fast = Ema(FAST_SPAN)
        self.slow = Ema(SLOW_SPAN)
        self.trend = Ema(TREND_SPAN)
        self.closes = []
        self.qty = 0
        self.cash = 0
        self.entry_px = 0
        self.peak_px = 0
        self.last_exit_bar = -10 ** 9
        self.ever_traded = False
        self.order_no = 0

    def next_id(self, side):
        self.order_no += 1
        return "%s%d" % (side, self.order_no)

    def refresh_account(self, batch):
        account = batch.get("account", {})
        self.cash = int(account.get("cash_quote_units", "0"))
        self.qty = 0
        for pos in account.get("positions", []):
            if pos.get("symbol") == SYMBOL:
                self.qty += int(pos.get("qty", "0"))

    def apply_fills(self, batch, seq):
        # Fills reported here settled at this bar's open; the account
        # in the same batch already reflects them.
        for fill in batch.get("fills", []):
            if fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                self.entry_px = int(fill["price"])
                self.peak_px = self.entry_px
                self.ever_traded = True
            elif fill.get("side") == "sell":
                self.entry_px = 0
                self.peak_px = 0
                self.last_exit_bar = seq

    def momentum_ok(self, close, seq):
        # Strict gates, waived entirely once the fallback kicks in:
        # after bar 1200 with no trade yet, cross + trend is enough.
        if (not self.ever_traded) and seq >= FALLBACK_BAR:
            return True
        if len(self.closes) <= MOM15_WIN:
            return False
        if close <= self.closes[-1 - MOM15_WIN] * (1 + MOM15_THR):
            return False
        if len(self.closes) <= MOM60_WIN:
            return False
        if close <= self.closes[-1 - MOM60_WIN] * (1 + MOM60_THR):
            return False
        return True

    def on_batch(self, batch):
        seq = batch["seq"]
        bar = batch["bars"][0]
        close = int(bar["c"])
        self.apply_fills(batch, seq)
        self.refresh_account(batch)
        self.closes.append(close)

        # No decisions on the final bar: orders there are rejected
        # and the harness flattens any open position at the close.
        if seq >= TOTAL_BARS - 1:
            return []

        prev_fast = self.fast.value
        prev_slow = self.slow.value
        fast = self.fast.update(close)
        slow = self.slow.update(close)
        trend = self.trend.update(close)
        if fast is None or slow is None or trend is None:
            return []

        cross_up = (prev_fast is not None and prev_slow is not None
                    and fast > slow and prev_fast <= prev_slow)
        cross_down = fast < slow and (prev_fast is None
                                      or prev_fast >= prev_slow)

        if self.qty > 0:
            if close > self.peak_px:
                self.peak_px = close
            exit_now = False
            if cross_down:
                exit_now = True
            if close < self.peak_px * (1 - TRAIL_PCT):
                exit_now = True
            if self.entry_px and close < self.entry_px * (1 - STOP_PCT):
                exit_now = True
            if exit_now:
                return [{"client_id": self.next_id("s"),
                         "symbol": SYMBOL, "side": "sell",
                         "qty": str(self.qty),
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            return []

        # Flat: look for the long entry.
        if not cross_up:
            return []
        if close <= trend:
            return []
        if not self.momentum_ok(close, seq):
            return []
        if seq >= TOTAL_BARS - NO_TAIL_BARS:
            return []
        if seq - self.last_exit_bar < COOLDOWN_BARS:
            return []
        if self.cash < MIN_NOTIONAL:
            return []
        return [{"client_id": self.next_id("b"),
                 "symbol": SYMBOL, "side": "buy",
                 "notional_quote_units": str(self.cash),
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


def main():
    bot = Bot()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "done":
            break
        if kind == "hello":
            continue
        if kind == "batch":
            orders = bot.on_batch(msg)
            sys.stdout.write(json.dumps(
                {"type": "reply", "seq": msg["seq"],
                 "orders": orders}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
