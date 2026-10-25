import { ref, type Ref } from 'vue';

/**
 * 贴底跟随（follow tail）—— 对话流的滚动策略。
 *
 * 三条约定都来自参考实现（dsh web / `use-chat-reading`），不自己发明：
 *
 * 1. **只有「本来就贴着底」才自动滚**。距底超过 `gap` 就算读者已经翻走了，新内容不许把他拽回去；
 *    读者自己滚回阈值内，跟随自动恢复。
 * 2. **程序滚动用 instant，不用 smooth**。流式插入是每几百毫秒一次，smooth 的长尾会和下一次插入
 *    互相拉扯，看起来在抖（参考实现只在「读者自己把滚动条拖到底」那一次用 smooth）。
 * 3. 跟随中给容器挂 `overflow-anchor: none`（见 `ChatView.vue` 的 `.tail-anchor`）：浏览器滚动锚定
 *    会和自动滚动抢同一个位置，表现为滚到底后又弹回一点。
 */

/** 距底多少像素内仍算「跟随最新」：参考实现取 25px。 */
export const FOLLOW_GAP = 25;

export type FollowBehavior = 'instant' | 'smooth';

export function useFollowTail(el: Ref<HTMLElement | null>, gap = FOLLOW_GAP) {
  /** 是否跟随最新内容（读者上翻即 false，滚回底部即 true）。 */
  const following = ref(true);
  /** 上一次程序滚动落点：用来把「浏览器发的 scroll 事件」和「读者自己滚」区分开。 */
  let programTop = Number.NaN;
  /** 平滑滚动还在飞的时间窗：中间那些位置不是读者的意图，别拿来判定跟随。 */
  let smoothUntil = 0;

  const floorOf = (node: HTMLElement): number => node.scrollHeight - node.clientHeight;
  const nearBottom = (node: HTMLElement): boolean => floorOf(node) - node.scrollTop <= gap;

  /** 无条件滚到底（无视当前跟随状态）。 */
  function pin(behavior: FollowBehavior = 'instant'): void {
    const node = el.value;
    if (!node) return;
    const top = floorOf(node);
    if (behavior === 'smooth') {
      smoothUntil = performance.now() + 800;
      programTop = top;               // 目标是底，不是动画第一帧的位置
      node.scrollTo({ top, behavior: 'smooth' });
      return;
    }
    smoothUntil = 0;
    node.scrollTo({ top, behavior: 'instant' });
    programTop = node.scrollTop;
  }

  /** 新内容落地后调用：跟随中才滚，读者翻走了就原地不动。 */
  function follow(behavior: FollowBehavior = 'instant'): void {
    if (!following.value) return;
    pin(behavior);
  }

  /** 绑到滚动容器的 `@scroll`：读者动过就按「离底多远」重算跟随意图。 */
  function onScroll(): void {
    const node = el.value;
    if (!node) return;
    if (performance.now() < smoothUntil) return;
    // ±2px 容差：scrollTo 的落点会被浏览器夹紧，同一帧里 scrollHeight 还在继续长
    if (Math.abs(node.scrollTop - programTop) <= 2) {
      following.value = true;         // 停在程序落点上 = 还贴着底
      return;
    }
    following.value = nearBottom(node);
  }

  /** 主动回到最新（「回到底部」按钮）：这一次给 smooth。 */
  function returnToBottom(): void {
    following.value = true;
    pin('smooth');
  }

  return { following, follow, pin, onScroll, returnToBottom };
}
