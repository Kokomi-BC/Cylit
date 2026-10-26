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
  /* ⚠️ 必须是 `htmlAndMathml`（KaTeX 默认）：`output: 'html'` 会把 MathML 一起省掉，
     而**公式的 LaTeX 源码就藏在 MathML 的 `<annotation encoding="application/x-tex">` 里** ——
     没有它，"完全选中公式时复制 LaTeX"（用户口径 2026-10-08）就无从谈起（实测抠出来是空串）。
     KaTeX 的 CSS 把 `.katex-mathml` 收成 1×1 隐藏，视觉与布局都不受影响；
     偏移量那套（`utils/domMarks`）本来就跳过 `.katex-mathml`，也不会被它的重复文字干扰。 */
  output: 'htmlAndMathml',
  errorColor: '#d8453f',
}));
// 论文正文里出现 `<` 时不能被当成 HTML 注入：HTML 令牌一律转义输出
marked.use({
  renderer: {
    html: (token: any) => escapeHtml(typeof token === 'string' ? token : (token?.text ?? '')),
  },
});

/**
 * **引用标号可点**（用户口径 2026-10-07 第三条）：正文里的 `[2, 4, 10]` 渲染成一个按钮，
 * 点它由 `ReaderView` 打开侧栏「文献」页签并**只显示被提到的**那几条。
 *
 * 为什么做成 marked 的**行内扩展**、而不是拿正则去改渲染完的 HTML：
 * 扩展只在"当前词法位置"被调用（`inlineTokens` 里扩展在最前面），所以
 * 代码块/行内代码里的方括号轮不到它（那些先被 `codespan`/`fences` 吃掉），
 * 公式里的 `[0,1]` 也一样（`$…$` 由 katex 扩展先接走，它比本扩展先注册 → 先在 `$` 处命中）。
 * 拿正则扫 HTML 则会把 katex 的 `<annotation>`（里面就是 LaTeX 原文）一起改坏。
 *
 * 渲染出来的文字仍然是 `[2, 4, 10]`：**一个字符都没多、也没少**，
 * 所以"高亮偏移量"那套坐标系（`utils/domMarks` 的渲染文本）不受影响。
 */
const CITE_RE = /^\[(\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\]/;

marked.use({
  extensions: [{
    name: 'cite',
    level: 'inline' as const,
    start(src: string) { return src.indexOf('['); },
    tokenizer(src: string) {
      const m = CITE_RE.exec(src);
      if (!m) return undefined;
      /* `[2](url)` 是**真的链接**（Markdown 语法），别抢：让内置的 link 规则去处理 */
      const next = src.charAt(m[0].length);
      if (next === '(' || next === '[') return undefined;
      return { type: 'cite', raw: m[0], refs: m[1] };
    },
    renderer(token: any) {
      const nums = String(token.refs).split(/[,;]/).map((s) => s.trim()).filter(Boolean);
      const label = nums.join(', ');
      /* 样式用**对话页那个跳转标号**的同一枚类（`.cite-inline`，定义在 `styles/base.css`）：
         用户口径 2026-10-07 —— 正文里的引用标号要跟对话页长得一样。
         ⚠️ **不要 `title`**（用户口径 2026-10-08）：悬浮标号时浏览器会额外弹一条原生提示
         （"这篇论文引用的第 x 条…"），而悬浮本身已经会弹出参考文献浮层 —— 两个提示叠在一起。
         无障碍名字改用 `aria-label`：它不会画出那条 tooltip。 */
      return `<button type="button" class="cite-inline" data-refs="${nums.join(',')}"`
        + ` aria-label="参考文献 ${label}（悬浮或点击查看）">[${label}]</button>`;
    },
  }],
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
 * 把 LaTeX 的圆括号 / 方括号定界符改写成 $ / $$。
 *
 * 症状（用户口径：标题的 markdown 有时候没有渲染）：图注里写成 \(x_t\) 或 \[ \min_q ... \]，
 * 而 marked-katex-extension 只认 $ 定界符 —— 结果是标题里原样吐出反斜杠和括号。
 * 围栏代码块里跳过：那里的反斜杠是代码，不是公式。
 */
function normalizeTexDelimiters(src: string): string {
  if (!src.includes('\\(') && !src.includes('\\[')) return src;
  /* 按围栏切成「代码段 / 非代码段」交替，只处理偶数位（非代码段） */
  return src.split(/(^[ \t]*(?:```|~~~)[^\n]*\n[\s\S]*?^[ \t]*(?:```|~~~)[ \t]*$)/m)
    .map((seg, i) => (i % 2 === 1 ? seg : seg
      .replace(/\\\[([\s\S]*?)\\\]/g, (_m, tex) => '$$' + tex + '$$')
      .replace(/\\\(([\s\S]*?)\\\)/g, (_m, tex) => '$' + tex + '$')))
    .join('');
}

/** 公式区间（$$…$$、$…$）或一个行内文本命令（\textbf{…} 等） */
const MATH_OR_TEXT_CMD = /\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\\(textbf|textit|textsc|emph)\{([^{}]*)\}/g;

/**
 * 公式**之外**的排版命令换成 Markdown：\textbf{Cylit} 一直是原样吐出来的
 * （用户口径：标题的 markdown 有时候没有渲染 —— 图注常常这么写）。
 * 公式里的 \textbf 不动，那是 KaTeX 该管的。
 */
function unwrapTextCommands(src: string): string {
  if (!src.includes('\\text') && !src.includes('\\emph')) return src;
  return src.replace(MATH_OR_TEXT_CMD, (m: string, cmd: string, body: string) => {
    if (!cmd) return m;                                   // 公式区间原样放行
    return cmd === 'emph' || cmd === 'textit' ? '*' + body + '*' : '**' + body + '**';
  });
}

/** 明确的数学命令：裸写在正文里时据此补 $…$（排版命令不在此列，它们已转成 Markdown） */
const BARE_MATH_CMD = /\\(?:mathbb|mathrm|mathbf|mathcal|mathit|operatorname|hat|bar|tilde|vec|frac|sqrt|sum|prod|int|partial|nabla|infty|alpha|beta|gamma|delta|lambda|mu|sigma|theta|omega|pi|le|leq|ge|geq|neq|approx|equiv|times|cdot|pm|to)\b/;

/**
 * 把**裸写的 LaTeX**补上公式定界符。
 *
 * 为什么会裸写：模型偶尔忘了包 `$…$`，直接写 `Q_mse^{-1}`、`S^{d-1}`、`\text{(MSE)} \quad \mathbb{E}`。
 * 渲染出来就是原文带花括号/反斜杠的样子（用户报过 "markdown 问题"），而且 `_` 还可能被当成强调。
 * 提示词已经加了硬要求（见 `page_translate.SYSTEM_PROMPT`），这里再挡一道，**存量产物不用重译也能好看**。
 *
 * 守则：宁可少补，也不要误把普通文字塞进公式 —— 只在两种情况下动手：
 *   ① 整行就是一段公式（含高置信 LaTeX 命令且**没有中文**）→ 整行包 `$$…$$`；
 *   ② 某个“词”里出现 `^{` / `_{`，或者带一个明确的数学命令（`\mathbb{E}[x]`）→ 只把这个词包进 `$…$`。
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
      /* 带 ^{ / _{ 的「词」，或者裸写的数学命令（\mathbb{E}[x] 这种）才补 —— 别的词一律不动 */
      if (!/[_^]\{/.test(tok) && !BARE_MATH_CMD.test(tok)) return tok;
      if (tok.includes('$') || tok.includes('`') || tok.startsWith('http')) return tok;
      if (tok.length > 60 || CJK.test(tok)) return tok;
      return `$${tok}$`;
    });
  }).join('\n');
}

/**
 * **软换行归一**：把 Markdown 里的"排版换行"还原成真正的排版。
 *
 * 症状（用户口径："Markdown 阅读排版里的异常换行"）：PDF 与模型产物里的段落经常是**硬折行**的
 * （每 ~40 字一个 `\n`），Markdown 里单个 `\n` 是**软换行**（渲染成一个空格）——
 * 于是中文句子中间被插进一个空格：`……用了两阶段量化器\n消除内积偏差` → "……量化器 消除内积偏差"。
 * 英文那边还多一种：PDF 断词留下的 `quantiza-\ntion`（连字符 + 换行）会被渲染成 `quantiza- tion`。
 *
 * 这不是"藏起来"能解决的问题（`white-space: nowrap` / 隐藏溢出都会把长公式、URL 一起毁掉），
 * 得按**排版规则**在渲染前把段落接回去：
 *   - 两侧任一在中文/全角字符集里 → **不插空格**（中文没有词间空格，硬插一个是错的）；
 *   - 两侧都是西文 → **一个空格**；
 *   - 行尾 `-` 且下一行以小写字母开头 → 去掉连字符（PDF 断词还原）。
 *
 * 哪些换行**不能动**（动一下就会把文档结构改坏）：
 *   - 行尾两个以上空格 / 反斜杠 = Markdown **硬换行**（作者故意要换行）；
 *   - 下一行是标题 / 列表项 / 引用 / 表格行 / 围栏 / 行间公式定界 —— 那是**新块**的开始；
 *   - 代码围栏内、`$$…$$` 内（公式换行是 LaTeX 的一部分，不能接）。
 */
/** 汉字 + 中文标点 + 全角形式：判断"要不要补空格"只看这一类比 */
const CJK_OR_FULL = /[\u3400-\u9fff\u3000-\u303f\uff01-\uff60\uffe0-\uffe6]/;
/** 新块的开头：标题 / 无序列表 / 有序列表 / 引用 / 表格行 / 围栏 / 行间公式 / 分隔线 */
const BLOCK_START = /^\s{0,3}(?:#{1,6}\s|[-*+]\s|\d{1,9}[.)]\s|>|\||```|~~~|\$\$|(?:[-*_]\s*){3,}$)/;
/** 行尾两个以上空格或反斜杠 = Markdown 硬换行 */
const HARD_BREAK = /(?:\s{2,}|\\)$/;

export function normalizeSoftBreaks(src: string): string {
  if (!src || !src.includes('\n')) return src;
  const lines = src.split('\n');
  const out: string[] = [];
  let inFence = false;
  let inMath = false;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^\s{0,3}(?:```|~~~)/.test(line)) { inFence = !inFence; out.push(line); continue; }
    if (inFence) { out.push(line); continue; }
    const dollars = (line.match(/\$\$/g) || []).length;
    if (inMath) { if (dollars % 2 === 1) inMath = false; out.push(line); continue; }
    if (dollars % 2 === 1) { inMath = true; out.push(line); continue; }

    const next = lines[i + 1];
    const joinable = next !== undefined
      && line.trim() !== '' && next.trim() !== ''
      && !BLOCK_START.test(line) && !HARD_BREAK.test(line)
      && !BLOCK_START.test(next);
    if (!joinable) { out.push(line); continue; }

    const left = line.replace(/\s+$/, '');
    const right = next.replace(/^\s+/, '');
    if (looksHyphenated(left, right)) out.push(left.slice(0, -1) + right);
    else if (CJK_OR_FULL.test(left.slice(-1)) || CJK_OR_FULL.test(right[0])) out.push(left + right);
    else out.push(`${left} ${right}`);
    i += 1;                                   // 这一行已经并进上一行了
  }
  return out.join('\n');
}

/** PDF 断词：`…quantiza-` + `tion…`（下一行以小写字母开头才算） */
function looksHyphenated(left: string, right: string): boolean {
  return /[A-Za-z]-$/.test(left) && /^[a-z]/.test(right);
}

/**
 * 中文里 `**` 紧贴**标点**时，CommonMark 的 flanking 规则会判定"这对定界符不成立"，
 * 字面漏出 `**`（用户 2026-10-03 报的两次："一句话总结没渲染"、
 * `**"压缩再解压之后，向量丢了多大劲"的平均值**`）。
 *
 * 原因不是配置，是 flanking 规则本身。`**` 要成立，左侧那个必须 left-flanking、右侧那个必须
 * right-flanking，而两者都要求"紧跟标点"时**外侧得是空白或标点**：
 *
 * - 收尾失败：`**一句话：……再投影回去。**论文的说法是……`
 *   收尾的 `**` 左边是标点（`。`）、右边紧挨文字（`论`）→ 不成立；
 * - 开场失败：`是**"压缩再解压"的平均值**…`
 *   开场的 `**` 紧接着引号（标点）、左边又贴着文字（`是`）→ 不成立。
 *
 * **中文没有词间空格，所以这两种形状在中文里到处都是**（`是**"…"**`、`用**$d_k$**归一化`、
 * `见**（图 3）**`……）。GitHub / cmark 同样如此（实测 marked 18 一致），只能我们自己接管
 * 这一对。
 *
 * 做法：把**这一对**（两个定界符一起）抽成占位符，解析完换成 `<strong>`；只在这种
 * "必然失败"的形状上动手，正常闭合的加粗一个字都不碰（`**重点**内容`、`他说。**重点**`、
 * `2**3**4`、`是 **"重点"** 后面` 都照旧）。
 * 代码围栏与行内代码里的 `**` 不动（那是代码，不是强调）。
 */
const BOLD_PAIR = /\*\*([^*\n]{1,400}?)\*\*/g;
/**
 * CommonMark 眼里的"标点"：**ASCII 标点全集**（`!` 到 `~` 里的四段）+ 中文/全角标点。
 * 两侧规则共用这一份 —— 上一版只列了中文标点，于是 `用**$d_k$**归一化` 这种
 * "`$` 收尾"的形状漏在外面（`$` 按 CommonMark 的定义**是** ASCII 标点）。
 */
const PUNCT = /[\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e。，、；：？！…—～·（）〈〉《》「」『』【】〔〕“”‘’]/;

function fixCjkBoldEdges(src: string): { text: string; strong: string[] } {
  const strong: string[] = [];
  if (!src.includes('**')) return { text: src, strong };
  let inFence = false;
  const text = src.split('\n').map((line) => {
    if (line.trimStart().startsWith('```')) { inFence = !inFence; return line; }
    if (inFence || !line.includes('**')) return line;
    return line.replace(BOLD_PAIR, (whole, body: string, off: number) => {
      if ((line.slice(0, off).match(/`/g) || []).length % 2 === 1) return whole;   // 落在行内代码里
      const prev = off > 0 ? line[off - 1] : '';
      const after = line[off + whole.length] ?? '';
      // 开场失配：紧跟标点、左侧又贴着文字（`是**"…"**`）
      const headBad = PUNCT.test(body[0])
        && prev !== '' && !/\s/.test(prev) && !PUNCT.test(prev);
      // 收尾失配：左侧是标点、右侧又紧挨文字（`**……。**论文`）
      const tailBad = PUNCT.test(body.slice(-1))
        && after !== '' && !/\s/.test(after) && !PUNCT.test(after);
      if (!headBad && !tailBad) return whole;                       // 这种 marked 自己认
      strong.push(body);
      return `%%STRONG${strong.length - 1}%%`;
    });
  }).join('\n');
  return { text, strong };
}

function renderStrong(html: string, strong: string[]): string {
  if (!strong.length) return html;
  return html.replace(/%%STRONG(\d+)%%/g, (_all, idx: string) => {
    const body = strong[Number(idx)];
    return body === undefined ? '' : `<strong>${marked.parseInline(body)}</strong>`;
  });
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

/** Markdown → HTML（带缓存；软换行归一 → 行间公式自己接管，行内公式由扩展接管）。
 *
 * `opts.breaks` —— **用户自己写的笔记**要传 `true`（用户口径 2026-10-10：
 * "收起状态下不支持 markdown 和换行"）：
 *   `normalizeSoftBreaks` 是给**论文抽取文本**用的（PDF 拆出来的硬换行要接回一行，
 *   中文接回不插空格）；而笔记里的回车是作者**故意敲的排版**，接回去就成了
 *   "第一行第二行"。传 `true` 时跳过归一，并让 marked 把单个换行渲染成 `<br>`。
 */
export function mdToHtml(
  text?: string | null,
  paperId?: string | null,
  opts?: { breaks?: boolean },
): string {
  const src0 = text === null || text === undefined ? '' : String(text);
  if (!src0) return '';
  const breaks = opts?.breaks === true;
  // 缓存键要带上 `breaks`：同一段文字在两条路径下的 HTML 并不一样
  const key = `${breaks ? '\u0001' : ''}${paperId ? `${paperId}\u0000` : ''}${src0}`;
  const hit = cache.get(key);
  if (hit !== undefined) return hit;
  /* 三步前置，顺序不能换：先把 \(…\) / \[…\] 归一到 $ 定界符，再转排版命令，
     最后才补裸公式 —— 反过来的话后一步看不见前面刚造出来的 $…$。 */
  const prepared = wrapBareMath(
    unwrapTextCommands(normalizeTexDelimiters(breaks ? src0 : normalizeSoftBreaks(src0))),
  );
  const bold = fixCjkBoldEdges(prepared);
  const { text: src, math } = extractBlockMath(bold.text);
  let html: string;
  try {
    html = marked.parse(src, { breaks }) as string;
  } catch (err) {
    console.warn('[marked] 解析失败，降级为纯文本', err);
    html = `<p>${escapeHtml(src0)}</p>`;
  }
  html = renderBlockMath(html, math);
  html = renderStrong(html, bold.strong);
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
  /* 短标题（图注 / 术语）同样要吃 \(…\) 与排版命令，否则标题里会露出反斜杠 */
  const bold = fixCjkBoldEdges(unwrapTextCommands(normalizeTexDelimiters(src)));
  try {
    return renderStrong(marked.parseInline(bold.text) as string, bold.strong);
  } catch {
    return escapeHtml(src);
  }
}

/**
 * 去掉公式里的**编号**（`\tag{1}`、`\qquad(1)`、行尾裸 `(1)`）。
 *
 * 为什么行内公式必须去掉：KaTeX 把 `\tag` 渲染成 `.tag`，绝对定位在 `.katex-html` 的
 * `right: 0`；行内盒子又被收成"内容宽"，于是那个 `(1)` 直接压在公式尾巴上
 * （用户口径 2026-12-05 的截图：`\left(\frac{QK^T}{\sqrt{d_k}}\right)(V)` 中间夹了个 (1)）。
 * 整块公式（display）保留编号，那里 right:0 落在公式右侧的留白上，是对的。
 */
export function stripEqNumber(tex: string): string {
  return String(tex ?? '')
    .replace(/\s*\\tag\*?\{[^{}]*\}/g, '')
    .replace(/\s*\\(?:qquad|quad)\s*\(\s*\d{1,3}[a-z]?\s*\)\s*$/i, '')
    .replace(/\s*\\(?:hfill|hspace\{[^{}]*\})\s*\(\s*\d{1,3}[a-z]?\s*\)\s*$/i, '')
    .replace(/\s{2,}\(\s*\d{1,3}[a-z]?\s*\)\s*$/i, '')
    .trim();
}

/** 裸 LaTeX（公式卡的 latex_final）→ HTML。 */
export function latexToHtml(latex?: string | null, display = true): string {
  const raw = String(latex ?? '').trim();
  if (!raw) return '<span class="faint">（无 LaTeX）</span>';
  /* 行内公式也**保留编号**（用户口径 2026-12-05：编号要留着，用横向滑动解决遮挡）——
     留位置与横向滚动的 CSS 在 base.css 的 .katex:not(.katex-display) 那一段。 */
  const src = raw;
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
    /* 不加 `title`（用户口径 2026-10-08："移除浏览器悬浮 toast 内容"）：高亮是"悬浮就出笔记浮层"
       的那类元素，再叠一条浏览器原生提示只是噪音（编辑/删除入口在划词菜单与浮层里）。 */
    start: h.start, end: h.end,
    /* 自定义色（`#rrggbb`）class 认不出来，得拼**内联变量**（与 `utils/domMarks` / `NotesView`
       同一口径：`--hl-bg` = 40% 底色，`--hl-ink` = 实色线）；命名色照旧走 class。
       不写内联 `background` —— 那样「高亮效果」切到下划线时，任意色的标注压不过内联样式。 */
    mark: /^#[0-9a-fA-F]{6}$/.test(String(h.color || ''))
      ? `<mark class="hl" style="--hl-bg:${h.color}66;--hl-ink:${h.color}" data-hl="${h.id}">`
      : `<mark class="hl ${h.color || 'yellow'}" data-hl="${h.id}">`,
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

/**
 * 笔记正文里的**引用标号** → 可点的 chip（短码 `[A023]` 与数字 `[3]`）。
 *
 * 为什么不能照正文那条路给 marked 再加一条行内扩展（`[2, 4, 10]` 就是这么做的）：短码要落成
 * "哪一行 / 哪一页 / 哪一枚公式"，那是**引用表**才有的信息（解读的 `refs_resolved`、
 * 笔记自己末尾那行 `<!-- refs:… -->`）；而标号显示的是**序号**，得按"这一篇里第一次出现的
 * 顺序"编，只有拿到整篇 HTML 才编得出来。Markdown 解析那条链路两样都拿不到。
 *
 * `[A023]` 在 Markdown 里本来就不是语法（没有同名定义时它就是普通文字），所以这层只做
 * "认出来 + 挂数据集"：**渲染出来的文字既不增加也不丢字符**，只有序号是替进去的。
 *
 * 两种编号走两条路，因为它们到达这一层时形状已经不同：
 *   - **数字标号** `[3]` 早被 `cite` 扩展画成了按钮（`data-refs`），所以是**升级**那枚按钮；
 *   - **短码** `[A023]` 还是纯文字，所以是**替换**文字。
 * 两条路换出来的东西都先转成占位符再拼回去：否则后一趟正则会把前一趟生成的
 * `<button>[1]</button>` 里的方括号再包一层按钮（实测就是 button 里套 button）。
 *
 * 认不出来的编号：**短码**照样画（点了不跳，抹掉会让读者以为漏了内容）；**数字**原样留着
 * （那一枚是"参考文献编号"还是 chunk 引用分不清时，不动比乱跳好 —— 与 `utils/citeRefs` 同一口径）。
 */
export interface CiteMarkTarget {
  /** 原始编号（`A023` / `3`）。**不是**显示出来的序号：保存笔记时按它把跳转信息接回 refs 表 */
  ref: string;
  /** 正文行 id（优先落它：公式 chunk 的块 id 常常是 `row-p006-03` 这类伪块 id，翻不成行） */
  row_id?: string | null;
  block_id?: string | null;
  page?: number | null;
  /** 公式条目的 LaTeX：同一行里几枚公式靠它才分得开 */
  latex?: string | null;
  /** 这一条覆盖的整段行 id（逗号分隔）：速览卡据此把标题和它下面的正文一起画出来 */
  rows?: string | null;
  /** 悬浮提示里的短名（"段落" / "公式"），空就不写 */
  label?: string | null;
}

/** 短码 `[A023]`：Markdown 里它就是普通文字（没有同名定义时既不链接也不成脚注） */
const CITE_CODE = /\[([A-Z]\d{2,4})\]/g;
/** `mdToHtml` 的 `cite` 扩展把 `[3]` 画成的按钮（`[2, 4, 10]` 那套参考文献编号也走它） */
const CITE_REFS_BTN = /<button\b[^>]*\bdata-refs="([^"]*)"[^>]*>[\s\S]*?<\/button>/gi;
/** 占位符：把已经换好的 chip 藏起来，免得后面的正则把它里面的 `[1]` 再包一层 */
const CITE_HOLD = /\u0001CM(\d+)\u0001/g;

export function decorateCiteMarks(
  html: string,
  lookup: (ref: string) => CiteMarkTarget | null,
): string {
  if (!html || !html.includes('[')) return html;
  const chips: string[] = [];
  const hold = (chip: string) => {
    chips.push(chip);
    return `\u0001CM${chips.length - 1}\u0001`;
  };
  /* ① **已经在按钮里的数字标号**（`[3]` 被 `cite` 扩展画成按钮了）就地升级：
     认得出来就换成带落点的那一枚，认不出来原样留着（与今天的行为一致）。
     `[2, 4, 10]` 那种是**参考文献编号**，不是 chunk 引用 —— 它不是一条，不动。
     ⚠️ 不能事后在 `[3]` 的位置再套一枚 chip：那会得到一个 button 里套 button。 */
  let out = html.replace(CITE_REFS_BTN, (whole, refs: string) => {
    const list = String(refs).split(',').map((s) => s.trim()).filter(Boolean);
    if (list.length !== 1) return whole;
    const hit = lookup(list[0]);
    return hit ? hold(citeButton(hit, list[0])) : whole;
  });
  /* ② 短码：序号按**这一篇里第一次出现**的顺序编（与「论文解读」的标号同一口径） */
  let seq = 0;
  out = out.split(/(<code[\s\S]*?<\/code>|<pre[\s\S]*?<\/pre>|<a\b[^>]*>[\s\S]*?<\/a>|<button\b[\s\S]*?<\/button>)/i)
    .map((part, index) => {
      if (index % 2 === 1) return part;      // 代码块 / 真链接 / 保留下来的按钮都不是引用
      return part.replace(CITE_CODE, (whole, code: string) => {
        const hit = lookup(code);
        /* 短码认不出来也照样画一枚（点了不跳）：它本来就是正文的一部分，
           抹掉会让读者以为漏了内容 —— 与「论文解读」同一口径。 */
        return hold(citeButton(hit ?? { ref: code }, String((seq += 1))));
      });
    })
    .join('');
  return out.replace(CITE_HOLD, (_m, i: string) => chips[Number(i)] ?? '');
}

/** 一枚标号：类名与「论文解读」/对话页那枚 `.cite-inline` 共用（观感只由 base.css 那一条决定） */
function citeButton(t: CiteMarkTarget, text: string): string {
  const anchor = String(t.row_id || t.block_id || '');
  /* `aria-label` 而不是 `title`：title 会再弹一条浏览器灰气泡，与悬浮出来的速览卡叠在一起
     （正文引用标号 2026-10-08 就已经踩过这一条）。 */
  const tip = [t.label || '', t.page ? `第 ${t.page} 页` : '', anchor ? '跳到原文' : '这一条引用已不在清单里']
    .filter(Boolean).join(' · ');
  return `<button type="button" class="cite-inline cite-mark" aria-label="${escapeHtml(tip)}"`
    + ` data-ref="${escapeHtml(t.ref)}"`
    + ` data-row="${escapeHtml(String(t.row_id || ''))}"`
    + ` data-block="${escapeHtml(String(t.block_id || ''))}"`
    + ` data-page="${t.page ?? ''}"`
    + ` data-latex="${escapeHtml(String(t.latex || ''))}"`
    + ` data-rows="${escapeHtml(String(t.rows || ''))}">[${text}]</button>`;
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
