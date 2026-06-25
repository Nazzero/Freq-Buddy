# Handoff: Chart Sidekick + CDP Browser Control

**Date:** 2026-06-16
**Project:** `/home/nazmoney/FTBotX4D/chartsidekick/`
**Context:** FreqTrade crypto trading bot project (FTBotX4D). Built a browser-based "chart sidekick" — a single-page app with a Plotly candlestick chart (left) + AI chat sidebar (right) that controls the chart via natural language. The AI brain runs through the local jcode/Claude Max subscription (NO Anthropic API key). Goal: user reads charts and analyzes indicator patterns (65 MATRIX/ROXY indicators from CSV dumps) to design entry/exit signals, with an AI co-pilot that drives the chart.

---

## What Was Built (Chart Sidekick App)

A complete, working single-page app. Four files do the work:

### `server.py` (197 lines) — FastAPI backend, port **8777**
Serves the SPA + indicator data + a chat bridge. Endpoints:
- `GET /api/pairs` — list of 7 pairs (BTC/ETH/SOL/XRP/BNB/DOGE/LINK_USDT_USDT)
- `GET /api/columns?pair=` — `{ohlcv:[...], indicators:[...]}` (65 indicators)
- `GET /api/data?pair=&start=&end=&max_points=` — columnar OHLC+indicator JSON.
  **Downsamples** to `max_points` (default 4000) via groupby-bucket aggregation
  (open=first, high=max, low=min, close=last, volume=sum, indicators=last). NaN/inf
  sanitized to `null` via `math.isfinite`. Returns `_n_total` + `_downsampled` flags.
- `GET /api/ranges?pair=` — per-indicator value range bucket: `'0-1' | '0-100' | 'price' | 'other'`.
  Used by the frontend to auto-assign indicators to the right subplot by scale.
- Chat bridge: `POST /api/chat` (browser → queue), `GET /api/chat/result/{id}` (browser polls),
  `GET /brain/pending` (brain polls), `POST /brain/reply` (brain pushes reply+actions).

Data source: `/home/nazmoney/FTBotX4D/user_data/indicator_dumps/{PAIR}_indicators.csv`
(~45MB each, 46273 rows, 15m bars, Jan2024–May2025, 71 columns).

### `index.html` (470 lines) — the SPA (Plotly + chat + controls)
State model: `ACTIVE = Map(name -> {place, color})` where `place` = `"overlay"` or `"sub:N"`.
Features (ALL implemented + verified):
1. **Performance fix** — loads downsampled data (3500-bar target → ~3306 rendered from 46273).
   On zoom-in, a debounced (250ms) `plotly_relayout` handler re-fetches the visible slice at
   full detail. Reset-zoom reloads the full downsampled view. Fixed the ~5fps lag.
2. **Y-axis autoscale toggle** — `y-auto: on/off` button in toolbar. Off = freezes current
   y-ranges so panning doesn't rescale.
3. **Multiple subplots** — each shown indicator gets a placement; auto-grouped by `/api/ranges`
   bucket (0-1, 0-100, price→overlay, other). UI dropdown per indicator + "+ new sub". Renders
   stacked y-axes (price top 0.42–1.0, oscillators share lower 0–0.36, non-overlapping domains).
4. **Per-indicator color** — native color swatch on each row in the "Shown" panel.
5. **Favorites + presets** — ★ on each indicator chip (localStorage `cs_favs`), favs-only filter.
   Preset bar: save current combo (indicators+colors+subplots) by name, apply, delete
   (localStorage `cs_presets`).
6. **Organized indicator picker** — categorized (MATRIX, ROXY/RPXY, Moving Avg, MID LINE, Bands,
   Momentum, Trend/Regime, Other), collapsible, searchable. 65 indicators total.

JS `OPS{}` object = the chart-control actions the brain can call (see brain ops below).

### `brain.py` (144 lines) — the LLM loop (uses jcode, no API key)
Polls `/brain/pending`. For each message, builds a prompt (SYS + OPS_DOC + chart_state + user msg),
calls `jcode run --json --quiet --no-update <prompt>`, parses the returned JSON `{reply, actions}`,
posts to `/brain/reply`. ~4s response time. Brain ops (in `OPS_DOC`):
`set_pair, show_indicator{name,subplot?,color?}, hide_indicator, hide_all_indicators, show_only,
set_color, set_subplot, set_autoscale{on}, apply_preset, save_preset, zoom{start,end}, zoom_y,
reset_zoom, pan{fraction}`.

### `start.sh` — launches server + brain + opens browser
### `verify.py` — headless Playwright end-to-end test (types a chat command, asserts chart reacts)
### `README.md` — full docs

---

## Verification Done

- All API endpoints return 200, data sanitized (no NaN crashes).
- `/api/ranges` buckets correct: 32 price, 9 in 0-1, 1 in 0-100, 23 other. ROXY/MATRIX have wild
  ranges (-106..495, 4..258) → "other" subplot; MID_LINE/roxy_support are price-scale → overlay.
- **Headless Playwright verify.py PASSED**: typed "show only MATRIX and vfi, zoom to feb 14 2024",
  confirmed chart added both traces + zoomed to Feb 13-16.
- **Node harness** (`/tmp/harness.mjs`, mocks Plotly/DOM/localStorage) verified multi-subplot math:
  4 indicators → 4 distinct non-overlapping y-axes (MID_LINE→y/overlay, htf_trend→y2, RPXY→y3,
  ROXY→y4), xaxis anchors to bottom axis, autoscale on/off paths, presets snapshot/apply.
- Brain round-trip verified live: NL → correct indicator-name matching → correct actions.
- Screenshot via gnome-screenshot (DISPLAY=:0) confirmed app renders correctly in the real browser.

---

## Current Running State

- **Server (server.py)** RUNNING on port **8777** (pid 3046471). HTTP 200.
- **Brain (brain.py)** RUNNING (restarted 2026-06-17), log at `/tmp/sidekick_brain.log`. Restart:
  `cd /home/nazmoney/FTBotX4D && setsid .venv/bin/python -u chartsidekick/brain.py < /dev/null > /tmp/sidekick_brain.log 2>&1 &`
- **CDP Chromium** RUNNING on port **9333** (pid 3155706), app page loaded.
- App confirmed loaded and working in the user's browser (Chrome + Playwright Chromium).

## CDP Browser Control — DONE (live verification working)

The "next step (in progress)" from the prior session is complete. CDP-driven live
render verification is implemented and PASSES:

- **`chartsidekick/cdp_verify.py`** — connects to CDP page WS (9333), reads live
  `document.getElementById('chart').data`, drives `OPS.show_indicator` like the brain
  would, screenshots before/after to `chartsidekick/_cdp_out/`. PASS: 1→3 traces
  (price + MATRIX + vfi), confirmed visually.
- **`chartsidekick/cdp_chat_test.py`** — full end-to-end: types a NL command in the
  live page, clicks Send, waits for the brain to reply + drive the chart, screenshots.
  PASS: "show only ROXY and roxy_support, switch to SOL" → brain returned
  `set_pair SOL + show_only[ROXY,roxy_support]`, chart applied them.
- CDP client = plain python `websockets` (15.0.1, in `.venv`). No playwright needed
  for the drive loop. `Page.captureScreenshot` gives the rendered pixels.

## Feature Added This Session: Model Selection

User can now pick which jcode model drives the brain, and the chat shows which model
produced each reply.

- **server.py**: `list_models()` runs `jcode model list --no-update` (48 models cached).
  New routes `GET /api/models` → `{models, current}`, `POST /api/model {model}`. Selected
  model held in `_selected_model` (thread-locked, default `claude-sonnet-4-6`). Each
  `/api/chat` message carries the current model into `_pending`. `BrainReply` + the result
  route now pass through `model_used`.
- **brain.py**: `decide()` reads `msg["model"]`, appends `-m <model>` to the `jcode run`
  command, and returns `model_used` in the reply.
- **index.html**: `<select id="modelsel">` in the "Sidekick" header (styled to match dark
  theme). `loadModels()` populates it from `/api/models` and selects current; on change it
  POSTs `/api/model`. Each brain reply appends `   [model_used]` to the message.
- Verified over CDP: dropdown shows 48 models (default sonnet-4-6); selecting haiku →
  `POST /api/model` → server `current` updated; sending "show only MATRIX" → brain used
  `-m claude-haiku-4-5-20251001`, reply rendered `Showing MATRIX only.   [claude-haiku-4-5-20251001]`,
  action applied. Screenshot: `_cdp_out/model_feature.png`.

## Bug Fixed This Session

**`set_pair` dropdown desync.** `loadData(pair)` set `CUR_PAIR` and reloaded data but
never updated the `<select id="pair">` value. So when the brain issued `set_pair`, the
chart data switched correctly but the visible pair dropdown stayed on the old pair
(confirmed: `CUR_PAIR=SOL` while `select.value=BTC`). Fixed in `index.html` `loadData()`
by syncing `psel.value=pair`. Re-verified over CDP: `set_pair XRP` → both CUR_PAIR and
dropdown = XRP.

---

## Key Process-Persistence Learnings (important for next session)

- Plain `setsid ... &` or `nohup ... &` from agent Bash often does NOT persist — the process dies
  when the Bash call returns (Bash holds the pipe / child gets reaped).
- **What works:** `bash chartsidekick/start.sh` (uses setsid internally, reliably starts both),
  OR launch via the `run_in_background:true` Bash tool with `exec`.
- For the CDP chromium, the launch that finally stuck used `DISPLAY=:0 setsid ... &` with explicit
  flags + an 8s sleep before checking the port. Earlier attempts with a stale `--user-data-dir`
  lock (`/tmp/chromium-sidekick/Singleton*`) failed to bind 9333.
- Regular Chrome already runs CDP on **9222** (perplexity.ai) — don't collide; ours uses **9333**.

---

## User Preferences

- Everything in ONE browser page — no CLI, no tab switching. Chat sidekick controls the chart.
- jcode/Claude Max subscription as the brain — NO Anthropic API key.
- Wants live browser verification (tried Firefox-via-Jcode first → blocked → use Playwright chromium).
- Casual, terse communication.

## Planned Next Layers (not built yet)

- Click a bar → read raw indicator values at that x (brain answers "what's vfi here").
- Screenshot-to-bot so the AI sees the chart visually (now feasible via CDP Page.captureScreenshot).
- Run backtests / edit strategy `.py` from chat (the 10% backtest part of the workflow).
- Repo is NOT a git repo (`/home/nazmoney/FTBotX4D` has no .git), so no commits were made.
