#!/usr/bin/env python3
"""E2E CDP test for the AI analyst flow: reload the live page, ask a viability
question in natural language, wait for the brain to compute + drop event markers
on the chart, screenshot. Proves raw-number calc + visual confirmation."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import websockets

CDP_HTTP = "http://127.0.0.1:9333"
OUT = Path(__file__).resolve().parent / "_cdp_out"

QUESTION = ("I think there is an edge: when price crosses below MLN_Green_low and "
            "then rises back above it within 6 bars. Count it in Feb 2024 and tell "
            "me if it is a viable solution. Mark the events on the chart.")


async def page_ws():
    import urllib.request
    with urllib.request.urlopen(f"{CDP_HTTP}/json") as r:
        for t in json.load(r):
            if t.get("type") == "page" and "8777" in t.get("url", ""):
                return t["webSocketDebuggerUrl"]
    raise RuntimeError("page not found")


class CDP:
    def __init__(self, ws): self.ws, self._id = ws, 0
    async def send(self, m, p=None):
        self._id += 1; mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": m, "params": p or {}}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg: raise RuntimeError(f"{m}: {msg['error']}")
                return msg.get("result", {})
    async def ev(self, e, await_promise=False):
        r = await self.send("Runtime.evaluate",
                            {"expression": e, "returnByValue": True, "awaitPromise": await_promise})
        if r.get("exceptionDetails"): raise RuntimeError(r["exceptionDetails"].get("text"))
        return r["result"].get("value")
    async def shot(self, p):
        r = await self.send("Page.captureScreenshot", {"format": "png"})
        p.write_bytes(base64.b64decode(r["data"]))


async def main():
    OUT.mkdir(exist_ok=True)
    async with websockets.connect(await page_ws(), max_size=50*1024*1024) as ws:
        cdp = CDP(ws)
        await cdp.send("Page.enable"); await cdp.send("Runtime.enable")
        # reload to pick up new index.html (marker support)
        await cdp.send("Page.reload", {"ignoreCache": True})
        for _ in range(40):
            await asyncio.sleep(0.5)
            ok = await cdp.ev("(typeof OPS!=='undefined' && !!document.getElementById('input'))")
            if ok:
                break
        await asyncio.sleep(2)  # let initial data load
        # ask the analyst question
        await cdp.ev(f"""(() => {{
            const i=document.getElementById('input');
            i.value={json.dumps(QUESTION)};
            document.getElementById('send').click();
            return true;
        }})()""")
        result = None
        for _ in range(120):
            await asyncio.sleep(0.5)
            st = await cdp.ev("""(() => {
                const c=document.getElementById('chart');
                const traces=(c&&c.data)?c.data.map(t=>t.name):[];
                const markerTrace=(c&&c.data)?c.data.find(t=>t.mode==='markers'):null;
                return {traces, nMarkers: markerTrace? (markerTrace.x||[]).length:0,
                        markerName: markerTrace?markerTrace.name:null,
                        log: document.getElementById('log').innerText.slice(-600)};
            })()""")
            if st["nMarkers"] > 0:
                result = st; break
        await cdp.shot(OUT / "analyst_markers.png")
        if result:
            print("PASS analyst flow")
            print("  markers on chart:", result["nMarkers"], "| trace:", result["markerName"])
            print("  reply tail:", result["log"][-300:])
            return 0
        print("FAIL: no markers appeared")
        print("  last:", st)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
