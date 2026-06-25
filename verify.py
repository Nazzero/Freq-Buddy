"""Headless verification: load page, type a chat command, confirm chart reacts."""
import sys, time
from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path="/usr/bin/google-chrome")
        page = browser.new_page(viewport={"width":1400,"height":800})
        errors = []
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error","warning") else None)
        page.on("pageerror", lambda e: errors.append(f"PAGEERROR: {e}"))
        page.goto("http://127.0.0.1:8777/", wait_until="networkidle")
        page.wait_for_timeout(4000)  # data load + render
        if errors:
            print("CONSOLE:", "\n".join(errors[:10]))

        plotly_ok = page.evaluate("typeof Plotly !== 'undefined'")
        has_data = page.evaluate("!!(document.getElementById('chart') && document.getElementById('chart').data)")
        print("Plotly loaded:", plotly_ok, "| chart.data present:", has_data)
        if not (plotly_ok and has_data):
            print("chart not ready, log:", page.evaluate("document.getElementById('log') ? document.getElementById('log').innerText : 'no log'"))
            page.screenshot(path="/tmp/sidekick_notready.png")
            browser.close(); sys.exit(2)


        # initial state
        n_traces_before = page.evaluate("document.getElementById('chart').data.length")
        print("traces before:", n_traces_before)
        page.screenshot(path="/tmp/sidekick_before.png")

        # send a chat command
        page.fill("#input", "show only MATRIX and vfi, zoom to feb 14 2024")
        page.click("#send")

        # wait for actions to apply (brain + execute)
        for _ in range(60):
            page.wait_for_timeout(1000)
            txt = page.evaluate("document.getElementById('log').innerText")
            if "show_only" in txt or "⚙" in txt or "MATRIX + vfi" in txt:
                break
        page.wait_for_timeout(2000)
        if errors:
            print("SEND CONSOLE:", "\n".join(errors[-8:]))

        n_traces_after = page.evaluate("document.getElementById('chart').data.length")
        trace_names = page.evaluate("document.getElementById('chart').data.map(t=>t.name)")
        xrange = page.evaluate("document.getElementById('chart').layout.xaxis.range")
        log = page.evaluate("document.getElementById('log').innerText")
        print("traces after:", n_traces_after, trace_names)
        print("xrange:", xrange)
        print("--- chat log ---")
        print(log)
        page.screenshot(path="/tmp/sidekick_after.png")
        browser.close()

        ok = ("MATRIX" in trace_names and "vfi" in trace_names
              and xrange and "2024-02" in str(xrange[0]))
        print("VERIFY:", "PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)

run()
