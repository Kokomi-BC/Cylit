import { inject, type InjectionKey, type Ref } from 'vue';

/**
 * 「这个论文标签现在是可见的那一个吗」。
 *
 * 外壳（`components/TabHost.vue`）把每个打开过的论文标签都**留在 DOM 里**，
 * 切标签只切 `display`。于是实例永远不会卸载，视图也就不能靠 `onUnmounted` 判断"我下班了" ——
 * 得由宿主告诉它。这里就是那个通道：宿主 provide 一个 `Ref<boolean>`，
 * 视图 `useTabActive()` 拿到后自己停止页面级监听与计时。
 *
 * 不在宿主里渲染（比如老的单 RouterView 路径）时拿到 `null`，视图按"一直可见"处理。
 */
export const TAB_ACTIVE: InjectionKey<Ref<boolean>> = Symbol('pmTabActive');

export function useTabActive(): Ref<boolean> | null {
  return inject(TAB_ACTIVE, null);
}
