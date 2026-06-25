#!/usr/bin/env python3
"""
Chart Sidekick brain loop.

Polls the server for chat messages, asks `jcode run` to decide a reply +
chart actions (as strict JSON), posts the result back. Uses the local jcode
subscription as the LLM, so no API key.

Run:  python3 chartsidekick/brain.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import traceback

import urllib.request

BASE = "http://127.0.0.1:8777"
POLL_SEC = 0.6

OPS_DOC = """
Available chart actions (op + args). Return only ops you actually need.
- set_pair {pair}                      switch pair. pairs: BTC_USDT_USDT ETH_USDT_USDT SOL_USDT_USDT XRP_USDT_USDT BNB_USDT_USDT DOGE_USDT_USDT LINK_USDT_USDT
- show_indicator {name, subplot?, color?}  add indicator. subplot: int (0,1,2..) puts it in that stacked subplot; omit = auto by scale. color: "#rrggbb"
- hide_indicator {name}                remove one indicator
- hide_all_indicators {}               clear all indicators
- show_only {names:[...]}              clear then show exactly these
- set_color {name, color}              recolor a shown indicator, color "#rrggbb"
- set_subplot {name, subplot}          move a shown indicator to subplot N (0-based); or {name, place:"overlay"}
- set_autoscale {on}                   y-axis autoscale on(true)/off(false). off = freeze current y view
- set_ymode {mode}                     y-axis mode: "auto" (autorange), "manual" (freeze current), or "log" (log scale)
- apply_preset {name}                  apply a saved preset combo
- save_preset {name}                   save current shown indicators+colors+subplots as a preset
- zoom {start,end}                     x-range, ISO dates e.g. "2024-02-14" "2024-02-16"
- zoom_y {min,max}                     price y-range
- jump_to_date {date, bars?}           center the view on an ISO date (optional window width in bars, default ~200)
- reset_zoom {}                        autorange both axes
- pan {fraction}                       shift window by fraction of width (+right, -left), e.g. 0.5 or -0.5
- request_screenshot {}                ask to SEE the chart again after your other ops are applied. The
                                       UI re-screenshots the updated chart and sends it back to you so you
                                       can keep looking (zoom/scroll/toggle, then look again). Use this when
                                       you need a fresh visual after changing the view. Stop requesting once
                                       you have what you need (the loop is capped).
- mark {markers:[{date,price,confirmed?}], label}  plot event markers on the chart (visual confirm). You normally DON'T fill this yourself; markers from a data query get attached automatically.
- clear_marks {}                       remove event markers
"""

# Analyst grounding tools. The brain CANNOT ingest the raw 42k x 159 CSV. Instead
# it is given a compact statistical PROFILE every turn and may FETCH deeper detail
# on demand (formula source, a small raw window) before proposing a compute query.
ANALYST_DOC = """
ANALYST GROUNDING — how you "know" the data without ingesting it:
You are given a compact PROFILE of every column (kind, value range, p50, corr-to-close,
and <file:function> = the python that PRODUCES it). This is derived from ALL rows, so it
faithfully describes the full dataset. The custom indicators here (MATRIX, ROXY, RPXY,
roxy_support, etc.) are NOT standard — you were not trained on them. To understand what an
indicator MEANS and how it is computed, FETCH its source. To see how it behaves over a
specific span, FETCH a small raw window. Then propose a compute query.

You may request fetches by adding a "fetch" list to your JSON. Each fetch is resolved on the
REAL data/code and fed back to you so you can reason with it BEFORE answering or querying:
- {"get":"source","name":"MATRIX"}                 the python function that computes this indicator
- {"get":"strategy","file":"Bankroll.py"}          full source of a strategy .py (for entry/exit logic)
- {"get":"slice","start":"2024-08-04","end":"2024-08-06","cols":["close","MATRIX","roxy_support"]}
                                                    a SMALL window of raw rows (≤60) to see actual values
When you emit "fetch", set "reply":"" and "query":null; you will be re-asked WITH the fetched
material. Only fetch what you actually need (each costs tokens). Once you understand enough,
answer or emit a compute query. Use source fetches whenever the user asks WHAT an indicator is,
what it measures, how it works, or to design/advise a signal based on it.
"""

SYS = """You are the chart sidekick brain for a crypto trading chart (Plotly candlestick + indicators).
The user talks to you in a chat next to the chart. Translate their request into chart actions.

You receive: the user message + current chart state (pair, visible x-range, which indicators are
shown, all available indicator names, date range of data).

VISION: when a screenshot of the user's chart is provided (an image path is given below), USE the
Read tool to look at it. It shows exactly what the user is currently seeing — the candles/price
line, which indicators are plotted, the visible date range, any markers. Ground your answer in what
you actually see. You can ZOOM, PAN, JUMP TO A DATE, change the Y axis, and HIDE/SHOW indicators via
the ops below, then call request_screenshot to SEE the updated chart and keep investigating until
you have the visual info you need. Describe what you observe when relevant.
The user can DRAW on the chart (cyan freehand strokes, lines, or rectangles). If you see cyan
drawings in the screenshot, they are the user pointing at something — treat them as the focus of
the question (e.g. "what is this circled region", "is this line a real trend") and ground your
analysis on the indicators/candles under those marks.

Indicator name matching: be smart. User says "matrix" -> "MATRIX". "vfi" -> "vfi". Match
case-insensitively and pick the closest real column from all_indicators. If ambiguous, pick the
most obvious base one (e.g. "matrix" -> MATRIX not MATRIX_raw).

Date parsing: data is 15-minute bars. "feb 14" with no year -> use the year present in the data
range. Zoom windows should be reasonable (a few days unless user says otherwise).

TIME -> BARS: data is 15-minute bars, so 1 hour = 4 bars, 12 hours = 48 bars, 1 day = 96 bars.
When the user says a time horizon (e.g. "after 12 hours you sold"), convert it to bars and set
forward_bars accordingly (12h -> forward_bars:48). within_bars (the confirm window) is also in bars.

MULTIPLE HOLD TIMES (holds): if the user asks for SEVERAL hold periods at once ("what about 24,
48 and 96?", "compare 12h vs 24h vs 48h hold", "mark each hold on the chart", "see how the exit
moves"), you MUST set "holds":[<bars>,<bars>,...] on the SAME query (works on eval, recipe, and
compare). The engine keeps the SAME entries and only varies the exit window, returning a
directly-comparable win%/avg-ret per hold AND per-hold exit markers so the chart shows each hold's
exit point. Convert each hold to bars (15-min bars: 12h=48, 24h=96, 48h=192, 96h=384). Set
forward_bars to the FIRST hold in the list. Do NOT run separate queries per hold, do NOT omit holds
when the user lists multiple durations — use the holds list every time multiple holds are named.

ONE POSITION AT A TIME (cooldown): by DEFAULT the engine enforces one trade at a time — after an
entry it will NOT take a new entry until that trade exits (cooldown_bars defaults to forward_bars).
This matches "hold N bars then sell, don't re-enter until it exits". You normally do NOT need to set
cooldown_bars. Only set it if the user wants a DIFFERENT cooldown than the hold (e.g. "wait 1 day
before re-entering" with a 12h hold -> cooldown_bars:96), or set cooldown_bars:0 if the user
explicitly wants EVERY signal counted even when overlapping an open trade.

ANALYSIS RANGE: if the user does NOT name an explicit date range, LEAVE start/end OUT of the query.
The server automatically uses the chart's currently-visible zoom window, so the numbers match
exactly what is shown on the chart. Only set start/end when the user names a specific period
(e.g. "in March", "this month", "Feb 2024").

REPLY FORMATTING: the "reply" string is rendered as MARKDOWN in the chat. Keep it tight, but when
the answer has more than ~2 sentences or covers multiple items, STRUCTURE it so it is skimmable:
- Use `##` / `###` headers to group sections.
- Use `-` bullet lists for parallel items (each setup, each variant, each pro/con).
- **Bold** the key name/term at the start of each bullet (e.g. `- **1-A**: deeper recovery ...`).
- Use a markdown table when comparing the same fields across several items.
- Use `code` for exact column/indicator names and conditions (e.g. `MATRIX<200`, `ZLEMA6`).
- Keep prose terse inside each bullet, no filler. One short clause per idea.
Do NOT dump one long wall of plain sentences. A multi-part explanation MUST use headers + bullets.

Output STRICT JSON only, no prose outside it:
{"reply": "<markdown answer: terse, but headers+bullets when multi-part (see REPLY FORMATTING)>", "actions": [ {"op": "...", "args": {...}} ], "query": <null or a data query>, "fetch": <null or [ {"get":"source|strategy|slice", ...} ]>}

When you need to UNDERSTAND a custom indicator or strategy before answering, set "fetch" (see
ANALYST GROUNDING) with reply:"" and query:null; you will be re-asked with the material.

DATA QUERIES: You are a quant analyst. You CAN read the raw data and run real calculations to
confirm whether a trading edge is viable. When the user describes ANY condition / edge / setup and
asks to count it, check it, or test if it's viable, build a "query" object (leave "reply" empty;
the real answer is computed and added after). The MAIN tool is the generic "eval" op:

- {"op":"eval","expr":"<boolean pandas expr over columns>","confirm_expr":"<optional 2nd condition>",
   "within_bars":N,"forward_bars":M,"edges_only":true,"start":"<ISO?>","end":"<ISO?>"}
   * expr: a boolean condition over the real columns. You may use any column name from
     all_indicators plus open/high/low/close/volume. Operators: < > <= >= == != & | ~ and
     .shift(k) (k bars ago), .diff(), .rolling(W).mean(), abs(), np.*  e.g.:
       "close < MLN_Green_low"                         price under indicator
       "close < MLN_Green_low & MATRIX > MATRIX.shift(1)"  ...and MATRIX rising
       "(close.shift(1) >= MLN_Green_low.shift(1)) & (close < MLN_Green_low)"  fresh cross-under
   * edges_only=true counts each ENTRY (transition into True), not every True bar (usually what you want).
   * confirm_expr (optional): a follow-up condition that must become true within `within_bars` bars
     after each entry. Use it for "...and then within N bars it does X". Each entry is tagged
     confirmed/unconfirmed and a confirm rate is computed.
   * forward_bars: how many bars ahead to measure outcome. The result includes win_rate_pct,
     avg/median forward return %, avg MFE/MAE % -> this is what tells you if the edge is VIABLE.
   * The result also returns event timestamps; these are auto-plotted as markers on the chart so the
     user can visually confirm every signal. You do NOT need to fill the mark action yourself.

Examples of mapping user asks to eval:
   "count when price falls under MLN_Green_low then rises above within 6 bars"
     -> expr:"(close.shift(1) >= MLN_Green_low) & (close < MLN_Green_low)", confirm_expr:"close > MLN_Green_low", within_bars:6
   "is buying when MATRIX crosses above 0 viable?"
     -> expr:"(MATRIX.shift(1) <= 0) & (MATRIX > 0)", forward_bars:24

Also available (simpler): {"op":"value_at","indicator":"<col>","start":"<ISO datetime>"} and
{"op":"stat","indicator":"<col>"} (min/max/mean).

CORR_SCAN op — FIND WHICH OTHER INDICATOR CORRELATES with a target. Use this WHENEVER the user
asks to "find an indicator correlated with X", "what moves with X", "discover a co-mover / pair /
edge from X", or "find alpha around X". It scans EVERY other numeric indicator and ranks them by
|correlation| with the target. You do NOT have to guess the pairs — the server scans all ~150.
- {"op":"corr_scan","indicator":"<target col>","corr_on":"return"|"level","corr_method":"pearson"|"spearman",
   "corr_lag":<int>,"corr_top":<int>,"corr_exclude":["band","substr",...],"start":"<ISO?>","end":"<ISO?>"}
   * corr_on:"return" (DEFAULT CHOICE for finding a real edge) correlates bar-to-bar % CHANGES, so
     two series that MOVE together rank high — this is what matters for alpha. corr_on:"level"
     correlates raw values (almost everything that trends with price scores ~1.0, less useful).
   * The server AUTO-DROPS the target's own band/variant family (e.g. MID_LINE_NEW_upper_band_1),
     which are trivially ~1.0. Add corr_exclude substrings to drop more obvious self-derivatives
     (e.g. ["band","_reg","MID_LINE"]) so you surface INDEPENDENT co-movers.
   * corr_lag>0 shifts the OTHER indicators FORWARD by N bars: a high corr at lag>0 means that
     indicator LEADS the target by N bars => a predictive/leading edge. Try lag 0, then a few small
     lags (1,2,3) when hunting for a leading signal.
   * Returns {top:[{indicator,corr,abs,n}...], candidates_scanned, data_range, bars}.
   * WORKFLOW for "find a strong correlation with another indicator -> give a possible edge with
     true alpha": (1) emit corr_scan (corr_on:"return", small range like the asked 1 month) to find
     the best co-mover; (2) on the NEXT turn, take the top non-trivial indicator and run an "eval" or
     "recipe" edge that USES it (e.g. cross between target and that indicator) with forward_bars to
     measure win_rate/forward_return — THAT is the alpha test. Echo the correlation finding first.
   Example: "find a strong correlation with MID_LINE_NEW for a possible edge, 1 month"
     -> {"op":"corr_scan","indicator":"MID_LINE_NEW","corr_on":"return","corr_top":10,
         "corr_exclude":["band","_reg"],"start":"2024-07-01","end":"2024-08-01"}

COMPARE / RANK MULTIPLE INDICATORS: if the user asks which of several indicators is best, or to
compare a setup across 2+ indicators, use the "compare" op (NOT "eval"). It runs the SAME edge
template on each indicator and ranks them by win rate then avg forward return:
- {"op":"compare","indicators":["MLN_Green_low","MLN_Green_low_2","MID_LINE_NEW"],
   "expr_template":"(close.shift(1) >= {ind}) & (close < {ind})","confirm_template":"close > {ind}",
   "within_bars":6,"forward_bars":8,"start":"<ISO?>","end":"<ISO?>"}
   * Use the literal token {ind} where the indicator column goes; the server substitutes each one.
   * Default templates (if you omit them) test price cross-under -> recover above each indicator.
   * Returns per-indicator stats + a ranked list + markers for the best one.
   Example: "when price drops from these 3 indicators and rises right after, which is best?"
     -> {"op":"compare","indicators":[...3 cols...],"expr_template":"(close.shift(1) >= {ind}) & (close < {ind})","confirm_template":"close > {ind}","within_bars":6,"forward_bars":8}

RECIPE op — MULTI-STEP / STATEFUL SIGNALS (the powerful one). For anything that a single boolean
expr can't express — sequences ("was below, then crossed above within N bars, then confirmed"),
"bars since an event", "support flat for N bars", one-signal-per-setup, indicator-vs-indicator
crosses, or building a brand-new CUSTOM INDICATOR — use the "recipe" op. You write real multi-line
pandas code; the server runs it in a SANDBOX (no imports/files/eval; only the data columns + pd/np).
- {"op":"recipe","recipe":"<multi-line pandas>","confirm_expr":"<optional>","within_bars":N,
   "forward_bars":M,"cooldown_bars":<optional>,"start":"<ISO?>","end":"<ISO?>"}
RULES for the recipe code:
  * Columns are available as bare variables by their REAL name (e.g. close, volume, roxy_support,
    ZLEMA, MATRIX, RPXY, WMA, ZLEMA6, MATRIX_ema, dip_depth_ok). pd, np, math, and scipy `stats` are available.
  * You MUST assign a boolean variable named `signal` = the per-bar entry condition (True where it fires).
  * FULL math/stats vocabulary is available — use any pandas/numpy/scipy.stats function:
    - pandas: .shift(k) .diff() .rolling(W).mean()/std()/var()/median()/min()/max()/sum()/skew()/kurt()/
      quantile(q)/corr(other)/cov(other)/rank()/apply(fn) .ewm(span=W).mean() .cumsum() .pct_change()
      .groupby()/.transform()/.cumcount() .fillna()/.astype()/.clip()/.where() comparisons & | ~
    - numpy: np.where, np.log, np.sqrt, np.sign, np.abs, np.percentile, np.corrcoef, np.std, np.mean ...
    - scipy stats: stats.zscore(arr), stats.skew, stats.kurtosis, stats.pearsonr, stats.spearmanr,
      stats.linregress, stats.percentileofscore, stats.rankdata ... (operate on .to_numpy() arrays or
      via .rolling(W).apply(lambda w: stats.<fn>(...), raw=True))
    NO imports (already imported), NO file/eval/exec, NO dunder/escape attributes.
    Examples: z = pd.Series(stats.zscore(close.ffill().to_numpy()), index=index)  ;
    rc = close.rolling(50).corr(MATRIX)  ;  sk = close.rolling(100).skew()  ;
    slope = close.rolling(20).apply(lambda w: stats.linregress(np.arange(len(w)), w).slope, raw=True)
  * Use intermediate variables prefixed with _ for steps (e.g. _support_flat, _cross_below).
  * A/B TEST (does a filter/condition MATTER?): when the user asks "does X make a difference",
    "is the filter irrelevant", "filtered vs unfiltered", or "which has a higher win rate" between
    two variants, assign BOTH booleans in the SAME recipe and set "baseline_name". Make `signal` the
    FILTERED/stricter variant and `baseline` the looser/unfiltered one. The server scores BOTH on the
    identical edge machinery and the table shows both win rates side by side, so you can state the
    delta. Example: signal = _cross & _quiet ; baseline = _cross ; query has "baseline_name":"baseline".
    Then in your reply compare the two win rates and say whether the filter helped or was irrelevant.
  * "bars since event X" pattern: _flag=(X).astype(int); _grp=_flag.cumsum(); _since=_grp.groupby(_grp).cumcount()
  * "one signal per setup" pattern: _first=_raw.groupby(_grp).transform(lambda x:((x.cumsum()==1)&(x==1)).astype(int))
  * To CREATE A CUSTOM INDICATOR, assign a numeric variable (e.g. my_band = close.rolling(20).mean()*1.01);
    it is returned in defined_indicators and can be plotted/referenced later. Still also assign `signal`.
  Example (LOW_BOTTOM-style bounce): support flat 4 bars -> close dips below -> bounces above within 10 ->
  ZLEMA confirms within 4 -> volume ok. Write each step as a pandas line, end with `signal = ...`.

ECHO FIRST: for a recipe, ALWAYS put a clear plain-English description of the entry LOGIC in "reply"
(what fires the signal, step by step, and any windows/thresholds) so the user can verify the logic
BEFORE trusting the numbers. The recipe code and stats are shown automatically.

Use the current pair from chart state. Pick exact column names from all_indicators (case-sensitive
in expr/template/recipe; if unsure, match the closest real name). NEVER invent an op name; the ONLY
valid ops are: eval, recipe, compare, corr_scan, value_at, stat. If no data query is needed, set "query": null."""


SYS_ANSWER = """You are the chart sidekick quant analyst. A data query was run on the REAL chart
data (the CSV); the computed result is below. A NUMBERS TABLE is already rendered separately and
shown ABOVE your text, so DO NOT output a table and DO NOT restate the figures. Write ONLY a short
verdict (2-4 sentences) interpreting those real numbers: for a compare, say which indicator wins
and why (reference win rate / forward return qualitatively, e.g. "higher win rate and positive
forward return"); for a single edge, say whether it looks viable. Never invent or change a number;
if a stat is missing, say so. Use markdown prose only (bold the winner name). No table.
Output STRICT JSON only: {"reply": "<markdown verdict, NO table, NO restated numbers>", "actions": []}
You may add a zoom action to the busiest month if it helps, else actions=[]. Do NOT add a mark
action; markers are attached automatically."""


def http_get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.load(r)


def http_post(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


# --- activity logging + cooperative cancel ---------------------------------
# The brain reports what it is doing (model calls, ops, subprocess, timings) to
# the server's /brain/log ring buffer, which the UI logs panel tails live. It
# also checks /api/chat/cancelled between phases so the user's STOP button can
# abort a long turn instead of waiting out the subprocess timeout.

class Cancelled(Exception):
    pass


def log_event(mid, phase, detail="", meta=None):
    """Best-effort: never let logging break the turn."""
    try:
        http_post("/brain/log", {"mid": mid, "phase": phase,
                                 "detail": str(detail)[:2000], "meta": meta or {}})
    except Exception:
        pass
    print(f"[brain:{mid}] {phase}: {str(detail)[:200]}", flush=True)


def check_cancel(mid):
    if not mid:
        return
    try:
        if http_get(f"/api/chat/cancelled/{mid}").get("cancelled"):
            raise Cancelled()
    except Cancelled:
        raise
    except Exception:
        pass  # if the check itself fails, don't abort the turn


def _strip_code_fences(text: str) -> str:
    """Remove a leading ```json / ``` fence wrapper if the model wrapped its JSON
    in a markdown code block (common with Claude/Sonnet)."""
    t = text.strip()
    m = re.match(r"^```(?:json|JSON)?\s*\n?(.*?)\n?```\s*$", t, re.DOTALL)
    if m:
        return m.group(1).strip()
    return t


def _find_balanced_json(text: str) -> dict | None:
    """Scan for the first balanced {...} object, respecting strings/escapes so a
    '}' inside a JSON string value does not terminate the object early. Returns
    the parsed dict or None. This is far more robust than a greedy `\\{.*\\}`
    regex, which over- or under-matches once the reply contains braces/quotes."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = text[start:i + 1]
                        try:
                            obj = json.loads(chunk)
                            if isinstance(obj, dict):
                                return obj
                        except Exception:
                            pass
                        break  # this candidate failed; try the next '{'
        start = text.find("{", start + 1)
    return None


def extract_json(text: str) -> dict:
    """Pull the brain's {reply, ops, fetch, query, ...} object out of the model's
    output. Tolerates markdown code fences, prose before/after the JSON, and
    braces inside string values. If no JSON is present at all, fall back to
    using the raw text AS the reply (the model answered in prose) rather than
    surfacing an unhelpful '(brain parse error)'."""
    text = _strip_code_fences(text)
    # 1) whole thing is clean JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # 2) first balanced JSON object embedded in the text
    obj = _find_balanced_json(text)
    if obj is not None:
        return obj
    # 3) no JSON found -> treat the model's prose as the answer, not an error
    cleaned = text.strip()
    if cleaned:
        return {"reply": cleaned, "actions": []}
    return {"reply": "(no reply)", "actions": []}


JCODE_TIMEOUT = 180   # hard cap per model call (s)


def run_jcode(prompt: str, model: str | None, mid=None, phase="model_call") -> str:
    cmd = ["jcode", "run", "--json", "--quiet", "--no-update"]
    if model:
        cmd += ["-m", model]
    # The prompt can grow past the OS argv limit (ARG_MAX, ~128KB) once we append
    # fetched indicator source + chart state, which made Popen die with
    # "OSError: [Errno 7] Argument list too long". jcode expands a "@<path>"
    # message into the file's contents, so we hand it a temp file instead of the
    # raw string. Keeps the whole prompt regardless of size.
    pf = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt", prefix="jc_prompt_", delete=False)
    try:
        pf.write(prompt)
        pf.flush()
        pf.close()
        cmd.append("@" + pf.name)
        t0 = time.time()
        log_event(mid, phase, f"model call ({model or 'default'}) starting", {"prompt_chars": len(prompt)})
        # Popen so we can poll for cancel and kill the model mid-flight instead of
        # blocking on the full timeout.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return _drive_jcode(proc, cmd, t0, mid, phase)
    finally:
        try:
            os.unlink(pf.name)
        except OSError:
            pass


def _drive_jcode(proc, cmd, t0, mid, phase) -> str:
    while True:
        try:
            out_s, _ = proc.communicate(timeout=2)
            break
        except subprocess.TimeoutExpired:
            if mid:
                try:
                    if http_get(f"/api/chat/cancelled/{mid}").get("cancelled"):
                        proc.kill()
                        proc.communicate()
                        log_event(mid, "stopped", f"{phase} killed by user after {time.time()-t0:.0f}s")
                        raise Cancelled()
                except Cancelled:
                    raise
                except Exception:
                    pass
            if time.time() - t0 > JCODE_TIMEOUT:
                proc.kill()
                proc.communicate()
                log_event(mid, "timeout", f"{phase} hit {JCODE_TIMEOUT}s cap")
                raise subprocess.TimeoutExpired(cmd, JCODE_TIMEOUT)
    raw = (out_s or "").strip()
    log_event(mid, phase + "_done", f"model returned in {time.time()-t0:.0f}s", {"out_chars": len(raw)})
    # jcode's --json envelope can be preceded by stray stdout noise (e.g. it echoes
    # part of a base64 image when the agent Reads a screenshot). Decode the FIRST
    # well-formed JSON object at-or-after the first '{' instead of parsing the whole
    # stream, so that leading junk doesn't break us.
    j = None
    i = raw.find("{")
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
    if isinstance(j, dict):
        text = j.get("text") or j.get("message") or j.get("output") or raw
        if isinstance(text, dict):
            text = json.dumps(text)
    else:
        text = raw
    return text


def _iso_date(v) -> str | None:
    """Coerce a chart x-range value (ISO string or epoch ms) to 'YYYY-MM-DD'."""
    if v is None:
        return None
    s = str(v)
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    try:  # plotly sometimes hands back ms-since-epoch
        ts = float(s)
        if ts > 1e11:
            ts /= 1000.0
        return time.strftime("%Y-%m-%d", time.gmtime(ts))
    except Exception:
        return None


def apply_visible_range(query: dict, state: dict) -> None:
    """If the user didn't pin a date range, default the query to the chart's
    visible zoom window so the analysis range == what is shown on the chart."""
    if query.get("start") or query.get("end"):
        return
    xr = state.get("visible_x")
    if isinstance(xr, (list, tuple)) and len(xr) == 2:
        a, b = _iso_date(xr[0]), _iso_date(xr[1])
        if a and b:
            query["start"], query["end"] = a, b


def _fmt(v, suffix="") -> str:
    return "—" if v is None else f"{v}{suffix}"


def stats_table(data: dict) -> str:
    """Deterministic markdown table built ONLY from the CSV computation.
    The LLM never gets to alter these numbers."""
    rng = ""
    if data.get("data_range"):
        rng = f"\n*Range analyzed: {data['data_range'][0]} → {data['data_range'][1]}*"
    if data.get("op") == "corr_scan":
        top = data.get("top") or []
        if not top:
            return f"*No correlates found for {data.get('target','?')}.*{rng}"
        on = data.get("on", "level")
        lag = data.get("lag", 0)
        space = "% returns" if on == "return" else "raw level"
        lag_note = f", other shifted +{lag}b (leads target)" if lag else ""
        head = (f"**Correlation scan vs `{data.get('target','?')}`** "
                f"({data.get('method','pearson')} on {space}{lag_note}, "
                f"{data.get('candidates_scanned','?')} indicators scanned)\n\n"
                f"| Indicator | Corr | n |\n|---|--:|--:|\n")
        body = "".join(f"| {r['indicator']} | {r['corr']:+.3f} | {r['n']} |\n" for r in top)
        return head + body + rng
    rows = None
    if data.get("op") == "compare":
        rows = data.get("ranked") or [r for r in data.get("results", []) if r.get("ok")]
        tmpl = data.get("expr_template", "")
        conf = data.get("confirm_template", "")
    else:
        s = data.get("summary") or {}
        if data.get("op") == "recipe":
            label = (data.get("signal_name") or "signal")
            tmpl = ""  # code is shown separately in the recipe echo, not in the table footer
        else:
            label = data.get("expr", "edge")
            tmpl = data.get("expr", "")
        rows = [{"indicator": label, **s}] if s else []
        # A/B: if the recipe also scored a baseline (e.g. unfiltered version),
        # add it as a second row so win rates sit side by side for comparison.
        base = data.get("baseline")
        if isinstance(base, dict) and base.get("win_rate_pct") is not None:
            rows.append({"indicator": (base.get("name") or "baseline") + " (baseline)", **base})
        conf = data.get("confirm_expr", "")
    if not rows:
        return ""
    fb = data.get("forward_bars") or (data.get("summary") or {}).get("forward_bars")
    cd = data.get("cooldown_bars")
    if cd is None:
        cd = (data.get("summary") or {}).get("cooldown_bars")
    fwd_lbl = f"Fwd {fb}b" if fb else "Fwd"
    head = (f"| Indicator | Entries | Confirm % | Win % | {fwd_lbl} ret | "
            f"Median ret | Avg MFE / MAE |\n|---|--:|--:|--:|--:|--:|--:|\n")
    body = ""
    for r in rows:
        ent = r.get("entries")
        raw = r.get("raw_entries")
        ent_cell = f"{ent}" + (f" / {raw} raw" if (raw is not None and raw != ent) else "")
        body += (f"| {r.get('indicator','?')} "
                 f"| {ent_cell} "
                 f"| {_fmt(r.get('confirm_rate_pct'),'%')} "
                 f"| {_fmt(r.get('win_rate_pct'),'%')} "
                 f"| {_fmt(r.get('avg_fwd_return_pct'),'%')} "
                 f"| {_fmt(r.get('median_fwd_return_pct'),'%')} "
                 f"| {_fmt(r.get('avg_mfe_pct'),'%')} / {_fmt(r.get('avg_mae_pct'),'%')} |\n")
    cd_note = f"cooldown {cd}b (one position at a time)" if cd else ""
    q = _edge_details(tmpl, conf, cd_note)
    return head + body + q + rng + _hold_sweep_table(data, rows)


def _fmt_expr(expr: str) -> str:
    """Pretty-print a boolean pandas edge expr like VS Code would: one condition
    per line, evenly indented, the &/| operator trailing each line."""
    if not expr:
        return ""
    conds, ops, depth, buf, i = [], [], 0, "", 0
    n = len(expr)
    while i < n:
        c = expr[i]
        nxt = expr[i + 1] if i + 1 < n else ""
        if c == "(":
            depth += 1; buf += c
        elif c == ")":
            depth -= 1; buf += c
        elif depth == 0 and c in "&|":
            conds.append(buf.strip())
            if nxt == c:  # && or ||
                ops.append(c + nxt); i += 2; buf = ""; continue
            ops.append(c); i += 1; buf = ""; continue
        else:
            buf += c
        i += 1
    if buf.strip():
        conds.append(buf.strip())
    indent = "    "
    lines = []
    for idx, cond in enumerate(conds):
        tail = (" " + ops[idx]) if idx < len(ops) else ""
        lines.append(indent + cond + tail)
    return "\n".join(lines)


def _edge_details(tmpl: str, conf: str, cd_note: str) -> str:
    """Collapsible <details> dropdown with the edge logic formatted as readable
    code, instead of a long inline one-liner."""
    if not tmpl and not conf and not cd_note:
        return ""
    body = []
    if tmpl:
        body.append("entry:\n" + _fmt_expr(tmpl))
    if conf:
        body.append("confirm:\n    " + conf.strip())
    if cd_note:
        body.append("# " + cd_note)
    code = "\n\n".join(body)
    return ("\n\n<details><summary>edge logic</summary>\n\n"
            "```python\n" + code + "\n```\n</details>")


def _bars_to_hold_label(b) -> str:
    """15-min bars → human hold label (e.g. 96 → '24h')."""
    try:
        b = int(b)
    except Exception:
        return str(b)
    h = b * 15 / 60
    if h == int(h):
        return f"{int(h)}h"
    return f"{h:.1f}h"


def _hold_sweep_table(data: dict, rows: list) -> str:
    """Render the multi-hold sweep (same entries, different exit windows) if present.
    For compare, each row carries its own hold_sweep; for single edge/recipe the
    sweep is on `data`. Numbers come straight from the engine."""
    # gather sweeps: {indicator -> [{forward_bars, win_rate_pct, ...}]}
    sweeps = {}
    if data.get("op") == "compare":
        for r in rows:
            if r.get("hold_sweep"):
                sweeps[r.get("indicator", "?")] = r["hold_sweep"]
    elif data.get("hold_sweep"):
        label = rows[0].get("indicator", "edge") if rows else "edge"
        sweeps[label] = data["hold_sweep"]
    if not sweeps:
        return ""
    holds = [h["forward_bars"] for h in next(iter(sweeps.values()))]
    cols = " | ".join(f"{_bars_to_hold_label(h)} ({h}b)" for h in holds)
    out = ["\n\n**Hold sweep** (same entries, win % / avg ret per hold):",
           f"| Indicator | {cols} |",
           "|---|" + "--:|" * len(holds)]
    for ind, sweep in sweeps.items():
        cells = []
        for h in sweep:
            wr = _fmt(h.get("win_rate_pct"), "%")
            ar = _fmt(h.get("avg_fwd_return_pct"), "%")
            cells.append(f"{wr} / {ar}")
        out.append(f"| {ind} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def slim_for_answer(data: dict) -> dict:
    """Strip bulky markers/sample so the stats survive into the answer pass."""
    keep = {k: v for k, v in data.items() if k not in ("markers", "sample", "marker_groups")}
    # drop per-hold markers from the sweep too (chart-only, not needed for prose)
    def _strip_sweep(sw):
        return [{k: v for k, v in h.items() if k != "markers"} for h in sw]
    if isinstance(keep.get("hold_sweep"), list):
        keep["hold_sweep"] = _strip_sweep(keep["hold_sweep"])
    for key in ("results", "ranked"):
        if isinstance(keep.get(key), list):
            keep[key] = [
                ({**r, "hold_sweep": _strip_sweep(r["hold_sweep"])} if isinstance(r.get("hold_sweep"), list) else r)
                for r in keep[key]
            ]
    return keep


def recipe_echo(query: dict, data: dict, logic_echo: str) -> str:
    """For a recipe op, show the entry LOGIC (English) + the exact pandas code that
    ran, so the user can verify before trusting the numbers. Empty for non-recipe."""
    if data.get("op") != "recipe" and query.get("op") != "recipe":
        return ""
    code = data.get("recipe") or query.get("recipe") or ""
    parts = []
    if logic_echo:
        parts.append("**Entry logic:** " + logic_echo)
    if code:
        parts.append("```python\n" + code.strip() + "\n```")
    defined = data.get("defined_indicators") or []
    if defined:
        parts.append("*Custom indicators defined: " + ", ".join(defined) + "*")
    warn = data.get("baseline_warn")
    if warn:
        parts.append("> Note: " + warn)
    return "\n\n".join(parts)


def _format_history(history: list) -> str:
    """Render prior turns as a transcript so the brain has cross-turn memory
    (e.g. "compare those two again with cooldown 96" can resolve "those two")."""
    if not history:
        return ""
    lines = []
    for turn in history[-12:]:
        role = turn.get("role", "")
        txt = (turn.get("text") or "").strip()
        if not txt:
            continue
        if len(txt) > 1200:
            txt = txt[:1200] + " …"
        who = "USER" if role == "user" else "ASSISTANT"
        lines.append(f"{who}: {txt}")
    if not lines:
        return ""
    return ("\n\nCONVERSATION SO FAR (most recent last — use this to resolve "
            "references like 'those two', 'that signal', 'do it again'):\n" +
            "\n".join(lines))


def http_get_q(path, params):
    from urllib.parse import urlencode
    return http_get(path + "?" + urlencode(params))


def get_profile_text(pair: str) -> str:
    """Compact statistical profile of the full dataset (~2.4k tokens), cached
    server-side. Always injected so the brain is grounded in the real data shape."""
    try:
        r = http_get_q("/api/profile_text", {"pair": pair})
        if r.get("ok"):
            rng = r.get("date_range") or ["?", "?"]
            return (f"DATA PROFILE for {pair} ({r.get('rows')} bars, {rng[0]}..{rng[1]}). "
                    "Each line: col: kind [min,max] p50 corr-to-close <producing file:function>.\n"
                    + r["text"])
    except Exception as e:
        return f"(profile unavailable: {e})"
    return ""


def resolve_fetches(fetches: list, pair: str) -> str:
    """Resolve the brain's fetch requests against REAL code/data and return the
    material as text to feed back into the next decide pass."""
    out = []
    for f in (fetches or [])[:6]:
        if not isinstance(f, dict):
            continue
        kind = f.get("get")
        try:
            if kind == "source":
                r = http_get_q("/api/indicator_source", {"name": f.get("name", "")})
                if r.get("ok"):
                    out.append(f"SOURCE of {f.get('name')} ({r['file']}:{r['function']} "
                               f"L{r['lineno']}):\n```python\n{r['source']}\n```")
                else:
                    out.append(f"SOURCE {f.get('name')}: {r.get('error')} {r.get('hint','')}")
            elif kind == "strategy":
                r = http_get_q("/api/strategy_source", {"file": f.get("file", "")})
                if r.get("ok"):
                    tr = " (truncated)" if r.get("truncated") else ""
                    out.append(f"STRATEGY {r['file']} ({r['n_lines']} lines{tr}):\n"
                               f"```python\n{r['source']}\n```")
                else:
                    out.append(f"STRATEGY {f.get('file')}: {r.get('error')}")
            elif kind == "slice":
                cols = f.get("cols")
                r = http_get_q("/api/slice", {
                    "pair": pair, "start": f.get("start") or "",
                    "end": f.get("end") or "",
                    "cols": ",".join(cols) if isinstance(cols, list) else (cols or ""),
                    "max_rows": f.get("max_rows", 60)})
                if r.get("ok"):
                    out.append(f"RAW SLICE ({r['n']} rows, cols {r['columns']}):\n"
                               + json.dumps(r["rows"], default=str)[:4000])
                else:
                    out.append(f"SLICE: {r.get('error')}")
        except Exception as e:
            out.append(f"fetch {kind} failed: {e}")
    return "\n\n".join(out)


def decide(msg: dict) -> dict:
    mid = msg.get("id")
    state = msg.get("chart_state", {})
    model = msg.get("model")
    pair = state.get("pair") or "BTC_USDT_USDT"
    log_event(mid, "received", msg.get("text", "")[:200], {"pair": pair, "model": model})
    convo = _format_history(msg.get("history") or [])
    shot = state.get("screenshot_path")
    shot_note = ""
    if shot and os.path.exists(shot):
        shot_note = ("\n\nCHART SCREENSHOT: a PNG of the user's current chart view is at this path. "
                     "Read it to SEE the chart before answering.\nImage path: " + shot + "\n")
    profile = get_profile_text(pair)
    base_prompt = (
        SYS + "\n\n" + OPS_DOC + "\n\n" + ANALYST_DOC +
        "\n\n" + profile +
        "\n\nCURRENT CHART STATE:\n" + json.dumps(state, default=str)[:4000] +
        shot_note +
        convo +
        "\n\nUSER MESSAGE:\n" + msg["text"]
    )

    # FETCH LOOP: the brain may ask for indicator source / strategy code / a raw
    # slice before deciding. Resolve those against real code/data and re-ask, up
    # to a small cap so it can't loop forever.
    fetched_ctx = ""
    result = {}
    MAX_HOPS = 4
    for hop in range(MAX_HOPS):
        check_cancel(mid)
        # On the LAST allowed hop, force the model to STOP fetching and answer /
        # query now, so the loop can't exhaust itself on fetch-only replies and
        # leave the user with an empty "(no reply)".
        last_hop = hop == MAX_HOPS - 1
        force = ("\n\nYou have already gathered enough material. Do NOT request any "
                 "more fetches. Set \"fetch\":null and produce your final \"reply\" "
                 "(or a \"query\") NOW.") if last_hop else ""
        prompt = base_prompt + fetched_ctx + force + "\n\nReturn the JSON now."
        try:
            text = run_jcode(prompt, model, mid=mid, phase=f"decide{hop+1}")
        except Cancelled:
            return {"reply": "(stopped by user)", "actions": [], "model_used": model, "cancelled": True}
        except subprocess.TimeoutExpired:
            log_event(mid, "error", "decide model call timed out")
            return {"reply": "(brain timeout — model took too long)", "actions": [], "model_used": model}
        result = extract_json(text)
        fetches = result.get("fetch")
        if not last_hop and isinstance(fetches, list) and fetches and not result.get("query"):
            log_event(mid, "fetch", f"fetching {len(fetches)} item(s): "
                      + ", ".join(str(f.get('get') or f) for f in fetches)[:200])
            material = resolve_fetches(fetches, pair)
            fetched_ctx += ("\n\nFETCHED MATERIAL (use this to ground your answer/query):\n"
                            + material)
            continue
        break
    result["model_used"] = model
    result.pop("fetch", None)

    # Safety net: if the fetch loop ended with neither a reply NOR a query (model
    # kept asking to fetch and never answered), run ONE forced answer pass with
    # all gathered material so the user never gets an empty "(no reply)".
    if not (result.get("reply") or "").strip() and not (isinstance(result.get("query"), dict) and result["query"].get("op")):
        log_event(mid, "force_answer", "fetch loop produced no reply; forcing final answer")
        try:
            check_cancel(mid)
            forced = run_jcode(
                base_prompt + fetched_ctx +
                "\n\nYou must ANSWER NOW from the material above. Do NOT request "
                "fetches. Return STRICT JSON with a non-empty \"reply\" (markdown) "
                "and \"fetch\":null.\n\nReturn the JSON now.",
                model, mid=mid, phase="force_answer")
            fr = extract_json(forced)
            if (fr.get("reply") or "").strip():
                result["reply"] = fr["reply"]
                if fr.get("actions"):
                    result["actions"] = fr["actions"]
                if isinstance(fr.get("query"), dict) and fr["query"].get("op"):
                    result["query"] = fr["query"]
            result.pop("fetch", None)
        except Cancelled:
            return {"reply": "(stopped by user)", "actions": [], "model_used": model, "cancelled": True}
        except Exception:
            pass
        if not (result.get("reply") or "").strip() and not (isinstance(result.get("query"), dict) and result["query"].get("op")):
            result["reply"] = ("I gathered the indicator/strategy sources but could not "
                               "finish composing an answer this turn. Resend or rephrase "
                               "and I'll complete it.")

    # If the LLM requested a data query, run it server-side and have the LLM
    # phrase the final answer from the real numbers.
    query = result.get("query")
    if isinstance(query, dict) and query.get("op"):
        check_cancel(mid)
        query.setdefault("pair", pair)
        apply_visible_range(query, state)  # analysis range == chart zoom unless user pinned dates
        log_event(mid, "op", f"running {query.get('op')}", {"query": json.dumps(query, default=str)[:600]})
        logic_echo = (result.get("reply") or "").strip()  # PASS-1 English logic, for recipe echo
        _ta = time.time()
        try:
            data = http_post("/api/analyze", query)
        except Exception as e:
            data = {"ok": False, "error": str(e)}
        log_event(mid, "op_done",
                  f"{query.get('op')} -> {'ok' if data.get('ok') else 'error: '+str(data.get('error'))[:120]}"
                  f" in {time.time()-_ta:.1f}s")

        # The numbers table is built DETERMINISTICALLY from the CSV computation.
        # The LLM only writes a verdict around it; it can never alter a number.
        table = stats_table(data) if data.get("ok") else ""
        echo = recipe_echo(query, data, logic_echo) if data.get("ok") else ""
        try:
            check_cancel(mid)
            answer_text = run_jcode(
                SYS_ANSWER + convo + "\n\nUSER MESSAGE:\n" + msg["text"] +
                "\n\nQUERY RESULT (real computed data, numbers are FINAL — do not restate "
                "different figures):\n" + json.dumps(slim_for_answer(data), default=str)[:6000] +
                "\n\nReturn the JSON now.",
                model, mid=mid, phase="answer",
            )
            ar = extract_json(answer_text)
            verdict = (ar.get("reply") or "").strip()
            result["actions"] = ar.get("actions") or result.get("actions") or []
        except Cancelled:
            return {"reply": "(stopped by user)", "actions": [], "model_used": model, "cancelled": True}
        except Exception:
            verdict = ""
        if data.get("ok"):
            # Logic echo (recipe code + English) → authentic numbers → AI verdict.
            parts = [p for p in (echo, table, verdict) if p]
            result["reply"] = "\n\n".join(parts).strip()
        else:
            err = str(data.get("error"))
            rc = query.get("recipe")
            result["reply"] = ("Query failed: " + err +
                               (f"\n\n```python\n{rc}\n```" if rc else ""))

        # attach markers from the query so the user can visually confirm events.
        # priority:
        #  - single edge/recipe WITH a hold sweep -> one group PER HOLD (so the user
        #    sees the exit point move as the hold changes: 12h vs 24h vs 48h)
        #  - compare -> one marker group PER indicator (so BOTH show on the chart)
        #  - eval/recipe -> a single group
        # Each marker carries entry + hypothetical exit (+confirm) for visual proof.
        if data.get("ok"):
            groups = []
            sweep = data.get("hold_sweep")
            # compare rows can each carry their own hold_sweep (per-indicator)
            cmp_rows = (data.get("ranked") or data.get("results") or []) if data.get("op") == "compare" else []
            cmp_has_sweep = any(r.get("hold_sweep") for r in cmp_rows)
            if data.get("op") != "compare" and sweep:
                # single edge/recipe + sweep -> one group per hold
                for h in sweep:
                    if h.get("markers"):
                        groups.append({"label": _bars_to_hold_label(h.get("forward_bars")),
                                       "markers": h["markers"]})
            elif data.get("op") == "compare" and cmp_has_sweep:
                # compare + sweep -> one group per indicator×hold (label "IND 24h")
                for r in cmp_rows:
                    ind = r.get("indicator", "edge")
                    for h in (r.get("hold_sweep") or []):
                        if h.get("markers"):
                            groups.append({"label": f"{ind} {_bars_to_hold_label(h.get('forward_bars'))}",
                                           "markers": h["markers"]})
            elif data.get("op") == "compare" and data.get("marker_groups"):
                for g in data["marker_groups"]:
                    if g.get("markers"):
                        groups.append({"label": g.get("indicator", "edge"),
                                       "markers": g["markers"]})
            elif data.get("markers"):
                lbl = (data.get("signal_name") or data.get("expr")
                       or query.get("op") or "events")
                groups.append({"label": str(lbl)[:40], "markers": data["markers"]})
            if groups:
                result.setdefault("actions", [])
                result["actions"] = [a for a in result["actions"] if a.get("op") != "mark"]
                result["actions"].append({"op": "mark", "args": {"groups": groups}})
    result.pop("query", None)
    log_event(mid, "done", "turn complete", {"reply_chars": len(result.get("reply", "") or ""),
                                              "actions": len(result.get("actions") or [])})
    return result


def main():
    print("[brain] polling", BASE)
    while True:
        try:
            pend = http_get("/brain/pending")
        except Exception as e:
            print("[brain] poll error:", e)
            time.sleep(2)
            continue
        for msg in pend.get("messages", []):
            print("[brain] <-", msg["text"])
            # A single bad turn must never kill the whole brain loop: a message is
            # already removed from the server's _pending the instant we poll it, so
            # if decide() raised and the process died, that message would hang the
            # UI spinner forever (it stays in _inflight, never reaches _responses).
            # Catch everything, post an error reply so the spinner resolves, and
            # keep polling.
            try:
                result = decide(msg)
            except Cancelled:
                # user pressed stop mid-turn; server already has the "stopped"
                # result, nothing to send.
                print("[brain] turn cancelled by user")
                continue
            except Exception as e:
                tb = traceback.format_exc()
                print("[brain] decide error:", tb)
                log_event(msg.get("id"), "error", f"turn failed: {e}")
                result = {
                    "reply": f"Brain hit an error on this turn and recovered:\n\n```\n{e}\n```\n\nTry rephrasing or resend.",
                    "actions": [],
                }
            print("[brain] ->", json.dumps(result)[:200])
            try:
                http_post("/brain/reply", {
                    "id": msg["id"],
                    "reply": result.get("reply", ""),
                    "actions": result.get("actions", []),
                    "model_used": result.get("model_used"),
                })
            except Exception as e:
                print("[brain] reply error:", e)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
