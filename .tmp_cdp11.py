
import asyncio, json, subprocess, time, urllib.request, tempfile
import websockets
CHROME = r"C:\Users\Kokomi\AppData\Local\Google\Chrome\Application\chrome.exe"
PORT = 9343
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
        await asyncio.sleep(9)
        js = r"""
        (() => {
          const hits = [];
          for (const sh of document.styleSheets) {
            let rules; try { rules = sh.cssRules; } catch(e) { continue; }
            for (const r of rules) {
              const sel = r.selectorText || '';
              if (/(^|,)\s*(\.md\s+)?hr\b/.test(sel) || sel.includes('hr,')) hits.push(r.cssText.slice(0, 200));
            }
          }
          return JSON.stringify({n: hits.length, rules: hits.slice(0, 10)});
        })()
        """
        res = await send("Runtime.evaluate", {"expression": js, "returnByValue": True})
        print(res.get("result", {}).get("result", {}).get("value"))
    proc.kill()
asyncio.run(main())
