/**
 * Markdown + 公式渲染（marked + marked-katex-extension + katex）。
 *
 * 为什么用 marked-katex-extension 而不是"先抠公式再交给 marked"：
 * 事实源里同时存在 ① 原生解析块（纯文本）② 扫描页块（模型返回的 Markdown，含 `$…$`/`$$…$$`）。
 * 若先交给 marked 再补渲染公式，`\{0,1\}^{b \cdot d}` 这类 LaTeX 的反斜杠会被 marked 的转义
 * 规则吃掉、下划线会被当成强调。扩展在 tokenizer 阶段就接管公式，从根上避免这个问题。
 */

import { Marked } from 'marked';
import markedKatex from 'marked-katex-extension';
import katex from 'katex';
import type { Highlight } from '@/api/types';

const marked = new Marked();
marked.use(markedKatex({
  throwOnError: false,
  nonStandard: true,          // 允许 `$…$` 单美元行内公式（论文里很常见）
  output: 'html',
  errorColor: '#d8453f',
}));
// 论文正文里出现 `<` 时不能被当成 HTML 注入：HTML 令牌一律转义输出
marked.use({
  renderer: {
    html: (token: any) => escapeHtml(typeof token === 'string' ? token : (token?.text ?? '')),
  },
});

const CACHE_LIMIT = 3000;
const cache = new Map<string, string>();

/** 高置信度的 LaTeX 命令（整行都是这些命令时，按独立公式处理） */
const STRONG_TEX = /\\(?:frac|dfrac|tfrac|sqrt|sum|prod|int|iint|oint|begin|end|tag|quad|qquad|mathbb|mathcal|mathfrak|operatorname|overline|widehat|widetilde|langle|rangle|lVert|rVert|Vert|partial|nabla|infty|cdot|cdots|ldots|dots|times|div|pm|le|leq|ge|geq|neq|approx|sim|equiv|propto|subset|supset|cup|cap|oplus|otimes|to|mapsto)\b/;
/** 同上，但要计数出现次数（`g` 标志） */
const STRONG_TEX_G = new RegExp(STRONG_TEX.source, 'g');
const CJK = /[\u3400-\u9fff\uf900-\ufaff]/;
/** 行内裸公式的“词”：连续的非空白/非中文标点字符 */
const TOKEN = /[^\s，。；：、（）()【】「」“”《》,.;:]+/g;

/**
 * 把**裸写的 LaTeX**补上公式定界符。
 *
 * 为什么会裸写：模型偶尔忘了包 `$…$`，直接写 `Q_mse^{-1}`、`S^{d-1}`、`\text{(MSE)} \quad \mathbb{E}`。
 * 渲染出来就是原文带花括号/反斜杠的样子（用户报过 "markdown 问题"），而且 `_` 还可能被当成强调。
 * 提示词已经加了硬要求（见 `page_translate.SYSTEM_PROMPT`），这里再挡一道，**存量产物不用重译也能好看**。
 *
 * 守则：宁可少补，也不要误把普通文字塞进公式 —— 只在两种情况下动手：
 *   ① 整行就是一段公式（含高置信 LaTeX 命令且**没有中文**）→ 整行包 `$$…$$`；
 *   ② 某个“词”里出现 `^{` / `_{` → 只把这个词包进 `$…$`。
 * 已有 `$` 的行、代码围栏、以及不常见的写法一律不动。
 */
export function wrapBareMath(src: string): string {
  if (!src || (!src.includes('\\') && !src.includes('^{') && !src.includes('_{'))) return src;
  let inFence = false;
  let inBlockMath = false;
  return src.split('\n').map((line) => {
    if (line.trimStart().startsWith('```')) { inFence = !inFence; return line; }
    if (inFence) return line;
    const dollars = (line.match(/\$\$/g) || []).length;
    if (inBlockMath) { if (dollars % 2 === 1) inBlockMath = false; return line; }
    if (dollars % 2 === 1) { inBlockMath = true; return line; }
    if (line.includes('$')) return line;                  // 这行已经有公式了，别添乱
    const t = line.trim();
    /* 整行当公式要更严：至少两个高置信命令，或者带 `^{`/`_{` ——
     * 免得把 "we map x \\to y" 这种英文句子整句当成公式。 */
    const strong = t ? (t.match(STRONG_TEX_G) || []).length : 0;
    if (t && !CJK.test(t) && STRONG_TEX.test(t) && (strong >= 2 || /[_^]\{/.test(t))) return `$$${t}$$`;
    return line.replace(TOKEN, (tok) => {
      if (!/[_^]\{/.test(tok)) return tok;
      if (tok.includes('$') || tok.includes('`') || tok.includes('\\') || tok.startsWith('http')) return tok;
      if (tok.length > 60 || CJK.test(tok)) return tok;
      return `$${tok}$`;
    });
  }).join('\n');
}

export function escapeHtml(text: unknown): string {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * 把块内 Markdown 的**相对资源路径**改写成后端静态资源 URL。
 *
 * 为什么需要：`md/full.md` 是相对 `data/papers/<id>/md/` 写的（`../figures/x.png`、
 * `../formula_crops/y.png`），浏览器按当前页面地址解析会变成 `/app/figures/...` → 404。
 */
export function rewriteAssetPaths(html: string, paperId?: string | null): string {
  if (!paperId || !html) return html;
  return html.replace(
    /(src|href)="(?:\.\.\/)+(figures|formula_crops|pages)\/([^"]+)"/g,
    (_m, attr: string, dir: string, name: string) => `${attr}="/papers/${paperId}/${dir}/${name}"`,
  );
}

/* ---------------- 表格：论文式「三线表」（booktabs） ----------------
 *
 * 为什么要在 HTML 上打类，而不是只用 CSS：CSS 看不见单元格里的**内容**。
 *   ① 论文表格里数字列右对齐、文字列左对齐（表头压在数字列正中）—— 哪一列是数字列只能按列统计；
 *   ② 论文用 `\cmidrule` 把不同数据集分成几块，HTML 表格没有这个概念 ——
 *      第一列（分组列，如"数据集"）出现空单元格时，就知道它分了组，于是在每块的**第一行**补一条细线。
 * 外加把每张表包进 `.tbl-wrap`（横向滚动容器）：宽表**不许把整页顶宽**
 * （`frontend_check` 的"整页不滚动"是永久回归项）。
 *
 * 只做这三件事，样式全部在 `base.css` 的 `.md table` —— 阅读器行、Markdown 视图、知识库、
 * AI 解读共用一套观感（以前三处各写一套，长得都不一样）。
 */
/** 纯数字单元格：`0.578` / `1,234` / `87%`（负号连字符、真减号都认） */
const NUMERIC_CELL = /^[+\-−]?\d[\d.,]*(?:\s*[%‰])?$/;
const ENTITIES: Record<string, string> = {
  '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'", '&nbsp;': ' ',
};

/** 单元格 HTML → 纯文本（判数字只看文字：标签、KaTeX、高亮占位符都要剥掉） */
function cellText(html: string): string {
  return html
    .replace(/<[^>]*>/g, '')
    .replace(/%%(?:HL|BLKMATH)\d+%%/g, '')
    .replace(/&[a-zA-Z#0-9]+;/g, (m) => ENTITIES[m.toLowerCase()] ?? m)
    .trim();
}

/** 给开标签补一个类名（`<td align="right">` → `<td align="right" class="num">`） */
function withClass(cell: string, cls: string): string {
  const m = /^<(t[hd])([^>]*)>/.exec(cell);
  if (!m) return cell;
  const rest = cell.slice(m[0].length);
  const attrs = m[2];
  if (/\bclass\s*=\s*"/.test(attrs)) {
    return `<${m[1]}${attrs.replace(/\bclass\s*=\s*"([^"]*)"/, (_a, c: string) => `class="${c} ${cls}"`)}>${rest}`;
  }
  return `<${m[1]}${attrs} class="${cls}">${rest}`;
}

/** 把 `<table>` 加工成「论文表格」：数字列 `.num`、分组行 `.grp`，并包上滚动容器。 */
export function decorateTables(html: string): string {
  if (!html || !html.includes('<table')) return html;
  return html.replace(/<table[\s\S]*?<\/table>/g, (block) => {
    const wrap = (s: string) => `<div class="tbl-wrap">${s}</div>`;
    const rows = block.match(/<tr[\s\S]*?<\/tr>/g) ?? [];
    if (rows.length < 2) return wrap(block);
    const cols = rows.map((r) => r.match(/<t[hd][\s\S]*?<\/t[hd]>/g) ?? []);
    const width = Math.max(...cols.map((c) => c.length), 0);
    if (!width) return wrap(block);

    // ① 数字列：正文里 ≥75% 的单元格是数字（且至少 2 个）才算，免得把"2024 / 2025"当数字列
    const numeric: boolean[] = [];
    for (let c = 0; c < width; c += 1) {
      const vals = cols.slice(1).map((cells) => cellText(cells[c] ?? '')).filter(Boolean);
      const hits = vals.filter((v) => NUMERIC_CELL.test(v)).length;
      numeric.push(vals.length >= 2 && hits / vals.length >= 0.75);
    }
    // ② 分组列：第一列出现空单元格 → 这是"数据集列"，块与块之间要一条细线
    const firsts = cols.slice(1).map((cells) => cellText(cells[0] ?? ''));
    const grouping = firsts.some((v) => !v);

    let i = -1;
    const out = block.replace(/<tr[\s\S]*?<\/tr>/g, (row) => {
      i += 1;
      let c = -1;
      let rowHtml = row.replace(/<t[hd][\s\S]*?<\/t[hd]>/g, (cell) => {
        c += 1;
        return numeric[c] ? withClass(cell, 'num') : cell;
      });
      const k = i - 1;                       // `i` 0 是表头行；k 是正文行下标
      if (grouping && k > 0 && firsts[k] && !firsts[k - 1]) {
        rowHtml = rowHtml.replace(/^<tr/, '<tr class="grp"');
      }
      return rowHtml;
    });
    return wrap(out);
  });
}

/**
 * 行间公式（`$$…$$`）自己接管，**不交给 marked-katex-extension**。
 *
 * 为什么必须这样：该扩展的 block 规则是 `/^(\${1,2})\n((?:\\[^]|[^\\])+?)\n\1(?:\n|$)/`，
 * 要求 `$$` **单独占一行**（`$$`⏎内容⏎`$$`）；而模型写的是 `$$\begin{aligned}` 这种
 * "定界符与内容同行"的形式，行内规则又排除换行（`[^\\\n]`），于是整块公式掉进普通段落：
 * 前端看到的是原始 LaTeX，`_` 还会被当成强调渲染成 `<em>`（实测 p019-08 的多行 aligned）。
 *
 * 做法：交给 marked 之前把 `$$…$$`（可跨行、定界符可与内容同行）抽成占位符，
 * 解析完把 KaTeX 渲染结果替换回去（与高亮同一套占位符思路）；代码围栏内不动。
 */
function extractBlockMath(src: string): { text: string; math: string[] } {
  const math: string[] = [];
  let out = '';
  let i = 0;
  let inFence = false;
  while (i < src.length) {
    if (src.startsWith('```', i)) {
      inFence = !inFence;
      out += '```';
      i += 3;
      continue;
    }
    if (!inFence && src.startsWith('$$', i)) {
      const end = src.indexOf('$$', i + 2);
      if (end > i + 1) {
        const tex = src.slice(i + 2, end).trim();
        if (tex) {
          math.push(tex);
          out += `%%BLKMATH${math.length - 1}%%`;
          i = end + 2;
          continue;
        }
      }
    }
    out += src[i];
    i += 1;
  }
  return { text: out, math };
}

function renderBlockMath(html: string, math: string[]): string {
  if (!math.length) return html;
  return html.replace(/%%BLKMATH(\d+)%%/g, (_all, idx: string) => {
    const tex = math[Number(idx)];
    return tex === undefined ? '' : latexToHtml(tex, true);
  });
}

/** Markdown → HTML（带缓存；行间公式自己接管，行内公式由扩展接管）。 */
export function mdToHtml(text?: string | null, paperId?: string | null): string {
  const src0 = text === null || text === undefined ? '' : String(text);
  if (!src0) return '';
  const key = paperId ? `${paperId}\u0000${src0}` : src0;
  const hit = cache.get(key);
  if (hit !== undefined) return hit;
  const { text: src, math } = extractBlockMath(wrapBareMath(src0));
  let html: string;
  try {
    html = marked.parse(src) as string;
  } catch (err) {
    console.warn('[marked] 解析失败，降级为纯文本', err);
    html = `<p>${escapeHtml(src0)}</p>`;
  }
  html = renderBlockMath(html, math);
  html = rewriteAssetPaths(html, paperId);
  html = decorateTables(html);
  if (cache.size >= CACHE_LIMIT) cache.clear();
  cache.set(key, html);
  return html;
}

/** 短文本（标题、术语、注记）→ 行内 HTML。 */
export function inlineHtml(text?: string | null): string {
  const src = String(text ?? '').trim();
  if (!src) return '';
  try {
    return marked.parseInline(src) as string;
  } catch {
    return escapeHtml(src);
  }
}

/** 裸 LaTeX（公式卡的 latex_final）→ HTML。 */
export function latexToHtml(latex?: string | null, display = true): string {
  const src = String(latex ?? '').trim();
  if (!src) return '<span class="faint">（无 LaTeX）</span>';
  try {
    return katex.renderToString(src, { displayMode: display, throwOnError: false, errorColor: '#d8453f' });
  } catch (err) {
    console.warn('[katex] renderToString 失败', err);
    return `<code>${escapeHtml(src)}</code>`;
  }
}

/**
 * 把用户高亮注入到块文本里再渲染。
 *
 * 高亮的偏移是相对**块原文**的，而 Markdown 解析会改变字符结构，所以做法是：
 * 解析前把命中区间换成 `%%HL0%%` 占位符 → 解析后把占位符换成 `<mark>`。
 * （占位符用 `%%` 而不是控制字符：`%` 在 Markdown 里没有语义，且能安全穿过解析器。）
 */
export function htmlWithHighlights(text: string, highlights: Highlight[], paperId?: string | null): string {
  if (!text) return '';
  const list = (highlights ?? [])
    .filter((h) => h.start < h.end)
    .sort((a, b) => a.start - b.start);
  if (!list.length) return mdToHtml(text, paperId);

  const { html } = renderWithPlaceholders(text, list.map((h) => ({
    start: h.start, end: h.end, mark: `<mark class="hl ${h.color || 'yellow'}" data-hl="${h.id}" title="点击删除高亮">`,
  })), paperId);
  return html;
}

/**
 * 把若干字符区间标成 `<mark>` 再渲染 Markdown（**搜索命中**用）。
 *
 * 与 :func:`htmlWithHighlights` 同一套占位符机制，区别只在于 mark 的属性：
 * 搜索命中不是"用户高亮"，不能带 `hl` 类与"点击删除"的提示（会误导）。
 * 侧栏的搜索片段因此也能显示 `$公式$`、`**加粗**`，而不是原始 Markdown 记号。
 */
export function htmlWithMarks(text: string, ranges: Array<{ start: number; end: number }>,
                              paperId?: string | null, markClass = 'ss-mark'): string {
  if (!text) return '';
  const list = (ranges ?? []).filter((r) => r.start < r.end).sort((a, b) => a.start - b.start);
  if (!list.length) return mdToHtml(text, paperId);
  return renderWithPlaceholders(text, list.map((r) => ({
    start: r.start, end: r.end, mark: `<mark class="${markClass}">`,
  })), paperId).html;
}

/** 共用实现：区间 → 占位符 → Markdown → 占位符换回 `<mark>`。 */
function renderWithPlaceholders(
  text: string,
  ranges: Array<{ start: number; end: number; mark: string }>,
  paperId?: string | null,
): { html: string; tokens: Array<{ token: string; text: string }> } {
  let out = '';
  let cursor = 0;
  const items: Array<{ token: string; text: string; mark: string }> = [];
  for (const r of ranges) {
    const s = Math.max(0, Math.min(text.length, r.start));
    const e = Math.max(s, Math.min(text.length, r.end));
    if (s < cursor) continue;                    // 重叠区间只认先到的那个
    out += text.slice(cursor, s);
    const token = `%%HL${items.length}%%`;
    items.push({ token, text: text.slice(s, e), mark: r.mark });
    out += token;
    cursor = e;
  }
  out += text.slice(cursor);

  let html = mdToHtml(out, paperId);
  for (const it of items) {
    html = html.split(it.token).join(`${it.mark}${escapeHtml(it.text)}</mark>`);
  }
  return { html, tokens: items.map(({ token, text: t }) => ({ token, text: t })) };
}
