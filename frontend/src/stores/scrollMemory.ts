/**
 * 每个标签页的**页面滚动位置**（窗口滚动，不含阅读器 —— 阅读器由服务端进度管，
 * 见 `router.scrollBehavior` 里 `to.name === 'reader'` 那一条）。
 *
 * 为什么需要：外壳是 `<RouterView :key="routeKey">`，切标签 = 卸载旧视图 + 新建新视图，
 * 浏览器把滚动位置归零 —— 在设置页翻到一半、去论文库看一眼再切回来，又从头开始
 * （用户 2026-10-03："切换标签页记录设置页等页面的阅读位置"）。
 *
 * 为什么记在内存里而不是 localStorage：这是"**这一会儿**看到哪"，刷新/重开浏览器之后
 * 从顶部开始才符合预期；标签条本身虽然持久化，但位置跟着会话走更自然。
 */

const memory = new Map<string, number>();

/** 记下某个标签键的位置（0 视为"没滚"，直接清掉，省得下次白滚一趟）。 */
export function rememberScroll(key: string, y: number): void {
  if (!key) return;
  const top = Math.round(y);
  if (top > 0) memory.set(key, top);
  else memory.delete(key);
}

/** 取某个标签键的位置（没记过 = 0 = 顶部）。 */
export function savedScroll(key: string): number {
  return memory.get(key) ?? 0;
}

/** 忘掉某个标签键的位置：显式重新打开 / 关掉标签时用（与 `openTab` 清 query 同一个口径）。 */
export function forgetScroll(key: string): void {
  memory.delete(key);
}
