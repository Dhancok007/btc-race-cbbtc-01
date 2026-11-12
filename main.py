"""cbBTC/USDC 1-minute trend bot for make.fun's bars-replay/v1.

Strategy (adapted from a defensive EMA design, retuned for this market):
a round trip costs ~26 bps in fees, slippage and impact, so the bot only
rides moves that are already established and likely to exceed that.

- Trend: EMA(20) over EMA(60) of 1-minute closes, both seeded as the mean
  of their first span of closes, then the usual 2/(span+1) recursion.
- Entry (flat, past warm-up, cooldown over): EMA fast above slow AND the
  close at least 25 bps above its level 30 bars ago AND the fast EMA at
  least 10 bps above the slow one (a real gap, not a touch). Buys 90% of
  cash; sizing under the cap, sized against the latest close depth.
- Exit (whole position, first match wins):
  * trend break: EMA fast below slow;
  * stop: position worth 3% under its cost;
  * trail: after the position has been worth 2% over cost, a fall of
    1.2% from its best close exits;
  * stale: 480 bars held without the trail ever arming exits at market
    if the position is not above cost.
- Cooldown: 15 bars after any exit before a new entry.
- Every order caps slippage at 100 bps past the deciding close.

Deterministic: floats only in fixed IEEE order, no clock, no randomness,
no files. Runs through the vendored bars SDK shim (vendor/makefun_bars.py,
a byte-for-byte copy of the kit's sdk/makefun_bars.py).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
FAST_SPAN = 20
SLOW_SPAN = 60
MOMENTUM_BARS = 30
MOMENTUM_MIN_BPS = 25.0
GAP_MIN_BPS = 10.0
STOP_LOSS = 0.97          # exit at 3% under cost
TAKE_PROFIT = 1.009       # bank a burst at +0.9% over cost
TRAIL_ARM = 1.02          # arm the trail at +2% over cost
TRAIL_GIVE = 0.988        # exit 1.2% under the best close once armed
STALE_BARS = 480          # give up on an unarmed position after 8 hours
COOLDOWN_BARS = 15
BUY_FRACTION = 0.90
MAX_SLIPPAGE_BPS = 100


class Ema:
    def __init__(self, span):
        self.span = span
        self.warm = []
        self.value = None

    def step(self, x):
        if self.value is not None:
            self.value += (2.0 / (self.span + 1)) * (x - self.value)
            return
        self.warm.append(x)
        if len(self.warm) == self.span:
            self.value = sum(self.warm) / self.span


class Bot:
    def __init__(self):
        self.fast = Ema(FAST_SPAN)
        self.slow = Ema(SLOW_SPAN)
        self.closes = []
        self.entry_bar = None      # seq at which the position was decided in
        self.best_value_ratio = 0.0  # best (value / cost) seen for the position
        self.armed = False
        self.cooldown_until = -1
        self.hold_bars = 0

    def on_batch(self, state, batch):
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == SYMBOL:
                bar = b
        if bar is None:
            return []
        close = int(bar["c"])
        self.closes.append(close)
        self.fast.step(float(close))
        self.slow.step(float(close))
        if state.warming_up:
            return []

        held = state.positions.get(SYMBOL)
        seq = state.seq

        if held is not None:
            self.hold_bars += 1
            value = state.value(SYMBOL, held.qty)
            ratio = value / held.cost if held.cost else 0.0
            if ratio > self.best_value_ratio:
                self.best_value_ratio = ratio
            if ratio >= TRAIL_ARM:
                self.armed = True
            exit_now = False
            if self.fast.value is not None and self.slow.value is not None \
                    and self.fast.value < self.slow.value:
                exit_now = True                      # trend break
            elif ratio >= TAKE_PROFIT:
                exit_now = True                      # bank the burst
            elif ratio <= STOP_LOSS:
                exit_now = True                      # stop loss
            elif self.armed and ratio <= self.best_value_ratio * TRAIL_GIVE:
                exit_now = True                      # trailing take-profit
            elif not self.armed and self.hold_bars >= STALE_BARS and ratio <= 1.0:
                exit_now = True                      # stale, not working
            if exit_now:
                self.cooldown_until = seq + COOLDOWN_BARS
                self.entry_bar = None
                self.best_value_ratio = 0.0
                self.armed = False
                self.hold_bars = 0
                return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            return []

        # Flat: look for an entry.
        if seq <= self.cooldown_until:
            return []
        if self.fast.value is None or self.slow.value is None:
            return []
        if len(self.closes) <= MOMENTUM_BARS:
            return []
        if self.fast.value <= self.slow.value:
            return []
        gap_bps = (self.fast.value / self.slow.value - 1.0) * 10_000.0
        if gap_bps < GAP_MIN_BPS:
            return []
        past = self.closes[-MOMENTUM_BARS - 1]
        momentum_bps = (close / past - 1.0) * 10_000.0 if past else 0.0
        if momentum_bps < MOMENTUM_MIN_BPS:
            return []
        room = state.weight_room(SYMBOL)
        spend = int(state.cash * BUY_FRACTION)
        if spend > room:
            spend = room
        min_notional = int(state.hello["fees"]["min_notional_quote_units"])
        if spend < min_notional:
            return []
        self.entry_bar = seq
        self.best_value_ratio = 0.0
        self.armed = False
        self.hold_bars = 0
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": spend,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


if __name__ == "__main__":
    makefun_bars.run(Bot().on_batch)
