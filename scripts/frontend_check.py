"""前端验收自检（静态资源 + 接口 + 真实浏览器渲染 + 整屏布局）。

1. **静态资源与接口冒烟**：逐个 GET，断言状态码（含 SPA history 回退、缺失资源 404、
   打包产物逐个可访问）。
2. **真实浏览器渲染**：用本机 Edge/Chrome 的 headless 模式打开每个前端路由，
   等 JS 跑完后 dump DOM，断言关键元素出现（能发现"白屏 / 模板写错 / 接口 404"）；
3. 报告落 `data/logs/frontend_check.json`，退出码 0 = 全通过。

用法：
    python cli/pr.py serve --port 8710        # 另开终端
    python scripts/frontend_check.py          # 默认 http://127.0.0.1:8710
    python scripts/frontend_check.py --block-markers --paper turboquant-iclr26
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "msedge", "google-chrome", "chromium",
]

# 每个路由的关键元素（出现即说明该页真的渲染出来了）。
# `size` 是浏览器窗口尺寸：默认桌面宽（1680×1050），用来断言"左右对照"这类桌面布局；
# 窄屏（900×900）单独断言 <1100px 的退化行为（A2-5：单栏、不横向滚动）。
DESKTOP = "1680,1050"
NARROW = "900,900"
ROUTES: list[dict] = [
    {"name": "library", "path": "/library", "markers": ["论文库", "导入 PDF"]},
    {"name": "reader", "path": "/reader/{paper}",
     "markers": ["reader-toolbar", "side-tabs", "对照", "目录"]},
    {"name": "reader-公式页签", "path": "/reader/{paper}?side=formulas",
     "markers": ["fl-bar", "行间", "katex"]},
    # 回归：多行 `$$\begin{aligned}…$$`（定界符与内容同行）曾经整块掉成段落、`_` 变 <em>，
    # 断言公式渲染成 KaTeX、且页面里不再残留 `$$`
    {"name": "reader-多行公式", "path": "/reader/{paper}?view=bilingual&left=text&row=p019-08",
     "markers": ["katex-display"], "absent": ["$$"]},
    {"name": "reader-块对齐", "path": "/reader/{paper}?view=bilingual&left=text",
     "markers": ["row-pair", "two-col", "rr-body", "side-zh"]},
    {"name": "reader-原PDF在左", "path": "/reader/{paper}?view=bilingual&left=page",
     "markers": ["pg-pair", "col-image", "pages/"]},
    {"name": "reader-深链定位", "path": "/reader/{paper}?block={block}",
     "markers": ["data-row-id", "rrow"]},
    {"name": "reader-窄屏退化", "path": "/reader/{paper}?view=bilingual", "size": NARROW,
     "markers": ["row-pair"], "absent": []},
    {"name": "knowledge", "path": "/knowledge", "markers": ["知识库", "chunk"]},
    {"name": "notes", "path": "/notes", "markers": ["笔记", "高亮", "批注"]},
    {"name": "models", "path": "/models", "markers": ["模型", "类型"]},
    {"name": "tasks", "path": "/tasks", "markers": ["任务中心", "队列"]},
    {"name": "settings", "path": "/settings", "markers": ["设置", "并发"]},
]

ASSETS = [
    ("/", 200, "根路径（重定向到 /app/）"),
    ("/app/", 200, "前端首页（dist/index.html）"),
    ("/app/library", 200, "history 路由直接访问（SPA 回退 index.html）"),
    ("/app/assets/not-exists.js", 404, "缺失的带扩展名资源应 404 而不是回退 HTML"),
    ("/health", 200, "系统健康"),
    ("/api/v1/papers", 200, "论文列表"),
    ("/api/v1/models", 200, "模型列表"),
    ("/api/v1/models/types", 200, "模型类型默认"),
    ("/api/v1/models/presets", 200, "模型预设（路由顺序回归点）"),
    ("/api/v1/models/secrets/list", 200, "密钥文件列表"),
    ("/api/v1/settings", 200, "设置"),
    ("/api/v1/settings/health", 200, "设置页健康检查"),
    ("/api/v1/knowledge/overview", 200, "知识库概览"),
    ("/api/v1/notes/overview", 200, "笔记聚合"),
    ("/api/v1/tasks", 200, "任务列表"),
    ("/api/v1/tasks/stats", 200, "任务统计"),
    ("/api/v1/papers/{paper}/outline", 200, "目录（中英双语）"),
    ("/api/v1/papers/{paper}/formulas", 200, "公式（从 Markdown 提取）"),
    ("/api/v1/papers/{paper}/figures", 200, "插图（图注锚定裁图）"),
    ("/api/v1/papers/{paper}/markdown/translated?write=false", 200, "双语 Markdown（带插图）"),
]


LAYOUT_PROBE = """<!doctype html>
<meta charset="utf-8"><title>layout probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:460px;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="__W__" height="__H__" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const f = document.getElementById('f');
f.addEventListener('load', () => setTimeout(() => {
  const w = f.contentWindow, d = f.contentDocument;
  const box = (sel) => {
    const e = d.querySelector(sel);
    if (!e) return null;
    const b = e.getBoundingClientRect();
    return [Math.round(b.width), Math.round(b.height), Math.round(b.right), Math.round(b.bottom)];
  };
  // 只关心**会造成整页横向溢出**的元素：被某个裁剪/可滚动祖先包住的（如工具栏横向滚动、
  // 表格容器）不算问题，否则会误报。
  const clippedByAncestor = (el) => {
    let p = el.parentElement;
    while (p) {
      const cs = w.getComputedStyle(p);
      if (/(auto|scroll|hidden|clip)/.test(cs.overflowX)) return true;
      p = p.parentElement;
    }
    return false;
  };
  const over = [];
  const collectOverflow = () => {
    over.length = 0;
    d.querySelectorAll('body *').forEach((el) => {
      if (el.tagName === 'svg' || el.closest('svg')) return;
      const b = el.getBoundingClientRect();
      if (b.right > w.innerWidth + 1 && !clippedByAncestor(el)) {
        over.push(el.tagName + '.' + String(el.className).slice(0, 24) + ' right=' + Math.round(b.right));
      }
    });
  };
  const report = (drawer) => {
    document.getElementById('out').textContent = JSON.stringify({
      viewport: [w.innerWidth, w.innerHeight],
      docScroll: [d.documentElement.scrollWidth, d.documentElement.scrollHeight],
      panel: box('.reader-aside'),
      panelBody: box('.reader-aside .toc') || box('.reader-aside .panel-body'),
      aside: box('.reader-aside'),
      reader: box('.reader'),
      drawer,
      overflowing: over.slice(0, 8),
    });
  };
  // 窄屏（<1100px）时侧栏是**抽屉**：默认收起，DOM 里根本没有 .reader-aside（v-if）。
  // 那是设计行为（A2-5：单栏退化 + 抽屉），所以先点一下工具栏的开关再断言它真的渲染出来了。
  const closedNarrow = !d.querySelector('.reader-aside') && !!d.querySelector('[data-aside-toggle]');
  if (closedNarrow) {
    d.querySelector('[data-aside-toggle]').click();
    setTimeout(() => { collectOverflow(); report(true); }, 600);
  } else {
    collectOverflow();
    report(false);
  }
}, __WAIT__));
</script>
"""


def find_browser(explicit: str | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).is_file() or shutil.which(explicit) else None
    for cand in BROWSERS:
        if Path(cand).is_file() or shutil.which(cand):
            return cand
    return None


def fetch(url: str, timeout: int = 25) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception:  # noqa: BLE001
        return -1, b""


def render(browser: str, url: str, budget_ms: int = 20000, size: str = DESKTOP) -> str:
    with tempfile.TemporaryDirectory(prefix="pm-render-") as profile:
        cmd = [
            browser, "--headless=new", "--disable-gpu", "--no-first-run",
            "--disable-extensions", "--disable-background-networking",
            f"--window-size={size}",
            f"--user-data-dir={profile}",
            f"--virtual-time-budget={budget_ms}",
            "--dump-dom", url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=budget_ms / 1000 + 45)
        except subprocess.TimeoutExpired:
            return ""
        return proc.stdout.decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8710")
    ap.add_argument("--paper", default="", help="阅读器用论文 id（默认自动取内容最全的一篇）")
    ap.add_argument("--block-markers", action="store_true", help="额外断言公式已渲染成 KaTeX")
    ap.add_argument("--browser", default=None)
    ap.add_argument("--budget", type=int, default=20000)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    results: list[dict] = []
    failures = 0

    print("=" * 78)
    print("① 静态资源与接口冒烟")
    print("=" * 78)
    paper_hint = args.paper
    if not paper_hint:
        status, body = fetch(base + "/api/v1/papers")
        try:
            items = json.loads(body.decode("utf-8"))["data"]["items"]
            parsed = [p for p in items
                      if p.get("status") in ("parsed", "translating", "translated", "indexed")]
            pool = parsed or items
            pool.sort(key=lambda p: (p.get("page_count") or 0), reverse=True)
            paper_hint = pool[0]["id"] if pool else ""
        except Exception:  # noqa: BLE001
            paper_hint = ""

    for path, expect, label in ASSETS:
        real = path.format(paper=paper_hint)
        status, body = fetch(base + real)
        ok = status == expect
        # history 路由回退检查：必须返回入口 HTML
        if ok and real == "/app/library":
            ok = b'<div id="app"' in body
        failures += 0 if ok else 1
        results.append({"kind": "asset", "path": real, "ok": ok, "detail": f"{status} {label}"})
        print(f"[{'PASS' if ok else 'FAIL'}] {status:>4} {real:<52} {label} ({len(body)} B)")

    # 首页里引用的打包产物必须可访问（哈希文件名，逐个验证）
    status, html = fetch(base + "/app/")
    assets = re.findall(r'(?:src|href)="(/app/assets/[^"]+)"', html.decode("utf-8", "replace"))
    for asset in sorted(set(assets)):
        st, body = fetch(base + asset)
        ok = st == 200 and len(body) > 0
        failures += 0 if ok else 1
        results.append({"kind": "bundle", "path": asset, "ok": ok, "detail": f"{st}"})
        print(f"[{'PASS' if ok else 'FAIL'}] {st:>4} {asset:<52} 打包产物 ({len(body)} B)")
    if not assets:
        print("[WARN] index.html 未引用 /app/assets/…（构建产物是否为空？）")
        failures += 1

    paper = paper_hint
    print(f"\n用于阅读器的论文：{paper or '（无）'}")

    block = ""
    if paper:
        status, body = fetch(f"{base}/api/v1/papers/{paper}/reader", timeout=40)
        try:
            blocks = json.loads(body.decode("utf-8"))["data"]["blocks"]
            block = blocks[0]["id"] if blocks else ""
        except Exception:  # noqa: BLE001
            block = ""

    print()
    print("=" * 78)
    print("② 真实浏览器渲染（headless dump-dom）")
    print("=" * 78)
    print("说明：URL 带 ?nosse=1 强制走「SSE 断开 → 轮询」降级路径。")
    print("      长连接会让 headless 的 virtual-time 永不推进（页面永远\"没加载完\"），")
    print("      该参数本身也是运行期可用开关（反向代理禁用 SSE 时）。")
    print(f"      窗口默认 {DESKTOP}（断言桌面左右对照），窄屏用例用 {NARROW}（断言 <1100px 退化）。")

    browser = find_browser(args.browser)
    if not browser:
        print("未找到 Edge / Chrome，跳过渲染检查（可传 --browser 指定路径）")
        failures += 1
    else:
        print(f"浏览器：{browser}\n")
        for route in ROUTES:
            name = route["name"]
            path = route["path"]
            markers = route["markers"]
            absent = route.get("absent", [])
            size = route.get("size", DESKTOP)
            url_path = path.format(paper=paper, block=block)
            if "{paper}" in path and not paper:
                print(f"[SKIP] {name:<16} {url_path}  （库内没有可用的论文）")
                continue
            if "{block}" in path and not block:
                print(f"[SKIP] {name:<16} {url_path}  （该论文没有块）")
                continue
            sep = "&" if "?" in url_path else "?"
            url = f"{base}/app{url_path}{sep}nosse=1"
            dom = render(browser, url, args.budget, size)
            if not dom:
                ok, detail = False, "未取到 DOM（浏览器超时或未安装）"
            elif "boot-spinner" in dom:
                ok, detail = False, "未渲染（仍是启动占位，疑似 JS 报错）"
            else:
                missing = [m for m in markers if m not in dom]
                present_bad = [m for m in absent if m in dom]
                ok = not missing and not present_bad
                if ok:
                    detail = "渲染正常"
                elif missing:
                    detail = f"缺少：{', '.join(missing)}"
                else:
                    detail = f"不应出现：{', '.join(present_bad)}"
            if ok and args.block_markers and name == "reader" and "?block=" not in path:
                if 'class="katex"' not in dom:
                    ok, detail = False, "公式未渲染为 KaTeX"
            failures += 0 if ok else 1
            results.append({"kind": "render", "path": url_path, "ok": ok,
                            "detail": detail, "dom_bytes": len(dom)})
            print(f"[{'PASS' if ok else 'FAIL'}] {name:<16} {url_path:<42} {detail} (DOM {len(dom)} B)")

    # ---------------- ③ 布局约束：整页不允许滚动（用户反馈过的问题，做成永久回归项） ----------------
    print()
    print("=" * 78)
    print("③ 布局约束（阅读器必须整屏铺满、滚动都发生在栏内）")
    print("=" * 78)
    if browser and paper:
        dist = ROOT / "frontend" / "dist"
        probe_file = dist / "_layout_probe.html"
        cases = [
            ("左右对照(文本)", f"{base}/app/reader/{paper}?view=bilingual&nosse=1", 1600, 1000),
            ("左右对照(原PDF)", f"{base}/app/reader/{paper}?view=bilingual&left=page&nosse=1", 1600, 1000),
            ("纯译文", f"{base}/app/reader/{paper}?view=translation&nosse=1", 1600, 1000),
            # 窄屏：A2-5 要"无横向滚动"，这里直接断言整页不横向可滚
            ("窄屏(900x900)", f"{base}/app/reader/{paper}?view=bilingual&nosse=1", 900, 900),
        ]
        try:
            for label, target, vw, vh in cases:
                probe_file.write_text(
                    LAYOUT_PROBE.replace('__W__', str(vw)).replace('__H__', str(vh))
                    .replace('__URL__', target).replace('__WAIT__', '8000'), encoding='utf-8')
                dom = render(browser, f"{base}/app/_layout_probe.html", 25000, "1700,1100")
                m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
                data = None
                if m and m.group(1).strip() != "waiting":
                    try:
                        data = json.loads(m.group(1))
                    except json.JSONDecodeError:
                        data = None
                if not data:
                    ok, detail = False, "未取到测量结果"
                else:
                    vw, vh = data["viewport"]
                    sw, sh = data["docScroll"]
                    problems = []
                    if sh > vh + 1:
                        problems.append(f"整页纵向可滚（{sh} > {vh}）")
                    if sw > vw + 1:
                        problems.append(f"整页横向可滚（{sw} > {vw}）")
                    if data["overflowing"]:
                        problems.append(f"越界元素 {data['overflowing'][:3]}")
                    panel = data.get("panel")
                    if panel and panel[3] > vh + 1:
                        problems.append(f"侧栏超出视口底部（{panel[3]} > {vh}）")
                    if not data.get("panel") or not data.get("panelBody"):
                        problems.append("侧栏/侧栏内容未渲染（抽屉也没能展开？）")
                    ok = not problems
                    if ok and data.get("drawer"):
                        detail = "整屏无滚动、窄屏抽屉可展开"
                    else:
                        detail = "整屏无滚动、侧栏在视口内" if ok else "；".join(problems)
                failures += 0 if ok else 1
                results.append({"kind": "layout", "path": label, "ok": ok, "detail": detail})
                print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
        finally:
            probe_file.unlink(missing_ok=True)
    else:
        print("[SKIP] 需要浏览器与论文数据")

    print()
    print("=" * 78)
    total = len(results)
    failed = sum(1 for r in results if not r["ok"])
    print(f"共 {total} 项：通过 {total - failed}，失败 {failed}")
    report = ROOT / "data" / "logs" / "frontend_check.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(
        {"base": base, "paper": paper, "passed": total - failed, "total": total,
         "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
