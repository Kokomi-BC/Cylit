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
import uuid
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
    /* 侧栏开合要**按论文**记（键里带 paperId）：切到另一篇论文的标签页时各还各的。
       以前是一个全局键，A 篇收起之后 B 篇也是收起的（用户 2026-10-03）。 */
    const toggle = d.querySelector('[data-aside-toggle]');
    if (toggle) {
      res.asidePressed0 = toggle.getAttribute('aria-pressed');
      toggle.click();
      await sleep(400);
      res.asidePressed1 = toggle.getAttribute('aria-pressed');
      res.asideKeys = Object.keys(w.localStorage).filter((k) => k.indexOf('pm.reader.aside') === 0);
      toggle.click();
      await sleep(300);
    }
    /* 「目录 / 页面视图」切换（《修改计划》第二步）：两者共享同一份"读到哪" */
    const modeOutline = d.querySelector('[data-toc-mode="outline"]');
    const modePages = d.querySelector('[data-toc-mode="pages"]');
    res.tocModes = !!(modeOutline && modePages);
    if (modePages) {
      modePages.click();
      await until(d, (x) => x.querySelector('.page-thumb'));
      const thumbs = [...d.querySelectorAll('.page-thumb')];
      res.thumbCount = thumbs.length;
      res.thumbOrdered = thumbs.length > 0
        && thumbs.every((t, i) => Number(t.getAttribute('data-page')) === i + 1);
      res.thumbNums = thumbs.slice(0, 3).map((t) => ((t.querySelector('.no') || {}).textContent || '').trim());
      res.thumbSrc = thumbs.length ? (thumbs[0].querySelector('img') || {}).getAttribute('src') : null;
      res.thumbCurrent = thumbs.filter((t) => t.classList.contains('active'))
        .map((t) => Number(t.getAttribute('data-page')));
      res.thumbRatio = thumbs.length ? (() => {
        const r = thumbs[0].getBoundingClientRect();
        return [Math.round(r.width), Math.round(r.height)];
      })() : null;
      /* 取图失败 → 占位（页码与点击都还在），把第一张图指到一个不存在的页强制 404 */
      const first = thumbs[0];
      const img0 = first.querySelector('img');
      if (img0) {
        img0.setAttribute('src', '/api/v1/papers/no-such-paper/pages/99999');
        await until(d, () => first.classList.contains('failed'), 20);
      }
      res.failedPh = first.classList.contains('failed') && !!first.querySelector('.ph');
      res.failedNo = ((first.querySelector('.no') || {}).textContent || '').trim();
      /* 点第 3 页 → 跳过去（URL 带 ?page=3），再切回目录仍然正常 */
      const target = thumbs[2] || thumbs[0];
      res.jumpWanted = Number(target.getAttribute('data-page'));
      target.click();
      await sleep(1000); spin(BURN); await sleep(300);
      res.jumpUrl = w.location.search;
      res.jumpActive = [...d.querySelectorAll('.page-thumb.active')]
        .map((t) => Number(t.getAttribute('data-page')));
      modeOutline.click();
      await sleep(300);
      res.backToOutline = !!d.querySelector('.toc-item');
    }
    /* 侧栏页签也要记进 URL query（标签条按标签记路由位置）：点「对话」→ 切首页 → 切回来 */
    const sideBtn = [...d.querySelectorAll('.side-tab')]
      .find((b) => (b.textContent || '').indexOf('对话') >= 0);
    if (sideBtn) {
      sideBtn.click();
      await sleep(600);
      res.sideUrl = d.location.search;
      const homeTab = [...d.querySelectorAll('.tab')]
        .find((a) => (a.getAttribute('href') || '').indexOf('/home') >= 0);
      const selfTab = [...d.querySelectorAll('.tab')]
        .find((a) => (a.getAttribute('href') || '').indexOf('/reader/') >= 0);
      if (homeTab && selfTab) {
        homeTab.click();
        await sleep(700);
        spin(BURN);
        selfTab.click();
        await sleep(1100);
        spin(BURN);
        await sleep(300);
        const active = d.querySelector('.side-tab.active');
        res.sideBack = active ? (active.textContent || '').trim() : null;
      }
    }
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

    /* 切走页签再回来：输入框里的草稿必须**原样还在** —— 这是 `KeepAlive` 的可观测证据。
       没有它，对话面板每次切回来都是重新挂载，会话列表要重新请求一次
       （用户报的"点对话页签，历史要等一下才出来"）。 */
    if (ta) {
      const setDraft = (el, v) => {
        const setter = Object.getOwnPropertyDescriptor(w.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(el, v);
        el.dispatchEvent(new w.Event('input', { bubbles: true }));
      };
      setDraft(ta, 'keepalive-check');
      await sleep(150);
      const tabOf = (name) => [...d.querySelectorAll('.side-tab')]
        .find((b) => (b.textContent || '').includes(name));
      const tocTab = tabOf('目录');
      const chatTab = tabOf('对话');
      if (tocTab && chatTab) {
        tocTab.click();
        await sleep(250);
        res.chatUnmounted = !d.querySelector('.chat');
        chatTab.click();
        await sleep(350);
        const ta2 = d.querySelector('.chat textarea.composer-input');
        res.draftKept = ta2 ? ta2.value : null;
        res.rowsOnReturn = d.querySelectorAll('.chat .row').length;
        if (ta2) { setDraft(ta2, ''); await sleep(150); }
      }
    }
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
      /* 锚点**长什么样**也要量：它们是 `v-html` 塞进来的，scoped 样式会整片失效，
         只看元素个数会漏（用户 2026-10-03 报的"标号没辨识度"就是这么漏掉的）。 */
      if (inlines.length) {
        const cs = w.getComputedStyle(inlines[0]);
        res.citeStyle = { bg: cs.backgroundColor, color: cs.color, deco: cs.textDecorationLine,
                          fw: cs.fontWeight, cur: cs.cursor };
      }
      /* 中文句末标点紧接 `**` 时 CommonMark 收不了尾（会字面漏出 `**`）——
         只数这个形状，`a**b` 这种正常文字不算。 */
      res.badBold = [...root.querySelectorAll('.md.body')]
        .map((el) => ((el.textContent || '').match(/[。，、；：？！…—）》」』”’,.;:?!][*][*]/g) || []).length)
        .reduce((a, b) => a + b, 0);
      /* 连点**同一个**引用标号两次：第二次也必须重播高亮。
         只靠"摘类再挂回"会被样式引擎合并成"没变" → 同一个 CSSAnimation 实例接着跑
         （已经 forwards 停在 opacity:0），用户看到的就是"再点没反应"。
         所以量两件事：闪的类名在 -a/-b 之间交替，且动画实例**换了对象**。 */
      if (inlines.length) {
        const kindOf = (el) => [...el.classList].find((c) => c.indexOf('flash-') === 0) || '';
        let prev = null;
        const snap = () => {
          const el = d.querySelector('.row-pair.flash-a, .row-pair.flash-b');
          if (!el) return { kind: '', fresh: null, row: '' };
          const as = el.getAnimations ? el.getAnimations({ subtree: true }) : [];
          const a = as.filter((x) => x.effect && x.effect.pseudoElement)[0] || null;
          const o = { kind: kindOf(el), fresh: a ? (a !== prev) : null,
                      row: el.getAttribute('data-row-id') || '' };
          prev = a;
          return o;
        };
        inlines[0].click();
        await sleep(250);
        const f1 = snap();
        inlines[0].click();
        await sleep(250);
        const f2 = snap();
        res.flash = { f1, f2 };
      }
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
    /* 等流结束：固定轮数 + 每轮烧一圈真 CPU（见文件头的三条铁律）。
       结束判据换成**过程轨迹抬头**：落库那条会写"已完成，用时 …"，而流式那条带 `data-live`。 */
    for (let i = 0; i < 900; i += 1) {
      const err = root().querySelector('.err');
      if (err && (err.textContent || '').trim()) break;
      const done = [...root().querySelectorAll('.msg.assistant .run-head')]
        .some((h) => (h.textContent || '').includes('已完成'));
      if (done && !root().querySelector('.msg[data-live]')) break;
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
    /* 过程轨迹（多轮思考 + 工具）：抬头文案 + 步骤行 + 思考段可展开 */
    const run = root().querySelector('.msg.assistant .run');
    if (run) {
      res.runHead = (run.querySelector('.run-head') || {}).textContent || '';
      res.runSteps = run.querySelectorAll('.run-body > *').length;
      const think = run.querySelector('.run-step.think');
      if (think) {
        res.thinkClamped = getComputedStyle(think.querySelector('.step-text')).webkitLineClamp;
        think.click();
        await sleep(250);
        res.thinkOpen = think.classList.contains('open');
      }
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
# 探针 ④：标签页状态（切走再回来，分区与阅读位置都还在；**不碰模型**）
# --------------------------------------------------------------------------
PROBE_TABS = PROBE_HEAD + """
let started = false;
f.addEventListener('load', run);
setTimeout(() => { if (!started) run(); }, 3000);
async function run() {
  if (started) return;
  started = true;
  const w = f.contentWindow, d = f.contentDocument;
  const res = { stage: 'loaded' };
  const tabBy = (frag) => [...d.querySelectorAll('.tab')].find((a) => (a.getAttribute('href') || '').indexOf(frag) >= 0);
  const activeSec = () => {
    const b = d.querySelector('.settings-nav-item.active');
    return b ? (b.textContent || '').trim() : null;
  };
  const click = async (el, ms) => { el.click(); await sleep(ms || 600); spin(BURN); await sleep(250); };
  try {
    await until(d, (x) => x.querySelector('.settings-rail'), 30);
    res.sec0 = activeSec();
    /* 切到**最长**的那一区（索引管理）：短区根本滚不动，量出来的 0 会骗人 */
    const kb = [...d.querySelectorAll('.settings-nav-item')]
      .find((b) => (b.textContent || '').indexOf('索引') >= 0);
    await click(kb, 1800);
    res.secKb = activeSec();
    res.hashKb = d.location.hash;
    res.maxY = d.documentElement.scrollHeight - w.innerHeight;
    w.scrollTo(0, Math.min(1200, res.maxY));
    await sleep(400);
    res.scrolled = Math.round(w.scrollY);
    await click(tabBy('/home'), 700);
    await until(d, (x) => x.querySelector('.nav-card'), 20);
    res.homeY = Math.round(w.scrollY);
    await click(tabBy('/settings'), 1200);
    await until(d, (x) => x.querySelector('.settings-rail'), 25);
    await sleep(700);
    res.backHash = d.location.hash;
    res.backSec = activeSec();
    res.backY = Math.round(w.scrollY);
    out(res);
    document.title = 'done';
  } catch (err) { res.error = String(err); out(res); document.title = 'error'; }
}
</script>"""


# --------------------------------------------------------------------------
# 探针 ⑤：提示条 toast（页面顶部居中 / 三层胶囊 / 毛玻璃 / **没有左侧色条**）
# --------------------------------------------------------------------------
PROBE_TOAST = PROBE_HEAD + """
let started = false;
f.addEventListener('load', run);
setTimeout(() => { if (!started) run(); }, 3000);
async function run() {
  if (started) return;
  started = true;
  const w = f.contentWindow, d = f.contentDocument;
  const res = { stage: 'loaded' };
  const setInput = (el, v) => {
    const setter = Object.getOwnPropertyDescriptor(w.HTMLInputElement.prototype, 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new w.Event('input', { bubbles: true }));
  };
  try {
    await until(d, (x) => x.querySelector('.settings-rail input'), 30);
    /* 改一项再点「撤销改动」：纯本地动作，弹一条 info toast，不打后端也不改配置 */
    const input = d.querySelector('#general input, .sec input');
    res.hasInput = !!input;
    if (input) setInput(input, String((Number(input.value) || 0) + 1));
    await sleep(200);
    const reset = [...d.querySelectorAll('button')]
      .find((b) => (b.textContent || '').indexOf('撤销改动') >= 0);
    res.resetEnabled = !!reset && !reset.disabled;
    if (reset && !reset.disabled) reset.click();
    await until(d, (x) => x.querySelector('.toast'), 20);
    const host = d.querySelector('.toast-host');
    const toast = d.querySelector('.toast');
    const pill = d.querySelector('.toast .toast-pill');
    res.toastCount = d.querySelectorAll('.toast').length;
    res.host = host ? { pos: getComputedStyle(host).position, top: getComputedStyle(host).top,
                        pe: getComputedStyle(host).pointerEvents } : null;
    res.toast = toast ? {
      borderLeft: getComputedStyle(toast).borderLeftWidth,
      radius: getComputedStyle(toast).borderTopLeftRadius,
      glass: getComputedStyle(toast).backgroundImage.indexOf('gradient') >= 0
             && getComputedStyle(toast).backdropFilter.indexOf('blur') >= 0,
      padding: getComputedStyle(toast).padding,
      centerX: (() => { const r = toast.getBoundingClientRect(); return Math.round(r.left + r.width / 2); })(),
      top: (() => Math.round(toast.getBoundingClientRect().top))(),
      vw: w.innerWidth,
    } : null;
    const tray = d.querySelector('.toast .toast-tray');
    res.tray = tray ? { bg: getComputedStyle(tray).backgroundColor,
                        radius: getComputedStyle(tray).borderTopLeftRadius,
                        pad: getComputedStyle(tray).padding } : null;
    /* **两层**：外壳里直接是灰托盘，不该再有白色 pill（用户口径 2026-10-03） */
    res.whitePill = d.querySelectorAll('.toast .toast-pill, .toast [class*="pill"]').length;
    res.trayRef = rgbOf(w, 'var(--bg-sunken)');
    const ico = d.querySelector('.toast .toast-ico');
    res.iconColor = ico ? getComputedStyle(ico).color : null;
    res.text = toast ? (toast.textContent || '').trim().slice(0, 16) : null;
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
# 探针 ⑥：PDF 页图上的高亮（《修改计划》第三步）—— 造一条真高亮，验完删掉
#
# 为什么必须真在浏览器里做：这条路要 pdf.js 的**真文字层**（span 是异步铺出来的）、
# 要真的划出选区、要真的点菜单里的色块，还要验"刷新之后高亮回到同一段文字上"——
# 桩不出来。它写一条高亮记录，验完自己 DELETE 掉（自检不留痕）。
# --------------------------------------------------------------------------
PROBE_PDFHL = PROBE_HEAD + """
let started = false;
f.addEventListener('load', run);
setTimeout(() => { if (!started) run(); }, 1500);
/* 不等 `load`：本探针第一件事就是轮询 iframe 里的 DOM（`until`），load 早到晚到都等得到；
   而这一页要 pdf.js 下载 + 光栅化整份 PDF，等 load 会把虚拟时间预算耗在加载上
   （实测：只挂 load + 3s 兜底时，探针一次都没跑起来，`<pre>` 一直停在 waiting）。 */
run();
async function run() {
  if (started) return;
  started = true;
  const w = f.contentWindow;
  const D = () => f.contentDocument;
  const res = { stage: 'loaded' };
  const api = '/api/v1';
  const jget = async (u) => (await fetch(u)).json();
  try {
    await until(D(), (x) => x.querySelector('[data-pdf-page] .pdf-text-layer span'), 60);
    const layer = D().querySelector('[data-pdf-page] .pdf-text-layer');
    res.hasLayer = !!layer;
    if (!layer) { out(res); return; }
    const pageEl = layer.closest('[data-pdf-page]');
    res.page = Number(pageEl.getAttribute('data-pdf-page'));
    const spans = [...layer.querySelectorAll('span')]
      .filter((s) => (s.textContent || '').trim().length > 8);
    res.spans = spans.length;
    const a = spans[0] && spans[0].firstChild;
    const b = spans[1] && spans[1].firstChild;
    if (!a || !b) { out({ stage: 'no-text-node', spans: res.spans }); return; }
    const range = D().createRange();
    range.setStart(a, 0);
    range.setEnd(b, Math.min(12, (b.nodeValue || '').length));
    const sel = w.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    res.selText = String(sel.toString()).replace(/\\s+/g, ' ').slice(0, 40);
    pageEl.dispatchEvent(new w.MouseEvent('mouseup', { bubbles: true, clientX: 300, clientY: 300 }));
    await until(D(), (x) => x.querySelector('[data-selection-menu]'), 20);
    const menu = D().querySelector('[data-selection-menu]');
    res.menu = !!menu;
    if (!menu) { out(res); return; }
    res.menuText = (menu.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
    const yellow = [...menu.querySelectorAll('[title]')]
      .find((e) => (e.getAttribute('title') || '') === '黄色高亮');
    res.hasColors = !!yellow;
    if (!yellow) { out(res); return; }
    yellow.click();
    await sleep(1000); spin(BURN); await sleep(300);
    const data = await jget(api + '/highlights?paper_id=__PAPER__');
    const items = (data.data && data.data.items) || [];
    const mine = items.filter((h) => h.scope === 'pdf' && h.block_id === ('pdf:p' + res.page));
    res.saved = mine.length;
    res.savedRange = mine.length ? [mine[0].start, mine[0].end] : null;
    res.savedText = mine.length ? String(mine[0].text || '') : '';
    /* 一次划词会落成**多个** `<mark>`（跨 span 时按文字节点拆）——拼起来才是那一段文字 */
    const marks = () => [...D().querySelectorAll('[data-pdf-page] .pdf-text-layer mark.hl')];
    res.marked = marks().length;
    res.markedText = marks().map((m) => m.textContent || '').join('');
    /* 重新定位：整页刷新之后，高亮要按同一套偏移量落回**同一段文字**上 */
    f.contentWindow.location.reload();
    await sleep(1500);
    await until(D(), (x) => x.querySelector('[data-pdf-page] .pdf-text-layer mark.hl'), 60);
    const afterMarks = [...D().querySelectorAll('[data-pdf-page] .pdf-text-layer mark.hl')];
    res.afterReload = afterMarks.length;
    res.afterText = afterMarks.map((m) => m.textContent || '').join('');
    /* 收尾：删掉自检造出来的那几条（一条都不留） */
    for (const h of mine) { await fetch(api + '/highlights/' + h.id, { method: 'DELETE' }); }
    const after = await jget(api + '/highlights?paper_id=__PAPER__');
    res.left = (((after.data && after.data.items) || []).filter((h) => h.scope === 'pdf')).length;
    out(res);
  } catch (err) {
    out({ stage: 'error', error: String((err && err.message) || err) });
  }
}
</script>"""


def check_pdf_highlight(browser: str, base: str, paper: str, budget_ms: int) -> None:
    print("\n⑥ PDF 页图高亮（划词 → 存库 → 画在文字层 → 刷新后回到原处 → 自删）")
    url = f"{base}/app/reader/{paper}?left=page&nosse=1"
    html = (PROBE_PDFHL.replace("__URL__", url).replace("__TITLE__", "pdfhl")
            .replace("__PAPER__", paper))
    res = run_probe(browser, base, "probe_pdfhl", html, budget_ms)
    if not res:
        record("pdfhl", "PDF 页图能划出高亮（真文字层 + 真选区 + 真菜单）", False, "探针没产出结果")
        return
    if res.get("stage") == "error":
        record("pdfhl", "PDF 页图能划出高亮（真文字层 + 真选区 + 真菜单）", False,
               str(res.get("error"))[:90])
        return
    record("pdfhl", "PDF 页图有 pdf.js 真文字层（高亮要画的就在这层上）",
           res.get("hasLayer") is True and (res.get("spans") or 0) >= 2,
           f"第 {res.get('page')} 页，{res.get('spans')} 段文字")
    record("pdfhl", "PDF 划词菜单里有高亮色（第三步之前这里只有解释/复制）",
           res.get("hasColors") is True and "解释" in str(res.get("menuText") or ""),
           f"菜单：{res.get('menuText')}")
    record("pdfhl", "高亮存进库：scope=pdf + `pdf:p<页号>` + 文字层偏移量",
           res.get("saved") == 1 and isinstance(res.get("savedRange"), list)
           and res["savedRange"][1] > res["savedRange"][0]
           and str(res.get("savedText") or "").strip() != "",
           f"{res.get('saved')} 条，偏移 {res.get('savedRange')}，文字 {str(res.get('savedText'))[:16]!r}")
    record("pdfhl", "高亮画在 PDF 文字层上，且画的就是存下来那段文字",
           (res.get("marked") or 0) >= 1
           and str(res.get("markedText") or "").strip() == str(res.get("savedText") or "").strip(),
           f"{res.get('marked')} 段 mark：{str(res.get('markedText'))[:18]!r}")
    record("pdfhl", "刷新后高亮**回到同一段文字**上（重新定位稳）",
           (res.get("afterReload") or 0) >= 1
           and str(res.get("afterText") or "").strip() == str(res.get("savedText") or "").strip(),
           f"刷新后 {res.get('afterReload')} 段：{str(res.get('afterText'))[:18]!r}")
    record("pdfhl", "自检不留痕（造出来的高亮已删）", res.get("left") == 0,
           f"残留 {res.get('left')} 条")


# --------------------------------------------------------------------------
# 探针 ④：过程轨迹（思考 / 正文 / 工具**夹杂**）—— 造一条临时会话，验完删掉
#
# 这一节为什么要造数据：夹杂渲染只有"一轮里既有思考、又有正文、又有工具"时才看得出来，
# 而那种形状只有真跑一轮模型才有 —— 自检默认不调模型，所以自己往库里写一条
# （形状与 `backend/agent/trace.py` 完全一致），在浏览器里读它渲染成什么样。
# --------------------------------------------------------------------------

#: 造出来的轨迹：stage → 思考 → 工具 → 思考 → 工具 → 正文(半句) → 工具 → 正文(回答)
RUN_STEPS = [
    {"kind": "stage", "round": 0, "text": "正在准备论文档案…"},
    {"kind": "thinking", "round": 1, "text": "用户问的是核心贡献。"},
    {"kind": "tool", "round": 1, "tool": "paper_search", "digest": "query=multi-head",
     "status": "done", "ms": 312},
    {"kind": "thinking", "round": 2, "text": "再看一眼书架。"},
    {"kind": "tool", "round": 2, "tool": "library_list", "digest": "query=attention",
     "status": "done", "ms": 180},
    {"kind": "text", "round": 3, "text": "我先说一句。"},
    {"kind": "tool", "round": 3, "tool": "page_markdown_get", "digest": "pages=3",
     "status": "done", "ms": 1200},
    {"kind": "text", "round": 4, "text": "最终回答里带引用锚[1]，还有**加粗**。"},
]
RUN_ANSWER = "我先说一句。最终回答里带引用锚[1]，还有**加粗**。"
#: 第二条：`content` 是完整回答，但轨迹里的正文段只有半截 —— 用来看"安全网"兜不兜得住
SAFE_ANSWER = "完整回答：第一段。第二段（这一段在轨迹里是没有的）。"

PROBE_RUN = PROBE_HEAD + """
let started = false;
f.addEventListener('load', run);
setTimeout(() => { if (!started) run(); }, 3000);
async function run() {
  if (started) return;
  started = true;
  const w = f.contentWindow, d = f.contentDocument;
  const res = {};
  try {
    /* 等列表起来（与 ② 同一套判据），再动手 */
    await until(d, (x) => x.querySelector('.chat .list-head'), 20);
    const root = () => d.querySelector('.chat');
    if (!root()) { out({ error: 'no .chat' }); document.title = 'error'; return; }
    /* 打开**第一行**会话（列表按更新时间倒序 → 自检刚造的那条就在最前面） */
    for (let i = 0; i < 120 && !root().querySelector('.row .row-main'); i += 1) { spin(BURN); await sleep(50); }
    const row = root().querySelector('.row .row-main');
    res.rowTitle = row ? (row.textContent || '').trim().slice(0, 24) : '';
    if (row) row.click();
    for (let i = 0; i < 120 && !root().querySelector('.msg.assistant .run'); i += 1) { spin(BURN); await sleep(50); }
    /* 进来就该停在底部（用户口径：从历史记录进入对话默认显示对话底部）——
       必须在**点任何东西之前**量：展开/折叠会改高度。 */
    await sleep(400);
    const scroller = root().querySelector('.msgs');
    if (scroller) {
      res.scrollTop = Math.round(scroller.scrollTop);
      res.scrollHeight = Math.round(scroller.scrollHeight);
      res.bottomGap = Math.round(scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight);
      res.atBottom = res.bottomGap <= 4;
    }
    const first = () => root().querySelectorAll('.msg.assistant')[0];
    const msgs = [...root().querySelectorAll('.msg.assistant')];
    const msg = msgs[0];        // 第一条 = 夹杂的那条；第二条用来验安全网
    const run = msg ? msg.querySelector('.run') : null;
    res.opened = !!run;
    if (run) {
      res.head = (run.querySelector('.run-head') || {}).textContent || '';
      /* 历史消息**默认收起**过程轨迹（从列表进来先看对话）—— 正文在轨迹外，收起不藏正文 */
      res.defaultCollapsed = !run.querySelector('.run-body');
      res.headOnlyText = (msg.querySelector(':scope > .md.body') || {}).textContent || '';
      run.querySelector('.run-head').click();       // 展开，接着量步骤
      await sleep(250);
      /* ⚠️ 每次都**重新查**：展开会重排，抓着旧节点读容易读到已经不在文档里的那棵树 */
      const body = first().querySelector('.run-body');
      res.expanded = !!body;
      const kids = body ? [...body.children] : [];
      res.kinds = kids.map((el) => (el.classList.contains('run-text') ? 'text'
        : el.classList.contains('think') ? 'thinking'
        : el.classList.contains('stage') ? 'stage' : 'tool'));
      res.texts = kids.filter((el) => el.classList.contains('run-text')).length;
      res.traceTexts = kids.filter((el) => el.classList.contains('run-text'))
        .map((el) => (el.textContent || '').trim());
      res.strong = kids.filter((el) => el.querySelector('strong')).length;
      res.anchors = body ? body.querySelectorAll('.run-text .cite-inline').length : 0;
      res.toolLines = kids.filter((el) => el.classList.contains('tool'))
        .map((el) => (el.textContent || '').replace(/\\s+/g, ' ').trim());
      const think = body ? body.querySelector('.run-step.think') : null;
      if (think) {
        res.clamp = getComputedStyle(think.querySelector('.step-text')).webkitLineClamp;
        res.thinkText = (think.querySelector('.step-text').textContent || '').length;
        /* ⚠️ `AppIcon` 的根就是 `<svg class="icon">`：传进去的 class 会**并到这一个元素上**，
            所以选 `svg.step-caret`，不要写 `.step-caret svg`（那是"再往里找一层"，找不到）。 */
        const caret = think.querySelector('svg.step-caret');
        const ico = think.querySelector('svg.step-ico');
        const text = think.querySelector('.step-text');
        res.caretBefore = caret ? (caret.innerHTML || '').slice(0, 40) : '';
        /* 图标与文字**并排**：图标在第一行（顶对齐），不是飘在整段中间 */
        if (ico && text) {
          const a = ico.getBoundingClientRect(), b = text.getBoundingClientRect();
          res.icoBesideText = Math.abs(a.top - b.top) < 6 && a.bottom <= b.bottom + 2
            && a.right <= b.left + 2;
        }
        think.click();
        await sleep(200);
        res.thinkOpen = think.classList.contains('open');
        res.caretAfter = caret ? (caret.innerHTML || '').slice(0, 40) : '';
        res.caretAtHead = !!caret && think.firstElementChild === caret;
      }
      /* 再收起一次：步骤藏起来，回答（在轨迹外）必须还在 */
      first().querySelector('.run-head').click();
      await sleep(250);
      res.collapsedSteps = first().querySelectorAll('.run-body > *').length;
      res.collapsedTexts = first().querySelectorAll('.run-text').length;
    }
    /* 回答渲染在轨迹外：`.msg.assistant` 的直接子 `.md.body`（不是 `.run-text`） */
    const answer = msg ? msg.querySelector(':scope > .md.body') : null;
    res.outsideBody = msg ? msg.querySelectorAll(':scope > .md.body').length : -1;
    res.answerText = answer ? (answer.textContent || '').trim() : '';
    res.answerStrong = answer ? answer.querySelectorAll('strong').length : 0;
    res.answerAnchors = answer ? answer.querySelectorAll('.cite-inline').length : 0;
    /* 第二条助手消息：轨迹里的正文段**拼不出** content（模拟"轨迹被截断/清洗器留尾"）→
       安全网必须直接渲染 content（宁可少一层夹杂，也不能少一段回答）。 */
    const safe = msgs.length > 1 ? msgs[1] : null;
    if (safe) {
      const safeAnswer = safe.querySelector(':scope > .md.body');
      res.safeAnswer = safeAnswer ? (safeAnswer.textContent || '').trim() : '';
      res.safeTraceTexts = safe.querySelectorAll('.run-text').length;
      res.safeHasRun = !!safe.querySelector('.run');
    }
    out(res);
    document.title = 'done';
  } catch (err) { res.error = String(err); out(res); document.title = 'error'; }
}
</script>"""


def _internal_headers() -> dict:
    """内部令牌头（`data/secrets/agent.token`，与 agent 服务同一份）。"""
    path = ROOT / "data" / "secrets" / "agent.token"
    if not path.is_file():
        return {}
    return {"X-Internal-Token": path.read_text(encoding="utf-8").strip(),
            "Content-Type": "application/json"}


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_run_trace(browser: str, base: str, paper: str, budget_ms: int) -> None:
    print("\n④ 过程轨迹：思考 / 正文 / 工具夹杂（造一条临时会话，验完删掉）")
    head = _internal_headers()
    api = f"{base}/api/v1/agent"
    if not head:
        skip("过程轨迹渲染", "没有 data/secrets/agent.token（先启动一次主后端）")
        return
    cid = str(uuid.uuid4())          # ⚠️ 只能是 uuid：`conversation.id` 是 VARCHAR(36)
    try:
        _post_json(f"{api}/internal/messages", {
            "conversation_id": cid, "role": "user", "paper_id": paper,
            "title": "自检：过程轨迹（会被删掉）", "content": "自检用：不要回答。",
        }, head)
        _post_json(f"{api}/internal/messages", {
            "conversation_id": cid, "role": "assistant", "content": RUN_ANSWER,
            "citations": [{"ref": 1, "kind": "chunk", "page": 3, "label": "第 3 页 · 正文",
                           "snippet": "……", "ids": []}],
            "run": {"steps": RUN_STEPS, "elapsed_ms": 1179000, "rounds": 4},
            "thinking": "用户问的是核心贡献。再看一眼书架。",
        }, head)
        # 第二条：轨迹里的正文段拼不出 content（模拟"轨迹被截断"）—— 安全网必须兜住
        _post_json(f"{api}/internal/messages", {
            "conversation_id": cid, "role": "assistant", "content": SAFE_ANSWER,
            "run": {"steps": [{"kind": "tool", "round": 1, "tool": "paper_search",
                               "digest": "query=x", "status": "done", "ms": 10},
                              {"kind": "text", "round": 2, "text": "只有半截"}],
                    "elapsed_ms": 2000, "rounds": 2},
        }, head)
        url = f"{base}/app/reader/{paper}?side=chat&nosse=1"
        res = run_probe(browser, base, "probe_run",
                        PROBE_RUN.replace("__URL__", url).replace("__TITLE__", "run"),
                        budget_ms)
        if not res:
            record("run", "过程轨迹能渲染出来", False, "探针没产出结果")
            return
        record("run", "打开临时会话（第一条就是它）", res.get("opened") is True,
               f"{res.get('rowTitle')!r} err={res.get('error')!r}")
        record("run", "从历史进来：过程轨迹**默认收起**、回答照旧显示",
               res.get("defaultCollapsed") is True and res.get("expanded") is True
               and "最终回答" in (res.get("headOnlyText") or ""),
               f"默认收起={res.get('defaultCollapsed')}，收起时回答={(res.get('headOnlyText') or '')[:16]!r}")
        record("run", "进来就停在对话底部（列表→会话）",
               res.get("atBottom") is True,
               f"距底 {res.get('bottomGap')}px（可滚 {res.get('scrollHeight')}）")
        want = ["stage", "thinking", "tool", "thinking", "tool", "text", "tool"]
        record("run", "思考 / 正文 / 工具**按发生顺序夹杂**",
               (res.get("kinds") or []) == want, "→".join(res.get("kinds") or []))
        record("run", "抬头：已完成 + 用时 + 干了什么的摘要",
               "已完成" in (res.get("head") or "") and "19分" in (res.get("head") or "")
               and "已检索" in (res.get("head") or "")
               and "翻过书架" in (res.get("head") or "")
               and " 等" in (res.get("head") or ""),
               (res.get("head") or "").strip()[:56])
        record("run", "回答渲染在**轨迹外面**（不被思考圈住、也不是轨迹里的一段）",
               res.get("outsideBody") == 1 and "最终回答" in (res.get("answerText") or "")
               and "我先说一句" not in (res.get("answerText") or ""),
               f"轨迹外正文 {res.get('outsideBody')} 块：{(res.get('answerText') or '')[:24]!r}")
        record("run", "回答按 Markdown 渲染（加粗 + 引用锚可点）",
               (res.get("answerStrong") or 0) >= 1 and (res.get("answerAnchors") or 0) >= 1,
               f"加粗 {res.get('answerStrong')} 处，锚点 {res.get('answerAnchors')} 个")
        record("run", "过程里那半句留在轨迹里（夹杂的另一半）",
               res.get("traceTexts") == ["我先说一句。"],
               f"轨迹内正文段 {res.get('traceTexts')}")
        record("run", "工具段：短语 + 参数摘要（翻书架 · attention）",
               any("翻书架" in t and "attention" in t for t in (res.get("toolLines") or []))
               and any("读正文" in t for t in (res.get("toolLines") or [])),
               " / ".join((res.get("toolLines") or [])[:3]))
        record("run", "思考段：默认收成 4 行、点一下展开（箭头跟着翻）",
               str(res.get("clamp")) == "4" and res.get("thinkOpen") is True
               and bool(res.get("caretBefore")) and res.get("caretBefore") != res.get("caretAfter")
               and res.get("caretAtHead") is True,
               f"line-clamp={res.get('clamp')}，{res.get('thinkText')} 字，"
               f"箭头 {'变了' if res.get('caretBefore') != res.get('caretAfter') else '没变'}"
               f"@{'行首' if res.get('caretAtHead') else '不在行首'}")
        record("run", "思考图标与文字**并排**（图标在第一行，不飘在整段中间）",
               res.get("icoBesideText") is True, f"并排={res.get('icoBesideText')}")
        record("run", "收起过程轨迹：步骤藏起来、**回答仍在**",
               res.get("collapsedSteps") == 0 and res.get("collapsedTexts") == 0
               and res.get("outsideBody") == 1,
               f"收起后步骤 {res.get('collapsedSteps')} 行，轨迹外回答 {res.get('outsideBody')} 块")
        record("run", "轨迹拼不出正文时直接渲染 content（回答不会被截断）",
               res.get("safeHasRun") is True and res.get("safeTraceTexts") == 0
               and SAFE_ANSWER in (res.get("safeAnswer") or ""),
               f"轨迹内正文 {res.get('safeTraceTexts')} 段，回答 {(res.get('safeAnswer') or '')[:22]!r}")
    except Exception as exc:  # noqa: BLE001
        record("run", "过程轨迹能渲染出来", False, f"{exc}")
    finally:
        try:
            _post_json(f"{api}/conversations/batch-delete", {"conversation_ids": [cid]}, head)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# ⓪ Markdown 加粗：`**` 紧贴中文标点的形状（不用浏览器、不用模型）
#
# 这一节存在的原因：CommonMark 的 flanking 规则在中文里会连着翻车两次 ——
# `**一句话：……。**论文`（收尾左边是标点）与 `是**"…"**`（开场紧跟标点）都会把 `**`
# 原样漏出来。判据必须打在**真实渲染器**上（marked 18 + KaTeX + 我们自己的两处补丁），
# 而"渲染出来的字"与页面状态无关，所以直接 bundle `useMarkdown.ts` 用 node 跑，
# 比开一次无头 Chrome 便宜两个数量级，也不依赖任何数据。
# --------------------------------------------------------------------------

#: (名字, 源文, 纯文本里应当出现/等于的内容, 是否要求完全相等)
MD_SHAPES = [
    ("朴素中文", "这是**重点**内容", "这是重点内容", True),
    ("开场引号（2026-10-03 报的）",
     '是**"压缩再解压之后，向量丢了多大劲"的平均值**，就是把原来的 x',
     '是"压缩再解压之后，向量丢了多大劲"的平均值，就是把原来的 x', True),
    ("开场直角引号", "是**「压缩再解压」的平均值**，就是这样", "是「压缩再解压」的平均值，就是这样", True),
    ("开场括号", "见**（图 3）**的说明", "见（图 3）的说明", True),
    ("开场书名号", "读**《论文》**第三章", "读《论文》第三章", True),
    ("开场省略号", "它说**……**然后", "它说……然后", True),
    ("行内公式紧贴", "是**$d_k$**的缩放", "的缩放", False),
    ("行内公式根号", "用**$\\sqrt{d_k}$**归一化", "归一化", False),
    ("收尾标点（2026-10-03 上一报）", "**一句话：这是重点。**论文的说法是",
     "一句话：这是重点。论文的说法是", True),
    ("行首括号开头", "**（1）**是定义", "（1）是定义", True),
    ("行首引号开头", '**"重点"**后面', '"重点"后面', True),
    ("中文逗号收尾", "前**a，**b", "前a，b", True),
    ("两边都坏", '是**"重点"。**后面', '是"重点"。后面', True),
    # ---- 下面这些 marked 本来就认：改动后必须与原来**逐字一样**（一字不改才有对照意义）----
    ("对照：正常空格分隔", '是 **"重点"** 后面', '是 "重点" 后面', True),
    ("对照：左侧是标点", '（**"重点"**）', '（"重点"）', True),
    ("对照：数字之间的 **", "2**3**4", "234", True),
    ("对照：美元在正文里", "**x**$", "x$", True),
    ("对照：行内代码里的 **", "写法是 `**重点**` 这样", "写法是 **重点** 这样", True),
]

_MD_DRIVER = """import { readFileSync } from 'node:fs';
import { mdToHtml, inlineHtml } from './md.mjs';
const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));
console.log(JSON.stringify({
  md: cases.shapes.map((c) => mdToHtml(c)),
  inline: inlineHtml(cases.shapes[1]),
  breaks: cases.breaks.map((c) => mdToHtml(c)),
}));
"""

#: 软换行归一（2026-10-07）：PDF / 模型产物里的**硬折行**要按排版规则接回去 ——
#: 中文之间不插空格、英文之间一个空格、`-` 断词接回去；而"新块的开头"（列表项 / 表格行 /
#: 围栏 / 公式定界）与硬换行（行尾两个空格）**一律不动**。
#: 元组：(名字, 源文, 纯文本里应当出现/等于的内容, 是否要求完全相等, HTML 里必须出现的片段)
MD_BREAKS = [
    ("中文软换行不插空格", "这是第一句。\n这是第二句。", "这是第一句。这是第二句。", True, None),
    ("中文里夹英文也不插空格", "用向量量化建模\nVector Quantization 是基线",
     "用向量量化建模Vector Quantization 是基线", True, None),
    ("英文软换行 → 一个空格", "the quick brown\nfox jumps over",
     "the quick brown fox jumps over", True, None),
    ("PDF 断词接回去（quantiza-+tion）", "quantiza-\ntion is hard", "quantization is hard", True, None),
    ("硬换行（行尾两空格）保持", "第一行  \n第二行", "第一行第二行", True, "<br>"),
    ("粗体跨软换行仍闭合", "**重点**\n继续说完", "重点继续说完", True, "<strong>"),
    ("列表项之间不合并", "- 甲\n- 乙", "甲\n乙", True, "<li>"),
    ("代码围栏里的换行不动", "```\n甲\n乙\n```", "甲\n乙", True, "<pre>"),
    ("表格行不合并", "| 模型 | 准确率 |\n| --- | --- |\n| A | 96.5 |", "96.5", False, "<table"),
]


def _md_plain(html: str) -> str:
    """HTML → 纯文本（去标签 + 反转义），用来比"渲染出来的字"。"""
    text = re.sub(r"<[^>]+>", "", html)
    return (text.replace("&quot;", '"').replace("&#39;", "'").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&amp;", "&").replace("\u200b", ""))


def check_markdown_shapes() -> None:
    """把形状表喂给真实渲染器（esbuild bundle `useMarkdown.ts` → node）。"""
    print("\n⓪ Markdown 加粗：`**` 紧贴标点（不用浏览器 / 不打模型）")
    node = shutil.which("node")
    frontend = ROOT / "frontend"
    if not node or not (frontend / "node_modules" / "esbuild").exists():
        skip("markdown 加粗形状表", "没有 node / esbuild（先在 frontend 里 npm i）")
        return
    src = [c[1] for c in MD_SHAPES]
    with tempfile.TemporaryDirectory(prefix="cyl_md_") as tmp:
        tmpdir = Path(tmp)
        bundle, driver, cases = tmpdir / "md.mjs", tmpdir / "run.mjs", tmpdir / "cases.json"
        cases.write_text(json.dumps({"shapes": src, "breaks": [c[1] for c in MD_BREAKS]},
                                    ensure_ascii=False), encoding="utf-8")
        driver.write_text(_MD_DRIVER, encoding="utf-8")
        build = subprocess.run(
            [node, "-e",
             "require('esbuild').buildSync({entryPoints:['src/composables/useMarkdown.ts'],"
             "bundle:true,format:'esm',alias:{'@':'./src'},logLevel:'error',"
             "outfile:process.argv[1]})", str(bundle)],
            cwd=frontend, capture_output=True, text=True)
        if build.returncode != 0 or not bundle.exists():
            record("markdown", "加粗形状表能跑（bundle 真渲染器）", False,
                   f"bundle 失败：{build.stderr[-200:]}")
            return
        proc = subprocess.run([node, str(driver), str(cases)], cwd=tmpdir,
                              capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            record("markdown", "加粗形状表能跑（bundle 真渲染器）", False,
                   f"node 失败：{(proc.stderr or '')[-200:]}")
            return
        data = json.loads(proc.stdout)
    htmls = data.get("md") or []
    if len(htmls) != len(MD_SHAPES):
        record("markdown", "加粗形状表：`**` 紧贴标点全部闭合", False,
               f"只回 {len(htmls)} / {len(MD_SHAPES)} 条")
        return
    failed: list[str] = []
    for (name, _src, want, exact), html in zip(MD_SHAPES, htmls):
        # 定界符泄漏：`<code>` 里的 `**` 是应当保留的（那是代码，不是强调）
        leaked = "**" in re.sub(r"<code[\s\S]*?</code>", "", html)
        plain = _md_plain(html).strip()          # marked 会在块尾留一个换行
        ok = (not leaked) and (plain == want if exact else want in plain)
        if not ok:
            failed.append(f"{name}（{'漏记号' if leaked else plain[:26]}）")
    record("markdown", "加粗形状表：`**` 紧贴标点全部闭合",
           not failed, f"{len(MD_SHAPES)} 种形状全过" if not failed else "；".join(failed[:3]))
    inline_html = data.get("inline") or ""
    inline_plain = _md_plain(inline_html).strip()
    # 行内版比的是**同一张表的第 2 行**（拿表里的期望值，避免两处期望各写一份、日后走散）
    record("markdown", "加粗：行内版（标题 / 术语）同样闭合",
           "**" not in re.sub(r"<code[\s\S]*?</code>", "", inline_html)
           and inline_plain == MD_SHAPES[1][2],
           inline_plain[:40])

    # ---- 软换行归一（2026-10-07）：PDF/产物的硬折行按排版规则接回去 ----
    break_htmls = data.get("breaks") or []
    if len(break_htmls) != len(MD_BREAKS):
        record("markdown", "软换行归一：中文不插空格 / 断词接回", False,
               f"只回 {len(break_htmls)} / {len(MD_BREAKS)} 条")
        return
    failed = []
    for (name, _src, want, exact, html_has), html in zip(MD_BREAKS, break_htmls):
        plain = _md_plain(html).strip()
        ok = (plain == want if exact else want in plain)
        if html_has and html_has not in html:
            ok = False
        if not ok:
            failed.append(f"{name}（{plain[:24]}）")
    record("markdown", "软换行归一：中文不插空格 / 断词接回 / 硬换行与块结构不动",
           not failed, f"{len(MD_BREAKS)} 种形状全过" if not failed else "；".join(failed[:3]))


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
    check_markdown_shapes()          # 不依赖浏览器与服务，先跑它
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
        keys = data.get("asideKeys") or []
        record("reader", "侧栏开合按论文记（键里带 paperId，切标签各还各的）",
               bool(keys) and all(k.startswith("pm.reader.aside.") and len(k) > len("pm.reader.aside.")
                                  for k in keys)
               and data.get("asidePressed0") != data.get("asidePressed1"),
               f"{data.get('asidePressed0')} → {data.get('asidePressed1')}，键 {keys}")
        record("reader", "侧栏页签记进 URL（切标签回来还是那一栏）",
               "side=chat" in str(data.get("sideUrl") or "")
               and str(data.get("sideBack") or "").startswith("对话"),
               f"{data.get('sideUrl')} → {data.get('sideBack')}")
        # ---- 「目录 / 页面视图」切换（共享阅读位置 + 缩略图 + 占位 + 点击跳页） ----
        if not data.get("tocModes"):
            record("reader", "目录页签里有「目录 / 页面视图」切换", False, "两枚切换按钮没找到")
        else:
            record("reader", "目录页签里有「目录 / 页面视图」切换", True, "两枚切换按钮都在")
            record("reader", "页面视图：缩略图按页码排满 + 带页码 + 用页面图接口",
                   bool(data.get("thumbOrdered"))
                   and (data.get("thumbNums") or [])[:2] == ["1", "2"]
                   and "/pages/1" in str(data.get("thumbSrc") or ""),
                   f"{data.get('thumbCount')} 张，页码 {data.get('thumbNums')}，src {str(data.get('thumbSrc'))[-24:]}")
            record("reader", "页面视图：缩略图是**纵向页面比例**（不是一条横条）",
                   bool(data.get("thumbRatio")) and data["thumbRatio"][1] > data["thumbRatio"][0],
                   f"{data.get('thumbRatio')} px")
            record("reader", "页面视图：当前阅读页高亮（与目录共享同一个读到哪）",
                   isinstance(data.get("thumbCurrent"), list) and len(data["thumbCurrent"]) <= 1,
                   f"高亮页 {data.get('thumbCurrent')}")
            record("reader", "页面视图：取图失败显示占位，页码还在",
                   data.get("failedPh") is True and data.get("failedNo") == "1",
                   f"占位={data.get('failedPh')}，页码={data.get('failedNo')}")
            record("reader", "页面视图：点缩略图跳到那一页（URL 写 ?page=N）",
                   f"page={data.get('jumpWanted')}" in str(data.get("jumpUrl") or "")
                   and data.get("jumpActive") == [data.get("jumpWanted")],
                   f"想跳 {data.get('jumpWanted')}，URL {data.get('jumpUrl')}，高亮 {data.get('jumpActive')}")
            record("reader", "页面视图能切回目录（大纲还在）", data.get("backToOutline") is True, "")

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
        if chat.get("draftKept") is None:
            skip("切走页签再回来不重挂（列表不用重新请求）", "没找到页签或输入框")
        else:
            record("chat", "切走页签再回来不重挂（草稿还在 → 列表不用重新请求）",
                   chat.get("chatUnmounted") is True and chat.get("draftKept") == "keepalive-check",
                   f"切走时面板卸载={chat.get('chatUnmounted')} "
                   f"回来草稿={chat.get('draftKept')!r} 列表 {chat.get('rowsOnReturn')} 行")
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
            styleOf = chat.get("citeStyle") or {}
            record("chat", "引用锚看得出能点（淡底 + 下划线 + 700 字重 + 手型）",
                   styleOf.get("deco") == "underline" and styleOf.get("fw") in ("700", "bold")
                   and styleOf.get("cur") == "pointer" and "rgba(0, 0, 0, 0)" != styleOf.get("bg"),
                   f"{styleOf}")
            record("chat", "`**加粗**` 不漏记号（中文句末标点紧接收尾定界符）",
                   (chat.get("badBold") or 0) == 0, f"漏记号 {chat.get('badBold')} 处")
            fl = chat.get("flash") or {}
            f1 = fl.get("f1") or {}
            f2 = fl.get("f2") or {}
            record("chat", "连点同一个引用标号：每次都重播高亮（动画名 -a/-b 交替）",
                   f1.get("kind") in ("flash-a", "flash-b")
                   and f2.get("kind") in ("flash-a", "flash-b")
                   and f1.get("kind") != f2.get("kind")
                   and f2.get("fresh") is True and bool(f1.get("row")),
                   f"{f1} → {f2}")

    # ---- ④ 过程轨迹：造一条临时会话，验完删掉（免费：不调模型） ----
    check_run_trace(browser, base, args.paper, args.budget)

    # ---- ⑥ PDF 页图高亮：划词 → 存库 → 画在文字层 → 刷新后仍在 → 自删 ----
    check_pdf_highlight(browser, base, args.paper, args.budget)

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

    # ---- ④ 标签页状态：切走再回来（免费） ----
    print("\n④ 标签页状态：切走再回来，分区与阅读位置还在")
    tabs = run_probe(browser, base, "probe_tabs",
                     PROBE_TABS.replace("__URL__", f"{base}/app/settings").replace("__TITLE__", "tabs"),
                     max(args.budget, 60000))
    if tabs:
        record("tabs", "设置页的分区记进标签（切回来还是 #kb 那一区）",
               tabs.get("hashKb") == "#kb" and tabs.get("backHash") == "#kb"
               and bool(tabs.get("secKb")) and tabs.get("backSec") == tabs.get("secKb"),
               f"{tabs.get('hashKb')} / {tabs.get('secKb')} → {tabs.get('backHash')} / {tabs.get('backSec')}")
        record("tabs", "切标签回来阅读位置还在（窗口滚动回到离开时）",
               (tabs.get("scrolled") or 0) > 200
               and abs((tabs.get("backY") or 0) - (tabs.get("scrolled") or 0)) <= 60,
               f"离开 {tabs.get('scrolled')}px（可滚 {tabs.get('maxY')}）→ 回来 {tabs.get('backY')}px")

    # ---- ⑤ 提示条 toast（免费） ----
    print("\n⑤ 提示条：页面顶部居中 / 三层胶囊 / 毛玻璃 / 无左侧色条")
    tst = run_probe(browser, base, "probe_toast",
                    PROBE_TOAST.replace("__URL__", f"{base}/app/settings").replace("__TITLE__", "toast"),
                    args.budget)
    if tst:
        if tst.get("toastCount"):
            host = tst.get("host") or {}
            toast = tst.get("toast") or {}
            pill = tst.get("pill") or {}
            record("toast", "提示条在**页面顶部居中**（固定定位 + 水平居中 + 不挡点击）",
                   host.get("pos") == "fixed" and host.get("pe") == "none"
                   and abs((toast.get("centerX") or 0) - (toast.get("vw") or 0) / 2) <= 2
                   and 40 <= (toast.get("top") or 0) <= 96,
                   f"{host.get('pos')} top={host.get('top')} 中心 x={toast.get('centerX')}"
                   f"/{toast.get('vw')} 胶囊 top={toast.get('top')}")
            tray = tst.get("tray") or {}
            record("toast", "两层胶囊 + 毛玻璃（玻璃外壳 → 灰色托盘装图标文字）",
                   toast.get("glass") is True and toast.get("radius") == "24px"
                   and toast.get("padding") == "3px"
                   and tray.get("bg") == tst.get("trayRef") and tray.get("radius") == "21px"
                   and tray.get("pad") == "5px 11px"
                   and (tst.get("whitePill") or 0) == 0,
                   f"外壳 r={toast.get('radius')} 玻璃={toast.get('glass')} pad={toast.get('padding')}；"
                   f"托盘 {tray.get('bg')}（--bg-sunken={tst.get('trayRef')}）r={tray.get('radius')} "
                   f"pad={tray.get('pad')}；白色 pill {tst.get('whitePill')} 层")
            record("toast", "左侧颜色竖线已移除（级别只由图标颜色表达）",
                   toast.get("borderLeft") == "0px" and bool(tst.get("iconColor")),
                   f"左边框 {toast.get('borderLeft')}，图标 {tst.get('iconColor')}（{tst.get('text')}）")
        else:
            skip("提示条样式", f"没弹出 toast（resetEnabled={tst.get('resetEnabled')}）")

    # ---- ⑥ 流式问答（可选） ----
    print("\n⑥ 流式问答（会调用模型；跑完自删会话）")
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
            if stream.get("runHead"):
                record("model", "过程轨迹：抬头写「已完成，用时 …」+ 有步骤行",
                       "已完成" in (stream.get("runHead") or "")
                       and (stream.get("runSteps") or 0) > 0,
                       f"{(stream.get('runHead') or '').strip()[:40]!r}，"
                       f"{stream.get('runSteps')} 步")
            else:
                skip("过程轨迹", "这一轮没有轨迹（模型没调工具也没有思考模式）")
            if stream.get("thinkClamped"):
                record("model", "思考段：默认收成几行、点一下展开",
                       str(stream.get("thinkClamped")) == "4" and stream.get("thinkOpen") is True,
                       f"line-clamp={stream.get('thinkClamped')} → open={stream.get('thinkOpen')}")
            else:
                skip("思考段折叠", "这一轮没有思考段（模型没开思考模式）")
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
