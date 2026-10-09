/**
 * 颜色小工具：只做"从几个基础色推出整套令牌"需要的那几件事。
 *
 * 为什么自己算而不是在 CSS 里 `color-mix`：自定义主题要能**在运行时**生成一份
 * `[data-theme="<id>"]{…}`（见 `theme.ts`），而 `color-mix` 的结果只能活在 CSS 里，
 * 没法回过头去喂给下一个变量（`--accent-text` 要基于 `--accent` 再算一次）。
 * 这里统一在 sRGB 空间里混，结果与 CSS 的 `color-mix(in srgb, …)` 基本一致。
 */

/** `#abc` / `#aabbcc` / `rgba(...)` / `rgb(...)` → `[r,g,b]`（认不出来时给中性灰）。 */
export function toRgb(value: string): [number, number, number] {
  const raw = String(value ?? '').trim();
  const hex = raw.replace(/^#/, '');
  if (/^[0-9a-f]{3}$/i.test(hex)) {
    return [parseInt(hex[0] + hex[0], 16), parseInt(hex[1] + hex[1], 16), parseInt(hex[2] + hex[2], 16)];
  }
  if (/^[0-9a-f]{6}$/i.test(hex)) {
    return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)];
  }
  const m = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/i.exec(raw);
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3])];
  return [128, 128, 128];
}

export function toHex(value: string): string {
  const [r, g, b] = toRgb(value).map((n) => Math.max(0, Math.min(255, Math.round(n))));
  return `#${[r, g, b].map((n) => n.toString(16).padStart(2, '0')).join('')}`;
}

/** 合法的 6 位十六进制（颜色选择器 / 手输都过这一关）。 */
export function isHex(value: unknown): boolean {
  return typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value.trim());
}

/** `t=0` 取 `a`，`t=1` 取 `b`。 */
export function mix(a: string, b: string, t: number): string {
  const k = Math.max(0, Math.min(1, t));
  const [r1, g1, b1] = toRgb(a);
  const [r2, g2, b2] = toRgb(b);
  const ch = (x: number, y: number) => Math.round(x + (y - x) * k);
  return toHex(`#${[ch(r1, r2), ch(g1, g2), ch(b1, b2)]
    .map((n) => n.toString(16).padStart(2, '0')).join('')}`);
}

export const lighten = (c: string, t: number) => mix(c, '#ffffff', t);
export const darken = (c: string, t: number) => mix(c, '#000000', t);

/** 带透明度的颜色字符串（供 `--bg-hover` / `--border` 这类令牌用）。 */
export function alpha(c: string, a: number): string {
  const [r, g, b] = toRgb(c).map((n) => Math.round(n));
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

/** 相对亮度（WCAG）。用来决定"压在这块颜色上的字该用白还是黑"。 */
export function luminance(c: string): number {
  const [r, g, b] = toRgb(c).map((n) => {
    const s = n / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** 底色 -> 可读的前景色（主按钮上的文字）。 */
export function readableOn(bg: string, dark = '#0b1315', light = '#ffffff'): string {
  return luminance(bg) > 0.55 ? dark : light;
}
