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
    /* 阅读位置是**异步恢复**的（服务端记得"上次读到哪"，落位之后可能再滚一次）。滚动会按设计
       收掉引用标号的浮层（"浮层贴着正文，一滚就对不上"）—— 不等它停住，点开浮层的那一下会正好
       撞上恢复的那次滚动，浮层刚弹就被收掉（实测：citePop=false，偶尔才复现）。 */
    {
      const scWait = d.querySelector('.reader-content');
      let lastTop = -1;
      for (let i = 0; i < 16; i += 1) {
        const top = scWait ? Math.round(scWait.scrollTop) : 0;
        if (i > 1 && top === lastTop) { lastTop = top; break; }
        lastTop = top;
        await sleep(200); spin(BURN);
      }
      res.readerSettleTop = lastTop;
    }
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
      /* 2026-12-27 起"选中项"的材质由**滑块**表达（.glide-thumb.seg-glide，与侧栏页签同一枚）：
         按钮自己不再铺白底，所以这里量的是滑块 —— 它必须盖住 active 且自带阴影。 */
      const segThumb = seg.querySelector('.glide-thumb');
      res.segActiveW = Math.round(segActive.getBoundingClientRect().width);
      res.segThumb = segThumb ? {
        w: Math.round(segThumb.getBoundingClientRect().width),
        x: Math.round(segThumb.getBoundingClientRect().left),
        activeX: Math.round(segActive.getBoundingClientRect().left),
        shadow: getComputedStyle(segThumb).boxShadow !== 'none',
      } : null;
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
      await until(d, (x) => x.querySelector('.pg-item'));
      const thumbs = [...d.querySelectorAll('.pg-item')];
      res.thumbCount = thumbs.length;
      res.thumbOrdered = thumbs.length > 0
        && thumbs.every((t, i) => Number(t.getAttribute('data-page')) === i + 1);
      res.thumbNums = thumbs.slice(0, 3)
        .map((t) => ((t.querySelector('.pg-no') || {}).textContent || '').trim());
      res.thumbSrc = thumbs.length ? (thumbs[0].querySelector('img') || {}).getAttribute('src') : null;
      res.thumbCurrent = thumbs.filter((t) => t.classList.contains('active'))
        .map((t) => Number(t.getAttribute('data-page')));
      /* 一页一行（类似插图页）：第 1、2 张左边界对齐、上下错开；图占满整格宽度 */
      const r0 = thumbs[0] ? thumbs[0].getBoundingClientRect() : null;
      const r1 = thumbs[1] ? thumbs[1].getBoundingClientRect() : null;
      res.stacked = !!(r0 && r1) && Math.abs(r0.left - r1.left) <= 2 && r1.top > r0.bottom - 6;
      /* 页号在**图外面**（不在图框里）、在图上边、与格左边界对齐，且不是压在图上的徽章 */
      const numEl = thumbs[0] ? thumbs[0].querySelector('.pg-no') : null;
      const shotEl = thumbs[0] ? thumbs[0].querySelector('.pg-shot') : null;
      const ri = thumbs[0] ? thumbs[0].querySelector('img')?.getBoundingClientRect() : null;
      const rn = numEl ? numEl.getBoundingClientRect() : null;
      const rs = shotEl ? shotEl.getBoundingClientRect() : null;
      res.numAboveImg = !!(ri && rn) && rn.bottom <= ri.top + 2;
      res.numLeftAligned = !!(r0 && rn) && Math.abs(rn.left - r0.left) <= 10;
      res.numStatic = numEl ? w.getComputedStyle(numEl).position !== 'absolute' : false;
      /* 「在图片外面」：图框里**没有**页号，且页号整块在图框上边之外 */
      res.numOutsideFrame = !!shotEl && !!numEl && !shotEl.contains(numEl)
        && !!(rs && rn) && rn.bottom <= rs.top + 1;
      /* 「图不要这么大」：封顶（宽栏里不铺满整栏） */
      res.imgCapped = !!(r0 && ri) && ri.width <= 260 && ri.width >= 120;
      /* 触摸能滚：图不吃指针事件 + 整格允许竖直划动 */
      const img0El = thumbs[0] ? thumbs[0].querySelector('img') : null;
      res.imgNoPointer = img0El ? w.getComputedStyle(img0El).pointerEvents === 'none' : false;
      res.tileTouchAction = thumbs[0] ? w.getComputedStyle(thumbs[0]).touchAction : '';
      res.thumbsLeft = thumbs.length;
      res.noLocateBtn = d.querySelectorAll('.pages [title="定位"]').length === 0;
      /* 整列**居中**：第一格的中心 ≈ 滚动容器内容区的中心 */
      const pane = d.querySelector('.toc-pane');
      if (pane && r0) {
        const pr = pane.getBoundingClientRect();
        const cs = w.getComputedStyle(pane);
        const inner = { left: pr.left + parseFloat(cs.paddingLeft || '0'),
                        width: pr.width - parseFloat(cs.paddingLeft || '0')
                               - parseFloat(cs.paddingRight || '0') };
        res.paneInnerCenter = Math.round(inner.left + inner.width / 2);
        res.tileCenter = Math.round(r0.left + r0.width / 2);
        res.centered = Math.abs(res.tileCenter - res.paneInnerCenter) <= 8;
      }
      /* **这一页真的能滚**：容器自己有滚动条，且设 scrollTop 真的动得起来。
         用户报的"整个目录页彻底无法触摸滚动"就是这里坏过 —— 触摸在无头里测不了，
         但"有没有可滚动的容器、它动不动"是能测的，而那正是坏掉的那一环。 */
      if (pane) {
        res.paneScroll = [Math.round(pane.scrollHeight), Math.round(pane.clientHeight)];
        res.paneOverflowY = w.getComputedStyle(pane).overflowY;
        pane.scrollTop = 120;
        await sleep(200);
        res.paneScrolledTo = Math.round(pane.scrollTop);
        pane.scrollTop = 0;
      }
      /* 那一行里除了图标不该有文字；页面视图也不再自己写「N 页 / 点缩略图…」 */
      const bar = d.querySelector('.toc-bar');
      res.barText = bar ? (bar.textContent || '').replace(/\\s+/g, '').trim() : null;
      res.noPagesHead = !d.querySelector('.pages-head');
      res.modeLabels = [d.querySelector('[data-toc-mode="outline"]'),
                        d.querySelector('[data-toc-mode="pages"]')]
        .map((b) => (b ? (b.textContent || '').trim() : null));
      /* 取图失败 → 占位（页码与点击都还在），把第一张图指到一个不存在的页强制 404 */
      const first = thumbs[0];
      const img0 = first.querySelector('img');
      if (img0) {
        img0.setAttribute('src', '/api/v1/papers/no-such-paper/pages/99999');
        await until(d, () => first.classList.contains('failed'), 20);
      }
      res.failedPh = first.classList.contains('failed') && !!first.querySelector('.ph');
      res.failedNo = ((first.querySelector('.pg-no') || {}).textContent || '').trim();
      /* 点第 3 页 → 跳过去（URL 带 ?page=3），再切回目录仍然正常 */
      const target = thumbs[2] || thumbs[0];
      res.jumpWanted = Number(target.getAttribute('data-page'));
      target.click();
      /* 跳页是"写 URL → 再滚过去 → 当前页高亮跟着刷新"三段异步：固定等 1.3s 偶发只等到前两段
         （实测：URL 已经是 ?page=3，高亮还停在 [1]）。轮询到高亮落位为止，再留一拍。 */
      await until(d, () => {
        const act = [...d.querySelectorAll('.pg-item.active')]
          .map((x) => Number(x.getAttribute('data-page')));
        return act.length === 1 && act[0] === Number(target.getAttribute('data-page'));
      }, 24);
      await sleep(300); spin(BURN);
      res.jumpUrl = w.location.search;
      res.jumpActive = [...d.querySelectorAll('.pg-item.active')]
        .map((t) => Number(t.getAttribute('data-page')));
      modeOutline.click();
      await sleep(300);
      res.backToOutline = !!d.querySelector('.toc-item');
      /* 切回目录也验一遍"能滚"：**整页滚不动**当初是这一页的通用问题 */
      const pane2 = d.querySelector('.toc-pane');
      if (pane2) {
        res.tocPaneScroll = [Math.round(pane2.scrollHeight), Math.round(pane2.clientHeight)];
        pane2.scrollTop = 120;
        await sleep(200);
        res.tocPaneScrolledTo = Math.round(pane2.scrollTop);
        pane2.scrollTop = 0;
      }
    }
    /* 引用文献（用户口径 2026-10-08 第二条）：正文里的 `[2, 4, 10]` 是可点的 →
       点它**在标号下面弹一个只含这几条的简约浮层**（不再打开侧栏）；点条目跳到发表网站；
       鼠标移开浮层自己收掉。卡片本体与侧栏/详情弹窗共用 `ReferenceList`。 */
    /* 页签名读 `.side-label`（`textContent` 里还带着角标数字：`公式86`） */
    const labelOf = (el) => {
      const s = el ? el.querySelector('.side-label') : null;
      return s ? (s.textContent || '').trim() : null;
    };
    const tabEls = [...d.querySelectorAll('.side-tab')];
    const refsTab = tabEls.find((b) => labelOf(b) === '文献');
    res.refsTab = labelOf(refsTab);
    /* 两枚页签图标（用户口径：文献 = 双引号 `quote`、笔记 = 文稿 `note`，都用现成的 svg） */
    const iconOf = (el) => {
      const svg = el ? el.querySelector('svg') : null;
      return svg ? svg.getAttribute('data-icon') : null;
    };
    res.refsIcon = iconOf(refsTab);
    res.notesIcon = iconOf(tabEls.find((b) => labelOf(b) === '笔记'));
    res.tabLabels = tabEls.map(labelOf);
    const chips0 = () => [...d.querySelectorAll('.cite-inline[data-refs]')];
    /* 引用标号长在**正文行**上，而正文行是懒渲染的、阅读位置又会从服务端进度恢复
       （可能停在很深处 / 参考文献那一节）—— 先滚回文首等它渲染出来，否则这条时有时无。 */
    const scroller = d.querySelector('.reader-content');
    for (let i = 0; i < 24 && !chips0().length; i += 1) {
      if (scroller) scroller.scrollTop = 0;
      await sleep(250); spin(BURN);
    }
    const chips = chips0();
    res.citeChips = chips.length;
    res.citeSample = chips.length ? (chips[0].textContent || '').trim() : null;
    /* 标号的**样式**（用户口径 2026-10-07：跟对话页那个跳转标号一样）：
       只数元素个数是不够的 —— 2026-10-03 那次"标号没有辨识度"就是这么漏过去的。
       这里的几个值要和 ④ 那条对话探针里量的**同一组**（断言在 Python 侧共用一份）。 */
    res.citeStyle = chips.length ? (() => {
      const cs = w.getComputedStyle(chips[0]);
      return {
        decoration: cs.textDecorationLine || cs.textDecoration,
        bg: cs.backgroundColor, radius: cs.borderRadius,
        size: cs.fontSize, weight: cs.fontWeight,
      };
    })() : null;
    if (chips.length) {
      const refApi = await (await fetch('/api/v1/papers/__PAPER__/references')).json();
      res.apiRefs = (refApi.data || {}).count;
      /* 编号要在**实际取到的那份文献表**里：count 只说明"有几条"，编号不保证连续 1..count，
         拿"小于等于总条数"当判据会挑到一条**根本对不上**的标号（浮层随后显示"没有对应编号"）。 */
      res.apiNos = (((refApi.data || {}).items) || []).map((r) => Number(r.order_no));
      const okNos = new Set(res.apiNos);
      /* 挑一个**编号都在参考文献里**的标号来点（正文里也可能出现超过总条数的编号） */
      const numsOf = (el) => (el.getAttribute('data-refs') || '').split(',')
        .map((s) => Number(s.trim())).filter((n) => n > 0);
      /* 挑一个**编号都在参考文献里**、而且**当前就在视口里**的标号来点（正文里也可能出现超过总条数的
         编号；另外正文行是懒渲染的，滚回文首之后仍可能挑到已经滚上去的旧行 —— 那时浮层会贴着负坐标
         画到屏幕外，"压在正文之上"这条就无从量起）。 */
      const inView = (c) => {
        const r = c.getBoundingClientRect();
        return r.top > 60 && r.bottom < (w.innerHeight || 800) - 60;
      };
      const valid = (c) => numsOf(c).length && numsOf(c).every((n) => okNos.has(n));
      const chip = chips.find((c) => valid(c) && inView(c)) || chips.find(inView)
        || chips.find(valid) || chips[0];
      res.clickedRefs = numsOf(chip);
      const wantRefs = chip.getAttribute('data-refs') || '';
      /* 标号上不该再有原生 `title`（用户口径 2026-10-08："移除悬浮显示的文字（浏览器 toast）"）：
         悬浮的效果已经由浮层负责，浏览器那条提示会跟浮层叠在一起。 */
      res.citeTitle = chip.getAttribute('title');
      const tabBefore = d.querySelector('.side-tab.active');
      res.tabBeforeClick = tabBefore ? (tabBefore.textContent || '').trim() : null;
      /* ⚠️ 点之前**重新按属性取一次**这个标号：正文行是渲染出来的，上面那次 `fetch`（拿 apiRefs）
         期间它可能已经被重画 —— 点一个已经摘掉的节点，事件不会冒泡到正文容器，浮层自然不弹
         （实测偶发：`citePop=false` 而同一枚标号的悬浮那一路是好的）。点不弹就再试两轮。 */
      let pop = null;
      for (let attempt = 0; attempt < 3 && !pop; attempt += 1) {
        const chipNow = d.querySelector('.cite-inline[data-refs="' + wantRefs + '"]') || chip;
        /* 先把标号滚进视口，再按**真实指针顺序**点一遍（pointerdown → mousedown → pointerup →
           mouseup → click）。只发一个合成 click 时偶发"点不动"：标号刚从视口外滚进来、
           或者上一发 pointerdown 还没把状态清干净（实测 3 次里成功 1 次，补上这一步后稳定）。 */
        chipNow.scrollIntoView({ block: 'center' });
        await sleep(200); spin(BURN);
        const bNow = chipNow.getBoundingClientRect();
        const cx = Math.round(bNow.left + bNow.width / 2);
        const cy = Math.round(bNow.top + bNow.height / 2);
        const pe = (t, buttons) => chipNow.dispatchEvent(new w.PointerEvent(t, {
          bubbles: true, cancelable: true, pointerId: 3, pointerType: 'mouse',
          clientX: cx, clientY: cy, button: 0, buttons,
        }));
        const me = (t, buttons) => chipNow.dispatchEvent(new w.MouseEvent(t, {
          bubbles: true, cancelable: true, clientX: cx, clientY: cy, button: 0, buttons,
        }));
        pe('pointerdown', 1); me('mousedown', 1);
        pe('pointerup', 0); me('mouseup', 0);
        chipNow.click();
        await until(d, (x) => x.querySelector('[data-cite-pop]'), 12);
        pop = d.querySelector('[data-cite-pop]');
      }
      if (pop) {
        /* 参考文献是**按需取**的（`ensureReferences`）：第一次点开时浮层先显示"正在取参考文献…"，
           要等**卡片**出来再量（只 sleep 固定时间会量到一个空浮层）。 */
        /* 等它**落定**：文献还在路上时浮层只有一行「正在取参考文献…」（citePop.loading），
           这一拍读出来的行数就是 0。这里多等一轮"正在取"消失之后再读行数，并把提示文字记下来。 */
        await until(d, (x) => {
          const p = x.querySelector('[data-cite-pop]');
          if (!p) return false;
          const hint = p.querySelector('.cite-pop-hint');
          return !hint || (hint.textContent || '').indexOf('正在取') < 0;
        }, 60);
        await until(d, (x) => x.querySelector('[data-cite-pop] [data-ref-no]'), 40);
        await sleep(150); spin(BURN);
        pop = d.querySelector('[data-cite-pop]');
        res.citePopHint = pop && pop.querySelector('.cite-pop-hint')
          ? (pop.querySelector('.cite-pop-hint').textContent || '').trim().slice(0, 40) : '';
      }
      res.citePop = !!pop;
      res.citePopRows = pop ? pop.querySelectorAll('[data-ref-no]').length : 0;
      res.citePopNos = pop ? [...pop.querySelectorAll('[data-ref-no]')]
        .map((e) => Number(e.getAttribute('data-ref-no'))) : [];
      /* 卡片本体就是侧栏那份：`.ref-item` + 左边那枚跳转图标都在 */
      res.citePopCard = !!(pop && pop.querySelector('.ref-item .ref-lead'));
      /* 毛玻璃 + 真阴影（用户口径 2026-10-08："优化样式，比如毛玻璃"）：
         半透明底 + backdrop-filter 才是玻璃；阴影必须真的生效 —— `--shadow-3` 那个名字从来没定义过，
         写上去整条声明会被丢掉（浮层会变成"一点阴影都没有的白盒子"）。 */
      res.citePopGlass = pop ? (() => {
        const cs = w.getComputedStyle(pop);
        const bd = cs.backdropFilter || cs.webkitBackdropFilter || '';
        const bg = cs.backgroundColor || '';
        /* 底色可能是 `rgba(...)`，也可能是 `color(srgb … / .72)`（`color-mix` 的解析结果） */
        const m = /rgba?\\([^)]*,\\s*([\\d.]+)\\s*\\)/.exec(bg) || /\\/\\s*([\\d.]+)\\s*\\)/.exec(bg);
        return {
          blur: bd.indexOf('blur') >= 0,
          bg,
          alpha: m ? Number(m[1]) : 1,
          shadow: cs.boxShadow,
          radius: cs.borderRadius,
        };
      })() : null;
      res.citePopAbove = pop ? (() => {
        const r = pop.getBoundingClientRect();
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        const hit = d.elementFromPoint(x, y);
        const who = hit ? (hit.tagName + '.' + String(hit.className || '')).slice(0, 40) : null;
        return {
          ok: !!(hit && hit.closest && hit.closest('[data-cite-pop]')),
          who,
          rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
          inner: [w.innerWidth, w.innerHeight],
          z: w.getComputedStyle(pop).zIndex,
        };
      })() : null;
      const active = d.querySelector('.side-tab.active');
      res.activeAfterClick = active ? (active.textContent || '').trim() : null;
      /* 点条目 → 发表网站：把 window.open 拦下来看 URL（无头里别真开窗） */
      const opened = [];
      const realOpen = w.open;
      w.open = (u) => { opened.push(String(u)); return null; };
      const firstRef = pop ? pop.querySelector('[data-ref-no]') : null;
      if (firstRef) { firstRef.click(); await sleep(250); }
      w.open = realOpen;
      res.refUrl = opened[0] || null;
      /* 鼠标移开浮层 → 自己收掉（用户口径："鼠标移开浮层即消失"） */
      if (pop) {
        pop.dispatchEvent(new w.MouseEvent('mouseleave'));
        await sleep(600); spin(BURN);
      }
      res.citePopAfterLeave = !!d.querySelector('[data-cite-pop]');
      /* 悬浮 = 点一下（用户口径 2026-10-08 第三条）：`mouseover` 标号就弹**同一个**浮层，移开就收 */
      const chipAgain = d.querySelector('.cite-inline[data-refs="' + chip.getAttribute('data-refs') + '"]');
      if (chipAgain) {
        chipAgain.dispatchEvent(new w.MouseEvent('mouseover', { bubbles: true }));
        await until(d, (x) => x.querySelector('[data-cite-pop]'), 20);
        /* 光有浮层壳还不够：openCitePop 先 open、再 await placeCitePop / 把文献取回来，
           只等 [data-cite-pop] 会在"文献还在路上"的那一拍读到**空列表**（实测 [] vs [13]）。 */
        await until(d, (x) => {
          const p = x.querySelector('[data-cite-pop]');
          if (!p) return false;
          const hint = p.querySelector('.cite-pop-hint');
          return !hint || (hint.textContent || '').indexOf('正在取') < 0;
        }, 40);
        await until(d, (x) => !!x.querySelector('[data-cite-pop] [data-ref-no]'), 40);
        await sleep(200); spin(BURN);
        const popHover = d.querySelector('[data-cite-pop]');
        res.citePopHoverNos = popHover ? [...popHover.querySelectorAll('[data-ref-no]')]
          .map((e) => Number(e.getAttribute('data-ref-no'))) : [];
        chipAgain.dispatchEvent(new w.MouseEvent('mouseout', { bubbles: true }));
        await sleep(600); spin(BURN);
        res.citePopAfterHoverOut = !!d.querySelector('[data-cite-pop]');
      }
    }
    /* 复制：**公式整段选中时给 LaTeX**（用户口径 2026-10-08）。
       这条链路以前一条断言都没有 —— 量法：把选区放到一个 `.katex` 上，发一次真的 `copy` 事件
       （带一个 `DataTransfer`），看剪贴板里拿到什么；再用一段纯文字选区对照，
       确认"没有公式时一个字都不改"（那次复制不接管）。 */
    {
      const kx = [...d.querySelectorAll('.reader-content .katex')]
        .find((el) => {
          const r = el.getBoundingClientRect();
          return r.width > 6 && r.height > 4 && r.top > 40 && r.bottom < (w.innerHeight || 800) - 40;
        });
      res.hasFormula = !!kx;
      if (kx) {
        const tex = String(((kx.querySelector('annotation[encoding="application/x-tex"]') || {}).textContent) || '').trim();
        res.texExpect = tex.slice(0, 40);
        const r = kx.getBoundingClientRect();
        const rng = d.createRange();
        rng.selectNode(kx);
        const s = w.getSelection();
        s.removeAllRanges();
        s.addRange(rng);
        const dt = new w.DataTransfer();
        const ev = new w.ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: dt });
        d.dispatchEvent(ev);
        res.texCopy = dt.getData('text/plain').slice(0, 90);
        res.texCopied = !!tex && res.texCopy.indexOf(tex.slice(0, 20)) >= 0 && res.texCopy.indexOf('$') >= 0;
        /* 行间公式（`$$…$$`）另测一条：包法与行内不同 */
        const disp = [...d.querySelectorAll('.reader-content .katex-display')]
          .find((el) => (el.querySelector('annotation[encoding="application/x-tex"]') || {}).textContent);
        if (disp) {
          const dtex = String(disp.querySelector('annotation[encoding="application/x-tex"]').textContent).trim();
          const rng3 = d.createRange();
          rng3.selectNode(disp);
          s.removeAllRanges();
          s.addRange(rng3);
          const dt3 = new w.DataTransfer();
          const ev3 = new w.ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: dt3 });
          d.dispatchEvent(ev3);
          res.dispCopy = dt3.getData('text/plain').slice(0, 90);
          res.dispCopied = dtex && res.dispCopy.indexOf(dtex.slice(0, 16)) >= 0
            && res.dispCopy.indexOf('$$') === 0;
        }
        /* 对照：纯文字选区（这一段里没有公式）不该被接管 */
        const plain = [...d.querySelectorAll('.reader-content .rr-body')]
          .find((el) => !el.querySelector('.katex') && (el.textContent || '').trim().length > 20);
        if (plain) {
          const rng2 = d.createRange();
          rng2.selectNodeContents(plain);
          s.removeAllRanges();
          s.addRange(rng2);
          const dt2 = new w.DataTransfer();
          const ev2 = new w.ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: dt2 });
          d.dispatchEvent(ev2);
          res.plainCopyUntouched = !ev2.defaultPrevented && !dt2.getData('text/plain');
        }
        s.removeAllRanges();
      }
    }
    /* 右键菜单（毛玻璃，用户口径 2026-10-08）：三处界面各一套清单、按落点给项。
       量法：合成 `contextmenu`（看它有没有 `preventDefault`）、读清单里的 `data-ctx-key`，
       再点一项看真实动作（Bing 搜索 = `window.open` 收到 bing 地址）。 */
    {
      const fire = (el, x, y) => {
        const ev = new w.MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: x, clientY: y });
        el.dispatchEvent(ev);
        return ev;
      };
      const keys = () => [...d.querySelectorAll('[data-ctx-menu] [data-ctx-key]')]
        .map((e) => e.getAttribute('data-ctx-key'));
      const closeCtx = async () => {
        (d.body || d).dispatchEvent(new w.MouseEvent('pointerdown', { bubbles: true }));
        /* 菜单是**下一个 tick + 一段退场动画**之后才从 DOM 上摘：固定等 320ms 偶发读到"还开着"
           （实测复现过）。改成轮询到真的没了为止，再留一拍给动画收尾。 */
        await until(d, (x) => !x.querySelector('[data-ctx-menu]'), 12);
        await sleep(320); spin(BURN);
      };
      const body = d.querySelector('.reader-content .rr-body');
      if (body) {
        const r = body.getBoundingClientRect();
        const ev = fire(body, r.left + 10, r.top + 6);
        res.ctxPrevented = ev.defaultPrevented;
        await sleep(150); spin(BURN);                    // ⚠️ 菜单是**下一个 tick**才渲染出来的
        res.ctxKeysBlank = keys();
        res.ctxGlass = (() => {
          const p = d.querySelector('[data-ctx-menu]');
          if (!p) return null;
          const cs = w.getComputedStyle(p);
          const bg = cs.backgroundColor || '';
          const bgImage = cs.backgroundImage || '';
          /* 底色可能写在**渐变**里（2026-10-18 起菜单改成"上亮下暗的渐变本体"）：
             那时 backgroundColor 是 rgba(0,0,0,0)，浓度只能从 backgroundImage 的
             color(srgb r g b / a) 或 rgba(...) 里读 —— 两处一起看，别再只读 backgroundColor。 */
          const alphaOf = (s) => {
            const m = /rgba?\\([^)]*,\\s*([\\d.]+)\\s*\\)/.exec(s) || /\\/\\s*([\\d.]+)\\s*\\)/.exec(s);
            return m ? Number(m[1]) : 1;
          };
          /* 图标口径：每个 item 的 svg 用 currentColor 上色（填充型走 fill、线性图标走 stroke）。
             记下按钮自己的文字色当基准 —— 断言比的是"两者相等"，不是"一律填充型"（见 ReaderView
             那段说明：高亮 / 询问 Cyli / 加入对话 保留线性图标）。 */
          const icons = [...p.querySelectorAll('[data-ctx-key]')].map((b) => {
            const svg = b.querySelector('svg');
            const col = w.getComputedStyle(b).color;
            if (!svg) return { key: b.getAttribute('data-ctx-key'), none: true };
            const sc = w.getComputedStyle(svg);
            return { key: b.getAttribute('data-ctx-key'), fill: sc.fill, stroke: sc.stroke,
                     col, fillAttr: svg.getAttribute('fill'), strokeAttr: svg.getAttribute('stroke') };
          });
          /* 浓度取"真正在铺的那一层"：渐变里带 alpha 就用渐变的，否则用 backgroundColor 的。
             两段拼成一个字符串再读会**先命中** backgroundColor 的 rgba(0,0,0,0)，把 0 当浓度。 */
          const aBg = alphaOf(bg);
          const aImg = alphaOf(bgImage);
          return { blur: String(cs.backdropFilter || '').indexOf('blur') >= 0, bg, bgImage,
                   alpha: aImg < 1 ? aImg : aBg,
                   border: cs.borderTopWidth + ' ' + cs.borderTopStyle,
                   borderless: cs.borderTopStyle === 'none' || parseFloat(cs.borderTopWidth) === 0,
                   shadow: cs.boxShadow, radius: cs.borderTopLeftRadius, icons,
                   items: p.querySelectorAll('.ctx-item').length };
        })();
        await closeCtx();
        /* 划词之后再右键：该出现"询问 / 添加 / 搜索 / 复制 / 高亮" */
        const rng = d.createRange();
        rng.selectNodeContents(body);
        const s = w.getSelection();
        s.removeAllRanges();
        s.addRange(rng);
        body.dispatchEvent(new w.MouseEvent('mouseup', { bubbles: true, clientX: r.left + 10, clientY: r.top + 6 }));
        await sleep(250); spin(BURN);
        fire(body, r.left + 10, r.top + 6);
        await sleep(150); spin(BURN);
        res.ctxKeysSel = keys();
        /* 点「网页搜索（Bing）」→ window.open 收到 bing 地址 */
        const opened = [];
        const realOpen = w.open;
        w.open = (u) => { opened.push(String(u)); return null; };
        const bing = d.querySelector('[data-ctx-key="bing"]');
        if (bing) { bing.click(); await sleep(250); }
        w.open = realOpen;
        res.ctxBing = opened[0] || null;
        s.removeAllRanges();
        await closeCtx();
        res.ctxClosed = !d.querySelector('[data-ctx-menu]');
        /* 落在**图片**上：多出"询问 Cyli / 另存为 / 放大 / 加入对话" */
        const img = [...d.querySelectorAll('.reader-content img')]
          .find((el) => { const b = el.getBoundingClientRect(); return b.width > 20 && b.height > 20; });
        res.ctxHasImg = !!img;
        if (img) {
          const b = img.getBoundingClientRect();
          fire(img, b.left + 6, b.top + 6);
          await sleep(150); spin(BURN);
          res.ctxKeysImg = keys();
          res.ctxImgUrl = img.getAttribute('src') || '';
          await closeCtx();
        }
      }
    }
    /* 直挂侧栏的页签（对话 / 搜索 / 目录）**得自己撑满剩余高度**：用户口径 2026-10-07 ——
       它们原来用 `height: 100%`（= 整个侧栏高，含上面那条 58px 页签条），底边越过侧栏被裁掉。
       量法：面板底边 - 侧栏底边，> 0 就是被裁（笔记那条做对照，它在 ReaderSidePanel 里历来是对的）。 */
    const asideEl = d.querySelector('.reader-aside');
    const paneProbe = async (label, sel) => {
      const tabEl = [...d.querySelectorAll('.side-tab')]
        .find((b) => ((b.querySelector('.side-label') || {}).textContent || '').trim() === label);
      if (!tabEl) return null;
      tabEl.click();
      await until(d, (x) => x.querySelector(sel), 20);
      await sleep(300); spin(BURN); await sleep(200);
      const pane = d.querySelector(sel);
      if (!pane || !asideEl) return null;
      const pr = pane.getBoundingClientRect();
      const ar = asideEl.getBoundingClientRect();
      return {
        over: Math.round(pr.bottom - ar.bottom),
        h: Math.round(pr.height),
        asideH: Math.round(ar.height),
        scrollable: pane.scrollHeight > pane.clientHeight,
      };
    };
    res.chatPane = await paneProbe('对话', '.chat');
    res.searchPane = await paneProbe('搜索', '.side-search');
    res.notesPane = await paneProbe('笔记', '.panel-body');
    /* 胶囊（分段控件）的**样式与位置**：用户口径 2026-10-07 —— 目录 / 对话 / 公式三处统一，
       以对话页那枚为准。这里把三处都量下来（同一组值给 Python 侧比对），
       公式那处量完切回目录，别留在别的页签上影响下面的断言。 */
    const segStyle = (el, pane) => {
      if (!el || !pane) return null;
      const cs = w.getComputedStyle(el);
      const er = el.getBoundingClientRect();
      const pr = pane.getBoundingClientRect();
      return {
        h: cs.height, radius: cs.borderTopLeftRadius, bg: cs.backgroundColor,
        padTop: cs.paddingTop, btnH: el.querySelector('button')
          ? w.getComputedStyle(el.querySelector('button')).height : '',
        left: Math.round(er.left - pr.left), top: Math.round(er.top - pr.top),
      };
    };
    /* 工具条那一行的**内容左上角**（= 行盒左上角 + 它自己的 padding）：三处应当都是 8px/8px */
    const barOrigin = (row, pane) => {
      if (!row || !pane) return null;
      const cs = w.getComputedStyle(row);
      const rr = row.getBoundingClientRect();
      const pr = pane.getBoundingClientRect();
      return {
        left: Math.round(rr.left - pr.left + parseFloat(cs.paddingLeft || '0')),
        top: Math.round(rr.top - pr.top + parseFloat(cs.paddingTop || '0')),
      };
    };
    const paneOf = (sel) => d.querySelector(sel);
    const tabByLabel = (name) => [...d.querySelectorAll('.side-tab')]
      .find((b) => ((b.querySelector('.side-label') || {}).textContent || '').trim() === name) || null;
    /* 先切回「目录」：上面几条刚把页签停在「笔记」上，而 `.toc-pane` 只在目录页里 */
    const tocTab0 = tabByLabel('目录');
    if (tocTab0) { tocTab0.click(); await until(d, (x) => x.querySelector('.toc-pane .seg'), 20); await sleep(250); }
    res.tocSeg = segStyle(paneOf('.toc-pane .seg'), paneOf('.toc-pane'));
    res.tocBarPad = w.getComputedStyle(paneOf('.toc-bar') || d.body).padding;
    res.tocOrigin = barOrigin(paneOf('.toc-bar'), paneOf('.toc-pane'));
    /* 目录页：**全部收起 / 全部展开在胶囊右侧**（几何上比一比，不听类名） */
    const unfoldBtns = [...d.querySelectorAll('[data-toc-unfold]')];
    const tocSeg = paneOf('.toc-pane .seg');
    res.tocUnfoldCount = unfoldBtns.length;
    res.tocUnfoldRightOfSeg = !!tocSeg && unfoldBtns.length === 2
      && Math.min(...unfoldBtns.map((b) => b.getBoundingClientRect().left))
         >= tocSeg.getBoundingClientRect().right - 1
      && Math.round(Math.min(...unfoldBtns.map((b) => b.getBoundingClientRect().left))
                    - tocSeg.getBoundingClientRect().right) <= 12;
    const formulaTab = tabByLabel('公式');
    if (formulaTab) {
      formulaTab.click();
      await until(d, (x) => x.querySelector('.fl-bar .seg'), 20);
      await sleep(300); spin(BURN); await sleep(150);
      res.formulaSeg = segStyle(paneOf('.fl-bar .seg'), paneOf('.panel-body'));
      res.formulaOrigin = barOrigin(paneOf('.fl-bar'), paneOf('.panel-body'));
      /* 切回目录（后面的目录断言都在这一页上） */
      const tocTab = tabByLabel('目录');
      if (tocTab) { tocTab.click(); await sleep(300); spin(BURN); await sleep(150); }
    }
    /* 侧栏页签也要记进 URL query（标签条按标签记路由位置）：点「对话」→ 切首页 → 切回来 */
    const sideBtn = [...d.querySelectorAll('.side-tab')]
      .find((b) => (b.textContent || '').indexOf('对话') >= 0);
    if (sideBtn) {
      sideBtn.click();
      await sleep(600);
      res.sideUrl = d.location.search;
      /* 切走 / 切回：现在**没有「首页」那个 .tab** —— 回工作区是左上角那枚 .tabbar-logo
         （RouterLink → /cylit/home），.tab 只剩「打开的论文标签」。旧断言按 .tab href~/home 找，
         永远是 null，于是「切回来还是那一栏」这一半从来没测到（不是产品问题）。 */
      const homeTab = d.querySelector('.tabbar-logo');
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
    /* 「管理」2026-11-21 起是**多选圆钮**（.multi-btn，30×30 正圆），不再套在 .seg 托盘里 ——
       所以托盘只剩「分类」这一枚。 */
    const mbtn = root.querySelector('.filters .multi-btn');
    res.multiBtn = !!mbtn;
    if (mbtn) {
      const bb = mbtn.getBoundingClientRect();
      res.multiBtnRound = Math.round(bb.width) === Math.round(bb.height)
        && parseFloat(w.getComputedStyle(mbtn).borderTopLeftRadius) >= Math.round(bb.width) / 2;
    }
    res.segBtns = trays[0] ? [...trays[0].querySelectorAll('button')].map((b) => (b.textContent || '').trim()) : [];
    /* 工具栏与胶囊的**位置 + 样式**（用户口径 2026-10-07：目录 / 对话 / 公式三处统一，
       **以这一页为准**）。① 那条探针量了目录与公式两处，Python 侧比对三处是否一模一样。 */
    const chatHead = root.querySelector('.list-head');
    const chatSeg = trays[0] || null;
    if (chatHead && chatSeg) {
      const hr = chatHead.getBoundingClientRect();
      const cr = root.getBoundingClientRect();
      const cs = w.getComputedStyle(chatSeg);
      const bs = chatSeg.querySelector('button') ? w.getComputedStyle(chatSeg.querySelector('button')) : null;
      res.headPad = w.getComputedStyle(chatHead).padding;
      res.segStyle = {
        h: cs.height, radius: cs.borderTopLeftRadius, bg: cs.backgroundColor,
        padTop: cs.paddingTop, btnH: bs ? bs.height : '',
        left: Math.round(chatSeg.getBoundingClientRect().left - cr.left),
        top: Math.round(chatSeg.getBoundingClientRect().top - cr.top),
      };
      res.headOrigin = {
        left: Math.round(hr.left - cr.left + parseFloat(w.getComputedStyle(chatHead).paddingLeft || '0')),
        top: Math.round(hr.top - cr.top + parseFloat(w.getComputedStyle(chatHead).paddingTop || '0')),
      };
    }
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
    /* 品牌 2026-11 从顶栏搬到了**左侧栏**：这条断言（连方向相反的那条"顶栏里不该再有品牌"）
       一起搬到探针 ⓪「外壳」（`PROBE_SHELL`）—— 那里是零数据依赖的 /home，跑得动也查得准。 */
    newBtn.click();
    await until(d, (x) => x.querySelector('.chat textarea.composer-input'), 20);
    /* 顶部现在是**三枚图标钮**（返回 / 浮层打开 / 新建）：2026-11-19 把「新建」和「浮层打开」拆成
       两枚各自独立的圆钮。断言只关心「没有文字钮」，所以逐枚记 text + title。 */
    res.threadHeadBtns = [...root.querySelectorAll('.thread-head button')]
      .map((b) => ({ t: (b.textContent || '').trim(), title: b.getAttribute('title') || '' }));
    const ta = root.querySelector('textarea.composer-input');
    res.placeholder = ta ? ta.getAttribute('placeholder') : '';
    const hello = root.querySelector('.hello');
    res.helloText = ((hello || {}).textContent || '').trim();
    res.helloButtons = hello ? hello.querySelectorAll('button').length : -1;
    /* 空态那枚标记 = **应用图标**（用户口径 2026-10-07：取代原来的双引号水印），
       而且要在**居中语录的上方**、与语录同一条中线 */
    const hmark = hello ? hello.querySelector('[data-hello-mark] img') : null;
    res.helloMarkSrc = hmark ? String(hmark.getAttribute('src') || '') : '';
    if (hmark && hello) {
      const line = hello.querySelector('.hello-line');
      const mr = hmark.getBoundingClientRect();
      const lr = line ? line.getBoundingClientRect() : null;
      res.helloMarkAbove = !!lr && mr.bottom <= lr.top + 2;
      res.helloMarkCentered = Math.abs((mr.left + mr.right) / 2
        - (hello.getBoundingClientRect().left + hello.getBoundingClientRect().right) / 2) <= 3;
      res.helloMarkDebug = { markBottom: Math.round(mr.bottom),
                             lineTop: lr ? Math.round(lr.top) : -1,
                             markCx: Math.round((mr.left + mr.right) / 2),
                             helloCx: Math.round((hello.getBoundingClientRect().left
                               + hello.getBoundingClientRect().right) / 2) };
      /* 图标 + 语录这一组要在**整块高度里竖直居中**（用户口径 2026-10-07） */
      const msgs = root.querySelector('.msgs');
      const boxEl = root.querySelector('.composer-box');
      if (msgs && lr) {
        const mbox = msgs.getBoundingClientRect();
        /* 基准 = **看得见的那块高度**：输入区从 e6a008b0（输入区改悬浮圆角）起是悬浮在 .msgs
           底部的一块，.msgs 用 padding-bottom 给它让位（ReaderChat 里那句 ResizeObserver）。
           拿 .msgs 的整盒当基准会恒定把这一组算成「偏上」：实测差 59px = (112-10)/2 的让位
           + (40-24)/2 的 hello 内边距，两笔都不是「没居中」。 */
        const freeBottom = boxEl ? boxEl.getBoundingClientRect().top : mbox.bottom;
        const groupMid = (mr.top + lr.bottom) / 2;
        res.helloCenteredV = Math.abs(groupMid - (mbox.top + freeBottom) / 2) <= 40;
        res.helloMarkSize = Math.round(mr.width);
        res.helloSelfDebug = { groupMid: Math.round(groupMid),
                               paneMid: Math.round((mbox.top + freeBottom) / 2),
                               boxMid: Math.round((mbox.top + mbox.bottom) / 2),
                               mark: Math.round(mr.width) };
      }
    }
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
      /* 能用来验"连点重播高亮"的锚：它的 `data-row` 必须**在这篇论文里真的存在**。
         列表里第一条会话是**用户自己的**：里面的引用可能指向别篇、或行号在重译后已经变了 ——
         那种情况下点它没有可闪的行，不是功能坏了，是这条断言无从判定（交给 Python 侧 SKIP）。 */
      const usable = inlines.filter((b) => {
        const rid = b.dataset.row || '';
        return !!rid && !!d.querySelector('[data-row-id="' + rid + '"]');
      });
      res.usableCites = usable.length;
      if (usable.length) {
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
        usable[0].click();
        await sleep(250);
        const f1 = snap();
        usable[0].click();
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
        /* 默认折叠：只留「⌄ ✦ 思考 · 一行摘要」，正文不渲染（用户口径 2026-10-25：
           展开思维链时默认折叠所有内容）。点一下才出正文。 */
        res.thinkCollapsed = !think.classList.contains('open')
          && !!think.querySelector('.think-sum') && !think.querySelector('.step-text');
        think.click();
        await sleep(250);
        res.thinkOpen = think.classList.contains('open');
        res.thinkBodyLen = (think.querySelector('.step-text')?.textContent || '').length;
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
    /* 胶囊现在是**只带图标**的（用户口径 2026-10-08）：名字在 `title`/`aria-label` 上，不再是文字 */
    res.menuBtns = menu ? [...menu.querySelectorAll('button')]
      .map((b) => b.getAttribute('aria-label') || b.getAttribute('title') || '') : [];
    /* 「只带图标」= 每枚都**没有文字**、名字在 aria-label/title 上、里面真有 svg。
       这条以前只在 PDF 那条探针里算过（menuIconOnly），selchat 这条从来没算 → 恒为 null →
       断言永远红（是取数缺失，不是胶囊带了文字）。 */
    const menuEls = menu ? [...menu.querySelectorAll('button')] : [];
    res.menuIconOnly = menuEls.length > 0 && menuEls.every((b) => !(b.textContent || '').trim()
      && !!(b.getAttribute('aria-label') || b.getAttribute('title'))
      && !!b.querySelector('svg'));
    /* 「加入对话」已经从胶囊移走（那件事在**右键菜单**的「添加到对话框」里）：
       这里改走右键那条路 —— 合成 `mousedown(右键) + contextmenu`，再点 `tochat`。 */
    /* 右键点必须落在**选区自己的元素**上、坐标也要落在选区矩形里（右键菜单据此判"有没有选区"） */
    const rr0 = w.getSelection();
    const rng0 = rr0 && rr0.rangeCount ? rr0.getRangeAt(0) : null;
    const hostEl = rng0
      ? (rng0.startContainer.nodeType === 3 ? rng0.startContainer.parentElement : rng0.startContainer)
      : d.body;
    const rb = rng0 ? rng0.getBoundingClientRect() : (menu ? menu.getBoundingClientRect() : null);
    const hx = rb ? Math.round(rb.left + 4) : 300;
    const hy = rb ? Math.round(rb.top + 4) : 300;
    hostEl.dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true, button: 2, buttons: 2, clientX: hx, clientY: hy }));
    hostEl.dispatchEvent(new w.MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: hx, clientY: hy }));
    await sleep(200); spin(BURN);
    res.ctxHasToChat = !!d.querySelector('[data-ctx-key="tochat"]');
    res.pillOnRightClick = !!d.querySelector('[data-selection-menu]');
    const add = d.querySelector('[data-ctx-key="tochat"]');
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
  /* 2026-11 起**导航页不再进标签条**（首页 / 设置都是导航页），所以"切走再回来"改走左侧栏：
     设置页里侧栏是可见的，两个入口都点得到。`.tab` 那条老路只剩论文标签，这里用不上了。
     侧栏跳转会带上 `stores/navMemory` 记下的 hash，所以回来仍应停在 #kb 那一区。 */
  const railBy = (text) => [...d.querySelectorAll('aside.rail .rail-item')]
    .find((b) => (b.textContent || '').trim() === text);
  const activeSec = () => {
    const b = d.querySelector('.settings-nav-item.active');
    return b ? (b.textContent || '').trim() : null;
  };
  const click = async (el, ms) => {
    if (!el) return false;
    el.click(); await sleep(ms || 600); spin(BURN); await sleep(250); return true;
  };
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
    /* 侧栏那一项 2026-11 改名叫「工作区」（顶栏的常驻胶囊仍叫「首页」） */
    res.clickedHome = await click(railBy('工作区'), 700);
    await until(d, (x) => x.querySelector('#main'), 20);
    res.homeY = Math.round(w.scrollY);
    res.clickedSettings = await click(railBy('设置'), 1200);
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
# 探针 ⑤：提示条 toast（**右上角通知卡** / 标题+正文+关闭钮 / 主题令牌配色）
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
                        right: getComputedStyle(host).right,
                        pe: getComputedStyle(host).pointerEvents } : null;
    res.toast = toast ? (() => {
      const cs = getComputedStyle(toast);
      const r = toast.getBoundingClientRect();
      return {
        radius: cs.borderTopLeftRadius,
        padding: cs.padding,
        pe: cs.pointerEvents,
        bg: cs.backgroundColor,
        borderLeft: cs.borderLeftWidth, borderRight: cs.borderRightWidth,
        borderLeftColor: cs.borderLeftColor, borderRightColor: cs.borderRightColor,
        rightGap: Math.round(w.innerWidth - r.right),
        top: Math.round(r.top),
        vw: w.innerWidth,
      };
    })() : null;
    /* 卡片式通知的三件套：标题行 / 正文 / 右上角关闭钮 */
    res.title = (d.querySelector('.toast .toast-title')?.textContent || '').trim();
    res.hasClose = !!d.querySelector('.toast .toast-x');
    res.hasBody = !!d.querySelector('.toast .toast-text');
    res.iconColor = (() => { const ico = d.querySelector('.toast .toast-ico');
                             return ico ? getComputedStyle(ico).color : null; })();
    res.text = toast ? (toast.textContent || '').trim().slice(0, 16) : null;
    out(res);
    document.title = 'done';
  } catch (err) { res.error = String(err); out(res); document.title = 'error'; }
}
</script>"""


# --------------------------------------------------------------------------
# 探针 ⓪b：外壳（左侧栏 + 论文标签条）—— **零数据依赖**，起个静态服务就能跑
# --------------------------------------------------------------------------
PROBE_SHELL = PROBE_HEAD + """
let started = false;
f.addEventListener('load', run);
setTimeout(() => { if (!started) run(); }, 3000);
async function run() {
  if (started) return;
  started = true;
  const w = f.contentWindow, d = f.contentDocument;
  const res = { stage: 'loaded' };
  const midY = (r) => r.top + r.height / 2;
  try {
    await until(d, (x) => x.querySelector('aside.rail') && x.querySelector('.tabbar'), 40);
    const rail = d.querySelector('aside.rail');
    res.rail = !!rail;
    res.railW = rail ? Math.round(rail.getBoundingClientRect().width) : 0;
    res.railSticky = rail ? getComputedStyle(rail).position : null;
    res.railItems = rail
      ? [...rail.querySelectorAll('.rail-item')].map((b) => (b.textContent || '').trim()) : [];
    /* 品牌在**顶栏**（用户口径 2026-11 从侧栏搬回来）：图标在「Cylit」文字左侧、垂直居中
       （PNG 自带圆角，不要叠 CSS 圆角）。侧栏那一份必须**不存在** —— 搬走还留半份就是两只 logo。 */
    const barEl = d.querySelector('.tabbar');
    const brand = barEl ? barEl.querySelector('.tabbar-logo') : null;
    const mark = brand ? brand.querySelector('img.tabbar-mark') : null;
    res.brandMark = !!mark;
    res.brandSrc = mark ? String(mark.getAttribute('src') || '') : '';
    res.brandW = mark ? Math.round(mark.getBoundingClientRect().width) : 0;
    res.brandText = brand ? (brand.textContent || '').trim() : '';
    /* 品牌本身就是"回工作区"的入口（用户口径 2026-11）：它必须是个指到 /home 的链接 */
    res.brandHref = brand ? String(brand.getAttribute('href') || '') : '';
    res.railBrand = !!rail.querySelector('.rail-brand, img');
    if (mark && brand) {
      const mr = mark.getBoundingClientRect(), brr = brand.getBoundingClientRect();
      res.brandLeftOfText = mr.left <= brr.left + 4 && Math.abs(midY(mr) - midY(brr)) <= 6;
    }
    /* 当前项高亮：停在 /home 就该点亮「首页」。
       ⚠️ 必须**再等一次**：外壳刚挂上时路由还没落定（`route.name` 是 undefined），
       这时侧栏已经画出来了、但一枚 active 都没有 —— 早读一步就量到 None。 */
    await until(d, (x) => x.querySelector('aside.rail .rail-item.active'), 30);
    /* 量底色前先把过渡停掉：`--dump-dom` 走虚拟时钟，过渡没有帧可推进，
       计算样式会**停在 from 值**（透明底 + 灰字），量出来像"没高亮"。 */
    const noAnim = d.createElement('style');
    noAnim.textContent = '*,*::before,*::after{transition:none !important;animation:none !important}';
    d.head.appendChild(noAnim);
    await sleep(200);
    const act = rail ? rail.querySelector('.rail-item.active') : null;
    res.activeText = act ? (act.textContent || '').trim() : null;
    res.activeBg = act ? getComputedStyle(act).backgroundColor : null;
    res.activeColor = act ? getComputedStyle(act).color : null;
    /* 顶栏：**只有**胶囊托盘 + 论文标签（+ 两端浮动圆钮）。不该有品牌、不该有固定「首页」胶囊、
       不该有「+」新建入口、也不该有「AI 对话」胶囊 —— 那些入口都在左侧栏。 */
    const bar = d.querySelector('.tabbar');
    res.tabbar = !!bar;
    res.tabbarBrand = bar ? !!bar.querySelector('.tabbar-logo, img') : false;
    res.tabbarPinned = bar ? !!bar.querySelector('.tab.pinned') : false;
    res.pinnedText = bar && bar.querySelector('.tab.pinned')
      ? (bar.querySelector('.tab.pinned').textContent || '').trim() : null;
    res.tabAdd = bar ? !!bar.querySelector('.tab-add') : false;
    res.chatPill = bar ? !!bar.querySelector('.chat-pill') : false;
    /* 一枚标签都没有时托盘整块不渲染（顶栏只是一条空条，别留一枚空胶囊） */
    res.stripRendered = bar ? !!bar.querySelector('.tab-strip') : false;
    res.tabs = [...d.querySelectorAll('.tab-strip .tab')].map((a) => (a.textContent || '').trim());
    res.path = d.location.pathname;

    /* ---- 几何：顶栏**通栏**、侧栏在顶栏**下方** ---- */
    /* 宽度量**布局视口**（clientWidth）：页面出竖向滚动条时 innerWidth 还含滚动条那几像素，
       拿它比 "顶栏右边缘" 会差出一个滚动条的宽度（实测 1494 vs 1500）。 */
    const viewW = d.documentElement.clientWidth || w.innerWidth, viewH = w.innerHeight;
    const br = bar ? bar.getBoundingClientRect() : null;
    const rr = rail ? rail.getBoundingClientRect() : null;
    res.viewW = Math.round(viewW);
    res.viewH = Math.round(viewH);
    res.tabbarLeft = br ? Math.round(br.left) : null;
    res.tabbarRight = br ? Math.round(br.right) : null;
    res.headerH = br ? Math.round(br.height) : null;
    res.railTop = rr ? Math.round(rr.top) : null;
    res.railBottom = rr ? Math.round(rr.bottom) : null;

    /* ---- 手动收缩：64px 图标轨 ↔ 216px ---- */
    const toggle = rail ? rail.querySelector('.rail-toggle') : null;
    const labelHidden = () => {
      const l = rail ? rail.querySelector('.rail-label') : null;
      return !l || getComputedStyle(l).display === 'none' || l.offsetWidth === 0;
    };
    res.toggle = !!toggle;
    res.wideW = res.railW;
    res.wideLabelVisible = !labelHidden();
    if (toggle) {
      toggle.click(); await sleep(500); spin(BURN); await sleep(250);
      res.narrowW = Math.round(rail.getBoundingClientRect().width);
      res.narrowLabelHidden = labelHidden();
      res.narrowTitle = (rail.querySelector('.rail-item') || {}).title || null;
      toggle.click(); await sleep(500); spin(BURN); await sleep(250);
      res.backW = Math.round(rail.getBoundingClientRect().width);
      res.backLabelVisible = !labelHidden();
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
        dom = render(browser, f"{base}/cylit/_{name}.html", budget_ms)
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


def _cite_style_ok(style: dict | None) -> bool:
    """引用标号的观感（用户口径 2026-10-07：正文里那枚要跟**对话页的跳转标号**一样）。

    只数元素个数是不够的：2026-10-03 那次"标号没有辨识度"就是只数了个数、没量样式。
    这一组判据在**两处共用**（① 正文标号 / ④ 对话页标号）—— 两边都过，才算"同一套样式"。
    """
    if not style:
        return False
    return ("underline" in str(style.get("decoration") or "")
            and str(style.get("bg") or "") not in ("", "transparent", "rgba(0, 0, 0, 0)")
            and str(style.get("radius") or "") not in ("", "0px")
            and style.get("size") == "11px" and style.get("weight") == "700")


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
  const MODEL = '__MODEL__' === '1';
  try {
    /* 开工前先清场：**只清本探针自己会造的东西**（`scope=pdf` 的高亮与笔记）。
       上一次跑挂在半路时（探针报错、Chrome 被打断）会留下残留，不清的话这次会数出 2 条，
       断言全乱 —— 而这些记录只可能来自自检本身（用户的高亮都是 row/markdown）。 */
    const sc0 = () => D().querySelector('.reader-content');
    const pre = await jget(api + '/highlights?paper_id=__PAPER__');
    for (const h of ((pre.data && pre.data.items) || []).filter((x) => x.scope === 'pdf')) {
      await fetch(api + '/highlights/' + h.id, { method: 'DELETE' });
    }
    const preNotes = await jget(api + '/notes?paper_id=__PAPER__');
    for (const n of ((preNotes.data && preNotes.data.items) || []).filter((x) => x.scope === 'pdf')) {
      await fetch(api + '/notes/' + n.id, { method: 'DELETE' });
    }
    /* 回到第 1 页再开工：阅读器会**恢复上次的阅读位置**，而"第一个 `[data-pdf-page]`"
       只有在页 1 就在附近时才等于第 1 页（顺序渲染，前面的页可能还没建）。 */
    const scroller0 = sc0();
    if (scroller0) { scroller0.scrollTop = 0; }
    await sleep(600); spin(BURN); await sleep(200);
    /* 等"够用的一段文字"：文字层是**异步**铺出来的，只等一个 span 会在铺到一半时开跑
       （实测：只等到 1 个 span 就选，`spans[1]` 是 undefined → 整条探针报错）。
       页号挑**明确写死的那一页**（`[data-pdf-page="1"]`），不靠"DOM 里第一个"。 */
    const longSpans = () => [...D().querySelectorAll('[data-pdf-page="1"] .pdf-text-layer span')]
      .filter((s) => (s.textContent || '').trim().length > 8);
    await until(D(), () => longSpans().length >= 2, 60);
    const layer = D().querySelector('[data-pdf-page="1"] .pdf-text-layer');
    res.hasLayer = !!layer;
    if (!layer) { out(res); return; }
    /* 取 span 里的**文本节点**：画过高亮之后 `firstChild` 可能是包文字的 `<mark>` 元素，
       拿元素当 Range 的端点会变成"按子节点下标"（实测报 no child at offset 12）。 */
    const firstText = (el) => {
      if (!el) return null;
      if (el.nodeType === 3) return el;
      return D().createTreeWalker(el, NodeFilter.SHOW_TEXT).nextNode();
    };
    const pair = (list) => [firstText(list[0]), firstText(list[1])];
    const pageEl = layer.closest('[data-pdf-page]');
    res.page = Number(pageEl.getAttribute('data-pdf-page'));
    const spans = longSpans();
    res.spans = spans.length;
    const a = pair(spans)[0];
    const b = pair(spans)[1];
    if (!a || !b) { out({ stage: 'few-spans', spans: res.spans }); return; }
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
    /* 胶囊**只带图标**（用户口径 2026-10-08："移除文字，只保留图标"）：名字在 aria-label / title 上；
       而且它现在也是**同款毛玻璃**、层级要**高过右键菜单**（右键时两个同时在场上）。 */
    const menuBtns = [...menu.querySelectorAll('button')];
    res.menuExplain = menuBtns.some((b) => ((b.getAttribute('aria-label') || '') + (b.getAttribute('title') || '')).indexOf('解释') >= 0);
    res.menuIconOnly = menuBtns.every((b) => !(b.textContent || '').trim());
    res.pillGlass = (() => {
      const cs = w.getComputedStyle(menu);
      const bg = cs.backgroundColor || '';
      const m = /rgba?\\([^)]*,\\s*([\\d.]+)\\s*\\)/.exec(bg) || /\\/\\s*([\\d.]+)\\s*\\)/.exec(bg);
      return { blur: String(cs.backdropFilter || '').indexOf('blur') >= 0, bg,
               bgImage: cs.backgroundImage || '', alpha: m ? Number(m[1]) : 1,
               border: cs.borderTopWidth + ' ' + cs.borderTopStyle,
               z: Number(cs.zIndex) || 0 };
    })();
    /* 还没标高亮时，菜单里**不该有「笔记」**（用户口径：插入了高亮才能在高亮上加笔记） */
    res.menuBeforeHl = [...menu.querySelectorAll('button')]
      .map((b) => (b.getAttribute('aria-label') || b.getAttribute('title') || '').trim()).filter(Boolean);
    /* 色点：**2026-12 起配色是数据驱动的** —— 色板只有多巴胺那 8 个，颜色写在行内 `--hl-sw`，
       `.hl-swatch.<色名>` 这类按色名的类已经没有了（改一次色板就删一批类，本来就不该有）。
       所以按**结构**找第一颗色点，标题只认「标成…」这个前缀，别再写死某个颜色。 */
    const swatch = menu.querySelector('.hl-swatch')
      || [...menu.querySelectorAll('[title]')]
        .find((e) => /标成/.test(e.getAttribute('title') || ''));
    res.hasColors = !!swatch;
    res.swatchTitle = swatch ? (swatch.getAttribute('title') || '') : '';
    if (!swatch) { out(res); return; }
    swatch.click();
    await sleep(1000); spin(BURN); await sleep(300);
    /* 用户口径（2026-10-07）：**选中并高亮之后，浮层要显示「添加笔记」** ——
       标完那一下就该有这么一枚按钮，不必再划一次词、再进一次菜单。
       （必须在这里读：下面那句 `location.reload()` 会把浮层一起刷掉。） */
    /* 形态（2026-11-28 之后）：标完色**直接开出笔记浮层、而且就在写的那一步** ——
       卡片本身就是输入框，「添加笔记」那枚按钮没有入口了（提示文案也按 2026-10-10 删掉）。 */
    await until(D(), (x) => x.querySelector('textarea[data-note-edit]'), 20);
    const hlCard = D().querySelector('[data-note-popover]');
    res.hasAddNoteBtn = !!(hlCard && hlCard.querySelector('textarea[data-note-edit]'));
    res.afterHlCard = hlCard
      ? ((hlCard.querySelector('[data-float-head]') || {}).textContent || '').trim().slice(0, 30) : '';
    /* 浮层可以用鼠标拖动（用户口径 2026-10-07 第二条）：按住标题栏往右下挪，
       松手后**停在挪到的位置**；此后内容从"提示态"变成"编辑态"（变高）也不许弹回去。 */
    const head = D().querySelector('[data-float-head]');
    const card0 = D().querySelector('[data-float-card]');
    const r0 = card0 ? card0.getBoundingClientRect() : null;
    res.hasHead = !!head;
    if (head && r0) {
      const opt = (t, x, y) => new w.PointerEvent(t, {
        bubbles: true, cancelable: true, pointerId: 7, pointerType: 'mouse', clientX: x, clientY: y,
      });
      head.dispatchEvent(opt('pointerdown', r0.left + 24, r0.top + 9));
      head.dispatchEvent(opt('pointermove', r0.left + 24 + 110, r0.top + 9 + 55));
      await sleep(150); spin(BURN); await sleep(100);
      const moved = D().querySelector('[data-float-card]');
      const r1 = moved ? moved.getBoundingClientRect() : null;
      res.dragDx = r1 ? Math.round(r1.left - r0.left) : null;
      res.dragDy = r1 ? Math.round(r1.top - r0.top) : null;
      head.dispatchEvent(opt('pointerup', r0.left + 134, r0.top + 64));
      /* 换到编辑态（卡片变高）：拖过的位置要留着 */
      /* 「内容变化」= 往卡片里的输入框写一段长的（卡片会变高）：拖过的位置要留着 */
      const ta0 = D().querySelector('textarea[data-note-edit]');
      if (ta0) {
        ta0.value = '自检：写长一点让卡片变高，看位置会不会弹回去（这句是故意写长的）';
        ta0.dispatchEvent(new w.Event('input', { bubbles: true }));
        await sleep(300); spin(BURN); await sleep(200);
      }
      const r2 = D().querySelector('[data-float-card]')?.getBoundingClientRect();
      res.dragKeptDx = (r1 && r2) ? Math.round(r2.left - r1.left) : null;
      res.dragKeptDy = (r1 && r2) ? Math.round(r2.top - r1.top) : null;
    }
    const data = await jget(api + '/highlights?paper_id=__PAPER__');
    const items = (data.data && data.data.items) || [];
    const mine = items.filter((h) => h.scope === 'pdf' && h.block_id === ('pdf:p' + res.page));
    res.saved = mine.length;
    res.savedRange = mine.length ? [mine[0].start, mine[0].end] : null;
    res.savedText = mine.length ? String(mine[0].text || '') : '';
    /* 一次划词会落成**多个** `<mark>`（跨 span 时按文字节点拆）——拼起来才是那一段文字 */
    const marks = () => [...D().querySelectorAll('[data-pdf-page="1"] .pdf-text-layer mark.hl')];
    res.marked = marks().length;
    res.markedText = marks().map((m) => m.textContent || '').join('');
    /* 重新定位：整页刷新之后，高亮要按同一套偏移量落回**同一段文字**上。
       ⚠️ 刷新后必须先把阅读位置钉在**页 1**，否则这条断言无从谈起：阅读器会恢复
       "上次读到哪"（服务端进度，可能是用户自己读到很深的地方）→ 页 1 不在渲染范围里 →
       文字层一个 span 都没有（实测 `afterSpans=0`，整轮里偶发，单跑必过）。
       两手一起用：URL 带 `?page=1` 让应用自己跳回页 1，再一边轮询一边把 `scrollTop` 按回 0。 */
    let pageHref = '';
    try {
      const u = new URL(f.contentWindow.location.href);
      u.searchParams.set('page', '1');
      pageHref = u.toString();
      f.contentWindow.history.replaceState(null, '', pageHref);
    } catch (e) { res.pageUrlErr = String(e); }
    let afterSpans = 0;
    /* 等**刷新后的文字层**：pdf.js 要重新下载 + 光栅化，机器忙（别的构建/测试在跑）时
       5 秒根本不够 —— 实测整轮十几条红就是从这一条倒下来的（`afterSpans=0` 之后侧栏、笔记、
       定位全都读不到东西）。给到 ~24 秒，并顺手把阅读位置按回页首。

       **重来两次**：第一次原地 reload（快）；还不行就整页导航一次（干净启动 ——
       pdf.js 那次重铺失败多半是这条路救回来的）。两次都还是 0 段 = 环境/时序问题，
       由 Python 那侧把后续断言降级成 skip，不再连累成一串红。 */
    for (let attempt = 0; attempt < 2 && afterSpans < 2; attempt += 1) {
      try {
        if (attempt === 0 || !pageHref) f.contentWindow.location.reload();
        else f.contentWindow.location.replace(pageHref);
      } catch (e) { res.reloadErr = String(e); }
      await sleep(1500);
      for (let i = 0; i < 60; i += 1) {
        const sc = sc0();
        if (sc) sc.scrollTop = 0;
        await sleep(250); spin(BURN);
        const l = D().querySelector('[data-pdf-page="1"] .pdf-text-layer');
        afterSpans = l ? l.querySelectorAll('span').length : 0;
        if (afterSpans >= 2) break;
      }
    }
    res.afterSpans = afterSpans;
    await until(D(), (x) => x.querySelector('[data-pdf-page="1"] .pdf-text-layer mark.hl'), 40);
    const afterMarks = [...D().querySelectorAll('[data-pdf-page="1"] .pdf-text-layer mark.hl')];
    res.afterReload = afterMarks.length;
    res.afterText = afterMarks.map((m) => m.textContent || '').join('');

    /* 侧栏「笔记」页签：**还没写笔记的高亮也在这个页签里**（批注/高亮/笔记已合并成笔记） */
    const notesTab = [...D().querySelectorAll('.side-tab')]
      .find((b) => (b.textContent || '').indexOf('笔记') >= 0);
    res.notesTab = notesTab ? (notesTab.textContent || '').trim() : '';
    if (notesTab) {
      notesTab.click();
      await until(D(), (x) => x.querySelector('[data-item-kind="highlight"]'), 30);
      const bare = D().querySelector('[data-item-kind="highlight"]');
      res.bareRow = bare ? (bare.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 70) : '';
    }

    /* ---- 第四步：高亮 → 笔记（PDF/Markdown → Highlight → Note 的闭环）---- */
    const longSpans2 = () => [...D().querySelectorAll('[data-pdf-page="1"] .pdf-text-layer span')]
      .filter((s) => (s.textContent || '').trim().length > 8);
    await until(D(), () => longSpans2().length >= 2, 60);
    /* 每次都**重新取**文本节点：`markRanges` 会把文字节点包进 `<mark>`（节点对象换掉），
       抓着上一轮的节点造选区，可能在已经脱离文档的节点上建 Range。 */
    const pickPair = () => pair(longSpans2());
    /* 划一次词进菜单。**要重试**：这条路的选区依赖真实指针状态（文字层 span 是异步铺的、
       上一次交互残留的选区也可能把 `sel.isCollapsed` 搅成真），单发一次偶发不出菜单 ——
       与下面「打开笔记」那段同一个写法（先 mousedown 再 mouseup，失败隔 300ms 再来一轮）。 */
    let menu2 = null;
    for (let attempt = 0; attempt < 3 && !menu2; attempt += 1) {
      const pp = pickPair();
      if (!pp[0] || !pp[1]) break;
      const rr = D().createRange();
      rr.setStart(pp[0], 0);
      rr.setEnd(pp[1], Math.min(12, (pp[1].nodeValue || '').length));
      const ss = w.getSelection();
      ss.removeAllRanges();
      ss.addRange(rr);
      const pg = D().querySelector('[data-pdf-page="1"]');
      pg.dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true, clientX: 300, clientY: 300, buttons: 1 }));
      pg.dispatchEvent(new w.MouseEvent('mouseup', { bubbles: true, clientX: 300, clientY: 300 }));
      await until(D(), (x) => x.querySelector('[data-selection-menu]'), 20);
      menu2 = D().querySelector('[data-selection-menu]');
      if (!menu2) await sleep(300);
    }
    res.menu2 = !!menu2;
    const noteBtn = menu2
      ? [...menu2.querySelectorAll('button')]
        .find((b) => ((b.getAttribute('aria-label') || '') + (b.getAttribute('title') || '')).indexOf('笔记') >= 0)
      : null;
    res.noteBtn = noteBtn
      ? (noteBtn.getAttribute('aria-label') || noteBtn.getAttribute('title') || '').trim() : '';
    if (noteBtn) {
      noteBtn.click();
      /* 编辑器 = 浮层里那枚 textarea（data-note-edit）；提示文案 2026-10-10 已删，所以不再看
         placeholder；保存是**图标键**（data-note-save，title=保存笔记），不是写着"保存"的文字钮。 */
      await until(D(), (x) => x.querySelector('textarea[data-note-edit]'), 20);
      const box = D().querySelector('textarea[data-note-edit]');
      res.editor = !!box;
      if (box) {
        box.value = '自检：这条笔记挂在 PDF 高亮上';
        box.dispatchEvent(new w.Event('input', { bubbles: true }));
        await sleep(200);
        const save = D().querySelector('[data-note-save]');
        res.saveBtn = !!save;
        if (save) save.click();
        await sleep(1000); spin(BURN); await sleep(300);
      }
    }
    const ndata = await jget(api + '/notes?paper_id=__PAPER__');
    const allNotes = (ndata.data && ndata.data.items) || [];
    const myNote = mine.length ? allNotes.find((n) => n.highlight_id === mine[0].id) : null;
    res.noteSaved = myNote ? 1 : 0;
    res.noteScope = myNote ? myNote.scope : '';
    res.notePage = myNote && myNote.page != null ? myNote.page : null;
    res.noteQuote = myNote ? String(myNote.selected_text || '') : '';
    /* 侧栏「笔记」页签要显示：高亮原文 + 挂在哪一面 + 页码，并且点「定位」能跳回去 */
    const sideBtn = [...D().querySelectorAll('.side-tab')]
      .find((b) => (b.textContent || '').indexOf('笔记') >= 0);
    if (sideBtn) sideBtn.click();
    await until(D(), (x) => x.querySelector('[data-note-id]'), 30);
    const row = myNote ? D().querySelector('[data-note-id="' + myNote.id + '"]') : null;
    res.rowFound = !!row;
    res.rowText = row ? (row.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80) : '';
    res.rowQuoteOk = !!row && (row.textContent || '')
      .indexOf(String(res.savedText || '').slice(0, 12)) >= 0;
    /* 「一处高亮 + 它的笔记」必须只算**一条**：阅读器 payload 的 notes 少带 `highlight_id` 时，
       侧栏归并挂不上，同一段文字会被渲染两遍 —— 一条「待写」高亮 + 一条「笔记」
       （用户报过"产生了两条莫名的笔记"）。这里按这段文字数两类行。 */
    const sameText = String(res.savedText || '').slice(0, 10);
    const feedRows = [...D().querySelectorAll('[data-item-kind]')]
      .filter((el) => (el.textContent || '').indexOf(sameText) >= 0);
    res.feedNoteRows = feedRows.filter((el) => el.getAttribute('data-item-kind') === 'note').length;
    res.feedBareRows = feedRows.filter((el) => el.getAttribute('data-item-kind') === 'highlight').length;
    /* 高亮叠加层**只画底色、不画文字**（用户报"pdf 添加高亮的叠加层显示文字的问题"）：
       文字层那一面的字形由画布提供，`mark` 及它里面任何元素只要算出可见颜色就是重影。 */
    const pdfMarks = [...D().querySelectorAll('.pdf-text-layer mark.hl')];
    const invisible = (c) => !c || c === 'transparent' || /rgba?\\([^)]*,\\s*0\\)$/.test(c);
    res.pdfMarkRows = pdfMarks.length;
    res.pdfMarkOpaque = pdfMarks.filter((m) => {
      const cols = [w.getComputedStyle(m).color,
                    ...[...m.querySelectorAll('*')].map((el) => w.getComputedStyle(el).color)];
      return cols.some((c) => !invisible(c));
    }).length;
    res.pdfMarkColor = pdfMarks.length ? w.getComputedStyle(pdfMarks[0]).color : '';
    res.pdfMarkPad = pdfMarks.length ? w.getComputedStyle(pdfMarks[0]).paddingTop : '';
    const sc = D().querySelector('.reader-content');
    if (sc) sc.scrollTop = sc.scrollHeight;
    await sleep(400); spin(BURN); await sleep(200);
    res.scrolledAway = sc ? Math.round(sc.scrollTop) : null;
    const loc = row ? row.querySelector('[title="定位"]') : null;
    res.hasLocate = !!loc;
    if (loc) {
      /* `focusRow` 用的是 `scrollTo({ behavior: 'smooth' })`，而**补间动画由合成器推进：
         无头 + 虚拟时间下不一定走**（早前"点了按钮纹丝不动"就是栽在这上面）。
         所以"滚没滚"不能只看 `scrollTop`，同时把**它请求滚到哪**记下来 ——
         那才是应用真正的意图，慢/不动是浏览器那一侧的事。 */
      const proto = w.Element.prototype;
      const origScrollTo = proto.scrollTo;
      let asked = null;
      proto.scrollTo = function (...args) {
        const o = args[0];
        asked = (o && typeof o === 'object') ? o.top : args[1];
        return origScrollTo.apply(this, args);
      };
      try {
        loc.click();
        // 平滑滚动要几帧，轮询到落位为止（无头 + 虚拟时间下别只 sleep 一下就读数）
        await until(D(), () => !sc || sc.scrollTop < 100, 20);
        spin(BURN);
        await sleep(200);
        /* 再点一次兜底：笔记浮层 / 侧栏正好在这一拍重渲染时，第一次点到的可能是**已经脱离文档**的
           那个按钮（`row` 是先抓下来的），点了等于没点 —— 重抓一次 DOM 再点。 */
        if (sc && sc.scrollTop >= 100 && myNote) {
          const again = D().querySelector('[data-note-id="' + myNote.id + '"] [title="定位"]');
          if (again) {
            again.click();
            await until(D(), () => sc.scrollTop < 100, 20);
            spin(BURN);
            await sleep(200);
          }
        }
      } finally {
        proto.scrollTo = origScrollTo;
      }
      res.scrollAsked = asked;
    }
    res.locatedTop = sc ? Math.round(sc.scrollTop) : null;

    /* ---- 笔记页签的**就地编辑**（缩略显示已在 eb6801b4 移除，那一组断言随之删掉） ---- */
    /* 侧栏那条现在给的是「打开浮层」（data-item-popover）—— 就地编辑那一套（data-item-edit /
       data-pane-note-edit）已经没有入口了：改正文统一走浮层（查看 → 编辑 → 保存）。 */
    const paneRow = () => (myNote ? D().querySelector('[data-note-id="' + myNote.id + '"]') : null);
    const paneOpen = paneRow() ? paneRow().querySelector('[data-item-popover]') : null;
    res.paneEditBtn = !!paneOpen;
    if (paneOpen) {
      paneOpen.click();
      await until(D(), (x) => x.querySelector('[data-note-popover]'), 20);
      const toEdit = D().querySelector('button[data-note-edit]');
      if (toEdit) { toEdit.click(); await sleep(250); spin(BURN); }
      await until(D(), (x) => x.querySelector('textarea[data-note-edit]'), 20);
      const ta2 = D().querySelector('textarea[data-note-edit]');
      res.paneEditor = !!ta2;
      if (ta2) {
        ta2.value = '自检：侧栏里改过的笔记';
        ta2.dispatchEvent(new w.Event('input', { bubbles: true }));
        await sleep(200);
        const sv = D().querySelector('[data-note-save]');
        res.paneSaveBtn = !!sv;
        if (sv) sv.click();
        await sleep(900); spin(BURN); await sleep(300);
      }
    }
    const ndataPane = await jget(api + '/notes?paper_id=__PAPER__');
    const paneNote = (((ndataPane.data && ndataPane.data.items) || [])
      .find((n) => n.highlight_id === (mine[0] && mine[0].id)));
    res.paneEdited = paneNote ? String(paneNote.content_md || '') : '';
    /* 存进去了就收起编辑器（保存失败才留着，免得用户敲的字丢了） */
    res.paneEditorClosed = !D().querySelector('textarea[data-note-edit]');

    /* ---- 第五步：笔记浮层（悬停显示 / 移开消失 / 锚定 / 查看↔编辑）---- */
    const card = () => D().querySelector('[data-float-card]');
    const hlMark = () => D().querySelector('[data-pdf-page="1"] .pdf-text-layer mark.hl');
    res.oldModalGone = ![...D().querySelectorAll('*')]
      .some((e) => (e.textContent || '').trim() === '新建笔记'
        && e.className && String(e.className).indexOf('modal') >= 0);
    /* 先把上一步（新建笔记）留下的浮层关掉：它是**锚定**打开且保存后仍开着，
       不关的话下面的"悬停→移开消失"验的就不是悬停那条路径。 */
    /* 编辑态**没有 ✕**（hide-close），所以先保存再关；否则下面"悬停 → 移开消失"验的是残留那张。 */
    if (D().querySelector('textarea[data-note-edit]')) {
      const sv0 = D().querySelector('[data-note-save]');
      if (sv0) { sv0.click(); await sleep(800); spin(BURN); }
    }
    const closeBtn = D().querySelector('[data-float-close]');
    if (closeBtn) { closeBtn.click(); await sleep(300); spin(BURN); }
    res.popClosedBefore = !card();
    const m1 = hlMark();
    if (m1) {
      /* 高亮上不该再有浏览器原生提示（用户口径 2026-10-08："移除浏览器悬浮 toast 内容"） */
      res.markTitle = m1.getAttribute('title');
      m1.dispatchEvent(new w.MouseEvent('mouseover', { bubbles: true }));
      await until(D(), () => !!card(), 20);
      res.popOpen = !!card();
      res.popView = !!D().querySelector('[data-note-view]');
      res.popText = card() ? (card().textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60) : '';
      res.popHint = card() ? ((card().textContent || '').indexOf('已锚定') >= 0 ? 'anchored' : 'auto') : '';
      /* 移开（未锚定）→ 自动消失 */
      m1.dispatchEvent(new w.MouseEvent('mouseout', { bubbles: true, relatedTarget: D().body }));
      await sleep(700); spin(BURN); await sleep(300);
      res.popAutoClosed = !card();
      res.popHoverNow = (() => {
        const b = card();
        return b ? b.matches(':hover') : null;
      })();
      /* 点这处高亮 → 菜单「打开笔记」→ 浮层锚定打开，移开也不关。
         用**重新划词**这条路进菜单（点已有高亮进菜单依赖真实指针状态，探针里不稳），
         并补一次 `mousedown`（真拖拽有按下那一下；划词的"按列夹取"要看它），失败重试两轮。 */
      let openBtn = null;
      for (let attempt = 0; attempt < 3 && !openBtn; attempt += 1) {
        const p3 = pickPair();
        if (!p3[0] || !p3[1]) break;
        const r3 = D().createRange();
        r3.setStart(p3[0], 0);
        r3.setEnd(p3[1], Math.min(12, (p3[1].nodeValue || '').length));
        const s3 = w.getSelection();
        s3.removeAllRanges();
        s3.addRange(r3);
        const pageEl3 = D().querySelector('[data-pdf-page="1"]');
        pageEl3.dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true, clientX: 300, clientY: 300, buttons: 1 }));
        pageEl3.dispatchEvent(new w.MouseEvent('mouseup', { bubbles: true, clientX: 300, clientY: 300 }));
        await until(D(), (x) => x.querySelector('[data-selection-menu]'), 12);
        const m3 = D().querySelector('[data-selection-menu]');
        res.menuText3 = m3 ? (m3.textContent || '').replace(/\\s+/g, ' ').trim() : '';
        openBtn = m3 ? [...m3.querySelectorAll('button')]
          .find((b) => ((b.getAttribute('aria-label') || '') + (b.getAttribute('title') || '')).indexOf('打开笔记') >= 0)
          : null;
        if (!openBtn) await sleep(300);
      }
      res.openNoteBtn = openBtn
        ? (openBtn.getAttribute('aria-label') || openBtn.getAttribute('title') || '').trim() : '';
      if (openBtn) {
        openBtn.click();
        await until(D(), () => !!card(), 20);
        res.anchoredOpen = !!card();
        const m2 = hlMark();
        if (m2) m2.dispatchEvent(new w.MouseEvent('mouseout', { bubbles: true, relatedTarget: D().body }));
        await sleep(700); spin(BURN); await sleep(200);
        res.anchorKept = !!card();
        /* 查看 → 编辑 → 保存 */
        /* 查看态的「编辑」是 button[data-note-edit]（与编辑态那枚 textarea **同名**，
           所以两处都必须带标签名，不能只按属性名找）。 */
        const editBtn = D().querySelector('button[data-note-edit]');
        res.hasEditBtn = !!editBtn;
        if (editBtn) {
          editBtn.click();
          await until(D(), (x) => x.querySelector('textarea[data-note-edit]'), 20);
          const ta = D().querySelector('textarea[data-note-edit]');
          res.hasEditArea = !!ta;
          if (ta) {
            ta.value = '自检改过的笔记正文';
            ta.dispatchEvent(new w.Event('input', { bubbles: true }));
            await sleep(200);
            const save = D().querySelector('[data-note-save]');
            res.hasSaveBtn = !!save;
            if (save) save.click();
            await sleep(900); spin(BURN); await sleep(300);
          }
        }
      }
    }
    const ndata2 = await jget(api + '/notes?paper_id=__PAPER__');
    const edited = (((ndata2.data && ndata2.data.items) || [])
      .find((n) => n.highlight_id === (mine[0] && mine[0].id)));
    res.editedText = edited ? String(edited.content_md || '') : '';

    /* ---- 第五步·下：AI 解释浮层（**要真调一次模型**，只在 --model 下跑）---- */
    if (MODEL) {
      const p4 = pickPair();
      if (p4[0] && p4[1]) {
        const r4 = D().createRange();
        r4.setStart(p4[0], 0);
        r4.setEnd(p4[1], Math.min(12, (p4[1].nodeValue || '').length));
        const s4 = w.getSelection();
        s4.removeAllRanges();
        s4.addRange(r4);
        const pg4 = D().querySelector('[data-pdf-page="1"]');
        pg4.dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true, clientX: 300, clientY: 300, buttons: 1 }));
        pg4.dispatchEvent(new w.MouseEvent('mouseup', { bubbles: true, clientX: 300, clientY: 300 }));
        await until(D(), (x) => x.querySelector('[data-selection-menu]'), 20);
        const aiBtn = [...D().querySelectorAll('[data-selection-menu] button')]
          .find((b) => ((b.getAttribute('aria-label') || '') + (b.getAttribute('title') || '')).indexOf('AI 解释') >= 0);
        res.aiBtn = !!aiBtn;
        if (aiBtn) {
          aiBtn.click();
          /* 出过程期间：底部输入框与按钮**不该在**（用户口径第 3 条）——
             趁它还在跑先量一眼，再等正文出来。 */
          await sleep(600); spin(BURN);
          res.aiBusyHidden = !D().querySelector('.ex-ask')
            && !D().querySelector('[data-ex-color]');
          await until(D(), (x) => x.querySelector('[data-ex-answer]'), 400);
          const cardEl = D().querySelector('[data-explain-card]');
          res.aiCard = !!cardEl;
          res.aiAnswerLen = (D().querySelector('[data-ex-answer]')?.textContent || '').trim().length;
          /* 正文里**不该带过程碎句**（用户口径第 2 条）：后端 `done.answer` 是所有正文段的拼接，
             浮层只显示"最后一次工具调用之后"的那段。过程碎句会出现在轨迹里。 */
          const bodyText = (D().querySelector('[data-ex-answer]')?.textContent || '');
          res.aiBodyHead = bodyText.replace(/\\s+/g, ' ').trim().slice(0, 40);
          res.aiBodyLean = !/I'?ll read the selected block|Let me check|让我看看|我先读/i.test(bodyText);
          res.aiCentered = cardEl ? (() => {
            const r = cardEl.getBoundingClientRect();
            return Math.abs((r.left + r.width / 2) - w.innerWidth / 2) < 40
              && Math.abs((r.top + r.height / 2) - w.innerHeight / 2) < 40;
          })() : null;
          res.aiProcCollapsed = !D().querySelector('.ex-proc-body');
          const head = D().querySelector('[data-ex-proc]');
          res.aiProcSummary = head ? (head.textContent || '').replace(/\\s+/g, ' ').trim() : '';
          if (head) { head.click(); await sleep(400); spin(BURN); }
          res.aiProcSteps = D().querySelectorAll('.ex-proc-body .ex-step').length;
          /* 底部的色块（用户口径第 1 条）：点一个颜色 = 标出这段 + 把解释存成它的笔记 */
          res.aiNoOldButtons = !D().querySelector('[data-ex-save-note]')
            && !D().querySelector('.ex-history')
            && ![...D().querySelectorAll('[data-explain-card] button')]
              .some((b) => (b.textContent || '').indexOf('存为批注') >= 0
                || (b.textContent || '').indexOf('历史') >= 0);
          const swatches = [...D().querySelectorAll('[data-ex-color]')];
          res.aiSwatches = swatches.length;
          /* 第一颗色块（配色是数据驱动的，别再写死某个色名） */
          const anySwatch = D().querySelector('[data-ex-color]');
          res.aiSwatchTitle = anySwatch ? (anySwatch.getAttribute('title') || '') : '';
          if (anySwatch) { anySwatch.click(); await sleep(1800); spin(BURN); await sleep(500); }
        }
      }
      const hdata = await jget(api + '/highlights?paper_id=__PAPER__');
      const hls = (hdata.data && hdata.data.items) || [];
      const aiHl = hls.filter((h) => h.scope === 'pdf').pop() || null;
      res.aiHlColor = aiHl ? String(aiHl.color || '') : '';
      const ndata3 = await jget(api + '/notes?paper_id=__PAPER__');
      const notesNow = (ndata3.data && ndata3.data.items) || [];
      const aiNote = notesNow.filter((n) => n.scope === 'pdf').pop() || null;
      res.aiNoteSaved = aiNote ? 1 : 0;
      res.aiNoteText = aiNote ? String(aiNote.content_md || '') : '';
      res.aiNoteHl = aiNote ? !!aiNote.highlight_id : false;
      res.aiNoteOnSame = !!(aiNote && aiHl && aiNote.highlight_id === aiHl.id);
    }

    /* 划词接管的两处坑（用户 2026-10-08 报："选中到空白区域时会反过来选中本页上面的所有内容" +
       "从上往下复制会被 Edge 识别为拖动字符块搜索文本"）：
       ① 页面**空白**处按下也要接管 —— 以前 `textPointAt` 找不到文字就直接交给浏览器，
          而浏览器这时把锚点放到**文字层容器**上，选区按"第几个子节点"整段铺开；
       ② 文字层里不许起**原生拖放** —— 我们自己在移动中放选区，按下点必然落在选区里面，
          接着拖就被当成"搬走这段文字"，Edge 上就是拖放搜索，而且 `pointermove` 一断接管全废。
       这里用**合成 pointer/mouse 事件**走一遍真几何（命中点由 `caretRangeFromPoint` /
       `elementFromPoint` 现算，与真鼠标是同一条代码路径），只量"选区落在哪、多长"与"默认动作有没有被压掉"。
       真鼠标那一侧的数字见 data/logs 下的一次性 CDP 脚本（改前：空白按下往上一拖 = 2393 字整页）。 */
    {
      /* 先把上一步留下的浮层关掉：查看态的笔记浮层会**盖在文字层上面**，量"空白处按下"时命中的
         就成了卡片（实测 blankHit0=DIV.np-view、blankHit1=DIV.fc-grab，选区自然是 0 字）。 */
      for (let i = 0; i < 5 && D().querySelector('[data-float-card]'); i += 1) {
        const closeX = D().querySelector('[data-float-close]');
        if (!closeX) break;
        closeX.click(); await sleep(250); spin(BURN);
      }
      res.blankCardsLeft = D().querySelectorAll('[data-float-card]').length;
      /* 页 1 的文字层是**懒铺**的（滚出渲染范围就没有 span）—— 前面那几步会把阅读位置挪走，
         先把它滚回**视口里**再量。⚠️ 判据不能只看"层里有 span"：铺好的 span 会留在 DOM 上，
         页码滚到 13000px 之外它照样在（实测这一条就因为早退量到了纸外，选区自然是 0 字）。 */
      for (let i = 0; i < 12; i += 1) {
        const el = D().querySelector('[data-pdf-page="1"]');
        const r = el ? el.getBoundingClientRect() : null;
        const l = D().querySelector('[data-pdf-page="1"] .pdf-text-layer');
        const inView = !!r && r.top > 40 && r.bottom < w.innerHeight - 40;
        if (inView && l && l.querySelectorAll('span').length >= 2) break;
        el?.scrollIntoView({ block: 'center' });
        await sleep(300); spin(BURN);
      }
      const layer = D().querySelector('[data-pdf-page="1"] .pdf-text-layer');
      const box = layer ? layer.getBoundingClientRect() : null;
      const spans = [...(layer ? layer.querySelectorAll('span') : [])]
        .map((s) => ({ s, r: s.getBoundingClientRect() }))
        .filter((o) => o.r.width > 4 && o.r.height > 2);
      /* 找一条"右半段是空白"的行（论文里短行很常见）：从它的**右边距**按下才不会压在文字上 */
      const short = box ? spans.find((o) => o.r.right < box.right - 60 && o.r.top > box.top + 100) : null;
      w.getSelection().removeAllRanges();
      res.blankHasSpot = !!short;
      res.blankBox = box ? [Math.round(box.left), Math.round(box.top),
                            Math.round(box.width), Math.round(box.height)] : null;
      res.blankSpans = spans.length;
      res.blankShortRight = short ? Math.round(short.r.right) : null;
      if (layer && box && short) {
        const x = box.right - 6;
        const y0 = short.r.top + short.r.height / 2;
        const y1 = y0 - 60;
        /* 诊断用：这两个点命中了谁（浮层卡片压在上面时 `elementFromPoint` 交回的就不是文字层） */
        const who = (el) => (el ? el.tagName + '.' + String(el.className || '').slice(0, 20) : 'null');
        res.blankHit0 = who(D().elementFromPoint(x, y0));
        res.blankHit1 = who(D().elementFromPoint(x, y1));
        const caret1 = D().caretRangeFromPoint(x, y1);
        res.blankCaret1 = caret1
          ? (caret1.startContainer.nodeType === 3 ? 'text' : who(caret1.startContainer)) : 'null';
        const near = (y) => spans.slice().sort((a, b) =>
          Math.max(a.r.top - y, 0, y - a.r.bottom) - Math.max(b.r.top - y, 0, y - b.r.bottom))[0];
        res.blankNear0 = near(y0) ? [Math.round(near(y0).r.top), Math.round(near(y0).r.bottom)] : null;
        res.blankNear1 = near(y1) ? [Math.round(near(y1).r.top), Math.round(near(y1).r.bottom)] : null;
        const pe = (t, y, buttons) => new w.PointerEvent(t, {
          bubbles: true, cancelable: true, pointerId: 9, pointerType: 'mouse',
          clientX: x, clientY: y, button: 0, buttons,
        });
        layer.dispatchEvent(pe('pointerdown', y0, 1));
        const md = new w.MouseEvent('mousedown', {
          bubbles: true, cancelable: true, clientX: x, clientY: y0, button: 0, buttons: 1,
        });
        layer.dispatchEvent(md);
        res.blankMouseCut = md.defaultPrevented;
        for (let i = 1; i <= 6; i += 1) layer.dispatchEvent(pe('pointermove', y0 + (y1 - y0) * i / 6, 1));
        const sel = w.getSelection();
        const range = sel.rangeCount ? sel.getRangeAt(0) : null;
        const inLayer = (n) => {
          const el = n && (n.nodeType === 3 ? n.parentElement : n);
          return !!(el && el.closest && el.closest('.pdf-text-layer') === layer);
        };
        res.blankAnchorText = !!(sel.anchorNode && sel.anchorNode.nodeType === 3 && inLayer(sel.anchorNode));
        res.blankFocusText = !!(sel.focusNode && sel.focusNode.nodeType === 3 && inLayer(sel.focusNode));
        res.blankSelLen = String(sel.toString()).length;
        res.blankSelPx = range ? Math.round(range.getBoundingClientRect().height) : 0;
        res.blankDragPx = Math.round(y0 - y1);
        layer.dispatchEvent(pe('pointerup', y1, 0));
        /* 兜底那道：文字层里的 `dragstart` 必须被拦掉（主拦在按下那一下，见 ⑤） */
        const span0 = layer.querySelector('span');
        const ds = new w.DragEvent('dragstart', { bubbles: true, cancelable: true });
        if (span0) span0.dispatchEvent(ds);
        res.dragStartCut = ds.defaultPrevented;
        res.userDrag = span0
          ? String(w.getComputedStyle(span0).getPropertyValue('-webkit-user-drag') || '').trim()
          : '';
        w.getSelection().removeAllRanges();
      }
    }

    /* 收尾：删掉自检造出来的那几条（笔记 + 高亮，一条都不留） */
    if (myNote) { await fetch(api + '/notes/' + myNote.id, { method: 'DELETE' }); }
    for (const h of mine) { await fetch(api + '/highlights/' + h.id, { method: 'DELETE' }); }
    const after = await jget(api + '/highlights?paper_id=__PAPER__');
    res.left = (((after.data && after.data.items) || []).filter((h) => h.scope === 'pdf')).length;
    const afterNotes = await jget(api + '/notes?paper_id=__PAPER__');
    res.leftNotes = (((afterNotes.data && afterNotes.data.items) || [])
      .filter((n) => n.scope === 'pdf')).length;
    out(res);
  } catch (err) {
    out({ stage: 'error', error: String((err && err.message) || err) });
  }
}
</script>"""


def check_explain_shell() -> None:
    """AI 解释浮层的**结构**自检（免费、不看浏览器）：它是不是真的换成了浮层。

    为什么要在源码层面也查一遍：这条链路要真调一次模型才能端到端跑（那部分在 `--model` 那条线上），
    现在这里只剩「浮层壳能拖动」是纯源码可验的；浮层自身的交互要走 `--model` 那条线真调一次
    （原先那几条源码断言已于 2026-10 按用户口径删除，见下方注释）。
    """
    # 2026-10 用户要求：以下 4 条（共用浮层 / 锚定跳转不关 / 底部色块 / 过程期隐藏输入框 /
    # answerBody 只取末段）对应的实现已不再随本页演进，长期红灯无信号，故删除。
    src = (ROOT / "frontend" / "src" / "views" / "ReaderView.vue").read_text(encoding="utf-8")

    # 同一份浮层壳（笔记 / AI 解释共用）：**按住标题栏可以拖**
    fc = (ROOT / "frontend" / "src" / "components" / "reader" / "FloatingCard.vue"
          ).read_text(encoding="utf-8")
    drag_handlers = all(k in fc for k in ("onHeadDown", "onHeadMove", "onHeadUp", "data-float-head"))
    drag_kept = "if (!props.open || dragged.value) return;" in fc and "dragged.value = false;" in fc
    record("explain", "浮层可以鼠标拖动（标题栏当把手；拖过之后不再自动弹回）",
           drag_handlers and drag_kept,
           f"拖动处理={drag_handlers} 拖过不弹回={drag_kept}")


def check_pdf_highlight(browser: str, base: str, paper: str, budget_ms: int,
                        model: bool = False) -> None:
    print("\n⑥ PDF 页图高亮 + 笔记浮层（划词 → 存库 → 画在文字层 → 刷新后回到原处 → 自删）")
    url = f"{base}/cylit/reader/{paper}?left=page&nosse=1"
    html = (PROBE_PDFHL.replace("__URL__", url).replace("__TITLE__", "pdfhl")
            .replace("__PAPER__", paper).replace("__MODEL__", "1" if model else "0"))
    # 这条探针要**真渲染整份 PDF**（pdf.js 下载 + 光栅化 + 文字层）再走一遍高亮/笔记/浮层的全套，
    # 按默认 40s 的虚拟时间预算跑会在无头里被墙钟掐掉（实测：dump 回来 0 字节）—— 给宽一点。
    res = run_probe(browser, base, "probe_pdfhl", html, max(budget_ms, 90_000))
    if not res:
        record("pdfhl", "PDF 页图能划出高亮（真文字层 + 真选区 + 真菜单）", False, "探针没产出结果")
        return
    if res.get("stage") == "error":
        record("pdfhl", "PDF 页图能划出高亮（真文字层 + 真选区 + 真菜单）", False,
               str(res.get("error"))[:90])
        return
    # 探针的原始字段全量存一份：报告里只有断言结论，追问题时要看的是**这些**中间量
    (ROOT / "data" / "logs" / "_pdfhl_raw.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    record("pdfhl", "PDF 页图有 pdf.js 真文字层（高亮要画的就在这层上）",
           res.get("hasLayer") is True and (res.get("spans") or 0) >= 2,
           f"第 {res.get('page')} 页，{res.get('spans')} 段文字")
    record("pdfhl", "PDF 划词菜单里有高亮色（第三步之前这里只有解释/复制）",
           res.get("hasColors") is True and res.get("menuExplain") is True,
           f"有解释键={res.get('menuExplain')} 图标键={res.get('menuIconOnly')}")
    record("pdfhl", "胶囊与右键菜单**同款玻璃**（半透明底 + blur + 1px 描边），且层级在菜单之上",
           res.get("menuIconOnly") is True and (res.get("pillGlass") or {}).get("blur") is True
           and float((res.get("pillGlass") or {}).get("alpha") or 1) < 1
           and "solid" in str((res.get("pillGlass") or {}).get("border") or "")
           and int((res.get("pillGlass") or {}).get("z") or 0) > 320,
           f"{res.get('pillGlass')}")
    record("pdfhl", "还没标高亮时菜单里**没有「笔记」**（先插高亮，才能在高亮上加笔记）",
           "笔记" not in (res.get("menuBeforeHl") or []),
           f"菜单项 {res.get('menuBeforeHl')}")
    record("pdfhl", "高亮存进库：scope=pdf + `pdf:p<页号>` + 文字层偏移量",
           res.get("saved") == 1 and isinstance(res.get("savedRange"), list)
           and res["savedRange"][1] > res["savedRange"][0]
           and str(res.get("savedText") or "").strip() != "",
           f"{res.get('saved')} 条，偏移 {res.get('savedRange')}，文字 {str(res.get('savedText'))[:16]!r}")
    record("pdfhl", "高亮画在 PDF 文字层上，且画的就是存下来那段文字",
           (res.get("marked") or 0) >= 1
           and str(res.get("markedText") or "").strip() == str(res.get("savedText") or "").strip(),
           f"{res.get('marked')} 段 mark：{str(res.get('markedText'))[:18]!r}")
    # 刷新后文字层一段都没回来 = 环境/时序问题（pdf.js 重铺失败），不是产品缺陷。
    # 这一段从"刷新"往后全靠文字层：把整段降级成 skip，免得一条因到 9 条红
    # （用户口径 2026-12：核实过内容没有陈旧，是探针自身的时序抖动）。
    blind = int(res.get("afterSpans") or 0) < 2

    def rec_hl(kind: str, path: str, ok: bool, detail: str = "") -> None:
        if blind:
            skip(path, f"刷新后文字层 0 段（时序/环境），跳过：{detail}")
        else:
            record(kind, path, ok, detail)

    rec_hl("pdfhl", "刷新后高亮**回到同一段文字**上（重新定位稳）",
           (res.get("afterReload") or 0) >= 1
           and str(res.get("afterText") or "").strip() == str(res.get("savedText") or "").strip(),
           f"刷新后 {res.get('afterReload')} 段：{str(res.get('afterText'))[:18]!r}")
    # ---- 第四步闭环：高亮 → 笔记 → 侧栏 → 定位回原文 ----
    # 「添加笔记」那枚按钮已经没有了：2026-11-28 之后**标完色直接开出笔记浮层、而且就在写的那一步**
    # （卡片本身就是输入框，所以头部写的是「编辑」）。断言跟着改成"浮层自己开了、且已经是输入态"。
    rec_hl("pdfhl", "选中并高亮之后，笔记浮层**自己就开了、而且直接在写的那一步**（不用再划一次词）",
           res.get("hasAddNoteBtn") is True and "编辑" in str(res.get("afterHlCard") or ""),
           f"高亮后的浮层头部：{str(res.get('afterHlCard'))[:24]!r}")
    kept_dx, kept_dy = res.get("dragKeptDx"), res.get("dragKeptDy")
    rec_hl("pdfhl", "浮层**可以拖**（按住标题栏挪 110×55），拖过之后内容变化也不弹回",
           res.get("hasHead") is True
           and (res.get("dragDx") or 0) >= 100 and (res.get("dragDy") or 0) >= 45
           and kept_dx is not None and kept_dy is not None
           and abs(int(kept_dx)) <= 2 and abs(int(kept_dy)) <= 2,
           f"挪了 ({res.get('dragDx')},{res.get('dragDy')})，换编辑态后又挪 ({kept_dx},{kept_dy})")
    rec_hl("pdfhl", "侧栏页签是**一个「笔记」**（批注/高亮/笔记已合并，旧的「批注·笔记」已撤）",
           "笔记" in str(res.get("notesTab") or "")
           and "批注" not in str(res.get("notesTab") or ""),
           f"页签={res.get('notesTab')!r}")
    # 「待写」这个标签行里已经不渲染了（2026-11-28 那轮把行收成「pN + 原文 + 三个钮」），
    # 所以判据落在**这一行确实存在、而且带的就是那段原文**上。
    rec_hl("pdfhl", "**还没写正文的高亮**也在笔记页签里（高亮本身就是一条笔记）",
           str(res.get("savedText") or "").strip() != ""
           and str(res.get("savedText") or "")[:10] in str(res.get("bareRow") or ""),
           f"条目：{str(res.get('bareRow'))[:50]!r}")
    rec_hl("pdfhl", "划词菜单里能就这处高亮写笔记（第四步的入口）",
           str(res.get("noteBtn") or "").strip() in ("添加笔记", "笔记", "打开笔记"),
           f"菜单项：{res.get('noteBtn')!r}")
    rec_hl("pdfhl", "笔记编辑器能开能存（浮层里那枚输入框 + 图标保存键）",
           res.get("editor") is True and res.get("saveBtn") is True,
           f"编辑器={res.get('editor')} 保存键={res.get('saveBtn')}")
    rec_hl("pdfhl", "笔记**挂在那条高亮上**（highlight_id + 面 + 页码 + 高亮原文）",
           res.get("noteSaved") == 1 and res.get("noteScope") == "pdf"
           and res.get("notePage") == 1
            and str(res.get("noteQuote") or "").strip() == str(res.get("savedText") or "").strip(),
           f"scope={res.get('noteScope')} p{res.get('notePage')} 引用={str(res.get('noteQuote'))[:14]!r}")
    rec_hl("pdfhl", "一处高亮 + 它的笔记 = **一条**（不是「待写 + 笔记」两条）",
           res.get("feedNoteRows") == 1 and res.get("feedBareRows") == 0,
           f"笔记行 {res.get('feedNoteRows')} 条、同文本的待写行 {res.get('feedBareRows')} 条")
    rec_hl("pdfhl", "高亮叠加层**只画底色、不画文字**（PDF 那一面的字形由画布提供）",
           (res.get("pdfMarkRows") or 0) >= 1 and res.get("pdfMarkOpaque") == 0
           and str(res.get("pdfMarkColor") or "").endswith(", 0)")
           and str(res.get("pdfMarkPad") or "") == "0px",
           f"{res.get('pdfMarkRows')} 段 mark · 文字可见的 {res.get('pdfMarkOpaque')} 段 · "
           f"color={res.get('pdfMarkColor')} · padding={res.get('pdfMarkPad')}")
    # 「面」那个标签（SCOPE_LABEL）从 2026-11-28 起在行里不再渲染（ReaderSidePanel 里现在只剩定义、
    # 没有任何引用），行里是「pN + 时间 + 定位 / 浮层 / 更多 + 原文」。断言跟着只看原文 + 页码。
    rec_hl("pdfhl", "侧栏列表显示：高亮原文 + 页码",
           res.get("rowFound") is True and res.get("rowQuoteOk") is True
           and "p1" in str(res.get("rowText") or ""),
           f"{res.get('rowText')}")
    asked = res.get("scrollAsked")
    rec_hl("pdfhl", "点侧栏「定位」滚回那一页（滚回页首）",
           res.get("hasLocate") is True and (res.get("scrolledAway") or 0) > 100
           and ((res.get("locatedTop") or 0) < (res.get("scrolledAway") or 1) / 5
                or (asked is not None and asked <= 120)),
           f"定位键={res.get('hasLocate')}，滚走 {res.get('scrolledAway')} → 定位后 "
           f"{res.get('locatedTop')}（请求滚到 {asked}）")
    # 「缩略显示」这个功能已在 eb6801b4 从前端移除（`[data-notes-thumb]` 全仓不存在），
    # 对应的断言已删 —— 留着它只会长期红，让人以为坏的是别的东西。
    # ---- 划词接管：空白处按下 + 原生拖放（用户 2026-10-08 报的两条） ----
    # 空白处按下：改之前这条会红（锚点根本进不了接管 → 用合成事件时选不出任何东西；
    # 真鼠标那侧更糟，交给浏览器后锚点落到文字层容器上，实测往上一拖 = 2393 字整页）。
    rec_hl("pdfhl", "页面**空白处**按下也能选：锚点落在文字上，且只覆盖拖过的范围",
           res.get("blankAnchorText") is True and res.get("blankFocusText") is True
           and 0 < (res.get("blankSelLen") or 0) < 400,
           f"锚点/焦点都在文字上={res.get('blankAnchorText')}/{res.get('blankFocusText')}，"
           f"拖 {res.get('blankDragPx')}px 选中 {res.get('blankSelLen')} 字"
           f"（高 {res.get('blankSelPx')}px，空白行={res.get('blankHasSpot')}）")
    rec_hl("pdfhl", "文字层里不起**原生拖放**（按住压掉默认动作 + dragstart 兜底 + user-drag:none）",
           res.get("blankMouseCut") is True and res.get("dragStartCut") is True
           and str(res.get("userDrag") or "").startswith("none"),
           f"mousedown 被取消={res.get('blankMouseCut')} dragstart 被拦={res.get('dragStartCut')} "
           f"user-drag={res.get('userDrag')!r}")
    rec_hl("pdfhl", "侧栏那条「打开浮层」→ 改正文 → 保存写回库里（就地编辑已并入浮层）",
           res.get("paneEditBtn") is True and res.get("paneEditor") is True
           and res.get("paneSaveBtn") is True
           and res.get("paneEdited") == "自检：侧栏里改过的笔记"
           and res.get("paneEditorClosed") is True,
           f"库里正文={str(res.get('paneEdited'))[:20]!r} 收起={res.get('paneEditorClosed')}")
    rec_hl("pdfhl", "笔记改用**轻量浮层**（居中大弹窗已撤）",
           res.get("oldModalGone") is True, f"旧弹窗还在={not res.get('oldModalGone')}")
    rec_hl("pdfhl", "悬停高亮显示笔记（查看模式渲染 Markdown）",
           res.get("popOpen") is True and res.get("popView") is True
           and "笔记" in str(res.get("popText") or "")
           and "自检" in str(res.get("popText") or ""),
           f"浮层：{str(res.get('popText'))[:40]}")
    rec_hl("pdfhl", "高亮上没有浏览器原生提示条（悬浮的提示交给浮层/菜单）",
           not str(res.get("markTitle") or ""),
           f"title={res.get('markTitle')!r}")
    rec_hl("pdfhl", "鼠标移开自动消失（默认**不锚定**）", res.get("popAutoClosed") is True,
           f"移开后还在={not res.get('popAutoClosed')}，提示={res.get('popHint')!r}，"
           f"此刻 :hover={res.get('popHoverNow')}")
    rec_hl("pdfhl", "从高亮「打开笔记」→ 锚定打开，移开也不关",
           str(res.get("openNoteBtn") or "").strip() == "打开笔记"
           and res.get("anchoredOpen") is True and res.get("anchorKept") is True,
           f"菜单={str(res.get('menuText3'))[:36]!r}，移开后还在={res.get('anchorKept')}")
    rec_hl("pdfhl", "查看 ⇄ 编辑切换（编辑后保存写回库里）",
           res.get("hasEditBtn") is True and res.get("hasEditArea") is True
           and res.get("hasSaveBtn") is True
           and res.get("editedText") == "自检改过的笔记正文",
           f"库里正文={str(res.get('editedText'))[:18]!r}")
    rec_hl("pdfhl", "自检不留痕（造出来的高亮与笔记都已删）",
           res.get("left") == 0 and res.get("leftNotes") == 0,
           f"残留高亮 {res.get('left')} 条、笔记 {res.get('leftNotes')} 条")
    # ---- 第五步·下：AI 解释浮层（**要模型**；默认只做源码层结构检查） ----
    if not model:
        skip("AI 解释浮层（真调模型那一半）", "默认不跑（加 --model 才调模型）")
    elif not res.get("aiBtn"):
        record("explain", "AI 解释浮层：划词菜单里能打开它", False, "菜单里没有 AI 解释")
    else:
        record("explain", "AI 解释是**浮层**（不是居中大弹窗），且不遮整页",
               res.get("aiCard") is True and res.get("aiCentered") is False,
               f"浮层={res.get('aiCard')} 居中={res.get('aiCentered')}，回答 {res.get('aiAnswerLen')} 字")
        record("explain", "出过程期间藏起底部输入框与按钮（没有本轮正文时不给操作）",
               res.get("aiBusyHidden") is True, f"藏起了={res.get('aiBusyHidden')}")
        record("explain", "浮层正文**不带过程碎句**（只显示最后一次工具之后的那段）",
               res.get("aiBodyLean") is True,
               f"开头={str(res.get('aiBodyHead'))[:34]!r}")
        record("explain", "过程默认**折叠**（只展示最终回答），点开才铺开",
               res.get("aiProcCollapsed") is True and (res.get("aiProcSteps") or 0) >= 1,
               f"折叠时步骤={0 if res.get('aiProcCollapsed') else '?'}，"
               f"展开后 {res.get('aiProcSteps')} 步，摘要={str(res.get('aiProcSummary'))[:28]!r}")
        record("explain", "底部那两枚旧按钮与「历史」都已撤（色块取代它们）",
               res.get("aiNoOldButtons") is True and (res.get("aiSwatches") or 0) == 4,
               f"旧按钮/历史还在={not res.get('aiNoOldButtons')}，色块 {res.get('aiSwatches')} 枚")
        record("explain", "点色块 = **标出这段 + 把解释存成它的笔记**（同一处高亮）",
               str(res.get("aiHlColor") or "").startswith("#") and res.get("aiNoteSaved") == 1
               and res.get("aiNoteHl") is True and res.get("aiNoteOnSame") is True
               and len(str(res.get("aiNoteText") or "")) > 20,
               f"高亮颜色={res.get('aiHlColor')!r}，笔记={str(res.get('aiNoteText'))[:20]!r} "
               f"挂同一处={res.get('aiNoteOnSame')}")


# --------------------------------------------------------------------------
# 探针 ④：过程轨迹（思考 / 正文 / 工具**夹杂**）—— 造一条临时会话，验完删掉
#
# 这一节为什么要造数据：夹杂渲染只有"一轮里既有思考、又有正文、又有工具"时才看得出来，
# 而那种形状只有真跑一轮模型才有 —— 自检默认不调模型，所以自己往库里写一条
# （形状与 `backend/agent/trace.py` 完全一致），在浏览器里读它渲染成什么样。
# --------------------------------------------------------------------------

#: 造出来的轨迹：stage → 思考 → 工具 → 思考 → 工具 → 正文(半句) → 工具 → 正文(回答)
RUN_STEPS = [
    {"kind": "stage", "round": 0, "text": "正在阅读并组织回答…"},
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
      /* 对话页跳转标号的**样式**（与 ① 那条正文标号探针量同一组值 → 两处必须是同一套） */
      const chip0 = root().querySelector('.md.body .cite-inline');
      res.citeStyle = chip0 ? (() => {
        const cs = w.getComputedStyle(chip0);
        return {
          decoration: cs.textDecorationLine || cs.textDecoration,
          bg: cs.backgroundColor, radius: cs.borderRadius,
          size: cs.fontSize, weight: cs.fontWeight,
        };
      })() : null;
      res.toolGroups = body ? body.querySelectorAll('.tool-group').length : 0;
      res.toolStates = body
        ? [...body.querySelectorAll('.tool-group')].map((g) => !!g.querySelector('.tool-lines'))
        : [];
      /* 最后一段默认展开（用户口径 2026-10-25）：它是"刚在做什么"，其余默认折叠 */
      res.lastItemOpen = (() => {
        if (!body) return null;
        const items = [...body.children];
        const last = items[items.length - 1];
        if (!last) return null;
        return !!last.querySelector('.step-text') || !!last.querySelector('.tool-lines');
      })();
      const toolHeads = body ? [...body.querySelectorAll('.tool-head')] : [];
      /* 每一组都点开再收行：参数摘要分散在各组里，只看第一组会漏（自检探针自己用） */
      for (const h of toolHeads) {
        const group = h.parentElement;
        if (group && !group.querySelector('.tool-lines')) { h.click(); await sleep(180); }
      }
      res.toolLines = [...(body ? body.querySelectorAll('.tool-lines .run-step') : [])]
        .map((el) => (el.textContent || '').replace(/\\s+/g, ' ').trim());
      const think = body ? body.querySelector('.run-step.think') : null;
      if (think) {
        /* 默认折叠（只留一行摘要），点一下才出正文；正文**从最左起排**、用正文字体 */
        res.thinkCollapsed = !think.classList.contains('open')
          && !!think.querySelector('.think-sum') && !think.querySelector('.step-text');
        res.thinkSum = (think.querySelector('.think-sum')?.textContent || '').trim().slice(0, 24);
        /* ⚠️ `AppIcon` 的根就是 `<svg class="icon">`：传进去的 class 会**并到这一个元素上**，
            所以选 `svg.step-caret`，不要写 `.step-caret svg`（那是"再往里找一层"，找不到）。 */
        const caret = think.querySelector('svg.step-caret');
        /* 箭头现在**不换字形**，靠 `transform: rotate(-90deg)`（`.caret.shut`）转过去 ——
           量的是这个类，不是 svg 的内容（内容永远是 chevron-down）。 */
        res.caretBefore = caret ? String(caret.getAttribute('class') || '') : '';
        think.click();
        await sleep(220);
        res.thinkOpen = think.classList.contains('open');
        const text = think.querySelector('.step-text');
        const lead = think.querySelector('.think-lead');
        res.thinkText = (text?.textContent || '').length;
        res.caretAfter = caret ? String(caret.getAttribute('class') || '') : '';
        /* 正文在标签行的**下一行**、且左边缘与标签行齐平（不缩进到「思考」下面）。
           用 offset* 而不是 getBoundingClientRect：进场过渡（fold-enter-from 的 translateY）
           在无头虚拟时间下停在中途，rect 会带上那 -3px，量不准"排在哪"。 */
        if (lead && text) {
          res.textAtLeft = Math.abs(lead.offsetLeft - text.offsetLeft) < 1
            && text.offsetTop >= lead.offsetTop + lead.offsetHeight - 1;
          res.thinkFont = getComputedStyle(text).fontFamily;
        }
        think.click();            // 收回折叠态：后面的"收起整条轨迹"断言不依赖它
        await sleep(120);
      }
      /* 再收起一次：步骤藏起来，回答（在轨迹外）必须还在。
         收起现在是一段淡出（Transition name=fold，~160ms），节点要等 leave 跑完才从 DOM 移除 ——
         所以这里**轮询到它真的消失**并记下耗时（这个数就是收起动画的真实时长），
         而不是拍一个固定等待：固定等待只能证明"等得够久"，量不出动画本身。 */
      const t0 = performance.now();
      first().querySelector('.run-head').click();
      let waited = 0;
      while (waited < 1500 && first().querySelector('.run-body')) {
        /* 收起是 JS 钩子驱动的 Web Animations；无头 `--virtual-time-budget` 下合成帧不推进，
           动画不会自己结束。这里把它**推到最后**（finish 会触发 onfinish → Vue 的 done → 移除），
           这样量到的是"接线是否通"，不是"虚拟时钟跑了多久"。 */
        const animating = first().querySelector('.run-body');
        if (animating && animating.getAnimations().length) {
          animating.getAnimations().forEach((a) => a.finish());
        }
        await sleep(50);
        waited = performance.now() - t0;
      }
      res.collapseMs = Math.round(waited);
      const diagBody = first().querySelector('.run-body');
      /* 收起走的是一段 JS 高度动画（`:css="false"`）。无头 `--virtual-time-budget` 下 rAF 被冻住，
         leave 动画根本不会被创建，节点也就一直挂着 —— 所以这里判**已提交的状态**：
         `.run` 上的 `data-open` 没了 = 已经收起（真浏览器里 200ms 后节点会被移除）。 */
      const runEl = first().querySelector('.run');
      res.runClosed = !!runEl && !runEl.hasAttribute('data-open');
      res.collapseGone = !diagBody;
      res.collapseLeaving = !!diagBody && diagBody.getAnimations().length > 0;
      res.collapseHidden = !diagBody || Number(getComputedStyle(diagBody).opacity) < 1;
      res.collapsedSteps = diagBody ? diagBody.querySelectorAll(':scope > *').length : 0;
      res.collapsedTexts = diagBody ? diagBody.querySelectorAll('.run-text').length : 0;
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


def check_cite_hover() -> None:
    """悬浮"停一会儿才弹"（用户口径 2026-10-08：引用标号 + **高亮**都调）——**源码层**钉住。

    为什么不放进浏览器探针：无头探针跑在 `--virtual-time-budget` 下，定时器会被虚拟时间直接推过去，
    "延时窗口内还没弹"这个中间态量不准（真鼠标那一侧的时间在 `data/logs/cite_cdp.mjs` 里量）。
    这里钉三件事：延时是个常量、两条悬浮入口（标号 / 高亮）都走它、点击那条路**不等**延时。
    """
    src = (ROOT / "frontend" / "src" / "views" / "ReaderView.vue").read_text(encoding="utf-8")
    delay = re.search(r"HOVER_OPEN_DELAY\s*=\s*(\d+)", src)
    cite_path = (src.count("scheduleCiteOpen(citeRefs(chip), chip)") >= 2
                 and "citeHoverTimer = window.setTimeout" in src)
    note_path = ("scheduleNoteOpen(note, hl, mark)" in src
                 and "noteHoverTimer = window.setTimeout" in src)
    # 参考文献浮层往上翻时**要按真实高度贴**（否则标号靠页底时会被顶到页面上半部分）
    cite_measured = "citePopEl.value?.offsetHeight" in src and "await placeCitePop(" in src
    click_now = "cancelCiteOpen();" in src and "void openCitePop(citeRefs(cite), cite);" in src
    record("reader", "悬浮**停一会儿**才弹浮层（标号与高亮同一口径；点击仍然立刻弹）",
           bool(delay) and int(delay.group(1)) >= 250 and cite_path and note_path and click_now,
           f"延时={delay.group(1) if delay else None}ms 标号走延时={cite_path} "
           f"高亮走延时={note_path} 点击不等={click_now}")
    record("reader", "参考文献浮层往上翻时按**真实高度**贴标号（不再被顶到页面上半部分）",
           cite_measured, f"量了真实高度={cite_measured}")
    # 悬浮元素上不留原生 `title`（用户口径 2026-10-08："移除浏览器悬浮 toast 内容"）：
    # 高亮（两条渲染路：正文 DOM / markdown HTML）与浮层标题栏都算 —— 它们都是"悬浮就出卡片"的元素，
    # 再叠一条浏览器提示只是噪音。真机那一侧在 pdfhl 探针里量（`markTitle`）。
    dom = (ROOT / "frontend" / "src" / "utils" / "domMarks.ts").read_text(encoding="utf-8")
    md = (ROOT / "frontend" / "src" / "composables" / "useMarkdown.ts").read_text(encoding="utf-8")
    fc = (ROOT / "frontend" / "src" / "components" / "reader" / "FloatingCard.vue"
          ).read_text(encoding="utf-8")
    marks_clean = (".title =" not in dom and 'title="点击删除高亮"' not in md
                   and 'title="按住这里可以拖动浮层"' not in fc)
    record("reader", "悬浮元素上不留原生 `title` 提示（高亮 / 浮层标题栏）",
           marks_clean,
           f"domMarks 还有 title={'.title =' in dom} markdown 标记还有 title="
           f"{'点击删除高亮' in md} 浮层标题栏还有 title={'按住这里可以拖动浮层' in fc}")


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


def _req_json(url: str, payload: dict | None = None, method: str | None = None) -> dict:
    """小工具：发一个 JSON 请求并取回 JSON（不传 payload 默认 GET）。"""
    method = method or ("POST" if payload is not None else "GET")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


#: 一段"过一遍 `render_like_dom` 也逐字不变"的真实正文：**连续汉字**（≥12 字）。
#: 天然避开 `$…$` 公式、Markdown 记号与空白 —— 后端按文字定位时不会挪位。
_REAL_RUN = re.compile(r"[\u4e00-\u9fff]{12,}")


def _real_quote(base: str, paper: str) -> tuple[str, str]:
    """从论文前几页的**真实译文**里取一小段可定位的引文 → ``(block_id, 引文)``。

    为什么要这样取（2026-12 新校验之后）：写入口加了"选中文字在目标行的正文里定位不到就 400"
    的校验（`annotation_service._located_span`）——它防的正是"建成一条看不见的高亮"。
    探针再用假文本建高亮会被直接拒（`HTTPError 400`），整轮就断在那一步。

    取法：逐页读 `/papers/<id>/translations`，在**译文**里找第一段连续汉字（≥12 字，
    天然避开 `$…$` 公式、Markdown 记号与行首标点），截前 12 个字；还要确认这段字在**同页
    更靠前的行**里没出现过 —— 后端是"按行序取第一处命中"，撞了就会判"不在这一行里"。
    """
    seen: list[str] = []
    for page in (1, 2, 3):
        try:
            data = _req_json(f"{base}/api/v1/papers/{paper}/translations?page={page}")
        except Exception:  # noqa: BLE001 —— 取不到这一页（没译过 / 没这页）就换下一页
            continue
        seen = []                              # 只在同一页里比：定位是按页找行的
        for item in (data.get("data") or {}).get("items") or []:
            for row in item.get("rows") or []:
                zh = str(row.get("translation") or "")
                hit = _REAL_RUN.search(zh)
                quote = hit.group(0)[:12] if hit else ""
                if quote and not any(quote in prev for prev in seen):
                    return str(row.get("id") or ""), quote
                seen.append(zh)                # 更靠前的行里也有这串 → 换这一页的下一行
    raise RuntimeError("论文前几页里找不到可定位的中文正文（是不是还没译过？）")


def check_highlight_update_api(base: str, paper: str) -> None:
    """新增的 `PUT /highlights/{id}`：AI 解释浮层的色块用它给「已经标过的这段」改颜色。

    为什么单独测这一条：色块那条链路（点色 → 标高亮 + 存笔记）要真调一次模型才看得见，
    而它用到的新接口是这一轮唯一的后端改动 —— 先在这里把接口本身钉住（建→改色→删，不留痕）。

    ⚠️ 建的这条高亮必须挂在**真实正文**上：写入口会验"这段字在这一行里找得到"（见
    `_real_quote`），假文本一律 400 —— 探针不该为了省事去撞它。
    """
    label = "PUT /highlights/{id} 能改颜色（浮层色块的那一路）"
    print("\n⓪ 高亮改色接口（浮层色块的「已有高亮就改颜色」那一路）")
    api = f"{base}/api/v1"
    try:
        block_id, quote = _real_quote(base, paper)
    except Exception as exc:  # noqa: BLE001 —— 取不到正文就没法建（如实记一条，不中断整轮）
        record("refs", label, False, f"取不到真实正文：{str(exc)[:80]}")
        return
    before = _req_json(f"{api}/highlights?paper_id={paper}").get("data", {}).get("items") or []
    # 只传 selected_text、**不传偏移量**：后端按文字在当前页的行里自己定位（新校验支持的正路）
    try:
        created = _req_json(f"{api}/highlights", {
            "paper_id": paper, "block_id": block_id, "selected_text": quote,
            "color": "yellow",
        }).get("data") or {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        record("refs", label, False, f"建高亮失败 HTTP {exc.code}：{body[:100]}")
        return
    hid = created.get("id")
    if not hid:
        record("refs", label, False, "建高亮失败")
        return
    print(f"   引文来自正文 {block_id}：{quote!r}（建之前 {len(before)} 条高亮）")
    try:
        updated = _req_json(f"{api}/highlights/{hid}", {"color": "blue"}, method="PUT").get("data") or {}
        same = _req_json(f"{api}/highlights?paper_id={paper}").get("data", {}).get("items") or []
        got = next((h for h in same if h.get("id") == hid), {})
        print(f"   改色前 {created.get('color')!r} → 返回 {updated.get('color')!r}，"
              f"库里 {got.get('color')!r}")
        # 口径 2026-12-10：**库里只存 hex**，色名只是输入别名（annotation_service.HIGHLIGHT_PALETTE）。
        # 所以比「落库的就是返回的那个 hex、而且真的换了一个颜色」，不再拿色名去比。
        record("refs", label,
               str(updated.get("color") or "").startswith("#")
               and got.get("color") == updated.get("color")
               and got.get("color") != created.get("color")
               and got.get("text") == quote,
               f"返回={updated.get('color')!r} 库里={got.get('color')!r} "
               f"改前={created.get('color')!r} 原文={got.get('text')!r}")
    finally:
        req = urllib.request.Request(f"{api}/highlights/{hid}", method="DELETE")
        urllib.request.urlopen(req, timeout=30).read()
    left = _req_json(f"{api}/highlights?paper_id={paper}").get("data", {}).get("items") or []
    print(f"   删完剩 {len(left)} 条高亮（建之前 {len(before)} 条）")
    record("refs", "改色这条自检不留痕",
           not any(h.get("id") == hid for h in left), f"残留 {sum(1 for h in left if h.get('id') == hid)} 条")


# --------------------------------------------------------------------------
# 探针 ⑧：参考文献侧栏的中英对照排版（`?side=references`）
#
# 为什么要造数据：中英对照靠的是**译文**（新提示词把标题译成《中文标题》），而库里现有的译文
# 都是旧提示词产出的（只有原文），所以这里直接插一条"已经译好"的参考文献当样本 ——
# 验完立刻删掉（只活这一个探针的几秒），用户的数据一行都不碰。
# --------------------------------------------------------------------------
#: 临时样本：一条"原文 + 中文标题 + 英文标题 + 作者段"齐全的参考文献
_TEMP_REF = {
    "order_no": 9999,
    "raw": "Josh Achiam, Steven Adler, Sandhini Agarwal. GPT-4 technical report. "
           "arXiv preprint arXiv:2303.08774, 2023.",
    "title": "GPT-4 技术报告",
    "title_en": "GPT-4 technical report",
    "authors": "Josh Achiam, Steven Adler, Sandhini Agarwal.",
}

#: 临时样本：**没有中文标题、只有规则切出来的英文题名**（用户口径 2026-10-07：
#: 作者名后面是 `.` 的 → 侧栏显示文献名称而不是整条原文）
_TEMP_REF_RULE = {
    "order_no": 9998,
    "raw": "Ran Hadad and Uri Erez. Dithered quantization via orthogonal transformations. "
           "IEEE Transactions on Signal Processing, 64(22):5887–5900, 2016.",
    "title": None,
    "title_en": "Dithered quantization via orthogonal transformations",
    "authors": "Ran Hadad and Uri Erez.",
}


#: 临时样本：**既有论文链接又有 GitHub 链接**（用户口径 2026-10-07：并排两枚图标，分别跳论文页与仓库）
_TEMP_REF_CODE = {
    "order_no": 9997,
    "raw": "Yao Fu, Rameswar Panda, Xinyao Niu. Data engineering for scaling language models. "
           "arXiv preprint arXiv:2402.10171, 2024. URL https://github.com/yaofu/data-engineering.",
    "title": None,
    "title_en": "Data engineering for scaling language models",
    "authors": "Yao Fu, Rameswar Panda, Xinyao Niu.",
    "code_url": "https://github.com/yaofu/data-engineering",
}

#: 临时样本：**只有 GitHub 链接**（左边那枚图标就该是 GitHub，不再另给一行）
_TEMP_REF_REPO = {
    "order_no": 9996,
    "raw": "Pgvector search., 2025. https://github.com/pgvector/pgvector/.",
    "title": None, "title_en": None, "authors": None,
    "code_url": "https://github.com/pgvector/pgvector",
}

#: 临时样本：**只有普通网址**（左边那枚图标是"链接"）
_TEMP_REF_WEB = {
    "order_no": 9995,
    "raw": "Elastic search., 2025. https://www.elastic.co/enterprise-search/vector-search.",
    "title": None, "title_en": None, "authors": None,
}

#: 所有临时样本的 order_no（列表里用 ≥9990 一律当作自检样本，别混进"普通条目"的断言）
_TEMP_NOS = (9995, 9996, 9997, 9998, 9999)


def _temp_ref_sql(paper: str, remove_only: bool = False) -> str | None:
    """插/删那几条临时参考文献（直连应用自己的 MySQL 会话；没有 API 能造参考文献）。"""
    import uuid

    import sys
    sys.path.insert(0, str(ROOT))
    from backend.repositories.mysql.models import PaperReference
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        sess.query(PaperReference).filter(PaperReference.paper_id == paper,
                                          PaperReference.order_no.in_(_TEMP_NOS)).delete()
        if remove_only:
            return None
        rows = [(_TEMP_REF, uuid.uuid4().hex[:32]),
                (_TEMP_REF_RULE, uuid.uuid4().hex[:32]),
                (_TEMP_REF_CODE, uuid.uuid4().hex[:32]),
                (_TEMP_REF_REPO, uuid.uuid4().hex[:32]),
                (_TEMP_REF_WEB, uuid.uuid4().hex[:32])]
        for item, rid in rows:
            sess.add(PaperReference(id=rid, paper_id=paper, order_no=item["order_no"],
                                    source="selfcheck", raw=item["raw"], title=item["title"],
                                    title_en=item["title_en"], authors=item["authors"],
                                    code_url=item.get("code_url"),
                                    page_no=item.get("page_no")))
        return rows[0][1]


PROBE_REFS = PROBE_HEAD + """
let started = false;
f.addEventListener('load', run);
setTimeout(() => { if (!started) run(); }, 2500);
async function run() {
  if (started) return;
  started = true;
  const w = f.contentWindow;
  const D = () => f.contentDocument;
  const res = { stage: 'loaded' };
  try {
    await until(D(), (x) => x.querySelector('[data-ref-no]'), 60);
    const row = D().querySelector('[data-ref-no="9999"]');
    res.tempRow = !!row;
    res.tempText = row ? (row.textContent || '').replace(/\\s+/g, ' ').trim() : '';
    res.hasZh = res.tempText.indexOf('GPT-4 技术报告') >= 0;
    res.hasEn = res.tempText.indexOf('GPT-4 technical report') >= 0;
    /* 作者默认收起（只在 tooltip 里）：卡片正文里不该出现作者名 */
    res.authorHidden = res.tempText.indexOf('Sandhini') < 0;
    res.tooltip = row ? (row.getAttribute('title') || '') : '';
    res.tooltipHasRaw = res.tooltip.indexOf('Sandhini') >= 0;
    /* 顶栏（用户口径 2026-10-08 第一条）：`REFERENCES 47 条`（"按论文里的顺序"挪进 tooltip） */
    const bar = D().querySelector('.ref-bar');
    res.refBarText = bar ? (bar.textContent || '').replace(/\\s+/g, ' ').trim() : null;
    res.refBarCap = bar ? !!bar.querySelector('.ref-cap') : false;
    res.refBarTitle = bar ? (bar.querySelector('[title]') || {}).title || '' : '';
    /* 右上角那枚跳转图标已移除（整张卡片就是那个跳转） */
    res.noGoIcon = !D().querySelector('.ref-item svg[data-icon="external"]');
    /* 标号**内联**在中文标题前面：同一行（顶边对齐）且在它左边 */
    const no = row ? row.querySelector('.ref-no') : null;
    const zh = row ? row.querySelector('.ref-zh') : null;
    res.hasInlineNo = !!(no && zh);
    if (no && zh) {
      const a = no.getBoundingClientRect(), b = zh.getBoundingClientRect();
      res.numInline = Math.abs(a.top - b.top) <= 4 && a.right <= b.left + 2;
    }
    /* 没翻译过的那种条目（只有原文）也要标号内联：**标号后面同一行还有正文**
       （第一版是块级钳制把标号挤成独立一行，截图里一眼就看出来了：`[1]` 孤零零一行）。 */
    const plain = [...D().querySelectorAll('.ref-item')]
      .find((el) => Number(el.getAttribute('data-ref-no') || 0) < 9990);
    const pno = plain ? plain.querySelector('.ref-no') : null;
    const phead = plain ? plain.querySelector('.ref-head') : null;
    if (pno && phead) {
      /* 要量的是**标号后面**那段文字（不是标号自己的文本节点）：取 head 里第一段
         既不是标号、也不是最左边那枚跳转图标的内容，再看它的第一个文本节点 */
      const after = [...phead.children].find((el) => !el.classList.contains('ref-no')
        && !el.classList.contains('ref-lead'));
      const tn = after ? D().createTreeWalker(after, w.NodeFilter.SHOW_TEXT).nextNode() : null;
      if (tn) {
        const rng = D().createRange();
        rng.setStart(tn, 0);
        rng.setEnd(tn, Math.min(6, (tn.nodeValue || '').length));
        const nr = pno.getBoundingClientRect(), tr = rng.getBoundingClientRect();
        res.plainInline = Math.abs(nr.top - tr.top) <= 4 && nr.right <= tr.left + 2;
        res.plainDebug = { noTop: Math.round(nr.top), textTop: Math.round(tr.top),
                           noRight: Math.round(nr.right), textLeft: Math.round(tr.left) };
      }
    }
    /* 整张卡片仍能跳发表网站（把 window.open 拦下来看 URL） */
    const opened = [];
    const realOpen = w.open;
    w.open = (u) => { opened.push(String(u)); return null; };
    if (row) { row.click(); await sleep(250); }
    w.open = realOpen;
    res.tempUrl = opened[0] || null;
    /* 只有英文题名的那种条目（没翻译过 / 规则切出来的）：显示**题名**、不显示作者 */
    const rrow = D().querySelector('[data-ref-no="9998"]');
    res.ruleText = rrow ? (rrow.textContent || '').replace(/\\s+/g, ' ').trim() : '';
    res.ruleHasTitle = res.ruleText.indexOf('Dithered quantization via orthogonal transformations') >= 0;
    res.ruleAuthorHidden = !!rrow && res.ruleText.indexOf('Ran Hadad') < 0;
    res.ruleTooltipRaw = !!rrow
      && (rrow.getAttribute('title') || '').indexOf('Ran Hadad and Uri Erez.') >= 0;
    /* 只有英文题名时**不出现**第二条（英文）行：那条只在有中文标题时才画 */
    res.ruleNoSecondLine = !!rrow && !rrow.querySelector('.ref-en');
    /* 卡片上**不该**再有「定位」按钮（用户口径 2026-10-07 第三轮：定位到不了精确那一行，撤掉） */
    res.noLocate = !D().querySelector('[data-ref-locate]');
    /* 两种链接都有的条目：**最左边**是论文图标，**标题下面**才是 GitHub 图标 */
    const leadOf = (no) => {
      const el = D().querySelector(`[data-ref-no="${no}"] [data-ref-lead]`);
      return el ? String(el.getAttribute('data-ref-lead') || '') : '';
    };
    const leadIconOf = (no) => {
      const el = D().querySelector(`[data-ref-no="${no}"] [data-ref-lead] svg`);
      return el ? String(el.getAttribute('data-icon') || '') : '';
    };
    res.leadBoth = leadOf('9997');        // 论文 + GitHub → 左边是论文
    res.leadRepo = leadOf('9996');        // 只有 GitHub → 左边是 GitHub
    res.leadWeb = leadOf('9995');         // 只有普通网址 → 左边是链接
    res.leadFallback = leadOf('9998');    // 什么都抠不出来 → 仍是论文图标（走学术搜索）
    res.leadIcons = { both: leadIconOf('9997'), repo: leadIconOf('9996'),
                      web: leadIconOf('9995') };
    /* 图标**占一整列**、两侧不留大片空白（用户口径 2026-10-08）：
       所有条目必须落在同一列（左边界 + 列宽一致）、图标够大（≥16px）、
       外面只留一点点区（≤3px）、到正文的间距也不大（≤6px）。 */
    const leadBoxes = [...D().querySelectorAll('.ref-item')].map((el) => {
      const lead = el.querySelector('.ref-lead');
      const main = el.querySelector('.ref-main');
      if (!lead || !main) return null;
      const lb = lead.getBoundingClientRect();
      const mb = main.getBoundingClientRect();
      const svg = lead.querySelector('svg');
      const sb = svg ? svg.getBoundingClientRect() : null;
      return { x: Math.round(lb.left), w: Math.round(lb.width),
               icon: sb ? Math.round(sb.width) : 0,
               pad: Math.round((lb.width - (sb ? sb.width : 0)) / 2),
               gap: Math.round(mb.left - lb.right) };
    }).filter(Boolean);
    res.leadRows = leadBoxes.length;
    res.leadColumnKind = new Set(leadBoxes.map((b) => b.x + 'x' + b.w)).size;
    res.leadIconMin = leadBoxes.length ? Math.min(...leadBoxes.map((b) => b.icon)) : 0;
    res.leadPadMax = leadBoxes.length ? Math.max(...leadBoxes.map((b) => b.pad)) : 99;
    res.leadGapMax = leadBoxes.length ? Math.max(...leadBoxes.map((b) => b.gap)) : 99;
    const footIcon = D().querySelector('[data-ref-no="9997"] [data-ref-code] svg');
    res.footGithubIcon = footIcon ? String(footIcon.getAttribute('data-icon') || '') : '';
    res.noFootOnRepoOnly = !D().querySelector('[data-ref-no="9996"] [data-ref-code]');
    const crow = D().querySelector('[data-ref-no="9997"]');
    const ccode = crow ? crow.querySelector('[data-ref-code]') : null;
    const clead = crow ? crow.querySelector('[data-ref-lead]') : null;
    const opened3 = [];
    const realOpen3 = w.open;
    w.open = (u) => { opened3.push(String(u)); return null; };
    if (clead) { clead.click(); await sleep(300); }
    if (ccode) { ccode.click(); await sleep(300); }
    w.open = realOpen3;
    res.codeOpened = opened3;
    res.codePaper = String(opened3[0] || '').startsWith('https://arxiv.org/abs/2402.10171');
    res.codeGithub = String(opened3[1] || '').startsWith('https://github.com/yaofu/data-engineering');
  } catch (err) {
    res.error = String((err && err.message) || err);
  }
  out(res);
  document.title = 'done';
}
</script>"""


def check_references_pane(browser: str, base: str, paper: str, budget_ms: int) -> None:
    """参考文献侧栏：中英对照 + 作者收起 + 标号内联 + 无跳转图标（临时样本，验完删）。"""
    print("\n⑧ 参考文献侧栏（中英对照 / 作者收起 / 标号内联 / 无跳转图标；临时样本验完删）")
    try:
        _temp_ref_sql(paper)
    except Exception as exc:  # noqa: BLE001 —— 连不上库就跳过，别把整轮自检带挂
        skip("参考文献侧栏中英对照", f"造样本失败：{str(exc)[:60]}")
        return
    try:
        url = f"{base}/cylit/reader/{paper}?side=references&nosse=1"
        html = (PROBE_REFS.replace("__URL__", url).replace("__TITLE__", "refs"))
        res = run_probe(browser, base, "probe_refs", html, max(budget_ms, 60_000))
    finally:
        try:
            _temp_ref_sql(paper, remove_only=True)
        except Exception:  # noqa: BLE001
            pass
    if not res or res.get("error"):
        record("refs-pane", "参考文献侧栏能打开（探针拿到结果）", False,
               str((res or {}).get("error") or "探针没产出结果")[:90])
        return
    bar_text = str(res.get("refBarText") or "")
    record("refs-pane", "顶栏是 `REFERENCES N 条`（顺序说明挪进 tooltip）",
           res.get("refBarCap") is True and bar_text.startswith("REFERENCES ")
           and bar_text.endswith("条") and "按论文里的顺序" in str(res.get("refBarTitle") or ""),
           f"顶栏={bar_text!r} tooltip={res.get('refBarTitle')!r}")
    record("refs-pane", "中英对照：中文标题在上、英文标题在下",
           res.get("tempRow") is True and res.get("hasZh") is True and res.get("hasEn") is True,
           f"中文={res.get('hasZh')} 英文={res.get('hasEn')}｜{str(res.get('tempText'))[:44]}")
    record("refs-pane", "作者部分默认收起（只在 tooltip 里给整条原文）",
           res.get("authorHidden") is True and res.get("tooltipHasRaw") is True,
           f"卡片里有作者={not res.get('authorHidden')} tooltip 有原文={res.get('tooltipHasRaw')}")
    record("refs-pane", "标号**内联**在标题前（不再整列显示）+ 右上角跳转图标已移除",
           res.get("hasInlineNo") is True and res.get("numInline") is True
           and res.get("plainInline") is True and res.get("noGoIcon") is True,
           f"中英对照那条内联={res.get('numInline')} 只有原文那条也内联={res.get('plainInline')} "
           f"无跳转图标={res.get('noGoIcon')}｜debug={res.get('plainDebug')}")
    record("refs-pane", "整张卡片仍能跳发表网站（原文里的 arXiv 号）",
           str(res.get("tempUrl") or "").startswith("https://arxiv.org/abs/2303.08774"),
           f"{res.get('tempUrl')}")
    record("refs-pane", "没有中文标题但有英文题名 → 显示题名、作者仍收起（用户口径）",
           res.get("ruleHasTitle") is True and res.get("ruleAuthorHidden") is True
           and res.get("ruleTooltipRaw") is True and res.get("ruleNoSecondLine") is True,
           f"题名={res.get('ruleHasTitle')} 作者收起={res.get('ruleAuthorHidden')} "
           f"tooltip有原文={res.get('ruleTooltipRaw')}｜{str(res.get('ruleText'))[:44]}")
    record("refs-pane", "卡片没有「定位」按钮（撤掉：定位到不了精确那一行）",
           res.get("noLocate") is True, f"页面里还有定位按钮={not res.get('noLocate')}")
    record("refs-pane", "**每条左边**都有跳转图标，造型按链接类型选（论文 / GitHub / 网页链接）",
           res.get("leadBoth") == "paper" and res.get("leadRepo") == "github"
           and res.get("leadWeb") == "link" and res.get("leadFallback") == "paper"
           and res.get("leadIcons") == {"both": "document", "repo": "github", "web": "link"},
           f"论文+代码={res.get('leadBoth')} 只有代码={res.get('leadRepo')} "
           f"只有网址={res.get('leadWeb')} 兜底={res.get('leadFallback')}"
           f"｜图标 {res.get('leadIcons')}")
    record("refs-pane", "图标**占一整列**（左边界与列宽一致）且两侧不留大片空白",
           (res.get("leadRows") or 0) >= 20 and res.get("leadColumnKind") == 1
           and (res.get("leadIconMin") or 0) >= 16 and (res.get("leadPadMax") or 99) <= 3
           and (res.get("leadGapMax") or 99) <= 6,
           f"{res.get('leadRows')} 条 · 列样式 {res.get('leadColumnKind')} 种 · "
           f"图标最小 {res.get('leadIconMin')}px · 点区最大 {res.get('leadPadMax')}px · "
           f"到正文最大 {res.get('leadGapMax')}px")
    record("refs-pane", "论文 + GitHub 都有 → GitHub 图标在**标题下面**，两枚图标分别跳不同页面",
           res.get("footGithubIcon") == "github" and res.get("noFootOnRepoOnly") is True
           and res.get("codePaper") is True and res.get("codeGithub") is True,
           f"标题下 GitHub 图标={res.get('footGithubIcon')} 只有代码那条不重复={res.get('noFootOnRepoOnly')} "
           f"论文={res.get('codePaper')} GitHub={res.get('codeGithub')}｜{res.get('codeOpened')}")


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
        url = f"{base}/cylit/reader/{paper}?side=chat&nosse=1"
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
        want = ["thinking", "tool", "thinking", "tool", "text", "tool"]
        record("run", "思考 / 正文 / 工具**按发生顺序夹杂**（旁白那步不出现）",
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
        record("run", "对话页的跳转标号样式（与正文里的引用标号**同一组断言**）",
               _cite_style_ok(res.get("citeStyle")), f"{res.get('citeStyle')}")
        record("run", "过程里那半句留在轨迹里（夹杂的另一半）",
               res.get("traceTexts") == ["我先说一句。"],
               f"轨迹内正文段 {res.get('traceTexts')}")
        record("run", "工具段：收成一组「工具 · N 次调用」，非最后一段默认折叠，点开才列子行",
               (res.get("toolGroups") or 0) >= 1
               and (res.get("toolStates") or [None])[0] is False
               and any("翻书架" in t and "attention" in t for t in (res.get("toolLines") or []))
               and any("读正文" in t for t in (res.get("toolLines") or [])),
               f"{res.get('toolGroups')} 组，展开状态={res.get('toolStates')}；"
               + " / ".join((res.get("toolLines") or [])[:3]))
        record("run", "历史消息：展开思维链后**内部全部折叠**（不会默认摊开最后一段）",
               res.get("thinkCollapsed") is True and res.get("lastItemOpen") is False
               and all(v is False for v in (res.get("toolStates") or [False]))
               and (res.get("toolGroups") or 0) >= 1,
               f"最后一段展开={res.get('lastItemOpen')}，思考折叠={res.get('thinkCollapsed')}，"
               f"工具组={res.get('toolStates')}")
        record("run", "思考段：默认收成一行摘要、点一下展开全文（箭头跟着翻）",
               res.get("thinkCollapsed") is True and res.get("thinkOpen") is True
               and (res.get("thinkText") or 0) > 0
               and bool(res.get("caretBefore")) and res.get("caretBefore") != res.get("caretAfter"),
               f"摘要 {res.get('thinkSum')!r}，展开 {res.get('thinkText')} 字，"
               f"箭头 {'变了' if res.get('caretBefore') != res.get('caretAfter') else '没变'}")
        record("run", "思考正文**从最左起排**（不缩进到标签下面）且不用等宽字体",
               res.get("textAtLeft") is True
               and "mono" not in str(res.get("thinkFont") or "").lower()
               and "Consolas" not in str(res.get("thinkFont") or ""),
               f"齐平={res.get('textAtLeft')}，字体={res.get('thinkFont')}")
        record("run", "收起过程轨迹：轨迹收起、**回答仍在**",
               (res.get("collapseGone") is True or res.get("runClosed") is True)
               and res.get("outsideBody") == 1,
               f"收起后 {'已移除' if res.get('collapseGone') else ('data-open 已清' if res.get('runClosed') else '仍在展开')}"
               f"（{res.get('collapseMs')}ms），轨迹外回答 {res.get('outsideBody')} 块")
        record("run", "轨迹拼不出正文时直接渲染 content（回答不会被截断）",
               res.get("safeHasRun") is True and res.get("safeTraceTexts") == 0
               and SAFE_ANSWER in (res.get("safeAnswer") or ""),
               f"轨迹内正文 {res.get('safeTraceTexts')} 段，回答 {(res.get('safeAnswer') or '')[:22]!r}")
        # 对话期间（流式那一轮）才自动展开最后一段；历史消息一律全折叠 —— **源码层**钉住：
        # 这个默认值住在 `isItemOpen` 里，浏览器探针造的是历史消息，量不到"正在跑"的那一支。
        src_chat = (ROOT / "frontend" / "src" / "components" / "reader" / "ReaderChat.vue"
                    ).read_text(encoding="utf-8")
        live_default = ("m.message_id === liveId.value && item.last === true" in src_chat)
        reset_on_fold = ("openSteps.value = openSteps.value.filter(k => !k.startsWith(prefix))"
                         in src_chat)
        record("run", "对话期间最后一段**自动展开**（历史消息全折叠）+ 收起整条后内部状态清空",
               live_default and reset_on_fold,
               f"live 默认展开={live_default}，收起清空内部状态={reset_on_fold}")
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
  cites: cases.cites.map((c) => mdToHtml(c)),
}));
"""

#: 引用标号可点（2026-10-07 第三条）：`[2, 4, 10]` → `<button class="cite-inline" data-refs="…">`。
#: 元组：(名字, 源文, HTML 里必须含的片段, HTML 里**不许**含的片段, 纯文本期望（None = 不比）)
#: ⚠️ 纯文本那一列是这条链路的命根子：按钮里写的仍是 `[2, 4, 10]`，
#: "渲染出来的字"一个不多一个不少 —— 高亮的偏移量坐标系（`utils/domMarks`）就建在这上面。
MD_CITES = [
    ("单个标号", "如 [12] 所示", 'data-refs="12"', None, None),
    ("用户那条（多标号）", "见 [2, 4, 10, 12, 20, 25, 33] 的方法",
     'data-refs="2,4,10,12,20,25,33"', None, "见 [2, 4, 10, 12, 20, 25, 33] 的方法"),
    ("挤在一起写", "见 [2,4,10] 的方法", 'data-refs="2,4,10"', None, None),
    # 悬浮的效果由**参考文献浮层**负责，标号上不能再来一条原生 `title`（用户口径 2026-10-08：
    # "移除悬浮显示的文字（浏览器 toast）"）—— 两者会叠在一起。
    ("标号不带原生 title 提示条", "如 [12] 所示", 'aria-label="参考文献 12', 'title=', None),
    ("行内代码里不动", "写法是 `[1]` 这样", None, "cite-inline", "写法是 [1] 这样"),
    ("代码围栏里不动", "```\n[1]\n```", None, "cite-inline", None),
    ("公式里的区间不动", "在区间 $[0, 1]$ 上", None, "cite-inline", None),
    ("真的链接不被抢", "见 [2](https://example.com) 一节",
     'href="https://example.com"', "cite-inline", None),
]

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


def check_trace_motion() -> None:
    """过程轨迹的**动效与自动展开**——源码层钉住（无头探针量不了这些，原因见下）。

    为什么不能在浏览器探针里断：探针跑在 `--virtual-time-budget` 下，合成帧不推进，
    Vue 的 `<Transition>` 在 leave 的 `nextFrame` 上卡住，节点会一直挂在 `fold-leave-from`
    （实测 1500ms 仍在，连 enter 的 `fold-enter-from` 都没摘），于是「收起后节点被移除」这类断言
    量到的是**无头环境的状态**，不是产品行为。真浏览器里的真实耗时是用 CDP 直接量的
    （进场 70ms 时 opacity 0.334 → 280ms 到 1；离场 194ms 后节点移除），那条量法不放进自检。

    能确定性检查的是**接线**：过渡类在、reduced-motion 真的把它们关掉、自动展开的判定写成
    「手动优先 → 否则看流式中最后一步是不是思考」。这三件事错了，动效一定错；对了，动效由浏览器照常演。
    """
    src = (ROOT / "frontend" / "src" / "components" / "reader" / "ReaderChat.vue").read_text(encoding="utf-8")
    has_fold = '<Transition name="fold"' in src
    has_group = '<TransitionGroup name="step">' in src
    record("motion", "轨迹展开/收起走 <Transition name=fold>（高度由 JS 钩子驱动），步骤行走 <TransitionGroup name=step>",
           has_fold and has_group,
           f"Transition={has_fold} TransitionGroup={has_group}")
    classes = [".step-enter-from", ".step-enter-active", ".step-move",
               ".fold-enter-active", ".fold-leave-active"]
    record("motion", "过渡类齐（进场淡入上移 / 让位 / 轨迹展开收起）",
           all(f"{one} {{}} ".strip().split(" ")[0] in src or one in src for one in classes),
           " / ".join(f"{one}={'有' if one in src else '缺'}" for one in classes))
    blocks = src.count("@media (prefers-reduced-motion: reduce)")
    #: 取**最后一段**媒体查询往后 1600 字当它的体内（不写跨嵌套的正则：媒体查询里还有规则块，
    #: 非贪婪的 \n} 会在第一条规则结束处就收尾 —— 第一版正是这么误判的）
    mark = src.rfind("@media (prefers-reduced-motion: reduce)")
    calm_text = src[mark:mark + 1600] if mark >= 0 else ""
    record("motion", "系统关掉动效时这些过渡全停（同一段媒体查询里，不另写一段）",
           blocks == 2 and "transition: none !important" in calm_text
           and ".fold-leave-active" in calm_text and ".step-enter-active" in calm_text,
           f"媒体查询 {blocks} 段，命中 fold/step={'是' if '.fold-leave-active' in calm_text else '否'}")
    live = re.search(r"function liveThinkingAt\((.{0,900}?)\n\}", src, re.S)
    live_text = live.group(1) if live else ""
    record("motion", "最后一段思考**只有流式中**自动展开（历史消息永远收起）",
           "liveId.value" in live_text and "thinking" in live_text
           and "steps.length - 1" in live_text,
           "判定里含 liveId / thinking / 最后一步" if live_text else "找不到 liveThinkingAt")
    open_fn = re.search(r"function isItemOpen\((.{0,900}?)\n\}", src, re.S)
    open_text = open_fn.group(1) if open_fn else ""
    # 2026-12-14 用户口径：「最后一段工具调用跑完就折回去」是 bug —— 现在只要这一轮是 live、
    # 且它是最后一段就展开（不再要求 `itemRunning`）；新的一步进来后不再是最后一段，自然收起。
    record("motion", "手点优先：展开集合 > 收起集合 > 对话期间最后一段自动展开（跑完也留着）",
           open_text.index("openSteps") < open_text.index("closedSteps") < open_text.index("liveId.value")
           and "item.last === true" in open_text and "itemRunning" not in open_text
           if all(k in open_text for k in ("openSteps", "closedSteps", "liveId.value")) else False,
           "顺序 展开→收起→（live 且是最后一段）" if open_text else "找不到 isItemOpen")
    record("motion", "段内开合按**实测高度**过渡（Web Animations，不是 max-height 猜）",
           src.count('<Transition name="fold" :css="false"') >= 3
           and "function foldEnter" in src and "function foldLeave" in src
           and "function fold(" in src
           and "height: '0px'" in src,
           f":css=false 的 fold 共 {src.count('<Transition name=\"fold\" :css=\"false\"')} 处")
    record("motion", "折叠动画：时长跟着高度走 + 高度与透明度不同步（收起时字先淡）",
           "function foldMs(" in src and "const FOLD_IN = 'cubic-bezier(.22, .61, .36, 1)'" in src
           and "const FOLD_OUT = 'cubic-bezier(.4, 0, .2, 1)'" in src
           and "offset: 0.62" in src and "offset: 0.72" in src,
           "foldMs 按距离给时长，收起时 .62 处只剩 .3 不透明")
    record("motion", "折叠动画期间外层对话**每帧贴底**（不然正文先被顶走再弹回来）",
           "function followThroughFold" in src and "followThroughFold(ms + 32)" in src
           and "if (el.dataset.foldAnim === '1') return;" in src,
           "followThroughFold 每帧 followBottom；轨迹容器折叠时暂停钉底")
    record("motion", "折叠箭头是**转过去的**（一枚图标 + transform，不是右/下箭头换字形）",
           ".caret.shut { transform: rotate(-90deg); }" in src
           and 'class="caret" :class="{ shut: !isRunOpen(' in src
           and 'name="chevron-down" :size="12"' in src,
           "caret 旋转 220ms，与段内开合同一节奏")
    record("motion", "思维链**写正文就收起**（过程事件展开 / answer_delta 防抖折叠，手点优先）",
           "function keepRunOpen" in src and "function foldRunSoon" in src
           and "keepRunOpen(ensureLive());" in src
           and "foldRunSoon(live);" in src
           and "if (runTouched.has(m.message_id)) return;" in src
           and "}, 320);" in src,
           "thinking_delta/tool/answer_reset → keepRunOpen；answer_delta → foldRunSoon(320ms)")
    record("motion", "自动收起不被旁白卡住（阶段提示不算过程事件；正文一直来也不重置计时）",
           "lastProcessAt = performance.now();" in src
           and "if (pendingFold === m.message_id) return;" in src
           and "performance.now() - lastProcessAt < 400" in src
           and "// 旁白：不动轨迹的开合" in src,
           "pendingFold 只认第一次 + lastProcessAt 400ms 窗口 + stage 不重开")
    record("motion", "旁白**不进思维链**（`stage` 步骤数据还在，只是不渲染成一行）",
           "if (s.kind === 'stage') return;" in src
           and "function hasTrace(" in src
           and ':aria-expanded="hasTrace(m) ? isRunOpen(m.message_id) : undefined"' in src
           and 'v-if="isRunOpen(m.message_id) && hasTrace(m)"' in src
           and ".caret-gap {" in src,
           "traceRows 跳过 stage + 只有旁白的一轮不给箭头（留 12px 占位）")
    title_fn = re.search(r"function runTitle\((.{0,1200}?)\n\}", src, re.S)
    title_text = title_fn.group(1) if title_fn else ""
    record("motion", "旁白整句删掉（抬头只说「正在思考…」，不再报「正在准备…」）",
           "last?.kind === 'stage'" not in title_text and "'正在思考…'" in title_text
           and "appendLiveStep('stage', mode ===" not in src,
           "runTitle 里没有 stage 分支；ask() 起手不再塞旁白步骤（SSE 的 stage 帧仍可收）")
    record("motion", "一步都没有的那一轮也照样有抬头（起手旁白删掉后的空窗）",
           "function showRun(" in src and 'v-if="showRun(m)"' in src
           and "m.role === 'assistant' && m.message_id === liveId.value" in src,
           "showRun：有步骤 || 还在跑的那一轮")
    record("motion", "正文按块渲染：已封口的块不重建，只有正在长的最后一块跟着增量重画",
           "function splitAnswerParts" in src and "function answerParts" in src
           and ".md-part { display: contents; }" in src
           and 'class="md body md-part"' in src
           and ':scope > .md-part' in src
           and ".md-part-tail :deep(> *:last-child)::after" in src,
           "splitAnswerParts + answerParts + display:contents + 光标只挂最后一块")
    record("motion", "正文载入动画：新块 / 末尾新长的一截淡入（照 DSH 的 reveal-piece）",
           "function revealFreshBlocks" in src and "function wrapTail" in src
           and "@keyframes reveal-piece" in src
           and "animation: reveal-piece .16s cubic-bezier(.16, .84, .44, 1)" in src
           and "revealFreshBlocks()" in src,
           "revealFreshBlocks + wrapTail + reveal-piece .16s 柔缓 ease-out")
    record("motion", "思考写完就**立刻**折叠（不等下一个步骤出现，`thinking`(done) → `endedSteps`）",
           "const endedSteps = ref<string[]>([])" in src
           and "function markThinkingDone" in src
           and "markThinkingDone(ensureLive())" in src
           and 'name === \'thinking\'' in src
           and "endedSteps.value.includes(stepKey(id, i))" in src,
           "thinking(done) → markThinkingDone → liveThinkingAt 里排除已写完的那段")
    record("motion", "思维链有自己的限高滚动容器（44vh / 420px，边缘渐隐，滚到底继续带动外层）",
           "max-height: min(44vh, 420px)" in src and "overscroll-behavior: contain" not in src
           and "function setRunBody" in src and ".run-body.can-up" in src,
           "限高 + can-up/can-down 渐隐 + 允许滚动链")

def _bundle_tmp_root() -> Path:
    """esbuild / node 那两条真代码检查的**临时目录根**（见下面各调用点的说明）。"""
    root = ROOT / "data" / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def check_markdown_shapes() -> None:
    """把形状表喂给真实渲染器（esbuild bundle `useMarkdown.ts` → node）。"""
    print("\n⓪ Markdown 加粗：`**` 紧贴标点（不用浏览器 / 不打模型）")
    node = shutil.which("node")
    frontend = ROOT / "frontend"
    if not node or not (frontend / "node_modules" / "esbuild").exists():
        skip("markdown 加粗形状表", "没有 node / esbuild（先在 frontend 里 npm i）")
        return
    src = [c[1] for c in MD_SHAPES]
    #: 临时目录放**仓库内**（`data/logs`），不放系统 %TEMP%：实测在系统临时目录里 esbuild
    #: 偶发写不出 `md.mjs`（`Failed to write to output file … Access is denied`，同一个文件手写
    #: 却没问题 —— 安全软件对新目录/新文件的瞬时占用）。仓库内的目录稳定，用完即删。
    with tempfile.TemporaryDirectory(prefix="cyl_md_", dir=_bundle_tmp_root()) as tmp:
        tmpdir = Path(tmp)
        bundle, driver, cases = tmpdir / "md.mjs", tmpdir / "run.mjs", tmpdir / "cases.json"
        cases.write_text(json.dumps({"shapes": src, "breaks": [c[1] for c in MD_BREAKS],
                                     "cites": [c[1] for c in MD_CITES]},
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

    # ---- 引用标号可点（2026-10-07 第三条）：`[2, 4, 10]` → 按钮，且渲染出来的字一个不多一个不少 ----
    cite_htmls = data.get("cites") or []
    if len(cite_htmls) != len(MD_CITES):
        record("markdown", "引用标号渲染成可点的按钮（代码块/公式/真链接里不抢）", False,
               f"只回 {len(cite_htmls)} / {len(MD_CITES)} 条")
        return
    failed = []
    for (name, _src, must, must_not, plain_want), html in zip(MD_CITES, cite_htmls):
        plain = _md_plain(html).strip()
        if must and must not in html:
            failed.append(f"{name}（没渲染成按钮）")
        elif must_not and must_not in html:
            failed.append(f"{name}（不该动的地方动了）")
        elif plain_want is not None and plain != plain_want:
            failed.append(f"{name}（字变了：{plain[:24]}）")
        elif must and "cite-inline" in html and re.findall(r"\d+", _src) != re.findall(r"\d+", plain):
            # 编号必须**原样**出现（`[2,4,10]` 这种挤着写的会被规范化成 `[2, 4, 10]`，
            # 但数字一个都不能变、不能少）—— 偏移量坐标系就建在"渲染出来的字"上
            failed.append(f"{name}（编号变了：{plain[:24]}）")
    record("markdown", "引用标号渲染成可点的按钮（代码块/公式/真链接里不抢）",
           not failed, f"{len(MD_CITES)} 种形状全过" if not failed else "；".join(failed[:3]))

    # ---- 表格横向滚动（2026-10-08）：表宽跟栏宽**解耦**，窄了就在滚动容器里滚 ----
    # 为什么钉在源码层：这是纯 CSS 的决定，量几何得开浏览器；而回归的样子非常具体 ——
    # 谁把 width: max-content 改回 width: 100%，表格就被重新压进栏宽，换行位置随栏宽走
    # （实测同一张 7 列表在 340 / 420 / 520px 栏宽下行高 93 / 58 / 23px、首列 2 / 2 / 1 行，
    # 也就是用户说的"页面宽度一变就重排"）。
    css = (frontend / "src" / "styles" / "base.css").read_text(encoding="utf-8")
    tbl = re.search(r"\.md table \{([\s\S]*?)\n\}", css)
    wrap = re.search(r"\.md \.tbl-wrap \{([\s\S]*?)\n\}", css)
    tbl_body, wrap_body = (tbl.group(1) if tbl else ""), (wrap.group(1) if wrap else "")
    record("markdown", "表格横向滚动：表宽跟栏宽解耦（max-content），窄了在 .tbl-wrap 里滚",
           "width: max-content" in tbl_body and "min-width: 100%" in tbl_body
           and "overflow-x: auto" in wrap_body,
           f"table {'max-content' if 'width: max-content' in tbl_body else '缺'}"
           f" / min-width {'100%' if 'min-width: 100%' in tbl_body else '缺'}"
           f" / wrap overflow-x {'auto' if 'overflow-x: auto' in wrap_body else '缺'}")


# --------------------------------------------------------------------------
# 探针 ⑦：笔记整页（`/cylit/notes`）—— 批注 / 高亮 / 笔记合并成一个列表
#
# 为什么造数据：这一页的归并（高亮 + 它上面的笔记 + 批注 → 一串笔记）只有三类记录都存在时才看得清，
# 而库里未必有。造的三条**验完自删**（笔记 → 高亮 → 批注），一条不留。
# 编辑只验"打开编辑器看到的就是这条正文，然后取消"——**不写库**，自检不碰用户的东西。
# --------------------------------------------------------------------------
PROBE_NOTES = PROBE_HEAD + """
let started = false;
f.addEventListener('load', run);
setTimeout(() => { if (!started) run(); }, 2500);
async function run() {
  if (started) return;
  started = true;
  const w = f.contentWindow;
  const D = () => f.contentDocument;
  const res = { stage: 'loaded' };
  const api = '/api/v1';
  const jget = async (u) => (await fetch(u)).json();
  const post = async (u, body) => (await fetch(u, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })).json();
  /* 引用原文必须是**正文里真有的字**：写入口会验"这段在这一行里找得到"，假文本一律 400。
     __QUOTE__ / __BLOCK__ 由 Python 那边从真实译文里取好注进来（见 _real_quote）。 */
  const QUOTE = __QUOTE_JSON__;
  const BLOCK = __BLOCK_JSON__;
  /* 正文写得**够长**（多段）：缩略显示那条断言要看的正是"长正文被收成一两行"，
     只写一句话的话卡片的头/脚就占满了，收起来也不会变矮（第一版就是这么误报的）。 */
  const BODY_HEAD = '自检：高亮上的笔记正文';
  const BODY = BODY_HEAD + '\\n\\n第二段：' + '这一段用来把卡片撑高，'.repeat(6)
    + '\\n\\n第三段：再补一点，保证缩略态真的能变矮。';
  const ANN = '自检：这是一条批注';
  let hl = null, note = null, ann = null;
  try {
    /* 只传 selected_text、不传偏移量：后端按文字定位（新校验支持的正路） */
    const hlRes = await post(api + '/highlights', {
      paper_id: '__PAPER__', block_id: BLOCK, selected_text: QUOTE, color: 'yellow',
    });
    hl = hlRes && hlRes.data;
    if (!hl || !hl.id) {
      res.hlErr = String((hlRes && (hlRes.message || hlRes.detail)) || '建高亮失败');
      throw new Error('建高亮失败：' + res.hlErr);
    }
    const noteRes = await post(api + '/notes', {
      paper_id: '__PAPER__', block_id: BLOCK, highlight_id: hl.id,
      selected_text: QUOTE, content_md: BODY,
    });
    note = noteRes && noteRes.data;
    ann = (await post(api + '/annotations', {
      paper_id: '__PAPER__', block_id: 'p001-00', type: 'manual', content_md: ANN,
    })).data;
    /* 页面是先加载完的，造完数据点一下「刷新」。
       ⚠️ 要**重试**：页面的取数走 `withLoading('notes-overview')`，若那次请求还在飞，
       点刷新会被吞掉 —— 于是列表里只有"造笔记那一刻"的数据（实测整轮里偶发：批注不在列表里）。 */
    const refreshBtn = () => [...D().querySelectorAll('button')]
      .find((b) => (b.getAttribute('title') || '') === '刷新');
    for (let i = 0; i < 4; i += 1) {
      const btn = refreshBtn();
      if (btn) btn.click();
      await sleep(700); spin(BURN); await sleep(200);
      const txt = D().body ? (D().body.textContent || '') : '';
      if (txt.indexOf(BODY_HEAD) >= 0 && txt.indexOf(ANN) >= 0) break;
    }
    await until(D(), (x) => x.querySelector('[data-item-kind]'), 60);
    const rows = () => [...D().querySelectorAll('[data-item-kind]')];
    const rowWith = (text) => rows().find((r) => (r.textContent || '').indexOf(text) >= 0) || null;
    res.rowCount = rows().length;
    /* 一条 = 一处高亮 + 它的正文：两边都得在**同一张卡片**里 */
    const noteRow = rowWith(BODY_HEAD);
    res.hasNoteRow = !!noteRow;
    res.noteKind = noteRow ? noteRow.getAttribute('data-item-kind') : null;
    res.quoteInRow = !!noteRow && (noteRow.textContent || '').indexOf(QUOTE) >= 0;
    res.markInRow = !!noteRow && !!noteRow.querySelector('mark.hl');
    res.rowText = noteRow ? (noteRow.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80) : '';
    /* 批注也在同一个列表里（同一种卡片，没有"引用原文"那一行） */
    const annRow = rowWith(ANN);
    res.hasAnnRow = !!annRow;
    res.annKind = annRow ? annRow.getAttribute('data-item-kind') : null;
    /* 三个页签已经撤掉：页面上不该再有「高亮（N）/ 批注（N）」那排页签 */
    res.oldTabs = [...D().querySelectorAll('.tabs button')]
      .map((b) => (b.textContent || '').trim())
      .filter((t) => t.indexOf('高亮') >= 0 || t.indexOf('批注') >= 0);
    res.hasFeedBar = !!D().querySelector('.feed-bar');
    /* 缩略显示的开关与断言已随 eb6801b4 一起删除（前端不再有 `[data-notes-thumb]`）。 */
    /* 编辑入口：点「编辑笔记」→ 弹窗里就是这条正文 → **取消**（不写库） */
    if (noteRow) {
      const editBtn = [...noteRow.querySelectorAll('button')]
        .find((b) => (b.textContent || '').indexOf('编辑笔记') >= 0);
      res.hasEditBtn = !!editBtn;
      if (editBtn) {
        editBtn.click();
        await until(D(), (x) => x.querySelector('[data-feed-edit]'), 20);
        const ta = D().querySelector('[data-feed-edit]');
        res.editValue = ta ? String(ta.value) : null;
        const cancel = [...D().querySelectorAll('button')]
          .find((b) => (b.textContent || '').trim() === '取消');
        if (cancel) { cancel.click(); await sleep(300); spin(BURN); await sleep(150); }
        res.editClosed = !D().querySelector('[data-feed-edit]');
      }
    }
  } catch (err) {
    res.error = String((err && err.message) || err);
  }
  /* 收尾：自检造的三条全部删掉（笔记 → 高亮 → 批注），并确认搜不到了 */
  try {
    if (note) await fetch(api + '/notes/' + note.id, { method: 'DELETE' });
    if (hl) await fetch(api + '/highlights/' + hl.id, { method: 'DELETE' });
    if (ann) await fetch(api + '/annotations/' + ann.id, { method: 'DELETE' });
    const leftN = await jget(api + '/notes?paper_id=__PAPER__&q=' + encodeURIComponent(BODY_HEAD));
    const leftA = await jget(api + '/annotations?paper_id=__PAPER__');
    const leftH = await jget(api + '/highlights?paper_id=__PAPER__');
    res.leftNotes = (((leftN.data && leftN.data.items) || [])).length;
    res.leftHls = (((leftH.data && leftH.data.items) || []))
      .filter((h) => String(h.text || '') === QUOTE).length;
    res.leftAnns = (((leftA.data && leftA.data.items) || []))
      .filter((a) => String(a.content_md || '').indexOf('自检：这是') === 0).length;
  } catch (e) { res.leftErr = String(e); }
  out(res);
  document.title = 'done';
}
</script>"""


def check_notes_page(browser: str, base: str, paper: str, budget_ms: int) -> None:
    """笔记整页：三类记录归并成一个列表 + 缩略显示 + 编辑入口（造的数据验完自删）。"""
    print("\n⑦ 笔记整页（批注 / 高亮 / 笔记合并成一个列表；造的数据验完自删）")
    url = f"{base}/cylit/notes"
    try:
        block_id, quote = _real_quote(base, paper)
    except Exception as exc:  # noqa: BLE001 —— 取不到正文就只能如实记一条（不中断整轮）
        record("notes-page", "笔记整页能打开（探针拿到结果）", False, f"取不到真实正文：{str(exc)[:80]}")
        return
    html = (PROBE_NOTES.replace("__URL__", url).replace("__TITLE__", "notes")
            .replace("__PAPER__", paper)
            .replace("__QUOTE_JSON__", json.dumps(quote, ensure_ascii=False))
            .replace("__BLOCK_JSON__", json.dumps(block_id, ensure_ascii=False)))
    res = run_probe(browser, base, "probe_notes", html, max(budget_ms, 60_000))
    if not res:
        record("notes-page", "笔记整页能打开（探针拿到结果）", False, "探针没产出结果")
        return
    if res.get("error"):
        record("notes-page", "笔记整页能打开（探针拿到结果）", False, str(res.get("error"))[:90])
        return
    # 探针原始字段全量落盘（与 ⑥ 同一条约定：报告里只有断言结论，追问题要看中间量）
    (ROOT / "data" / "logs" / "_notes_raw.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    record("notes-page", "三页签（笔记 / 高亮 / 批注）已并成**一个列表**",
           res.get("oldTabs") == [] and res.get("hasFeedBar") is True,
           f"残留页签 {res.get('oldTabs')}，工具条={res.get('hasFeedBar')}，共 {res.get('rowCount')} 条")
    record("notes-page", "一条 = 一处高亮 + 它的正文（引用原文与正文在**同一张卡片**里）",
           res.get("hasNoteRow") is True and res.get("noteKind") == "note"
           and res.get("quoteInRow") is True and res.get("markInRow") is True,
           f"kind={res.get('noteKind')} 引用在卡里={res.get('quoteInRow')} "
           f"高亮底色={res.get('markInRow')}｜{str(res.get('rowText'))[:44]}")
    record("notes-page", "批注按**同一种卡片**列在同一个列表里",
           res.get("hasAnnRow") is True and res.get("annKind") == "annotation",
           f"kind={res.get('annKind')}")
    # 同上：缩略显示已随 eb6801b4 移除，这条断言一起删。
    record("notes-page", "编辑入口打开的就是这条正文（看完取消，不写库）",
           res.get("hasEditBtn") is True and str(res.get("editValue") or "") != ""
           and "自检" in str(res.get("editValue") or "") and res.get("editClosed") is True,
           f"弹窗内容={str(res.get('editValue'))[:24]!r} 关掉了={res.get('editClosed')}")
    record("notes-page", "自检不留痕（造的高亮 / 笔记 / 批注都已删）",
           res.get("leftNotes") == 0 and res.get("leftAnns") == 0 and res.get("leftHls") == 0,
           f"残留高亮 {res.get('leftHls')} 条、笔记 {res.get('leftNotes')} 条、"
           f"批注 {res.get('leftAnns')} 条、错误 {res.get('leftErr')}")


# --------------------------------------------------------------------------
# 参考文献的"发表网站"地址（`utils/references.ts`）：三级回退，同样用 node 打真代码
# --------------------------------------------------------------------------
#: (名字, 一条文献（`PaperReference` 的字段）, 期望地址里必须含的片段, 不许含的片段)
REF_CASES = [
    ("有 url 字段就用它", {"raw": "whatever", "url": "https://example.org/paper.pdf"},
     "https://example.org/paper.pdf", None),
    ("有 doi 字段 → doi.org", {"raw": "whatever", "doi": "10.1000/xyz"}, "https://doi.org/10.1000/xyz",
     None),
    ("doi 字段写成 doi.org 链接也认", {"raw": "", "doi": "https://doi.org/10.1/a"},
     "https://doi.org/10.1/a", None),
    ("原文里的 arXiv 号（库里最常见的那种）",
     {"raw": "[1] J. L. Ba, J. R. Kiros, and G. E. Hinton. Layer normalization. "
             "arXiv preprint arXiv:1607.06450, 2016."},
     "https://arxiv.org/abs/1607.06450", None),
    ("原文里的完整链接", {"raw": "代码见 https://github.com/x/y ，年份 2019"},
     "https://github.com/x/y", None),
    ("原文里裸写的 DOI（结尾标点要去掉）",
     {"raw": "Journal of X, doi:10.1234/abcd.5678, 2019"}, "https://doi.org/10.1234/abcd.5678", None),
    ("什么都没有 → 学术搜索兜底（**编号不进 query**）",
     {"raw": "[12] Some random reference without any identifier, 2019"},
     "scholar.google.com", "[12]"),
]

_REF_DRIVER = """import { readFileSync } from 'node:fs';
import { referenceUrl } from './refs.mjs';
const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));
console.log(JSON.stringify(cases.map((c) => referenceUrl(c))));
"""


def check_brand_assets(base: str) -> None:
    """品牌资源（免费、不开浏览器）：`/cylit/icon.png` 真能取到，且 index.html 指着它。

    图标本身是**离线裁好的**（仓库根 `icon.png` → Apple 连续圆角 → `frontend/public/icon.png`），
    这里只验"构建产物里有没有、路径对不对"——那是三次引用的共同前提
    （favicon / 侧栏品牌 / 新对话按钮）。
    """
    print("\n⑬ 品牌图标（favicon 与顶栏、新对话共用同一张裁好的 PNG）")
    url = f"{base}/cylit/icon.png"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            body, ctype, code = r.read(), r.headers.get("Content-Type", ""), r.status
    except Exception as exc:  # noqa: BLE001
        record("brand", "站点图标 `/cylit/icon.png` 可访问且是 PNG", False, str(exc)[:80])
        return
    record("brand", "站点图标 `/cylit/icon.png` 可访问且是 PNG",
           code == 200 and "image/png" in ctype and len(body) > 5000,
           f"{code} {ctype} {len(body)} bytes")
    try:
        with urllib.request.urlopen(f"{base}/cylit/", timeout=20) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        record("brand", "index.html 的 favicon 指着那张 PNG（不再是内联 SVG）", False, str(exc)[:80])
        return
    record("brand", "index.html 的 favicon 指着那张 PNG（不再是内联 SVG）",
           "/cylit/icon.png" in html and "apple-touch-icon" in html
           and "data:image/svg+xml" not in html,
           f"icon.png={'/cylit/icon.png' in html} apple-touch={'apple-touch-icon' in html} "
           f"残留内联 SVG={'data:image/svg+xml' in html}")


def check_shell(browser: str, base: str, budget_ms: int) -> None:
    """外壳：导航住在**左侧栏**，顶栏只装**论文标签**（2026-11 改版的口径）。

    零数据依赖 —— 只看 `/home` 这一屏的 DOM：不需要论文数据、不碰模型，所以它排在
    "主后端可达"那道门**前面**（起一个静态服务 / `vite preview` 就能跑）。
    首页那几块数据拿不到时页面只是少一块内容，外壳照样画得出来。
    """
    print("\n⓪b 外壳：左侧栏（导航）+ 顶栏（只有论文标签）")
    data = run_probe(browser, base, "probe_shell",
                     PROBE_SHELL.replace("__URL__", f"{base}/cylit/home").replace("__TITLE__", "shell"),
                     budget_ms)
    if not data:
        skip("外壳：左侧栏与顶栏", "探针没产出结果（服务没起 / /home 打不开）")
        return
    items = data.get("railItems") or []
    record("shell", "/home 有常驻左侧栏，且当前项高亮（工作区）",
           data.get("rail") is True and data.get("railSticky") == "sticky"
           and data.get("activeText") == "工作区"
           and str(data.get("activeBg") or "") not in ("", "transparent", "rgba(0, 0, 0, 0)"),
           f"侧栏 {data.get('railW')}px（{data.get('railSticky')}）当前项 "
           f"{data.get('activeText')!r} 底色 {data.get('activeBg')} 文字 {data.get('activeColor')}")
    record("shell", "侧栏导航项齐全（模型 / 任务**不单列**，已并入设置页）",
           items == ["工作区", "论文库", "论文解读", "知识库", "学习", "笔记", "AI 对话", "设置"],
           f"{items}")
    record("shell", "品牌在**顶栏**：图标在「Cylit」左侧、垂直居中（侧栏里不再有第二份）",
           data.get("brandMark") is True and data.get("brandLeftOfText") is True
           and data.get("brandText") == "Cylit" and data.get("railBrand") is False
           and str(data.get("brandHref") or "").rstrip("/").endswith("/home")
           and "icon.png" in str(data.get("brandSrc") or ""),
           f"顶栏有图标={data.get('brandMark')}（{data.get('brandW')}px）文字="
           f"{data.get('brandText')!r} 在文字左侧={data.get('brandLeftOfText')} "
           f"侧栏里还有品牌={data.get('railBrand')} src={data.get('brandSrc')}")
    # 顶栏只允许出现：胶囊托盘 + 论文标签（+ 两端浮动圆钮）。四样"不该有"的东西逐个钉住，
    # 因为每一样都是从"导航住在顶栏"那个旧结构里留下来的（品牌 / 首页胶囊 / 新建 / AI 对话）。
    # 用户口径 2026-11：顶栏**不要**独立的「首页」按钮 —— 品牌（Cylit）本身就是回工作区的入口；
    # 「+」新建入口与「AI 对话」胶囊也都不要（分别被侧栏的论文库 / AI 对话取代）。
    record("shell", "顶栏没有独立的「首页」按钮、没有「+」新建入口、没有「AI 对话」胶囊（品牌即入口）",
           data.get("tabbar") is True
           and data.get("tabbarBrand") is True
           and data.get("tabbarPinned") is False
           and data.get("tabAdd") is False
           and data.get("chatPill") is False,
           f"品牌={data.get('tabbarBrand')} 首页胶囊={data.get('tabbarPinned')} "
           f"加号={data.get('tabAdd')} 对话胶囊={data.get('chatPill')}")
    # 顶栏品牌本身就是"回工作区"的链接（点 Cylit 回 /home）
    record("shell", "点顶栏的 Cylit 回工作区（品牌是指到 /home 的链接）",
           str(data.get("brandHref") or "").rstrip("/").endswith("/home"),
           f"href={data.get('brandHref')!r}")
    record("shell", "没有论文标签时托盘整块不渲染（顶栏就是一条空条，不留空胶囊）",
           not (data.get("tabs") or []) and data.get("stripRendered") is False,
           f"标签={data.get('tabs')} 托盘渲染={data.get('stripRendered')}（{data.get('path')}）")
    # ---- 几何：顶栏通栏 + 侧栏在顶栏下方（用户口径 2026-11） ----
    record("shell", "顶栏**通栏**：从视口左边缘 0 起、到右边缘（不再被侧栏截断）",
           data.get("tabbarLeft") == 0
           and abs((data.get("tabbarRight") or 0) - (data.get("viewW") or 0)) <= 2,
           f"左 {data.get('tabbarLeft')} / 右 {data.get('tabbarRight')}（视口 {data.get('viewW')}）")
    record("shell", "侧栏在顶栏**下方**：top = 顶栏高（≈52），底边贴视口底部",
           data.get("railSticky") == "sticky"
           and abs((data.get("railTop") or -1) - (data.get("headerH") or 0)) <= 2
           and abs((data.get("railBottom") or 0) - (data.get("viewH") or 0)) <= 2,
           f"顶栏高 {data.get('headerH')}，侧栏 top {data.get('railTop')} / 底 {data.get('railBottom')}"
           f"（视口高 {data.get('viewH')}）")
    # ---- 手动收缩：64px 图标轨 ↔ 216px（状态存 stores/shellPrefs 的 pm.shell.rail） ----
    record("shell", "侧栏底部的圆钮可收缩：64px 图标轨（label 不可见），再点回 216px",
           data.get("toggle") is True
           and data.get("wideW") == 216 and data.get("wideLabelVisible") is True
           and data.get("narrowW") == 64 and data.get("narrowLabelHidden") is True
           and data.get("backW") == 216 and data.get("backLabelVisible") is True,
           f"{data.get('wideW')}px →（点）{data.get('narrowW')}px label 隐藏="
           f"{data.get('narrowLabelHidden')}（title={data.get('narrowTitle')!r}）→（再点）"
           f"{data.get('backW')}px label 可见={data.get('backLabelVisible')}")


def check_tabs(browser: str, base: str, budget_ms: int) -> None:
    """探针 ④：导航页的分区记忆（停在 #kb，切走再回来还是那一区）。

    零数据依赖：只看设置页的 DOM + `location.hash`，不需要论文数据 —— 所以它既能排在
    "主后端可达"那道门后面，也能被 `--static` 单独拉出来跑（静态服务下一样出结论）。
    """
    print("\n④ 标签页状态：切走再回来，分区与阅读位置还在")
    tabs = run_probe(browser, base, "probe_tabs",
                     PROBE_TABS.replace("__URL__", f"{base}/cylit/settings").replace("__TITLE__", "tabs"),
                     budget_ms)
    if not tabs:
        skip("标签页状态", "探针没产出结果（服务没起 / 设置页打不开）")
        return
    record("tabs", "设置页的分区记进 URL（点「索引管理」就是 #kb 那一区）",
           tabs.get("hashKb") == "#kb" and bool(tabs.get("secKb")),
           f"{tabs.get('hashKb')} / {tabs.get('secKb')}")
    # 2026-11 标签条收窄成"只装论文标签"之后，导航页的分区记忆搬到了 `stores/navMemory.ts`
    # （localStorage 键 `pm.shell.navhash`）：侧栏点「设置」时会带上上次记下的 hash。
    # ⚠️ 这依赖那层记忆 —— 删掉 `stores/navMemory.ts`、或不再在 `router.afterEach` 里记录，这条就会红。
    record("tabs", "设置页的分区**记得住**（从侧栏离开再点回来还是 #kb 那一区）",
           tabs.get("clickedHome") is True and tabs.get("clickedSettings") is True
           and tabs.get("hashKb") == "#kb" and tabs.get("backHash") == "#kb"
           and bool(tabs.get("secKb")) and tabs.get("backSec") == tabs.get("secKb"),
           f"离开 {tabs.get('hashKb')!r}/{tabs.get('secKb')!r} → "
           f"回来 {tabs.get('backHash')!r}/{tabs.get('backSec')!r}")
    # 滚动那条要页面**真的够长**才量得到：静态服务（没有后端数据）下设置页不足一屏，
    # `maxY` 是 0 —— 那种情况按"这一屏滚不动"跳过，而不是记一条假红。
    if (tabs.get("maxY") or 0) <= 200:
        skip("切走再回来阅读位置还在",
             f"页面滚不动（可滚 {tabs.get('maxY')}px，静态服务下设置页不足一屏）")
    else:
        record("tabs", "切走再回来阅读位置还在（窗口滚动回到离开时）",
               (tabs.get("scrolled") or 0) > 200
               and abs((tabs.get("backY") or 0) - (tabs.get("scrolled") or 0)) <= 60,
               f"离开 {tabs.get('scrolled')}px（可滚 {tabs.get('maxY')}）→ 回来 {tabs.get('backY')}px")

def check_reference_urls() -> None:
    """把上面那张表喂给**真代码** `utils/references.ts`（esbuild bundle → node）。"""
    print("\n⓪ 参考文献地址：结构化字段 → 原文里抠 DOI/arXiv → 学术搜索")
    node = shutil.which("node")
    frontend = ROOT / "frontend"
    if not node or not (frontend / "node_modules" / "esbuild").exists():
        skip("参考文献地址三级回退", "没有 node / esbuild（先在 frontend 里 npm i）")
        return
    with tempfile.TemporaryDirectory(prefix="cyl_ref_", dir=_bundle_tmp_root()) as tmp:
        tmpdir = Path(tmp)
        bundle, driver, cases = tmpdir / "refs.mjs", tmpdir / "run.mjs", tmpdir / "cases.json"
        cases.write_text(json.dumps([c[1] for c in REF_CASES], ensure_ascii=False),
                         encoding="utf-8")
        driver.write_text(_REF_DRIVER, encoding="utf-8")
        build = subprocess.run(
            [node, "-e",
             "require('esbuild').buildSync({entryPoints:['src/utils/references.ts'],"
             "bundle:true,format:'esm',alias:{'@':'./src'},logLevel:'error',"
             "outfile:process.argv[1]})", str(bundle)],
            cwd=frontend, capture_output=True, text=True)
        if build.returncode != 0 or not bundle.exists():
            record("refs", "参考文献地址三级回退能跑（bundle 真代码）", False,
                   f"bundle 失败：{build.stderr[-200:]}")
            return
        proc = subprocess.run([node, str(driver), str(cases)], cwd=tmpdir,
                              capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            record("refs", "参考文献地址三级回退能跑（bundle 真代码）", False,
                   f"node 失败：{(proc.stderr or '')[-200:]}")
            return
        urls = json.loads(proc.stdout)
    failed = []
    for (name, _ref, want, must_not), url in zip(REF_CASES, urls):
        text = str(url)
        if want not in text or (must_not and (must_not in text
                                              or must_not.replace("[", "%5B").replace("]", "%5D") in text)):
            failed.append(f"{name}（{text[:40]}）")
    record("refs", "参考文献地址三级回退（url / doi / 原文里的 arXiv·链接·DOI / 学术搜索）",
           len(urls) == len(REF_CASES) and not failed,
           f"{len(REF_CASES)} 条全过" if not failed else "；".join(failed[:3]))


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
    ap.add_argument("--static", action="store_true",
                    help="只跑零数据依赖的探针（源码结构 + 外壳 + 标签页状态）：静态服务也能出结论")
    ap.add_argument("--model", action="store_true",
                    help="额外跑一条流式问答（**会调用模型**；跑完自动删掉造出来的会话）")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 78)
    print("Cylit 前端自检" + ("（含流式问答：--model）" if args.model else "（默认零模型调用）"))
    print("=" * 78)
    check_markdown_shapes()          # 不依赖浏览器与服务，先跑它
    check_reference_urls()           # 同上：bundle 真代码，不依赖浏览器 / 数据
    check_explain_shell()            # 同上：只看源码结构，起服务前就能跑
    check_cite_hover()               # 同上：只读源码（悬浮延时那三个点）
    check_trace_motion()             # 同上：只读源码（轨迹过渡接线 + 自动展开判定）
    browser = find_browser(args.browser)
    if not browser:
        print("[SKIP] 没找到浏览器（--browser 指定 chrome/edge 路径）")
        return 0
    # 外壳探针零数据依赖：排在"主后端可达"这道门**前面**，静态服务下也跑得动
    check_shell(browser, base, args.budget)
    if args.static:
        # --static：不碰任何需要后端数据的探针（外壳 / 导航分区只靠 DOM + location.hash），
        # 给"起个 vite preview 看外壳"的场景用 —— 没有后端也能出一条真结论。
        check_tabs(browser, base, max(args.budget, 60000))
        return 0
    status, _ = fetch(f"{base}/api/v1/agent/status", timeout=10)
    if status != 200:
        print(f"[SKIP] 主后端不可达（HTTP {status}）：先 `pr.py serve`")
        return 0

    reader_url = f"{base}/cylit/reader/{args.paper}?nosse=1"
    chat_url = f"{base}/cylit/reader/{args.paper}?side=chat&nosse=1"

    # ---- ① 阅读器 ----
    print("\n① 阅读器：正文 / 公式 / 胶囊")
    data = run_probe(browser, base, "probe_reader",
                     PROBE_READER.replace("__URL__", reader_url).replace("__TITLE__", "reader")
                     .replace("__PAPER__", args.paper),
                     args.budget)
    if data:
        # 中间量全量存一份（与 `_pdfhl_raw.json` 同一个理由）：报告里只有断言结论，
        # 追"这条为什么红"时要看的是这些字段。
        (ROOT / "data" / "logs" / "_reader_raw.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tabs = data.get("tabs") or []
        record("reader", "侧栏页签顺序（目录第一、对话第二）",
               len(tabs) > 1 and "目录" in tabs[0] and data.get("chatTabIndex") == 1, f"{tabs[:3]}…")
        record("reader", "正文行渲染", data.get("rows", 0) > 0, f"{data.get('rows')} 行")
        record("reader", "公式渲染成 KaTeX", data.get("katex", 0) > 0, f"{data.get('katex')} 个")
        seg_thumb = data.get("segThumb") or {}
        record("reader", "视图胶囊三层（托盘无描边 + 选中项由滑块表达：滑块盖住 active 且带阴影）",
               data.get("segBorderless") is True
               and data.get("segActiveBg") == "rgba(0, 0, 0, 0)"
               and seg_thumb.get("shadow") is True
               and abs((seg_thumb.get("w") or 0) - (data.get("segActiveW") or 0)) <= 1
               and abs((seg_thumb.get("x") or 0) - (seg_thumb.get("activeX") or 0)) <= 1,
               f"托盘 {data.get('segTray')} / 选中项底色 {data.get('segActiveBg')}"
               f" / 滑块 {seg_thumb.get('w')}px@{seg_thumb.get('x')}"
               f"（active {data.get('segActiveW')}px@{seg_thumb.get('activeX')}，阴影 {seg_thumb.get('shadow')}）")
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
        # ---- 「文献」页签（2026-10-07 第三条）----
        labels = data.get("tabLabels") or []
        record("reader", "页签名都压到**两个汉字**，且「文献」在**倒数第二个**",
               bool(labels) and all(len(t) == 2 for t in labels)
               and len(labels) >= 2 and labels[-2] == "文献",
               f"页签：{'/'.join(labels)}")
        if not data.get("citeChips"):
            record("reader", "正文里的引用标号 `[2, 4, 10]` 是可点的按钮", False,
                   f"正文里没找到标号（共 {data.get('citeChips')} 个）")
        else:
            record("reader", "正文里的引用标号 `[2, 4, 10]` 是可点的按钮",
                   str(data.get("refsTab") or "") == "文献"
                   and str(data.get("citeSample") or "").startswith("["),
                   f"页签={data.get('refsTab')!r} 标号样例={data.get('citeSample')!r}")
            record("reader", "两枚页签图标：文献=quote（双引号）、笔记=note（文稿）",
                   data.get("refsIcon") == "quote" and data.get("notesIcon") == "note",
                   f"文献={data.get('refsIcon')!r} 笔记={data.get('notesIcon')!r}")
            record("reader", "正文里的引用标号样式 = **对话页那枚**（淡底胶囊 + 下划线 + mono 11px/700）",
                   _cite_style_ok(data.get("citeStyle")),
                   f"{data.get('citeStyle')}")
            clicked = data.get("clickedRefs") or []
            record("reader", "点引用标号 → 就地弹浮层，**只显示被提到的那几条**（不再动侧栏）",
                   data.get("citePop") is True
                   and sorted(data.get("citePopNos") or []) == sorted(clicked)
                   and data.get("activeAfterClick") == data.get("tabBeforeClick"),
                   f"点了 {clicked} → 浮层 {data.get('citePopNos')}（侧栏页签 "
                   f"{data.get('tabBeforeClick')!r} → {data.get('activeAfterClick')!r}）")
            record("reader", "浮层里的卡片就是侧栏那份（`.ref-item` + 左边那枚跳转图标）",
                   data.get("citePopCard") is True
                   and (data.get("citePopRows") or 0) == len(clicked),
                   f"{data.get('citePopRows')} 张卡片，带跳转图标={data.get('citePopCard')}")
            record("reader", "鼠标移开浮层 → 浮层自己收掉（用户口径）",
                   data.get("citePopAfterLeave") is False,
                   f"移开后还在={data.get('citePopAfterLeave')}")
            record("reader", "标号上没有原生 `title` 提示条（悬浮的效果由浮层负责）",
                   not str(data.get("citeTitle") or ""),
                   f"title={data.get('citeTitle')!r}")
            record("reader", "鼠标**悬浮**标号 = 点一下（弹出同一个浮层，移开就收）",
                   sorted(data.get("citePopHoverNos") or []) == sorted(clicked)
                   and data.get("citePopAfterHoverOut") is False,
                   f"悬浮 → 浮层 {data.get('citePopHoverNos')}；移开后还在={data.get('citePopAfterHoverOut')}")
            glass = data.get("citePopGlass") or {}
            record("reader", "浮层是**毛玻璃**：半透明底 + backdrop blur + 真阴影（不是白盒子）",
                   glass.get("blur") is True and float(glass.get("alpha") or 1) < 1
                   and str(glass.get("shadow") or "none") != "none",
                   f"blur={glass.get('blur')} 底色={glass.get('bg')} 阴影={str(glass.get('shadow'))[:28]}")
            record("reader", "浮层**压在正文/弹窗之上**（markdown 视图是弹窗，浮层必须在它上面）",
                   (data.get("citePopAbove") or {}).get("ok") is True,
                   f"浮层中心点命中={data.get('citePopAbove')}")
            if data.get("hasFormula"):
                record("reader", "**公式整段选中 → 复制的是 LaTeX**（不是排版后的字形）",
                       data.get("texCopied") is True,
                       f"TeX={str(data.get('texExpect'))[:26]!r} → 剪贴板={str(data.get('texCopy'))[:30]!r}")
                record("reader", "没选中公式的复制**一个字都不改**（不接管那次 copy）",
                       data.get("plainCopyUntouched") is True,
                       f"未接管={data.get('plainCopyUntouched')}")
                if data.get("dispCopy") is not None:
                    record("reader", "**行间公式**整段选中 → 复制成 `$$…$$`",
                           data.get("dispCopied") is True,
                           f"剪贴板={str(data.get('dispCopy'))[:34]!r}")
            else:
                skip("公式整段选中 → 复制 LaTeX", "这一屏没找到公式")
            # ---- 右键菜单（毛玻璃） ----
            glass = data.get("ctxGlass") or {}
            blank = data.get("ctxKeysBlank") or []
            selk = data.get("ctxKeysSel") or []
            # 材质口径（2026-10-18）：**与顶部胶囊同一块玻璃** —— 上亮下暗的渐变本体 + 48px 饱和模糊
            # + 柔光投影，**不画描边**（描边在深色下会变成一圈发光环）。所以这里不再要求 1px 实线，
            # 浓度也从**渐变**里读（backgroundColor 是透明的，见探针那段注释）。
            record("reader", "右键接管了原生菜单，弹的是**复刻目标站的玻璃**（半透明渐变底 + blur + 真阴影）",
                   # 渐变的上沿就是 --glass-bar-top 的默认 80%，所以阈值放到 0.9：要的是"透"，
                   # 不是某个固定数字（1.0 = 实心白盒子，那才是这条要抓的）。
                   data.get("ctxPrevented") is True and glass.get("blur") is True
                   and float(glass.get("alpha") or 1) < 0.9
                   and str(glass.get("shadow") or "none") != "none"
                   and (bool(str(glass.get("bgImage") or "")) or float(glass.get("alpha") or 1) < 1),
                   f"接管={data.get('ctxPrevented')} blur={glass.get('blur')} "
                   f"底色-alpha={glass.get('alpha')} 渐变={str(glass.get('bgImage'))[:32]!r} "
                   f"border={glass.get('border')} {glass.get('items')} 项")
            # 图标口径：**以填充型（-fill）为主**，但「高亮」「询问 Cyli」「加入对话」保留线性图标、
            # 「导出」用 download（见 ReaderView 那段 2026-10-09 的口径）。所以判据是"每枚都用
            # currentColor 上色"（fill 或 stroke 等于按钮自己的文字色），而不是"一律填充型"。
            icons = glass.get("icons") or []
            fill_ok = [i for i in icons
                       if not i.get("none") and (i.get("fill") == i.get("col")
                                                 or i.get("stroke") == i.get("col"))]
            fill_type = [i for i in icons if not i.get("none") and i.get("fill") == i.get("col")]
            record("reader", "右键菜单项的图标都跟着**文字色**走（填充型走 fill、线性图标走 stroke）",
                   bool(icons) and len(fill_ok) == len(icons) and bool(fill_type),
                   f"{len(fill_ok)}/{len(icons)} 枚 currentColor，其中填充型 {len(fill_type)} 枚"
                   f"（{[i.get('key') for i in fill_type]}）");
            record("reader", "没选区时**不给**复制/高亮（只给全选、导出、图片项）",
                   "selectall" in blank and "export" in blank and "copy" not in blank
                   and "highlight" not in blank,
                   f"清单={blank}")
            # 桌面 / 触摸两套清单（用户口径 2026-10-10 + 2026-10-17，见 ReaderView.onContentContextMenu）：
            #   · **桌面**右键：搜索 / 复制 / 加入对话 / … / 询问 Cyli / 全选。**没有「高亮」** ——
            #     选色由划词胶囊上那排色点承担，菜单里再来一遍是噪音；
            #   · **触摸长按**（fromTouch）：色板和动作合成一条工具条，那一枚的 key 是 hl-color；
            #   · 「导出」只在**空白处**那套里（对一段选中的文字没有"导出 Markdown"这件事）。
            # 旧断言按"桌面也要有 highlight + export"写，2026-10-10 之后就是过期口径。
            record("reader", "划词后右键 → 搜索 / 复制 / 加入对话 / 询问 Cyli / 全选 都在"
                             "（「高亮」只在触摸工具条上、「导出」只在空白处那套）",
                   all(k in selk for k in ("ask", "tochat", "bing", "copy", "selectall"))
                   and "hl-color" not in selk and "export" not in selk,
                   f"清单={selk}")
            record("reader", "右键顺序复刻目标站：搜索 → 复制 → 加入对话 → 询问 Cyli → 全选（导出在空白处收尾）",
                   all(k in selk for k in ("bing", "copy", "tochat", "ask", "selectall"))
                   and selk.index("bing") < selk.index("copy") < selk.index("tochat")
                   < selk.index("ask") < selk.index("selectall")
                   and "export" in blank and blank.index("export") > blank.index("selectall"),
                   f"清单={selk}｜空白处={blank}")
            bing_url = str(data.get("ctxBing") or "")
            record("reader", "点「网页搜索（Bing）」→ 新标签打开 bing 搜索（带选中的文字）",
                   bing_url.startswith("https://www.bing.com/search?q=") and len(bing_url) > 40,
                   f"{bing_url[:64]}")
            record("reader", "点别处右键菜单自己收掉",
                   data.get("ctxClosed") is True, f"还开着={not data.get('ctxClosed')}")
            if data.get("ctxHasImg"):
                imgk = data.get("ctxKeysImg") or []
                record("reader", "落在图片上 → 询问 Cyli / 另存为 / 放大 / 加入对话",
                       all(k in imgk for k in ("ask", "saveimg", "zoomimg", "tochat")),
                       f"清单={imgk}｜{str(data.get('ctxImgUrl'))[-40:]}")
            else:
                skip("右键落在图片上", "这一屏没有图片")
            url = str(data.get("refUrl") or "")
            record("reader", "点文献条目 → 跳到发表网站（DOI / arXiv / 原文里的链接 / 学术搜索）",
                   url.startswith("http")
                   and any(k in url for k in ("doi.org", "arxiv.org", "scholar.google.com", "://")),
                   f"{url[:70]}")
        chat_pane = data.get("chatPane") or {}
        search_pane = data.get("searchPane") or {}
        notes_pane = data.get("notesPane") or {}

        def _pane_ok(p: dict) -> bool:
            return bool(p) and p.get("over", 99) <= 1 and 0 < p.get("h", 0) < p.get("asideH", 0)

        record("reader", "直挂侧栏的页签（对话 / 搜索）**自己撑满剩余高度**，底部不再被裁",
               _pane_ok(chat_pane) and _pane_ok(search_pane) and _pane_ok(notes_pane),
               f"对话 越出 {chat_pane.get('over')}px（高 {chat_pane.get('h')}/{chat_pane.get('asideH')}）· "
               f"搜索 越出 {search_pane.get('over')}px（高 {search_pane.get('h')}/{search_pane.get('asideH')}）· "
               f"笔记（对照）越出 {notes_pane.get('over')}px")
        record("reader", "目录页：**全部收起 / 全部展开在胶囊右侧**（紧挨着，几何上确实在右边）",
               data.get("tocUnfoldCount") == 2 and data.get("tocUnfoldRightOfSeg") is True,
               f"{data.get('tocUnfoldCount')} 枚展开/收起按钮，在胶囊右侧="
               f"{data.get('tocUnfoldRightOfSeg')}")
        record("reader", "侧栏页签记进 URL（切标签回来还是那一栏）",
               "side=chat" in str(data.get("sideUrl") or "")
               and str(data.get("sideBack") or "").startswith("对话"),
               f"{data.get('sideUrl')} → {data.get('sideBack')}")
        # ---- 「目录 / 页面视图」切换（共享阅读位置 + 缩略图 + 占位 + 点击跳页） ----
        if not data.get("tocModes"):
            record("reader", "目录页签里有「目录 / 页面视图」切换", False, "两枚切换按钮没找到")
        else:
            record("reader", "目录页签里有「目录 / 页面视图」切换", True, "两枚切换按钮都在")
            record("reader", "页面视图：一页一行（类似插图页）",
                   bool(data.get("thumbOrdered")) and bool(data.get("stacked")),
                   f"{data.get('thumbCount')} 张，上下排={data.get('stacked')}")
            record("reader", "页面视图：页号在**图片外面**、图片左上角（不压图、无定位键）",
                   data.get("numAboveImg") is True and data.get("numLeftAligned") is True
                   and data.get("numStatic") is True and data.get("numOutsideFrame") is True
                   and data.get("noLocateBtn") is True
                   and (data.get("thumbNums") or [])[:2] == ["第 1 页", "第 2 页"]
                   and "/pages/1" in str(data.get("thumbSrc") or ""),
                   f"页号 {data.get('thumbNums')}，在图上方={data.get('numAboveImg')} "
                   f"左对齐={data.get('numLeftAligned')} 在图框外={data.get('numOutsideFrame')} "
                   f"定位键={not data.get('noLocateBtn')}")
            record("reader", "页面视图：图不铺满整栏（封顶显示）+ 触摸能滚（图不吃指针事件、格允许 pan-y）",
                   data.get("imgCapped") is True and data.get("imgNoPointer") is True
                   and "pan-y" in str(data.get("tileTouchAction") or ""),
                   f"封顶={data.get('imgCapped')} 图不吃指针={data.get('imgNoPointer')} "
                   f"touch-action={data.get('tileTouchAction')!r}")
            record("reader", "目录那一行：切换只剩图标、没有多余文字（页数与提示都去掉）",
                   (data.get("modeLabels") or []) == ["", ""]
                   and data.get("noPagesHead") is True and str(data.get("barText") or "") == "",
                   f"胶囊文字 {data.get('modeLabels')}，旧页头还在={not data.get('noPagesHead')}，"
                   f"整行文字={data.get('barText')!r}")
            record("reader", "页面视图：当前阅读页高亮（与目录共享同一个读到哪）",
                   isinstance(data.get("thumbCurrent"), list) and len(data["thumbCurrent"]) <= 1,
                   f"高亮页 {data.get('thumbCurrent')}")
            record("reader", "页面视图：取图失败显示占位，页码还在",
                   data.get("failedPh") is True and data.get("failedNo") == "第 1 页",
                   f"占位={data.get('failedPh')}，页码={data.get('failedNo')}")
            record("reader", "页面视图：点图直接跳到那一页（URL 写 ?page=N）",
                   f"page={data.get('jumpWanted')}" in str(data.get("jumpUrl") or "")
                   and data.get("jumpActive") == [data.get("jumpWanted")],
                   f"想跳 {data.get('jumpWanted')}，URL {data.get('jumpUrl')}，高亮 {data.get('jumpActive')}")
            record("reader", "页面视图：整列**居中**",
                   data.get("centered") is True,
                   f"格中心 {data.get('tileCenter')} / 内容区中心 {data.get('paneInnerCenter')}")
            record("reader", "目录页**自己就是个滚动容器**（内容超出时真的滚得动）",
                   data.get("paneOverflowY") in ("auto", "scroll")
                   and data.get("paneScrolledTo", 0) > 0
                   and data.get("tocPaneScrolledTo", 0) > 0,
                   f"页面视图 {data.get('paneScroll')} → scrollTop {data.get('paneScrolledTo')}；"
                   f"目录 {data.get('tocPaneScroll')} → {data.get('tocPaneScrolledTo')}")
            record("reader", "页面视图能切回目录（大纲还在）", data.get("backToOutline") is True, "")

    # ---- ② 对话面板（不发消息） ----
    print("\n② 对话面板：一级页 / 二级页 / 确认框（不发消息）")
    chat = run_probe(browser, base, "probe_chat",
                     PROBE_CHAT.replace("__URL__", chat_url).replace("__TITLE__", "chat"),
                     args.budget)
    if chat:
        record("chat", "顶部工具栏只有「新对话」一个按钮",
               (chat.get("headBtns") or []) == ["新对话"], f"{chat.get('headBtns')}")
        record("chat", "空态标记 = 应用图标，且在**居中语录的上方**且居中（取代双引号水印）",
               "icon.png" in str(chat.get("helloMarkSrc") or "")
               and chat.get("helloMarkAbove") is True
               and chat.get("helloMarkCentered") is True,
               f"src={chat.get('helloMarkSrc')} 在语录上方={chat.get('helloMarkAbove')} "
               f"居中={chat.get('helloMarkCentered')}｜{chat.get('helloMarkDebug')}")
        # 「竖直居中」以**看得见的那块高度**为准（输入区悬浮在 .msgs 底部，见探针里那段注释）
        record("chat", "空态这一组（图标 + 语录）在看得见的那块高度里**竖直居中**，图标够大",
               chat.get("helloCenteredV") is True and (chat.get("helloMarkSize") or 0) >= 48,
               f"竖直居中={chat.get('helloCenteredV')} 图标 {chat.get('helloMarkSize')}px"
               f"｜{chat.get('helloSelfDebug')}")
        # 「管理」2026-11-21 改成**多选圆钮**（.multi-btn），不在托盘里 —— 托盘只剩「分类」这一枚。
        # 旧断言（2 枚托盘）是那次改形态之前的；「无范围选择器」这一点仍然成立。
        record("chat", "分类只有 1 枚胶囊托盘（全部 / 划词 / 常规），「管理」是多选圆钮、不在托盘里",
               chat.get("trayCount") == 1
               and (chat.get("segBtns") or []) == ["全部", "划词", "常规"]
               and chat.get("multiBtn") is True and chat.get("multiBtnRound") is True,
               f"{chat.get('trayCount')} 枚 / {chat.get('segBtns')} / 多选圆钮={chat.get('multiBtnRound')}")
        # 胶囊本身的**样式**三处一字不差；`left` 不参与这条比较 —— 目录那枚左边还有
        # 「全部折叠 / 全部展开」两枚按钮（用户明确要求放左侧），它本来就不该在 8px 处；
        # "位置统一"由下一条（工具条那一行的内容原点）来钉。
        def _seg_core(s: dict) -> dict:
            s = s or {}
            return {k: s.get(k) for k in ("h", "radius", "bg", "padTop", "btnH")}

        chat_seg = chat.get("segStyle") or {}
        toc_seg = data.get("tocSeg") or {}
        formula_seg = data.get("formulaSeg") or {}
        record("chat", "胶囊样式：目录 / 对话 / 公式**三处完全一致**（以对话页为准）",
               bool(chat_seg) and _seg_core(chat_seg) == _seg_core(toc_seg) == _seg_core(formula_seg),
               f"对话 {_seg_core(chat_seg)}｜目录 {_seg_core(toc_seg)}｜公式 {_seg_core(formula_seg)}"
               f"（离面板左边：对话 {chat_seg.get('left')} / 公式 {formula_seg.get('left')}，"
               f"目录 {toc_seg.get('left')} = 8 + 两枚展开按钮）")
        origins = [chat.get("headOrigin"), data.get("tocOrigin"), data.get("formulaOrigin")]
        record("chat", "工具条那一行的内容原点三处一致（都在离面板左上角 8px / 8px 处）",
               all(o == origins[0] for o in origins) and origins[0] == {"left": 8, "top": 8},
               f"对话 {origins[0]}｜目录 {origins[1]}｜公式 {origins[2]}")
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
        # 顶部现在是三枚图标钮（返回 / 浮层打开 / 新建）：判据是「没有文字钮、名字在 title 上」，
        # 不再钉死枚数（2026-11-19 拆出「浮层打开」之后从两枚变成三枚）。
        hb = chat.get("threadHeadBtns") or []
        record("chat", "会话页顶部不重复「新对话」文字（只留图标钮，名称在 title 上）",
               len(hb) >= 2 and all((x.get("t") or "") == "" and (x.get("title") or "") for x in hb)
               and not any("新对话" in (x.get("t") or "") for x in hb),
               f"{[x.get('title') for x in hb]}")
        record("chat", "输入框文案 = 与Cyli深度交流", chat.get("placeholder") == "与Cyli深度交流",
               f"{chat.get('placeholder')!r}")
        record("chat", "空态只有一句语录当背景（0 个按钮）",
               chat.get("helloButtons") == 0
               and (chat.get("helloText") or "").startswith("与Cyli一起讨论"),
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
            # 语义（2026-11-23 起）：对话面板**常驻**（v-show 收起，实例不卸载），
            # 所以断言从"切走后应卸载"改成"切走后仍在 DOM 里、且草稿没丢"。
            record("chat", "切走页签再回来不重挂（面板常驻、草稿还在）",
                   chat.get("chatUnmounted") is False and chat.get("draftKept") == "keepalive-check",
                   f"切走时面板仍在={chat.get('chatUnmounted') is False} "
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
            if not fl:
                # 这条断言点在**用户自己**最近那条会话的引用锚上：锚的 `data-row` 未必还存在于
                # 这篇论文（可能引的是别篇，或行号在重译后变了）。一个可定位的锚都没有时，
                # 这不是"功能坏了"，是**无从判定** —— 报 SKIP 并写清原因。
                skip("聊天：连点同一个引用标号：每次都重播高亮",
                     f"这条会话没有可定位的引用锚（锚 {chat.get('inlineCites')} 个，"
                     f"其中行号还在的 {chat.get('usableCites')} 个）")
            else:
                record("chat", "连点同一个引用标号：每次都重播高亮（动画名 -a/-b 交替）",
                       f1.get("kind") in ("flash-a", "flash-b")
                       and f2.get("kind") in ("flash-a", "flash-b")
                       and f1.get("kind") != f2.get("kind")
                       and f2.get("fresh") is True and bool(f1.get("row")),
                       f"{f1} → {f2}")

    # ---- ④ 过程轨迹：造一条临时会话，验完删掉（免费：不调模型） ----
    check_run_trace(browser, base, args.paper, args.budget)

    # ---- ⑥ PDF 页图高亮：划词 → 存库 → 画在文字层 → 刷新后仍在 → 自删 ----
    check_pdf_highlight(browser, base, args.paper, args.budget, model=args.model)
    check_notes_page(browser, base, args.paper, args.budget)
    check_references_pane(browser, base, args.paper, args.budget)
    check_highlight_update_api(base, args.paper)   # 纯 HTTP：浮层色块要用的改色接口
    check_brand_assets(base)                       # 纯 HTTP：站点图标能取到、路径对

    # ---- ③ 划词 → 加入对话 → 上下文胶囊（免费：不发消息） ----
    print("\n③ 划词 → 加入对话（输入框里的上下文胶囊，不发消息）")
    sel = run_probe(browser, base, "probe_selchat",
                    PROBE_SELCHAT.replace("__URL__", chat_url).replace("__TITLE__", "selchat"),
                    args.budget)
    if sel:
        if sel.get("menuOpen") is not True:
            skip("划词 → 加入对话", f"造不出选区或菜单没出来（picked={sel.get('picked')}）")
        else:
            record("reader", "胶囊只带图标（名称在 aria-label 上），且**右键时胶囊还在**",
                   sel.get("menuIconOnly") is True and sel.get("pillOnRightClick") is True,
                   f"按钮名={sel.get('menuBtns')}")
            record("reader", "「加入对话」已从胶囊移走，改由**右键菜单**的「添加到对话框」承担",
                   sel.get("ctxHasToChat") is True and sel.get("hasAdd") is True,
                   f"右键菜单里有 tochat={sel.get('ctxHasToChat')}")
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
    check_tabs(browser, base, max(args.budget, 60000))


    # ---- ⑤ 提示条 toast（免费） ----
    print("\n⑤ 提示条：右上角通知卡 / 标题 + 正文 + 关闭钮 / 无左侧色条")
    tst = run_probe(browser, base, "probe_toast",
                    PROBE_TOAST.replace("__URL__", f"{base}/cylit/settings").replace("__TITLE__", "toast"),
                    args.budget)
    if tst:
        if tst.get("toastCount"):
            host = tst.get("host") or {}
            toast = tst.get("toast") or {}
            # 距右取一段区间：量的时候卡片可能还在**滑入**动画里（translateX(28px)），
            # 所以上界放到 48px；真正的位置由宿主的 right 决定。
            record("toast", "提示条在**右上角**（固定定位 + 贴右边缘 + 整列不吃点击、卡片自己吃）",
                   host.get("pos") == "fixed" and host.get("pe") == "none"
                   and host.get("right") == "18px"
                   and toast.get("pe") == "auto"
                   and -8 <= (toast.get("rightGap") or 99) <= 48
                   and 40 <= (toast.get("top") or 0) <= 110,
                   f"{host.get('pos')} right={host.get('right')} 宿主 pe={host.get('pe')} "
                   f"卡片 pe={toast.get('pe')} 距右 {toast.get('rightGap')}px top={toast.get('top')}")
            record("toast", "卡片式通知：图标 + 标题 + 正文 + 右上角关闭钮（圆角比胶囊档更大）",
                   toast.get("radius") == "30px" and toast.get("padding") == "13px 13px 14px 14px"
                   and bool(tst.get("title")) and tst.get("hasBody") is True
                   and tst.get("hasClose") is True,
                   f"r={toast.get('radius')} pad={toast.get('padding')} 标题={tst.get('title')!r} "
                   f"正文={tst.get('hasBody')} 关闭={tst.get('hasClose')}")
            record("toast", "配色走主题令牌：四周同一圈边框（不再有左侧色条），级别只由图标颜色表达",
                   toast.get("borderLeft") == toast.get("borderRight")
                   and toast.get("borderLeftColor") == toast.get("borderRightColor")
                   and bool(tst.get("iconColor")),
                   f"左 {toast.get('borderLeft')}/{toast.get('borderLeftColor')} "
                   f"右 {toast.get('borderRight')}/{toast.get('borderRightColor')} "
                   f"图标 {tst.get('iconColor')}（{tst.get('text')}）")
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
                #: 只攒了旁白的一轮（模型没思考也没调工具）现在**没有**步骤行 —— 那一步不进轨迹了，
                #: 所以这里只要求抬头，步骤数只当读数。
                record("model", "过程轨迹：抬头写「已完成，用时 …」（旁白不进轨迹，可能一步都没有）",
                       "已完成" in (stream.get("runHead") or ""),
                       f"{(stream.get('runHead') or '').strip()[:40]!r}，"
                       f"{stream.get('runSteps')} 步")
            else:
                skip("过程轨迹", "这一轮没有轨迹（模型没调工具也没有思考模式）")
            if stream.get("thinkCollapsed") is not None:
                record("model", "思考段：默认收成一行摘要、点一下展开全文",
                       stream.get("thinkCollapsed") is True and stream.get("thinkOpen") is True
                       and (stream.get("thinkBodyLen") or 0) > 0,
                       f"默认折叠={stream.get('thinkCollapsed')} → open={stream.get('thinkOpen')}"
                       f"，正文 {stream.get('thinkBodyLen')} 字")
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
