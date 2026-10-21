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
  /** **只按文字找、不信偏移量**：跨面兜底用（见 ReaderRow 的说明），偏移量在另一面没有意义 */
  loose?: boolean;
  /** **直接信偏移量、跳过文字校验**：跨行选择切出来的那几条共用整段选区文字，
   *  与单行切片天然不相等（见 ReaderRow 的说明）—— 偏移量本身是 DOM 空间里算的，可信。 */
  trust?: boolean;
  /** 公式的 LaTeX（`Highlight.text`）：**优先按它认那枚 KaTeX**，偏移量/序号只当兜底。
   *  为什么：服务端偏移与"第几条"都假定两边公式数量一致，客户端一旦少渲染一枚就整体错位
   *  （用户报「位置不对」）；而 KaTeX 会把 LaTeX 写在 `annotation[encoding=application/x-tex]` 里，
   *  按它找是**内容匹配**，不怕顺序变化。 */
  formula?: string;
  /** 跨面的**公式**：它在另一面里是第几条公式（0 起）。给了就按这一面的第 N 个 ATOM 画，
   *  不走文字匹配 —— 渲染文本里没有 LaTeX（见 `formulaOrdinal`）。 */
  atom?: number;
}

export function clearUserMarks(root: HTMLElement | null): void {
  unwrap(root, 'mark.hl');
  unbandFormula(root);
}

function makeMark(color: string, id: string): HTMLElement {
  const mark = document.createElement('mark');
  /* 命名色走 class（全站配色在 base.css 的 `mark.hl.<color>`）；**其它一律内联** ——
     hex(3/6/8) / rgb() / hsl() 这些「任意色」class 认不出来，会变成透明＝高亮看不见
     （用户口径 2026-11-26：允许 agent 自定义任意颜色）。
     `#rrggbb` 用 `66`（40% 不透明）与命名色观感对齐；带 alpha 或函数写法交给 color-mix。 */
  const c = String(color || '').trim();
  if (/^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(c)) {
    mark.className = 'hl';
    mark.style.background = c.length === 7 ? `${c}66` : c;
  } else if (/^(?:rgba?|hsla?)\(/i.test(c)) {
    mark.className = 'hl';
    mark.style.background = `color-mix(in srgb, ${c} 40%, transparent)`;
  } else {
    mark.className = `hl ${c || 'yellow'}`;
  }
  mark.setAttribute('data-hl', id);
  /* ⚠️ 这里**不要**写 `title`（用户口径 2026-10-08："移除浏览器悬浮 toast 内容"）：
     高亮是"悬浮就出笔记浮层"的那类元素，再叠一条浏览器原生提示（"选中这一段…"）
     只是噪音 —— 删除/编辑的入口在划词菜单与浮层里。 */
  return mark;
}

/** 被 `bandFormula` 挪过位置的编号角标（拆标记时要挪回去） */
const MOVED_TAG = 'data-hl-moved';

/**
 * 把**显示公式**的 `.katex-html` 变成"贴着公式的色带"（`display: inline-block`，见 `base.css`）。
 *
 * 为什么不把底色画在 `mark` 上：`mark` 是**整行宽**的块级盒子（编号 `.tag` 的绝对定位依赖这个
 * 宽度），底色画上去就是"整行涂色"（用户 2026-09-28："标记行间公式，那一行都会被高亮"）。
 * 为什么也不用 KaTeX 的 `.base`：一张公式会被拆成**多个** `.base`（"不可断行单元"，实测 4 个、
 * 高度 26/67px 参差），逐个涂色是锯齿状的，而且它们的行盒还比 `.katex-html` 的块级行盒矮
 * （实测 37px vs 47px）—— 都不等于公式真正的外接框。
 *
 * `.katex-html` 自己就是那个外接框（宽 = 各单元之和，高 = 行盒），但它里面挂着编号 `.tag`
 * （`position: absolute; right: 0`）—— 盒子一收窄，编号就被拽到公式右端、压在公式上。
 * 所以**带编号时把 `.tag` 挪到 `.katex` 下**：`.katex` 是整行宽的块级盒子，
 * `right: 0` 仍然是"整行最右"，与论文里的位置一致，也不会被色带压住。
 */
function bandFormula(displayEl: HTMLElement): void {
  const html = displayEl.querySelector<HTMLElement>('.katex-html');
  if (!html) return;
  const katex = html.parentElement;
  const tag = html.querySelector<HTMLElement>(':scope > .tag');
  if (!katex || !tag || tag.hasAttribute(MOVED_TAG)) return;
  tag.setAttribute(MOVED_TAG, '1');
  katex.appendChild(tag);
}

/** 把挪过的编号角标放回 `.katex-html`（高亮删掉后不该留下痕迹、更不该改论文的结构） */
function unbandFormula(root: HTMLElement | null): void {
  if (!root) return;
  root.querySelectorAll<HTMLElement>(`.katex > .tag[${MOVED_TAG}]`).forEach((tag) => {
    const html = tag.parentElement?.querySelector<HTMLElement>(':scope > .katex-html');
    if (!html) return;
    tag.removeAttribute(MOVED_TAG);
    html.appendChild(tag);
  });
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
      // 块级公式（`.katex-display`）：`mark` 自己得是块级，否则底色只有一条行高（见 `formulaOf`）；
      // 但底色**不画在这个块级外壳上**（那是整行宽），而是画在 `bandFormula` 收出来的
      // `.katex-html` 上（它的大小 = 公式外接框）。
      if (el.classList?.contains('katex-display')) {
        mark.classList.add('block');
        bandFormula(el);
      }
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
/** 公式的两种写法（行间优先，行内不重复计入行间内部）—— 与后端 math_spans 同一口径。 */
const MATH_RE = /\$\$([\s\S]+?)\$\$|(?<!\$)\$(?!\$)([^$\n]+?)\$(?!\$)/g;

/** 公式比对前的规范化：去掉 $ 与所有空白（\\frac {a} {b} 与 \\frac{a}{b} 是同一个式子）。 */
function normLatex(value: string): string {
  return String(value || '').replace(/\$/g, '').replace(/\s+/g, '');
}

/** 这段文字看起来是不是**公式**（没有汉字，且带 $ 或反斜杠命令）。 */
export function isFormulaText(text?: string | null): boolean {
  const probe = String(text ?? '').trim();
  if (!probe || /[\u3400-\u9fff]/.test(probe)) return false;
  return probe.includes('$') || probe.includes('\\');
}

/**
 * 这条公式是那一面里的**第几条**（0 起）；不是公式 / 找不到给 -1。
 *
 * 为什么要"序号"而不是"文字"：高亮挂在另一面时（loose），渲染文本里根本没有 LaTeX
 * （公式被压成 1 个 U+FFFC），按文字找必然落空 —— 以前跨面的公式高亮就是这么不画的。
 * 两面的公式**顺序一致**，所以"另一面里它是第几条"就能在**这一面**指向同一个 ATOM。
 */
export function formulaOrdinal(md: string, probe?: string | null): number {
  const want = normLatex(probe ?? '');
  if (!want) return -1;
  const re = new RegExp(MATH_RE.source, 'g');
  let index = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(md || ''))) {
    const latex = normLatex(match[1] ?? match[2] ?? '');
    const short = Math.min(latex.length, want.length);
    if (latex === want || (short >= 8 && (latex.includes(want) || want.includes(latex)))) {
      return index;
    }
    index += 1;
  }
  return -1;
}

/** KaTeX 节点里的 LaTeX 源码（MathML 的 annotation）；没有就给空串。 */
function atomLatex(el: Element | null | undefined): string {
  if (!el) return '';
  const node = el.querySelector('annotation[encoding="application/x-tex"]');
  return normLatex(node?.textContent ?? '');
}

/**
 * 按 LaTeX 在**这一行**里认出那枚公式 → 它的 ATOM 段；认不出给 null。
 *
 * 先比完全相同，再退到片段包含（两边都 ≥ 8 个字符，免得一个 `x` 撞到别的式子）——
 * 与后端 `find_formula_span` 同一套口径。
 */
function matchFormulaAtom(segs: Seg[], probe: string): Seg | null {
  const want = normLatex(probe);
  if (!want) return null;
  const atoms = segs.filter((s) => s.kind === 'atom');
  for (const seg of atoms) {
    if (atomLatex(seg.el) === want) return seg;
  }
  for (const seg of atoms) {
    const latex = atomLatex(seg.el);
    if (Math.min(latex.length, want.length) < 8) continue;
    if (latex.includes(want) || want.includes(latex)) return seg;
  }
  return null;
}

/** 渲染文本里所有 ATOM 的位置（公式各占 1 个字符）。 */
function atomPositions(text: string): number[] {
  const out: number[] = [];
  for (let i = 0; i < text.length; i += 1) if (text[i] === ATOM) out.push(i);
  return out;
}

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
    if (m.trust) {
      /* 跨行高亮：这条记录的 selected_text 是**整段选区**，与这一行的切片本来就不相等 ——
         拿它做文字校验会把整条高亮判成「对不上」而一条都不画（用户报的「笔记在列表里、
         正文看不到彩色底」）。偏移量是前端在同一套 DOM 遍历里算的，直接信它。 */
      if (wrapRange(segs, start, end, m.color, m.id)) done += 1;
      continue;
    }
    /* 公式：**先按 LaTeX 认出那枚 KaTeX**（内容匹配，不怕顺序变化），认不出才退到序号/偏移。 */
    if (m.formula) {
      const hit = matchFormulaAtom(segs, m.formula);
      if (hit && wrapRange(segs, hit.start, hit.end, m.color, m.id)) {
        done += 1;
        continue;
      }
    }
    /* 跨面的**公式**高亮：文字里没有 LaTeX，只认 ATOM 序号（见 formulaOrdinal）。 */
    if (typeof m.atom === 'number' && m.atom >= 0) {
      const at = atomPositions(all)[m.atom];
      if (typeof at === 'number') {
        if (wrapRange(segs, at, at + 1, m.color, m.id)) done += 1;
      }
      continue;
    }
    if (m.loose) {
      /* 跨面兜底：这条高亮当初标在另一面，偏移量在这一面没有意义 —— 只按文字找。
         找不到就跳过（宁可少画一条，也不要画错位置）。 */
      const want = m.text ?? '';
      let at = want ? all.indexOf(want) : -1;
      if (at < 0 && want) {
        for (const n of [40, 24, 12]) {
          if (want.length < n) continue;
          at = all.indexOf(want.slice(0, n));
          if (at >= 0) break;
        }
      }
      if (at < 0) continue;
      if (wrapRange(segs, at, Math.min(at + (want || '').length, total), m.color, m.id)) done += 1;
      continue;
    }
    const slice = all.slice(start, end);
    const wanted = m.text ?? '';
    if (!slice.includes(ATOM)) {
      const same = slice === wanted || norm(slice) === norm(wanted);
      if (!same) {
        const at = wanted ? all.indexOf(wanted) : -1;      // 兜底：按文字找一个位置
        if (at >= 0) {
          start = at;
          end = Math.min(at + wanted.length, total);
        } else {
          /* 再退一步：拿**前缀**找（40 → 24 → 12 字）。agent 建的高亮偏移量来自服务端渲染的
             近似，差几个字很常见 —— 差一点就整条不画，就成了「笔记在列表里但看不到高亮」
             （用户口径 2026-11-26）。宁可标得略短一点，也不要一条都不出现。 */
          let found = -1;
          for (const n of [40, 24, 12]) {
            if (wanted.length < n) continue;
            found = all.indexOf(wanted.slice(0, n));
            if (found >= 0) break;
          }
          /* 最后一条兜底：**什么都不改，就按记录里的偏移量画**。
             文字校验的初衷是防「重译之后偏移量错位」，但它现在的作用是**整条不画** ——
             用户看到的是「笔记在列表里、正文一片白」（2026-11-26 连报两次）。
             宁可落点差几个字，也要把标记画出来：错位能看见、能改；不画等于功能失效。 */
          if (found >= 0) {
            start = found;
            end = Math.min(found + wanted.length, total);
          }
        }
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
