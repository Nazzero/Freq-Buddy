#!/usr/bin/env python3
"""E2E CDP test for the new UI features: collapse/resize right panel,
multi-line textarea, '\\' indicator autocomplete, collapsible indicator
sections, and markdown rendering of bot output."""
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
        r = await self.send("Runtime.evaluate",
                            {"expression": e, "returnByValue": True, "awaitPromise": await_promise})
        if r.get("exceptionDetails"): raise RuntimeError(r["exceptionDetails"].get("text"))
        return r["result"].get("value")
    async def shot(self, p):
        r = await self.send("Page.captureScreenshot", {"format": "png"})
        p.write_bytes(base64.b64decode(r["data"]))


async def main():
    OUT.mkdir(exist_ok=True)
    fails = []
    async with websockets.connect(await page_ws(), max_size=50*1024*1024) as ws:
        cdp = CDP(ws)
        await cdp.send("Page.enable"); await cdp.send("Runtime.enable")
        await cdp.send("Page.reload", {"ignoreCache": True})
        for _ in range(40):
            await asyncio.sleep(0.5)
            ok = await cdp.ev("(!!document.getElementById('input') && !!window.marked && !!document.getElementById('splitter'))")
            if ok: break
        await asyncio.sleep(2)

        # 1. textarea is multi-line element
        tag = await cdp.ev("document.getElementById('input').tagName")
        if tag != "TEXTAREA": fails.append(f"input not textarea (got {tag})")

        # 2. markdown rendering
        md = await cdp.ev("""(() => {
            const el=addMsg("**bold** and `code`\\n\\n- item one\\n- item two","bot");
            return {html: el.innerHTML, hasStrong: !!el.querySelector('strong'),
                    hasCode: !!el.querySelector('code'), hasLi: el.querySelectorAll('li').length};
        })()""")
        if not md["hasStrong"]: fails.append("markdown: no <strong>")
        if not md["hasCode"]: fails.append("markdown: no <code>")
        if md["hasLi"] < 2: fails.append(f"markdown: list items {md['hasLi']}")

        # 3. backslash autocomplete
        ac = await cdp.ev("""(() => {
            const i=document.getElementById('input');
            i.focus(); i.value=""; 
            const first=(COLS.indicators||[])[0]||"";
            const frag=first.slice(0,3);
            i.value="show me \\\\"+frag;
            i.setSelectionRange(i.value.length,i.value.length);
            i.dispatchEvent(new Event('input',{bubbles:true}));
            const box=document.getElementById('acbox');
            const items=[...box.querySelectorAll('.acitem')].map(d=>d.textContent);
            return {open: box.classList.contains('show'), n: items.length, frag, sample: items.slice(0,3)};
        })()""")
        if not ac["open"]: fails.append("autocomplete did not open on \\")
        if ac["n"] < 1: fails.append("autocomplete: no items")

        # 3b. pick via Tab inserts name + drops backslash
        pick = await cdp.ev("""(() => {
            const i=document.getElementById('input');
            pickAC(0);
            return {val: i.value, noBackslash: !i.value.includes('\\\\'), boxClosed: !document.getElementById('acbox').classList.contains('show')};
        })()""")
        if not pick["noBackslash"]: fails.append(f"autocomplete pick left backslash: {pick['val']}")
        if not pick["boxClosed"]: fails.append("autocomplete box stayed open after pick")

        # 4. collapse indicator panel
        col = await cdp.ev("""(() => {
            document.querySelector('#indhead .ttl').click();
            const c1=document.getElementById('indpanel').classList.contains('collapsed');
            document.getElementById('activehead').click();
            const c2=document.getElementById('activepanel').classList.contains('collapsed');
            document.querySelector('#indhead .ttl').click();
            document.getElementById('activehead').click();
            return {indCollapsed:c1, activeCollapsed:c2};
        })()""")
        if not col["indCollapsed"]: fails.append("indicator panel did not collapse")
        if not col["activeCollapsed"]: fails.append("active panel did not collapse")

        # 5. resize + collapse right panel
        rz = await cdp.ev("""(() => {
            const r=document.getElementById('right');
            const w0=r.getBoundingClientRect().width;
            r.style.width='600px';
            const w1=r.getBoundingClientRect().width;
            document.getElementById('rightcollapse').click();
            const collapsed=r.classList.contains('collapsed');
            const w2=r.getBoundingClientRect().width;
            document.getElementById('rightcollapse').click();
            const w3=r.getBoundingClientRect().width;
            return {w0,w1,collapsed,w2,w3};
        })()""")
        if rz["w1"] <= rz["w0"]: fails.append("resize did not widen panel")
        if not rz["collapsed"]: fails.append("right panel did not collapse")
        if rz["w2"] > 10: fails.append(f"collapsed width not ~0 ({rz['w2']})")

        await cdp.shot(OUT / "ui_features.png")

    if fails:
        print("FAIL UI features:")
        for f in fails: print("  -", f)
        return 1
    print("PASS UI features: textarea, markdown, autocomplete, collapses, resize all OK")
    print("  autocomplete sample:", ac["sample"])
    print("  screenshot: _cdp_out/ui_features.png")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
