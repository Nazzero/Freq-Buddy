#!/usr/bin/env python3
"""Validate the hover-readout toggle (#hoverrail)."""
from __future__ import annotations
import asyncio, json, base64, urllib.request
from pathlib import Path
import websockets

CDP_HTTP = "http://127.0.0.1:9333"
OUT = Path(__file__).resolve().parent / "_cdp_out"; OUT.mkdir(exist_ok=True)


async def page_ws():
    bws = json.load(urllib.request.urlopen(f"{CDP_HTTP}/json/version"))["webSocketDebuggerUrl"]
    async with websockets.connect(bws, max_size=40_000_000) as ws:
        await ws.send(json.dumps({"id":1,"method":"Target.createTarget","params":{"url":"http://127.0.0.1:8777/"}}))
        while True:
            m=json.loads(await ws.recv())
            if m.get("id")==1: tid=m["result"]["targetId"]; break
    for t in json.load(urllib.request.urlopen(f"{CDP_HTTP}/json")):
        if t.get("id")==tid: return t["webSocketDebuggerUrl"]
    raise RuntimeError("target not found")


class CDP:
    def __init__(self, ws): self.ws, self._id = ws, 0
    async def send(self, m, p=None):
        self._id+=1; await self.ws.send(json.dumps({"id":self._id,"method":m,"params":p or {}}))
        while True:
            r=json.loads(await self.ws.recv())
            if r.get("id")==self._id:
                if "error" in r: raise RuntimeError(r["error"])
                return r.get("result",{})
    async def js(self, e):
        r=await self.send("Runtime.evaluate",{"expression":e,"returnByValue":True,"awaitPromise":True})
        return r.get("result",{}).get("value")


async def main():
    async with websockets.connect(await page_ws(), max_size=40_000_000) as ws:
        c=CDP(ws); await c.send("Page.enable"); await c.send("Runtime.enable")
        for _ in range(80):
            if await c.js("(()=>{try{return !!(DATA && gd && gd._fullLayout);}catch(e){return false;}})()"): break
            await asyncio.sleep(0.5)
        async def click():
            await c.js("document.getElementById('hoverrail').click()"); await asyncio.sleep(0.4)
        async def state():
            return json.loads(await c.js("JSON.stringify({hover:HOVER, hm:gd._fullLayout.hovermode, active:document.getElementById('hoverrail').classList.contains('active'), ls:localStorage.getItem('cs_hover')})"))

        s0=await state()                      # default ON
        await click(); s1=await state()       # OFF
        await click(); s2=await state()       # ON again
        shot=await c.send("Page.captureScreenshot",{"format":"png"})
        (OUT/"hover_toggle.png").write_bytes(base64.b64decode(shot["data"]))

        print("default:", s0); print("after 1 click:", s1); print("after 2 clicks:", s2)
        fails=[]
        if not (s0["hover"] and s0["hm"]=="x" and s0["active"]): fails.append("default should be ON (hovermode 'x')")
        if not ((s1["hover"] is False) and (s1["hm"] is False) and (s1["active"] is False) and s1["ls"]=="0"):
            fails.append(f"toggle OFF failed: {s1}")
        if not (s2["hover"] and s2["hm"]=="x" and s2["active"] and s2["ls"]=="1"):
            fails.append(f"toggle back ON failed: {s2}")
        if fails:
            print("FAIL:"); [print("  -",f) for f in fails]; raise SystemExit(1)
        print("ALL PASS: hover toggle works ->", OUT/"hover_toggle.png")


asyncio.run(main())
