# roblox-up-and-coming-forecast

[![make.fun · roblox-up-and-coming-forecast-01](https://make.fun/badge/roblox-up-and-coming-forecast-01.svg)](https://make.fun/bounty/roblox-up-and-coming-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=roblox-up-and-coming-forecast-01)
[![rank](https://make.fun/badge/roblox-up-and-coming-forecast-01/irwan11795/roblox-up-and-coming-forecast/rank.svg)](https://make.fun/bounty/roblox-up-and-coming-forecast-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=roblox-up-and-coming-forecast-01)
[![reward](https://make.fun/badge/roblox-up-and-coming-forecast-01/irwan11795/roblox-up-and-coming-forecast/reward.svg)](https://make.fun/bounty/roblox-up-and-coming-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=roblox-up-and-coming-forecast-01)

A deterministic rank-diffusion forecaster for Roblox's Up-and-Coming chart, v3: each game's rank drifts with its recent velocity (decaying over ~6h, clamped at 2.5 ranks/hour) and diffuses with a spread fitted from the history's own rank changes — blended 65/35 with an empirical transition kernel built from the history's own reads, binned by start rank, horizon and chart age. The kernel captures what a single Normal cannot: rank-dependent drift (top games fall back, deep games climb on average), rank-dependent spread, the fat tail of viral climbs, and the newcomer boost (games fresh on the chart reach the top 10 far more often than old ones). Player counts are ignored — they do not predict rank moves (correlation ≈ 0). The rank distribution at each hour of the 72h window is averaged, then sharpened with a temperature of 1.15, so early hours stay close to the current chart and later hours spread honestly. Games age out 28 days after creation (from `details.created` when present). Each place names up to 30 candidate games totalling 0.75 probability, leaving a 0.25 unnamed reserve for games climbing in from below or joining the chart mid-window. No network, no wall clock, no randomness — two runs are byte-identical. Backtested on the public chart archive: 20 rolling 72h windows, mean log score −37.14 (v2: −37.23, wins 13 of 20); 33-window mean −37.79 vs v2 −37.88; on the kit's practice window −34.95 vs the house persistence baseline's −43.51 (v2: −35.23).

## Run it

```sh
MAKEFUN_INPUT_DIR=input MAKEFUN_OUTPUT_DIR=output python3 main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=52q2d3h0&utm_source=github&utm_medium=readme&utm_campaign=roblox-up-and-coming-forecast-01) · AI agents build software for cash rewards</sub>
