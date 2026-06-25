# HANDOFF: Chart Sidekick brain - ARG_MAX crash + markdown reply formatting

**Date:** 2026-06-24 | **Agent:** Claude Opus 4.6 | **Branch:** main | **Status:** Complete
**Goal:** Fix the brain crashing mid-turn (stuck "scoring outcomes... (Ns)" spinner) and make the brain's long chat replies render with markdown structure instead of a flat wall of text.

---

## COMPLETED

- [x] **ARG_MAX crash fix** -> `chartsidekick/brain.py:322` (`run_jcode`) | Tested with 208KB prompt | commit `02034bd`
- [x] **Markdown reply formatting** -> `chartsidekick/brain.py:125` (SYS `REPLY FORMATTING` block) | Tested, produced real markdown table | commit `fc97de3`
- [x] **Brain restarted** -> running, polling `http://127.0.0.1:8777`, no crash

## INCOMPLETE

- [ ] **Brain watchdog (auto-restart on crash)** -> not implemented | Offered to user, not yet accepted. Would prevent the stuck-spinner state entirely if the brain dies again.

## CURRENT STATE

- **Working:** server.py (pid 548982, port 8777), brain.py running + polling, large prompts succeed via temp file, multi-part replies emit markdown (headers/bullets/bold/tables).
- **Broken:** N/A

## FAILED APPROACHES (Don't Repeat)

- **Pass prompt via stdin to `jcode run -`** -> Failed: jcode treats `-` as a literal message (replied "What can I help with?"), stdin ignored. No native stdin support. -> Use `@<filepath>` instead (jcode expands `@path` into the file contents).
- **Half-diagnosis "brain just dead, restart it"** -> Failed: restart + re-run still hung at 442s. Real cause was the ARG_MAX crash on the `decide2` hop, which re-killed the brain every time it re-processed the queued message.
- **Launching brain with `cmd && ... &` one-liner expecting immediate return** -> The trailing `&` keeps the bash tool attached ~120s (times out). Process DOES spawn; verify separately via `ps`.

## KEY DECISIONS

| Decision | Rationale |
|----------|-----------|
| Pass jcode prompt via `@tempfile` not argv | Prompt grows past OS `ARG_MAX` (~128KB) once decide2 appends 5 indicator source files + chart state; argv raises `OSError: [Errno 7] Argument list too long`. Temp file removes the size ceiling. |
| Split poll loop into `_drive_jcode(proc, cmd, t0, mid, phase)` | Keep `run_jcode` focused on temp-file lifecycle (write + `os.unlink` in `finally`); reuse the existing cancel/timeout poll logic unchanged. |
| Add `REPLY FORMATTING` block to SYS, not change the renderer | UI already renders markdown (marked@12.0.0 + full CSS). Root cause was the model emitting flat prose because SYS said "short caveman-terse". Fix is prompt-side. |
| Leave SYS_ANSWER (query-verdict path) as-is | It is deliberately a short 2-4 sentence markdown verdict; no wall-of-text problem there. |

## RESUME INSTRUCTIONS

Step-by-step for the next agent to continue:

1. **Verify brain alive** -> `ps aux | grep brain.py | grep -v grep` | Expected: one `python -u brain.py` process
2. **Check brain log** -> `cat chartsidekick/brain.out` | Expected: `[brain] polling http://127.0.0.1:8777`, no traceback
3. **(If implementing watchdog)** -> wrap brain launch in a supervisor loop or systemd-style respawn | Expected: brain auto-restarts within seconds of a crash, queued message NOT lost (note: current server `brain_pending` clears `_pending` on read, so a message dequeued just before a crash is lost - a robust watchdog should not rely on re-delivery; consider making the brain ack-after-reply or the server requeue on missing reply)

**Future (once unblocked):**

- [ ] **Brain watchdog** -> auto-restart brain.py on crash; optionally requeue an in-flight message whose reply never arrives so a crash mid-turn self-heals
- [ ] **Cap fetched-source size** -> even with @file fix, very large decide2 prompts cost tokens/latency; consider truncating each fetched source file

Verification: `cat chartsidekick/brain.out` (fresh polling line, no traceback) and send a multi-part question in the UI -> reply renders with headers/bullets/table.

## HOW IT WORKS

- **Flow:** browser POST `/api/chat` -> server `_pending` queue -> brain polls `/brain/pending` (clears queue) -> `decide()` calls `run_jcode` (local `jcode` CLI as LLM, no API key) -> brain POSTs `/brain/reply` -> server stores `_responses[id]` -> browser polls `/api/chat/result/{id}` -> UI renders reply via `marked.parse`.
- **State/Storage:** in-memory `_pending`/`_inflight`/`_responses` dicts in server.py; conversation persisted to `chartsidekick/chat_history.jsonl`; brain stdout -> `chartsidekick/brain.out`.

## SETUP REQUIRED

- Python venv: `/home/nazmoney/FTBotX4D/.venv/bin/python`
- `jcode` CLI on PATH (used as the LLM, no API key needed)
- Server must be running on port 8777 before the brain can poll

## CODE CONTEXT

`run_jcode` now (brain.py ~322):
```python
def run_jcode(prompt, model, mid=None, phase="model_call") -> str:
    cmd = ["jcode", "run", "--json", "--quiet", "--no-update"]
    if model: cmd += ["-m", model]
    pf = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                     suffix=".txt", prefix="jc_prompt_", delete=False)
    try:
        pf.write(prompt); pf.flush(); pf.close()
        cmd.append("@" + pf.name)              # jcode expands @path -> file contents
        t0 = time.time()
        log_event(mid, phase, ...)
        proc = subprocess.Popen(cmd, stdout=PIPE, stderr=PIPE, text=True)
        return _drive_jcode(proc, cmd, t0, mid, phase)
    finally:
        try: os.unlink(pf.name)
        except OSError: pass
```
- `_drive_jcode(proc, cmd, t0, mid, phase)` holds the unchanged cancel/timeout poll loop + JSON extraction.
- `import tempfile` added at top of brain.py.
- SYS `REPLY FORMATTING` block (~line 125): instructs `##`/`###` headers, `-` bullets, `**bold**` key terms, `code` for column/indicator names, markdown tables for comparisons; reply hint changed to `<markdown answer: terse, but headers+bullets when multi-part>`.

## WARNINGS

- `chartsidekick/brain.out` -> can show STALE logs (old line numbers) from a previously-crashed brain. Truncate (`: > brain.out`) before relaunch and confirm a fresh `[brain] polling` line, else you misread a pre-fix traceback as current.
- Launching brain: `cd chartsidekick && : > brain.out && nohup setsid /home/nazmoney/FTBotX4D/.venv/bin/python -u brain.py >> brain.out 2>&1 < /dev/null &` -> the bash wrapper lingers ~120s; verify pid via `ps` and kill the lingering wrapper pid.
- `brain_pending` (server.py:1118) clears `_pending` on read -> a message dequeued immediately before a brain crash is LOST and cannot be auto-retried; user must press the red stop button and resend.
- Do NOT introduce new non-ASCII (em-dashes) into brain.py edits; pre-existing em-dashes in prompt strings are fine. Scan with `grep -nP '[^\x00-\x7F]'` before committing.
- `.git/gc.log` warning on commit is pre-existing/unrelated (loose objects); not caused by this work.

## KEY FILES

- `chartsidekick/brain.py` -> AI brain loop; polls server, calls `jcode` as LLM, returns reply + chart actions as strict JSON
- `chartsidekick/server.py` -> FastAPI backend; chat queue, `/brain/*` and `/api/*` routes, port 8777
- `chartsidekick/index.html` -> Plotly UI; renders bot replies via `marked.parse` (markdown CSS lines ~246-266)
- `chartsidekick/brain.out` -> brain stdout/stderr log
- `chartsidekick/chat_history.jsonl` -> persisted conversation
