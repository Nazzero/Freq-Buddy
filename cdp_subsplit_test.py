#!/usr/bin/env python3
"""Validate the draggable subplot-resize handle (#subsplit).

Loads the page over CDP, forces an indicator into a subplot, then drives the
#subsplit drag via synthetic mouse events and asserts:
  - the handle is visible only when a subplot exists
  - dragging DOWN shrinks the subplot (raises SUBPLOT_SPLIT / price domain bottom)
  - dragging UP grows it
  - the value persists to localStorage
"""
from __future__ import annotations
import asyncio, json, base64, urllib.request
from pathlib import Path
import websockets

CDP_HTTP = "http://127.0.0.1:9333"
OUT = Path(__file__).resolve().parent / "_cdp_out"
OUT.mkdir(exist_ok=True)


async def page_ws():
    # newer Chrome rejects GET /json/new; create a target via the browser endpoint.
    with urllib.request.urlopen(f"{CDP_HTTP}/json/version") as r:
        bws = json.load(r)["webSocketDebuggerUrl"]
    async with websockets.connect(bws, max_size=40_000_000) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                  "params": {"url": "http://127.0.0.1:8777/"}}))
        while True:
            m = json.loads(await ws.recv())
            if m.get("id") == 1:
                tid = m["result"]["targetId"]; break
    with urllib.request.urlopen(f"{CDP_HTTP}/json") as r:
        for t in json.load(r):
            if t.get("id") == tid or t.get("targetId") == tid:
                return t["webSocketDebuggerUrl"]
    raise RuntimeError("target not found")


class CDP:
    def __init__(self, ws): self.ws, self._id = ws, 0
    async def send(self, m, p=None):
        self._id += 1
        await self.ws.send(json.dumps({"id": self._id, "method": m, "params": p or {}}))
        while True:
            r = json.loads(await self.ws.recv())
            if r.get("id") == self._id:
                if "error" in r: raise RuntimeError(r["error"])
                return r.get("result", {})
    async def js(self, expr):
        r = await self.send("Runtime.evaluate",
                            {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("value")


async def main():
    ws_url = await page_ws()
    async with websockets.connect(ws_url, max_size=40_000_000) as ws:
        c = CDP(ws)
        await c.send("Page.enable"); await c.send("Runtime.enable")
        # wait for chart + data (DATA/gd are module-scoped; reference by bare name)
        for _ in range(80):
            ok = await c.js("(()=>{try{return !!(DATA && gd && gd._fullLayout && gd._fullLayout.yaxis);}catch(e){return false;}})()")
            if ok: break
            await asyncio.sleep(0.5)
        assert ok, "chart never initialized"

        # handle must be hidden with no subplots
        hidden0 = await c.js("getComputedStyle(document.getElementById('subsplit')).display")
        # force an indicator into a subplot (sub:0). pick the first available column.
        await c.js("""(()=>{
          const name=(window.COLS&&COLS.indicators&&COLS.indicators[0])||Object.keys(DATA||{}).filter(k=>k!=='date')[0];
          ACTIVE.set(name,{place:'sub:0',color:'#7ee787'}); render(true); buildActive();
          return name;
        })()""")
        await asyncio.sleep(0.8)
        vis = await c.js("getComputedStyle(document.getElementById('subsplit')).display")
        d0 = await c.js("JSON.stringify({split:SUBPLOT_SPLIT, priceDom:gd._fullLayout.yaxis.domain, top:document.getElementById('subsplit').style.top})")
        d0 = json.loads(d0)

        # geometry for the drag
        geo = await c.js("""(()=>{const b=document.getElementById('subsplit').getBoundingClientRect();
          return JSON.stringify({x:b.left+b.width/2, y:b.top+b.height/2});})()""")
        geo = json.loads(geo)

        async def drag(dy):
            x, y = geo["x"], geo["y"]
            await c.send("Input.dispatchMouseEvent", {"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1})
            steps=8
            for i in range(1, steps+1):
                await c.send("Input.dispatchMouseEvent", {"type":"mouseMoved","x":x,"y":y+dy*i/steps,"button":"left"})
                await asyncio.sleep(0.02)
            await c.send("Input.dispatchMouseEvent", {"type":"mouseReleased","x":x,"y":y+dy,"button":"left","clickCount":1})
            await asyncio.sleep(0.4)

        # drag DOWN 140px -> shrink subplot (split should INCREASE)
        await drag(140)
        dDown = json.loads(await c.js("JSON.stringify({split:SUBPLOT_SPLIT, priceDom:gd._fullLayout.yaxis.domain, ls:localStorage.getItem('cs_subsplit')})"))

        # re-read handle position for the next drag (it moved)
        geo = json.loads(await c.js("""(()=>{const b=document.getElementById('subsplit').getBoundingClientRect();
          return JSON.stringify({x:b.left+b.width/2, y:b.top+b.height/2});})()"""))
        # drag UP 200px -> grow subplot (split should DECREASE below the down value)
        await drag(-200)
        dUp = json.loads(await c.js("JSON.stringify({split:SUBPLOT_SPLIT, priceDom:gd._fullLayout.yaxis.domain, ls:localStorage.getItem('cs_subsplit')})"))

        # screenshot
        shot = await c.send("Page.captureScreenshot", {"format":"png"})
        (OUT/"subsplit_verify.png").write_bytes(base64.b64decode(shot["data"]))

        print("hidden(no subplot) display:", hidden0)
        print("visible(with subplot) display:", vis)
        print("initial:", d0)
        print("after drag DOWN:", dDown)
        print("after drag UP:", dUp)

        fails=[]
        if hidden0 != "none": fails.append("handle should be hidden with no subplots")
        if vis == "none": fails.append("handle should be visible with a subplot")
        # User intent: push the boundary DOWN -> bottom subplot SHRINKS (price grows).
        # subplot region = [0, split]; smaller split = smaller subplot. So drag DOWN
        # must DECREASE split, drag UP must INCREASE it.
        if not (dDown["split"] < d0["split"] - 0.02): fails.append(f"drag DOWN did not shrink subplot ({d0['split']:.3f}->{dDown['split']:.3f})")
        if not (dUp["split"] > dDown["split"] + 0.02): fails.append(f"drag UP did not grow subplot ({dDown['split']:.3f}->{dUp['split']:.3f})")
        if dUp["ls"] is None: fails.append("split not persisted to localStorage")
        # price domain bottom must equal split (the wiring)
        if abs(float(dUp["priceDom"][0]) - float(dUp["split"])) > 0.001:
            fails.append(f"price domain bottom {dUp['priceDom'][0]} != split {dUp['split']}")

        if fails:
            print("FAIL:"); [print("  -", f) for f in fails]; raise SystemExit(1)
        print("ALL PASS: subplot resize handle works, screenshot ->", OUT/"subsplit_verify.png")


asyncio.run(main())
