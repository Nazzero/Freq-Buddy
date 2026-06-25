# Chart Sidekick

One browser page: Plotly candlestick chart + a chat sidekick that controls it.
You stay in the browser. Talk to the bot, it drives the chart.

## Run

```bash
./chartsidekick/start.sh
# opens http://127.0.0.1:8777/
```

Starts two processes:
- `server.py` (FastAPI, port 8777) - serves the page + indicator data from
  `user_data/indicator_dumps/*.csv` + the chat bridge.
- `brain.py` - polls for chat messages, asks `jcode run` (your local Claude
  subscription, no API key) to turn them into chart actions, posts them back.

## What you can say

- "show MATRIX and vfi", "hide everything except price", "show only kama_mid"
- "zoom to feb 14 2024", "zoom to march", "reset zoom"
- "pan right", "switch to SOL"

The bot matches loose indicator names to real columns (e.g. "matrix" -> MATRIX).

## Architecture

```
browser (chart + chat)
   | POST /api/chat (msg + chart state)
   v
server.py  --(queue)-->  brain.py  --(jcode run)-->  Claude
   ^                          |
   | GET /api/chat/result     | POST /brain/reply {reply, actions}
   v
browser runs actions on chart via Plotly.restyle / relayout
```

Chart-control ops live in `index.html` `OPS{}`. The brain's allowed ops are
documented in `brain.py` `OPS_DOC`. Add a new capability = add to both.

## Status

v1 = chart control (show/hide indicators, zoom, pan, switch pair). Verified by
`verify.py` (headless: types a command, confirms chart reacts).

Not yet: reading raw values at a clicked bar, running backtests, editing
strategy files. Those are the planned next layers.

## Files

| File | Purpose |
|------|---------|
| `server.py` | FastAPI: page + data + chat bridge |
| `index.html` | SPA: chart + chat + JS op executor |
| `brain.py` | LLM loop turning chat into chart actions |
| `start.sh` | launch both + open browser |
| `verify.py` | headless end-to-end test |
