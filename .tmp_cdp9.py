
import asyncio, json, subprocess, time, urllib.request, tempfile, base64
import websockets

CHROME = r"C:\Users\Kokomi\AppData\Local\Google\Chrome\Application\chrome.exe"
PORT = 9341
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
          const out = {rows: [], hr: [], author: null};
          const row = document.querySelector('[data-row-id="p001-06"]');
          if (row) {
            out.rows.push({id: 'p001-06', html: row.innerHTML.slice(0, 400)});
            const hr = row.querySelector('hr');
            if (hr) { const cs = getComputedStyle(hr); const r = hr.getBoundingClientRect();
              out.hr.push({h: Math.round(r.height), w: Math.round(r.width), display: cs.display,
                           borderTop: cs.borderTopWidth + ' ' + cs.borderTopStyle + ' ' + cs.borderTopColor,
                           margin: cs.margin, opacity: cs.opacity}); }
          }
          /* 也看看整页有没有任何 hr */
          out.hrCount = document.querySelectorAll('.rr-body hr, .md hr').length;
          /* 作者块那一行 */
          const a = document.querySelector('[data-row-id="p001-01"]');
          if (a) {
            const body = a.querySelector('.rr-body');
            const cell = body.getBoundingClientRect();
            out.author = {text: (body.textContent||'').slice(0, 160), w: Math.round(cell.width),
                          lines: Math.round(cell.height / parseFloat(getComputedStyle(body).lineHeight || '20')),
                          html: body.innerHTML.slice(0, 300)};
          }
          return JSON.stringify(out);
        })()
        """
        res = await send("Runtime.evaluate", {"expression": js, "returnByValue": True})
        print(res.get("result", {}).get("result", {}).get("value"))
        shot = await send("Page.captureScreenshot", {"format": "png"})
        open("data/logs/cdp_hr.png", "wb").write(base64.b64decode(shot["result"]["data"]))
    proc.kill()

asyncio.run(main())
