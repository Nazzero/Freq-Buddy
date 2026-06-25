#!/usr/bin/env python3
"""Validate (1) legend default OFF + persist, (2) /clear slash command:
autofill in chat + clears markers/drawings while keeping the active preset."""
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
    async def type_into(self, sel, text):
        await self.js(f"(()=>{{const el=document.querySelector({json.dumps(sel)});el.focus();el.value='';}})()")
        for ch in text:
            await self.send("Input.dispatchKeyEvent",{"type":"keyDown","text":ch})
            await self.send("Input.dispatchKeyEvent",{"type":"keyUp","text":ch})
        await asyncio.sleep(0.15)
    async def press(self, key, code, vk):
        for t in ("keyDown","keyUp"):
            await self.send("Input.dispatchKeyEvent",{"type":t,"key":key,"code":code,"windowsVirtualKeyCode":vk,"nativeVirtualKeyCode":vk})
        await asyncio.sleep(0.2)


async def main():
    async with websockets.connect(await page_ws(), max_size=40_000_000) as ws:
        c=CDP(ws); await c.send("Page.enable"); await c.send("Runtime.enable")
        # ensure clean slate
        await c.js("localStorage.removeItem('cs_legend')")
        for _ in range(80):
            if await c.js("(()=>{try{return !!(DATA && gd && gd._fullLayout);}catch(e){return false;}})()"): break
            await asyncio.sleep(0.5)
        fails=[]

        # ---- Task 1: legend default OFF ----
        s=json.loads(await c.js("JSON.stringify({sl:SHOW_LEGEND, hm:gd._fullLayout.showlegend, active:document.getElementById('legendtoggle').classList.contains('active')})"))
        if s["sl"] is not False or s["hm"] is not False or s["active"] is not False:
            fails.append(f"legend should default OFF: {s}")
        # toggle on -> persists "1"
        await c.js("document.getElementById('legendtoggle').click()"); await asyncio.sleep(0.3)
        s2=json.loads(await c.js("JSON.stringify({sl:SHOW_LEGEND, hm:gd._fullLayout.showlegend, ls:localStorage.getItem('cs_legend')})"))
        if not (s2["sl"] and s2["hm"] and s2["ls"]=="1"): fails.append(f"legend toggle on failed: {s2}")
        # back off
        await c.js("document.getElementById('legendtoggle').click()"); await asyncio.sleep(0.2)

        # ---- Task 2a: /clear autofill ----
        await c.type_into("#input", "/")
        ac_vis=await c.js("document.getElementById('acbox').classList.contains('show')")
        ac_items=json.loads(await c.js("JSON.stringify([...document.querySelectorAll('#acbox .acitem span')].map(x=>x.textContent))"))
        if not ac_vis or "/clear" not in ac_items:
            fails.append(f"/ autocomplete did not show /clear: vis={ac_vis} items={ac_items}")
        # press Enter to pick -> box becomes '/clear'
        await c.press("Enter","Enter",13)
        boxval=await c.js("document.getElementById('input').value")
        if boxval!="/clear": fails.append(f"autofill did not fill /clear, got: {boxval!r}")

        # ---- Task 2b: set up preset + markers + drawing, then run /clear ----
        await c.js("""(()=>{
          // active preset with 2 indicators
          PRESETS['__t']={'ema_20':{place:'price',color:'#fff'},'rsi_14':{place:'sub',color:'#0f0'}};
          CUR_PRESET='__t'; applyConfig(PRESETS['__t']);
          // add an analysis overlay (markers)
          OVERLAYS=[{label:'evt',pair:CUR_PAIR,markers:[{date:DATA.date[10],price:DATA.close[10],confirmed:true}],visible:true,color:'#f80'}];
          syncOverlays();
          // add a user drawing shape
          const sh=(gd.layout.shapes||[]).slice(); sh.push({type:'line',x0:DATA.date[5],x1:DATA.date[20],y0:DATA.close[5],y1:DATA.close[20],line:{color:'#22d3ee'}});
          Plotly.relayout(gd,{shapes:sh});
        })()""")
        await asyncio.sleep(0.5)
        pre=json.loads(await c.js("JSON.stringify({ov:OVERLAYS.length,mg:MARKER_GROUPS.length,shapes:(gd.layout.shapes||[]).filter(s=>!s._meas&&!s._div).length,active:ACTIVE.size})"))

        # send /clear (box already has it)
        await c.js("document.getElementById('input').focus()")
        await c.press("Enter","Enter",13)   # sends /clear
        await asyncio.sleep(0.6)
        post=json.loads(await c.js("JSON.stringify({ov:OVERLAYS.length,mg:MARKER_GROUPS.length,shapes:(gd.layout.shapes||[]).filter(s=>!s._meas&&!s._div).length,active:ACTIVE.size,akeys:[...ACTIVE.keys()].sort()})"))

        shot=await c.send("Page.captureScreenshot",{"format":"png"})
        (OUT/"slash_clear.png").write_bytes(base64.b64decode(shot["data"]))

        print("before /clear:", pre)
        print("after  /clear:", post)
        if pre["ov"]<1 or pre["shapes"]<1 or pre["active"]!=2:
            fails.append(f"setup didn't take: {pre}")
        if post["ov"]!=0 or post["mg"]!=0: fails.append(f"/clear did not remove markers: {post}")
        if post["shapes"]!=0: fails.append(f"/clear did not remove drawings: {post}")
        if post["active"]!=2 or post["akeys"]!=["ema_20","rsi_14"]:
            fails.append(f"/clear did not keep preset indicators: {post}")

        if fails:
            print("FAIL:"); [print("  -",f) for f in fails]; raise SystemExit(1)
        print("ALL PASS ->", OUT/"slash_clear.png")


asyncio.run(main())
