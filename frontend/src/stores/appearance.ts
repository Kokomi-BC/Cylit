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

/** 一类文字用的字体：三套预设之一，或本机字体（`local:<族名>`）。 */
export type FontChoice = Typography | `local:${string}`;

/** 五类文字各自的字体（设置页「分类字号」每一行最右边那个下拉）。 */
export type CategoryFonts = Record<'body' | 'chat' | 'note' | 'side' | 'ui', FontChoice>;

export const FONT_CATEGORIES = ['body', 'chat', 'note', 'side', 'ui'] as const;

export const appearance = reactive<{
  typography: Typography;
  motion: boolean;
  highlight: HighlightStyle;
  /** 分类级字体。默认全是系统栈；`typography` 只当旧配置的兜底。 */
  fonts: CategoryFonts;
}>({
  typography: 'system',
  motion: true,
  /* 旧配置里没有这个字段：默认荧光笔（用户口径 2026-12） */
  highlight: 'marker',
  fonts: { body: 'system', chat: 'system', note: 'system', side: 'system', ui: 'system' },
});

function isTypography(value: unknown): value is Typography {
  return value === 'system' || value === 'yahei' || value === 'serif';
}

/** 把存盘值收成一个合法的 FontChoice：只认三套预设或 `local:<名字>`。 */
function normalizeFont(value: unknown, fallback: FontChoice): FontChoice {
  if (isTypography(value)) return value;
  if (typeof value === 'string' && value.startsWith('local:')) {
    const name = value.slice(6).replace(/[\r\n"';{}]/g, '').trim().slice(0, 64);
    if (name) return `local:${name}`;
  }
  return fallback;
}

/** 字体选择 → CSS 字面栈。选「系统默认」返回空串（让 tokens.css 的默认栈接手）。 */
function fontStackOf(choice: FontChoice): string {
  if (choice === 'system') return '';
  if (choice === 'yahei') return 'var(--font-stack-yahei)';
  if (choice === 'serif') return 'var(--font-stack-serif)';
  const name = choice.slice(6);
  return `${JSON.stringify(name)}, var(--font-stack-system)`;
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
      /* 分类字体：只收认得的预设名或 local:<合理长度>；老配置没有就跟随旧的字面预设 */
      const legacy = isTypography(raw.typography) ? raw.typography : 'system';
      const legacyLocal = typeof raw.fontFamily === 'string'
        ? raw.fontFamily.replace(/[\r\n"';{}]/g, '').trim().slice(0, 64)
        : '';
      const stored = (raw.fonts && typeof raw.fonts === 'object' ? raw.fonts : {}) as Record<string, unknown>;
      for (const key of FONT_CATEGORIES) {
        appearance.fonts[key] = normalizeFont(stored[key], legacyLocal ? `local:${legacyLocal}` : legacy);
      }
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
  /* 分类字体：每类把内联 `--font-sans-<类>` 写到 :root 上，base.css 的分类根再读它 ——
     选「系统默认」就把内联值摘掉，交回 tokens.css 里的默认栈。 */
  for (const key of FONT_CATEGORIES) {
    const stack = fontStackOf(appearance.fonts[key]);
    if (stack) root.style.setProperty(`--font-sans-${key}`, stack);
    else root.style.removeProperty(`--font-sans-${key}`);
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

/** 旧的全局入口：把五类一起设成这个预设（现在设置页改用 `setCategoryFont` 逐类选）。 */
export function setTypography(next: Typography): void {
  if (!isTypography(next)) return;
  appearance.typography = next;
  for (const key of FONT_CATEGORIES) appearance.fonts[key] = next;
  commit();
}

/**
 * 给**某一类**文字换字体（设置页「分类字号」每一行最右那个下拉）。
 *
 * 五类各存一份：`system` / `yahei` / `serif` 是预设栈，`local:<族名>` 是本机字体。
 * 以前是一个全局 `fontFamily`：用户口径 2026-12 改成「每类都能自己选」，于是拆成五份。
 */
export function setCategoryFont(key: keyof CategoryFonts, next: FontChoice): void {
  if (!FONT_CATEGORIES.includes(key)) return;
  appearance.fonts[key] = normalizeFont(next, 'system');
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
