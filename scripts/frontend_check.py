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
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

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
     "markers": ["side-search", "ss-chip", "ss-hit", "ss-snippet", "清空搜索", "class=\"srch\""],
     "absent": ["search-compact", "ss-mode", "ss-scope", "ss-sem-bar"]},
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
    ("/api/v1/papers/{paper}/formulas", 200, "公式（译文优先，未翻译页用原文兜底）"),
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
  /* 顶栏把「原文栏呈现 / 文字列侧 / 左右顺序 / 字号 / 缩放」收进了弹出面板
   * （`.cap-panel`，用 `v-show` 常挂载）：真人要先点开面板才够得着里面的按钮，
   * 这里照做 —— 面板关着时元素虽然能被 `click()` 点到，但那不是用户路径。 */
  const openPanelFor = (b) => {
    if (!b || !b.closest) return;
    const panel = b.closest('.cap-panel');
    if (!panel || getComputedStyle(panel).display !== 'none') return;
    const which = panel.getAttribute('data-cap-panel-body');
    const trigger = d.querySelector(`.reader-toolbar [data-cap-panel="${which}"]`);
    if (trigger) trigger.click();
  };
  const clickText = (txt) => {
    const b = [...d.querySelectorAll('.reader-toolbar button')]
      .find((x) => x.textContent.trim() === txt);
    openPanelFor(b);
    if (b) b.click();
    return !!b;
  };
  const clickTitle = (t) => {
    const b = [...d.querySelectorAll('.reader-toolbar button')].find((x) => x.title === t);
    openPanelFor(b);
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
    // ⓪ 卡片尺寸（用户口径 2026-09-28："阅读器正文里的插图卡片缩小"）：
    //    与 Markdown 视图 / 知识库的插图同一口径（560×420，含 6px 白边）。
    //    收小的前提是"点开能看原始尺寸"—— 所以下面还要量弹窗里那张图的**原始像素宽**。
    const cardImg = fig && fig.querySelector('img');
    const cr = cardImg ? cardImg.getBoundingClientRect() : null;
    res.card = cr ? { w: Math.round(cr.width), h: Math.round(cr.height),
                      over: cr.width > 568 || cr.height > 428 } : null;
    // ⓪b 整幅大图（占页面 ≥25%）：允许放大到 880×800 —— 用户口径"占比很大则允许显示更大"。
    //     这一条在**含大图的论文**上量（见 `find_big_figure_paper` / ⑥b）。
    const largeImg = d.querySelector('.row-figure.large img');
    const lr = largeImg ? largeImg.getBoundingClientRect() : null;
    res.large = lr ? { w: Math.round(lr.width), h: Math.round(lr.height) } : null;
    res.largeCards = d.querySelectorAll('.row-figure.large').length;
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
    res.modalNat = modal.querySelector('img') ? modal.querySelector('img').naturalWidth : 0;
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
      // ⚠️ 要选**含公式的那一侧**，不能拿 `fRow.querySelector('.rr-body')`（DOM 里第一个）。
      // 一行有两个 `.rr-body`（原文 / 译文），左右顺序由 `pair_order` 决定，而公式常常只在
      // 一侧（实测 turboquant p001-03：原文里是 `≈2.7`，译文那侧没有公式）—— 选错侧就标了个
      // 没有公式的段落，断言"整块包住 katex"必然为假（本项长期红就是这个原因，不是产品 bug）。
      const katexEl = fRow.querySelector('.rr-body .katex');
      const body = katexEl.closest('.rr-body');
      res.formulaClick = await selectElement(body, 'hl-yellow');
      res.formula = {
        wrapped: !!fRow.querySelector('mark.hl > .katex, mark.hl > .katex-display'),
        katexIntact: !!fRow.querySelector('.katex'),
        marks: fRow.querySelectorAll('mark.hl').length,
        // 失败时要一眼看出"选中的是哪一行、哪一侧、那一侧到底有没有公式"
        rowId: fRow.getAttribute('data-row-id'),
        bodySide: (body.closest('.rrow') || {}).className || '',
        bodyHasKatex: !!body.querySelector('.katex'),
        katexInRow: fRow.querySelectorAll('.katex').length,
        markChildren: [...fRow.querySelectorAll('mark.hl')]
          .map((m) => (m.firstElementChild
            ? m.firstElementChild.tagName.toLowerCase() + '.' + m.firstElementChild.className
            : 'text')),
      };
      res.formulaIds = [...fRow.querySelectorAll('mark.hl[data-hl]')]
        .map((m) => m.getAttribute('data-hl'));
    }
    flush();

    // ③b 多行**显示公式**：底色必须覆盖整块，但**不能铺满整行**、也不能压住公式编号 (1)(2)。
    //     回归点一：`mark` 以前套在内层行内 `.katex` 上，而 `.katex-display` 是块级 ——
    //     浏览器只把底色画在内层那一条行高上，三行公式看起来"只有中间一行被标黄"（用户报过）。
    //     回归点二：底色画在**外壳** `mark.hl.block` 上时，外壳宽度默认铺满整行 →
    //     标一张行间公式变成整行涂色；而把外壳收窄成 fit-content 又会把 KaTeX 的编号
    //     `.tag`（绝对定位在 `.katex-html` 的 right:0）拽到公式上 → 编号与公式重叠。
    //     所以这里**优先挑带编号的显示公式**，把"编号不被压住"也一起断言。
    const inkOf = (k) => {
      const bs = [...k.querySelectorAll('.katex-html > .base')].map((b) => b.getBoundingClientRect());
      if (!bs.length) return null;
      return { w: Math.max(...bs.map((b) => b.right)) - Math.min(...bs.map((b) => b.left)),
               h: Math.max(...bs.map((b) => b.bottom)) - Math.min(...bs.map((b) => b.top)),
               left: Math.min(...bs.map((b) => b.left)),
               right: Math.max(...bs.map((b) => b.right)) };
    };
    const DISP = [...d.querySelectorAll('[data-row-id] .rr-body .katex-display')]
      .filter((k) => k.getBoundingClientRect().height > 40);
    const tagged = DISP.filter((k) => k.querySelector('.tag'));
    // 优先挑"高亮前编号就与公式分开"的那种（`tag.left >= 墨迹右沿 + 2`）：这样"编号不被压住"这条
    // 断言才是真的在测东西。公式本来就跟整栏一样宽时，KaTeX 自己就把编号压在公式上
    // （那是排版本身的问题，不是高亮造成的），退而求其次按"至少窄 40px"挑，再不行随便给一个。
    const tagGap = (k) => {
      const ink = inkOf(k);
      const tag = k.querySelector('.tag')?.getBoundingClientRect() ?? null;
      return ink && tag ? tag.left - ink.right : -1;
    };
    const dispEl = tagged.find((k) => tagGap(k) >= 2)
      // ② 没带编号但明显比整栏窄的（≥60px）：注入一个 KaTeX 同款编号来测（见 `tagInjected`）——
      //    实测真实页面里"高亮前编号就与公式分开"的很少，不注入这条回归就永远测不到
      || DISP.find((k) => {
        const ink = inkOf(k);
        return !k.querySelector('.tag') && ink
          && ink.w <= k.getBoundingClientRect().width - 60;
      })
      || tagged.find((k) => {
        const ink = inkOf(k);
        return ink && ink.w <= k.getBoundingClientRect().width - 40;
      }) || tagged[0] || DISP[0];
    res.hasDisp = !!dispEl;
    if (dispEl) {
      const dRow = dispEl.closest('[data-row-id]');
      const before = new Set([...d.querySelectorAll('mark.hl[data-hl]')]
        .map((m) => m.getAttribute('data-hl')));
      /* ⚠️ 带编号的行间公式在真实 DOM 里很少（实测两篇都没有"高亮前编号就与公式分开"的那种），
       * 而"编号被色带压住"正是用户 2026-09-28 截图报的那个 bug —— 不能只靠运气测到。
       * 这里**注入一个 KaTeX 同款编号**（同样的 `.katex-html > .tag` 结构与 CSS 选择器），
       * 把这条回归固定住；页面本身是自检的临时 iframe，注入不会留到产品里。 */
      const htmlEl = dispEl.querySelector('.katex-html');
      const ink0 = inkOf(dispEl);
      let tagInjected = false;
      if (!dispEl.querySelector('.tag') && htmlEl && ink0
          && dispEl.getBoundingClientRect().width - ink0.w >= 60) {
        const tag = d.createElement('span');
        tag.className = 'tag';
        tag.textContent = '(9)';
        htmlEl.appendChild(tag);
        tagInjected = true;
      }
      res.tagInjected = tagInjected;
      // 高亮**之前**编号是否本来就与公式分开（分开 → 高亮之后也必须分开）
      const tagBefore = dispEl.querySelector('.tag')?.getBoundingClientRect() ?? null;
      const inkBefore = inkOf(dispEl);      res.dispClick = await selectElement(dispEl, 'hl-green');
      const mk = [...dRow.querySelectorAll('mark.hl')].find((m) => m.classList.contains('block'));
      const box = (el) => (el ? el.getBoundingClientRect() : null);
      // ⚠️ 量的是**真正被涂色的元素**（`.base` / `.tag`），不是外壳 mark：外壳是透明的整行盒子，
      // 拿它量高度会"看起来覆盖整块"，量宽度又永远等于整行 —— 两个 bug 都测不出来。
      const painted = mk ? [...mk.querySelectorAll('*')].find((el) => {
        const bg = w.getComputedStyle(el).backgroundColor || '';
        return bg && bg !== 'transparent' && !/^rgba?\(0,\s*0,\s*0,\s*0\)$/.test(bg);
      }) : null;
      const bandBox = box(painted);
      const dispBox = box(dispEl);
      const bodyBox = box(dispEl.closest('.rr-body'));
      const tagBox = box(dispEl.querySelector('.tag'));
      const kBox = box(dispEl.querySelector('.katex'));
      const htmlBox = box(dispEl.querySelector('.katex-html'));
      const bases = [...dispEl.querySelectorAll('.katex-html > .base')];
      const boxes = bases.map((b) => b.getBoundingClientRect());
      // 公式自己的外接框 = 各"不可断行单元"的并集（不能只看最高的那个：多行公式要按并集算）
      const inkH = boxes.length
        ? Math.max(...boxes.map((b) => b.bottom)) - Math.min(...boxes.map((b) => b.top)) : 0;
      const inkW = boxes.length
        ? Math.max(...boxes.map((b) => b.right)) - Math.min(...boxes.map((b) => b.left)) : 0;
      const cx = (b) => (b.left + b.right) / 2;
      res.disp = {
        hasBlockClass: !!mk,
        hasTag: !!tagBox,
        rowId: dRow.getAttribute('data-row-id'),
        // 诊断用：一眼看出"公式块高 47 但色带只有 37"这类差在哪
        boxes: {
          disp: dispBox && [Math.round(dispBox.x), Math.round(dispBox.y),
                            Math.round(dispBox.width), Math.round(dispBox.height)],
          html: htmlBox && Math.round(htmlBox.width),
          katex: kBox && Math.round(kBox.width),
          body: bodyBox && Math.round(bodyBox.width),
          bases: boxes.map((b) => Math.round(b.height)),
          ink: [Math.round(inkW), Math.round(inkH)],
          band: bandBox && [Math.round(bandBox.x), Math.round(bandBox.y),
                            Math.round(bandBox.width), Math.round(bandBox.height)],
        },
        // 底色画在公式自己的盒子里（`.katex` 之内），不是外壳 / 整行的 `.katex-display`
        bandInsideFormula: !!painted && painted !== mk
          && !!mk.querySelector('.katex')?.contains(painted),
        // 色带大小必须**贴着公式外接框**（多截一点就是"铺满整行"，少截就是"只黄一行"）
        bandMatchesInk: !!bandBox && inkW > 0 && inkH > 0
          && Math.abs(bandBox.width - inkW) <= 6 && Math.abs(bandBox.height - inkH) <= 6,
        markH: bandBox ? Math.round(bandBox.height) : 0,
        dispH: dispBox ? Math.round(dispBox.height) : 0,
        baseMaxH: Math.round(inkH),
        // 覆盖判据按**公式墨迹的并集高度**（旧 bug 是"只黄中间一行"，那时色带只有一条行高）；
        // `.katex-display` 的高度可能含一条横向滚动条（差 ~10px），拿它当基准会误判。
        covers: !!bandBox && inkH > 0 && bandBox.height >= inkH * 0.95,
        bandW: bandBox ? Math.round(bandBox.width) : 0,
        lineW: kBox ? Math.round(kBox.width) : 0,
        centered: !!bandBox && !!bodyBox && Math.abs(cx(bandBox) - cx(bodyBox)) <= 2,
        // 编号 (1)：高亮之前它就不压在公式上时，高亮之后也必须不压（色带只该包住公式本体）；
        // 而且仍在整行最右边（论文里的位置）。公式比整栏还宽那种（KaTeX 自己就压着）不苛责。
        preTagClear: !!(tagBefore && inkBefore && tagBefore.left >= inkBefore.right - 1),
        tagClear: !!(tagBox && bandBox && tagBox.left >= bandBox.right - 1),
        tagOverlap: !!tagBox && !!bandBox
          && tagBox.left < bandBox.right - 1 && tagBox.right > bandBox.left + 1,
        tagAtRight: !!tagBox && !!bodyBox && Math.abs(tagBox.right - bodyBox.right) <= 4,
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

    // ③ 插图尺寸：用户口径"图片在 markdown 中不要那么大"。
    //    CSS 把宽高都收在 560 × 420 以内（base.css 的 `.md img`），并且**点一下能开大图**（原始尺寸）——
    //    收小是敢收的前提：没有放大入口，收小就等于看不清细节。
    const imgs = [...d.querySelectorAll('.md-doc img')]
      .filter((i) => i.getBoundingClientRect().width > 4);
    const boxes = imgs.map((i) => i.getBoundingClientRect());
    res.images = {
      count: imgs.length,
      maxW: boxes.length ? Math.round(Math.max(...boxes.map((b) => b.width))) : 0,
      maxH: boxes.length ? Math.round(Math.max(...boxes.map((b) => b.height))) : 0,
      over: boxes.filter((b) => b.width > 568 || b.height > 428).length,
      docW: Math.round((doc ? doc.getBoundingClientRect().width : 0)),
      alt: imgs.length ? (imgs[0].getAttribute('alt') || '').slice(0, 40) : '',
    };
    if (imgs.length) {
      imgs[0].scrollIntoView({ block: 'center' });
      await sleep(250);
      imgs[0].dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));
      await sleep(400);
      const modal = [...d.querySelectorAll('.modal')].find((x) => x.querySelector('.modal-title'));
      const mi = modal && modal.querySelector('img');
      res.imageZoom = {
        open: !!modal,
        hasImg: !!mi,
        title: modal ? (modal.querySelector('.modal-title').textContent || '').trim().slice(0, 40) : '',
        src: mi ? (mi.getAttribute('src') || '').split('/').pop() : '',
        natW: mi ? mi.naturalWidth : 0,
      };
    }
    out(res);
  } catch (err) { res.error = String(err); out(res); }
});
</script>
"""


# 表格渲染探针（用户口径：markdown 表格要"更像论文"）。
#
# 论文表格 = booktabs 的**三线表**（`\toprule` / `\midrule` / `\bottomrule`）：
# 没有竖线、没有逐行横线，数字列右对齐（表头压在正中），数据集分组之间一条细线（`\cmidrule`）。
#
# 回归点：读者行里的表格以前是「每行一条 border-bottom + 所有单元格右对齐」——一眼是网页表格；
#         而 base.css 里那套又是 `border: 1px solid` 的方格表（还有竖线）。三处（阅读器 / Markdown
#         视图 / AI 解读）各写一套，长得都不一样，现在统一到 base.css 的 `.md table`。
# 判据全部量**计算样式**（不是类名）：换实现只要还是三线表就仍然通过。
# `.tbl-wrap` 是宽表的横向滚动容器 —— 缺了它宽表会把**整页**顶宽（另有一条永久回归项）。
TABLE_PROBE = """<!doctype html>
<meta charset="utf-8"><title>markdown table probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:600px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
/* 与前端 `decorateTables` 同一判据：整列 ≥75% 的单元格是纯数字才算数字列 */
const NUM = /^[+\\-−]?\\d[\\d.,]*(?:\\s*[%‰])?$/;
f.addEventListener('load', async () => {
  const w = f.contentWindow, d = f.contentDocument;
  const res = {};
  try {
    await sleep(__WAIT__);
    const cs = (el, prop) => w.getComputedStyle(el)[prop];
    const px = (el, prop) => parseFloat(cs(el, prop)) || 0;
    const txt = (el) => ((el && el.textContent) || '').trim();
    /* 挑一张"真表格"：列多、行多（Table 1 那种）；找不到就退而求其次 */
    const cands = [...d.querySelectorAll('.t-table table, .md table')].map((t) => ({
      t, cols: t.querySelectorAll('thead th').length, rows: t.querySelectorAll('tbody tr').length,
    })).filter((c) => c.cols >= 3 && c.rows >= 2)
      .sort((a, b) => (b.cols * b.rows) - (a.cols * a.rows));
    res.candidates = cands.length;
    if (!cands.length) { out(res); return; }
    const t = cands[0].t;
    const ths = [...t.querySelectorAll('thead th')];
    const trs = [...t.querySelectorAll('tbody tr')];
    const tds = (r) => [...r.querySelectorAll('td')];
    /* ① 竖线：表头 + 全部正文单元格的左右边框都是 0 */
    res.vertical = Math.max(0, ...[...ths, ...trs.flatMap(tds)].map(
      (el) => px(el, 'borderLeftWidth') + px(el, 'borderRightWidth')));
    /* ② 三条横线：表头顶线 / 表头下线 / 表体底线；中间的逐行横线必须没有 */
    res.topRule = px(ths[0], 'borderTopWidth');
    res.midRule = px(ths[0], 'borderBottomWidth');
    res.bottomRule = px(tds(trs[trs.length - 1])[0], 'borderBottomWidth');
    res.rowLines = trs.slice(0, -1).filter((r) => px(tds(r)[0], 'borderBottomWidth') > 0).length;
    res.ruleColor = cs(ths[0], 'borderTopColor');
    /* ③ 对齐：数字列右对齐 + 表头压正中；文字列左对齐；数字用等宽数字（等宽才好按位读） */
    const width = ths.length;
    const numeric = [];
    for (let c = 0; c < width; c += 1) {
      const vals = trs.map((r) => txt(tds(r)[c])).filter(Boolean);
      numeric.push(vals.length >= 2 && vals.filter((v) => NUM.test(v)).length / vals.length >= 0.75);
    }
    const numC = numeric.indexOf(true), txtC = numeric.indexOf(false);
    res.numCols = numeric.filter(Boolean).length;
    res.textCols = numeric.filter((v) => !v).length;
    res.numAlign = numC >= 0 ? cs(tds(trs[0])[numC] || ths[numC], 'textAlign') : '';
    res.numHeadAlign = numC >= 0 ? cs(ths[numC], 'textAlign') : '';
    res.txtAlign = txtC >= 0 ? cs(tds(trs[0])[txtC] || ths[txtC], 'textAlign') : '';
    res.tnum = numC >= 0 ? cs(tds(trs[0])[numC], 'fontVariantNumeric') : '';
    /* ④ 分组细线：第一列有空单元格 → 那是"数据集列"，非首行里该有一条 border-top */
    const firsts = trs.map((r) => txt(tds(r)[0]));
    res.grouping = firsts.some((v) => !v);
    res.groupLines = trs.slice(1).filter((r) => px(tds(r)[0], 'borderTopWidth') > 0).length;
    /* ⑤ 宽表不能顶宽整页 */
    res.wrap = !!t.closest('.tbl-wrap');
    res.pageScrollX = Math.round(d.documentElement.scrollWidth - w.innerWidth);
    res.wrapScroll = t.closest('.tbl-wrap')
      ? cs(t.closest('.tbl-wrap'), 'overflowX') : (t.closest('.rr-body') ? 'rr-body' : '');
    res.cols = width;
    res.rows = trs.length;
    res.tableW = Math.round(t.getBoundingClientRect().width);
    res.fontSize = cs(t, 'fontSize');
    res.rowH = Math.round(trs[0].getBoundingClientRect().height);
    res.headH = Math.round(ths[0].getBoundingClientRect().height);
    res.sample = ths.map((h) => txt(h)).join(' | ');
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
  /* 顶栏把「原文栏呈现 / 文字列侧 / 左右顺序 / 字号 / 缩放」收进了弹出面板（`.cap-panel`）：
   * 先点开面板再点里面的按钮 —— 与真人一致（面板是 `v-show` 常挂载，元素一直在 DOM 里）。 */
  const openPanelFor = (b) => {
    if (!b || !b.closest) return;
    const panel = b.closest('.cap-panel');
    if (!panel || getComputedStyle(panel).display !== 'none') return;
    const which = panel.getAttribute('data-cap-panel-body');
    const trigger = d.querySelector(`.reader-toolbar [data-cap-panel="${which}"]`);
    if (trigger) trigger.click();
  };
  const clickText = (txt) => {
    const b = [...d.querySelectorAll('.reader-toolbar button')]
      .find((x) => x.textContent.trim() === txt);
    openPanelFor(b);
    if (b) { b.click(); return true; }
    return false;
  };
  const clickTitle = (t) => {
    const b = [...d.querySelectorAll('.reader-toolbar button')].find((x) => x.title === t);
    openPanelFor(b);
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
      /* 顶栏三块玻璃（左=侧栏开关 / 中=主胶囊 / 右=更多）：**同高同线**是用户口径里的硬要求
       * —— 以前开关是 34px 的圆、胶囊 38px，还不在一行上。这里把 top/height 采回来比对。 */
      const glass = ['.cap-solo.left', '.reader-capsule', '.cap-solo.right']
        .map((s) => d.querySelector(s))
        .filter(Boolean)
        .map((e) => { const r = e.getBoundingClientRect();
                      return `${Math.round(r.top)}/${Math.round(r.height)}`; });
      const panels = d.querySelectorAll('.cap-panel').length;
      /* 侧栏开关：用户口径是"始终只有一枚"（圆形的那个）—— 历史上做成过"侧栏里一枚 +
       * 顶栏一枚"的互斥渲染，切换时会看到两枚交接。这里数一下 DOM 里到底有几枚。 */
      const toggles = d.querySelectorAll('[data-aside-toggle]').length;
      /* 标签页胶囊（顶栏）：用户口径 = **不再裁字**、与下方那枚页签胶囊同一种圆形。
       * 「裁字」不能看 scrollHeight（它只报行盒高）：要看**文字本身的自然高度** ——
       * Range.getBoundingClientRect() 量的是字体 ascent + descent（与 line-height 无关），
       * 一旦它高过元素的内容盒，.truncate 的 overflow: hidden 就把上下缘切掉了。 */
      const tabTitles = [...d.querySelectorAll('a.tab .tab-title')];
      let tabClip = 0, tabRatio = 0;
      tabTitles.forEach((el) => {
        const rg = d.createRange();
        rg.selectNodeContents(el);
        const natural = rg.getBoundingClientRect().height || 0;
        const box = el.clientHeight || 0;
        if (box && natural > box + 1) tabClip += 1;
        if (box) tabRatio = Math.max(tabRatio, natural / box);
      });
      const tabOne = d.querySelector('a.tab');
      const tabRect = tabOne ? tabOne.getBoundingClientRect() : null;
      const tabBar = {
        n: tabTitles.length,
        clip: tabClip,
        ratio: Math.round(tabRatio * 100) / 100,
        h: tabRect ? Math.round(tabRect.height) : 0,
        radius: tabOne ? parseFloat(getComputedStyle(tabOne).borderTopLeftRadius) || 0 : 0,
      };
      /* 侧栏头部那枚页签胶囊：用户口径 = **与左侧圆钮同高同线**（都是 38px）、
       * **汉字只在当前页显示**（其余页签只留图标；当前页是图标在上、文字在下并加粗）。
       * 采回两者的 top/height 比对，量当前页那枚是不是竖排两行，并数一共露出几枚文字。 */
      const stBox = d.querySelector('.side-tabs');
      const stSolo = d.querySelector('.cap-solo.left');
      const stRect = stBox ? stBox.getBoundingClientRect() : null;
      const soloRect = stSolo ? stSolo.getBoundingClientRect() : null;
      const stItem = d.querySelector('.side-tab.active') || d.querySelector('.side-tab');
      const stIcon = stItem ? stItem.querySelector('svg') : null;
      const stLabel = stItem ? stItem.querySelector('.side-label') : null;
      const stIconRect = stIcon ? stIcon.getBoundingClientRect() : null;
      const stLabelRect = stLabel ? stLabel.getBoundingClientRect() : null;
      const sideTabs = {
        top: stRect ? Math.round(stRect.top) : -1,
        h: stRect ? Math.round(stRect.height) : -1,
        soloTop: soloRect ? Math.round(soloRect.top) : -1,
        soloH: soloRect ? Math.round(soloRect.height) : -1,
        stacked: !!(stIconRect && stLabelRect && stIconRect.bottom <= stLabelRect.top + 0.5),
        labels: [...d.querySelectorAll('.side-tab .side-label')]
          .filter((e) => e.offsetParent !== null).length,
        border: stBox ? getComputedStyle(stBox).borderTopStyle : '',
        flat: stBox
          ? !/gradient/.test(getComputedStyle(stBox).backgroundImage)
            && getComputedStyle(stBox).backdropFilter === 'none'
          : false,
        inset: stBox ? /inset/.test(getComputedStyle(stBox).boxShadow) : false,
      };
      /* 毛玻璃上的**内阴影**（inset 0 1px 0 那道顶部高光）按用户口径去掉了：
       * 浅色下几乎看不见，深色下反而像一圈发光边。这里数一下还有几块玻璃带 inset。 */
      const insets = ['.cap-solo.left', '.reader-capsule', '.cap-panel', '.side-tabs']
        .map((s) => d.querySelector(s)).filter(Boolean)
        .filter((e) => /inset/.test(getComputedStyle(e).boxShadow)).length;
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
        glass,
        glassKeys: [...new Set(glass)],
        panels,
        toggles,
        tabBar,
        sideTabs,
        insets,
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


# 导入弹窗探针（本轮新增：三个「切换开关」+ 解析引擎二选一）。
#
# 回归点：
# 1. 三个复选框换成了 `<Switch>`，但它**里面必须是真实 `<input type="checkbox" role="switch">`**
#    （键盘 Space / 点文字即切换 / AT 语义都靠它）—— 只靠一个 div + click 的自绘开关会在
#    "看起来一样"的同时把这三样全丢掉，所以这里逐项断言标签、角色与"点了真的变"。
# 2. 解析引擎必须是**互斥二选一**，选了 MinerU 后「深度图片搜索」要置灰，
#    并且没配 token 时要出现"去哪里配"的可执行提示（而不是等导入失败才说）。
IMPORT_DIALOG_PROBE = """<!doctype html>
<meta charset="utf-8"><title>import dialog probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:620px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
/* `__URL__` 是前端入口（同源才能读 iframe 的 DOM）；真正的目标页由 query 传进来 */
const target = new URLSearchParams(location.search).get('page') || '/library';
let started = false;
f.addEventListener('load', async () => {
  if (started) return;
  started = true;
  let w = f.contentWindow, d = f.contentDocument;
  const res = {};
  try {
    /* 第一跳：先让入口页把外壳（顶栏 / 标签条 / localStorage）建起来 */
    await sleep(900);
    w.location.replace(new URL('__URL__').origin + '/app' + target + '?nosse=1');
    await sleep(__WAIT__);
    w = f.contentWindow; d = f.contentDocument;

    const openBtn = [...d.querySelectorAll('.page-actions button')]
      .find((b) => (b.textContent || '').includes('导入 PDF'));
    res.opened = !!openBtn;
    if (openBtn) openBtn.click();
    // 弹窗打开时会去拉 /mineru/config：留一点时间让请求落地，否则量到的可能是"还没拿到"的那一帧
    await sleep(1200);

    const modals = [...d.querySelectorAll('.modal')];
    const modal = modals[modals.length - 1];
    res.modal = !!modal;
    if (!modal) { out(res); return; }

    /* 先显式切回「本地解析」再量下面那三个开关。
     * 用户口径（2026-09-28）：这三个开关**只在本地解析时显示**，而弹窗的初始引擎来自
     * 设置页的默认值 —— 不先归一，本机把默认引擎设成 MinerU 时这组断言就会整片红
     * （那时开关按设计就不在）。归一之后，这组断言就与"本机默认引擎是什么"无关了。 */
    const localBtn = [...modal.querySelectorAll('.engine-opt')]
      .find((b) => (b.textContent || '').includes('本地解析'));
    if (localBtn && !localBtn.classList.contains('active')) {
      localBtn.click();
      await sleep(300);
    }
    res.localSelected = localBtn ? localBtn.classList.contains('active') : null;
    /* 切回本地解析之后，"在线解析参数"区必须**不在**（它只属于在线那条路） */
    res.onlineGroupWhenLocal = !!modal.querySelector('.online-params');

    const rows = [...modal.querySelectorAll('.switch-row')];
    res.rowCount = rows.length;
    /* 按**文案**认行（不按下标）：行序变了不该让断言跟着一起错位 */
    const rowOf = (txt) => rows.find((r) => (r.textContent || '').includes(txt)) || null;

    /* ⚠️ 为什么量之前要"摘下来再放回去"：
     * headless 跑在 `--virtual-time-budget` 下时，**已经在上一帧画过的节点不会因为 JS 改状态
     * 而重算样式** —— 实测：把 checkbox 的 `checked` 设成 true（`:checked` 也确实命中）、
     * 甚至给它写 `style="background:… !important"`，`getComputedStyle` 都还回旧值；
     * 同一棵子树"摘下来再插回去"立刻就是新值（`rgb(15,123,138)`）。
     * 这是**测试环境**的重算问题，不是产品行为（真人浏览器里点击后样式立刻就变），
     * 所以修在探针里：量开关外观前先把这一行重新挂载一次。 */
    const remount = (el) => {
      const parent = el.parentElement, next = el.nextElementSibling;
      el.remove();
      parent.insertBefore(el, next);
    };

    const snap = (row, { fresh = false } = {}) => {
      if (!row) return null;
      const cb = row.querySelector('input');
      const track = row.querySelector('.switch-track');
      const knob = row.querySelector('.switch-knob');
      if (!cb || !track || !knob) return { broken: true };
      if (fresh) remount(row);
      const cs = w.getComputedStyle(track);
      const ks = w.getComputedStyle(knob);
      const csCb = w.getComputedStyle(cb);
      return {
        // 真实 checkbox + 开关语义 + sr-only（视觉藏起、仍可聚焦、仍被读屏读到）
        type: cb.getAttribute('type'),
        role: cb.getAttribute('role'),
        srOnly: cb.classList.contains('sr-only'),
        visuallyHidden: csCb.position === 'absolute' && parseFloat(csCb.width) <= 2
          && parseFloat(csCb.height) <= 2,
        /* 记下真实的盒子。全局 `input[type=checkbox]`（15×15）与 `.sr-only`（1×1）同权重、
         * 后者会被顶掉 —— `Switch.vue` 里用 `.switch-row > input.sr-only` 明确压住，
         * 所以这里量到的应当是 `absolute/1px/1px`；一旦有人删了那条规则，数字会变成 15px。 */
        cbBox: `${csCb.position}/${csCb.width}/${csCb.height}/${csCb.clip}`,
        disabled: cb.disabled,
        checked: cb.checked,
        // 轨道 36×20、滑块 16、圆角 pill
        trackW: Math.round(parseFloat(cs.width)),
        trackH: Math.round(parseFloat(cs.height)),
        knobW: Math.round(parseFloat(ks.width)),
        radius: cs.borderRadius,
        // 轨道当前底色（开 = `--accent`、关 = `--bg-sunken`）
        bg: cs.backgroundColor,
        // 滑块位置：关 = 贴左（2px）、开 = 右移 16px（视觉上真的动了，而不只是轨道换色）
        knobX: Math.round(knob.getBoundingClientRect().left - track.getBoundingClientRect().left),
        // 滑块自己的过渡必须是 140ms（轨道换色走全站 `--dur`，两者分开量）
        knobTransition: ks.transitionDuration,
        trackTransition: cs.transitionDuration,
        text: (row.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
      };
    };

    const rowAuto = rowOf('导入后立即解析');
    const rowCap = rowOf('图片是否包含图注');
    const rowDeep = rowOf('深度图片搜索');
    res.before = { auto: snap(rowAuto), cap: snap(rowCap), deep: snap(rowDeep) };
    // 说明文案必须还在（语义别在换组件时被顺手删掉）
    res.notes = {
      cap: !!rowCap && rowCap.textContent.includes('只裁图体本身'),
      deep: !!rowDeep && rowDeep.textContent.includes('只出图注锚定的图'),
    };
    // 默认态的外观：自动解析默认开（突出强调色是否真的生效）、插图开关默认关
    res.skin = {
      autoOn: snap(rowAuto, { fresh: true }),
      capOff: snap(rowCap, { fresh: true }),
    };

    /* 点整行（label）→ 原生行为应当勾上；再点一次应当还原。
     * 两次点击都要量：只测"能开"会漏掉"emit 丢了值 → 父组件拿到 undefined → 永远关着"这类 bug
     * （本轮真踩过：DOM 的 checked 翻了、`--accent` 没上来，看着像开关点不动）。 */
    if (rowCap) {
      const was = !!rowCap.querySelector('input').checked;
      rowCap.click();
      await sleep(450);
      const on = snap(rowCap, { fresh: true });
      rowCap.click();
      await sleep(450);
      const off = snap(rowCap, { fresh: true });
      res.toggle = { was, on: on.checked, off: off.checked,
                     onBg: on.bg, offBg: off.bg,
                     onKnob: on.knobX, offKnob: off.knobX };
    }

    /* `--accent` 的解析值：不写死色值 —— 令牌改色时这条断言不该跟着变红。
     * （临时造一个元素把这支令牌用起来，让浏览器自己解析。） */
    const probeEl = d.createElement('span');
    probeEl.style.backgroundColor = 'var(--accent)';
    (modal || d.body).appendChild(probeEl);
    res.accent = w.getComputedStyle(probeEl).backgroundColor;
    probeEl.remove();

    /* 键盘 Space：这就是"为什么里面要放真实 checkbox"的兑现点。
     * ⚠️ 合成键盘事件本身不会让浏览器替我们翻转 checked（那是真人按键才有的默认动作），
     * 所以这里只能断言"这个隐藏 checkbox **拿得到焦点**（Tab 能到、读屏能读到）"——
     * 它一旦换成 div + click（`tabindex` 忘写、或 pointer-events:none 把焦点也弄丢），
     * 这一条就会红。 */
    const focusProbe = (row) => {
      if (!row) return null;
      const cb = row.querySelector('input');
      try { cb.focus(); } catch (e) { return 'focus 抛错：' + e.message; }
      return { focused: d.activeElement === cb, role: cb.getAttribute('role'),
               tabIndex: cb.tabIndex };
    };
    res.keyboard = { auto: focusProbe(rowAuto), deep: focusProbe(rowDeep) };

    /* 解析引擎：互斥二选一（role=radio + aria-checked），两个按钮里必须各有本地/在线的说明 */
    const engineRow = modal.querySelector('.engine-row');
    const opts = engineRow ? [...engineRow.querySelectorAll('.engine-opt')] : [];
    res.engine = {
      count: opts.length,
      role: engineRow ? engineRow.getAttribute('role') : '',
      labels: opts.map((b) => (b.querySelector('.engine-name') || {}).textContent || ''),
      roles: opts.map((b) => b.getAttribute('role')),
      checked: opts.map((b) => b.getAttribute('aria-checked')),
      notes: opts.map((b) => ((b.querySelector('.engine-note') || {}).textContent || '').trim()),
      active: opts.map((b) => b.classList.contains('active')),
    };

    const mineruBtn = opts.find((b) => (b.textContent || '').includes('MinerU'));
    res.engine.clicked = !!mineruBtn;
    if (mineruBtn) {
      mineruBtn.click();
      await sleep(400);
      const opts2 = [...modal.querySelectorAll('.engine-opt')];
      res.engine.after = {
        active: opts2.map((b) => b.classList.contains('active')),
        checked: opts2.map((b) => b.getAttribute('aria-checked')),
      };
      /* 用户口径（2026-09-28）：下面三个开关**只在本地解析时显示**。
       * ⚠️ 必须**重新查一次 DOM**：顶上那个 `rows` 是切换前抓的数组，
       * 里面还留着已被卸载的节点 —— 拿它去找"深度图片搜索"会找到一个游离元素，
       * 量出 `disabled=false`，于是"已隐藏"被误判成"还在"。
       * ⚠️ 还要**排除在线参数区里的开关**（强制 OCR / 公式 / 表格 也是 `.switch-row`）：
       * 不排除就会数出 3 个，把"本地那三个已经消失"误判成"还在"。 */
      const rowsAfter = [...modal.querySelectorAll('.switch-row')]
        .filter((r) => !r.closest('.online-params'));
      res.engine.switchRowsAfter = rowsAfter.length;
      const deep2 = rowsAfter.find((r) => (r.textContent || '').includes('深度图片搜索')) || null;
      res.engine.deepDisabled = deep2 ? snap(deep2).disabled : null;
      res.engine.deepChecked = deep2 ? snap(deep2).checked : null;
      res.engine.deepNote = deep2 ? (deep2.textContent || '') : '';
      const warn = modal.querySelector('.engine-warn');
      res.engine.warn = warn ? (warn.textContent || '').replace(/\\s+/g, ' ').trim() : '';

      /* 在线解析参数区（模型 / OCR 语言 / 强制 OCR / 公式 / 表格 / 页码范围）：
       * 只应在"在线解析"选中时出现，且每个旋钮都要真的可用（有选项、有默认值、有示例提示）。 */
      const op = modal.querySelector('.online-params');
      res.engine.onlineGroup = !!op;
      if (op) {
        const modelSel = op.querySelector('[data-mineru-model]');
        const langSel = op.querySelector('[data-mineru-language]');
        const ranges = op.querySelector('[data-mineru-ranges]');
        const opRows = [...op.querySelectorAll('.switch-row')];
        res.engine.opModelOptions = modelSel
          ? [...modelSel.querySelectorAll('option')].map((o) => o.value) : [];
        res.engine.opModelValue = modelSel ? modelSel.value : '';
        res.engine.opLangOptions = langSel
          ? [...langSel.querySelectorAll('option')].map((o) => o.value) : [];
        res.engine.opRangesPlaceholder = ranges ? (ranges.placeholder || '') : '';
        res.engine.opSwitchTexts = opRows.map((r) => {
          const t = r.querySelector('.opt-text');
          return t ? (t.textContent || '').trim() : '';
        });
        res.engine.opSwitchChecked = opRows.map((r) => {
          const i = r.querySelector('input');
          return i ? !!i.checked : null;
        });
      }
    }
    out(res);
  } catch (err) { res.error = String(err); out(res); }
});
</script>
"""


# 设置页「在线解析（MinerU）」分区探针（本轮新增）。
#
# 断言的是**这一区真的能用**（而不只是存在一段文案）：token 输入框是 password、
# 验证按钮在请求期间变「验证中…」且禁用、验证结论落到行内状态（有效/延迟/服务端消息）、
# 以及模型版本 / 语言 / 默认解析引擎三个下拉齐全。
MINERU_SECTION_PROBE = """<!doctype html>
<meta charset="utf-8"><title>mineru section probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:620px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
/* 同 `IMPORT_DIALOG_PROBE`：`__URL__` 是入口页，真正的目标页由 `?page=` 传入 */
const target = new URLSearchParams(location.search).get('page') || '/settings';
let started = false;
f.addEventListener('load', async () => {
  if (started) return;
  started = true;
  let w = f.contentWindow, d = f.contentDocument;
  const res = {};
  try {
    await sleep(900);
    w.location.replace(new URL('__URL__').origin + '/app' + target);
    await sleep(__WAIT__);
    w = f.contentWindow; d = f.contentDocument;

    const sec = d.getElementById('mineru');
    res.section = !!sec;
    if (!sec) { out(res); return; }
    res.title = (sec.querySelector('.sec-title') || {}).textContent || '';
    res.inNav = [...d.querySelectorAll('.sec-nav button')]
      .some((b) => (b.textContent || '').includes('在线解析'));

    const pwd = sec.querySelector('input[type="password"]');
    res.password = !!pwd;
    res.passwordPlaceholder = pwd ? pwd.getAttribute('placeholder') : '';
    res.passwordValue = pwd ? pwd.value : null;

    const btns = [...sec.querySelectorAll('button')];
    const byText = (t) => btns.find((b) => (b.textContent || '').includes(t)) || null;
    const verify = byText('验证');
    const save = byText('保存');
    const clear = byText('清除');
    res.buttons = btns.map((b) => (b.textContent || '').trim());
    res.hasSave = !!save;
    res.hasClear = !!clear;
    res.verifyLabel = verify ? (verify.textContent || '').trim() : '';
    res.verifyDisabled = verify ? verify.disabled : null;

    // 三个下拉：模型版本 / 语言 / 默认解析引擎（选项文案里带 pipeline 与 vlm）
    res.selects = [...sec.querySelectorAll('select')].map((s) => ({
      value: s.value,
      options: [...s.options].map((o) => o.value + '=' + o.textContent.trim()),
    }));
    res.modelOptions = res.selects.some((s) => s.options.some((o) => o.startsWith('pipeline='))
      && s.options.some((o) => o.startsWith('vlm=')));
    res.langOptions = res.selects.some((s) => s.options.some((o) => o.startsWith('en='))
      && s.options.some((o) => o.startsWith('ch=')));
    res.engineOptions = res.selects.some((s) => s.options.some((o) => o.startsWith('local='))
      && s.options.some((o) => o.startsWith('mineru=')));
    res.hasTokenFile = (sec.textContent || '').includes('token 文件');
    res.has90Days = (sec.textContent || '').includes('90 天');
    res.hasToken = !verify || res.verifyDisabled !== true;

    /* ① 「验证」的忙碌态：请求期间必须是「验证中…」且禁用（否则用户会连点）。
     *    没配 token 时按钮本来就该是禁用的（没有可验证的东西），这是**正确行为**，
     *    所以只在有 token 时才点它 —— 否则这条会变成"因为后端没上线所以红"。 */
    res.verifyClickable = !!verify && !verify.disabled;
    if (res.verifyClickable) {
      /* 同「保存」：用 MutationObserver 抓整个验证过程里出现过的文案与禁用态，
       * 只在某一帧读一次会漏掉"请求很快就回来了"的情况。 */
      const seen = [];
      const obs = new w.MutationObserver(() => {
        const t = (verify.textContent || '').trim();
        if (!seen.length || seen[seen.length - 1].label !== t) {
          seen.push({ label: t, disabled: verify.disabled });
        }
      });
      obs.observe(verify, { subtree: true, childList: true, characterData: true,
                            attributes: true, attributeFilter: ['disabled'] });
      verify.click();
      await sleep(80);
      res.verifyingLabel = (verify.textContent || '').trim();
      res.verifyingDisabled = verify.disabled;
      await sleep(6000);
      obs.disconnect();
      res.verifyStates = seen;
      const msg = sec.querySelector('.verify-msg');
      res.verifyMsg = msg ? (msg.textContent || '').trim() : '';
      res.verifyMsgOk = msg ? msg.classList.contains('ok') : null;
      res.verifyBadges = [...sec.querySelectorAll('.badge')]
        .map((b) => (b.textContent || '').trim())
        .filter((t) => t.includes('Token'));
    }

    /* ② 「保存」的忙碌态：没有 token 也能验 —— 把模型版本改一下让"有改动"成立，再点保存。
     *    请求期间按钮文案必须变「保存中…」且禁用（这条不依赖后端是否已上线）。 */
    if (save) {
      const sel = [...sec.querySelectorAll('select')]
        .find((s) => [...s.options].some((o) => o.value === 'vlm'));
      if (sel) {
        sel.value = sel.value === 'vlm' ? 'pipeline' : 'vlm';
        sel.dispatchEvent(new w.Event('change', { bubbles: true }));
        await sleep(400);
      }
      res.saveEnabledAfterEdit = !save.disabled;
      if (res.saveEnabledAfterEdit) {
        /* ⚠️ 不能用"点完睡 120ms 再读"来抓忙碌态：后端已经把请求拒掉（404）时，
         * 这一帧早就过去了 —— 实测读到的还是「保存」。所以用 MutationObserver 抓**整个过程中
         * 出现过什么**（文案 + disabled），只要曾经出现过「保存中… + 禁用」就算过。 */
        const seen = [];
        const obs = new w.MutationObserver(() => {
          const t = (save.textContent || '').trim();
          if (!seen.length || seen[seen.length - 1].label !== t) {
            seen.push({ label: t, disabled: save.disabled });
          }
        });
        obs.observe(save, { subtree: true, childList: true, characterData: true,
                            attributes: true, attributeFilter: ['disabled'] });
        save.click();
        await sleep(80);
        res.savingLabel = (save.textContent || '').trim();
        res.savingDisabled = save.disabled;
        res.verifyingDisabledDuringSave = verify ? verify.disabled : null;
        await sleep(5000);
        obs.disconnect();
        res.saveStates = seen;
        res.afterSaveLabel = (save.textContent || '').trim();
        const msg = sec.querySelector('.verify-msg');
        res.verifyMsg = res.verifyMsg || (msg ? (msg.textContent || '').trim() : '');
        res.verifyBadges = res.verifyBadges || [...sec.querySelectorAll('.badge')]
          .map((b) => (b.textContent || '').trim())
          .filter((t) => t.includes('Token'));
      }
    }
    out(res);
  } catch (err) { res.error = String(err); out(res); }
});
</script>
"""


#: ⑮ 公式面板探针：真开一次阅读器的「公式」页签，回报面板里到底有没有条目。
#:
#: 量这四件事：`.fitem` 条目数、空态文案（"这一篇还没有可提取的公式"）、「解析」徽章数、
#: 页签角标与「全部 N」的计数 —— 用户报的"联网解析没有显示公式"就是前两项。
FORMULA_PROBE = """<!doctype html>
<meta charset="utf-8"><title>formula probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:460px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
let started = false;
f.addEventListener('load', async () => {
  if (started) return;
  started = true;
  const res = {};
  try {
    /* 阅读器是 SPA 里的一个路由，**直接整页打开就行**（不必像别的探针那样先建外壳再跳） */
    await sleep(__WAIT__);
    const d = f.contentDocument;
    /* 侧栏可能还没切到「公式」：按标题找页签点一下（`side=formulas` 一般已经切好） */
    const tabs = [...d.querySelectorAll('.side-tab')];
    const tab = tabs.find((b) => (b.textContent || '').includes('公式'));
    res.tabFound = !!tab;
    if (tab && !tab.classList.contains('active')) { tab.click(); await sleep(1200); }
    /* 页签角标：`.side-count`（`v-if="t.count"`，0 时整个 span 不渲染 → null） */
    const badgeEl = tab ? tab.querySelector('.side-count') : null;
    res.tabBadge = badgeEl ? Number((badgeEl.textContent || '').trim()) : null;
    const items = [...d.querySelectorAll('.fitem')];
    res.items = items.length;
    res.badges = items.slice(0, 6).map((it) => {
      const b = it.querySelector('.badge');
      return b ? (b.textContent || '').trim() : '';
    }).filter(Boolean);
    res.parseBadges = items.filter((it) => {
      const bs = [...it.querySelectorAll('.badge')];
      return bs.some((b) => (b.textContent || '').trim().startsWith('解析'));
    }).length;
    const empty = [...d.querySelectorAll('.empty')]
      .map((e) => (e.textContent || '').trim())
      .find((t) => t.includes('公式'));
    res.emptyText = empty || '';
    const allBtn = [...d.querySelectorAll('.seg button')]
      .find((b) => (b.textContent || '').includes('全部'));
    const am = allBtn ? (allBtn.textContent || '').match(/(\\d+)/) : null;
    res.allCount = am ? Number(am[1]) : null;
    out(res);
  } catch (err) { res.error = String(err); out(res); }
});
</script>
"""


SEARCH_LIVE_PROBE = """<!doctype html>
<meta charset="utf-8"><title>search live</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:520px;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
/* 论文库搜索岛「**有字符变动就搜**」（用户口径）：只改 input 的值 + 派发 input 事件，
 * **不按回车**，列表也必须自己收窄；再换一个不匹配的词，列表必须变空。
 * 用原生 setter 改值而不是 el.value = x —— Vue 的 v-model 监听 input 事件，
 * 直接赋值不触发它（"探针点了但 Vue 没反应"的经典坑）。 */
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
let started = false;
f.addEventListener('load', async () => {
  if (started) return;
  started = true;
  const w = f.contentWindow, d = f.contentDocument;
  const res = {};
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const cards = () => [...d.querySelectorAll('.lib-grid .paper-card, .lib-list .paper-card')];
  try {
    await sleep(__WAIT__);
    const input = d.querySelector('.lib-dock-search input');
    res.input = !!input;
    if (!input) { out(res); return; }
    res.placeholder = input.getAttribute('placeholder') || '';
    res.before = cards().length;
    const setter = Object.getOwnPropertyDescriptor(w.HTMLInputElement.prototype, 'value').set;
    const type = (v) => {
      setter.call(input, v);
      input.dispatchEvent(new w.Event('input', { bubbles: true }));
    };
    type('turbo');
    await sleep(1600);
    res.after = cards().length;
    res.titles = cards().map((c) => (c.textContent || '').trim().slice(0, 28));
    type('zzzz-no-such-paper-here');
    await sleep(1600);
    res.none = cards().length;
    out(res);
  } catch (err) { res.error = String(err); out(res); }
});
</script>
"""


# 探针里那张图的 base64（16x16 PNG，红底 + 蓝方块）。
# ⚠️ 真图，不是手搓的魔数：坏 PNG 能过后端的魔数嗅探，却会被模型端按
# "unsupported image" 拒掉 —— 那样探针量到的只是自己的 bug（踩过一次）。
PROBE_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAALElEQVR4nGO8IyfHQApgIkk1"
                 "Az00sCBzLOT2YVV04pETHZ3ENBI1MA6DtAQA8VYFUbO5ASoAAAAASUVORK5CYII=")


# 侧栏「对话」面板探针（`--chat`）：**两级**面板的端到端走查。
# 1. 页签顺序（「对话」必须紧跟「目录」）+ 一级页（搜索 / 分类 / 管理 / 会话行）；
# 2. 新建 → 二级页：输入框文案、空态文案（随机一条「与大肥鱼一起讨论…」）、加号是主题色；
# 3. 返回一级 → 从列表点进一个会话（真实历史）；
# 4. 上传一张图（走 file input 的 change，和用户选文件是同一条路）→ 缩略图出现；
# 5. 打字 → 发送键可用 → 点发送 → 等流结束 → 回答与引用都渲染出来。
#
# ⚠️ 用**1 秒轮询**而不是一次长 sleep：轮询的每个 tick 都是一次"虚拟时间检查点"，
# 有请求在飞时会停住等它；而单次 `sleep(45000)` 会被直接跳过（实测量到的还是空回答）。
# ⚠️ iframe 的 src 直接就是阅读器（同 `ZOOM_PROBE` 那批）：先进入口页再跳会让虚拟时间卡住。
CHAT_PROBE = """<!doctype html>
<meta charset="utf-8"><title>chat probe</title>
<style>body{margin:0}iframe{border:0;display:block}
pre{position:fixed;right:0;top:0;width:620px;height:100%;overflow:auto;background:#fff;border-left:1px solid #ccc;padding:8px;margin:0;white-space:pre-wrap;font:11px monospace}</style>
<iframe id="f" width="1500" height="1000" src="__URL__"></iframe>
<pre id="out">waiting</pre>
<script>
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const f = document.getElementById('f');
const out = (o) => { document.getElementById('out').textContent = JSON.stringify(o, null, 1); };
/* 16x16 的真 PNG（红底 + 蓝方块）：**必须是真图** —— 只靠魔数糊一个坏 PNG 过得了后端，
   但会被模型端按 "unsupported image" 拒掉，那样量到的是探针自己的 bug。 */
const PNG_B64 = '__PNG_B64__';
function tinyPng(w) {
  const bin = w.atob(PNG_B64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) buf[i] = bin.charCodeAt(i);
  return new w.File([buf], 'probe.png', { type: 'image/png' });
}
/* 主题色对比：`--accent` 是十六进制，`getComputedStyle` 给的是 rgb()，比之前先统一 */
function rgbOf(w, value) {
  const probe = w.document.createElement('span');
  probe.style.color = String(value).trim();
  w.document.body.appendChild(probe);
  const out = w.getComputedStyle(probe).color;
  probe.remove();
  return out;
}
let started = false;
f.addEventListener('load', async () => {
  if (started) return;
  started = true;
  const w = f.contentWindow, d = f.contentDocument;
  const res = { stage: 'loaded' };
  try {
    await sleep(__WAIT__);
    res.tabs = [...d.querySelectorAll('.side-tab')].map((b) => (b.textContent || '').trim());
    res.tabIndex = res.tabs.findIndex((t) => t.indexOf('对话') >= 0);
    const chat = d.querySelector('.chat');
    res.hasChat = !!chat;
    if (!chat) { out(res); return; }
    res.chatWidth = Math.round(chat.getBoundingClientRect().width);

    /* ── 一级页：搜索 / 分类 / 管理 / 会话行 ── */
    res.hasListHead = !!chat.querySelector('.list-head .search-input');
    res.segs = [...chat.querySelectorAll('.seg .seg-btn')].map((b) => (b.textContent || '').trim());
    res.hasManage = [...chat.querySelectorAll('.chip-btn')].some((b) => (b.textContent || '').includes('管理'));
    res.rows = chat.querySelectorAll('.row').length;
    const newBtn = chat.querySelector('.list-head .plus');
    res.hasNewBtn = !!newBtn;
    res.newBtnBg = newBtn ? getComputedStyle(newBtn).backgroundColor : '';
    /* 批量管理：勾两个看是否出现批量条（选完再退出） */
    const manage = [...chat.querySelectorAll('.chip-btn')].find((b) => (b.textContent || '').includes('管理'));
    if (manage && res.rows > 0) {
      manage.click();
      await sleep(300);
      const ticks = chat.querySelectorAll('.row .tick');
      res.tickCount = ticks.length;
      if (ticks.length) { ticks[0].click(); await sleep(200); }
      res.batchBar = !!chat.querySelector('.batch-bar');
      res.batchCount = (chat.querySelector('.batch-bar .tiny') || {}).textContent || '';
      manage.click();
      await sleep(200);
      res.batchOff = !chat.querySelector('.batch-bar');
    }
    out(res);

    /* ── 新建 → 二级页 ── */
    newBtn.click();
    await sleep(500);
    res.threadHead = !!chat.querySelector('.thread-head');
    const ta = chat.querySelector('textarea.input');
    res.placeholder = ta ? ta.getAttribute('placeholder') : '';
    res.headline = (chat.querySelector('.hello-line') || {}).textContent || '';
    res.headlineStartsWithPersona = res.headline.trim().indexOf('与大肥鱼一起讨论') === 0;
    const plus = chat.querySelector('.composer-bar .plus');
    res.plusBg = plus ? getComputedStyle(plus).backgroundColor : '';
    res.accentBg = rgbOf(w, getComputedStyle(d.documentElement).getPropertyValue('--accent'));
    res.plusIsAccent = !!plus && res.plusBg === res.accentBg && res.plusBg !== 'rgba(0, 0, 0, 0)';
    /* 列表页那个加号在上面就量过了（这一层没有 .list-head 了，别在这里再查一次） */
    res.listPlusBg = res.newBtnBg;
    res.listPlusIsAccent = !!res.newBtnBg && res.newBtnBg === res.accentBg;
    res.hasFileInput = !!chat.querySelector('input[type=file][multiple]');
    res.hasRing = !!chat.querySelector('.ring');
    res.model = (chat.querySelector('.model') || {}).textContent || '';
    out(res);

    /* ── 上传一张图（与用户选文件同一条路） ── */
    const fileInput = chat.querySelector('input[type=file]');
    const dt = new w.DataTransfer();
    dt.items.add(tinyPng(w));
    fileInput.files = dt.files;
    fileInput.dispatchEvent(new w.Event('change', { bubbles: true }));
    for (let i = 0; i < 20; i += 1) {
      await sleep(1000);
      if (chat.querySelector('.atts.pending .att')) break;
    }
    res.pendingAtts = chat.querySelectorAll('.atts.pending .att').length;
    res.attImgLoaded = !!chat.querySelector('.atts.pending .att img');
    out(res);

    /* ── 打字 → 发送 → 等流结束 ── */
    const setter = Object.getOwnPropertyDescriptor(w.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(ta, '这张图里有什么颜色？一句话回答。');
    ta.dispatchEvent(new w.Event('input', { bubbles: true }));
    await sleep(400);
    res.sendDisabled = chat.querySelector('.send').disabled;
    out(res);
    chat.querySelector('.send').click();
    for (let i = 0; i < 70; i += 1) {
      await sleep(1000);
      if (chat.querySelector('.err') && chat.querySelector('.err').textContent.trim()) break;
      if (!chat.querySelector('.msg.live') && chat.querySelectorAll('.msg').length > 0) break;
    }
    await sleep(1200);
    res.chatMsgs = chat.querySelectorAll('.msg').length;
    const answers = [...chat.querySelectorAll('.msg.assistant .md.body')];
    res.chatAnswer = answers.length ? answers[answers.length - 1].textContent.trim().slice(0, 120) : '';
    res.userAtts = chat.querySelectorAll('.msg.user .atts .att').length;
    res.chatCites = [...chat.querySelectorAll('.cite')].map((c) => (c.textContent || '').trim()).slice(0, 6);
    res.chatErr = (chat.querySelector('.err') || {}).textContent || '';
    res.chatStreamingLeft = !!chat.querySelector('.msg.live');
    res.hasBranchTool = !!chat.querySelector('.msg-tools .ico');
    out(res);

    /* ── 返回一级：历史里应该多出一条 ── */
    const back = chat.querySelector('.thread-head .ico');
    back.click();
    await sleep(800);
    res.backToList = !!chat.querySelector('.list-head');
    res.rowsAfter = chat.querySelectorAll('.row').length;
    out(res);
  } catch (err) { res.error = String(err); out(res); }
});
</script>
"""


def put_progress(base: str, paper: str, payload: dict) -> dict:
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


def _formulas_api(base: str, paper: str) -> dict:
    """`GET /papers/{id}/formulas` 的 data（⑮ 用；读不到就回空字典）。"""
    code, body = fetch(f"{base}/api/v1/papers/{paper}/formulas", timeout=25)
    if code != 200:
        return {}
    try:
        return json.loads(body.decode("utf-8")).get("data") or {}
    except Exception:  # noqa: BLE001
        return {}


def find_formula_fallback_paper(base: str, items: list[dict], limit: int = 12) -> str:
    """找一篇**公式来自原文兜底**的论文（`source=parse` / `mixed` 且有条目）。

    ⑮ 要验的正是"还没翻译的论文公式面板不再为空"这件事 —— 挑一篇已译完的论文是验不到的。
    找不到就返回空串（用例 SKIP，而不是硬失败）。
    """
    for item in items[:limit]:
        pid = item.get("id") or ""
        if not pid:
            continue
        api = _formulas_api(base, pid)
        if api.get("source") in ("parse", "mixed") and (api.get("count") or 0) > 0:
            return pid
    return ""


def _mineru_has_token(base: str) -> bool:
    """`/mineru/config` 里有没有 token —— 导入弹窗的"去配置"提示该不该出现，由它决定。

    **必须问后端、不能假设**：这条断言在"已配 token"的机器上本该反向成立
    （提示必须消失），写死成"提示必须出现"会让配好 token 的环境永远红。
    读不到（接口没上线 / 网络问题）时返回 False —— 那时"提示应当出现"才是对的期望。
    """
    status, body = fetch(f"{base}/api/v1/mineru/config", timeout=15)
    if status != 200:
        return False
    try:
        return bool((json.loads(body.decode("utf-8")) or {}).get("data", {}).get("has_token"))
    except Exception:  # noqa: BLE001
        return False


#: 自检会通过 UI 改动的三个设置项（token 不在其中：自检**从不**写 token）
_MINERU_SETTING_KEYS = ("engine_default", "model_version", "language")


def _mineru_config_snapshot(base: str) -> dict:
    """记下 `/mineru/config` 里那三个设置项的原值（读不到就返回空 → 不还原）。"""
    status, body = fetch(f"{base}/api/v1/mineru/config", timeout=15)
    if status != 200:
        return {}
    try:
        data = (json.loads(body.decode("utf-8")) or {}).get("data") or {}
    except Exception:  # noqa: BLE001
        return {}
    return {k: data[k] for k in _MINERU_SETTING_KEYS if data.get(k)}


def _mineru_config_restore(base: str, snapshot: dict) -> str:
    """把自检改掉的设置 PUT 回原值；返回一句人话（没得还原时返回空串）。

    为什么必须还原：⑬ 要用**真实点击**「保存」来验忙碌态，那就真的会写库。
    自检把"用户的默认引擎/模型版本"改掉、用户下次导入才发现，是比自检失败更糟的事。
    """
    if not snapshot:
        return ""
    req = urllib.request.Request(
        f"{base}/api/v1/mineru/config", method="PUT",
        data=json.dumps(snapshot).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            if resp.status != 200:
                return f"（还原失败：HTTP {resp.status}，当前值可能仍是自检改过的）"
    except Exception as exc:  # noqa: BLE001
        return f"（还原失败：{exc}，当前值可能仍是自检改过的）"
    return "、".join(f"{k}={v}" for k, v in snapshot.items())


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


def find_big_figure_paper(base: str, items: list[dict], limit: int = 8,
                          min_share: float = 0.25,
                          min_aspect: float = 1.2) -> str:
    """找一篇含"整幅大图"的论文（区域面积占页面 ≥ `min_share`）—— 给「大图允许放大」用例用。

    判据与前端 `ReaderPageRows.isLargeFigure` 完全同源：`media[].bbox` 的面积 ÷ 页面尺寸。
    逐篇拉一次 `/reader`，只看前几篇；找不到返回空串（用例 SKIP）。

    ⚠️ 除面积外还要求**横向**够宽（`min_aspect` = 宽/高）。这条用例断言的是"卡片宽度能超过
    568px"，而**竖长图**（实测新导入的一篇：385×760）受**高度**上限约束，宽度天然上不去 ——
    拿它测"能显示更大"必然红，那是假红：产品行为是对的（宽度受限而已）。
    挑横向大图才对应口径里"整幅图占比很大 → 允许显示更大"的那个场景。
    """
    for item in items[:limit]:
        pid = item.get("id") or ""
        if not pid:
            continue
        try:
            _st, body = fetch(f"{base}/api/v1/papers/{pid}/reader", timeout=40)
            data = json.loads(body.decode("utf-8"))["data"]
        except Exception:  # noqa: BLE001
            continue
        best: tuple[float, float] = (0.0, 0.0)      # (面积占比, 宽高比)
        for page in data.get("pages") or []:
            w = float(page.get("width") or 0)
            h = float(page.get("height") or 0)
            if w <= 0 or h <= 0:
                continue
            for m in page.get("media") or []:
                b = m.get("bbox") or []
                if len(b) < 4:
                    continue
                rw, rh = (b[2] - b[0]), (b[3] - b[1])
                share = (rw * rh) / (w * h)
                aspect = (rw / rh) if rh > 0 else 0.0
                if share > best[0]:
                    best = (share, aspect)
        if best[0] >= min_share and best[1] >= min_aspect:
            return pid
    return ""


def find_table_paper(base: str, items: list[dict], limit: int = 8) -> str:
    r"""找一篇**真的会渲染出 Markdown 表格**的论文（⑪ 表格渲染用例用）。

    注意"有 `type == 'table'` 的行"还不够：未翻译页退回解析块时，表格行里只有那行**题注**，
    没有表格本体（表格本体在页级翻译的行里）。所以要确认某个 `table` 行里确实带 `|---|` 的
    Markdown 表格。逐篇拉一次 `/reader`，只看前几篇；找不到返回空串（用例 SKIP 而不是硬失败）。

    **优先挑有"分组列"的表格**（第一列有空单元格，如 MMDFND 的 Table 1：`Weibo` / `Weibo-21`
    两段之间 `\cmidrule`）：不然"分组之间画细线"那条判据永远空转（没有分组列时它必然通过）。
    挑不到分组表格时退回第一篇带表格的论文。
    """
    fallback = ""
    for item in items[:limit]:
        pid = item.get("id") or ""
        if not pid:
            continue
        try:
            _st, body = fetch(f"{base}/api/v1/papers/{pid}/reader", timeout=40)
            data = json.loads(body.decode("utf-8"))["data"]
        except Exception:  # noqa: BLE001
            continue
        for tr in data.get("page_translations") or []:
            for row in tr.get("rows") or []:
                if row.get("type") != "table":
                    continue
                md = row.get("translation") or row.get("original") or ""
                if "\n|" not in md or "---" not in md:
                    continue
                if not fallback:
                    fallback = pid
                # 分组列的样子：某一行以 `| | `（第一格空）开头
                if any(ln.strip().startswith(("| |", "||")) for ln in md.splitlines()):
                    return pid
    return fallback


def element_by_text(html: str, tag: str, *texts: str) -> dict | None:
    r"""在 dump 出来的 DOM 里，按"标签 + 文本子串"找**最后一个**匹配元素，返回它的类名与属性。

    给"点了才出现"的界面用（弹窗、面板）：同一段文案可能先出现在别处，真正要断言的那个在后面。

    ⚠️ 用正则扒 HTML 是权宜之计：`dump-dom` 出来的属性里可能有 `>`，而这里只用于**取类名/属性**，
    拿不到就退化为 None（调用方按"没找到"报失败），不会靠它做布局断言 —— 需要量尺寸的一律走
    `dom_probe()` 的真实浏览器探针。
    """
    for needle in texts:
        pat = re.compile(
            rf"<{tag}\b([^>]*)>((?:(?!</{tag}>).)*?){re.escape(needle)}(?:(?!</{tag}>).)*?</{tag}>",
            re.S)
        hits = list(pat.finditer(html))
        if not hits:
            continue
        attrs, inner = hits[-1].group(1), hits[-1].group(2)
        cls = re.search(r'class="([^"]*)"', attrs)
        return {"cls": cls.group(1) if cls else "", "attrs": attrs, "inner": inner}
    return None


def count_element(html: str, tag: str) -> int:
    """数某个标签出现几次（自闭合写法不单列 —— dump-dom 出来的表单控件都是成对标签）。"""
    return len(re.findall(rf"<{tag}\b", html))


def dom_probe(browser: str, base: str, probe: str, page: str = "/", wait_ms: int = 7000,
              budget_ms: int = 30000, size: str = "1700,1100") -> dict | None:
    r"""跑一个「真实浏览器探针」并取回它写进 `<pre id="out">` 的 JSON。

    探针是临时写到 `dist/` 下的一份小页面（同源 → 能开 iframe、能读 iframe 的 DOM 与
    `getComputedStyle`）。这比在 Python 侧扒 dump 出来的 HTML 强在**它真的会点**：
    开关是不是真切换、按钮是不是真的置灰、警告是不是真的出现，只有点过才知道。

    文件名取自探针自己的 `<title>`（每个探针都有），省得再单传一个名字。

    `page` 是探针最后要停在的前端路径（`/library`、`/settings`…）。它走 query 传给探针，
    由探针自己 `w.location.replace(...)` 跳过去 —— iframe 的 `src` 必须是**同源的入口页**
    （`{base}/app/`），否则跨出去连 iframe 的 DOM 都读不到。
    """
    app_root = base if base.endswith("/app") else base + "/app/"
    m = re.search(r"<title>(.*?)</title>", probe)
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", (m.group(1) if m else "probe").strip()) or "probe"
    probe_file = ROOT / "frontend" / "dist" / f"_{name}.html"
    try:
        probe_file.write_text(
            probe.replace("__URL__", app_root).replace("__WAIT__", str(wait_ms)),
            encoding="utf-8")
        url = f"{base}/app/{probe_file.name}?page={quote(page, safe='/?&=')}"
        dom = render(browser, url, budget_ms, size)
        out = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
        if not out or out.group(1).strip() == "waiting":
            return None
        try:
            return json.loads(out.group(1))
        except json.JSONDecodeError:
            return None
    finally:
        probe_file.unlink(missing_ok=True)


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
    # 控制台按 UTF-8 输出。**不是美化**：Windows 默认代码页是 GBK，而自检的章节标题里有
    # `⑪`（U+246A，GBK 收的圈码只到 `⑩`）——`print` 会直接抛 UnicodeEncodeError，
    # 把自检打断在第十一节，后半段（含本轮新增的⑫⑬⑭）根本不跑（本轮实测）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 —— 老 Python / 已被重定向的流：保持原样
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8710")
    ap.add_argument("--paper", default="", help="阅读器用论文 id（默认自动取内容最全的一篇）")
    ap.add_argument("--block-markers", action="store_true",
                    help="额外断言：公式已渲染成 KaTeX；导入弹窗的三个切换开关与解析引擎"
                         "二选一；设置页「在线解析（MinerU）」分区与 /mineru/* 契约")
    ap.add_argument("--browser", default=None)
    ap.add_argument("--budget", type=int, default=20000)
    ap.add_argument("--chat", action="store_true",
                    help="额外跑「侧栏对话面板」探针：发一条消息并等流式回答（需 agent 服务在跑，"
                         "会真调一次模型）")
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
                card = data.get("card") or {}
                checks = [
                    ("插图卡片不超 560×420",
                     bool(card) and (card.get("w") or 0) > 40 and not card.get("over"),
                     f"卡片 {card.get('w')}×{card.get('h')}px（超框={card.get('over')}）"),
                    ("点开仍是原始尺寸",
                     (data.get("modalNat") or 0) >= max(2 * (card.get("w") or 1), 800),
                     f"弹窗里的图原始宽 {data.get('modalNat')}px（卡片才 {card.get('w')}px）"),
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

    # ---------------- ⑥b 整幅大图允许放大（用户口径：占页面很大就显示更大） ----------------
    big_paper = find_big_figure_paper(base, items) if items else ""
    print(f"\n用于「大图放得更大」用例的论文：{big_paper or '（库里没有整幅大图）'}")
    if browser and big_paper:
        dist = ROOT / "frontend" / "dist"
        probe_file = dist / "_zoom_big_probe.html"
        try:
            probe_file.write_text(
                ZOOM_PROBE.replace('__URL__', f"{base}/app/reader/{big_paper}?view=bilingual&left=text&nosse=1")
                .replace('__WAIT__', '7000'), encoding="utf-8")
            dom = render(browser, f"{base}/app/_zoom_big_probe.html", 25000, "1700,1100")
            m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
            data = None
            if m and m.group(1).strip() != "waiting":
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    data = None
            big = (data or {}).get("large") or {}
            ok = bool(big) and (big.get("w") or 0) > 568 and (big.get("w") or 0) <= 888 \
                and (big.get("h") or 0) <= 808
            failures += 0 if ok else 1
            detail = (f"整幅图卡片 {big.get('w')}×{big.get('h')}px（上限 880×800；"
                      f"整页 {data.get('largeCards') if data else '?'} 张标记为大图）")
            results.append({"kind": "zoom", "path": "整幅大图允许放大", "ok": ok, "detail": detail})
            print(f"[{'PASS' if ok else 'FAIL'}] {'整幅大图允许放大':<16} {detail}")
        finally:
            probe_file.unlink(missing_ok=True)
    else:
        print("[SKIP] 需要浏览器与含整幅大图的论文")

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
                     f"{formula.get('marks')} 段（行 {formula.get('rowId')} / 选中 {formula.get('bodySide')}"
                     f"，该侧有公式={formula.get('bodyHasKatex')}，整行 {formula.get('katexInRow')} 个公式，"
                     f"标记子节点={formula.get('markChildren')}）"),
                    ("多行公式底色覆盖整块", bool(disp.get("covers")) and bool(disp.get("hasBlockClass"))
                     and bool(disp.get("bandInsideFormula")) and bool(disp.get("bandMatchesInk"))
                     and bool(disp.get("centered"))
                     and (not disp.get("hasTag") or not disp.get("preTagClear")
                          or (bool(disp.get("tagClear")) and bool(disp.get("tagAtRight")))),
                     f"色块高 {disp.get('markH')}px / 公式墨迹高 {disp.get('baseMaxH')}px"
                     f"（公式块 {disp.get('dispH')}px），"
                     f"色块宽 {disp.get('bandW')}px / 公式宽 {disp.get('lineW')}px"
                     f"（只涂公式本体={disp.get('bandInsideFormula')}、贴住公式={disp.get('bandMatchesInk')}、"
                     f"仍居中={disp.get('centered')}），"
                     f"有无编号={disp.get('hasTag')}（注入={data.get('tagInjected')}、"
                     f"高亮前不压={disp.get('preTagClear')}、"
                     f"高亮后不压={disp.get('tagClear')}、编号仍在最右={disp.get('tagAtRight')}），"
                     f"几何={disp.get('boxes')}（行 {disp.get('rowId')}），"
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
                imgs = data.get("images") or {}
                iz = data.get("imageZoom") or {}
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
                    ("插图不铺满整栏（≤560×420）",
                     (imgs.get("count") or 0) >= 1 and (imgs.get("over") or 0) == 0,
                     f"{imgs.get('count')} 张图：最大 {imgs.get('maxW')}×{imgs.get('maxH')}px"
                     f"（整栏 {imgs.get('docW')}px），超出上限 {imgs.get('over')} 张；"
                     f"第一张 alt={imgs.get('alt')!r}"),
                    ("点插图能开大图", bool(iz.get("open")) and bool(iz.get("hasImg")),
                     f"弹窗={iz.get('open')}，标题={iz.get('title')!r}，大图={iz.get('src')}"
                     f"（原始宽 {iz.get('natW')}px）"),
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
                    "顶栏三块玻璃同高同线 + 弹出面板在场",
                    len(ui.get("glass") or []) >= 2 and len(ui.get("glassKeys") or []) == 1
                    and int(ui.get("panels") or 0) >= 2,
                    f"顶/高={ui.get('glass')}（左开关 / 主胶囊 / 右更多，收起侧栏时三块都在）"
                    f"· 弹出面板 {ui.get('panels')} 个"))
                checks.append((
                    "侧栏开关始终只有一枚",
                    int(ui.get("toggles") or 0) == 1,
                    f"[data-aside-toggle] 在 DOM 里 {ui.get('toggles')} 枚"
                    f"（侧栏开着时它在侧栏头部最左，收起时在正文左边缘 —— 同一枚，不交接）"))
                tb = ui.get("tabBar") or {}
                st = ui.get("sideTabs") or {}
                checks.append((
                    "标签页胶囊不裁文字 + 与下方胶囊同一种圆形",
                    int(tb.get("clip") or 0) == 0
                    and float(tb.get("ratio") or 9) <= 1.1
                    and int(tb.get("h") or 0) >= 30
                    and float(tb.get("radius") or 0) >= int(tb.get("h") or 0) / 2,
                    f"标签页 {tb.get('n')} 枚：高 {tb.get('h')}px、圆角 {tb.get('radius')}px、"
                    f"裁字 {tb.get('clip')} 枚（字高/盒高 = {tb.get('ratio')}）"))
                checks.append((
                    "侧栏页签胶囊与左侧圆钮同高同线 + 只在当前页显示文字",
                    st.get("h") == 38 and st.get("soloH") == 38
                    and st.get("top") == st.get("soloTop")
                    and st.get("stacked") is True
                    and int(st.get("labels") or 0) == 1
                    and st.get("border") == "none"
                    and st.get("flat") is True,
                    f"页签胶囊 top/高 = {st.get('top')}/{st.get('h')}、"
                    f"圆钮 top/高 = {st.get('soloTop')}/{st.get('soloH')}、"
                    f"当前页两行 = {st.get('stacked')}、露出文字 {st.get('labels')} 枚、"
                    f"描边 = {st.get('border')}、不铺毛玻璃 = {st.get('flat')}"))
                checks.append((
                    "毛玻璃不带内阴影（inset 高光）",
                    int(ui.get("insets") if ui.get("insets") is not None else -1) == 0,
                    f"四块玻璃（顶栏三块 + 侧栏页签胶囊）里带 inset 的有 "
                    f"{ui.get('insets')} 块；页签胶囊自身 inset = {st.get('inset')}"))
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

    # ---------------- ⑪ 表格渲染：论文式「三线表」 ----------------
    print()
    print("=" * 78)
    print("⑪ 表格渲染（论文式三线表：无竖线 / 三条横线 / 数字右对齐 / 分组细线）")
    print("=" * 78)
    table_paper = find_table_paper(base, items) if items else ""
    print(f"用于表格用例的论文：{table_paper or '（库里没有带 Markdown 表格的论文）'}")
    if browser and table_paper:
        dist = ROOT / "frontend" / "dist"
        probe_file = dist / "_table_probe.html"
        try:
            probe_file.write_text(
                TABLE_PROBE.replace('__URL__', f"{base}/app/reader/{table_paper}?view=bilingual&left=text&nosse=1")
                .replace('__WAIT__', '9000'), encoding="utf-8")
            dom = render(browser, f"{base}/app/_table_probe.html", 32000, "1700,1100")
            m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
            data = None
            if m and m.group(1).strip() != "waiting":
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    data = None
            if not data or not data.get("cols"):
                failures += 1
                results.append({"kind": "table", "path": "Markdown 表格渲染", "ok": False,
                                "detail": f"没找到可测的表格（{data}）"})
                print(f"[FAIL] 表格渲染         没找到可测的表格（{data}）")
            else:
                checks = [
                    ("表格没有竖线", data.get("vertical") == 0,
                     f"表头/正文单元格左右边框最大 {data.get('vertical')}px"),
                    ("表格只有三条横线（且不细）", (data.get("topRule") or 0) >= 2
                     and (data.get("midRule") or 0) >= 1 and (data.get("bottomRule") or 0) >= 2
                     and (data.get("rowLines") or 0) == 0,
                     f"顶线 {data.get('topRule')}px / 表头下线 {data.get('midRule')}px / "
                     f"底线 {data.get('bottomRule')}px，逐行横线 {data.get('rowLines')} 条"
                     f"（颜色 {data.get('ruleColor')}）"),
                    ("数字列右对齐、表头压正中", (data.get("numCols") or 0) >= 1
                     and data.get("numAlign") == "right" and data.get("numHeadAlign") == "center"
                     and "tabular-nums" in str(data.get("tnum") or "")
                     and data.get("txtAlign") == "left",
                     f"{data.get('numCols')} 个数字列（正文 {data.get('numAlign')} / 表头 "
                     f"{data.get('numHeadAlign')}，等宽数字={data.get('tnum')}）；"
                     f"{data.get('textCols')} 个文字列（正文 {data.get('txtAlign')}）"),
                    ("数据集分组之间画细线", (not data.get("grouping")) or (data.get("groupLines") or 0) >= 1,
                     f"首列有空单元格（分组列）={data.get('grouping')}，非首行里的细线 "
                     f"{data.get('groupLines')} 条"),
                    ("宽表不顶宽整页", bool(data.get("wrap")) and (data.get("pageScrollX") or 0) <= 1,
                     f"横向滚动容器={data.get('wrap')}（{data.get('wrapScroll')}），"
                     f"页面横向溢出 {data.get('pageScrollX')}px，表宽 {data.get('tableW')}px，"
                     f"{data.get('cols')} 列 × {data.get('rows')} 行，行高 {data.get('rowH')}px / "
                     f"表头高 {data.get('headH')}px / 字号 {data.get('fontSize')}"),
                ]
                for label, ok, detail in checks:
                    failures += 0 if ok else 1
                    results.append({"kind": "table", "path": label, "ok": ok, "detail": detail})
                    print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
                print(f"         表头：{data.get('sample')}")
        finally:
            probe_file.unlink(missing_ok=True)
    else:
        print("[SKIP] 需要浏览器与带 Markdown 表格的论文")

    # ---------------- ⑫ 导入弹窗：三个切换开关 + 解析引擎二选一 ----------------
    # ⚠️ ⑫⑬⑭ 是本轮（MinerU / Switch）新增的检查，**一律受 `--block-markers` 开关约束**：
    # 不带这个参数时自检的输出与项数必须与改动前**逐项一致**（本轮只允许"新增检查项"，
    # 不允许改已有 97 项的语义），所以这里是一个整块的门。
    if args.block_markers:
        print()
        print("=" * 78)
        print("⑫ 导入弹窗（三个切换开关 + 解析引擎二选一）")
        print("=" * 78)
        if browser:
            data = dom_probe(browser, base, IMPORT_DIALOG_PROBE, page="/library", wait_ms=6000)
            if not data or not data.get("modal"):
                failures += 1
                results.append({"kind": "import-dialog", "path": "导入弹窗", "ok": False,
                                "detail": f"没打开导入弹窗 / 未取到测量结果（{data}）"})
                print(f"[FAIL] 导入弹窗          没打开导入弹窗 / 未取到测量结果（{data}）")
            else:
                before = data.get("before") or {}
                auto, cap, deep = before.get("auto"), before.get("cap"), before.get("deep")
                skin = data.get("skin") or {}
                autoOn, capOff = skin.get("autoOn") or {}, skin.get("capOff") or {}
                notes = data.get("notes") or {}
                keyboard = data.get("keyboard") or {}
                toggle = data.get("toggle") or {}
                engine = data.get("engine") or {}
                after = engine.get("after") or {}
                checks: list[tuple[str, bool, str]] = [
                    # ① 三个开关都在，而且**里面是真实 checkbox + role=switch + sr-only**
                    #    （自绘 div 开关会看起来一样但丢掉键盘 / 点文字 / AT 语义，所以逐项断言）
                    ("三个开关都在，且是真实 checkbox",
                     data.get("rowCount") == 3
                     and all(b is not None for b in (auto, cap, deep))
                     and all(b.get("type") == "checkbox" and b.get("role") == "switch"
                             and b.get("srOnly") and b.get("visuallyHidden")
                             for b in (auto, cap, deep)),
                     f"{data.get('rowCount')} 行；"
                     + "；".join(f"{n}: type={b.get('type')}/role={b.get('role')}/"
                                 f"sr-only={b.get('srOnly')}/盒子={b.get('cbBox')}"
                                 for n, b in (("立即解析", auto), ("含图注", cap), ("深度", deep)))),
                    # ② 视觉规格：轨道 36×20、滑块 16、圆角 pill、滑块自己 140ms
                    ("轨道 36×20 / 滑块 16 / 140ms",
                     all(b.get("trackW") == 36 and b.get("trackH") == 20 and b.get("knobW") == 16
                         for b in (auto, cap, deep))
                     and all(str(b.get("knobTransition") or "").startswith("0.14s")
                             for b in (auto, cap, deep))
                     and "999px" in str((auto or {}).get("radius") or ""),
                     f"{[(b.get('trackW'), b.get('trackH'), b.get('knobW')) for b in (auto, cap, deep)]}，"
                     f"圆角 {(auto or {}).get('radius')}，滑块过渡 {(auto or {}).get('knobTransition')}，"
                     f"轨道过渡 {(auto or {}).get('trackTransition')}"),
                    # ③ 默认态就必须看得出"开/关"：自动解析默认开（= --accent + 滑块右移），
                    #    插图开关默认关（= 浅底 + 滑块贴左）。这条对"只渲染了轨道没有状态"也敏感。
                    ("默认开关态看得出开/关",
                     bool(capOff.get("checked")) is False and bool(autoOn.get("checked")) is True
                     and autoOn.get("bg") == data.get("accent")
                     and autoOn.get("bg") != capOff.get("bg")
                     and (autoOn.get("knobX") or 0) - (capOff.get("knobX") or 0) == 16,
                     f"--accent={data.get('accent')}；开的轨道 {autoOn.get('bg')}"
                     f"（滑块 x={autoOn.get('knobX')}）/ 关的轨道 {capOff.get('bg')}"
                     f"（滑块 x={capOff.get('knobX')}）"),
                    # ④ 点整行（label）真的切换，且**两次点击回到原状**（不是粘住 / 只能开不能关）
                    ("点文字即切换（开→关可逆）",
                     toggle.get("on") is not toggle.get("was")
                     and toggle.get("off") == toggle.get("was")
                     and (toggle.get("onKnob") or 0) > (toggle.get("offKnob") or 0),
                     f"初始 checked={toggle.get('was')} → 点一下={toggle.get('on')}"
                     f"（滑块 x={toggle.get('onKnob')}）→ 再点={toggle.get('off')}"
                     f"（滑块 x={toggle.get('offKnob')}）"),
                    # ⑤ 点击后的开态真的是强调色、滑块右移 16px
                    ("开态换成强调色、滑块右移",
                     bool(toggle.get("onBg")) and toggle.get("onBg") == data.get("accent")
                     and toggle.get("onBg") != toggle.get("offBg")
                     and (toggle.get("onKnob") or 0) - (toggle.get("offKnob") or 0) == 16,
                     f"轨道 {toggle.get('offBg')} → {toggle.get('onBg')}"
                     f"（--accent={data.get('accent')}）；"
                     f"滑块 x {toggle.get('offKnob')} → {toggle.get('onKnob')}"),
                    # ⑥ 键盘可达：隐藏的 checkbox 必须真的能拿到焦点（否则"用原生控件换取键盘操作"
                    #    这件事就没兑现 —— div 自绘开关最常丢的就是这一条）
                    ("隐藏 checkbox 可聚焦（键盘可达）",
                     all(f and f.get("focused") is True and f.get("role") == "switch"
                         and (f.get("tabIndex") or 0) >= 0
                         for f in (keyboard.get("auto"), keyboard.get("deep"))),
                     f"立即解析={keyboard.get('auto')}，深度搜索={keyboard.get('deep')}"),
                    # ⑦ 两句语义说明一字不改（换组件时别顺手把口径删了）
                    ("两句说明文案保留",
                     bool(notes.get("cap")) and bool(notes.get("deep")),
                     f"含图注说明={notes.get('cap')}，深度说明={notes.get('deep')}"),
                    # ⑦ 解析引擎是**互斥二选一**（role=radio + aria-checked 跟着走）
                    ("解析引擎是互斥二选一",
                     engine.get("count") == 2 and engine.get("role") == "radiogroup"
                     and all(r == "radio" for r in (engine.get("roles") or []))
                     and sorted(engine.get("checked") or []) == ["false", "true"]
                     and len([a for a in (after.get("active") or []) if a]) == 1
                     and sorted(after.get("checked") or []) == ["false", "true"],
                     f"{engine.get('count')} 个选项 {engine.get('labels')}；点 MinerU 后"
                     f" active={after.get('active')} aria-checked={after.get('checked')}"),
                    # ⑧ 两个选项各带一行"代价说清楚"的小字（在线必须写明要 token + 联网）
                    ("两个选项各有一行说明",
                     all(len(str(n or "")) >= 12 for n in (engine.get("notes") or []))
                     and any("token" in str(n) and "联网" in str(n) for n in (engine.get("notes") or []))
                     and any("离线" in str(n) for n in (engine.get("notes") or [])),
                     f"{engine.get('notes')}"),
                    # ⑨ 选 MinerU → 下面三个开关**整块消失**（用户口径：只在本地解析时显示）。
                    #    不是置灰：置灰仍然是一句"你可以改"的邀请，而这三个开关在在线解析里
                    #    根本没有对应物（版面/插图/图注全由 MinerU 决定）。
                    ("选 MinerU 后三个开关不再显示",
                     engine.get("clicked") is True and engine.get("switchRowsAfter") == 0
                     and engine.get("deepDisabled") is None
                     and engine.get("deepChecked") is None,
                     f"剩余 .switch-row={engine.get('switchRowsAfter')}，"
                     f"深度搜索行={engine.get('deepDisabled')}"),
                    # ⑩ **在线解析参数**只在选在线解析时出现（切回本地时必须已经不在了）
                    ("在线参数区只在选 MinerU 时出现",
                     engine.get("onlineGroup") is True
                     and data.get("onlineGroupWhenLocal") is False,
                     f"选 MinerU 后={engine.get('onlineGroup')}，"
                     f"切回本地时={data.get('onlineGroupWhenLocal')}"),
                    # ⑪ 模型可选（pipeline / vlm），且当前值就在选项里 —— 用户要求"支持模型选择"
                    ("模型可选（pipeline / vlm）",
                     sorted(engine.get("opModelOptions") or []) == ["pipeline", "vlm"]
                     and engine.get("opModelValue") in ("pipeline", "vlm"),
                     f"选项={engine.get('opModelOptions')}，当前={engine.get('opModelValue')}"),
                    # ⑫ OCR 语言也有下拉（扫描件才用得上，但必须能选）
                    ("OCR 语言可选",
                     len(engine.get("opLangOptions") or []) >= 2
                     and "en" in (engine.get("opLangOptions") or []),
                     f"选项={engine.get('opLangOptions')}"),
                    # ⑬ 三个能力开关齐全，且默认值与官方口径一致（OCR 关、公式/表格开）
                    ("强制 OCR / 识别公式 / 识别表格 三个开关",
                     engine.get("opSwitchTexts") == ["强制 OCR", "识别公式", "识别表格"]
                     and engine.get("opSwitchChecked") == [False, True, True],
                     f"开关={engine.get('opSwitchTexts')}，默认={engine.get('opSwitchChecked')}"),
                    # ⑭ 页码范围输入框带**可照抄**的示例（写错格式只有服务端才知道怎么改）
                    ("页码范围有输入框与示例",
                     "1-10" in str(engine.get("opRangesPlaceholder") or "")
                     and "留空" in str(engine.get("opRangesPlaceholder") or ""),
                     f"placeholder={engine.get('opRangesPlaceholder')!r}"),
                ]
                # ⑨ token 提示**跟着配置状态走**（不是"永远应该出现"）：
                #    没配 token 时必须给出"去哪配、要做什么"这句可执行的话；
                #    已配好 token 时那句提示必须消失（否则用户会以为没生效，反复去填）。
                warn = str(engine.get("warn") or "")
                has_token = _mineru_has_token(base)
                if has_token:
                    checks.append((
                        "已配 token 时不再提示去配置",
                        not warn,
                        f"提示={warn[:90] or '(没有)'}（/mineru/config 里 has_token=true）",
                    ))
                else:
                    checks.append((
                        "没配 token 时提示去哪配",
                        bool(warn) and "设置" in warn and "MinerU" in warn and "token" in warn,
                        f"提示={warn[:90] or '(没有)'}",
                    ))
                for label, ok, detail in checks:
                    failures += 0 if ok else 1
                    results.append({"kind": "import-dialog", "path": label, "ok": ok, "detail": detail})
                    print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
        else:
            print("[SKIP] 需要浏览器")

        # ---------------- ⑬ 设置页「在线解析（MinerU）」分区 ----------------
        print()
        print("=" * 78)
        print("⑬ 设置页「在线解析（MinerU）」分区（token 只写不回显 / 验证 / 默认参数）")
        print("=" * 78)
        # 这一节要用 UI 真点一次「保存」（验忙碌态），也就**真的会改掉本机的设置**。
        # 自检不该改变机器状态：先记下原值，跑完再 PUT 回去（只回写这三个设置项，不碰 token）。
        _cfg_before = _mineru_config_snapshot(base)
        if browser:
            data = dom_probe(browser, base, MINERU_SECTION_PROBE, page="/settings", wait_ms=6000)
            if not data or not data.get("section"):
                failures += 1
                results.append({"kind": "mineru", "path": "MinerU 分区", "ok": False,
                                "detail": f"设置页没有 #mineru 分区 / 未取到测量结果（{data}）"})
                print(f"[FAIL] MinerU 分区      设置页没有 #mineru 分区 / 未取到测量结果（{data}）")
            else:
                checks = [
                    ("分区与导航项都在",
                     "在线解析" in str(data.get("title")) and data.get("inNav") is True,
                     f"标题={data.get('title')}，药丸导航里有={data.get('inNav')}"),
                    # token 必须**只写不回显**：value 空、placeholder 给掩码 + "留空 = 不修改"
                    ("token 是 password 且不回显",
                     data.get("password") is True and not data.get("passwordValue"),
                     f"placeholder={data.get('passwordPlaceholder')!r}，"
                     f"当前值={data.get('passwordValue')!r}"),
                    ("保存 / 验证 / 清除 三个按钮",
                     data.get("hasSave") and data.get("hasClear")
                     and "验证" in str(data.get("verifyLabel")),
                     f"按钮={data.get('buttons')}"),
                    ("模型版本 / 语言 / 默认引擎三个下拉",
                     data.get("modelOptions") and data.get("langOptions") and data.get("engineOptions"),
                     f"{[s.get('value') for s in (data.get('selects') or [])]}；"
                     f"模型={data.get('modelOptions')} 语言={data.get('langOptions')} "
                     f"引擎={data.get('engineOptions')}"),
                    ("写明 token 文件路径与 90 天过期",
                     data.get("hasTokenFile") and data.get("has90Days"),
                     f"token 文件行={data.get('hasTokenFile')}，90 天提示={data.get('has90Days')}"),
                ]
                # 点「验证」：请求期间必须是「验证中…」+ 禁用（否则用户会连点）。
                # 没配 token 时按钮本就该禁用（没有可验证的东西），所以那种情况断言的是"禁用"，
                # 而不是"点得动" —— 否则这条会变成"因为后端没上线所以红"。
                if data.get("verifyClickable"):
                    states = data.get("verifyStates") or []
                    checks.append((
                        "验证中按钮变文案并禁用",
                        any("验证中" in str(s.get("label")) and s.get("disabled") is True
                            for s in states) or (
                            "验证中" in str(data.get("verifyingLabel"))
                            and data.get("verifyingDisabled") is True),
                        f"过程中的按钮状态={states}；点击后立刻读到 "
                        f"{data.get('verifyingLabel')!r}/disabled={data.get('verifyingDisabled')}",
                    ))
                    # 验证结论：成功走 --ok、失败走 --danger，且能看到服务端原话与延迟
                    msg = str(data.get("verifyMsg") or "")
                    checks.append((
                        "验证结论落到行内状态",
                        bool(msg) or bool(data.get("verifyBadges")),
                        f"消息={msg[:90] or '(空)'}；徽章={data.get('verifyBadges')}",
                    ))
                else:
                    checks.append((
                        "没 token 时「验证」禁用",
                        data.get("verifyDisabled") is True,
                        f"disabled={data.get('verifyDisabled')}，标签={data.get('verifyLabel')!r}",
                    ))
                # 点「保存」：请求期间文案变「保存中…」且禁用。
                # 这条**不依赖 token** —— 改一下模型版本就能把保存按钮点亮，用它验忙碌态。
                if data.get("saveEnabledAfterEdit") is True:
                    states = data.get("saveStates") or []
                    busy = [s for s in states
                            if "保存中" in str(s.get("label")) and s.get("disabled") is True]
                    checks.append((
                        "保存中按钮变文案并禁用",
                        bool(busy)
                        or ("保存中" in str(data.get("savingLabel"))
                            and data.get("savingDisabled") is True),
                        f"过程中的按钮状态={states}；保存完成后按钮={data.get('afterSaveLabel')!r}；"
                        f"同一时刻「验证」disabled={data.get('verifyingDisabledDuringSave')}",
                    ))
                else:
                    checks.append((
                        "保存中按钮变文案并禁用",
                        False,
                        f"改模型版本后「保存」仍是禁用的（`mineruChanged` 没被点亮，"
                        f"或 /mineru/config 读失败？）：{data.get('buttons')}",
                    ))
                for label, ok, detail in checks:
                    failures += 0 if ok else 1
                    results.append({"kind": "mineru", "path": label, "ok": ok, "detail": detail})
                    print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
        else:
            print("[SKIP] 需要浏览器")
        # 还原这一节通过 UI 改掉的设置（见上面的 `_cfg_before`）
        restored = _mineru_config_restore(base, _cfg_before)
        if restored:
            print(f"[INFO] 已把自检改动的设置还原：{restored}")

        # ---------------- ⑭ /mineru/* 接口契约（前端按这个形状解析） ----------------
        print()
        print("=" * 78)
        print("⑭ /mineru/* 接口契约（config 的字段形状 / verify 恒 200 + ok·state）")
        print("=" * 78)
        cfg_ok, cfg_detail = False, ""
        try:
            _st, body = fetch(f"{base}/api/v1/mineru/config", timeout=20)
            if _st != 200:
                cfg_detail = f"GET /mineru/config → HTTP {_st}（后端尚未上线？）"
            else:
                cfg = json.loads(body.decode("utf-8")).get("data") or {}
                need = ["has_token", "token_masked", "engine_default", "model_version", "language"]
                missing = [k for k in need if k not in cfg]
                cfg_ok = not missing and cfg.get("engine_default") in ("local", "mineru") \
                    and cfg.get("model_version") in ("pipeline", "vlm")
                cfg_detail = (f"缺字段 {missing}" if missing else
                              f"has_token={cfg.get('has_token')} masked={cfg.get('token_masked')} "
                              f"engine={cfg.get('engine_default')} model={cfg.get('model_version')} "
                              f"lang={cfg.get('language')} file={cfg.get('token_file')}")
        except Exception as exc:  # noqa: BLE001
            cfg_detail = f"GET /mineru/config 失败：{exc}"
        failures += 0 if cfg_ok else 1
        results.append({"kind": "mineru-api", "path": "GET /mineru/config", "ok": cfg_ok,
                        "detail": cfg_detail})
        print(f"[{'PASS' if cfg_ok else 'FAIL'}] {'GET config':<16} {cfg_detail}")

        # PUT：只提交**与当前值相同**的 engine_default（幂等 no-op，不会改坏用户的配置）
        put_ok, put_detail = False, ""
        try:
            _st, body = fetch(f"{base}/api/v1/mineru/config", timeout=20)
            cur = (json.loads(body.decode("utf-8")).get("data") or {}).get("engine_default")
            req = urllib.request.Request(
                f"{base}/api/v1/mineru/config", method="PUT",
                data=json.dumps({"engine_default": cur or "local"}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            data = payload.get("data") or {}
            put_ok = resp.status == 200 and "engine_default" in data
            put_detail = (f"HTTP {resp.status}，回包字段 {sorted(data)[:6]}，"
                          f"message={payload.get('message')!r}")
        except Exception as exc:  # noqa: BLE001
            put_detail = f"PUT /mineru/config 失败：{exc}"
        failures += 0 if put_ok else 1
        results.append({"kind": "mineru-api", "path": "PUT /mineru/config", "ok": put_ok,
                        "detail": put_detail})
        print(f"[{'PASS' if put_ok else 'FAIL'}] {'PUT config':<16} {put_detail}")

        # verify：**HTTP 恒 200**，成败看 ok/state（前端就是按这个写的）
        ver_ok, ver_detail = False, ""
        try:
            req = urllib.request.Request(
                f"{base}/api/v1/mineru/verify", method="POST", data=b"{}",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            data = payload.get("data") or {}
            states = ("valid", "invalid", "expired", "network", "missing")
            ver_ok = (resp.status == 200 and isinstance(data.get("ok"), bool)
                      and data.get("state") in states)
            ver_detail = (f"HTTP {resp.status}，ok={data.get('ok')} state={data.get('state')} "
                          f"latency={data.get('latency_ms')} "
                          f"message={str(data.get('message'))[:70]!r}")
        except Exception as exc:  # noqa: BLE001
            ver_detail = f"POST /mineru/verify 失败：{exc}"
        failures += 0 if ver_ok else 1
        results.append({"kind": "mineru-api", "path": "POST /mineru/verify", "ok": ver_ok,
                        "detail": ver_detail})
        print(f"[{'PASS' if ver_ok else 'FAIL'}] {'POST verify':<16} {ver_detail}")

        # ---------------- ⑮ 公式面板：译文优先 / 未翻译的页用原文兜底 ----------------
        # 用户报过（2026-09-28）："联网解析没有显示公式（但其实有公式的）" —— 真因是公式列表
        # 原来只从**页级产物（译文）**抽，一篇还没翻译的论文于是永远空着，而正文里明明有公式。
        # 这一节拿一篇"公式来自解析产物"的论文（`source=parse` 或 `mixed`）真开一次阅读器。
        print()
        print("=" * 78)
        print("⑮ 公式面板（译文优先；未翻译的页用原文兜底）")
        print("=" * 78)
        if args.block_markers:
            fb_paper = find_formula_fallback_paper(base, items) if items else ""
            print(f"用于公式兜底用例的论文：{fb_paper or '（库里没有未翻译但有公式的论文）'}")
            if browser and fb_paper:
                probe_file = ROOT / "frontend" / "dist" / "_formula_probe.html"
                try:
                    probe_file.write_text(
                        FORMULA_PROBE.replace(
                            "__URL__",
                            f"{base}/app/reader/{fb_paper}?side=formulas&nosse=1")
                        .replace("__WAIT__", "7000"), encoding="utf-8")
                    dom = render(browser, f"{base}/app/_formula_probe.html", 25000, "1500,1000")
                    m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
                    data = None
                    if m and m.group(1).strip() != "waiting":
                        try:
                            data = json.loads(m.group(1))
                        except json.JSONDecodeError:
                            data = None
                    api = _formulas_api(base, fb_paper)
                    checks = [
                        # ① 接口侧确实走的是兜底（否则这一节什么都没证明）
                        ("论文的公式来自原文兜底",
                         api.get("source") in ("parse", "mixed") and (api.get("count") or 0) > 0,
                         f"source={api.get('source')} count={api.get('count')} "
                         f"兜底页={api.get('fallback_pages')}"),
                        # ② 面板里有条目，且不再显示"没有可提取的公式"
                        ("面板渲染出公式条目",
                         (data or {}).get("items", 0) > 0
                         and not (data or {}).get("emptyText"),
                         f"条目数={(data or {}).get('items')} 空态文案="
                         f"{(data or {}).get('emptyText')!r}"),
                        # ③ 兜底条目带「解析」徽章（让用户看出它不来自译文）
                        ("兜底条目标了「解析」", (data or {}).get("parseBadges", 0) > 0,
                         f"「解析」徽章={(data or {}).get('parseBadges')}，"
                         f"徽章文案={(data or {}).get('badges')}"),
                        # ④ 页签角标 = 条目数（不能出现"角标 0 但有条目"这种自相矛盾）
                        ("公式页签角标 = 条目数",
                         (data or {}).get("tabBadge") is not None
                         and (data or {}).get("tabBadge") == (data or {}).get("items"),
                         f"角标={(data or {}).get('tabBadge')} 条目={(data or {}).get('items')}"),
                        # ⑤ 行间/行内/全部的计数与实际条目一致
                        ("筛选条计数自洽",
                         (data or {}).get("allCount") == (data or {}).get("items"),
                         f"全部={(data or {}).get('allCount')} 条目={(data or {}).get('items')}"),
                    ]
                    for label, ok, detail in checks:
                        failures += 0 if ok else 1
                        results.append({"kind": "formula-panel", "path": label, "ok": ok,
                                        "detail": detail})
                        print(f"[{'PASS' if ok else 'FAIL'}] {label:<16} {detail}")
                finally:
                    probe_file.unlink(missing_ok=True)
            else:
                print("[SKIP] 需要浏览器与「未翻译但有公式」的论文")
        else:
            print("[SKIP] 加 --block-markers 才跑这一节")

    print()
    print("=" * 78)
    print("⑯ 搜索栏「有字符变动就搜」（论文库搜索岛：打字不按回车，列表自己收窄）")
    print("=" * 78)
    if browser:
        probe = SEARCH_LIVE_PROBE.replace("__URL__", f"{base}/app/library?nosse=1")
        data = dom_probe(browser, base, probe, "/library", 9000, 34000, DESKTOP) or {}
        before, after, none = data.get("before"), data.get("after"), data.get("none")
        ok = (isinstance(before, int) and before >= 2
              and isinstance(after, int) and 1 <= after < before and none == 0)
        results.append({"kind": "search-live", "path": "论文库搜索岛打字即搜", "ok": ok,
                        "detail": f"卡片数：打字前={before} → 打 turbo={after} → "
                                  f"打不匹配的词={none}；命中标题={data.get('titles')}"})
        print(f"[{'PASS' if ok else 'FAIL'}] 论文库搜索岛打字即搜  "
              f"卡片数：打字前={before} → 打 turbo={after} → 打不匹配的词={none}"
              f"（全程只改输入框的值，没有按回车）")
    else:
        print("[SKIP] 需要浏览器")

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
    print("⑰ 侧栏「对话」面板：两级 + 图片上传 + 一条消息（需 agent 服务在跑）")
    print("=" * 78)
    if not args.chat:
        print("[SKIP] 加 --chat 才跑这一节（要 `pr.py agent` 在跑，且会真调一次模型）")
    elif not browser:
        print("[SKIP] 需要浏览器")
    else:
        probe = CHAT_PROBE.replace("__URL__", f"{base}/app/reader/{paper}?side=chat&nosse=1")
        probe = probe.replace("__WAIT__", "9000").replace("__PNG_B64__", PROBE_PNG_B64)
        probe_file = ROOT / "frontend" / "dist" / "_chat_probe.html"
        try:
            probe_file.write_text(probe, encoding="utf-8")
            dom = render(browser, f"{base}/app/_chat_probe.html", args.budget * 8, DESKTOP)
            data = None
            m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
            if m and m.group(1).strip() != "waiting":
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    data = None
            data = data or {}
            tabs = data.get("tabs") or []
            idx = data.get("tabIndex", -1)
            segs = data.get("segs") or []
            checks = [
                # 「对话」必须紧跟「目录」（用户口径：侧栏第二个）
                ("「对话」页签排第二", idx == 1 and "对话" in (tabs[1] if len(tabs) > 1 else ""),
                 f"页签={tabs}"),
                # ① 一级页：搜索 / 分类 / 批量管理 / 会话行
                ("一级页就位（搜索框 + 分类 + 管理）",
                 all([data.get("hasListHead"), segs == ["全部", "划词", "常规"],
                      data.get("hasManage")]),
                 f"宽 {data.get('chatWidth')}px 分类={segs}"),
                ("会话行渲染出来（真实历史）", data.get("rows", 0) > 0, f"{data.get('rows')} 行"),
                ("批量管理：可选 + 出现批量条 + 能退出",
                 all([data.get("tickCount", 0) > 0, data.get("batchBar"),
                      data.get("batchCount"), data.get("batchOff")]),
                 f"勾选 {data.get('tickCount')} 个，条={data.get('batchCount')!r}"),
                # ② 二级页：输入框文案 / 空态随机文案 / 加号主题色
                ("新建 → 二级页（输入框文案 = 请输入问题）",
                 bool(data.get("threadHead")) and data.get("placeholder") == "请输入问题",
                 f"placeholder={data.get('placeholder')!r}"),
                ("空态文案是「与大肥鱼一起讨论…」中随机一条",
                 bool(data.get("headlineStartsWithPersona")),
                 f"{data.get('headline')!r}"),
                ("加号按钮是主题色（列表页与输入区各一个）",
                 bool(data.get("plusIsAccent")) and bool(data.get("listPlusIsAccent")),
                 f"输入区={data.get('plusBg')} 列表={data.get('listPlusBg')} "
                 f"accent={data.get('accentBg')}"),                # ③ 图片上传（不限张数：input 带 multiple）
                ("图片上传入口（multiple file input）",
                 bool(data.get("hasFileInput")),
                 "input[type=file][multiple]"),
                ("上传后出现缩略图（真的落到了后端）",
                 data.get("pendingAtts", 0) >= 1 and data.get("attImgLoaded"),
                 f"{data.get('pendingAtts')} 张"),
                # ④ 一轮问答
                ("面板就位（圆环 / 模型名）",
                 all([data.get("hasRing"), bool(data.get("model"))]),
                 f"模型={data.get('model')}"),
                ("发得出去（打字后发送键可用）", data.get("sendDisabled") is False,
                 f"sendDisabled={data.get('sendDisabled')}"),
                ("回答落地（流结束后不再是流式态）",
                 bool(data.get("chatAnswer")) and data.get("chatMsgs", 0) > 0
                 and not data.get("chatStreamingLeft"),
                 f"{data.get('chatMsgs')} 条；回答={str(data.get('chatAnswer'))[:40]!r}"),
                ("提问里的图片留在气泡上", data.get("userAtts", 0) >= 1,
                 f"{data.get('userAtts')} 张"),
                ("引用渲染成可点 chip", bool(data.get("chatCites")), f"{data.get('chatCites')}"),
                ("助手消息带分叉/复制入口", bool(data.get("hasBranchTool")), ""),
                # ⑤ 返回一级
                ("返回一级页（历史里多了一条）",
                 bool(data.get("backToList")) and data.get("rowsAfter", 0) > 0,
                 f"{data.get('rows')} → {data.get('rowsAfter')} 行"),
                ("没有报错", not data.get("chatErr") and not data.get("error"),
                 f"err={data.get('chatErr')!r} probe={data.get('error')!r}"),
            ]
            for label, ok, detail in checks:
                failures += 0 if ok else 1
                results.append({"kind": "chat", "path": label, "ok": ok, "detail": detail})
                print(f"[{'PASS' if ok else 'FAIL'}] {label:<24} {detail}")
        finally:
            probe_file.unlink(missing_ok=True)

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

