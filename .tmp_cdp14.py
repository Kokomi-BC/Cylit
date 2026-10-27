
import asyncio, json, subprocess, time, urllib.request, tempfile, base64
import websockets
CHROME = r"C:\Users\Kokomi\AppData\Local\Google\Chrome\Application\chrome.exe"
PORT = 9346
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
          const hr = document.querySelector('.rr-body hr');
          hr.scrollIntoView({block: 'center'});
          return JSON.stringify({ok: !!hr});
        })()
        """
        await send("Runtime.evaluate", {"expression": js, "returnByValue": True})
        await asyncio.sleep(1.2)
        js2 = r"""
        (() => {
          const hr = document.querySelector('.rr-body hr');
          const r = hr.getBoundingClientRect();
          return JSON.stringify({x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width)});
        })()
        """
        res = await send("Runtime.evaluate", {"expression": js2, "returnByValue": True})
        box = json.loads(res["result"]["result"]["value"])
        print(box)
        clip = {"x": max(0, box["x"] - 30), "y": max(0, box["y"] - 60), "width": 360, "height": 140, "scale": 3}
        shot = await send("Page.captureScreenshot", {"format": "png", "clip": clip})
        open("data/logs/cdp_hr4.png", "wb").write(base64.b64decode(shot["result"]["data"]))
        print("saved clip")
    proc.kill()
asyncio.run(main())
