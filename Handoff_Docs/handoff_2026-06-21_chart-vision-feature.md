# HANDOFF: Chart Sidekick — AI Vision Feature (AI sees & controls the chart)

**Date:** 2026-06-21 | **Agent:** Claude Opus 4.6 | **Branch:** main (NOTE: `main` is the parent repo FTBotX4D; `chartsidekick/` itself has NO git repo) | **Status:** Complete (vision pipeline shipped + verified end-to-end)
**Goal:** Let the AI quant-analyst SEE the user's live chart (screenshots) and CONTROL it (zoom both axes, jump to a date, hide/show indicators, scroll, and re-look) so it has live visual context during chat. Absolute rule preserved: every number still comes from real CSV computation, never the LLM.

---

## COMPLETED

- [x] **Screenshot upload endpoint** -> `chartsidekick/server.py` `POST /api/screenshot` | Tested: decodes base64 PNG (tolerates `data:` URL prefix), saves `chartsidekick/shots/<id>.png`, prunes to newest ~40, returns `{ok, path}` (absolute path).
- [x] **`import base64` + `SHOT_DIR` + `ShotMsg` model** -> `chartsidekick/server.py` | Implemented.
- [x] **Vision prompt + new ops in OPS_DOC/SYS** -> `chartsidekick/brain.py` | `jump_to_date {date, bars?}` and `request_screenshot {}` documented; SYS has a VISION paragraph telling the model to Read the screenshot path and ground answers in what it sees.
- [x] **`decide()` attaches screenshot path** -> `chartsidekick/brain.py` ~line 463 | Reads `state.get("screenshot_path")`; if file exists, appends `shot_note` (image path) to the jcode prompt.
- [x] **CRITICAL BUG FIX: jcode `--json` junk-prefix parse** -> `chartsidekick/brain.py` `run_jcode()` lines ~219-244 | When jcode Reads an image, its stdout gets a garbage prefix (echoes base64 image bytes like `_Ga=T,f=100,...iVBORw0K...==\`) BEFORE the JSON envelope, breaking `json.loads(raw)` -> empty replies. Fixed: scan for the FIRST well-formed JSON object via `json.JSONDecoder().raw_decode()` loop instead of parsing the whole stream.
- [x] **jcode timeout bump 120s -> 180s** -> `chartsidekick/brain.py` `run_jcode()` line ~219 | Image reads are slower.
- [x] **Frontend `captureChart()`** -> `chartsidekick/index.html` ~line 675 | `Plotly.toImage(gd,{format:'png',width,height,scale:1})` -> `POST /api/screenshot` -> returns server path; null on failure (chat still works text-only).
- [x] **Frontend `chatTurn(text, pend, shotPath)`** -> `chartsidekick/index.html` ~line 689 | Posts text + chart_state (+ `screenshot_path`), polls `/api/chat/result/{id}`, renders reply, applies ops; returns the result object.
- [x] **Frontend `send()` rewritten + re-look loop** -> `chartsidekick/index.html` ~line 711 | Captures a screenshot before each turn; if AI emits `request_screenshot`, captures a fresh PNG and continues the SAME investigation (capped at 3 hops via `wantsScreenshot()`).
- [x] **Frontend OPS handlers** -> `chartsidekick/index.html` `OPS` object ~line 624 | `jump_to_date` (centers view on ISO date, ~`bars`*15min window, default 200 bars) + `request_screenshot` (no-op; handled by chat loop).
- [x] **Y-axis zoom strip** (prior session, still live) -> `chartsidekick/index.html` `#yzone` | 26px left-edge drag-strip, drag up = zoom in, down = zoom out; double-click resets to auto.

## INCOMPLETE

- N/A (vision feature itself is complete and verified). Older backlog items remain (see "Future").

## CURRENT STATE

- **Working:**
  - server.py running on :8777, HTTP 200 (PID was 703196 this session — re-check, see Resume).
  - brain.py running, polling :8777 (PID was 717262 this session — re-check).
  - `/api/screenshot` saves PNG + returns abs path. Verified with `/tmp/redbox.png`.
  - End-to-end vision: screenshot path -> brain -> jcode Reads image -> correct answer. Verified: asked "what color fills the attached screenshot" with redbox -> `reply:"Red."`.
  - `request_screenshot` op flows back to frontend `actions` array. Verified.
  - All 9 OPS_DOC ops exist in index.html OPS (`zoom, zoom_y, pan, reset_zoom, show_indicator, hide_indicator, mark, jump_to_date, request_screenshot`).
  - JS validates clean (`node --check`).
- **Broken:** Nothing known. The re-look loop and `jump_to_date` ops were unit-validated and syntactically checked but have NOT been exercised through a real browser session with a live Plotly chart (only the redbox image + a synthetic chart_state were used). See Warnings.

## FAILED APPROACHES (Don't Repeat)

- **`json.loads(raw)` on jcode stdout when an image is read** -> Failed: stdout has a garbage base64-echo prefix before the `{...}` envelope, so the parse fell through to `text=raw`, then `extract_json`'s `\{.*\}` regex matched the OUTER envelope `{session_id,...,text,usage}` (which has no `"reply"` key) -> empty reply. Use the `raw_decode` scan-for-first-valid-object approach now in `run_jcode`.
- **Combining `pkill` + `setsid ... &` + restart in ONE Bash call** -> Failed: the `&` backgrounds inside a bash that then exits, racing/killing the child; log showed stale content and no running process. Use a SEPARATE clean launch call (the launch will time out the Bash tool — that's EXPECTED — then verify with `ps` + `curl`).
- **Corner-drag zoom handle (`#czone`) and separate x/y zoom strips** (prior sessions) -> Rejected by user (unwanted resize cursor / not TradingView-like). Settled on Y-axis-only left-edge strip. Do not re-add czone.

## KEY DECISIONS

| Decision | Rationale |
|----------|-----------|
| Pass screenshot as a FILE PATH in the jcode prompt (no `--image` flag) | jcode has no image CLI flag; the agent's Read tool loads the path itself. Proven working with `/tmp/redbox.png`. |
| `run_jcode` scans for first valid JSON object via `raw_decode` | jcode `--json` leaks base64 bytes to stdout on image reads; tolerate junk prefix without depending on an upstream fix. |
| Re-look loop capped at 3 hops | Prevent infinite screenshot/continue loops while allowing the AI to investigate (zoom -> look -> zoom again). |
| `jump_to_date` assumes 15-min bars for the window math | Matches the dataset's bar interval; window = `bars*15*60*1000` ms centered on the date. |
| Numbers still come from `/api/analyze` CSV computation only | Absolute project rule; vision only adds visual context, never numeric authority. |

## RESUME INSTRUCTIONS

Step-by-step for the next agent:

1. **Verify processes** -> `ps -eo pid,etimes,args | grep -E "chartsidekick/(server|brain)" | grep -v grep | grep -v 'bash -c'` | Expected: exactly ONE server + ONE brain. Then `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8777/` -> `200`.
2. **If not running, start server** -> `cd /home/nazmoney/FTBotX4D && setsid .venv/bin/python -u chartsidekick/server.py < /dev/null > /tmp/sidekick_server.log 2>&1 & disown` | The Bash tool call WILL time out — expected. Verify separately with ps+curl.
3. **Start brain** -> `cd /home/nazmoney/FTBotX4D && setsid .venv/bin/python -u chartsidekick/brain.py < /dev/null > /tmp/sidekick_brain.log 2>&1 & disown` | Same timeout-then-verify pattern. Expected log: `[brain] polling http://127.0.0.1:8777`.
4. **Smoke-test vision (no browser needed)** -> post a chat with a known image path (see Verification) | Expected: `reply:"Red."`.
5. **REAL browser test (the one thing not yet done)** -> open `http://127.0.0.1:8777/` in Chrome (`/usr/bin/google-chrome`), pick a pair, zoom/hide a trace, then ask the AI something visual ("what's happening on the chart right now?"). Confirm: a PNG lands in `chartsidekick/shots/`, the AI's reply references what's visible, and any `jump_to_date`/`request_screenshot` ops actually move the chart + trigger a re-look. No JS driver/puppeteer installed — drive manually or wire one up.

**Future (once unblocked — older backlog):**

- [ ] **"Show raw engine JSON" toggle** -> verify it still works.
- [ ] **Persist model selection** -> across reloads.
- [ ] **Compare across pairs** -> run same edge on multiple pairs.
- [ ] **Save named edge** -> persist a user's edge definition.

Verification (one-shot vision smoke test, run from `/home/nazmoney/FTBotX4D`):
```bash
# ensure a test image exists; redbox was at /tmp/redbox.png this session
B64=$(base64 -w0 /tmp/redbox.png)
P=$(curl -s -X POST http://127.0.0.1:8777/api/screenshot -H "Content-Type: application/json" -d "{\"png_b64\":\"$B64\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['path'])")
ID=$(curl -s -X POST http://127.0.0.1:8777/api/chat -H "Content-Type: application/json" \
  -d "{\"text\":\"What color fills the attached screenshot? One word.\",\"chart_state\":{\"pair\":\"TEST\",\"screenshot_path\":\"$P\"}}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
for i in $(seq 1 75); do R=$(curl -s http://127.0.0.1:8777/api/chat/result/$ID); echo "$R" | grep -q '"ready":true' && { echo "$R"; break; }; sleep 2; done
# Expected: {"ready":true,"reply":"Red.",...}
```

## HOW IT WORKS

- **Flow:** Browser `send()` -> `captureChart()` (Plotly.toImage PNG) -> `POST /api/screenshot` (server saves to `shots/`, returns abs path) -> `chatTurn()` `POST /api/chat {text, chart_state{...,screenshot_path}}` -> server queues -> brain polls `/brain/pending` -> `decide()` builds prompt with image path -> `jcode run --json --quiet --no-update [-m model] <prompt>` (agent Reads the PNG) -> brain parses reply+actions -> `POST /brain/reply` -> browser polls `/api/chat/result/{id}` -> renders reply, runs OPS -> if `request_screenshot` op present, re-capture + continue (max 3 hops).
- **State/Storage:** Screenshots on disk at `chartsidekick/shots/<id>.png` (pruned to ~40 newest). Chat state in-memory in server. NO database. `index.html` static-served. CSV indicator data is the numeric source of truth (via `/api/analyze`).

## SETUP REQUIRED

- Python: use `/home/nazmoney/FTBotX4D/.venv/bin/python` (has pandas/numpy/fastapi). `jcode` on PATH.
- Node (for JS validation): `~/.nvm/versions/node/v22.22.2/bin/node`.
- Chrome: `/usr/bin/google-chrome` (no JS automation driver installed).
- A test image for the smoke test (redbox PNG was `/tmp/redbox.png`; recreate any solid-color PNG if gone).

## CODE CONTEXT

```python
# server.py
class ShotMsg(BaseModel):
    png_b64: str
# POST /api/screenshot -> {"ok": True, "path": "<abs path to shots/<id>.png>"}

# brain.py run_jcode() — junk-tolerant parse (the critical fix):
out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
raw = out.stdout.strip()
j = None; i = raw.find("{")
if i != -1:
    dec = json.JSONDecoder()
    while i != -1:
        try:
            j, _ = dec.raw_decode(raw, i)
            if isinstance(j, dict) and ("text" in j or "message" in j or "output" in j):
                break
            j = None
        except json.JSONDecodeError:
            pass
        i = raw.find("{", i + 1)
text = (j.get("text") or j.get("message") or j.get("output") or raw) if isinstance(j, dict) else raw

# brain.py decide() — attach screenshot:
shot = state.get("screenshot_path")
if shot and os.path.exists(shot):
    shot_note = "\n\nCHART SCREENSHOT: ... Image path: " + shot + "\n"
# OPS_DOC adds: jump_to_date {date, bars?}  and  request_screenshot {}
```

```javascript
// index.html
async function captureChart()            // Plotly.toImage -> POST /api/screenshot -> returns path|null
async function chatTurn(text, pend, shot)// posts text+chart_state(+screenshot_path), polls, renders, runs ops
function  wantsScreenshot(actions)       // actions.some(a => a.op === "request_screenshot")
// send(): capture -> chatTurn -> while(wantsScreenshot && hop<3){ recapture; chatTurn("(continue...)") }
// OPS.jump_to_date(a): center xaxis on a.date, window = a.bars*15min (default 200)
// OPS.request_screenshot: no-op (loop handles re-look)
```

## WARNINGS

- `chartsidekick/server.py` & `chartsidekick/brain.py` -> edits require a PROCESS RESTART. `index.html` is static-served -> just reload the page.
- Background launches (`setsid ... &`) -> WILL time out the Bash tool. That is EXPECTED. Never assume failure; verify with `ps` + `curl`. Run EXACTLY one server + one brain.
- Restarting brain: do it in a SEPARATE Bash call from the `pkill`. Combining pkill+launch in one call races and leaves no process running (hit this twice this session).
- Bash `timeout` arg is in MILLISECONDS in this harness.
- jcode `--json` stdout can be prefixed with base64 garbage on image reads -> handled in `run_jcode`, but if you see empty replies again, dump `out.stdout` and check for new corruption shapes. (Worth fixing upstream in jcode so it doesn't leak image bytes to stdout.)
- The re-look loop + `jump_to_date` are NOT yet exercised against a live browser Plotly chart (only synthetic state). Validate in a real browser before claiming the full UX works.
- Plotly `render()` uses `Plotly.react`; layout `shapes`/`annotations` from relayout are NOT auto-preserved across react (already re-injected for `_meas` measure shapes). Keep this in mind if adding any screenshot-related layout.
- ZLEMA is treated as the user's price line; real OHLC candles are hideable and STAY hidden through pan/zoom. Don't un-hide them.

## KEY FILES

- `chartsidekick/server.py` -> FastAPI on :8777, deterministic edge engine, `/api/analyze`, `/api/chat`, `/api/screenshot`, `/brain/*`, static-serves `index.html`.
- `chartsidekick/brain.py` -> polls server, calls `jcode run` to decide reply + ops + write verdict prose; junk-tolerant jcode parse; vision prompt.
- `chartsidekick/index.html` -> Plotly UI, chat, OPS object, Y-zoom strip, screenshot capture + re-look loop.
- `chartsidekick/shots/` -> saved chart screenshots (pruned to ~40 newest).
- `/tmp/sidekick_server.log`, `/tmp/sidekick_brain.log` -> process logs.
