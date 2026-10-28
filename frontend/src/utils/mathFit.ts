/**
 * 行内公式的**自适应宽度**：放得下原样，放不下等比缩小；缩到下限还放不下才独占一行横滑。
 *
 * 为什么需要（用户口径 2026-12-28）：对照栏这种窄栏里，一张 `$…$` 长公式会把一栏占满甚至溢出 ——
 * 浏览器以前允许它在 KaTeX 的 `.base` 单元之间断行，于是公式被切成两三行、末尾的中文句号
 * 又被挤到下一行单独站着。三条症状（公式断行 / 高亮没跟着换行 / 句号单独一行）都从这里来。
 *
 * 分工（CSS 那边只有一条是"尺寸"，其余是"盒子形态"，都写在 `styles/base.css`）：
 *   - `.katex:not(.katex-display)` 定成 `white-space: nowrap` —— 公式是一个原子，永不内部断行；
 *   - `--katex-fit`（这里量、这里写）→ 那条 `font-size: calc(1.21em * var(--katex-fit))` 等比缩；
 *   - `.katex-tail`（这里判断）：公式后面紧跟中文收尾标点时留出标点的位置，标点不再单独成行；
 *   - `.katex-wide`（这里兜底）：缩到 `MIN_FIT` 还放不下 → 独占一行、居中、自己横滑。
 *
 * 为什么量 `clientWidth` / `scrollWidth` 就够了：`.katex` 的 `max-width: 100%` 的百分比是
 * **包含块的宽度**，所以它自己就是"这一行有多少地方"的答案 —— 不用去猜祖先的 padding / 边框。
 * `nowrap` 之后 `scrollWidth` 就是公式的自然宽度（以前会被断行压回去，两值相等，量不出来）。
 *
 * ⚠️ 只在**插入 DOM 之后**量：`mdToHtml` 返回的是字符串，那时一行都还没排。
 */

/** 缩放到这个比例就到底了，再小正文里的公式就和注脚一样难认 —— 宁可让它独占一行横滑。
 *  0.65 是量出来的：对照栏 430px（减去标点让位后行内可用 367px）、那张自然宽 530px 的
 *  D_prod 公式落在 0.69 —— 16px 正文里公式缩到 13.4px，比公式内部的下标还大，读得清。 */
const MIN_FIT = 0.65;
/** 中文的**行首禁则**字符：标点落在行首就是排错了，所以公式后面跟着它们时要让位 */
const CLOSERS = /^[。，、；：？！）〕〉》」』】”’·…—,.;:!?)\]}]/;
/** 行内"外壳"标签：公式常被高亮 `<mark>` / `<strong>` 之类包着，找"后面那个字"要跳出去 */
const INLINE_WRAPPER = new Set(['mark', 'strong', 'em', 'span', 'a', 'code', 'b', 'i', 'sup', 'sub', 'button', 'del', 's']);

/** 公式之后**紧挨着的那个字符**（跳过空白；跳过行内外壳；遇到另一枚公式就不算） */
function nextChar(node: Node | null, depth = 0): string {
  let cur = node;
  while (cur && depth < 12) {
    if (cur.nodeType === Node.TEXT_NODE) {
      const value = cur.nodeValue ?? '';
      if (value.trim()) return value.trimStart().charAt(0);
      cur = cur.nextSibling;
      continue;
    }
    if (cur.nodeType === Node.ELEMENT_NODE) {
      const el = cur as HTMLElement;
      if (el.classList.contains('katex')) return '';        // 紧挨着另一枚公式：没有标点跟着
      const inner = nextChar(el.firstChild, depth + 1);
      if (inner) return inner;
    }
    cur = cur.nextSibling;
  }
  return '';
}

/** `el` 之后的内容起点：自己被 `<mark>` 之类包着时，往上爬到那个外壳的后面 */
function afterWrappers(el: HTMLElement): Node | null {
  let cur: Node = el;
  for (let hop = 0; hop < 8; hop += 1) {
    if (cur.nextSibling) return cur.nextSibling;
    const parent = cur.parentElement;
    if (!parent || !INLINE_WRAPPER.has(parent.tagName.toLowerCase())) return null;
    cur = parent;
  }
  return null;
}

/**
 * 收集**真正的行内公式**：`.katex:not(.katex-display)` 会把**行间公式的内层 `.katex`** 也收进来
 * （`.katex-display > .katex` 确实不是 `.katex-display`）。那种盒子是整栏宽的，
 * 内容超出时它本来就要横向滑动（用户口径 2026-12-07：「修法不应该是支持横向滚动吗」），
 * 绝不能被当成"行内放不下"缩掉 —— 所以按父节点把它排掉。
 */
function inlineMathIn(seeds: HTMLElement[], root: ParentNode): HTMLElement[] {
  const out: HTMLElement[] = [];
  const push = (el: HTMLElement) => {
    if (el.classList.contains('katex-display')) return;
    if (el.parentElement?.classList.contains('katex-display')) return;   // 行间公式的内层
    out.push(el);
  };
  for (const seed of seeds) {
    if (seed.matches('.katex')) push(seed);
    seed.querySelectorAll<HTMLElement>('.katex').forEach(push);
  }
  if (seeds.length === 0) root.querySelectorAll<HTMLElement>('.katex').forEach(push);
  return out;
}

/** 这批公式：复位 → 量 → 写缩放 / 打标记（读与写各走一趟，别让浏览器反复重排） */
function fitList(list: HTMLElement[]): void {
  if (!list.length) return;
  /* ① 复位 + 标点让位（写）。复位是必须的：同一个节点会被反复量（窗口变宽、栏变宽、字号变化），
     上一次写的缩放不清掉，第二次量到的就是"缩过之后"的宽度，越缩越小。 */
  for (const el of list) {
    el.classList.remove('katex-wide');
    if (el.style.getPropertyValue('--katex-fit')) el.style.removeProperty('--katex-fit');
    el.classList.toggle('katex-tail', CLOSERS.test(nextChar(afterWrappers(el))));
  }
  /* ② 量（一次布局） */
  const jobs: Array<{ el: HTMLElement; box: number; natural: number; fit: number }> = [];
  for (const el of list) {
    const box = el.clientWidth;                            // 这一行能用的宽度（含 .katex-tail 让出的位置）
    const natural = Math.max(el.scrollWidth, box);          // 公式的自然宽度（nowrap 之后才量得准）
    /* `+ 2` 的容差：KaTeX 内部有 1~2px 的负间距（`\!` / `\mkern`），几乎每条都"多出 2px"，
       不设容差的话每一条都会被当成放不下而缩一丁点（等于全站字号悄悄变小）。 */
    if (box <= 0 || natural <= box + 2) continue;            // 隐藏的行（0）留给下一次；放得下的不动
    jobs.push({ el, box, natural, fit: Math.max(MIN_FIT, (box - 2) / natural) });
  }
  if (!jobs.length) return;
  /* ③ 写缩放 */
  for (const job of jobs) job.el.style.setProperty('--katex-fit', String(Math.round(job.fit * 1000) / 1000));
  /* ④ 兜底：缩到下限仍放不下 → 独占一行（原尺寸 + 横向滑动，内容一个字都不丢） */
  for (const job of jobs) {
    if (job.natural * MIN_FIT <= job.box + 2) continue;
    job.el.style.removeProperty('--katex-fit');
    job.el.classList.add('katex-wide');
  }
}

/** 把 `root` 里（含 root 自己）所有行内公式量一遍。可重复调用：每次都先复位。 */
export function fitInlineMath(root: ParentNode | null = document.body): void {
  const scope = root ?? document.body;
  if (!scope) return;
  fitList(inlineMathIn(scope instanceof HTMLElement ? [scope] : [], scope));
}

/**
 * 全局装一次（`main.ts`）：新插进来的公式自动量；窗口改宽时整页重量一次。
 *
 * 为什么用 MutationObserver 而不是在每个渲染点调 `fitInlineMath`：`mdToHtml` 的调用点有十几个
 * （阅读器行、解读、笔记、知识库、对话回答、浮层…），漏一个就漏一处；而且"插进 DOM"这件事
 * 只有 DOM 自己知道。观察 `childList`（不看 `attributes`）—— 我们自己改 class / style 属于
 * 属性变动，不会把观察器再叫起来，不会自激。
 */
export function bootMathFit(): void {
  if (typeof window === 'undefined' || typeof MutationObserver === 'undefined') return;
  const pending = new Set<HTMLElement>();
  let queued = false;
  const flush = () => {
    queued = false;
    const batch = [...pending];
    pending.clear();
    const only = new Set<HTMLElement>();
    for (const el of batch) {
      if (!el.isConnected) continue;
      for (const k of inlineMathIn([el], el)) only.add(k);
    }
    fitList([...only]);
  };
  const mo = new MutationObserver((records) => {
    for (const rec of records) {
      rec.addedNodes.forEach((node) => {
        if (node.nodeType === Node.ELEMENT_NODE) pending.add(node as HTMLElement);
      });
    }
    if (!pending.size || queued) return;
    queued = true;
    requestAnimationFrame(flush);                 // 一帧内插进来的合成一次，避免逐条量
  });
  mo.observe(document.body, { childList: true, subtree: true });
  let timer = 0;
  window.addEventListener('resize', () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fitInlineMath(document.body), 160);
  });
}
