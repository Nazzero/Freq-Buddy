#!/usr/bin/env python3
"""End-to-end chat round-trip test over CDP: type a command in the live page,
wait for the brain to reply and drive the chart, screenshot the result."""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import websockets

CDP_HTTP = "http://127.0.0.1:9333"
OUT = Path(__file__).resolve().parent / "_cdp_out"


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
        r = await self.send("Runtime.evaluate", {"expression": e, "returnByValue": True, "awaitPromise": await_promise})
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
        # clear then type a command in the input and click Send
        await cdp.ev("OPS.hide_all_indicators({})")
        await cdp.ev("""(() => {
            const i = document.getElementById('input');
            i.value = 'show only ROXY and roxy_support, switch to SOL';
            document.getElementById('send').click();
            return true;
        })()""")
        # poll the live chart for up to 40s for the brain to act
        result = None
        for _ in range(80):
            await asyncio.sleep(0.5)
            st = await cdp.ev("""(() => {
                const c = document.getElementById('chart');
                const traces = (c&&c.data)? c.data.map(t=>t.name):[];
                const pair = (document.getElementById('pair')||{}).value;
                const log = document.getElementById('log').innerText.slice(-400);
                return {traces, pair, log};
            })()""")
            if any("ROXY" in str(t) for t in st["traces"]):
                result = st; break
        await cdp.shot(OUT / "chat_roundtrip.png")
        if result:
            print("PASS chat round-trip")
            print("  pair:", result["pair"], "traces:", result["traces"])
        else:
            print("FAIL: brain did not drive chart in time")
            print("  last:", st)
        return 0 if result else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
