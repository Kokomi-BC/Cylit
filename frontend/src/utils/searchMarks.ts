/**
 * 搜索命中高亮：**用户搜索词 → 片段里的字符区间**。
 *
 * 为什么单独一个模块：知识库左栏的「Chunk 摘要」要把命中的词标出来（`上下文压缩` 这类
 * 中文词、`KV cache` 这类英文词都要命中），而 `composables/useMarkdown` 里的
 * `htmlWithMarks` 只接受**相对原文的字符区间**、自己不做匹配。匹配规则放这儿，渲染仍然复用
 * 阅读器侧栏那一套（区间 → 占位符 → Markdown/KaTeX → `<mark>`），避免两处渲染不一致。
 *
 * 三条守则：
 * - **不做模糊匹配**：只认字面子串（大小写不敏感），搜到什么就标什么，不猜近义词；
 * - **注入安全**：全部走 `indexOf` 找位置，**不构造正则** —— 用户输入里的 `.` `(` `[` 都是
 *   普通字符，不会变成通配符（正则字面量必须转义，漏一个就是"搜 `(` 报错"）；
 * - **不贪心**：中文词/短语整段匹配；英文与数字的连续块（`kv`、`cache`）才各自匹配，
 *   并按大小写敏感与否取最长的那一种写法，避免 "KV cache" 里只标到一半。
 */

import { htmlWithMarks } from '@/composables/useMarkdown';

const CJK = /[\u3400-\u9fff\uf900-\ufaff]/;
/** 连续的中文串 / 连续的西文数字串（互不混合，`上下文compression` 自然拆成两段） */
const RUN = /[\u3400-\u9fff\uf900-\ufaff]+|[A-Za-z0-9][A-Za-z0-9'’\-_.]*/g;

/** 单个拉丁字符不足以构成"命中"（`a` 会在每句话里标一片），最少两个字符。 */
const MIN_LATIN = 2;

/** 查询词 → 若干"要找的字面串"（已按长度从长到短排序，先标长的）。 */
function queryTokens(query: string): string[] {
  const out = new Set<string>();
  for (const run of String(query ?? '').match(RUN) ?? []) {
    if (CJK.test(run)) {
      if (run.length >= 2) out.add(run);          // 整词（`上下文压缩`）
      else if (run.length === 1) out.add(run);    // 单字也认（`熵` 这种单字术语）
    } else if (run.length >= MIN_LATIN) {
      out.add(run);
    }
  }
  return [...out].sort((a, b) => b.length - a.length);
}

interface Range { start: number; end: number; }

/** 一个 token 在文本里的**全部**命中位置（`.toLowerCase()` 只用于比对，偏移量仍取原文下标）。 */
function literalRanges(text: string, token: string): Range[] {
  const lowText = text.toLowerCase();
  const out: Range[] = [];
  const variants = [...new Set([token, token.toLowerCase()])];
  for (const variant of variants) {
    const needle = variant.toLowerCase();
    if (!needle) continue;
    let from = 0;
    for (;;) {
      const at = lowText.indexOf(needle, from);
      if (at < 0) break;
      out.push({ start: at, end: at + needle.length });
      from = at + needle.length;
    }
  }
  return out;
}

/**
 * 把 `text` 里命中 `query` 的片段渲染成带 `<mark class="kb-mark">` 的 HTML
 * （Markdown / KaTeX 照常渲染，见 `useMarkdown.htmlWithMarks`）。
 *
 * `paperId` 用于把片段里的相对资源路径（`../figures/x.png`）改写成后端静态 URL。
 */
export function searchMarksHtml(text: string, query: string, paperId?: string | null): string {
  const src = String(text ?? '');
  const tokens = queryTokens(query);
  if (!src) return '';
  if (!tokens.length) return htmlWithMarks(src, [], paperId);

  const all: Range[] = [];
  for (const token of tokens) all.push(...literalRanges(src, token));
  if (!all.length) return htmlWithMarks(src, [], paperId);

  // 合并重叠区间（长 token 先入，短的落在里面就丢掉），再按位置排序
  all.sort((a, b) => (a.start - b.start) || (b.end - a.end));
  const merged: Range[] = [];
  for (const r of all) {
    const last = merged[merged.length - 1];
    if (last && r.start < last.end) {
      if (r.end > last.end) last.end = r.end;
      continue;
    }
    merged.push({ ...r });
  }
  return htmlWithMarks(src, merged, paperId, 'kb-mark');
}
