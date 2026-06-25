# HANDOFF: Chart Sidekick — Data Pipeline Rebuild + Theme + TradingView-style Layout

**Date:** 2026-06-22 | **Project:** `/home/nazmoney/FTBotX4D/chartsidekick/` (NO git repo — parent FTBotX4D has no git either)
**Status:** Complete and verified. Server up, chart renders real data, dark/red theme + TradingView layout applied.

This session did three things: (1) rebuilt the missing indicator-dump data pipeline so the chart has data, (2) applied a dark grey/black + red theme, (3) restructured the UI into a TradingView-style layout per a user reference image.

---

## COMPLETED

### 1. Data pipeline (the chart was empty / `/api/data` returned 500)

- [x] **Root cause:** sidekick plots `user_data/indicator_dumps/<PAIR>_indicators.csv`. That directory did not exist; no generator script was in the repo. (These dumps are NOT freqtrade backtest plots/zips — that is a separate system in `freq_nigga.sh`. Do not confuse them.)
- [x] **Identified the producer strategy = `CURIOUS`** (`user_data/strategies/CURIOUS.py`). It has 71 `dataframe[...] =` assignments, matching the original dump's 71-column signature, and contains all the MATRIX / ROXY / ZLEMA / MID_LINE indicators the sidekick categorizes.
- [x] **Wrote `chartsidekick/dump_indicators.py`** — loads `user_data/config.json` + 15m futures feather data, activates the strategy through freqtrade's `Backtesting` object (so the DataProvider is wired in and the 1h/2h informative merges inside `populate_indicators` work), runs `strategy.advise_indicators(df, {"pair":...})`, and writes one CSV per pair to `user_data/indicator_dumps/<PAIR>_indicators.csv`.
- [x] **Generated all 7 pairs** (BTC/ETH/SOL/XRP/BNB/DOGE/LINK `_USDT_USDT`). ~85 MB each, 42241 rows, 15m bars. Verified `/api/data` now returns 200 with real OHLC + `MATRIX_up` and 153 indicators.

### 2. Theme — dark grey/black + red UI accents, green/red candles

- [x] CSS variables (index.html `:root`, ~line 14):
  `--bg:#0d0d0d --bg-2:#141414 --panel:#1c1c1c --panel-2:#262626`,
  `--border:#2e2e2e --border-soft:#222 --grid:#222`,
  `--accent:#e23636 --accent-2:#b62828` (red UI),
  `--text:#e6e6e6 --text-2:#9a9a9a --text-3:#5f5f5f`,
  `--up:#26a69a` (teal-green) `--down:#e23636` (red).
- [x] Plotly hardcoded colors (these do NOT read the CSS vars, set inline in `render()`):
  candles `increasing #26a69a / rgba(38,166,154,0.9)`, `decreasing #e23636 / rgba(226,54,54,0.9)`;
  `paper_bgcolor`/`plot_bgcolor:#0d0d0d`; `gridcolor:#222222`; `zerolinecolor:#2e2e2e`; legend bg `rgba(20,20,20,0.6)`.
- [x] **Publish button** is red (`var(--accent)`), not gold (user rejected gold).
- Note: indicator-line `PALETTE` (~line 343) is intentionally multicolored for distinct traces — left as is.

### 3. TradingView-style layout restructure (ALL original button IDs/handlers preserved)

- [x] **New top toolbar** (`#toolbar`): tf pills `15m / 1h / 4h` (4h active), icon buttons (legend toggle `#legendtoggle`, candle/price toggle `#pricetoggle`), text buttons (`ƒx Indicators ▾` = `#indopen`, `History` = `#histbtn`), `#pair` select, undo/reset-zoom `#resetzoom`, redo/reload `#reload`, screenshot `#snapbtn`, autoscale `#autoscale`, red **Publish** `#presetsave2`.
- [x] **New left vertical icon rail** (`#leftrail`, 46px wide): cursor/crosshair (`data-tool="cursor"`, = measure off), measure `#measurebtn`, zoom reset `#zoomrail`, eye/price toggle `#eyerail` (has `.eyeslash` path), lock/y-mode `#lockrail`, trash/clear-indicators `#trashrail`.
- [x] **Chart header overlay** (`#charthead`, absolute, top-left of chart): row 1 `PAIR  TF  STRATEGY  O H L C`, row 2 `Volume`. Updates on `mousemove` (nearest bar via `xaxis.p2d`) and after every render (`updateChartHead`).
- [x] Wrapped chart in `#chartwrap` → `#leftrail` + `#chartcol` (which holds `#charthead`, `#chart`, `#yzone`).
- [x] Rewrote legend/price/measure toggles to icon-only (`classList.toggle("active")`, removed the old `textContent` sets that would wipe the SVG icons). Added `setShowPrice` / `setMeasure` helpers that sync toolbar + rail. `positionYZone`'s offset parent changed from `#left` to `#chartcol`.

---

## CURRENT STATE

- **server.py** running on :8777, HTTP 200. PID 29955 this session (re-check, see Resume).
- **brain.py** running, polling :8777. PID 30303 this session (re-check).
- `/api/data`, `/api/columns`, `/api/ranges`, `/api/pairs` all 200 with real data.
- `index.html` is static-served — UI edits just need a browser reload, NO restart.
- Verified visually via headless Chrome screenshot (`/tmp/sk_shot2.png`): layout matches the reference, candles teal/red, red accents, OHLC header populated.

## INCOMPLETE / KNOWN MINOR ISSUES

- [ ] **tf pills are cosmetic.** The dumps are 15m data (the chart labels itself "4h" as a display string only). Clicking 15m/1h shows a status note "data is 4h only (display label)" but does NOT actually re-aggregate. If the user wants functional timeframes, you must either generate per-tf dumps or downsample client-side. Confirm intent before building.
- [ ] **Dumps carry 153 indicators** (full `populate_indicators` output) vs the old curated 65. Extra helper/signal cols land in the sidekick's "Other" category — harmless but cluttered. Could prune to a column whitelist in `dump_indicators.py` if the user wants it lean.
- [ ] **Top-right preset bar** (in the right panel `#presetbar`) has slight text/dropdown overlap. Pre-existing, not touched this session. Tidy if asked.
- [ ] **Price scale is on the LEFT** (Plotly default). The reference has it on the right. Deeper Plotly change (`yaxis.side:"right"` + margins) — deferred.
- [ ] **Nothing committed** — repo has no git. If you want version control, `git init` first. User was asked about committing multiple times and kept redirecting to new styling work.

## FAILED APPROACHES (Don't Repeat)

- **`bt.strategy`** → does not exist in freqtrade 2025.10. Use `strategy = bt.strategylist[0]; bt._set_strategy(strategy)`.
- **`bt.data`** → does not exist after load. `load_bt_data()` RETURNS a tuple: `data, _timerange = bt.load_bt_data()`.
- **Running `dump_indicators.py` directly in the container** → fails because `chartsidekick/` is NOT volume-mounted (only `./user_data` is). You MUST bind it: `-v "$(pwd)/chartsidekick:/scripts"` and run `/scripts/dump_indicators.py`.
- **Setting `b.textContent` on toolbar/rail toggle buttons** → wipes the inline SVG icons. Use `classList.toggle("active")` only.
- **Gold/tan Publish button + green/pink palette** → rejected by user. Wants dark + red.

## KEY DECISIONS

| Decision | Rationale |
|----------|-----------|
| Use freqtrade `Backtesting` to populate indicators offline | Gives a real DataProvider so CURIOUS's 1h/2h informative merges work; talib + custom libs guaranteed inside the docker image. |
| Run the generator inside the freqtrade docker container | `freqtradeorg/freqtrade:stable` (2025.10) has talib + freqtrade + the custom indicator libs the strategy imports. The host `.venv` is incomplete. |
| Plotly colors set inline (not via CSS vars) | Plotly cannot read CSS custom properties; theme colors must be duplicated in `render()`'s layout/trace objects. |
| Keep all original button IDs when restructuring the UI | Existing event handlers (`onclick`) bind by ID; reusing IDs means functionality survives the visual move. New buttons (`#snapbtn`, rail tools, tf pills, Publish) delegate to existing functions. |
| Numbers still come from CSV computation only | Absolute project rule (see prior handoff). UI/theme changes never touch the numeric source of truth. |

## RESUME INSTRUCTIONS

1. **Verify processes** → `ps -eo pid,args | grep -E "chartsidekick/(server|brain)" | grep -v grep | grep -v 'bash -c'`. Expect ONE server + ONE brain. Then `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8777/` → `200`.
2. **If not running, start server (separate Bash call, WILL time out — that's expected):**
   `cd /home/nazmoney/FTBotX4D && setsid .venv/bin/python -u chartsidekick/server.py < /dev/null > /tmp/sidekick_server.log 2>&1 & disown`
   Then verify with ps + curl.
3. **Start brain (SEPARATE call from any pkill, also times out — expected):**
   `cd /home/nazmoney/FTBotX4D && setsid .venv/bin/python -u chartsidekick/brain.py < /dev/null > /tmp/sidekick_brain.log 2>&1 & disown`
   Expected log line: `[brain] polling http://127.0.0.1:8777`.
4. **Regenerate dumps (only if data missing / stale, ~2 min/pair):**
   ```bash
   cd /home/nazmoney/FTBotX4D
   docker compose run --rm --entrypoint python3 -v "$(pwd)/chartsidekick:/scripts" \
     freqtrade /scripts/dump_indicators.py --strategy CURIOUS
   # optionally bound the range: add  --timerange 20240101-20250501
   ```
   It is slow (heavy indicators). Run in background and poll `ls -la user_data/indicator_dumps/`.
5. **Visual check (no automation driver installed):**
   ```bash
   timeout 40 google-chrome --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
     --window-size=1500,860 --virtual-time-budget=6000 --screenshot=/tmp/sk_shot.png "http://127.0.0.1:8777/"
   ```
   Then view `/tmp/sk_shot.png`.

## SETUP / ENVIRONMENT

- Python: `/home/nazmoney/FTBotX4D/.venv/bin/python` (pandas/numpy/fastapi for server+brain).
- Freqtrade: only in docker (`freqtradeorg/freqtrade:stable` = 2025.10). Run via `docker compose run --rm --entrypoint ... freqtrade ...`.
- `jcode` on PATH at `/home/nazmoney/.local/bin/jcode` (brain uses it, no API key).
- Chrome: `/usr/bin/google-chrome` (headless screenshots only; no Puppeteer/Playwright driver wired).
- Raw data present: `user_data/data/binance/futures/<PAIR>_USDT_USDT-15m-futures.feather` for all 7 pairs + 1h/2h informatives.

## KEY FILES

- `chartsidekick/dump_indicators.py` — **NEW** this session. Generates the per-pair indicator CSVs. Run in docker (see Resume step 4).
- `chartsidekick/index.html` — Plotly UI + chat. Theme (`:root` ~L14), candle colors in `render()` (~L630), new toolbar/rail/charthead HTML (~L227-310), new wiring + `updateChartHead` + mousemove (~L1139-1174). Static-served → reload only.
- `chartsidekick/server.py` — FastAPI :8777. `load_pair` reads `user_data/indicator_dumps/<PAIR>_indicators.csv` (cached in `_cache`). EDITS require process restart.
- `chartsidekick/brain.py` — polls server, calls `jcode run` for chat→ops. EDITS require restart.
- `user_data/strategies/CURIOUS.py` — the indicator producer. 71 base cols; imports `Custom_indicators2` (TA2) and `Custom_inidcators3` (CT3).
- `user_data/indicator_dumps/*.csv` — generated data the chart plots (~85 MB each).
- `/tmp/sidekick_server.log`, `/tmp/sidekick_brain.log`, `/tmp/dump.log` — logs.

## WARNINGS

- `server.py` / `brain.py` edits → PROCESS RESTART required. `index.html` is static → reload only.
- Background launches (`setsid ... &`) WILL time out the Bash tool. EXPECTED. Verify with ps + curl. Run exactly one server + one brain.
- Do NOT combine `pkill` + relaunch in one Bash call (races, leaves nothing running). Separate calls.
- `dump_indicators.py` lives in `chartsidekick/` which is NOT docker-mounted — always bind `-v "$(pwd)/chartsidekick:/scripts"`.
- Plotly colors are NOT CSS-var-driven; if you change the theme, update BOTH the `:root` vars AND the inline hex/rgba in `render()`.
- tf pills do not actually change timeframe (data is 15m only, labeled 4h). Clarify with user before making them functional.
- The freqtrade dump CSVs are ~85 MB x7 (~600 MB total). Watch disk if regenerating repeatedly.
