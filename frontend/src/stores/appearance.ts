/**
 * 显示偏好：**字体族 + 细腻动画 + 高亮画法**（本机偏好，只写 localStorage）。
 *
 * 三项都落成 `<html>` 上的 data 属性，再由样式层里同名选择器接管：
 * · `data-typo` 只换 `--sans` / `--display` 两个字面栈（`--mono` / `--serif` 不动 ——
 *   代码块与论文原文的字体是内容语义，不是阅读偏好）；
 * · `data-motion="off"` 把三个动效时长令牌归零。不逐处关 transition：新增组件总会漏一个，
 *   而归零令牌是一次性的、覆盖全站的；
 * · `data-hl-style="marker" | "underline"` 决定**已保存的文本标注**怎么画（`base.css` 的
 *   `mark.hl` 一段）：荧光笔 = 半透明底色，下划线 = 只画一条线。标注数据一个字不动，
 *   切的是渲染；`::selection`（浏览器里的临时选区）不归它管。
 *
 * ⚠️ 开关是**正向**的（「细腻动画」默认开），不是"减少动画"：用户来设置页是想让界面更好用，
 * 不该先看到一条"关掉 XX"的消极选项；关掉它只是给低配机器与晕动症留的退路。
 */
import { reactive } from 'vue';

export type Typography = 'system' | 'yahei' | 'serif';

/** 三档都只用系统里**真的有**的字面（Windows / macOS 各有一个命中，缺了自动落下一档）。 */
export const TYPOGRAPHY_OPTIONS: ReadonlyArray<{ value: Typography; label: string }> = [
  { value: 'system', label: '系统默认' },
  { value: 'yahei', label: '微软雅黑' },
  { value: 'serif', label: '宋体衬线' },
];

/** 已保存标注的画法：`marker` = 荧光笔（半透明底色），`underline` = 下划线。 */
export type HighlightStyle = 'marker' | 'underline';

/** 设置页那两张并排的小卡就是照这份表渲染的（顺序 = 界面顺序）。 */
export const HIGHLIGHT_OPTIONS: ReadonlyArray<{ value: HighlightStyle; label: string; desc: string }> = [
  { value: 'marker', label: '荧光笔', desc: '半透明底色盖住文字，跨行也贴着行盒走' },
  { value: 'underline', label: '下划线', desc: '只在文字下方画一条线，不铺底色' },
];

export const APPEARANCE_STORAGE_KEY = 'cylit:appearance';

export const appearance = reactive<{
  typography: Typography;
  motion: boolean;
  highlight: HighlightStyle;
  /** 本机字体族（用户在设置页从自己电脑里选的那一款）。空 = 用 typography 那三套预设栈。 */
  fontFamily: string;
}>({
  typography: 'system',
  motion: true,
  /* 旧配置里没有这个字段：默认荧光笔（用户口径 2026-12） */
  highlight: 'marker',
  fontFamily: '',
});

function isTypography(value: unknown): value is Typography {
  return value === 'system' || value === 'yahei' || value === 'serif';
}

function isHighlightStyle(value: unknown): value is HighlightStyle {
  return value === 'marker' || value === 'underline';
}

/** 系统级「减少动态效果」是更强的意图：没存过偏好时按它给初值。 */
function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;      /* 老浏览器没有 matchMedia：当"不需要减少" */
  }
}

function load(): void {
  try {
    const raw = JSON.parse(localStorage.getItem(APPEARANCE_STORAGE_KEY) || 'null');
    if (raw && typeof raw === 'object') {
      appearance.typography = isTypography(raw.typography) ? raw.typography : 'system';
      /* 老配置没这个字段 → 落在荧光笔（reactive 的初值）。**不整份覆盖**：只取认得的键，
         读的时候也不会把别的偏好抹掉。 */
      appearance.highlight = isHighlightStyle(raw.highlight) ? raw.highlight : 'marker';
      /* 本机字体：只收合理长度的字符串（字体名不该带引号/换行），坏值当没设 */
      appearance.fontFamily = typeof raw.fontFamily === 'string'
        ? raw.fontFamily.replace(/[\r\n"';{}]/g, '').trim().slice(0, 64)
        : '';
      /* 老版本存的是反过来的 reduceMotion：读得到就换算，别让老用户的选择被静默抹掉 */
      if (typeof raw.motion === 'boolean') appearance.motion = raw.motion;
      else if (typeof raw.reduceMotion === 'boolean') appearance.motion = !raw.reduceMotion;
      else appearance.motion = !prefersReducedMotion();
      return;
    }
  } catch {
    /* 坏值 / 隐私模式：落到下面的默认 */
  }
  appearance.motion = !prefersReducedMotion();
}

/** 把偏好写到 :root。挂载前也要跑一次，否则首帧会用系统栈排一遍再跳。 */
export function applyAppearance(): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.dataset.typo = appearance.typography;
  /* 本机字体：内联 --sans / --display 优先级高于 tokens.css 里 data-typo 那几条，
     所以选了一款就盖住预设栈；清空（选回预设）时把内联值摘掉，预设栈自动接管。 */
  if (appearance.fontFamily) {
    const family = JSON.stringify(appearance.fontFamily);
    root.style.setProperty('--sans', `${family}, var(--sans-fallback, ui-sans-serif)`);
    root.style.setProperty('--display', `${family}, var(--sans-fallback, ui-sans-serif)`);
  } else {
    root.style.removeProperty('--sans');
    root.style.removeProperty('--display');
  }
  root.dataset.motion = appearance.motion ? 'on' : 'off';
  /* `data-hl-style`：base.css 用它切「荧光笔 / 下划线」两种画法 */
  root.dataset.hlStyle = appearance.highlight;
}

function commit(): void {
  applyAppearance();
  try {
    localStorage.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify(appearance));
  } catch {
    /* 隐私模式 / 配额满：这一会儿仍然生效，只是记不住 */
  }
}

export function setTypography(next: Typography): void {
  if (!isTypography(next)) return;
  appearance.typography = next;
  /* 预设与本机字体互斥：点预设就等于放弃本机字体 */
  appearance.fontFamily = '';
  commit();
}

/**
 * 换**本机字体**（设置页从 queryLocalFonts() 里选的那一款）；传空串 = 回到预设栈。
 *
 * 为什么单独一个字段而不是扩 Typography：预设栈是固定枚举（三选一），而本机字体有几百款、
 * 名字由用户的系统决定，只能存字符串。两者互斥：选了本机字体，typography 只当兜底。
 */
export function setFontFamily(next: string): void {
  appearance.fontFamily = String(next ?? '').replace(/[\r\n"';{}]/g, '').trim().slice(0, 64);
  commit();
}

export function setMotion(next: boolean): void {
  appearance.motion = next === true;
  commit();
}

/** 换高亮画法：即时生效（写 `data-hl-style`），并立刻落盘 —— 标注数据一个字不动。 */
export function setHighlight(next: HighlightStyle): void {
  if (!isHighlightStyle(next) || next === appearance.highlight) return;
  appearance.highlight = next;
  commit();
}

/** 恢复默认：字体回系统栈、细腻动画回开、高亮回荧光笔。**不**重新跟随系统偏好 —— 用户点的是"恢复默认"。 */
export function resetAppearance(): void {
  appearance.typography = 'system';
  appearance.motion = true;
  appearance.highlight = 'marker';
  commit();
}

let booted = false;

/** 入口最顶部（`bootDisplayScale` 旁边）调用一次。 */
export function bootAppearance(): void {
  if (booted) return;
  booted = true;
  load();
  applyAppearance();
}
