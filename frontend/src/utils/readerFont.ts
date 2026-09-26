/**
 * 正文字号档位（**阅读器工具栏**与两个解读整页共用同一份口径）。
 *
 * 为什么抽出来：字号是"这篇论文上次是怎么看的"的一部分，存在服务端进度里（`text_size`）；
 * 阅读器工具栏用它、整页解读也要用它 —— 两边各写一份 `{sm:13, md:15, lg:17}`，
 * 迟早会出现"阅读器显示大、整页显示中"这种对不上。
 *
 * ⚠️ 数值与阅读器工具栏的「小 / 中 / 大」一致（13 / 15 / 17）。改这里就是改两处的正文大小，
 * 顺带影响 `--reader-font-size`（正文/标题/图注/公式全部用 em 相对它缩放）。
 */
export const READER_TEXT_SIZES = { sm: 13, md: 15, lg: 17 } as const;

export type ReaderTextSize = keyof typeof READER_TEXT_SIZES;

/** `text_size` → px；拿不到 / 不认识的值回落到"小"（与阅读器默认一致）。 */
export function readerFontPx(size?: string | null): number {
  const key = String(size ?? '') as ReaderTextSize;
  return READER_TEXT_SIZES[key] ?? READER_TEXT_SIZES.sm;
}
