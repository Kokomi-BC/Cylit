/**
 * 在**已渲染的 DOM** 上做标记：搜索命中高亮 + 用户高亮（按字符偏移量）。
 *
 * 为什么都在 DOM 上做，而不是像最早那样先替换 Markdown 源：
 * - 搜索词是用户随手输入的（`^`、`_`、`[` 都可能有），拿去替换 Markdown 源会撞上下标/强调/链接语法；
 * - **公式没法在源里标**：`$…$` 的字符和渲染出来的字形不是一回事，插个标记进去还会把 KaTeX 弄坏；
 * - Markdown 视图里的文字是渲染之后的，源文件里的偏移和看到的文字对不上。
 * 所以统一口径：**偏移量一律相对"渲染后的文字"**。
 *
 * 公式（`.katex`，含跨行的 `\begin{aligned}` 那种 `.katex-display`）按**一个原子单位**处理：
 * - 在拼接文本里占 1 个字符（U+FFFC，对象替换符），所以"选区含公式"时偏移量照样算得出来；
 * - 画的时候**整块套一层 `<mark>`**（不去动 KaTeX 内部的 span —— 它的排版靠定位与负边距，
 *   从中间切开必然错位）。于是跨行公式也能整体高亮。
 *
 * 标记都只是"在文本/元素外面套一层"，不改文档结构、**不改变文字长度** ——
 * 多条标记之间不会互相错位（`v-html` 重设 innerHTML 时这些标记本来就会被抹掉，随后再包一遍）。
 */

const SEARCH_CLASS = 'srch';
/** 拼接文本里代表"一个公式"的占位字符 */
export const ATOM = '\uFFFC';
/** 这些子树完全不参与标记：脚本 / 样式 / 矢量图 */
const SKIP = new Set(['SCRIPT', 'STYLE', 'SVG']);
/** 公式里的不可见副本（KaTeX 给无障碍输的 MathML）：不参与标记，也不计入偏移量 */
const MATHML = 'katex-mathml';

function isSkipped(node: Node): boolean {
  let el = node.nodeType === Node.TEXT_NODE ? node.parentElement : (node as HTMLElement);
  while (el) {
    if (SKIP.has(el.tagName)) return true;
    if (el.classList?.contains(MATHML)) return true;
    el = el.parentElement;
  }
  return false;
}

/**
 * 这个文本节点属于哪个公式（不在公式里则返回 null）。
 *
 * 显示公式必须取**最外层的 `.katex-display`**，不能取内层 `.katex`：
 * KaTeX 的显示公式是 `.katex-display > .katex > …`，外层是块级盒子（`\begin{aligned}` 的每一行
 * 都在它里面），内层 `.katex` 是行内盒子（高度只有一个行高）。
 * `<mark>` 套在内层上时，浏览器只会把底色画在那一条行高上 —— 三行公式看起来"只有中间一行被标黄"
 * （用户报过："公式高亮"不对）。套在最外层块级元素上，底色才覆盖整块公式。
 */
function formulaOf(node: Node): HTMLElement | null {
  let el = node.nodeType === Node.TEXT_NODE ? node.parentElement : (node as HTMLElement);
  let katex: HTMLElement | null = null;
  while (el) {
    if (el.classList?.contains('katex-display')) return el;
    if (!katex && el.classList?.contains('katex')) katex = el;
    el = el.parentElement;
  }
  return katex;
}

/** 容器里的有序段：普通文本段（按字符）+ 公式段（整体算 1 个字符） */
export interface Seg {
  kind: 'text' | 'atom';
  node?: Text;
  el?: HTMLElement;
  start: number;
  end: number;
}

/** 偏移量、画标记、算选区三件事共同的坐标系 */
export function segments(root: Node | null): Seg[] {
  if (!root) return [];
  if (root.nodeType === Node.TEXT_NODE) {
    const text = root as Text;
    return [{ kind: 'text', node: text, start: 0, end: (text.nodeValue ?? '').length }];
  }
  const out: Seg[] = [];
  const seen = new Set<HTMLElement>();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let cursor = 0;
  let node = walker.nextNode();
  while (node) {
    const text = node as Text;
    const value = text.nodeValue ?? '';
    const formula = formulaOf(text);
    if (formula) {
      if (!seen.has(formula)) {                  // 一个公式只记一次（内部有很多文本节点）
        seen.add(formula);
        out.push({ kind: 'atom', el: formula, start: cursor, end: cursor + 1 });
        cursor += 1;
      }
    } else if (value.trim() && !isSkipped(text)) {
      out.push({ kind: 'text', node: text, start: cursor, end: cursor + value.length });
      cursor += value.length;
    }
    node = walker.nextNode();
  }
  return out;
}

/** 容器里"看得见的文字"（公式算 1 个 U+FFFC），与偏移量同一套口径 */
export function renderedText(root: Node | null): string {
  return segments(root).map((s) => (s.kind === 'atom' ? ATOM : (s.node?.nodeValue ?? ''))).join('');
}

/** 搜索用：只要普通文本节点（公式的字形是拼出来的，命中概率极低，而且套标记会破坏它的排版） */
export function textNodes(root: Node | null): Text[] {
  return segments(root).filter((s) => s.kind === 'text').map((s) => s.node as Text);
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

const norm = (text: string) => (text ?? '').replace(/\s+/g, '');

/** 摘掉一类标记：文本标记还原成文本，**公式标记要把里面的元素搬回来**（不能拍平成文字） */
function unwrap(root: HTMLElement | null, selector: string): void {
  if (!root) return;
  const marks = root.querySelectorAll(selector);
  if (!marks.length) return;
  const parents = new Set<Node>();
  marks.forEach((m) => {
    const parent = m.parentNode;
    if (!parent) return;
    const inner = m.firstElementChild as HTMLElement | null;
    const hasElement = !!inner && m.childNodes.length === 1;
    parent.replaceChild(hasElement ? inner! : document.createTextNode(m.textContent ?? ''), m);
    parents.add(parent);
  });
  parents.forEach((p) => (p as HTMLElement).normalize?.());
}

// --------------------------------------------------------------------------
// 搜索命中：按**词**匹配，全部高亮
// --------------------------------------------------------------------------

export function clearSearchMarks(root: HTMLElement | null): void {
  unwrap(root, `mark.${SEARCH_CLASS}`);
}

/** 把 `term` 在 `root` 里的**所有**出现包成 `mark.srch`，返回标记个数（大小写不敏感）。 */
export function markSearchTerm(root: HTMLElement | null, term: string): number {
  if (!root) return 0;
  clearSearchMarks(root);
  const needle = (term ?? '').trim();
  if (!needle) return 0;

  const re = new RegExp(escapeRegExp(needle), 'gi');
  let count = 0;
  for (const text of textNodes(root)) {
    const raw = text.nodeValue ?? '';
    re.lastIndex = 0;
    if (!re.test(raw)) continue;
    re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const m of raw.matchAll(re)) {
      const at = m.index ?? 0;
      if (at > last) frag.appendChild(document.createTextNode(raw.slice(last, at)));
      const mark = document.createElement('mark');
      mark.className = SEARCH_CLASS;
      mark.textContent = m[0];
      frag.appendChild(mark);
      last = at + m[0].length;
      count += 1;
    }
    if (last < raw.length) frag.appendChild(document.createTextNode(raw.slice(last)));
    text.parentNode?.replaceChild(frag, text);
  }
  return count;
}

// --------------------------------------------------------------------------
// 用户高亮：按**字符偏移量**套一层 `<mark class="hl">`
// --------------------------------------------------------------------------

export interface DomMark {
  start: number;
  end: number;
  /** 高亮颜色（yellow/green/blue/pink/purple） */
  color: string;
  /** 高亮 id：挂在 `data-hl` 上，菜单里删除时用 */
  id: string;
  /** 当初选中的文字：偏移量对不上时用它兜底重定位 */
  text?: string | null;
}

export function clearUserMarks(root: HTMLElement | null): void {
  unwrap(root, 'mark.hl');
}

function makeMark(color: string, id: string): HTMLElement {
  const mark = document.createElement('mark');
  mark.className = `hl ${color || 'yellow'}`;
  mark.setAttribute('data-hl', id);
  mark.title = '选中这一段，可从菜单删除高亮';
  return mark;
}

/** 把 [start, end) 画上：文本段逐段包，公式段整体搬进 `<mark>` 里。 */
function wrapRange(segs: Seg[], start: number, end: number, color: string, id: string): boolean {
  let wrapped = false;
  for (const seg of segs) {
    const s = Math.max(start, seg.start);
    const e = Math.min(end, seg.end);
    if (s >= e) continue;
    if (seg.kind === 'atom') {
      const el = seg.el;
      if (!el?.parentNode) continue;
      const mark = makeMark(color, id);
      // 块级公式（`.katex-display`）：`mark` 自己也得是块级，否则底色只有一条行高（见 `formulaOf`）
      if (el.classList?.contains('katex-display')) mark.classList.add('block');
      el.parentNode.replaceChild(mark, el);
      mark.appendChild(el);
      wrapped = true;
      continue;
    }
    const node = seg.node;
    if (!node?.parentNode) continue;
    const localS = s - seg.start;
    const target = localS > 0 ? node.splitText(localS) : node;
    const width = e - s;
    if (width < (target.nodeValue ?? '').length) target.splitText(width);
    const mark = makeMark(color, id);
    target.parentNode?.replaceChild(mark, target);
    mark.appendChild(target);
    wrapped = true;
  }
  return wrapped;
}

/**
 * 按偏移量画高亮，返回画上的条数。
 *
 * 校验：偏移量来自同一套 DOM 遍历，基本可信；但内容被重译之后旧偏移会错位，所以仍然对一次文本。
 * **选区里含公式时跳过文本比对** —— 那时 `selected_text` 是 KaTeX 的字形文字（还带着 MathML 的
 * 重复副本），跟拼接文本（公式 = 1 个占位符）永远对不上。真的一点都对不上就跳过：
 * 宁可少画一条，也不要画错位置。
 */
export function markRanges(root: HTMLElement | null, marks: DomMark[]): number {
  if (!root) return 0;
  clearUserMarks(root);
  const list = (marks ?? []).filter((m) => m && m.end > m.start)
    .slice().sort((a, b) => a.start - b.start);
  if (!list.length) return 0;

  let done = 0;
  for (const m of list) {
    const segs = segments(root);                 // 每次都重收集：splitText 会换节点对象
    const total = segs.length ? segs[segs.length - 1].end : 0;
    if (!total) break;
    let start = m.start;
    if (start < 0 || start >= total) continue;
    let end = Math.min(m.end, total);
    if (end <= start) continue;
    const all = renderedText(root);
    const slice = all.slice(start, end);
    const wanted = m.text ?? '';
    if (!slice.includes(ATOM)) {
      const same = slice === wanted || norm(slice) === norm(wanted);
      if (!same) {
        const at = wanted ? all.indexOf(wanted) : -1;      // 兜底：按文字找一个位置
        if (at < 0) continue;
        start = at;
        end = Math.min(at + wanted.length, total);
      }
    }
    if (wrapRange(segs, start, end, m.color, m.id)) done += 1;
  }
  return done;
}

/** 某个节点（可能是元素）在拼接文本里的起始/结束偏移量 */
function offsetOfNode(segs: Seg[], node: Node, fromStart: boolean): number | null {
  const nodeOf = (s: Seg) => (s.kind === 'atom' ? (s.el as Node) : (s.node as Node));
  if (node.nodeType === Node.TEXT_NODE) {
    const formula = formulaOf(node);
    const seg = formula
      ? segs.find((s) => s.kind === 'atom' && s.el === formula)
      : segs.find((s) => s.kind === 'text' && s.node === node);
    if (!seg) return nearestOffset(segs, node, fromStart);
    return fromStart ? seg.start : seg.end;
  }
  const inside = segs.filter((s) => nodeOf(s) && node.contains(nodeOf(s)));
  if (!inside.length) return nearestOffset(segs, node, fromStart);
  return fromStart ? inside[0].start : inside[inside.length - 1].end;
}

/**
 * 边界落在**不在坐标系里的节点**上时（纯空白文本节点 —— 划到段落末尾/换行处很常见），
 * 取最近的一个真实文本边界：起点往后找，终点往前找。
 * 不做这一步的话，这种边界会让整行算不出偏移量，那一行的高亮就整段丢了。
 */
function nearestOffset(segs: Seg[], node: Node, fromStart: boolean): number | null {
  if (!segs.length) return null;
  const nodeOf = (s: Seg) => (s.kind === 'atom' ? (s.el as Node) : (s.node as Node));
  if (fromStart) {
    const after = segs.find((s) => {
      const n = nodeOf(s);
      return n && (node.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
    });
    return after ? after.start : segs[segs.length - 1].end;
  }
  const before = [...segs].reverse().find((s) => {
    const n = nodeOf(s);
    return n && (node.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_PRECEDING) !== 0;
  });
  return before ? before.end : segs[0].start;
}

/**
 * 选区在容器里的字符偏移量（与 `markRanges` 同一套口径；公式算 1 个字符）。
 * 端点落在公式内部时**吸附到公式边界** —— 公式是整体标的，不会只标半个。
 */
export function rangeOffsets(root: HTMLElement | null, range: Range): { start: number; end: number } | null {
  if (!root || !root.contains(range.startContainer) || !root.contains(range.endContainer)) {
    return null;
  }
  const list = segments(root);
  const at = (node: Node, offset: number, isStart: boolean): number | null => {
    const formula = formulaOf(node);
    if (formula) {
      const seg = list.find((s) => s.kind === 'atom' && s.el === formula);
      if (seg) return isStart ? seg.start : seg.end;
    }
    if (node.nodeType === Node.TEXT_NODE) {
      const seg = list.find((s) => s.kind === 'text' && s.node === node);
      if (!seg) return nearestOffset(list, node, isStart);
      return seg.start + Math.min(offset, (node.nodeValue ?? '').length);
    }
    const children = Array.from(node.childNodes);
    for (let i = offset; i < children.length; i += 1) {
      const first = offsetOfNode(list, children[i], true);
      if (first !== null) return first;
    }
    for (let i = Math.min(offset, children.length) - 1; i >= 0; i -= 1) {
      const last = offsetOfNode(list, children[i], false);
      if (last !== null) return last;
    }
    return null;
  };
  const start = at(range.startContainer, range.startOffset, true);
  const end = at(range.endContainer, range.endOffset, false);
  if (start === null || end === null || end <= start) return null;
  return { start, end };
}
