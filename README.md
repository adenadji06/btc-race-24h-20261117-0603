# btc-race-24h-20261117-0603 — strong-move trend bot

Long-only cbBTC/USDC trend bot for `agent-fun/bars-replay/v1`, Python 3.13 stdlib only.

Entry: EMA(8) > EMA(21), close > EMA(120), 15-min return > 0.8%, 60-min return > 1.2%.
Exit: EMA cross back, 0.8% trailing stop, 0.8% hard stop, or 2.0% take-profit.
30-bar cooldown after exits; no entries in the last 60 bars; relaxed momentum
bars (0.6% / 0.5%) after bar 1200 if nothing has traded yet.

Dev window (kit dataset, 1440 bars): net PnL **+$24.06**, 2 fills (1 round trip),
max drawdown 0.5%. License: MIT.
