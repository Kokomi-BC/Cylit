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
  const { text: src, math } = extractBlockMath(src0);
  let html: string;
  try {
    html = marked.parse(src) as string;
  } catch (err) {
    console.warn('[marked] 解析失败，降级为纯文本', err);
    html = `<p>${escapeHtml(src0)}</p>`;
  }
  html = renderBlockMath(html, math);
  html = rewriteAssetPaths(html, paperId);
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

  let out = '';
  let cursor = 0;
  const tokens: Array<{ token: string; text: string; color: string; id: string }> = [];
  for (const h of list) {
    const s = Math.max(0, Math.min(text.length, h.start));
    const e = Math.max(s, Math.min(text.length, h.end));
    if (s < cursor) continue;
    out += text.slice(cursor, s);
    const token = `%%HL${tokens.length}%%`;
    tokens.push({ token, text: text.slice(s, e), color: h.color || 'yellow', id: h.id });
    out += token;
    cursor = e;
  }
  out += text.slice(cursor);

  let html = mdToHtml(out, paperId);
  for (const t of tokens) {
    html = html.split(t.token).join(
      `<mark class="hl ${t.color}" data-hl="${t.id}" title="点击删除高亮">${escapeHtml(t.text)}</mark>`,
    );
  }
  return html;
}
