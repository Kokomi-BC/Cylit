import { nextTick } from 'vue';

/**
 * 「从输入框飞进气泡」的交接动画（FLIP 的替身）。
 *
 * 参考实现里这一段是 400ms：输入框整块往消息落点上收，底部工具行同时缩到 0.55 并淡出，
 * 最后落成一个气泡。这里不改真实 DOM（输入框要留着、要保住焦点），而是量一次输入框的矩形，
 * 造一枚**只用于这一趟**的幽灵元素，把它贴到气泡的最终位置上再扔掉：
 *
 * - 起点 = 发送瞬间输入框的外框（含圆角 / 边框 / 底色的观感）；
 * - 终点 = 气泡的矩形（平移 + 缩放一起做，字号自然跟着缩）；
 * - 透明度在最后 30% 收掉，正好和气泡自己「从右下角长出来」的 320ms 接上。
 *
 * 系统开了「减少动效」就整段跳过 —— 气泡那边还有一条 scale 动画，同样会被全局媒体查询关掉。
 */

export type Flight = { rect: DOMRect; text: string };

/** 发送瞬间量一次输入框。`text` 会画在幽灵上，所以必须在清空 draft 之前调用。 */
export function captureFlight(el: HTMLElement | null | undefined, text: string): Flight | null {
  const trimmed = text.trim();
  if (!el || !trimmed) return null;
  if (typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches) return null;
  return { rect: el.getBoundingClientRect(), text: trimmed };
}

/** 量一个元素的**布局盒**：气泡自己正在播 scale 进场动画，直接量 rect 会量到缩到 0.2 的中间态。 */
function layoutRect(el: Element): DOMRect {
  const node = el as HTMLElement;
  const prev = node.style.animation;
  node.style.animation = 'none';
  const rect = node.getBoundingClientRect();
  node.style.animation = prev;
  return rect;
}

/** 气泡渲染完之后调用（记得先 `await nextTick()`）。 */
export function flyToBubble(flight: Flight | null, target: Element | null | undefined): void {
  if (!flight || !target) return;
  const to = layoutRect(target);
  const from = flight.rect;
  if (to.width <= 0 || from.width <= 0) return;

  const dx = to.left - from.left;
  const dy = to.top - from.top;
  const sx = to.width / from.width;
  const sy = to.height / from.height;
  const css = getComputedStyle(document.documentElement);
  const bubbleBg = getComputedStyle(target).backgroundColor || css.getPropertyValue('--accent-soft');

  const ghost = document.createElement('div');
  ghost.textContent = flight.text;
  ghost.setAttribute('aria-hidden', 'true');
  Object.assign(ghost.style, {
    position: 'fixed',
    left: `${from.left}px`,
    top: `${from.top}px`,
    width: `${from.width}px`,
    height: `${from.height}px`,
    boxSizing: 'border-box',
    padding: '6px 14px',
    border: '1px solid var(--border)',
    borderRadius: '16px',
    background: 'var(--bg-elev)',
    color: 'var(--text)',
    fontFamily: 'inherit',
    fontSize: 'calc(14px * var(--ts, 1))',
    lineHeight: '1.6',
    whiteSpace: 'pre-wrap',
    overflow: 'hidden',
    transformOrigin: 'top left',
    pointerEvents: 'none',
    zIndex: '9999',
  } satisfies Partial<CSSStyleDeclaration>);
  document.body.appendChild(ghost);

  const anim = ghost.animate(
    [
      { transform: 'none', opacity: 1, borderRadius: '16px', backgroundColor: 'var(--bg-elev)', borderColor: 'var(--border)' },
      { transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})`, opacity: .92, borderRadius: '14px', backgroundColor: bubbleBg, borderColor: 'transparent', offset: .55 },
      { transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})`, opacity: 0, borderRadius: '14px', backgroundColor: bubbleBg, borderColor: 'transparent', offset: .85 },
      { transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})`, opacity: 0, borderRadius: '14px', backgroundColor: bubbleBg, borderColor: 'transparent' },
    ],
    { duration: 400, easing: 'cubic-bezier(.22,.61,.36,1)', fill: 'forwards' },
  );
  void anim.finished.catch(() => {}).finally(() => ghost.remove());
}

/** 「发送 → 气泡落位」的整套交接：量、等渲染、飞。 */
export async function flyMessage(
  sourceEl: HTMLElement | null | undefined,
  text: string,
  findBubble: () => Element | null | undefined,
): Promise<void> {
  const flight = captureFlight(sourceEl, text);
  if (!flight) return;
  await nextTick();
  flyToBubble(flight, findBubble());
}
