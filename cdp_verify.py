#!/usr/bin/env python3
"""Live CDP render verification for the Chart Sidekick page.

Connects to the running Playwright Chromium over CDP (port 9333), reads the
live Plotly chart state, drives chart-control ops the way the brain would, and
captures a screenshot to prove the rendered chart actually changed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import websockets

CDP_HTTP = "http://127.0.0.1:9333"
OUT_DIR = Path(__file__).resolve().parent / "_cdp_out"


async def get_page_ws() -> str:
    import urllib.request

    with urllib.request.urlopen(f"{CDP_HTTP}/json") as r:
        targets = json.load(r)
    for t in targets:
        if t.get("type") == "page" and "8777" in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("chart sidekick page target not found on CDP")


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self._id = 0

    async def send(self, method, params=None):
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def evaluate(self, expr, await_promise=False):
        res = await self.send(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": await_promise},
        )
        if res.get("exceptionDetails"):
            raise RuntimeError(res["exceptionDetails"].get("text", "JS exception"))
        return res["result"].get("value")

    async def screenshot(self, path: Path):
        res = await self.send("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(res["data"]))


async def main():
    OUT_DIR.mkdir(exist_ok=True)
    ws_url = await get_page_ws()
    print(f"page ws: {ws_url}")
    async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
        cdp = CDP(ws)
        await cdp.send("Page.enable")
        await cdp.send("Runtime.enable")

        # 1. Read live chart state
        state = await cdp.evaluate(
            """(() => {
                const c = document.getElementById('chart');
                const traces = (c && c.data) ? c.data.map(t => t.name || t.type) : [];
                const title = document.title;
                const pair = (document.getElementById('pair')||{}).value || null;
                return { traceCount: traces.length, traces, title, pair };
            })()"""
        )
        print("BEFORE:", json.dumps(state))
        await cdp.screenshot(OUT_DIR / "before.png")

        # 2. Drive an op the brain would issue: show MATRIX + vfi via OPS
        applied = await cdp.evaluate(
            """(async () => {
                if (typeof OPS === 'undefined') return {ok:false, why:'OPS undefined'};
                const before = (document.getElementById('chart').data||[]).length;
                if (OPS.hide_all_indicators) await OPS.hide_all_indicators({});
                if (OPS.show_indicator) {
                    await OPS.show_indicator({name:'MATRIX'});
                    await OPS.show_indicator({name:'vfi'});
                }
                await new Promise(r => setTimeout(r, 600));
                const after = (document.getElementById('chart').data||[]).length;
                return {ok:true, before, after};
            })()""",
            await_promise=True,
        )
        print("APPLY:", json.dumps(applied))
        await asyncio.sleep(0.5)
        await cdp.screenshot(OUT_DIR / "after.png")

        after_state = await cdp.evaluate(
            """(() => {
                const c = document.getElementById('chart');
                const traces = (c && c.data) ? c.data.map(t => t.name) : [];
                return { traceCount: traces.length, traces };
            })()"""
        )
        print("AFTER:", json.dumps(after_state))

        ok = (
            applied
            and applied.get("ok")
            and any("MATRIX" in str(t) for t in after_state.get("traces", []))
        )
        print("VERIFY:", "PASS" if ok else "FAIL")
        print("screenshots:", OUT_DIR / "before.png", OUT_DIR / "after.png")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
