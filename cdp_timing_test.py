#!/usr/bin/env python3
"""Validate the response-duration readout in the bot 'details' toggle, plus the
brain no-reply fix: a real chat turn must produce a non-empty reply AND the
details line must show both model and 'took <duration>'."""
from __future__ import annotations
import asyncio, json, urllib.request
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
    ws_url=await page_ws()
    async with websockets.connect(ws_url, max_size=40_000_000) as ws:
        c=CDP(ws); await c.send("Runtime.enable"); await c.send("Page.enable")
        # wait for boot
        for _ in range(60):
            if await c.js("!!(window.DATA && document.getElementById('input'))"): break
            await asyncio.sleep(0.5)

        # 0) unit-test fmtDur2 formatting (no model call needed)
        fd=json.loads(await c.js("JSON.stringify([fmtDur2(500),fmtDur2(3400),fmtDur2(12000),fmtDur2(125000),fmtDur2(null)])"))
        want=["500ms","3.4s","12s","2m 5s",""]
        if fd!=want: print(f"FAIL fmtDur2: got {fd} want {want}"); return

        # 1) drive a real chat turn through the brain
        await c.js("""(()=>{ const ta=document.getElementById('input');
          ta.value='what is the current pair?'; ta.dispatchEvent(new Event('input',{bubbles:true})); send(); })()""")

        # wait until the latest bot msg is no longer the loading spinner and has rendered text
        reply=""; meta=""; ok=False
        for _ in range(240):  # up to 120s
            await asyncio.sleep(0.5)
            st=json.loads(await c.js("""(()=>{
              const bots=[...document.querySelectorAll('.msg.bot')];
              const b=bots[bots.length-1]; if(!b) return JSON.stringify({wait:1});
              const loading=!!b.querySelector('.loading');
              const meta=b.querySelector('.meta .mbody');
              const txt=(b.innerText||'').replace(/\\s+/g,' ').trim();
              return JSON.stringify({loading, hasMeta:!!meta, metaTxt:meta?meta.textContent:'', txt});
            })()"""))
            if st.get("wait"): continue
            if not st["loading"] and st["hasMeta"]:
                reply=st["txt"]; meta=st["metaTxt"]; ok=True; break

        fails=[]
        if not ok: fails.append("turn did not finish / no details meta line appeared")
        else:
            if "(no reply)" in reply or len(reply)<3: fails.append(f"empty reply: {reply!r}")
            if "model" not in meta.lower(): fails.append(f"details missing model: {meta!r}")
            if "took" not in meta.lower(): fails.append(f"details missing 'took' duration: {meta!r}")
            # duration token must contain a unit
            import re
            if not re.search(r"took\s+\S*(ms|s)\b", meta): fails.append(f"duration not formatted: {meta!r}")

        png=(await c.send("Page.captureScreenshot",{"format":"png"}))["data"]
        import base64; (OUT/"timing.png").write_bytes(base64.b64decode(png))
        print("reply:", reply[:120])
        print("meta :", meta)
        if fails:
            print("FAILURES:"); [print("  -",f) for f in fails]
        else:
            print("ALL PASS ->", OUT/"timing.png")


asyncio.run(main())
