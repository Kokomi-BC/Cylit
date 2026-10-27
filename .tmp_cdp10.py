
import asyncio, json, subprocess, time, urllib.request, tempfile, base64
import websockets

CHROME = r"C:\Users\Kokomi\AppData\Local\Google\Chrome\Application\chrome.exe"
PORT = 9342
BASE = "http://127.0.0.1:8710/cylit"

async def main():
    prof = tempfile.mkdtemp(prefix="cdp_")
    proc = subprocess.Popen([CHROME, f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
                             "--no-first-run", "--no-default-browser-check", "--headless=new",
                             "--window-size=1400,1000", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target = None
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list") as r:
                tabs = json.load(r)
            target = next((t for t in tabs if t["type"] == "page"), None)
            if target: break
        except Exception: pass
        time.sleep(0.5)
    ws_url = target["webSocketDebuggerUrl"]
    async with websockets.connect(ws_url, max_size=40_000_000) as ws:
        n = 0
        async def send(method, params=None):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n: return msg
        await send("Page.enable"); await send("Runtime.enable")
        await send("Page.navigate", {"url": f"{BASE}/reader/attention-is-all-you-need?nosse=1"})
        await asyncio.sleep(10)
        js = r"""
        (() => {
          const hrs = [...document.querySelectorAll('.rr-body hr, .md hr')];
          return JSON.stringify(hrs.map(h => {
            const cs = getComputedStyle(h); const r = h.getBoundingClientRect();
            const rowEl = h.closest('[data-row-id]');
            return {row: rowEl && rowEl.getAttribute('data-row-id'),
                    w: Math.round(r.width), h: Math.round(r.height), y: Math.round(r.y),
                    bt: cs.borderTopWidth + ' ' + cs.borderTopStyle + ' ' + cs.borderTopColor,
                    bb: cs.borderBottomWidth + ' ' + cs.borderBottomStyle,
                    bg: cs.backgroundColor, margin: cs.margin, display: cs.display, opacity: cs.opacity,
                    rowHtml: rowEl ? rowEl.innerHTML.slice(0, 220) : null};
          }));
        })()
        """
        res = await send("Runtime.evaluate", {"expression": js, "returnByValue": True})
        print(res.get("result", {}).get("result", {}).get("value"))
    proc.kill()

asyncio.run(main())
