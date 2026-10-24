/**
 * 「渲染文字」里公式占几个字符 —— 与 `utils/domMarks` 的 `ATOM` 同一口径：**一个公式 = 1 个字符**。
 *
 * 为什么需要它：落库的 `selected_text` 可能是 LaTeX（`$…$` / `$$…$$`，见 ReaderView 的
 * `sliceWithLatex`），公式源码比排版后的字形长得多。拿 `text.length` 当"这一段在 DOM 里有多长"
 * 去算偏移量，区间就会一路顺延到后面几句上（存下去的高亮从"这一段"变成"这一段再加一截"）。
 */

/** 行间 `$$…$$` 优先，行内 `$…$` 不重复计入行间内部 —— 与后端 `math_spans` 同一口径。 */
const MATH_RE = /\$\$[\s\S]+?\$\$|(?<!\$)\$(?!\$)[^$\n]+?\$(?!\$)/g;

/** 这段文字在"渲染坐标系"里有多长（公式各算 1 个字符）。 */
export function renderedLength(text?: string | null): number {
  const raw = String(text ?? '');
  if (!raw) return 0;
  let width = raw.length;
  for (const m of raw.matchAll(MATH_RE)) width -= m[0].length - 1;
  return width;
}

/** 这段文字里有几处公式（0 = 没有公式，调用方可以走"原样"的老路径）。 */
export function formulaCount(text?: string | null): number {
  const raw = String(text ?? '');
  if (!raw) return 0;
  return [...raw.matchAll(MATH_RE)].length;
}