#!/usr/bin/env python3
"""Validate (1) crosshair-options flyout list (ruler + hover toggles) and
(2) the On-chart/chat vertical resize handle (#rsplit)."""
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
    async def click_sel(self, sel):
        await self.js(f"document.querySelector({json.dumps(sel)}).click()"); await asyncio.sleep(0.35)
    async def drag(self, sel, dy):
        # simulate mousedown on element center, mousemove, mouseup at body level
        box=json.loads(await self.js(f"(()=>{{const r=document.querySelector({json.dumps(sel)}).getBoundingClientRect();return JSON.stringify({{x:r.x+r.width/2,y:r.y+r.height/2}});}})()"))
        x,y=box["x"],box["y"]
        for typ,yy in (("mousePressed",y),("mouseMoved",y+dy),("mouseReleased",y+dy)):
            await self.send("Input.dispatchMouseEvent",{"type":typ,"x":x,"y":yy,"button":"left","buttons":1,"clickCount":1})
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)


async def main():
    async with websockets.connect(await page_ws(), max_size=40_000_000) as ws:
        c=CDP(ws); await c.send("Page.enable"); await c.send("Runtime.enable")
        for _ in range(80):
            if await c.js("(()=>{try{return !!(DATA && gd && gd._fullLayout);}catch(e){return false;}})()"): break
            await asyncio.sleep(0.5)
        fails=[]

        # ---- Task 1: crosshair flyout list ----
        # pin the flyout open via trigger click
        await c.click_sel("#rulerrail")
        vis=await c.js("(()=>{const f=document.getElementById('crosshairflyout');return getComputedStyle(f).display!=='none';})()")
        if not vis: fails.append("crosshair flyout did not open on trigger click")
        # both items present
        labels=json.loads(await c.js("JSON.stringify([...document.querySelectorAll('#crosshairflyout .ch-lab')].map(x=>x.textContent))"))
        if labels!=["Ruler crosshair","Hover readout"]:
            fails.append(f"flyout labels wrong: {labels}")
        # toggle ruler off
        await c.click_sel("#rulertog")
        s=json.loads(await c.js("JSON.stringify({R:RULER,H:HOVER,rt:document.getElementById('rulertog').classList.contains('active'),trig:document.getElementById('rulerrail').classList.contains('active')})"))
        if s["R"] is not False or s["rt"] is not False: fails.append(f"ruler toggle off failed: {s}")
        if s["trig"] is not True: fails.append(f"trigger should stay active while hover still on: {s}")
        # toggle hover off -> trigger should go inactive
        await c.click_sel("#hovertog")
        s2=json.loads(await c.js("JSON.stringify({R:RULER,H:HOVER,hm:gd._fullLayout.hovermode,ht:document.getElementById('hovertog').classList.contains('active'),trig:document.getElementById('rulerrail').classList.contains('active')})"))
        if not (s2["H"] is False and s2["hm"] is False and s2["ht"] is False):
            fails.append(f"hover toggle off failed: {s2}")
        if s2["trig"] is not False: fails.append(f"trigger should be inactive when both off: {s2}")
        # toggle both back on
        await c.click_sel("#rulertog"); await c.click_sel("#hovertog")
        s3=json.loads(await c.js("JSON.stringify({R:RULER,H:HOVER,hm:gd._fullLayout.hovermode,ls_r:localStorage.getItem('cs_ruler'),ls_h:localStorage.getItem('cs_hover'),trig:document.getElementById('rulerrail').classList.contains('active')})"))
        if not (s3["R"] and s3["H"] and s3["hm"]=="x" and s3["ls_r"]=="1" and s3["ls_h"]=="1" and s3["trig"]):
            fails.append(f"toggle both back on failed: {s3}")
        # close flyout by outside click
        await c.click_sel("#chart")
        closed=await c.js("(()=>!document.getElementById('crosshairflyout').classList.contains('pin'))()")
        if not closed: fails.append("flyout did not unpin on outside click")

        # ---- Task 2: vertical resize handle ----
        h0=await c.js("Math.round(document.getElementById('activelist').getBoundingClientRect().height)")
        await c.drag("#rsplit", 120)   # drag down -> grow list
        h1=await c.js("Math.round(document.getElementById('activelist').getBoundingClientRect().height)")
        ls=await c.js("localStorage.getItem('cs_activeh')")
        if not (h1 > h0 + 40): fails.append(f"rsplit drag-down did not grow list: {h0} -> {h1}")
        if not ls: fails.append("cs_activeh not persisted after drag")
        await c.drag("#rsplit", -200)  # drag up -> shrink
        h2=await c.js("Math.round(document.getElementById('activelist').getBoundingClientRect().height)")
        if not (h2 < h1 - 40): fails.append(f"rsplit drag-up did not shrink list: {h1} -> {h2}")

        shot=await c.send("Page.captureScreenshot",{"format":"png"})
        (OUT/"crosshair_rsplit.png").write_bytes(base64.b64decode(shot["data"]))

        print("labels:", labels)
        print(f"list height: start={h0} downdrag={h1} updrag={h2}  cs_activeh={ls}")
        if fails:
            print("FAIL:"); [print("  -",f) for f in fails]; raise SystemExit(1)
        print("ALL PASS ->", OUT/"crosshair_rsplit.png")


asyncio.run(main())
