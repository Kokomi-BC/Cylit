/**
 * 滑动指示块 —— **量位置这件事只做一次，三处共用**。
 *
 * 为什么是组合式函数而不是各处自己写：顶部论文标签条（`TabBar`）、阅读器侧栏页签、
 * 顶部胶囊（原文 / 译文 / 对照）三处是**同一套物理** —— 一枚绝对定位的块，激活项一变就
 * `translate3d` + `width` 挪过去。三处各写一套的话，"夹在容器可见范围内"的规则、
 * "量不到就不显示"的兜底、以及增删 / 换序后重量的时机，迟早会长歪三份；
 * 材质与过渡同理，只留一份全局的 `.glide-thumb`（见 `styles/base.css`）。
 *
 * 只碰 `transform` 与 `width`：前者走合成器、不重排；后者是**绝对定位元素自身**的宽度，
 * 不带动任何兄弟节点。所以切页签时容器的高度 / 布局一动不动。
 *
 * 用法（容器那层要有 `position: relative`）：
 *   const { thumbStyle, thumbVisible, remeasure } = useGlideThumb({
 *     container: strip,                       // Ref<HTMLElement | null>
 *     active: () => strip.value?.querySelector<HTMLElement>('.tab.active') ?? null,
 *   });
 *   <span v-if="thumbVisible" class="glide-thumb" :style="thumbStyle" />
 */
import {
  computed, onBeforeUnmount, onMounted, ref, watch,
  type ComputedRef, type CSSProperties, type Ref,
} from 'vue';

export interface GlideThumbOptions {
  /** 容器 = 指示块的定位基准（必须 `position: relative`），也是夹边 / 裁剪的范围 */
  container: Ref<HTMLElement | null>;
  /**
   * 当前激活的那一枚。**由调用方决定**（选择器 / `.active` 类 / ref 都行），
   * 本函数因此不绑任何 DOM 结构；返回 null（或长度不满两项）就隐藏指示块。
   */
  active: () => HTMLElement | null | undefined;
  /** 可见范围两端各让开的距离（容器两端浮着圆钮的场合夹边时内缩），默认 0 */
  inset?: number;
}

export interface GlideThumb {
  /** 直接绑到指示块上：`translate3d` 走合成，只有 `width` 是真会变的属性 */
  thumbStyle: ComputedRef<CSSProperties>;
  /** 量不到位置（没有激活项 / 处于隐藏 / 还没上屏）时 false —— 别闪一枚 0 宽的方块 */
  thumbVisible: ComputedRef<boolean>;
  /** 手动重量：跑完 FLIP 换序、或程序化滚动之后想立刻校正时调一下 */
  remeasure: () => void;
}

export function useGlideThumb(opts: GlideThumbOptions): GlideThumb {
  /** 相对容器**内容原点**的位移（已含 scrollLeft 换算）：指示块是容器内容的一部分，
   *  容器滚动时它跟着标签一起走，不需要每帧重算 —— 重算反而会拖出一帧滞后。 */
  const x = ref(0);
  const w = ref(0);
  /** 最近一次量成功了没有（量不到就别显示） */
  const located = ref(false);

  function remeasure() {
    const box = opts.container.value;
    const el = opts.active();
    if (!box || !el) { located.value = false; return; }
    /* 宽度取**布局盒**（offsetWidth）而不是 rect：rect 会跟着 `:active` 的 scale 一起缩，
       鼠标按下那一刻指示块会跟着抖一下。 */
    const width = el.offsetWidth;
    if (!width) { located.value = false; return; }      // 被藏起来（display:none）的元素量不出东西

    let left: number;
    if (el.offsetParent === box) {
      /* 同一套坐标：absolute 的 `left: 0` 与 `offsetLeft` 都以容器的 padding 边为原点。
         而且 offset 是**布局**位置 —— 拖拽换序时标签正在跑 FLIP（带着 transform），
         rect 拿到的是动画中间态，offset 才是它真正的落点。 */
      left = el.offsetLeft;
    } else {
      /* 激活项不在容器的直接定位链上（比如多包了一层）：退回 rect 换算，仍是内容坐标 */
      const b = box.getBoundingClientRect();
      const a = el.getBoundingClientRect();
      left = a.left - b.left + box.scrollLeft - box.clientLeft;
    }

    /* 夹在容器**可见范围**里：激活项被滚出视野 / 被拖到边上时，指示块停在边上不跟出去 */
    const inset = opts.inset ?? 0;
    const lo = box.scrollLeft + inset;
    const hi = Math.max(lo, box.scrollLeft + box.clientWidth - inset - width);
    const nextX = Math.round(Math.min(Math.max(left, lo), hi));

    /* 值没变**就不写**：写进去会触发一次 MutationObserver（容器里多了一条 class 变化），
       再量一次又写一次 —— 用"只有真的变了才更新"把这条自激的回路掐掉。 */
    if (nextX !== x.value) x.value = nextX;
    if (width !== w.value) w.value = width;
    located.value = true;
  }

  const thumbStyle = computed<CSSProperties>(() => ({
    width: `${w.value}px`,
    transform: `translate3d(${x.value}px, 0, 0)`,
  }));
  const thumbVisible = computed(() => located.value && w.value > 0);

  /* --- 什么时候重量：容器换了尺寸 / 里面增删了节点 / 激活项换了类 / 容器被滚动 ---
   * 调用方因此不用再各自挂 watch：这三件事对三处切换器是同一批事件。 */
  let ro: ResizeObserver | null = null;
  let mo: MutationObserver | null = null;
  let bound: HTMLElement | null = null;

  function unbind() {
    ro?.disconnect(); ro = null;
    mo?.disconnect(); mo = null;
    bound?.removeEventListener('scroll', remeasure);
    bound = null;
  }

  function bind() {
    const box = opts.container.value;
    if (box === bound) return;
    unbind();
    if (!box) { located.value = false; return; }
    bound = box;
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(remeasure);
      ro.observe(box);
    }
    /* 只看 class：激活项换的就是 class（增删由 childList 兜）。**不看 style** ——
       指示块自己的 style 是本函数写进去的，盯着它会绕成死循环。 */
    if (typeof MutationObserver !== 'undefined') {
      mo = new MutationObserver(remeasure);
      mo.observe(box, { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] });
    }
    box.addEventListener('scroll', remeasure, { passive: true });
    remeasure();
  }

  onMounted(() => {
    bind();
    window.addEventListener('resize', remeasure);
  });
  /* 容器本身可能是 `v-if` 出来的（标签条一枚标签都没有时整个托盘不渲染）：
     出现了再接观察者，别只在 onMounted 试一次。 */
  watch(opts.container, bind);
  onBeforeUnmount(() => {
    unbind();
    window.removeEventListener('resize', remeasure);
  });

  return { thumbStyle, thumbVisible, remeasure };
}
