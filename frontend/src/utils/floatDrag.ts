/**
 * 浮层（对话浮窗 / 笔记卡 / 解释卡）**手势期间的选区闸门**。
 *
 * 以前这里是 `document.body.style.userSelect = 'none'`，两个问题（用户口径 2026-11-24 第 2、3 条）：
 * - 它锁的是**整个文档**：一次手势两次文档级样式重算，落定那一帧就是一次可见的卡顿；
 * - 更要紧的是它**把浮层自己的文字也锁死了** —— 想在对话/笔记里框选一段复制，选不动；
 *   而且任何一条漏掉的 pointerup 都会让整个应用一直不可选。
 *
 * 真正需要关掉的只有一层：PDF 的 `.pdf-text-layer`。它在 CSS 里写死了 `user-select: text`，
 * 是唯一不跟随继承、能自己起原生选区的东西（指针扫过就长出一段，拖角放大时尤其明显）。
 * 所以手势期间只给 `<html>` 挂一个类（见 base.css 的 `html.cy-float-gesture`），其余内容照旧可选。
 *
 * 计数式：对话浮窗与两张卡可以交替发起手势，谁也不会把闸门提前关掉。
 */
let depth = 0;

export function lockFloatSelection(on: boolean): void {
  depth = Math.max(0, depth + (on ? 1 : -1));
  document.documentElement.classList.toggle('cy-float-gesture', depth > 0);
}

/** 卸载/异常路径的兜底：无条件放闸（宁可多放一次，也不要卡住整页的选区） */
export function releaseFloatSelection(): void {
  depth = 0;
  document.documentElement.classList.remove('cy-float-gesture');
}

/** 清掉已有的选区：手势期间不该在正文里留着一段"看起来像待操作"的选中文字 */
export function clearDocSelection(): void {
  const sel = window.getSelection();
  if (sel && !sel.isCollapsed) sel.removeAllRanges();
}
