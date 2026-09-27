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
    # 应用外壳：顶栏是**标签条**（不再是主导航）；导航入口在首页
    # `tab active pinned` = 常驻的「首页」标签确实"当前页 + 常驻"（它不铺填充胶囊，见 TabBar）；
    # 反过来在别的页面上这个组合**必须不存在**（否则就是"首页一直亮着"那个回归）。
    {"name": "home", "path": "/home",
     "markers": ["tab-strip", "nav-card", "继续阅读", "tab active pinned"]},
    {"name": "library", "path": "/library",
     "markers": ["论文库", "导入 PDF", "tab-strip"], "absent": ["tab active pinned"]},
    {"name": "reader", "path": "/reader/{paper}",
     "markers": ["reader-toolbar", "side-tabs", "对照", "目录"]},
    {"name": "reader-公式页签", "path": "/reader/{paper}?side=formulas",
     "markers": ["fl-bar", "行间", "katex"]},
    # 术语页签默认**只读**（用户口径：仅在编辑时才显示编辑框）。编辑态的四个输入框由 `v-if` 控制，
    # 不在 DOM 里，所以这里用"编辑态独有文案"做负向断言 —— 一旦有人把 v-if 改成 v-show 就会红。
    {"name": "reader-术语只读", "path": "/reader/{paper}?side=terms",
     "markers": ["brief-bar", "brief-k", "文档简报", "编辑"],
     "absent": ["术语表（每行", "保持英文不译（每行", "要点（每行一条"]},
    # 回归：多行 `$$\begin{aligned}…$$`（定界符与内容同行）曾经整块掉成段落、`_` 变 <em>，
    # 断言公式渲染成 KaTeX、且页面里不再残留 `$$`
    {"name": "reader-多行公式", "path": "/reader/{paper}?view=bilingual&left=text&row=p019-08",
     "markers": ["katex-display"], "absent": ["$$"]},
    {"name": "reader-块对齐", "path": "/reader/{paper}?view=bilingual&left=text",
     "markers": ["row-pair", "two-col", "rr-body", "side-zh"]},
    # 「原 PDF」栏必须是**真实 PDF**：pdf.js 渲染的矢量页 + 文字层（可选中/复制）。
    # 回归：这里以前是 `<img src="…/pages/N">` 位图，读者选不中文字。
    {"name": "reader-原PDF在左", "path": "/reader/{paper}?view=bilingual&left=page",
     "markers": ["pg-pair", "col-image", "pdf-text-layer", "canvas"]},
    # 扫描件反过来：PDF 里没有文字层 → 必须继续用后端位图页图（不白跑 pdf.js）
    {"name": "reader-原PDF(扫描件)", "path": "/reader/{scan}?view=bilingual&left=page",
     "markers": ["pg-pair", "col-image", "/pages/"]},
    {"name": "reader-深链定位", "path": "/reader/{paper}?block={block}",
     "markers": ["data-row-id", "rrow"]},
    {"name": "reader-窄屏退化", "path": "/reader/{paper}?view=bilingual", "size": NARROW,
     "markers": ["row-pair"], "absent": []},
    # 搜索：`?q=` 深链直接跑一遍，结果落在侧栏**独立的「搜索」页签**里（命中条目 + 命中词高亮 +
    # 清空按钮）；工具栏不再有搜索框（负向断言）。命中条数由页签角标表示，面板里不再写状态文案。
    # `class="srch"` = 正文里**所有**命中都打了高亮标记（不只是当前那一条）。
    {"name": "reader-搜索", "path": "/reader/{paper}?view=bilingual&q=attention",
     "markers": ["side-search", "ss-hit", "ss-snippet", "清空搜索", "class=\"srch\""],
     "absent": ["search-compact"]},
    # Markdown 视图（`?md=1` 深链直接打开）：工具行 + 正文 +「下载<当前版本>」+ 右上角「全屏」。
    # `data-md-page="3"` = **整篇一次性渲染**（不再翻页，所以"上一页/下一页"必须不存在）。
    {"name": "reader-Markdown视图", "path": "/reader/{paper}?md=1",
     "markers": ["md-doc", "md-bar", "data-md-page=\"3\"", "下载中英对照 Markdown", "全屏"],
     "absent": ["title=\"上一页\"", "title=\"下一页\""]},
    {"name": "knowledge", "path": "/knowledge", "markers": ["知识库", "chunk"]},
    {"name": "notes", "path": "/notes", "markers": ["笔记", "高亮", "批注"]},
    {"name": "models", "path": "/models", "markers": ["模型", "类型"]},
    {"name": "tasks", "path": "/tasks", "markers": ["任务中心", "队列"]},
    {"name": "settings", "path": "/settings", "markers": ["设置", "渲染"]},
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
  // 侧栏有两种收起形态：窄屏是**抽屉**（靠 transform 移出屏幕），宽屏是**宽度收 0**。
  // 两种都不是“没渲染”（aside 一直在 DOM 里，否则做不了过渡），所以判据改成“看不见就点开”。
  const asideRect = (() => { const a = d.querySelector('.reader-aside'); return a ? a.getBoundingClientRect() : null; })();
  const asideOpen = !!asideRect && asideRect.width > 40 && asideRect.right > 0;
  if (!asideOpen) {
    d.querySelector('[data-aside-toggle]')?.click();
    // 开合动画 240ms，留足时间再断言
    setTimeout(() => { collectOverflow(); report(true); }, 700);
  } else {
    collectOverflow();
    report(false);
  }
}, __WAIT__));
</script>
"""


# 网格卡片几何探针：首页的"圆角矩形"必须一样大。
# 曾经 `.card + .card { margin-top: 12px }`（本是给竖向堆叠留间距的）在 `.grid` 里也生效，
# 于是每排第一张卡（前面没有 `.card` 兄弟）被拉伸得比同排高 12px、底部两张卡一高一低。
# 分组用"竖直重叠"而不是"top 相等"：被 margin 顶下去的卡片会另起一行，用它分行就抓不到这个 bug。
GRID_PROBE = """<!doctype html>
<meta charset="utf-8"><title>grid probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:520px;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const f = document.getElementById('f');
f.addEventListener('load', () => setTimeout(() => {
  const d = f.contentDocument;
  const grids = [...d.querySelectorAll('.grid')].map((g) => {
    const cards = [...g.children].filter((c) => c.classList && c.classList.contains('card'));
    const boxes = cards.map((c) => {
      const b = c.getBoundingClientRect();
      return { w: Math.round(b.width), h: Math.round(b.height),
               top: Math.round(b.top), bottom: Math.round(b.bottom) };
    }).sort((a, b) => a.top - b.top);
    const rows = [];
    boxes.forEach((b) => {
      const row = rows[rows.length - 1];
      if (row && b.top < row.bottom - 1) {
        row.cards.push(b); row.bottom = Math.max(row.bottom, b.bottom);
      } else {
        rows.push({ top: b.top, bottom: b.bottom, cards: [b] });
      }
    });
    return {
      cls: String(g.className),
      uniform: g.classList.contains('nav-grid'),   // 这 6 张导航卡结构一致 → 跨排也必须等高
      rows: rows.map((r) => ({ top: r.top, cards: r.cards.map((c) => ({ w: c.w, h: c.h })) })),
    };
  });
  document.getElementById('out').textContent = JSON.stringify(grids);
}, __WAIT__));
</script>
"""


# 多标签页切换探针：两个论文标签之间来回点。
#
# 回归点：`RouterView` 曾经没有 `:key`，vue-router 会复用同一个阅读器实例 ——
# 实例里的数据是挂载那篇论文的，而 `paperId` 读的是"当前路由"，于是切到另一篇的标签后
# 要么内容不变、要么被轮询进来的另一篇页行**串台**（实测行数 123 → 266 → 211，越切越乱）。
# 判据用"每个标签每次的正文行数必须一致"，它对串台极敏感，且不依赖任何文案。
TABS_PROBE = """<!doctype html>
<meta charset="utf-8"><title>tabs probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:640px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="about:blank"></iframe>
<pre id="out">waiting</pre>
<script>
const A = '__A__', B = '__B__';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
// 预置两个论文标签（同源 localStorage）：这样不必绕论文库，也不会因为点标签丢掉
// nosse=1（标签会记住自己的 query）而让 headless 被长连接卡住。
const mk = (id) => ({
  key: 'reader:paperId=' + id, name: 'reader', params: { paperId: id },
  query: { nosse: '1' }, title: 'TAB-' + id.slice(0, 6), icon: 'document',
});
localStorage.setItem('pm.shell.tabs', JSON.stringify([
  { key: 'home', name: 'home', params: {}, query: {}, title: '首页', icon: 'home' },
  mk(B), mk(A),
]));
localStorage.setItem('pm.shell.tabs.active', 'reader:paperId=' + A);

const snap = (w, d) => {
  const rows = [...d.querySelectorAll('[data-row-id]')];
  const btn = d.querySelector('.btn');
  return {
    path: w.location.pathname.replace('/app', ''),
    rows: rows.length,
    text: (rows[0] ? rows[0].textContent : '').replace(/\\s+/g, ' ').trim().slice(0, 40),
    // 触摸高亮：浏览器给可点元素叠的原生灰色遮罩（Chrome 默认 rgba(0,0,0,0.18)）
    tap: btn ? w.getComputedStyle(btn).webkitTapHighlightColor : '',
    tapBody: w.getComputedStyle(d.body).webkitTapHighlightColor,
  };
};

let started = false;
f.addEventListener('load', async () => {
  if (started) return;
  started = true;
  const w = f.contentWindow, d = f.contentDocument;
  const res = {};
  const clickTab = (id) => {
    const el = [...d.querySelectorAll('a.tab')]
      .find((t) => (t.getAttribute('href') || '').includes(id));
    if (el) el.click();
    return !!el;
  };
  const clickText = (txt) => {
    const b = [...d.querySelectorAll('.reader-toolbar button')]
      .find((x) => x.textContent.trim() === txt);
    if (b) b.click();
    return !!b;
  };
  const clickTitle = (t) => {
    const b = [...d.querySelectorAll('.reader-toolbar button')].find((x) => x.title === t);
    if (b) b.click();
    return !!b;
  };
  /** "这篇论文现在是怎么看的"：URL query + 版面（原 PDF 在左 / 译文在左） */
  const viewState = () => ({
    search: w.location.search,
    pgPair: !!d.querySelector('.pg-pair'),
    imageRight: !!d.querySelector('.pg-pair.image-right'),
    colImage: !!d.querySelector('.col-image'),
    active: [...d.querySelectorAll('.seg button.active')]
      .map((b) => b.textContent.trim() || b.title),
  });
  try {
    await sleep(__WAIT__);
    res.a = snap(w, d);
    // 在 A 上设成「原 PDF + 译文在左」：先切到另一套状态，保证下面两次点击真的改变状态
    clickText('原文'); await sleep(320);
    clickText('原文文本'); await sleep(320);
    clickText('对照'); await sleep(320);
    clickTitle('原文在左（译文在右）'); await sleep(420);
    clickText('原 PDF'); await sleep(420);
    clickTitle('译文在左（原文在右）'); await sleep(700);
    res.viewA1 = viewState();
    res.clickedB = clickTab(B);
    await sleep(__WAIT__);
    res.b = snap(w, d);
    res.clickedA = clickTab(A);
    await sleep(__WAIT__);
    res.aAgain = snap(w, d);
    res.viewA2 = viewState();          // 切回来：上次的打开状态必须还在
    res.clickedB2 = clickTab(B);
    await sleep(__WAIT__);
    res.bAgain = snap(w, d);
    out(res);
  } catch (err) { res.error = String(err); out(res); }
});
f.src = location.origin + '/app/reader/' + A + '?nosse=1';
</script>
"""


# 插图放大探针：点开正文里的第一张插图，看弹窗**标题**。
#
# 回归点：标题以前写死成"插图"，看大图时不知道在看哪一张（用户口径：应显示图片标题）。
# 图注本来就在 Markdown 产物里（`media.caption`，形如 `Figure 3: …`），标题必须来自它；
# 图注可能很长，所以还要断言"标题被一行截断、关闭按钮没被顶出弹窗"。
ZOOM_PROBE = """<!doctype html>
<meta charset="utf-8"><title>zoom probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:620px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
f.addEventListener('load', async () => {
  const w = f.contentWindow, d = f.contentDocument;
  const res = {};
  try {
    await sleep(__WAIT__);
    const fig = d.querySelector('.row-figure');
    res.hasFigure = !!fig;
    res.inlineTitle = (fig?.getAttribute('title') || '').slice(0, 40);
    fig.click();
    await sleep(600);
    const modal = [...d.querySelectorAll('.modal')].find((x) => x.querySelector('.modal-title'));
    const titleEl = modal.querySelector('.modal-title');
    const head = modal.querySelector('.modal-head');
    const closeBtn = [...head.querySelectorAll('button')].find((b) => b.getAttribute('title') === '关闭');
    // ① 标题本身：必须来自图注（不是写死的"插图"）
    res.caption = (titleEl.textContent || '').trim();
    res.tooltip = titleEl.getAttribute('title') || '';
    res.hasImage = !!modal.querySelector('img');
    // ② 超长图注（用户截图那种一句话几十上百字）：一行截断，且不把关闭按钮顶出弹窗
    const long = 'Figure 9: ' + 'long caption text '.repeat(24);
    titleEl.textContent = long;
    await sleep(200);
    const mb = modal.getBoundingClientRect();
    res.truncated = titleEl.scrollWidth > titleEl.clientWidth + 1;
    res.textOverflow = w.getComputedStyle(titleEl).textOverflow;
    res.headFits = head.scrollWidth <= head.clientWidth + 1;
    res.closeInside = closeBtn.getBoundingClientRect().right <= mb.right + 1;
    out(res);
  } catch (err) { res.error = String(err); out(res); }
});
</script>
"""


# 高亮分侧探针：在同一行的**原文**与**译文**上各标一条，断言两边互不影响。
#
# 回归点两个：
# 1. 一行对照的两侧共用同一个 `block_id`，只按 block_id 过滤的话原文的高亮会按同样的偏移量
#    画到译文上（`highlight.side` 就是为它加的）；
# 2. **点色块必须走真实的鼠标时序**（mousedown → 浏览器默认动作清选区 → mouseup → click）：
#    以前只写 `@mousedown.stop`，于是 mouseup 时选区已被清掉，`onMouseUp` 把菜单卸载在自己的
#    click 之前 → 点高亮毫无反应。这里显式模拟"默认动作把选区清掉"，并断言菜单没被卸载。
#    （只 `swatch.click()` 的合成事件不会触发默认动作，所以那种写法测不出这个 bug。）
# 探针自己造的高亮在 §⑦ 结尾由 Python 侧删掉（不留垃圾数据）。
HL_PROBE = """<!doctype html>
<meta charset="utf-8"><title>highlight probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:620px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
f.addEventListener('load', async () => {
  const w = f.contentWindow, d = f.contentDocument;
  const res = {};
  const flush = () => out(res);
  /* ⚠️ 每次派发合成鼠标事件前**先把焦点抢回 iframe**：
   * app 在 `window blur` 时会收起划词菜单（真人独立窗口里没问题），而 headless 跑在 iframe 里时
   * 外层页面会随机把焦点拿走 —— 不抢回来的话，菜单会在"选中之后、读菜单之前"凭空消失，
   * 表现为随机失败（实测：给 iframe 派发一次 blur，菜单立刻没了；不 blur 就一定在）。
   * 这属于**测试环境**问题，不是产品行为，所以修在探针里。 */
  const keepFocus = () => { try { w.focus(); } catch (e) { /* 忽略 */ } };
  const ev = (type, rect) => {
    keepFocus();
    return new w.MouseEvent(type, {
      bubbles: true, cancelable: true, clientX: rect.left + 3, clientY: rect.top + 3 });
  };
  const selectInRow = (side, chars) => {
    const bodies = [...d.querySelectorAll('[data-row-id] .rrow.side-' + side + ' .rr-body')];
    for (const body of bodies) {
      const walker = d.createTreeWalker(body, NodeFilter.SHOW_TEXT);
      let node = walker.nextNode();
      while (node) {
        const raw = node.nodeValue || '';
        const at = raw.search(/\\S/);
        if (at >= 0 && raw.length - at > chars + 4) {
          const range = d.createRange();
          range.setStart(node, at);
          range.setEnd(node, at + chars);
          const sel = w.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          const rect = range.getBoundingClientRect();
          node.parentElement.dispatchEvent(ev('mouseup', rect));
          return { row: body.closest('[data-row-id]')?.getAttribute('data-row-id'), side, rect };
        }
        node = walker.nextNode();
      }
    }
    return null;
  };
  /** 真实鼠标点菜单：mousedown → （浏览器默认动作）清选区 → mouseup → click */
  const clickSwatch = async (cls, rect) => {
    const menu = d.querySelector('.selection-menu');
    const swatch = menu && menu.querySelector('.swatch.' + cls);
    if (!swatch) return { ok: false, reason: menu
      ? ('菜单在、但没有 ' + cls + ' 色块（内容：' + (menu.textContent || '').trim().slice(0, 40) + '）')
      : '菜单没出来' };
    swatch.dispatchEvent(ev('mousedown', rect));
    w.getSelection().collapseToEnd();          // 模拟默认动作：点菜单会清掉页面选区
    // mouseup 必须打在**色块自己**身上（真实鼠标就是这样，事件再冒泡到 document）：
    // 打在 document 上会让 `ev.target` 变成 document，跟真实点击不是一回事
    swatch.dispatchEvent(ev('mouseup', rect));
    await sleep(200);
    const survived = swatch.isConnected;
    if (!survived) {
      // 真实浏览器不会把 click 交给已被卸载的元素（target 会变成最近的共同祖先）
      return { ok: false, reason: '菜单在 click 之前被卸载（点高亮会毫无反应）' };
    }
    swatch.dispatchEvent(ev('click', rect));
    await sleep(1000);
    return { ok: true };
  };
  /* 只认**这一轮探针自己造的**标记：库里本来就有别的用户高亮时，按"总数"断言会误判
   * （踩过：上一次失败的运行留下两条，导致"原文侧只画原文"变成 2 条）。 */
  const seen = new Set([...d.querySelectorAll('mark.hl[data-hl]')]
    .map((m) => m.getAttribute('data-hl')));
  const fresh = () => {
    const out = [...d.querySelectorAll('mark.hl[data-hl]')]
      .map((m) => m.getAttribute('data-hl')).filter((i) => i && !seen.has(i));
    out.forEach((i) => seen.add(i));       // 认领掉，下一次 fresh() 只报**再新**的
    return out;
  };
  const countIn = (row, side, ids) => [...d.querySelectorAll(
    '[data-row-id="' + row + '"] .rrow.side-' + side + ' mark.hl[data-hl]')]
    .filter((m) => ids.includes(m.getAttribute('data-hl'))).length;
  /** 选中某个元素（可能是含公式的一整段）→ 弹菜单 → 点色块 */
  const selectElement = async (el, cls) => {    const range = d.createRange();
    range.selectNodeContents(el);
    const sel = w.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    const rect = el.getBoundingClientRect();
    el.dispatchEvent(ev('mouseup', rect));
    await sleep(450);
    const menu = d.querySelector('.selection-menu');
    if (!menu) return { ok: false, reason: '菜单没出来' };
    const swatch = menu.querySelector('.swatch.' + cls);
    if (!swatch) return { ok: false, reason: '没有该色块' };
    swatch.dispatchEvent(ev('mousedown', rect));
    w.getSelection().collapseToEnd();
    swatch.dispatchEvent(ev('mouseup', rect));
    await sleep(200);
    if (!swatch.isConnected) return { ok: false, reason: '菜单在 click 前被卸载' };
    swatch.dispatchEvent(ev('click', rect));
    await sleep(1100);
    return { ok: true };
  };
  /** 点一个已有高亮 → 菜单应出现「删除高亮」→ 点它 → 返回删前后条数 */
  const deleteViaMenu = async (mark) => {
    const rect = mark.getBoundingClientRect();
    mark.dispatchEvent(new w.MouseEvent('click', {
      bubbles: true, cancelable: true, clientX: rect.left + 2, clientY: rect.top + 2 }));
    await sleep(400);
    const menu = d.querySelector('.selection-menu');
    const buttons = menu ? [...menu.querySelectorAll('button')]
      .map((b) => b.textContent.trim()).filter(Boolean) : [];
    const ban = menu && menu.querySelector('button.danger');
    const before = d.querySelectorAll('mark.hl[data-hl]').length;
    if (!ban) return { buttons, before, after: before, ok: false };
    ban.dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));
    await sleep(1300);
    return { buttons, before, after: d.querySelectorAll('mark.hl[data-hl]').length, ok: true,
             banIcon: !!ban.querySelector('svg') };
  };
  try {
    await sleep(__WAIT__);
    const a = selectInRow('en', 10);
    res.pickEn = a && { row: a.row, side: a.side };
    await sleep(400);
    let menu = d.querySelector('.selection-menu');
    res.menuHasNote = !!(menu && [...menu.querySelectorAll('button')]
      .find((b) => b.textContent.includes('笔记')));
    res.clickEn = await clickSwatch('hl-green', a.rect);
    res.newEn = fresh();
    res.afterEn = { en: countIn(a.row, 'en', res.newEn), zh: countIn(a.row, 'zh', res.newEn) };
    const b = selectInRow('zh', 8);
    res.pickZh = b && { row: b.row, side: b.side };
    await sleep(400);
    res.clickZh = await clickSwatch('hl-blue', b.rect);
    res.newZh = fresh();
    // "两侧各自独立"看的是**这一刻**该行的全貌（这一轮自己造的两条都在）
    res.afterZh = { en: countIn(a.row, 'en', [...res.newEn, ...res.newZh]),
                    zh: countIn(a.row, 'zh', [...res.newEn, ...res.newZh]) };
    res.sameRow = a.row === b.row;
    res.ids = {
      en: res.newEn[0], zh: res.newZh[0],
    };
    res.colors = {
      en: (() => { const m = res.newEn[0]
        ? d.querySelector('mark.hl[data-hl="' + res.newEn[0] + '"]') : null;
        return m ? w.getComputedStyle(m).backgroundColor : ''; })(),
      zh: (() => { const m = res.newZh[0]
        ? d.querySelector('mark.hl[data-hl="' + res.newZh[0] + '"]') : null;
        return m ? w.getComputedStyle(m).backgroundColor : ''; })(),
    };
    flush();

    // ③ 公式（含跨行的 aligned）：必须整块被 mark 包住，KaTeX 结构不坏
    const fRow = [...d.querySelectorAll('[data-row-id]')]
      .find((r) => r.querySelector('.rr-body .katex'));
    res.hasFormulaRow = !!fRow;
    if (fRow) {
      const body = fRow.querySelector('.rr-body');
      res.formulaClick = await selectElement(body, 'hl-yellow');
      res.formula = {
        wrapped: !!fRow.querySelector('mark.hl > .katex, mark.hl > .katex-display'),
        katexIntact: !!fRow.querySelector('.katex'),
        marks: fRow.querySelectorAll('mark.hl').length,
      };
      res.formulaIds = [...fRow.querySelectorAll('mark.hl[data-hl]')]
        .map((m) => m.getAttribute('data-hl'));
    }
    flush();

    // ③b 多行**显示公式**：底色必须覆盖整块。
    //     回归点：`mark` 以前套在内层行内 `.katex` 上，而 `.katex-display` 是块级 ——
    //     浏览器只把底色画在内层那一条行高上，三行公式看起来"只有中间一行被标黄"（用户报过）。
    const dispEl = [...d.querySelectorAll('[data-row-id] .rr-body .katex-display')]
      .find((k) => k.getBoundingClientRect().height > 40);
    res.hasDisp = !!dispEl;
    if (dispEl) {
      const dRow = dispEl.closest('[data-row-id]');
      const before = new Set([...d.querySelectorAll('mark.hl[data-hl]')]
        .map((m) => m.getAttribute('data-hl')));
      res.dispClick = await selectElement(dispEl, 'hl-green');
      const mk = [...dRow.querySelectorAll('mark.hl')].find((m) => m.classList.contains('block'));
      const dh = dispEl.getBoundingClientRect().height;
      res.disp = {
        hasBlockClass: !!mk,
        markH: mk ? Math.round(mk.getBoundingClientRect().height) : 0,
        dispH: Math.round(dh),
        covers: !!mk && mk.getBoundingClientRect().height >= dh * 0.8,
        wrapped: !!dRow.querySelector('mark.hl > .katex-display'),
        katexIntact: !!dRow.querySelector('.katex'),
        ids: [...dRow.querySelectorAll('mark.hl[data-hl]')]
          .map((m) => m.getAttribute('data-hl')).filter((i) => i && !before.has(i)),
      };
      res.formulaIds = [...(res.formulaIds || []), ...res.disp.ids];
    }
    flush();

    // ④ 跨行选择：同一侧的相邻两行各选一半 → 两条记录共用一个分组
    //    → 点其中一条，从菜单删除，两条必须一起消失
    const textEnds = (el) => {
      const walker = d.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      let n = walker.nextNode(), first = null, last = null;
      while (n) {
        // 只取**有实际文字**的节点：末尾的纯空白节点不在偏移量坐标系里（选它等于没选）
        if ((n.nodeValue || '').trim()) { if (!first) first = n; last = n; }
        n = walker.nextNode();
      }
      return { first, last };
    };
    /** 节点属于哪一"列"：pdf / rows:en / rows:zh（与 ReaderView 的 columnOf 同一口径） */
    const colOf = (node) => {
      const el = (node && node.nodeType === 3 ? node.parentElement : node);
      if (!el?.closest) return '';
      if (el.closest('[data-pdf-page]')) return 'pdf';
      const side = el.closest('[data-side]');
      return side ? 'rows:' + side.getAttribute('data-side') : 'rows';
    };
    const bodies = [...d.querySelectorAll('[data-row-id] .rrow.side-zh .rr-body')]
      .filter((x) => (x.textContent || '').trim().length > 40).slice(0, 2);
    res.hasTwoRows = bodies.length === 2;
    if (bodies.length === 2) {
      const a1 = textEnds(bodies[0]), a2 = textEnds(bodies[1]);
      const range = d.createRange();
      range.setStart(a1.last, Math.max(0, (a1.last.nodeValue || '').length - 6));
      range.setEnd(a2.first, Math.min(6, (a2.first.nodeValue || '').length));
      const sel = w.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      res.crossPicked = bodies.map((b) => b.closest('[data-row-id]')?.getAttribute('data-row-id'));
      res.crossSelText = (sel.toString() || '').slice(0, 26);
      const idsBefore = new Set([...d.querySelectorAll('mark.hl[data-hl]')]
        .map((m) => m.getAttribute('data-hl')));
      const picked = res.crossPicked || [];
      const rect = range.getBoundingClientRect();
      a1.last.parentElement.dispatchEvent(ev('mouseup', rect));
      await sleep(550);
      const menu2 = d.querySelector('.selection-menu');
      const sw = menu2 && menu2.querySelector('.swatch.hl-pink');
      res.crossMenu = menu2
        ? [...menu2.querySelectorAll('button')].map((b) => b.textContent.trim()).filter(Boolean) : null;
      if (sw) {
        sw.dispatchEvent(ev('mousedown', rect));
        w.getSelection().collapseToEnd();
        sw.dispatchEvent(ev('mouseup', rect));
        await sleep(200);
        if (sw.isConnected) sw.dispatchEvent(ev('click', rect));
        await sleep(1400);
      }
      // 这次跨行高亮**新造出来的**那几条（按前后差集认，不受前面几步的高亮干扰）
      const newIds = [...d.querySelectorAll('mark.hl[data-hl]')]
        .map((m) => m.getAttribute('data-hl')).filter((i) => i && !idsBefore.has(i));
      const rowEls = [...d.querySelectorAll('[data-row-id]')]
        .filter((r) => picked.includes(r.getAttribute('data-row-id'))
          && [...r.querySelectorAll('.rrow.side-zh mark.hl[data-hl]')]
            .some((m) => newIds.includes(m.getAttribute('data-hl'))));
      res.crossRow = {
        rowsWithMarks: rowEls.length,
        rowIds: rowEls.map((r) => r.getAttribute('data-row-id')),
        newIds,
      };
      const target = newIds.length
        ? d.querySelector('mark.hl[data-hl="' + newIds[0] + '"]') : null;
      if (target) res.crossDelete = await deleteViaMenu(target);
      res.crossRowAfterDelete = newIds.filter((i) => d.querySelector('mark.hl[data-hl="' + i + '"]')).length;
    }

    // ⑤ 跨列选区必须被夹回本列，而且必须**在拖拽那个任务里**就夹掉：
    //    只靠异步派发的 `selectionchange`，浏览器可能已经画过一帧跨列底色
    //    （用户看到的就是"选区还是不独立"）。这里派发一次**真实 mousemove（按住左键）**，
    //    然后**立刻**读选区 —— 中间不 await，测的就是"同一个任务里有没有夹"。
    const enBody = d.querySelector('[data-row-id] .rrow.side-en .rr-body');
    const zhBody = d.querySelector('[data-row-id] .rrow.side-zh .rr-body');
    if (enBody && zhBody) {
      const t1 = textEnds(enBody).last, t2 = textEnds(zhBody).first;
      if (t1 && t2) {
        enBody.scrollIntoView({ block: 'center' });
        await sleep(80);
        const tail = Math.max(0, (t1.nodeValue || '').length - 6);
        const range = d.createRange();
        range.setStart(t1, tail);
        range.setEnd(t2, Math.min(6, (t2.nodeValue || '').length));
        const sel = w.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        const snap = () => ({ anchor: colOf(sel.anchorNode), focus: colOf(sel.focusNode),
                              text: (sel.toString() || '').slice(0, 24) });
        res.crossColumnRaw = snap();
        // 鼠标放在**锚点那一行的高度**、横向落在译文列里：真实用户拖到对面列就是这样
        const mark = d.createRange();
        mark.setStart(t1, tail);
        mark.setEnd(t1, Math.min(tail + 4, (t1.nodeValue || '').length));
        const mr = mark.getBoundingClientRect();
        const zb = zhBody.getBoundingClientRect();
        const px = Math.round(zb.left + 20);
        const py = Math.round(mr.top + mr.height / 2);
        zhBody.dispatchEvent(new w.MouseEvent('mousemove', {
          bubbles: true, cancelable: true, buttons: 1, clientX: px, clientY: py }));
        res.crossColumnSync = snap();
        await sleep(400);                       // 再看异步那一路有没有漏
        res.crossColumn = snap();
      }
    }
    // ⑥ 纵向拖多行时，选区**必然**从对面列的单元格之间穿过（两列在 DOM 里交错）：
    //    这时"顺带被圈进来"的对面列高亮**不能**算作选中（菜单里的「删除高亮」只该删本列的）。
    const twoCol = [...d.querySelectorAll('[data-row-id]')].filter((r) =>
      r.querySelector('.rrow.side-en .rr-body') && r.querySelector('.rrow.side-zh .rr-body')).slice(0, 3);
    res.has3Rows = twoCol.length === 3;
    if (twoCol.length === 3) {
      // 中间那一行的**原文**上先造一条高亮（粉色；可选色只有 yellow/green/blue/pink）
      const midEn = twoCol[1].querySelector('.rrow.side-en .rr-body');
      const mt = textEnds(midEn).first;
      const mrange = d.createRange();
      mrange.setStart(mt, 0);
      mrange.setEnd(mt, Math.min(10, (mt.nodeValue || '').length));
      const msel = w.getSelection();
      msel.removeAllRanges();
      msel.addRange(mrange);
      const mrect = mrange.getBoundingClientRect();
      /* ⚠️ 先把目标行滚进视口并**等滚动停稳**：app 收到任何 scroll 都会收起划词菜单
       * （滚动时收起是对的），而"选区在视口外"会让浏览器在 mouseup 时自动滚一下 →
       * 菜单在渲染前就被收起（实测：菜单采样全 false、同一任务里出现一次 scroll）。 */
      try { midEn.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch (e) { /* 忽略 */ }
      await sleep(380);
      midEn.dispatchEvent(ev('mouseup', mrect));
      await sleep(420);
      res.intraClick = await clickSwatch('hl-pink', mrect);
      const otherIds = [...twoCol[1].querySelectorAll('.rrow.side-en mark.hl[data-hl]')]
        .map((m) => m.getAttribute('data-hl'));
      // 再在**译文列**里从第一行拖到最后一行（选区把中间那些原文单元格圈在中间）
      const z0 = textEnds(twoCol[0].querySelector('.rrow.side-zh .rr-body')).first;
      const z2 = textEnds(twoCol[2].querySelector('.rrow.side-zh .rr-body')).last;
      const zr = d.createRange();
      zr.setStart(z0, 0);
      zr.setEnd(z2, (z2.nodeValue || '').length);
      const zsel = w.getSelection();
      zsel.removeAllRanges();
      zsel.addRange(zr);
      const zrect = zr.getBoundingClientRect();
      const zBody = twoCol[0].querySelector('.rrow.side-zh .rr-body');
      zBody.dispatchEvent(ev('mouseup', zrect));
      await sleep(500);
      const m3 = d.querySelector('.selection-menu');
      const ban3 = m3 && m3.querySelector('button.danger');
      const m3rect = m3 ? m3.getBoundingClientRect() : null;
      const zcellRect = twoCol[0].querySelector('.cell-zh').getBoundingClientRect();
      res.intra = { rows: twoCol.map((r) => r.getAttribute('data-row-id')),
                    hasDelete: !!ban3, otherIds,
                    fixture: !!(res.intraClick || {}).ok,
                    /* 菜单位置必须落在**本列**（以前拿整段选区的 bbox 定位，纵向跨行时
                     * bbox 的左边界是对面列 → 菜单跑到左边头上，用户报过"胶囊菜单在左侧"） */
                    menuX: m3rect ? Math.round(m3rect.left) : null,
                    zhLeft: Math.round(zcellRect.left),
                    menuInColumn: !!m3rect && m3rect.left >= zcellRect.left - 4 };
      /* 松手之后"选区在哪一列"的标记必须**留着**：一摘掉，浏览器立刻把整段连续选区
       * （中间那些原文单元格）重新涂上底色 —— 用户看到的就是"拖拽时不会到左侧，一松手回到左侧"。 */
      res.colFlag = {
        afterUp: { cls: d.documentElement.classList.contains('pm-col-sel'),
                   col: d.documentElement.getAttribute('data-pm-col') || '' },
      };
      if (ban3) {
        ban3.dispatchEvent(ev('mousedown', zrect));
        w.getSelection().collapseToEnd();
        ban3.dispatchEvent(ev('mouseup', zrect));
        await sleep(150);
        if (ban3.isConnected) ban3.dispatchEvent(ev('click', zrect));
        await sleep(1400);
      }
      // 对面列那条粉色高亮必须**活着**（说明删除只按本列过滤）
      res.intraEnAlive = otherIds.length > 0
        && otherIds.every((i) => !!d.querySelector('mark.hl[data-hl="' + i + '"]'));
      // 选区一清掉，标记就得摘掉（不然对面列会因为 ::selection 透明而"看着选不中"）
      w.getSelection().removeAllRanges();
      await sleep(300);
      if (res.colFlag) {
        res.colFlag.afterClear = { cls: d.documentElement.classList.contains('pm-col-sel'),
                                   col: d.documentElement.getAttribute('data-pm-col') || '' };
      }
    }

    // ⑦b 端点是**元素**（在单元格空白处按下、或程序设置选区）时判侧不能默认成原文：
    //     以前 `closest('[data-side]')` 拿不到就 default 成 en → 菜单跑到左列、复制出原文、
    //     高亮记到原文侧（"选区在右、结果全在左"的半对半错）。现在判侧统一走 columnOf。
    const twoColB = [...d.querySelectorAll('[data-row-id]')].filter((r) =>
      r.querySelector('.rrow.side-zh .rr-body')).slice(0, 3);
    if (twoColB.length === 3) {
      const cellZh = twoColB[0].querySelector('.cell-zh');
      const zEnd = textEnds(twoColB[2].querySelector('.rrow.side-zh .rr-body')).last;
      if (cellZh && zEnd) {
        const er2 = d.createRange();
        er2.setStart(cellZh, 0);                       // ← 锚点是**元素**，不是文字节点
        er2.setEnd(zEnd, Math.min(12, (zEnd.nodeValue || '').length));
        const esel = w.getSelection();
        esel.removeAllRanges();
        esel.addRange(er2);
        const erect = er2.getBoundingClientRect();
        cellZh.dispatchEvent(ev('mouseup', erect));
        await sleep(500);
        const emenu = d.querySelector('.selection-menu');
        const emr = emenu ? emenu.getBoundingClientRect() : null;
        const ezr = cellZh.getBoundingClientRect();
        const before2 = new Set([...d.querySelectorAll('mark.hl[data-hl]')]
          .map((m) => m.getAttribute('data-hl')));
        const esw = emenu && emenu.querySelector('.swatch.hl-blue');
        if (esw) {
          esw.dispatchEvent(ev('mousedown', erect));
          w.getSelection().collapseToEnd();
          esw.dispatchEvent(ev('mouseup', erect));
          await sleep(200);
          if (esw.isConnected) esw.dispatchEvent(ev('click', erect));
          await sleep(1400);
        }
        const newIds2 = [...d.querySelectorAll('mark.hl[data-hl]')]
          .map((m) => m.getAttribute('data-hl'))
          .filter((i) => i && !before2.has(i));
        res.elemAnchor = {
          menu: !!emenu,
          menuInColumn: !!emr && emr.left >= ezr.left - 4,
          menuX: emr ? Math.round(emr.left) : null, zhLeft: Math.round(ezr.left),
          zhMarks: countIn(twoColB[0].getAttribute('data-row-id'), 'zh', newIds2),
          enMarks: countIn(twoColB[0].getAttribute('data-row-id'), 'en', newIds2),
        };
        w.getSelection().removeAllRanges();
        await sleep(250);
      }
    }

    // ⑦ 复制：选区跨列时，剪贴板里不能出现对面列的文字（浏览器复制的是整段连续选区）
    const cEn = d.querySelector('[data-row-id] .rrow.side-en .rr-body');
    const cZh = d.querySelector('[data-row-id] .rrow.side-zh .rr-body');
    if (cEn && cZh) {
      // 不管左右顺序是哪一种：起止点按**文档顺序**取，锚点落在哪一侧就断言那一侧
      const enBeforeZh = !!(cEn.compareDocumentPosition(cZh) & Node.DOCUMENT_POSITION_FOLLOWING);
      const first = textEnds(enBeforeZh ? cEn : cZh).first;
      const last = textEnds(enBeforeZh ? cZh : cEn).last;
      const cr = d.createRange();
      cr.setStart(first, 0);
      cr.setEnd(last, (last.nodeValue || '').length);
      const csel = w.getSelection();
      csel.removeAllRanges();
      csel.addRange(cr);
      res.copyWholeLen = (csel.toString() || '').length;
      res.copyAnchorSide = enBeforeZh ? 'en' : 'zh';
      const dt = new w.DataTransfer();
      d.dispatchEvent(new w.ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: dt }));
      const copied = dt.getData('text/plain') || '';
      res.copy = { len: copied.length, hasCjk: /[\u4e00-\u9fff]/.test(copied),
                   head: copied.slice(0, 30) };
      csel.removeAllRanges();
    }

    // 收尾：把这一轮探针造的高亮 id 全部报上去（Python 侧统一删干净）
    res.createdIds = [...d.querySelectorAll('mark.hl[data-hl]')]
      .map((m) => m.getAttribute('data-hl'));
    flush();
  } catch (err) { res.error = String(err); flush(); }
});
</script>
"""


# Markdown 视图划词探针（用户报过：那边"两列选区不独立、公式不能高亮"）。
#
# 用**逐点扩展选区**模拟真实拖拽（`caretRangeFromPoint` + `setBaseAndExtent`），
# 因为夹取逻辑是靠 `selectionchange` 起作用的，一次性 `addRange` 反而测不全。
# 点必须落在**滚动容器的可见区**里：页面很长时随手取元素中心会落到工具栏上，
# 那样测的就不是内容区了（踩过）。
MD_PROBE = """<!doctype html>
<meta charset="utf-8"><title>markdown selection probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:620px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
f.addEventListener('load', async () => {
  const w = f.contentWindow, d = f.contentDocument;
  const res = {};
  const visible = (r) => r && r.width > 6 && r.height > 5 && r.top > 70 && r.bottom < w.innerHeight - 30;
  const scrollerOf = (el) => {
    let p = el?.parentElement;
    while (p) { const cs = w.getComputedStyle(p);
      if (/(auto|scroll)/.test(cs.overflowY) && p.clientHeight > 80) return p; p = p.parentElement; }
    return null;
  };
  const insideScroller = (rect, el) => {
    const sc = scrollerOf(el); if (!sc) return true;
    const b = sc.getBoundingClientRect();
    return rect.top >= b.top + 6 && rect.bottom <= b.bottom - 6;
  };
  const colOf = (node) => {
    const el = (node && node.nodeType === 3 ? node.parentElement : node);
    if (!el?.closest) return '';
    const md = el.closest('[data-md-side]');
    return md ? 'md:' + (md.getAttribute('data-md-side') || '?') : 'out';
  };
  const pointIn = (el, off = 6) => {
    if (!el) return null;
    const walker = d.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let n = walker.nextNode();
    while (n) {
      const value = n.nodeValue || '';
      if (value.trim().length > off + 4) {
        const r = d.createRange();
        r.setStart(n, Math.min(off, value.length));
        r.setEnd(n, Math.min(off + 8, value.length));
        const rect = r.getBoundingClientRect();
        if (visible(rect) && insideScroller(rect, el)) {
          return { x: Math.round(rect.left + 3), y: Math.round(rect.top + rect.height / 2) };
        }
      }
      n = walker.nextNode();
    }
    return null;
  };
  const firstPoint = (sel) => {
    for (const el of d.querySelectorAll(sel)) { const p = pointIn(el); if (p) return p; }
    return null;
  };
  const drag = async (from, to, steps = 8) => {
    const a = d.caretRangeFromPoint(from.x, from.y);
    if (!a) return false;
    const s = w.getSelection();
    s.removeAllRanges(); s.addRange(a);
    await sleep(60);
    for (let i = 1; i <= steps; i += 1) {
      const x = from.x + (to.x - from.x) * i / steps, y = from.y + (to.y - from.y) * i / steps;
      const r = d.caretRangeFromPoint(x, y);
      if (!r) continue;
      try { s.setBaseAndExtent(a.startContainer, a.startOffset, r.startContainer, r.startOffset); }
      catch (e) { res.dragErr = String(e); }
      // 每一步都补一次真实 mousemove：应用的夹取在**拖拽的任务里**跑（`dragPoint` 也靠它更新）
      (d.elementFromPoint(x, y) || d.body).dispatchEvent(new w.MouseEvent('mousemove', {
        bubbles: true, cancelable: true, buttons: 1, clientX: x, clientY: y }));
      await sleep(35);
    }
    /* 真实鼠标还会派发 mousemove：夹取必须在**同一个任务里**完成（不能等异步的 selectionchange，
     * 否则浏览器可能已经画过一帧跨面底色）。这里补一次真实 mousemove，然后立刻读选区。 */
    const el = d.elementFromPoint(to.x, to.y) || d.body;
    el.dispatchEvent(new w.MouseEvent('mousemove', {
      bubbles: true, cancelable: true, buttons: 1, clientX: to.x, clientY: to.y }));
    res.syncs = [...(res.syncs || []), { anchor: colOf(s.anchorNode), focus: colOf(s.focusNode),
                                         collapsed: s.isCollapsed, len: (s.toString() || '').length }];
    /* 见 HL_PROBE 里 `keepFocus` 的说明：headless 下 iframe 会随机失焦，app 收到 blur 会收起菜单 */
    try { w.focus(); } catch (e) { /* 忽略 */ }
    el.dispatchEvent(new w.MouseEvent('mouseup', { bubbles: true, cancelable: true, clientX: to.x, clientY: to.y }));
    await sleep(450);
    return true;
  };
  try {
    await sleep(__WAIT__);
    const doc = d.querySelector('.md-doc');

    // ① 原文面 → 译文面：选区必须被夹回同一面
    let p1 = null, p2 = null;
    for (const frac of [0, 0.12, 0.25, 0.38, 0.5, 0.62, 0.75, 0.88, 0.06, 0.19, 0.31, 0.44]) {
      if (doc && doc.scrollHeight > doc.clientHeight) {
        doc.scrollTop = Math.round((doc.scrollHeight - doc.clientHeight) * frac);
        await sleep(150);
      }
      p1 = firstPoint('[data-md-side="en"]');
      p2 = firstPoint('[data-md-side="zh"]');
      if (p1 && p2) { res.scrollFrac = frac; break; }
    }
    res.hasBoth = !!(p1 && p2);
    if (p1 && p2) {
      await drag(p1, p2);
      const s = w.getSelection();
      res.cross = { anchor: colOf(s.anchorNode), focus: colOf(s.focusNode),
                    collapsed: s.isCollapsed, text: (s.toString() || '').slice(0, 20) };
      w.getSelection().removeAllRanges();
      await sleep(250);
    }

    // ② 公式：拖过公式 → 菜单应出色块 → 点色块后公式整块被 mark 包住
    //    优先挑**多行显示公式**（`.katex-display`）：底色必须覆盖整块，不能只黄中间一行。
    //    整篇 Markdown 很长，显示公式未必在第一屏 —— 逐段滚下去找，找到就用它。
    let katex = null;
    const pickDisp = () => [...d.querySelectorAll('[data-md-side] .katex-display')]
      .find((k) => { const r = k.getBoundingClientRect();
        return visible(r) && insideScroller(r, k) && r.height > 40; });
    katex = pickDisp();
    if (!katex && doc) {
      for (const frac of [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95]) {
        doc.scrollTop = Math.round((doc.scrollHeight - doc.clientHeight) * frac);
        await sleep(140);
        katex = pickDisp();
        if (katex) break;
      }
    }
    for (const k of (katex ? [] : d.querySelectorAll('[data-md-side] .katex'))) {
      const r = k.getBoundingClientRect();
      if (visible(r) && insideScroller(r, k) && r.width > 90) { katex = k; break; }
    }
    res.hasFormula = !!katex;
    res.isDisplay = !!katex && (katex.classList.contains('katex-display')
      || !!katex.closest('.katex-display'));
    if (katex) {
      /* ⚠️ 等滚动**停稳**再拖：app 收到任何 scroll 都会收起划词菜单（滚动时收起是对的），
       * 而上面为了找公式连滚了好几段 —— 滚动事件是异步的，最后一次可能正好落在拖拽期间，
       * 菜单就会"选完立刻消失"（表现为随机失败：swatches=0）。 */
      let _last = -1;
      for (let i = 0; i < 8 && doc && doc.scrollTop !== _last; i += 1) {
        _last = doc.scrollTop;
        await sleep(120);
      }
      try { w.focus(); } catch (e) { /* 见 HL_PROBE 的 keepFocus */ }
      const r = katex.getBoundingClientRect();
      await drag({ x: Math.round(r.left + 8), y: Math.round(r.top + r.height / 2) },
                 { x: Math.round(r.right - 8), y: Math.round(r.top + r.height / 2) }, 6);
      const menu = d.querySelector('.selection-menu');
      res.swatches = menu ? menu.querySelectorAll('.swatch').length : 0;
      const sw = menu && menu.querySelector('.swatch.hl-yellow');
      if (sw) {
        const sr = sw.getBoundingClientRect();
        const at = { x: Math.round(sr.left + sr.width / 2), y: Math.round(sr.top + sr.height / 2) };
        const mk = (t) => new w.MouseEvent(t, { bubbles: true, cancelable: true, ...at });
        sw.dispatchEvent(mk('mousedown'));
        w.getSelection().collapseToEnd();
        sw.dispatchEvent(mk('mouseup'));
        await sleep(200);
        if (sw.isConnected) { sw.dispatchEvent(mk('click')); await sleep(1300); }
        else res.menuGone = true;
      }
      const part = katex.closest('[data-md-side]');
      const blockMark = part && part.querySelector('mark.hl.block');
      const disp = katex.classList.contains('katex-display') ? katex : katex.closest('.katex-display');
      res.formula = {
        wrapped: !!(part && part.querySelector('mark.hl > .katex, mark.hl .katex')),
        intact: !!(part && part.querySelector('.katex')),
        marks: part ? part.querySelectorAll('mark.hl').length : 0,
        hasBlockClass: !!blockMark,
        markH: blockMark ? Math.round(blockMark.getBoundingClientRect().height) : 0,
        dispH: disp ? Math.round(disp.getBoundingClientRect().height) : 0,
        covers: !!blockMark && !!disp
          && blockMark.getBoundingClientRect().height >= disp.getBoundingClientRect().height * 0.8,
      };
    }
    out(res);
  } catch (err) { res.error = String(err); out(res); }
});
</script>
"""


# 原 PDF 栏划词菜单探针：那一栏没有块锚点，菜单只该有「AI 解释 / 复制」——
# **不再有**「原 PDF 栏不支持高亮」那行说明（用户口径：完全移除该字样）。
#
# 回归点：那行说明是上一轮加的，用户明确要求移除（菜单里不该出现解释性文案）。
PDF_MENU_PROBE = """<!doctype html>
<meta charset="utf-8"><title>pdf menu probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:700px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
f.addEventListener('load', async () => {
  const w = f.contentWindow, d = f.contentDocument;
  const res = {};
  try {
    await sleep(__WAIT__);
    /* pdf.js 的文字层是异步渲染的：轮询等它出来，别只 sleep 一个固定时长（早先就是这里没等到） */
    let spans = [];
    for (let i = 0; i < 40; i += 1) {
      spans = [...d.querySelectorAll('.pdf-text-layer span')]
        .filter((s) => (s.textContent || '').trim().length > 6);
      if (spans.length) break;
      await sleep(500);
    }
    res.spans = spans.length;
    if (!spans.length) { out(res); return; }
    const node = [...spans[0].childNodes].find((n) => n.nodeType === 3);
    if (!node) { res.noText = true; out(res); return; }
    const len = (node.nodeValue || '').length;
    const range = d.createRange();
    range.setStart(node, 0);
    range.setEnd(node, Math.max(1, Math.min(len, 8)));
    const sel = w.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    const rect = range.getBoundingClientRect();
    try { w.focus(); } catch (e) { /* 见 HL_PROBE 的 keepFocus：headless 下 iframe 会随机失焦 */ }
    spans[0].dispatchEvent(new w.MouseEvent('mouseup',
      { bubbles: true, cancelable: true, clientX: rect.left + 2, clientY: rect.top + 2 }));
    await sleep(450);
    const menu = d.querySelector('.selection-menu');
    res.menu = !!menu;
    if (menu) {
      res.buttons = [...menu.querySelectorAll('button')].map((b) => b.textContent.trim()).filter(Boolean);
      res.swatches = menu.querySelectorAll('.swatch').length;
      res.hasHint = (menu.textContent || '').includes('不支持高亮');
    }
    res.bodyHasHint = (d.body.textContent || '').includes('不支持高亮');

    /* ② 选了 PDF 之后，在**译文里按下**必须能把那次 PDF 选区收掉。
     * 回归点（用户报过）：PDF 选区还罩着时 `pm-pdf-drag` 一直挂着 → 非 PDF 内容
     * 是 `user-select: none`，而 Chrome 不允许在不可选的内容上开始新选区 →
     * "选了 PDF 之后译文再也选不中"。 */
    const zhRow = d.querySelector('.rrow.side-zh') || d.querySelector('.col-rows .rr-body');
    const zhBody2 = d.querySelector('.rrow.side-zh .rr-body');
    if (node && zhBody2 && zhRow) {
      const p = d.createRange();
      p.setStart(node, 0);
      p.setEnd(node, Math.max(1, Math.min((node.nodeValue || '').length, 8)));
      const psel = w.getSelection();
      psel.removeAllRanges();
      psel.addRange(p);
      await sleep(350);                       // selectionchange 是异步的（keepDragClass 靠它）
      res.afterPdfSel = {
        dragClass: d.documentElement.classList.contains('pm-pdf-drag'),
        zhUserSelect: w.getComputedStyle(zhRow).userSelect,
      };
      // 在译文文字上按下（合成 pointerdown，坐标落在译文文字里）
      const tw = d.createTreeWalker(zhBody2, NodeFilter.SHOW_TEXT);
      let tn = tw.nextNode();
      while (tn && (tn.nodeValue || '').trim().length < 12) tn = tw.nextNode();
      if (tn) {
        const tr = d.createRange();
        tr.setStart(tn, 2);
        tr.setEnd(tn, 8);
        const rect = tr.getBoundingClientRect();
        zhBody2.dispatchEvent(new w.PointerEvent('pointerdown', {
          bubbles: true, cancelable: true, button: 0, buttons: 1,
          clientX: Math.round(rect.left + 2), clientY: Math.round(rect.top + rect.height / 2) }));
        await sleep(300);
        res.afterZhPress = {
          dragClass: d.documentElement.classList.contains('pm-pdf-drag'),
          zhUserSelect: w.getComputedStyle(zhRow).userSelect,
          selLen: (psel.toString() || '').length,
        };
      }
    }
    out(res);
  } catch (err) { res.error = String(err); out(res); }
});
</script>
"""


# 论文"打开状态"探针：点工具栏的三个开关（视图 / 原文栏呈现 / 左右顺序），
# 断言它们被写进 URL query —— 标签条记住的就是"每个标签上次的位置（含 query）"，
# 切标签时靠它还原；服务端那份 `reading_progress` 负责重开也记得。
VIEW_STATE_PROBE = """<!doctype html>
<meta charset="utf-8"><title>view state probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:560px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
f.addEventListener('load', async () => {
  const w = f.contentWindow, d = f.contentDocument;
  const res = {};
  const active = (sel) => [...d.querySelectorAll(sel + ' button')]
    .filter((b) => b.classList.contains('active'))
    .map((b) => (b.textContent.trim() || b.title));
  const clickText = (txt) => {
    const b = [...d.querySelectorAll('.reader-toolbar button')]
      .find((x) => x.textContent.trim() === txt);
    if (b) { b.click(); return true; }
    return false;
  };
  const clickTitle = (t) => {
    const b = [...d.querySelectorAll('.reader-toolbar button')].find((x) => x.title === t);
    if (b) { b.click(); return true; }
    return false;
  };
  try {
    await sleep(__WAIT__);
    /* 打开时的界面状态：侧栏开合 / 字号 / 原 PDF 缩放；
     * 「显示参考文献」开关已按用户要求移除 → 这里改成断言"开关不存在 + 参考文献行确实渲染了" */
    const uiState = () => {
      const reader = d.querySelector('.reader');
      const sizeBtn = [...d.querySelectorAll('.reader-toolbar .seg button')]
        .find((b) => b.classList.contains('active')
          && ['小', '中', '大'].includes(b.textContent.trim()));
      const zoomLabel = [...d.querySelectorAll('.reader-toolbar .tiny.faint.nowrap')]
        .map((s) => s.textContent.trim())[0] || '';
      const cb = d.querySelector('.rows-foot input[type="checkbox"]');
      /* 参考文献行：**不能按行类型数** —— 有的论文（turboquant）参考文献行的类型是 `para`
       * 而不是 `ref`。改成"找到参考文献小节标题后，它后面还有多少行"：
       * 小节标题本身很短（REFERENCES / References / 参考文献，双语时两串拼在一起），正文里
       * 偶尔提到 references 的段落都长得多。 */
      const rows = [...d.querySelectorAll('[data-row-id]')];
      let refHead = -1;
      rows.forEach((el, i) => {
        const t = (el.textContent || '').trim();
        if (t.length > 0 && t.length < 40 && /(references|bibliography|参考文献)/i.test(t)) refHead = i;
      });
      const refRows = refHead < 0 ? 0 : rows.length - refHead - 1;
      return {
        asideHidden: !!reader && reader.classList.contains('no-aside'),
        font: sizeBtn ? sizeBtn.textContent.trim() : null,
        refsToggle: !!cb,
        refRows,
        zoomLabel,
      };
    };
    res.before = { search: w.location.search, view: active('.seg'), ui: uiState() };
    /* 先切到"另一套"状态，保证下面三个点击**确实改变了状态** ——
     * 点已经选中的按钮时 `setXxx` 会提前 return（不会写 URL），那样测的就不是这条路径了
     * （踩过：进来时恰好已经是目标状态 → 三次点击全是空操作）。 */
    clickText('原文');            // view = original
    await sleep(300);
    clickText('原文文本');         // 原文栏呈现 = text
    await sleep(300);
    clickText('对照');            // 回到对照（顺序开关才会出现）
    await sleep(300);
    clickTitle('原文在左（译文在右）');   // order = en_left
    await sleep(400);
    res.reset = { search: w.location.search, view: active('.seg') };
    // 目标状态：原 PDF + 译文在左
    res.clicked = { page: clickText('原 PDF') };
    await sleep(500);
    res.clicked.zhLeft = clickTitle('译文在左（原文在右）');
    await sleep(700);
    res.after = { search: w.location.search, view: active('.seg') };
    res.dom = {
      pgPair: !!d.querySelector('.pg-pair'),
      imageRight: !!d.querySelector('.pg-pair.image-right'),
      imageLeft: !!d.querySelector('.pg-pair.image-left'),
      colImage: !!d.querySelector('.col-image'),
    };
    out(res);
  } catch (err) { res.error = String(err); out(res); }
});
</script>
"""


def put_progress(base: str, paper: str, payload: dict) -> dict:
    """写回阅读进度（自检用它做"测试前置状态"与"跑完还原"）。"""
    req = urllib.request.Request(
        f"{base}/api/v1/papers/{paper}/progress", method="PUT",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8")).get("data") or {}


def read_progress(base: str, paper: str) -> dict | None:
    code, body = fetch(f"{base}/api/v1/papers/{paper}/progress")
    if code != 200:
        return None
    try:
        return json.loads(body.decode("utf-8")).get("data") or None
    except json.JSONDecodeError:
        return None


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


def find_scanned_paper(base: str, items: list[dict], exclude: str = "") -> str:
    """找一个**含无文字层页**的论文（扫描件），用来断言「原 PDF 栏回退位图」。

    每篇要拉一次 `/reader`，所以只看前几篇；找不到就返回空串（用例会 SKIP 而不是硬失败）。
    """
    for item in items[:8]:
        pid = item.get("id") or ""
        if not pid or pid == exclude:
            continue
        try:
            _st, body = fetch(f"{base}/api/v1/papers/{pid}/reader", timeout=40)
            pages = json.loads(body.decode("utf-8"))["data"].get("pages") or []
        except Exception:  # noqa: BLE001
            continue
        if any(p.get("has_text_layer") is False for p in pages):
            return pid
    return ""


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
    items: list[dict] = []
    if not paper_hint:
        status, body = fetch(base + "/api/v1/papers")
        try:
            items = json.loads(body.decode("utf-8"))["data"]["items"]
            parsed = [p for p in items
                      if p.get("status") in ("parsed", "translating", "translated", "indexed")]
            pool = parsed or items
            # ⚠️ 默认样本**必须"有内容可看"，不能只看页数**。实测（2026-09-27）：导入了一篇
            # 25 页、**还没翻译**的论文之后，它按"页数最多"成为默认样本，于是读者相关的探针
            # （md 视图 `data-md-page="3"`、简报 `brief-k`、划词高亮、原 PDF 文字层）全红，
            # 自检从 88/87/1 掉到 **71/63** —— 看着像代码回归，其实是样本选错了。
            # 排序键：先"已翻完/已入库"，再"已翻页数"，最后才页数。
            def _rank(p: dict) -> tuple:
                pages = int(p.get("page_count") or 0)
                done = int(p.get("translate_done") or 0)
                complete = bool(done and pages and done >= pages) or \
                    p.get("status") in ("translated", "indexed")
                return (1 if complete else 0, done, pages)

            pool.sort(key=_rank, reverse=True)
            paper_hint = pool[0]["id"] if pool else ""
        except Exception:  # noqa: BLE001
            paper_hint = ""
    else:
        try:
            _st, body = fetch(base + "/api/v1/papers")
            items = json.loads(body.decode("utf-8"))["data"]["items"]
        except Exception:  # noqa: BLE001
            items = []

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

    # 扫描件论文（含无文字层页）：给「原 PDF(扫描件)」用例用；没有则 SKIP 该用例
    scan_paper = find_scanned_paper(base, items, exclude=paper)
    print(f"用于扫描件用例的论文：{scan_paper or '（库里没有扫描件）'}")

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
    # 打开阅读器会**把当时的视图状态存回服务端**（阅读进度里含 view_mode/original_mode/pair_order），
    # 而下面的路由用例、⑤ 的标签用例、⑩ 都在用不同视图打开同一篇论文 ——
    # 所以在**任何渲染之前**先记下这篇论文原来的打开状态，全部跑完再原样写回。
    progress_snapshot = read_progress(base, paper) if paper else None
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
            url_path = path.format(paper=paper, block=block, scan=scan_paper)
            if "{scan}" in path and not scan_paper:
                print(f"[SKIP] {name:<16} {url_path}  （库里没有扫描件论文）")
                continue
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

    # ---------------- ④ 网格卡片等高（`.card + .card` 的竖向间距曾经把网格卡片拉歪） ----------------
    print()
    print("=" * 78)
    print("④ 网格卡片等高等宽（首页圆角矩形必须一样大）")
    print("=" * 78)
    if browser:
        dist = ROOT / "frontend" / "dist"
        probe_file = dist / "_grid_probe.html"
        try:
            probe_file.write_text(
                GRID_PROBE.replace('__URL__', f"{base}/app/home")
                .replace('__WAIT__', '6000'), encoding='utf-8')
            dom = render(browser, f"{base}/app/_grid_probe.html", 25000, "1700,1100")
            m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
            grids = None
            if m and m.group(1).strip() != "waiting":
                try:
                    grids = json.loads(m.group(1))
                except json.JSONDecodeError:
                    grids = None
            if not grids:
                failures += 1
                results.append({"kind": "grid", "path": "/home", "ok": False,
                                "detail": "未取到测量结果"})
                print("[FAIL] home-grid        未取到测量结果")
            else:
                for g in grids:
                    problems: list[str] = []
                    for row in g["rows"]:
                        hs = {c["h"] for c in row["cards"]}
                        ws = {c["w"] for c in row["cards"]}
                        if len(row["cards"]) > 1 and (max(hs) - min(hs) > 1 or max(ws) - min(ws) > 1):
                            problems.append(f"同排 {len(row['cards'])} 张卡尺寸不一：高{sorted(hs)} 宽{sorted(ws)}")
                    # 同一个网格里跨行的卡片高度也必须一致（首页两行导航卡一样高）
                    allh = {c["h"] for row in g["rows"] for c in row["cards"]}
                    if g.get("uniform") and max(allh) - min(allh) > 1:
                        problems.append(f"网格内卡片高度不一致：{sorted(allh)}")
                    label = g["cls"] or "(grid)"
                    ok = not problems
                    failures += 0 if ok else 1
                    results.append({"kind": "grid", "path": label, "ok": ok,
                                    "detail": "同排卡片等高等宽" if ok else "；".join(problems)})
                    print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} "
                          f"{results[-1]['detail']}")
        finally:
            probe_file.unlink(missing_ok=True)
    else:
        print("[SKIP] 需要浏览器")

    # ---------------- ⑤ 多标签页切论文（实例复用会串台） + 触摸原生遮罩 ----------------
    # 注意：这一节与 ⑩ 会真的去点工具栏开关 → 阅读器把新状态存回服务端；
    # 打开状态由 main 开头记下的 `progress_snapshot` 在最后统一还原。
    print()
    print("=" * 78)
    print("⑤ 多标签页切换论文（每个标签的正文必须各归各）")
    print("=" * 78)
    other = next((it.get("id") for it in items if it.get("id") and it["id"] != paper), "")
    if browser and paper and other:
        dist = ROOT / "frontend" / "dist"
        probe_file = dist / "_tabs_probe.html"
        try:
            probe_file.write_text(
                TABS_PROBE.replace('__A__', paper).replace('__B__', other)
                .replace('__WAIT__', '3400'), encoding='utf-8')
            dom = render(browser, f"{base}/app/_tabs_probe.html", 34000, "1700,1100")
            m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
            data = None
            if m and m.group(1).strip() != "waiting":
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    data = None
            if not data or "a" not in data:
                failures += 1
                results.append({"kind": "tabs", "path": "多标签页", "ok": False,
                                "detail": "未取到测量结果"})
                print("[FAIL] 多标签页            未取到测量结果")
            else:
                base_rows = {"A": data["a"]["rows"], "B": data["b"]["rows"]}
                checks = [
                    ("标签A首次", data["a"], None, "A"),
                    ("标签B首次", data["b"], None, "B"),
                    ("B→A 切回", data["aAgain"], base_rows["A"], "A"),
                    ("A→B 切回", data["bAgain"], base_rows["B"], "B"),
                ]
                for label, s, expect, who in checks:
                    problems: list[str] = []
                    if not s["rows"]:
                        problems.append("正文没渲染")
                    if expect is not None and s["rows"] != expect:
                        problems.append(f"行数变了（{s['rows']} ≠ {expect}）"
                                        f"：切论文时数据串台")
                    if who == "A" and s["text"] and s["text"] == data["b"]["text"]:
                        problems.append("显示的还是另一篇论文")
                    ok = not problems
                    failures += 0 if ok else 1
                    detail = (f"{s['rows']} 行 · {s['text'][:22]}" if ok
                              else "；".join(problems))
                    results.append({"kind": "tabs", "path": label, "ok": ok,
                                    "detail": detail})
                    print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
                # 触摸原生灰色遮罩必须已关闭（Chrome 默认 rgba(0, 0, 0, 0.18)）
                tap = data["a"].get("tap", "")
                ok = tap in ("rgba(0, 0, 0, 0)", "transparent")
                failures += 0 if ok else 1
                results.append({"kind": "tabs", "path": "触摸高亮", "ok": ok,
                                "detail": f"-webkit-tap-highlight-color = {tap}"})
                print(f"[{'PASS' if ok else 'FAIL'}] {'触摸原生遮罩':<16} {tap}")
                # 打开状态随标签走：在 A 上设成「原 PDF + 译文在左」，切到 B 再切回来必须还在
                v1 = data.get("viewA1") or {}
                v2 = data.get("viewA2") or {}
                q1, q2 = str(v1.get("search") or ""), str(v2.get("search") or "")
                for label, ok, detail in [
                    ("状态写进标签 URL",
                     all(k in q1 for k in ("view=", "left=page", "order=zh_left")),
                     f"设完之后的 URL = {q1 or '(空)'}，按钮 = {v1.get('active')}"),
                    ("切标签回来后还原",
                     bool(v2.get("imageRight")) and bool(v2.get("colImage"))
                     and "left=page" in q2 and "order=zh_left" in q2,
                     f"切回 A 之后 URL = {q2 or '(空)'}，原PDF在左={v2.get('imageRight')}，"
                     f"按钮 = {v2.get('active')}"),
                ]:
                    failures += 0 if ok else 1
                    results.append({"kind": "tabs", "path": label, "ok": ok, "detail": detail})
                    print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
        finally:
            probe_file.unlink(missing_ok=True)
    else:
        print("[SKIP] 需要浏览器与至少两篇论文")

    # ---------------- ⑥ 插图放大：弹窗标题 = 图注（不是写死的"插图"） ----------------
    print()
    print("=" * 78)
    print("⑥ 插图放大（标题必须是图片标题，且长图注不挤坏标题栏）")
    print("=" * 78)
    if browser and paper:
        dist = ROOT / "frontend" / "dist"
        probe_file = dist / "_zoom_probe.html"
        try:
            probe_file.write_text(
                ZOOM_PROBE.replace('__URL__', f"{base}/app/reader/{paper}?view=bilingual&left=text&nosse=1")
                .replace('__WAIT__', '7000'), encoding='utf-8')
            dom = render(browser, f"{base}/app/_zoom_probe.html", 25000, "1700,1100")
            m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
            data = None
            if m and m.group(1).strip() != "waiting":
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    data = None
            if not data or not data.get("hasFigure"):
                failures += 1
                results.append({"kind": "zoom", "path": "插图放大", "ok": False,
                                "detail": "没有可点开的插图 / 未取到测量结果"})
                print("[FAIL] 插图放大            没有可点开的插图 / 未取到测量结果")
            else:
                caption = str(data.get("caption") or "").strip()
                checks = [
                    ("标题=图注", bool(caption) and caption != "插图" and len(caption) >= 8,
                     f"{len(caption)} 字：{caption[:34]}"),
                    ("有图片", bool(data.get("hasImage")), "弹窗里有 <img>"),
                    ("长图注一行截断", bool(data.get("truncated"))
                     and data.get("textOverflow") == "ellipsis", "省略号截断"),
                    ("标题栏不溢出", bool(data.get("headFits")) and bool(data.get("closeInside")),
                     "关闭按钮仍在弹窗内"),
                ]
                for label, ok, detail in checks:
                    failures += 0 if ok else 1
                    results.append({"kind": "zoom", "path": label, "ok": ok, "detail": detail})
                    print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
        finally:
            probe_file.unlink(missing_ok=True)
    else:
        print("[SKIP] 需要浏览器与论文数据")

    # ---------------- ⑦ 划词高亮：原文 / 译文分开存 ----------------
    print()
    print("=" * 78)
    print("⑦ 划词高亮（原文与译文各标一条，互不影响）")
    print("=" * 78)
    if browser and paper:
        dist = ROOT / "frontend" / "dist"
        probe_file = dist / "_hl_probe.html"
        # 探针会**真的往库里写高亮**（自检不该留垃圾）：先记下已有的 id，跑完把新增的删干净。
        # 只信"跑之前 vs 跑之后"的差集 —— 探针自己报的 id 未必覆盖全部（比如跨行那两条）。
        def _paper_highlight_ids() -> set[str]:
            try:
                _st, body = fetch(f"{base}/api/v1/highlights?paper_id={paper}", timeout=20)
                return {r["id"] for r in json.loads(body.decode("utf-8"))["data"]["items"]}
            except Exception:  # noqa: BLE001
                return set()

        pre_ids = _paper_highlight_ids()
        try:
            probe_file.write_text(
                HL_PROBE.replace('__URL__',
                                 f"{base}/app/reader/{paper}?view=bilingual&left=text&nosse=1")
                .replace('__PAPER__', paper)
                .replace('__WAIT__', '7000'), encoding='utf-8')
            dom = render(browser, f"{base}/app/_hl_probe.html", 25000, "1700,1100")
            m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
            data = None
            if m and m.group(1).strip() != "waiting":
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    data = None
            ids = (data or {}).get("ids") or {}
            extra = (data or {}).get("formulaIds") or []
            created = [v for v in list(ids.values()) + list(extra)
                       + list((data or {}).get("createdIds") or []) if v]
            created = list(dict.fromkeys(created))
            if not data or "afterZh" not in data:
                failures += 1
                results.append({"kind": "highlight", "path": "划词高亮", "ok": False,
                                "detail": f"未取到测量结果（探针报错：{(data or {}).get('error')}）"})
                print(f"[FAIL] 划词高亮            未取到测量结果（探针报错：{(data or {}).get('error')}）")
            else:
                if data.get("error"):
                    # 探针抛错时后面的测量项会整段缺失，直接把原因打出来（不然只看到一堆 None）
                    print(f"         （探针报错：{data.get('error')}）")
                cross = data.get("crossRow") or {}
                formula = data.get("formula") or {}
                disp = data.get("disp") or {}
                crossDel = data.get("crossDelete") or {}
                sync = data.get("crossColumnSync") or {}
                checks = [
                    ("点色块真能生效", bool(data.get("clickEn", {}).get("ok"))
                     and bool(data.get("clickZh", {}).get("ok")),
                     (data.get("clickEn", {}).get("reason")
                      or data.get("clickZh", {}).get("reason") or "菜单没在 click 之前被卸载")),
                    ("原文侧只画原文", data["afterEn"]["en"] == 1 and data["afterEn"]["zh"] == 0,
                     f"原文 {data['afterEn']['en']} 条 / 译文 {data['afterEn']['zh']} 条"),
                    ("两侧各自独立", data["afterZh"]["en"] == 1 and data["afterZh"]["zh"] == 1
                     and bool(data.get("sameRow")),
                     f"同一行：原文 {data['afterZh']['en']} 条 / 译文 {data['afterZh']['zh']} 条"),
                    ("颜色真的生效", data["colors"]["en"] != data["colors"]["zh"]
                     and "rgba" in data["colors"]["en"],
                     f"{data['colors']['en']} vs {data['colors']['zh']}"),
                    ("公式能整体高亮", bool(formula.get("wrapped")) and bool(formula.get("katexIntact"))
                     and (formula.get("marks") or 0) >= 1
                     and bool((data.get("formulaClick") or {}).get("ok")),
                     f"整块包住 katex={formula.get('wrapped')}，公式结构完好={formula.get('katexIntact')}，"
                     f"{formula.get('marks')} 段"),
                    ("多行公式底色覆盖整块", bool(disp.get("covers")) and bool(disp.get("hasBlockClass")),
                     f"mark 高 {disp.get('markH')}px / 公式块高 {disp.get('dispH')}px，"
                     f"block 类={disp.get('hasBlockClass')}（点色块={bool((data.get('dispClick') or {}).get('ok'))}）"),
                    ("跨行选择分成同组多条", cross.get("rowsWithMarks") == 2
                     and len(cross.get("rowIds") or []) == 2,
                     f"选中 {data.get('crossPicked')}（{data.get('crossSelText')}）→ "
                     f"命中 {cross.get('rowsWithMarks')} 行：{cross.get('rowIds')}"),
                    ("菜单删除整条高亮", bool(crossDel.get("ok"))
                     and (cross.get("newIds") or []) != []
                     and data.get("crossRowAfterDelete") == 0,
                     f"菜单项 {crossDel.get('buttons')}；这次新造 {len(cross.get('newIds') or [])} 段 → "
                     f"删后还剩 {data.get('crossRowAfterDelete')} 段"),
                    ("正文行仍可写笔记", bool(data.get("menuHasNote")), "菜单里有「笔记」"),
                    ("拖拽当帧即夹回本列", bool(sync.get("anchor")) and sync.get("anchor") == sync.get("focus"),
                     f"跨列原始选区 {data.get('crossColumnRaw')} → 同一次 mousemove 内已夹成 {sync}"),
                    ("跨列选区最终仍在本列", bool((data.get("crossColumn") or {}).get("anchor"))
                     and (data.get("crossColumn") or {}).get("anchor")
                     == (data.get("crossColumn") or {}).get("focus"),
                     f"原文列→译文列拖选后停在 {data.get('crossColumn')}"),
                    ("纵向跨行的对面列高亮不算选中", bool((data.get("intra") or {}).get("fixture"))
                     and bool((data.get("intra") or {}).get("hasDelete"))
                     and bool(data.get("intraEnAlive")),
                     f"译文列里跨 {len((data.get('intra') or {}).get('rows') or [])} 行选择 → "
                     f"对面列造的 fixture={((data.get('intra') or {}).get('otherIds'))}"
                     f"（点色块={(data.get('intraClick') or {}).get('ok')}，"
                     f"原因={(data.get('intraClick') or {}).get('reason')}），"
                     f"has3Rows={data.get('has3Rows')}，"
                     f"菜单有删除={((data.get('intra') or {}).get('hasDelete'))}，"
                     f"删完后对面列那条仍在={data.get('intraEnAlive')}"),
                    ("复制只带本列文字", ((data.get("copy") or {}).get("hasCjk") is False
                      if data.get("copyAnchorSide") == "en"
                      else (data.get("copy") or {}).get("hasCjk") is True)
                     and 0 < ((data.get("copy") or {}).get("len") or 0)
                     < (data.get("copyWholeLen") or 0),
                     f"锚点在{data.get('copyAnchorSide')}侧，整段选区 {data.get('copyWholeLen')} 字 → "
                     f"剪贴板 {(data.get('copy') or {}).get('len')} 字"
                     f"（含中文={((data.get('copy') or {}).get('hasCjk'))}）"),
                    ("松手后不发白对面列（标记跟着选区）",
                     bool((data.get("colFlag") or {}).get("afterUp", {}).get("cls"))
                     and (data.get("colFlag") or {}).get("afterUp", {}).get("col") == "zh"
                     and (data.get("colFlag") or {}).get("afterClear", {}).get("cls") is False,
                     f"松手后 {((data.get('colFlag') or {}).get('afterUp'))} → "
                     f"清空选区后 {((data.get('colFlag') or {}).get('afterClear'))}"),
                    ("胶囊菜单落在本列（不是对面列）", bool((data.get("intra") or {}).get("menuInColumn")),
                     f"菜单 x={((data.get('intra') or {}).get('menuX'))}，"
                     f"译文列 left={((data.get('intra') or {}).get('zhLeft'))}"),
                    ("端点是元素时判侧仍正确", bool((data.get("elemAnchor") or {}).get("menuInColumn"))
                     and ((data.get("elemAnchor") or {}).get("zhMarks") or 0) >= 1
                     and ((data.get("elemAnchor") or {}).get("enMarks") or 0) == 0,
                     f"元素锚点：菜单 x={((data.get('elemAnchor') or {}).get('menuX'))}"
                     f"（译文列 left={((data.get('elemAnchor') or {}).get('zhLeft'))}），"
                     f"新造高亮 译文={((data.get('elemAnchor') or {}).get('zhMarks'))} / "
                     f"原文={((data.get('elemAnchor') or {}).get('enMarks'))}"),
                ]
                for label, ok, detail in checks:
                    failures += 0 if ok else 1
                    results.append({"kind": "highlight", "path": label, "ok": ok, "detail": detail})
                    print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
        finally:
            probe_file.unlink(missing_ok=True)
            # 探针造的高亮**自己删掉**：自检不该往用户库里留垃圾。
            # 按"跑之前 vs 跑之后"的差集删，比只信探针报上来的 id 靠谱（跨行那两条探针不报）。
            leftovers = sorted(_paper_highlight_ids() - pre_ids)
            for hid in leftovers:
                try:
                    req = urllib.request.Request(f"{base}/api/v1/highlights/{hid}", method="DELETE")
                    urllib.request.urlopen(req, timeout=15).read()
                except Exception:  # noqa: BLE001
                    pass
            if leftovers:
                print(f"         （已清理探针造的 {len(leftovers)} 条高亮）")
    else:
        print("[SKIP] 需要浏览器与论文数据")

    # ---------------- ⑧ Markdown 视图划词：两列独立 + 公式能整体高亮 ----------------
    print()
    print("=" * 78)
    print("⑧ Markdown 视图划词（原文/译文两面独立；公式能整体高亮）")
    print("=" * 78)
    if browser and paper:
        dist = ROOT / "frontend" / "dist"
        probe_file = dist / "_md_probe.html"
        pre_ids = _paper_highlight_ids()
        try:
            probe_file.write_text(
                MD_PROBE.replace('__URL__', f"{base}/app/reader/{paper}?md=1&nosse=1")
                .replace('__WAIT__', '7000'), encoding='utf-8')
            dom = render(browser, f"{base}/app/_md_probe.html", 26000, "1700,1100")
            m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
            data = None
            if m and m.group(1).strip() != "waiting":
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    data = None
            if not data or "hasBoth" not in data:
                failures += 1
                results.append({"kind": "md-select", "path": "Markdown 视图划词", "ok": False,
                                "detail": "未取到测量结果"})
                print("[FAIL] Markdown 划词      未取到测量结果")
            else:
                cross = data.get("cross") or {}
                formula = data.get("formula") or {}
                syncs = data.get("syncs") or []
                checks = [
                    ("两面选区互不串", bool(cross.get("anchor")) and cross.get("anchor") == cross.get("focus"),
                     f"原文面→译文面拖选后停在 {cross}"),
                    ("拖拽当帧即夹回同面", bool(syncs) and all(
                        s.get("anchor") and s.get("anchor") == s.get("focus") for s in syncs),
                     f"每次 mousemove 之后：{syncs}"),
                    ("公式能整体高亮", bool(formula.get("wrapped")) and bool(formula.get("intact"))
                     and (formula.get("marks") or 0) >= 1,
                     f"包住 katex={formula.get('wrapped')}，结构完好={formula.get('intact')}，"
                     f"{formula.get('marks')} 段（色块 {data.get('swatches')} 个）"),
                    ("多行公式底色覆盖整块", bool(formula.get("covers")) and bool(formula.get("hasBlockClass")),
                     f"mark 高 {formula.get('markH')}px / 公式块高 {formula.get('dispH')}px，"
                     f"block 类={formula.get('hasBlockClass')}（显示公式={data.get('isDisplay')}）"),
                ]
                for label, ok, detail in checks:
                    failures += 0 if ok else 1
                    results.append({"kind": "md-select", "path": label, "ok": ok, "detail": detail})
                    print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
        finally:
            probe_file.unlink(missing_ok=True)
            for hid in sorted(_paper_highlight_ids() - pre_ids):
                try:
                    req = urllib.request.Request(f"{base}/api/v1/highlights/{hid}", method="DELETE")
                    urllib.request.urlopen(req, timeout=15).read()
                except Exception:  # noqa: BLE001
                    pass
    else:
        print("[SKIP] 需要浏览器与论文数据")

    # ---------------- ⑨ 原 PDF 栏划词：菜单只该有解释/复制，且不含任何说明文案 ----------------
    print()
    print("=" * 78)
    print("⑨ 原 PDF 栏划词菜单（只有解释/复制；不再有「不支持高亮」字样）")
    print("=" * 78)
    if browser and paper:
        dist = ROOT / "frontend" / "dist"
        probe_file = dist / "_pdf_probe.html"
        try:
            probe_file.write_text(
                PDF_MENU_PROBE.replace('__URL__', f"{base}/app/reader/{paper}?view=bilingual&left=page&nosse=1")
                .replace('__WAIT__', '2500'), encoding="utf-8")
            dom = render(browser, f"{base}/app/_pdf_probe.html", 30000, "1700,1100")
            m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
            data = None
            if m and m.group(1).strip() != "waiting":
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    data = None
            if not data or not data.get("spans"):
                failures += 1
                results.append({"kind": "pdf-menu", "path": "原 PDF 划词菜单", "ok": False,
                                "detail": f"没取到 PDF 文字层（{data}）"})
                print(f"[FAIL] 原 PDF 划词菜单     没取到 PDF 文字层（{data}）")
            else:
                checks = [
                    ("PDF 栏菜单只有解释/复制", bool(data.get("menu"))
                     and not data.get("swatches")
                     and any('解释' in b for b in (data.get("buttons") or []))
                     and any('复制' in b for b in (data.get("buttons") or []))
                     and not any('笔记' in b for b in (data.get("buttons") or [])),
                     f"菜单项 {data.get('buttons')}，色块 {data.get('swatches')} 个"),
                    ("已移除「不支持高亮」字样", data.get("hasHint") is False
                     and data.get("bodyHasHint") is False,
                     f"菜单里={data.get('hasHint')}，整页={data.get('bodyHasHint')}"),
                    ("选了 PDF 后译文仍可选",
                     bool((data.get("afterPdfSel") or {}).get("dragClass"))
                     and (data.get("afterPdfSel") or {}).get("zhUserSelect") == "none"
                     and (data.get("afterZhPress") or {}).get("dragClass") is False
                     and (data.get("afterZhPress") or {}).get("zhUserSelect") != "none"
                     and (data.get("afterZhPress") or {}).get("selLen") == 0,
                     f"PDF 选区后 {data.get('afterPdfSel')} → 在译文里按下后 {data.get('afterZhPress')}"),
                ]
                for label, ok, detail in checks:
                    failures += 0 if ok else 1
                    results.append({"kind": "pdf-menu", "path": label, "ok": ok, "detail": detail})
                    print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
        finally:
            probe_file.unlink(missing_ok=True)
    else:
        print("[SKIP] 需要浏览器与论文数据")

    # ---------------- ⑩ 论文打开状态：按论文保存 + 切标签还原 ----------------
    print()
    print("=" * 78)
    print("⑩ 打开状态（对照 / 原 PDF / 译文在左）：按论文保存、切标签与重开都能还原")
    print("=" * 78)
    if browser and paper:
        def put_progress_local(payload: dict) -> dict:      # noqa: D401 - 局部简写
            return put_progress(base, paper, payload)

        dist = ROOT / "frontend" / "dist"
        probe_file = dist / "_view_probe.html"
        try:
            # ① 服务端那一份：写进去 → 不带任何 query 打开 → 界面必须就是那个状态
            #    （含界面状态：侧栏收起 / 字号=大 / 原 PDF 缩放 150%；参考文献恒显示，开关已移除）
            got = put_progress_local({"view_mode": "bilingual", "pair_order": "zh_left",
                                      "original_mode": "page", "aside_open": False,
                                      "text_size": "lg", "show_refs": True, "zoom": 1.5})
            results.append({"kind": "view-state", "path": "进度接口回读", "ok": True,
                            "detail": f"original_mode={got.get('original_mode')} "
                                      f"pair_order={got.get('pair_order')} "
                                      f"view_mode={got.get('view_mode')} "
                                      f"aside={got.get('aside_open')} size={got.get('text_size')} "
                                      f"refs={got.get('show_refs')} zoom={got.get('zoom')}"})
            print(f"[PASS] 进度接口回读        original_mode={got.get('original_mode')} "
                  f"pair_order={got.get('pair_order')} view_mode={got.get('view_mode')} "
                  f"aside={got.get('aside_open')} size={got.get('text_size')} "
                  f"refs={got.get('show_refs')} zoom={got.get('zoom')}")
            dom_bare = render(browser, f"{base}/app/reader/{paper}?nosse=1", 22000, DESKTOP)
            checks = [
                ("重开时还原「原 PDF + 译文在左」",
                 "pg-pair" in dom_bare and "image-right" in dom_bare and "col-image" in dom_bare,
                 "不带 query 打开 → " + ("左侧原 PDF、译文在右变体（image-right）都渲染了"
                                        if "image-right" in dom_bare else "没看到 pg-pair/image-right")),
                ("对照方向也还原", "image-right" in dom_bare and "image-left" not in dom_bare.split("pg-pair")[1][:200],
                 "译文在左 → .pg-pair.image-right"),
            ]
            # ② 标签条那一份：点三个开关 → URL query 必须带上三个值（切标签靠它还原）
            probe_file.write_text(
                VIEW_STATE_PROBE.replace('__URL__', f"{base}/app/reader/{paper}?nosse=1")
                .replace('__WAIT__', '4500'), encoding="utf-8")
            dom = render(browser, f"{base}/app/_view_probe.html", 30000, "1700,1100")
            m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
            data = None
            if m and m.group(1).strip() != "waiting":
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    data = None
            if not data:
                checks.append(("开关写进 URL（切标签还原用）", False, "探针没取到结果"))
            else:
                ui = (data.get("before") or {}).get("ui") or {}
                checks.append((
                    "界面状态按论文还原（侧栏/字号/缩放）+ 参考文献恒显示",
                    ui.get("asideHidden") is True and ui.get("font") == "大"
                    and ui.get("refsToggle") is False and (ui.get("refRows") or 0) > 0
                    and ui.get("zoomLabel") == "150%",
                    f"打开时：侧栏收起={ui.get('asideHidden')} 字号={ui.get('font')} "
                    f"参考文献开关={ui.get('refsToggle')} 参考文献行={ui.get('refRows')} "
                    f"缩放={ui.get('zoomLabel')}"))
                q = str((data.get("after") or {}).get("search") or "")
                checks.append((
                    "开关写进 URL（切标签还原用）",
                    "view=" in q and "left=page" in q and "order=zh_left" in q,
                    f"点完「原 PDF + 对照 + 译文在左」后 URL = {q or '(空)'}（点中情况：{data.get('clicked')}）"))
                checks.append((
                    "URL 里的状态能直接还原",
                    bool((data.get("dom") or {}).get("imageRight"))
                    and bool((data.get("dom") or {}).get("colImage")),
                    f"DOM：{data.get('dom')}"))
            for label, ok, detail in checks:
                failures += 0 if ok else 1
                results.append({"kind": "view-state", "path": label, "ok": ok, "detail": detail})
                print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
        finally:
            probe_file.unlink(missing_ok=True)
    else:
        print("[SKIP] 需要浏览器与论文数据")

    # 全部跑完：把这篇论文的打开状态还原成**跑之前**的样子
    # （路由用例 / ⑤ 标签用例 / ⑩ 都会用不同视图打开它，阅读器会把状态存回去）
    if progress_snapshot and paper:
        try:
            put_progress(base, paper, {k: progress_snapshot.get(k) for k in (
                "last_block_id", "last_page", "percent", "view_mode",
                "pair_order", "original_mode", "scroll_ratio")})
            print(f"         （已把打开状态还原成跑之前：{progress_snapshot.get('view_mode')}/"
                  f"{progress_snapshot.get('original_mode')}/"
                  f"{progress_snapshot.get('pair_order')}）")
        except Exception:  # noqa: BLE001
            pass

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

