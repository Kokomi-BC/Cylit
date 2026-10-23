/**
 * 显示偏好：**字体族 + 细腻动画**（本机偏好，只写 localStorage）。
 *
 * 两项都落成 `<html>` 上的 data 属性，再由 `styles/tokens.css` 里同名选择器接管：
 * · `data-typo` 只换 `--sans` / `--display` 两个字面栈（`--mono` / `--serif` 不动 ——
 *   代码块与论文原文的字体是内容语义，不是阅读偏好）；
 * · `data-motion="off"` 把三个动效时长令牌归零。不逐处关 transition：新增组件总会漏一个，
 *   而归零令牌是一次性的、覆盖全站的。
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

export const APPEARANCE_STORAGE_KEY = 'cylit:appearance';

export const appearance = reactive<{ typography: Typography; motion: boolean }>({
  typography: 'system',
  motion: true,
});

function isTypography(value: unknown): value is Typography {
  return value === 'system' || value === 'yahei' || value === 'serif';
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
  root.dataset.motion = appearance.motion ? 'on' : 'off';
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
  commit();
}

export function setMotion(next: boolean): void {
  appearance.motion = next === true;
  commit();
}

/** 恢复默认：字体回系统栈、细腻动画回开。**不**重新跟随系统偏好 —— 用户点的是"恢复默认"。 */
export function resetAppearance(): void {
  appearance.typography = 'system';
  appearance.motion = true;
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
