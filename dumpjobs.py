"""Indicator-dump job runner + strategy registry for Chart Sidekick.

The frontend "strategy selector" lets the user generate a new strategy's
indicator dump (pick strategy + pairs + timerange), watch live progress, and
later delete it. Each dump runs `dump_indicators.py` on the host venv via
subprocess (same approach as backtest.py) and streams "[i/N]" progress lines.

A small on-disk registry (chartsidekick_strategies.json) records each dumped
strategy's config so the dropdown + settings reflect what was generated and the
view can be reopened instantly.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
USER_DATA = PROJECT / "user_data"
DUMP_ROOT = USER_DATA / "indicator_dumps"
DATA_DIR = USER_DATA / "data" / "binance" / "futures"
DUMP_SCRIPT = ROOT / "dump_indicators.py"
PY = str(PROJECT / ".venv" / "bin" / "python")
REGISTRY = USER_DATA / "chartsidekick_strategies.json"

# canonical sidekick pairs (must mirror dump_indicators.SIDEKICK_PAIRS keys, csv form)
SIDEKICK_PAIRS = [
    "BTC_USDT_USDT", "ETH_USDT_USDT", "SOL_USDT_USDT", "XRP_USDT_USDT",
    "BNB_USDT_USDT", "DOGE_USDT_USDT", "LINK_USDT_USDT",
]

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_reg_lock = threading.Lock()

_PAIR_RE = re.compile(r"^[A-Z0-9]+_[A-Z0-9]+_[A-Z0-9]+$")
_STRAT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_TR_RE = re.compile(r"^\d{0,8}-\d{0,8}$")


# ---------------- registry ----------------
def _load_registry() -> dict:
    if not REGISTRY.exists():
        return {}
    try:
        return json.loads(REGISTRY.read_text())
    except Exception:
        return {}


def _save_registry(reg: dict) -> None:
    REGISTRY.write_text(json.dumps(reg, indent=2))


def registry_get(strategy: str) -> dict | None:
    with _reg_lock:
        return _load_registry().get(strategy)


def registry_put(strategy: str, entry: dict) -> None:
    with _reg_lock:
        reg = _load_registry()
        reg[strategy] = entry
        _save_registry(reg)


def registry_delete(strategy: str) -> None:
    with _reg_lock:
        reg = _load_registry()
        reg.pop(strategy, None)
        _save_registry(reg)


def registry_all() -> dict:
    with _reg_lock:
        return _load_registry()


# ---------------- discovery ----------------
def dumpable_pairs() -> list[str]:
    """Pairs that have 15m base data downloaded (csv form), sidekick pairs first."""
    found = set()
    if DATA_DIR.exists():
        for f in DATA_DIR.glob("*-15m-futures.feather"):
            found.add(f.name.split("-15m")[0])
    ordered = [p for p in SIDEKICK_PAIRS if p in found]
    extra = sorted(found - set(SIDEKICK_PAIRS))
    return ordered + extra


# ---------------- validation ----------------
def _valid_pairs(pairs: list[str]) -> list[str]:
    avail = set(dumpable_pairs())
    out = []
    for p in pairs:
        p = (p or "").strip()
        if _PAIR_RE.match(p) and p in avail:
            out.append(p)
    return out


def validate_dump(strategy: str, pairs: list[str], timerange: str | None) -> str | None:
    if not strategy or not _STRAT_RE.match(strategy):
        return "invalid strategy name"
    strat_file = USER_DATA / "strategies" / f"{strategy}.py"
    if not strat_file.exists():
        return f"strategy file {strategy}.py not found"
    if not pairs:
        return "no valid pairs selected (need downloaded 15m data)"
    if timerange and not _TR_RE.match(timerange):
        return "timerange must look like 20240101-20250501 (or blank)"
    return None


# ---------------- job runner ----------------
def _upd(job_id: str, **kw):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kw)


def _run_dump(job_id: str, strategy: str, pairs: list[str], timerange: str | None):
    total = len(pairs)
    _upd(job_id, status="running", phase="loading data", done=0, total=total, log="")
    cmd = [PY, "-u", str(DUMP_SCRIPT), "--strategy", strategy,
           "--out-root", str(DUMP_ROOT), "--pairs", ",".join(pairs)]
    if timerange:
        cmd += ["--timerange", timerange]
    _upd(job_id, cmd=" ".join(cmd))
    log_lines: list[str] = []
    try:
        proc = subprocess.Popen(cmd, cwd=str(PROJECT), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            log_lines.append(line)
            log_lines[:] = log_lines[-40:]
            m = re.match(r"\[(\d+)/(\d+)\]\s+(\w+)\s+(\S+)", line)
            if m:
                i, n, verb, pair = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
                _upd(job_id, done=i, total=n, phase=f"{verb} {pair}",
                     log="\n".join(log_lines))
            elif line.startswith("start "):
                _upd(job_id, phase="dumping", log="\n".join(log_lines))
            elif line.startswith("done "):
                _upd(job_id, phase="finishing", log="\n".join(log_lines))
            else:
                _upd(job_id, log="\n".join(log_lines))
        rc = proc.wait(timeout=5)
        if rc != 0:
            _upd(job_id, status="error", error=f"dump exited {rc}",
                 log="\n".join(log_lines), finished=time.time())
            return
        registry_put(strategy, {
            "strategy": strategy,
            "pairs": pairs,
            "timerange": timerange or "",
            "created": time.time(),
        })
        _upd(job_id, status="done", phase="done", done=total, total=total,
             log="\n".join(log_lines), finished=time.time())
    except subprocess.TimeoutExpired:
        _upd(job_id, status="error", error="dump timed out",
             log="\n".join(log_lines), finished=time.time())
    except Exception as e:
        _upd(job_id, status="error", error=str(e),
             log="\n".join(log_lines), finished=time.time())


def start_dump_job(strategy: str, pairs: list[str], timerange: str | None) -> str:
    pairs = _valid_pairs(pairs)
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "started": time.time(),
                         "strategy": strategy, "pairs": pairs,
                         "timerange": timerange or "", "done": 0, "total": len(pairs)}
    t = threading.Thread(target=_run_dump, args=(job_id, strategy, pairs, timerange),
                         daemon=True)
    t.start()
    return job_id


def dump_status(job_id: str) -> dict:
    with _jobs_lock:
        j = _jobs.get(job_id)
        return dict(j) if j else {"status": "unknown"}


# ---------------- delete ----------------
def delete_strategy(strategy: str) -> str | None:
    if not strategy or not _STRAT_RE.match(strategy):
        return "invalid strategy name"
    d = DUMP_ROOT / strategy
    if d.exists() and d.is_dir():
        shutil.rmtree(d)
    registry_delete(strategy)
    return None
