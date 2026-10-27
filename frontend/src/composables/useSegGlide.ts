/**
 * 给页面上**每一个** `.seg` 自动挂一枚滑动指示块（用户口径 2026-12-27「为类似设计的
 * 切换按钮添加动画」：侧栏页签 / 标签条 / 视图胶囊已经会滑，但目录页、对话页、设置面板里
 * 那一排排同样的托盘切换时还是"旧的白块消失、新的白块出现"）。
 *
 * 为什么不给每处各写一遍 `useGlideThumb`：`.seg` 全站有三十多枚，逐处 `ref` + `watch` 是纯样板，
 * 而且漏挂一枚在界面上根本看不出来（只是切换时不动）。这里只做三件事：
 *   ① 扫一遍根下所有 `.seg`，没有指示块的插一枚 `<span class="glide-thumb seg-glide">`、容器加 `has-glide`；
 *   ② 激活项一变（class 变化）或容器尺寸一变就重量（`offsetLeft` / `offsetWidth` = 布局位置，
 *      不受 hover / press 的 transform 影响）；
 *   ③ 卸载时把插进去的块与观察者一起撤掉。
 *
 * 已经被手动挂过（容器里已经有 `.glide-thumb`）的 `.seg` 一律**跳过**：那几处有自己的几何 / inset
 * 调法，交给 `useGlideThumb` 继续管 —— 两套不能同时往同一个托盘里插块。
 *
 * 为什么挂在 `document.body` 上而不是每个视图各挂一次：`App.vue` 只写一行就覆盖全部（含 teleport
 * 出去的浮层），视图增删也不用逐个记得挂。代价是观察者覆盖面很大，所以三处开销都要压住：
 * 合批（`MUTATION_DEBOUNCE_MS`）、缓存已挂托盘（不每次重扫整棵树）、只在**新增节点**时才重扫。
 */
import { onBeforeUnmount, onMounted } from 'vue';

/** 观察者的合批窗口：class 抖动一次切换会改两枚按钮的 class，别因此量两遍。 */
const MUTATION_DEBOUNCE_MS = 120;

export function useSegGlide(root: HTMLElement | (() => HTMLElement | null)): () => void {
  const getRoot = typeof root === 'function' ? root : () => root;
  /** 已挂过的托盘 → 它那枚指示块 */
  const segs = new Map<HTMLElement, HTMLElement>();
  let mo: MutationObserver | null = null;
  let ro: ResizeObserver | null = null;
  let timer = 0;
  let needRescan = false;

  function measure(seg: HTMLElement, thumb: HTMLElement) {
    /* 选中态有两套写法：全局 `.seg` 用 `.active`，设置面板那几枚用 `.on`；
       再兜一层 `aria-pressed="true"`（工具栏里的多选圆钮也是这么标的，但那些不在 .seg 里）。 */
    const act = seg.querySelector<HTMLElement>(
      ':scope > .active, :scope > .on, :scope > * > .active, :scope > * > .on');
    if (!act || !act.offsetWidth) { thumb.style.width = '0px'; return; }
    const parent = act.parentElement;
    const left = act.offsetLeft + (parent && parent !== seg ? parent.offsetLeft : 0);
    thumb.style.width = act.offsetWidth + 'px';
    thumb.style.transform = 'translate3d(' + Math.round(left) + 'px, 0, 0)';
  }

  function sync(seg: HTMLElement) {
    const thumb = segs.get(seg);
    if (thumb && thumb.isConnected) { measure(seg, thumb); return; }
    if (!seg.isConnected || seg.querySelector(':scope > .glide-thumb')) { segs.delete(seg); return; }
    const span = document.createElement('span');
    span.className = 'glide-thumb seg-glide';
    span.setAttribute('aria-hidden', 'true');
    seg.insertBefore(span, seg.firstChild);
    seg.classList.add('has-glide');
    segs.set(seg, span);
    ro?.observe(seg);
    measure(seg, span);
  }

  function flush() {
    timer = 0;
    const box = getRoot();
    if (needRescan && box) {
      needRescan = false;
      box.querySelectorAll<HTMLElement>('.seg').forEach(sync);
    }
    for (const seg of Array.from(segs.keys())) sync(seg);
  }

  function schedule(rescan: boolean) {
    if (rescan) needRescan = true;
    if (timer) return;
    timer = window.setTimeout(flush, MUTATION_DEBOUNCE_MS);
  }

  onMounted(() => {
    const box = getRoot();
    if (!box) return;
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => schedule(false));
    }
    if (typeof MutationObserver !== 'undefined') {
      mo = new MutationObserver((records) => {
        /* 只关心**增删节点**与 class：前者可能需要重扫（新托盘上屏），后者只需重量。 */
        let added = false;
        for (const r of records) if (r.addedNodes.length || r.removedNodes.length) { added = true; break; }
        schedule(added);
      });
      /* 只盯 class 与增删；span 自己的 style 是本函数写的，盯它会绕成死循环 */
      mo.observe(box, { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] });
    }
    schedule(true);
  });

  onBeforeUnmount(() => {
    mo?.disconnect(); mo = null;
    ro?.disconnect(); ro = null;
    if (timer) { clearTimeout(timer); timer = 0; }
    for (const [seg, thumb] of segs) { thumb.remove(); seg.classList.remove('has-glide'); }
    segs.clear();
  });

  /** 手动重扫（路由切换后想立刻校正时用；不传也有一条观察者兜着） */
  return () => schedule(true);
}