"""前端自检（**默认不调用模型、不留下任何痕迹**）。

设计原则（2026-10-03 重写，用户口径："大幅简化检查代码，避免在历史记录留下内容以及避免
额外的模型花费 —— 默认检查不调用模型"）：

1. **默认零成本**：默认只开浏览器看 DOM/计算样式，**一条消息都不发**；真发消息那一段
   （流式回答 / 思考块）收在 `--model` 后面。
2. **不留痕**：`--model` 造出来的会话在结束前按标题找出来删掉；确认框那一段只点「取消」。
3. **短**：三个探针页（阅读器 / 对话面板 / 确认框）+ 一个可选流式探针，断言写在调用点旁边。

用法：

    python scripts/frontend_check.py                 # 免费（推荐每次提交前跑）
    python scripts/frontend_check.py --model         # 额外跑一条流式问答（1 次模型调用，跑完自删）
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = "1680,1050"
BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\%s\AppData\Local\Google\Chrome\Application\chrome.exe",
)

#: 各探针共用的头部：iframe + 输出用的 <pre> + 几个小工具。
#:
#: ⚠️ 三条铁律（都是踩过的）：
#: 1. `spin()` 用**计数**不用时钟 —— `--virtual-time-budget` 下 `Date.now()` 走虚拟时钟，
#:    `while (Date.now() - t0 < ms)` 会死循环（整轮渲染拖到 205s 超时、一个字段都没有）；
#: 2. 等就绪要用"烧真 CPU + 放手一拍"的循环 —— `sleep()` 在无头下不为 XHR/fetch 停顿，
#:    首屏请求还没回来就被量了（量到空列表）；
#: 3. Python 里的 `\\n` 必须双写 —— 写成单个会被 Python 先变成真换行，落到 JS 里就是
#:    "单引号字符串带换行"，整个 <script> 语法错误、一个字符都不执行（探针永远停在 waiting）。
PROBE_HEAD = """<!doctype html>
<meta charset="utf-8"><title>__TITLE__</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:560px;height:100%;overflow:auto;background:#fff;
border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
const f = document.getElementById('f');
function spin(n) { let s = 0; for (let i = 0; i < n; i += 1) s += i % 7; return s; }
const BURN = 6e6;
/* 等 iframe 里的条件成立：最多 40 轮，每轮烧一圈真 CPU 再放行一拍 */
async function until(d, test, rounds = 40) {
  for (let i = 0; i < rounds; i += 1) {
    if (test(d)) return true;
    spin(BURN);
    await sleep(50);
  }
  return test(d);
}
function rgbOf(w, value) {
  const probe = w.document.createElement('span');
  probe.style.color = String(value).trim();
  w.document.body.appendChild(probe);
  const out = w.getComputedStyle(probe).color;
  probe.remove();
  return out;
}
"""

# --------------------------------------------------------------------------
# 探针 ①：阅读器（正文 / 公式 / 胶囊）
# --------------------------------------------------------------------------
PROBE_READER = PROBE_HEAD + """
let started = false;
f.addEventListener('load', run);
setTimeout(() => { if (!started) run(); }, 3000);
async function run() {
  if (started) return;
  started = true;
  const w = f.contentWindow, d = f.contentDocument;
  const res = { stage: 'loaded' };
  try {
    await until(d, (x) => x.querySelector('[data-row-id]'));
    const tabs = [...d.querySelectorAll('.side-tab')].map((b) => (b.textContent || '').trim());
    res.tabs = tabs;
    res.chatTabIndex = tabs.findIndex((t) => t.indexOf('对话') >= 0);
    res.rows = d.querySelectorAll('[data-row-id]').length;
    res.katex = d.querySelectorAll('.katex').length;
    const elev = rgbOf(w, getComputedStyle(d.documentElement).getPropertyValue('--bg-elev'));
    const sunken = rgbOf(w, getComputedStyle(d.documentElement).getPropertyValue('--bg-sunken'));
    res.elev = elev; res.sunken = sunken;
    /* 阅读器工具条里的「原文/译文/对照」：三层胶囊 = 托盘（无描边+浅底）+ 抬起的白 pill */
    const seg = d.querySelector('.reader-capsule .seg');
    const segActive = seg ? seg.querySelector('button.active') : null;
    if (seg && segActive) {
      const ts = getComputedStyle(seg), as = getComputedStyle(segActive);
      res.segBorderless = ts.borderTopStyle === 'none' || parseFloat(ts.borderTopWidth) === 0;
      res.segTray = ts.backgroundColor;
      res.segActiveBg = as.backgroundColor;
      res.segActiveShadow = as.boxShadow !== 'none';
    }
    /* 侧栏页签托盘：同一门语言（同样无描边、同一个托盘色） */
    const sideTabs = d.querySelector('.side-tabs');
    if (sideTabs) {
      const st = getComputedStyle(sideTabs);
      res.sideBorderless = st.borderTopStyle === 'none' || parseFloat(st.borderTopWidth) === 0;
      res.sideTray = st.backgroundColor;
    }
    /* 胶囊里的按钮**不投影**（它们在托盘内部，不是独立的一枚 pill） */
    const inner = [...d.querySelectorAll('.reader-capsule .cap-group .btn')];
    res.capsuleBtnCount = inner.length;
    res.capsuleBtnShadow = inner.length
      ? inner.map((b) => getComputedStyle(b).boxShadow !== 'none').filter(Boolean).length : -1;
    res.innerInputShadow = (() => {
      const t = d.querySelector('.chat textarea.composer-input');
      return t ? getComputedStyle(t).boxShadow : null;
    })();
    out(res);
    document.title = 'done';
  } catch (err) { res.error = String(err); out(res); document.title = 'error'; }
}
</script>"""

# --------------------------------------------------------------------------
# 探针 ②：对话面板（列表 ↔ 会话；**不发消息**）
# --------------------------------------------------------------------------
PROBE_CHAT = PROBE_HEAD + """
let started = false;
f.addEventListener('load', run);
setTimeout(() => { if (!started) run(); }, 3000);
async function run() {
  if (started) return;
  started = true;
  const w = f.contentWindow, d = f.contentDocument;
  const res = { stage: 'loaded' };
  try {
    const chat = () => d.querySelector('.chat');
    await until(d, (x) => x.querySelector('.chat .list-head') && !x.querySelector('.chat .empty'));
    const root = chat();
    if (!root) { out({ error: 'no .chat' }); return; }

    /* ── 一级页 ── */
    const heads = [...root.querySelectorAll('.list-head button')].map((b) => (b.textContent || '').trim());
    res.headBtns = heads;
    const trays = [...root.querySelectorAll('.filters .seg')];
    res.trayCount = trays.length;
    res.segBtns = trays[0] ? [...trays[0].querySelectorAll('button')].map((b) => (b.textContent || '').trim()) : [];
    res.rows = root.querySelectorAll('.row').length;
    res.listReady = res.rows > 0 || !!root.querySelector('.hollow');
    const titles = [...root.querySelectorAll('.row .row-title')];
    res.titleKatex = titles.filter((t) => t.querySelector('.katex')).length;
    res.titleWithMath = titles.filter((t) => (t.textContent || '').indexOf('$') >= 0).length;

    /* 管理模式：复选框**两个状态两个图标** */
    const manage = [...root.querySelectorAll('.filters button')].find((b) => (b.textContent || '').includes('管理'));
    if (manage && res.rows > 0) {
      manage.click();
      await sleep(250);
      const tick = root.querySelector('.row .tick');
      if (tick) {
        res.tickOff = (tick.querySelector('svg') || {}).innerHTML || '';
        tick.click();
        await sleep(200);
        res.tickOn = (tick.querySelector('svg') || {}).innerHTML || '';
        res.tickDiffer = !!res.tickOff && res.tickOff !== res.tickOn;
      }
      manage.click();
      await sleep(200);
    }
    /* 删除确认框：点开看一眼样式，然后**取消**（不许真删） */
    const rowCountBefore = res.rows;
    const del = root.querySelector('.row .row-acts .ico.danger');
    if (del) {
      del.click();
      await sleep(400);
      const mask = d.querySelector('.modal-mask');
      const modal = d.querySelector('.modal');
      res.modalOpen = !!mask;
      res.modalTitle = (d.querySelector('.modal-title') || {}).textContent || '';
      res.modalDividers = modal ? [
        getComputedStyle(d.querySelector('.modal-head')).borderBottomWidth,
        getComputedStyle(d.querySelector('.modal-foot')).borderTopWidth,
      ] : [];
      const danger = d.querySelector('.modal-foot .btn.danger');
      res.modalDangerSolid = danger ? getComputedStyle(danger).backgroundColor : '';
      const cancel = [...d.querySelectorAll('.modal-foot .btn')].find((b) => (b.textContent || '').includes('取消'));
      if (cancel) cancel.click();
      await sleep(400);
      res.modalClosed = !d.querySelector('.modal-mask');
      res.rowsAfterCancel = root.querySelectorAll('.row').length;
      res.cancelKeptRows = res.rowsAfterCancel === rowCountBefore;
    }

    /* ── 新建 → 二级页 ── */
    const newBtn = root.querySelector('.list-head .new-conv');
    newBtn.click();
    await until(d, (x) => x.querySelector('.chat textarea.composer-input'), 20);
    const head = [...root.querySelectorAll('.thread-head button')].map((b) => (b.textContent || '').trim());
    res.threadHeadBtns = head;
    const ta = root.querySelector('textarea.composer-input');
    res.placeholder = ta ? ta.getAttribute('placeholder') : '';
    const hello = root.querySelector('.hello');
    res.helloText = ((hello || {}).textContent || '').trim();
    res.helloButtons = hello ? hello.querySelectorAll('button').length : -1;
    const box = root.querySelector('.composer-box');
    res.boxRadius = box ? getComputedStyle(box).borderTopLeftRadius : '';
    res.boxParts = box ? ['textarea.composer-input', '.attach', '.send']
      .map((sel) => !!box.querySelector(sel)) : [];
    res.boxHasModel = !!(box && box.querySelector('.model-btn'));
    res.boxHasRing = !!(box && box.querySelector('.ring'));
    const foot = root.querySelector('.composer-foot');
    res.footParts = foot ? [!!foot.querySelector('.model-btn'), !!foot.querySelector('.ring')] : [];
    const ring = root.querySelector('.ring');
    res.ringSize = ring ? Math.round(ring.getBoundingClientRect().width) : -1;
    res.ringText = ring ? (ring.textContent || '').trim() : null;
    res.ringTitle = ring ? (ring.getAttribute('title') || '') : '';
    if (ta) {
      const ts = getComputedStyle(ta);
      res.inputBorder = ts.borderTopWidth + ' ' + ts.borderTopStyle;
      res.inputShadow = ts.boxShadow;
      res.inputMaxH = ts.maxHeight;
      /* 自动增高：塞 6 行内容后应该变高（且不超过 max-height） */
      const h1 = ta.getBoundingClientRect().height;
      const setter = Object.getOwnPropertyDescriptor(w.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(ta, '第一行\\n第二行\\n第三行\\n第四行\\n第五行\\n第六行');
      ta.dispatchEvent(new w.Event('input', { bubbles: true }));
      await sleep(200);
      res.inputH1 = Math.round(h1);
      res.inputH6 = Math.round(ta.getBoundingClientRect().height);
      setter.call(ta, '');
      ta.dispatchEvent(new w.Event('input', { bubbles: true }));
      await sleep(150);
    }
    /* 发送键：空输入必须 disabled */
    const send = root.querySelector('.composer-bar .send');
    res.sendDisabledWhenEmpty = send ? send.disabled : null;
    out(res);

    /* ── 回到列表 → 打开一个**已有会话**看消息操作条与引用锚（不发消息） ── */
    const back = root.querySelector('.thread-head .btn');
    back.click();
    await sleep(600);
    const row = root.querySelector('.row .row-main');
    res.openedConversation = !!row;
    if (row) {
      row.click();
      await until(d, (x) => x.querySelector('.chat .msg .md.body'), 20);
      const tools = root.querySelector('.msg.assistant .msg-tools');
      const body = tools ? tools.parentElement.querySelector('.md.body') : null;
      res.toolsAfterBody = !!(body && tools && (body.compareDocumentPosition(tools) & 4));
      res.toolTitles = tools ? [...tools.querySelectorAll('.ico')].map((b) => b.getAttribute('title') || '') : [];
      const inlines = [...root.querySelectorAll('.md.body .cite-inline')];
      res.inlineCites = inlines.length;
      res.inlineWithPage = inlines.filter((b) => (b.dataset.page || '') !== '').length;
      res.inlineWithRow = inlines.filter((b) => (b.dataset.row || '') !== '').length;
      res.bottomChips = root.querySelectorAll('.cites .cite').length;
    }
    out(res);
    document.title = 'done';
  } catch (err) { res.error = String(err); out(res); document.title = 'error'; }
}
</script>"""

# --------------------------------------------------------------------------
# 探针 ③（可选，--model）：真发一条消息，看流式回答 / 思考块 / 引用锚
# --------------------------------------------------------------------------
PROBE_STREAM = PROBE_HEAD + """
let started = false;
f.addEventListener('load', run);
setTimeout(() => { if (!started) run(); }, 3000);
async function run() {
  if (started) return;
  started = true;
  const w = f.contentWindow, d = f.contentDocument;
  const res = { stage: 'loaded' };
  try {
    await until(d, (x) => x.querySelector('.chat .list-head') && !x.querySelector('.chat .empty'));
    const root = () => d.querySelector('.chat');
    root().querySelector('.list-head .new-conv').click();
    await until(d, (x) => x.querySelector('.chat textarea.composer-input'), 20);
    const ta = root().querySelector('textarea.composer-input');
    const setter = Object.getOwnPropertyDescriptor(w.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(ta, '__QUESTION__');
    ta.dispatchEvent(new w.Event('input', { bubbles: true }));
    await sleep(300);
    const send = root().querySelector('.composer-bar .send');
    res.sendEnabled = send ? !send.disabled : null;
    if (!send || send.disabled) { res.error = '发送键不可用'; out(res); return; }
    send.click();
    /* 等流结束：固定轮数 + 每轮烧一圈真 CPU（见文件头的三条铁律） */
    for (let i = 0; i < 900; i += 1) {
      const err = root().querySelector('.err');
      if (err && (err.textContent || '').trim()) break;
      if (!root().querySelector('.msg.live') && root().querySelectorAll('.msg').length > 1) break;
      spin(BURN);
      await sleep(50);
    }
    await sleep(800);
    res.msgs = root().querySelectorAll('.msg').length;
    const answers = [...root().querySelectorAll('.msg.assistant .md.body')];
    res.answer = answers.length ? answers[answers.length - 1].textContent.trim().slice(0, 120) : '';
    res.inlineCites = root().querySelectorAll('.md.body .cite-inline').length;
    res.bottomChips = root().querySelectorAll('.cites .cite').length;
    res.toolTitles = [...root().querySelectorAll('.msg.assistant .msg-tools .ico')]
      .map((b) => b.getAttribute('title') || '');
    const think = root().querySelector('.msg.assistant .think');
    if (think) {
      res.thinkHead = (think.querySelector('.think-head') || {}).textContent || '';
      res.thinkCollapsed = !think.querySelector('.think-body');
      think.querySelector('.think-head').click();
      await sleep(250);
      res.thinkOpenLen = ((think.querySelector('.think-body') || {}).textContent || '').length;
    }
    res.chatErr = (root().querySelector('.err') || {}).textContent || '';

    /* 顺带把「删除确认框」也过一遍（**只点取消**）：默认跑没有会话可点，这里有了 */
    const backBtn = root().querySelector('.thread-head .btn');
    if (backBtn) {
      backBtn.click();
      await sleep(700);
      const del = root().querySelector('.row .row-acts .ico.danger');
      res.sawRowToDelete = !!del;
      if (del) {
        const before = root().querySelectorAll('.row').length;
        del.click();
        await sleep(400);
        res.modalOpen = !!d.querySelector('.modal-mask');
        res.modalTitle = (d.querySelector('.modal-title') || {}).textContent || '';
        res.modalDividers = [
          getComputedStyle(d.querySelector('.modal-head')).borderBottomWidth,
          getComputedStyle(d.querySelector('.modal-foot')).borderTopWidth,
        ];
        const dgr = d.querySelector('.modal-foot .btn.danger');
        res.modalDangerSolid = dgr ? getComputedStyle(dgr).backgroundColor : '';
        const cancel = [...d.querySelectorAll('.modal-foot .btn')]
          .find((b) => (b.textContent || '').includes('取消'));
        if (cancel) cancel.click();
        await sleep(400);
        res.modalClosed = !d.querySelector('.modal-mask');
        res.cancelKeptRows = root().querySelectorAll('.row').length === before;
      }
    }
    out(res);
    document.title = 'done';
  } catch (err) { res.error = String(err); out(res); document.title = 'error'; }
}
</script>"""


# --------------------------------------------------------------------------
# 探针 ③：划词 → 加入对话 → 输入框里的上下文胶囊（**不发消息**）
# --------------------------------------------------------------------------
# 这条链路以前只有肉眼验证：造一个真 Range 选区 → 派发 mouseup（阅读器的 `onMouseUp`
# 就是这么起的）→ 菜单里点「加入对话」→ 侧栏对话的输入框上沿出现胶囊。
PROBE_SELCHAT = PROBE_HEAD + """
let started = false;
f.addEventListener('load', run);
setTimeout(() => { if (!started) run(); }, 3000);
async function run() {
  if (started) return;
  started = true;
  const w = f.contentWindow, d = f.contentDocument;
  const res = { stage: 'loaded' };
  try {
    await until(d, (x) => x.querySelector('[data-row-id]'));
    /* 第一行常常是标题/作者（只有一两个短节点）—— 往下找**第一个正文行**：
       markdown 渲染会把一行切成**一堆短文本节点**（实测最长 39 字），所以要凑够 ≥2 个节点
       再**跨节点**建 Range。 */
    let nodes = [];
    for (const r of [...d.querySelectorAll('[data-row-id]')].slice(0, 12)) {
      const body = r.querySelector('.rr-body') || r;
      const walker = d.createTreeWalker(body, w.NodeFilter.SHOW_TEXT);
      const got = [];
      while (walker.nextNode()) {
        const t = walker.currentNode;
        if ((t.nodeValue || '').trim().length >= 8) got.push(t);
        if (got.length >= 4) break;
      }
      if (got.length > nodes.length) nodes = got;
      if (nodes.length >= 2) break;
    }
    res.picked = nodes.length > 1;
    if (res.picked) {
      const r = d.createRange();
      r.setStart(nodes[0], 0);
      const last = nodes[nodes.length - 1];
      r.setEnd(last, Math.min(20, (last.nodeValue || '').length));
      const sel = w.getSelection();
      sel.removeAllRanges();
      sel.addRange(r);
      res.selection = sel.toString().trim().slice(0, 20);
      const rect = r.getBoundingClientRect();
      d.dispatchEvent(new w.MouseEvent('mouseup', { bubbles: true,
        clientX: Math.round(rect.left + 6), clientY: Math.round(rect.top + 6) }));
    }
    await until(d, (x) => x.querySelector('[data-selection-menu]'));
    const menu = d.querySelector('[data-selection-menu]');
    res.menuOpen = !!menu;
    res.menuBtns = menu ? [...menu.querySelectorAll('button')].map((b) => (b.textContent || '').trim()) : [];
    const add = menu ? [...menu.querySelectorAll('button')]
      .find((b) => (b.textContent || '').indexOf('加入对话') >= 0) : null;
    res.hasAdd = !!add;
    if (add) add.click();
    await until(d, (x) => x.querySelector('.chat .sel-chip'));
    const chip = d.querySelector('.chat .sel-chip');
    res.chipShown = !!chip;
    if (chip) {
      const main = chip.querySelector('.sel-chip-main');
      res.chipTag = main ? main.tagName : '';
      res.chipText = main ? (main.textContent || '').trim().slice(0, 20) : '';
      /* 位置（"第 4 页" / "第 4 页 · Markdown"）必须摆在胶囊上：光有原文，
         用户分不清自己选的是哪一段（用户口径 2026-10-03）。 */
      const where = chip.querySelector('.sel-chip-where');
      res.chipWhere = where ? (where.textContent || '').trim() : '';
      res.chipOff0 = chip.classList.contains('off');
      const send0 = d.querySelector('.chat .composer-bar .send');
      res.sendDisabledWithChip = send0 ? send0.disabled : null;
      main.click();
      await sleep(200);
      res.chipOff1 = chip.classList.contains('off');
      const send1 = d.querySelector('.chat .composer-bar .send');
      res.sendDisabledWithoutChip = send1 ? send1.disabled : null;
      main.click();
      await sleep(200);
      res.chipOff2 = chip.classList.contains('off');
      const x = chip.querySelector('button.ico');
      res.hasRemove = !!x;
      if (x) x.click();
      await sleep(250);
      res.chipGone = !d.querySelector('.chat .sel-chip');
    }
    out(res);
    document.title = 'done';
  } catch (err) { res.error = String(err); out(res); document.title = 'error'; }
}
</script>"""


# --------------------------------------------------------------------------
# 跑探针
# --------------------------------------------------------------------------


def fetch(url: str, timeout: int = 20) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception:  # noqa: BLE001
        return -1, b""


def find_browser(explicit: str | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).is_file() or shutil.which(explicit) else None
    import os

    cands = [c % os.environ.get("USERNAME", "") if "%s" in c else c for c in BROWSERS]
    for cand in cands:
        if Path(cand).is_file() or shutil.which(cand):
            return cand
    return None


def render(browser: str, url: str, budget_ms: int, size: str = DESKTOP) -> str:
    """无头渲染一个页面并 dump DOM（探针把结果写在 `<pre id=out>` 里）。"""
    with tempfile.TemporaryDirectory(prefix="pm-render-") as profile:
        cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
               "--disable-extensions", "--disable-background-networking",
               f"--window-size={size}", f"--user-data-dir={profile}",
               f"--virtual-time-budget={budget_ms}", "--dump-dom", url]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=budget_ms / 1000 + 45)
        except subprocess.TimeoutExpired:
            return ""
        return proc.stdout.decode("utf-8", "replace")


def run_probe(browser: str, base: str, name: str, html: str, budget_ms: int) -> dict:
    """写探针页 → 渲染 → 取回它写在 `<pre id=out>` 里的 JSON。"""
    target = ROOT / "frontend" / "dist" / f"_{name}.html"
    try:
        target.write_text(html, encoding="utf-8")
        dom = render(browser, f"{base}/app/_{name}.html", budget_ms)
        match = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
        if not match or match.group(1).strip() == "waiting":
            print(f"[WARN] 探针 {name} 没产出结果（dom {len(dom)} 字节）")
            (ROOT / "data" / "logs" / f"_{name}_debug.html").write_text(dom, encoding="utf-8")
            return {}
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            print(f"[WARN] 探针 {name} 结果不是合法 JSON（{exc}）")
            return {}
    finally:
        target.unlink(missing_ok=True)


results: list[dict] = []


def record(kind: str, path: str, ok: bool, detail: str = "") -> None:
    results.append({"kind": kind, "path": path, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {path:<44} {detail}")


def skip(path: str, detail: str) -> None:
    print(f"[SKIP] {path:<44} {detail}")


# --------------------------------------------------------------------------
def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="前端自检（默认不调用模型）")
    ap.add_argument("--base", default="http://127.0.0.1:8710")
    ap.add_argument("--paper", default="attention-is-all-you-need")
    ap.add_argument("--browser", default=None)
    ap.add_argument("--budget", type=int, default=40000, help="单页虚拟时间预算（毫秒）")
    ap.add_argument("--model", action="store_true",
                    help="额外跑一条流式问答（**会调用模型**；跑完自动删掉造出来的会话）")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 78)
    print("Cylit 前端自检" + ("（含流式问答：--model）" if args.model else "（默认零模型调用）"))
    print("=" * 78)
    browser = find_browser(args.browser)
    if not browser:
        print("[SKIP] 没找到浏览器（--browser 指定 chrome/edge 路径）")
        return 0
    status, _ = fetch(f"{base}/api/v1/agent/status", timeout=10)
    if status != 200:
        print(f"[SKIP] 主后端不可达（HTTP {status}）：先 `pr.py serve`")
        return 0

    reader_url = f"{base}/app/reader/{args.paper}?nosse=1"
    chat_url = f"{base}/app/reader/{args.paper}?side=chat&nosse=1"

    # ---- ① 阅读器 ----
    print("\n① 阅读器：正文 / 公式 / 胶囊")
    data = run_probe(browser, base, "probe_reader",
                     PROBE_READER.replace("__URL__", reader_url).replace("__TITLE__", "reader"),
                     args.budget)
    if data:
        tabs = data.get("tabs") or []
        record("reader", "侧栏页签顺序（目录第一、对话第二）",
               len(tabs) > 1 and "目录" in tabs[0] and data.get("chatTabIndex") == 1, f"{tabs[:3]}…")
        record("reader", "正文行渲染", data.get("rows", 0) > 0, f"{data.get('rows')} 行")
        record("reader", "公式渲染成 KaTeX", data.get("katex", 0) > 0, f"{data.get('katex')} 个")
        record("reader", "视图胶囊三层（托盘无描边 + 选中项是白 pill + 有阴影）",
               data.get("segBorderless") is True
               and data.get("segActiveBg") == data.get("elev")
               and data.get("segActiveShadow") is True,
               f"托盘 {data.get('segTray')} / 选中 {data.get('segActiveBg')}"
               "（工具条那枚托盘是玻璃胶囊内的半透明底，不按 --bg-sunken 判定）")
        record("reader", "侧栏页签托盘同语言（无描边 + 同一个托盘色）",
               data.get("sideBorderless") is True and data.get("sideTray") == data.get("sunken"),
               f"{data.get('sideTray')}")
        record("reader", "工具条胶囊内的按钮不投影（在托盘内部）",
               data.get("capsuleBtnCount", 0) > 0 and data.get("capsuleBtnShadow") == 0,
               f"{data.get('capsuleBtnCount')} 枚，带阴影 {data.get('capsuleBtnShadow')} 枚")

    # ---- ② 对话面板（不发消息） ----
    print("\n② 对话面板：一级页 / 二级页 / 确认框（不发消息）")
    chat = run_probe(browser, base, "probe_chat",
                     PROBE_CHAT.replace("__URL__", chat_url).replace("__TITLE__", "chat"),
                     args.budget)
    if chat:
        record("chat", "顶部工具栏只有「新对话」一个按钮",
               (chat.get("headBtns") or []) == ["新对话"], f"{chat.get('headBtns')}")
        record("chat", "分类 / 管理共 2 枚胶囊托盘（无范围选择器）",
               chat.get("trayCount") == 2 and (chat.get("segBtns") or []) == ["全部", "划词", "常规"],
               f"{chat.get('trayCount')} 枚 / {chat.get('segBtns')}")
        record("chat", "会话列表已加载（行或空态）", chat.get("listReady") is True,
               f"{chat.get('rows')} 行")
        if (chat.get("titleWithMath") or 0) > 0:
            record("chat", "标题按行内 markdown 渲染（$…$ 出 KaTeX）",
                   chat.get("titleKatex", 0) > 0, f"{chat.get('titleKatex')} 个")
        else:
            skip("标题 markdown", "现有标题里没有 `$…$`，无从判定")
        if chat.get("tickDiffer") is None:
            skip("复选框两个状态图标", "列表为空，没有可勾选的会话")
        else:
            record("chat", "复选框两个状态图标（未勾 / 勾上）", chat.get("tickDiffer") is True)
        # 下面三条都**依赖"当前论文有历史会话"**：默认跑如果清过场就没得点 → 明确 SKIP，
        # 不报假失败（`--model` 那一轮会造一个会话，所以那边能覆盖到）
        if chat.get("modalOpen") is None:
            skip("删除确认框（站内弹窗样式 + 取消不删）", "这篇论文没有历史会话，点不到删除")
        else:
            record("chat", "删除走站内弹窗（无分隔线 + 危险按钮实心红），取消不删",
                   chat.get("modalOpen") is True
                   and (chat.get("modalDividers") or ["x", "x"])[0] == "0px"
                   and chat.get("modalClosed") is True and chat.get("cancelKeptRows") is True,
                   f"标题={(chat.get('modalTitle') or '')[:14]!r} 分隔线={chat.get('modalDividers')} "
                   f"危险按钮={chat.get('modalDangerSolid')}")
        record("chat", "会话页顶部不重复「新对话」文字（只留图标钮）",
               (chat.get("threadHeadBtns") or []) == ["", ""], f"{chat.get('threadHeadBtns')}")
        record("chat", "输入框文案 = 请输入问题", chat.get("placeholder") == "请输入问题",
               f"{chat.get('placeholder')!r}")
        record("chat", "空态只有一句语录当背景（0 个按钮）",
               chat.get("helloButtons") == 0
               and (chat.get("helloText") or "").startswith("与大肥鱼一起讨论"),
               f"{(chat.get('helloText') or '')[:26]!r}")
        record("chat", "输入区是一个圆角容器（内含输入框/加号/发送）",
               bool(chat.get("boxRadius")) and all(chat.get("boxParts") or []),
               f"圆角={chat.get('boxRadius')} 部件={chat.get('boxParts')}")
        record("chat", "模型切换与上下文圆环在输入框**外**",
               chat.get("boxHasModel") is False and chat.get("boxHasRing") is False
               and all(chat.get("footParts") or []),
               f"框内={chat.get('boxHasModel')}/{chat.get('boxHasRing')} 框外={chat.get('footParts')}")
        record("chat", "上下文圆环：小（≤18px）且中间不放数字/图标",
               0 < (chat.get("ringSize") or 99) <= 18 and chat.get("ringText") == ""
               and bool(chat.get("ringTitle")),
               f"{chat.get('ringSize')}px 内容={chat.get('ringText')!r}")
        record("chat", "输入框无边框、无焦点环（边界只归容器）",
               (chat.get("inputBorder") or "").startswith("0px") and chat.get("inputShadow") == "none",
               f"{chat.get('inputBorder')} / {chat.get('inputShadow')}")
        record("chat", "多行自动增高（不超过 max-height）",
               (chat.get("inputH6") or 0) > (chat.get("inputH1") or 0)
               and (chat.get("inputMaxH") or "none") != "none",
               f"{chat.get('inputH1')}px → {chat.get('inputH6')}px（上限 {chat.get('inputMaxH')}）")
        record("chat", "空输入时发送键 disabled", chat.get("sendDisabledWhenEmpty") is True)
        if chat.get("openedConversation") is not True:
            skip("消息操作条 / 引用锚", "这篇论文没有历史会话可打开（--model 那轮会造一个）")
        else:
            record("chat", "消息操作条在气泡**下面**（复制/就地改/就地重答/开分支）",
                   chat.get("toolsAfterBody") is True
                   and any("分支" in t for t in (chat.get("toolTitles") or [])),
                   f"泡下方={chat.get('toolsAfterBody')} 按钮={chat.get('toolTitles')}")
            record("chat", "正文里的 [n] 是引用锚（带跳转信息，底部不再列 chunk 号）",
                   (chat.get("inlineCites") or 0) > 0 and (chat.get("bottomChips") or 0) == 0
                   and (chat.get("inlineWithPage") or 0) > 0 and (chat.get("inlineWithRow") or 0) > 0,
                   f"锚点 {chat.get('inlineCites')} 个（带页 {chat.get('inlineWithPage')} / "
                   f"带行 {chat.get('inlineWithRow')}），底部 chip {chat.get('bottomChips')}")

    # ---- ③ 划词 → 加入对话 → 上下文胶囊（免费：不发消息） ----
    print("\n③ 划词 → 加入对话（输入框里的上下文胶囊，不发消息）")
    sel = run_probe(browser, base, "probe_selchat",
                    PROBE_SELCHAT.replace("__URL__", chat_url).replace("__TITLE__", "selchat"),
                    args.budget)
    if sel:
        if sel.get("menuOpen") is not True:
            skip("划词 → 加入对话", f"造不出选区或菜单没出来（picked={sel.get('picked')}）")
        else:
            record("reader", "划词菜单里有「加入对话」", sel.get("hasAdd") is True,
                   f"{sel.get('menuBtns')}")
            record("chat", "选区落成输入框上沿的上下文胶囊（按钮样式 + 显示选中文字）",
                   sel.get("chipShown") is True and sel.get("chipTag") == "BUTTON"
                   and bool(sel.get("chipText")),
                   f"tag={sel.get('chipTag')} 文本={sel.get('chipText')!r}")
            record("chat", "胶囊带上**文本位置**（第 N 页 / · Markdown）",
                   str(sel.get("chipWhere") or "").startswith("第 ")
                   and "页" in str(sel.get("chipWhere") or ""),
                   f"位置={sel.get('chipWhere')!r}")
            record("chat", "胶囊可切换「这轮带不带」并直接管着发送键",
                   sel.get("chipOff0") is False and sel.get("chipOff1") is True
                   and sel.get("chipOff2") is False
                   and sel.get("sendDisabledWithChip") is False
                   and sel.get("sendDisabledWithoutChip") is True,
                   f"带={sel.get('sendDisabledWithChip')} 不带={sel.get('sendDisabledWithoutChip')}"
                   "（发送键 disabled 值：False=可发）")
            record("chat", "✕ 把胶囊整块移除（选区一起清掉）", sel.get("chipGone") is True)

    # ---- ④ 流式问答（可选） ----
    print("\n④ 流式问答（会调用模型；跑完自删会话）")
    made = 0
    if not args.model:
        skip("流式问答", "默认不跑（加 --model 才调模型）")
    else:
        question = "这张论文里 scaled dot-product 为什么要除以根号 d_k？一句话。"
        stream = run_probe(browser, base, "probe_stream",
                           PROBE_STREAM.replace("__URL__", chat_url).replace("__TITLE__", "stream")
                           .replace("__QUESTION__", question),
                           args.budget * 3)
        if stream:
            record("model", "发得出去（有内容时发送键可用）", stream.get("sendEnabled") is True)
            record("model", "回答落地（流结束后不再是流式态）",
                   bool(stream.get("answer")) and stream.get("msgs", 0) >= 2,
                   f"{stream.get('msgs')} 条：{(stream.get('answer') or '')[:30]!r}")
            record("model", "正文里的引用锚可用（底部无 chip 云）",
                   (stream.get("inlineCites") or 0) > 0 and (stream.get("bottomChips") or 0) == 0,
                   f"锚点 {stream.get('inlineCites')} 个")
            record("model", "消息操作条在气泡下面", bool(stream.get("toolTitles")))
            if stream.get("thinkHead"):
                record("model", "深度思考块：默认折叠、可展开、抬头带耗时",
                       stream.get("thinkCollapsed") is True
                       and (stream.get("thinkOpenLen") or 0) > 0
                       and "秒" in (stream.get("thinkHead") or ""),
                       f"{(stream.get('thinkHead') or '')!r} → {stream.get('thinkOpenLen')} 字")
            else:
                skip("深度思考块", "当前模型没开思考模式（parameters.thinking != enabled）")
            if stream.get("sawRowToDelete"):
                record("model", "删除确认框（站内弹窗、无分隔线），取消不删",
                       stream.get("modalOpen") is True
                       and (stream.get("modalDividers") or ["x", "x"])[0] == "0px"
                       and stream.get("modalClosed") is True
                       and stream.get("cancelKeptRows") is True,
                       f"标题={(stream.get('modalTitle') or '')[:14]!r} "
                       f"分隔线={stream.get('modalDividers')} 危险按钮={stream.get('modalDangerSolid')}")
            record("model", "没有报错", not stream.get("chatErr") and not stream.get("error"),
                   f"err={stream.get('chatErr')!r}")

        # 清理：按标题把这次造的会话删掉（不留痕）
        try:
            status_code, body = fetch(f"{base}/api/v1/agent/conversations?limit=5")
            items = json.loads(body.decode("utf-8"))["data"]["items"] if status_code == 200 else []
            victims = [c["conversation_id"] for c in items
                       if (c.get("title") or "").startswith(question[:20])]
            if victims:
                req = urllib.request.Request(
                    f"{base}/api/v1/agent/conversations/batch-delete",
                    data=json.dumps({"conversation_ids": victims}).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    made = len(json.loads(resp.read().decode("utf-8"))["data"]["deleted"])
            record("cleanup", "跑完不留痕（造出来的会话都删了）",
                   len(victims) == made, f"删除 {made}/{len(victims)} 个")
        except Exception as exc:  # noqa: BLE001
            record("cleanup", "跑完不留痕（造出来的会话都删了）", False, f"{exc}")

    print("\n" + "=" * 78)
    total, failed = len(results), sum(1 for r in results if not r["ok"])
    print(f"共 {total} 项：通过 {total - failed}，失败 {failed}")
    report = ROOT / "data" / "logs" / "frontend_check.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"base": base, "paper": args.paper, "model": args.model,
                                  "passed": total - failed, "total": total, "results": results},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
