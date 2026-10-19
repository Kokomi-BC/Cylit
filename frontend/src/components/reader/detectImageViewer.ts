/**
 * 图片查看器手势控制器 —— 逐字移植自 `_detect_mobile.js`（第 3823 行 `initImageTouchHandlers` 起，
 * 含紧随其后的 `updateImageTransform` / `lockImageDimension` / `lockImageElementDimension` /
 * `settleImageToNaturalSize` / `switchModalImageByDelta`）。
 *
 * 为什么是"命令式控制器"而不是一堆响应式状态：这套手势算法靠**直接写 transform 字符串**、
 * **逐帧读布局**工作（捏合中不夹取、松手才归位、过渡只在对齐那一下开）。拆成 ref + computed 会改变
 * 写入时序（Vue 的渲染在下一个 tick），边界计算就会读到上一帧的尺寸，行为立刻漂移。所以保持原样：
 * 状态全在闭包里，DOM 由控制器直接操作；Vue 只负责挂载 / 卸载、把可用尺寸喂进来、订阅倍率读数。
 *
 * 移植时只有三处适配（其余全部照抄）：
 * 1. 源文件的模块级全局（imageScale / imageX / imageY / imageRotation / modalImageList /
 *    modalImageIndex / modalImageSwitching）收进 createDetectImageViewer 的闭包。
 * 2. document.getElementById('imageModal' | 'modalImage') 换成注入的 stage / img；
 *    window.history.back() 换成 opts.onClose()。
 * 3. 布局尺寸：源文件把锁定的像素宽高写进 img.style.width / height；Cylit 里 .sheet 才是被定尺寸的
 *    元素（由组件渲染），所以 lockImageElementDimension 把锁定结果回推给组件，控制器本身只写 transform。
 * 4. 源文件只有 touch 事件（移动端），桌面上按住鼠标拖图会失效。这里加一层 pointer 通道：鼠标左键的
 *    pointerdown/move/up 折成与 touch 同参数的 GestureEvent（当前活动指针 + 本次变化指针 + preventDefault），
 *    喂给同一套状态机 —— 手势数学只有一份。触摸设备上 pointerType === 'touch' 的事件直接丢弃，
 *    保证一份手势不会被 touch 与 pointer 各处理一遍。
 */

/** 源文件模块级常量（第 78 行） */
const ROTATION_INTENT_THRESHOLD = 18;

/**
 * 源文件里这个值是 touchmove 内的局部 const sensitivity = 1.25（第 4312 行），
 * 提到模块级只为对齐 const SENS = 1.25 的写法，数值不变。
 */
const SENS = 1.25;

/** zoomBy（按钮 / 键盘 / 双击）的倍率夹取范围：取捏合 0.2~12 与滚轮 0.1~10 的并集 */
const ZOOM_MIN = 0.1;
const ZOOM_MAX = 12;

/** 状态机只认"点的屏幕坐标"：touch 与 pointer 两条通道都折成它 */
type Pt = { clientX: number; clientY: number };

/** 状态机的入参：当前所有活动指针、本次变化的指针、preventDefault */
type GestureEvent = { touches: Pt[]; changedTouches: Pt[]; preventDefault: () => void };

/** setGallery 的列表项：url 必填，caption 对齐 ZoomTarget 的字段（这里不渲染标题，只透传） */
export interface GalleryEntry {
  url: string;
  caption?: string | null;
}

export interface DetectImageViewerOptions {
  /** 整屏舞台：手势监听挂在这里（源文件的 #imageModal），拖拽关闭时变的也是它的底色 */
  stage: HTMLElement;
  /** 被变换的那张图（源文件的 #modalImage） */
  img: HTMLImageElement;
  /** 源文件里的 window.history.back() */
  onClose: () => void;
  /** 100% 时的可用框（已留出边距与底部胶囊），由组件按现有 fit 计算给出 */
  getSize: () => { w: number; h: number };
  /** 锁定后的 100% 基准像素尺寸回推给组件，组件据此渲染 .sheet 的 width / height */
  onSize?: (w: number, h: number) => void;
}

export interface DetectImageViewer {
  destroy: () => void;
  /** 图片自然尺寸就绪 / 容器尺寸变化后重新锁定 100% 基准（不重置缩放位移） */
  setImage: (w: number, h: number) => void;
  /**
   * 写入相邻图片列表与当前下标：长度 > 1 时左右拖动就能切上一张 / 下一张。
   * 会把当前图换成 list[index].url，并把视图复位到 100%（缩放 / 位移 / 旋转全部归零后重新锁定尺寸）。
   */
  setGallery: (list: GalleryEntry[], index: number) => void;
  /** 当前在列表中的下标（切图成功后由控制器推进） */
  getIndex: () => number;
  /** 切图成功后回调一次新下标（左右拖动切图的唯一对外信号） */
  onIndexChange: (cb: (index: number) => void) => void;
  /** 按倍率缩放，可指定屏幕锚点（默认绕视口中心） */
  zoomBy: (factor: number, cx?: number, cy?: number) => void;
  /** 回到 100%、旋转归零、位移归零 */
  reset: () => void;
  /**
   * 退出动画：把缩放 / 位移直接写成给定值、旋转归零，带 `ms` 的过渡飞过去。
   * 几何由组件按触发缩略图的矩形算好，这里**不做 clamp**（那是手势边界的事，不是退出动画的事）。
   */
  animateTo: (scale: number, x: number, y: number, ms?: number) => void;
  /** 退出动画专用：立 exiting 标志，期间手势 / 归位 / 切图全部让路 */
  beginExit: (scale: number, x: number, y: number, ms?: number) => void;
  /** 切到相邻图（-1 上一张 / +1 下一张）；越界或正在切换时自己会拒绝 */
  stepTo: (delta: number) => void;
  getScale: () => number;
  onChange: (cb: (scale: number) => void) => void;
  /**
   * 本次按下是否落在黑底上（没落在图片、也没落在胶囊等 UI 上）。
   * 指针被捕获后 click 的 target 会变成 stage，光看 click 无法分辨点的到底是背景还是按钮，
   * 所以由按下时记下的目标来判断「这算不算点了背景」。
   */
  isBackgroundPress: () => boolean;
  /** 本次手势是否已经动过（拖动 / 捏合）—— 背景点击关闭时用来区分"只是点了下" */
  isGesturing: () => boolean;
}

export function createDetectImageViewer(opts: DetectImageViewerOptions): DetectImageViewer {
  const stage = opts.stage;
  const img = opts.img;
  const getSize = opts.getSize;
  const onClose = opts.onClose;

  /* ---------- 源文件的全局状态，收进闭包 ---------- */

  /** 视图状态：位移是屏幕像素，绕自身中心缩放与旋转 */
  let imageScale = 1;
  /** 退出动画进行中：期间一切会改写 transform / transition 的入口都要让路，否则过渡会被打断成瞬跳 */
  let exiting = false;
  let imageX = 0;
  let imageY = 0;
  let imageRotation = 0;

  /** 相邻图片列表（存原始 url 字符串，真正展示时才过 getDisplayUrl）；由组件的 setGallery 写入 */
  let modalImageList: string[] = [img.getAttribute('src') || ''];
  let modalImageIndex = 0;
  /** 最近一次真正写进 img.src 的原始 url：setGallery 据此判断要不要换 src（挂载时不重复请求同一张图） */
  let currentRawUrl = img.getAttribute('src') || '';
  /** 源文件第 83 行的模块级切换锁 */
  let modalImageSwitching = false;
  /** 图片自然尺寸（组件通过 setImage 报告；源文件直接读 imgEl.naturalWidth） */
  let natSize = { w: 0, h: 0 };
  /** 当前锁定的 100% 基准像素尺寸（源文件 = modalImg.style.width / height 的数值） */
  let lockedW = 0;
  let lockedH = 0;

  /** 源文件的 modal.classList.contains('active') —— 组件挂载即激活 */
  let active = true;
  /** 本次手势是否动过（背景点击关闭用） */
  let moved = false;

  const listeners: ((s: number) => void)[] = [];
  /** 切图成功后的下标订阅（组件用来同步它记的"当前第几张"） */
  const indexListeners: ((index: number) => void)[] = [];
  const emitIndex = (index: number) => {
    for (const cb of indexListeners) cb(index);
  };

  /* ---------- 源文件 initImageTouchHandlers 内的局部状态 ---------- */

  let initialDist = 0;
  let initialAngle = 0;
  let isPinching = false;
  let isDragging = false;
  let baseScale = 1;
  let baseRotation = 0;
  let lastTouchX = 0;
  let lastTouchY = 0;
  let initialMidX = 0;
  let initialMidY = 0;
  let baseImageX = 0;
  let baseImageY = 0;
  let lastPinchAngle = 0;
  let accumulatedAngleDiff = 0;
  /** 旋转死区：累计转角没越过这个值之前一律不转（手指搭上去的微小扰动不该让图跟着晃） */
  const ROT_DEAD_ZONE = 7;
  let rotArmed = false;
  let dragStartX = 0;
  let dragStartY = 0;
  let dragPreviewActive = false;
  let dragPreviewOverflowX = 0;
  let dragIntent = '';
  let lastMoveTime = 0;
  let previewCommitInProgress = false;
  let dragDismissBaseScale = 1;
  let nextImgEl: HTMLImageElement | null = null;
  let prevImgEl: HTMLImageElement | null = null;
  let adjacentReleaseTimer: number | null = null;

  /* ---------- 基础 transform（源文件模块级 updateImageTransform） ---------- */

  const updateImageTransform = () => {
    // 基础 transform 更新。不再在此处动态更改 maxWidth / maxHeight 以避免旋转时的布局跳动。
    img.style.transform = `translate(${imageX}px, ${imageY}px) scale(${imageScale}) rotate(${imageRotation}deg)`;
    for (const cb of listeners) cb(imageScale);
  };

  /* ---------- 尺寸锁定（源文件 lockImageDimension / lockImageElementDimension） ---------- */

  const lockImageElementDimension = (imgEl: HTMLImageElement | null, rotation = 0) => {
    if (!imgEl) return;

    const avail = getSize();
    const availW = avail.w;
    const availH = avail.h;
    // 源文件是 imgEl.naturalWidth || vw；这里多一层 setImage 报告的尺寸兜底
    const nw = imgEl.naturalWidth || natSize.w || availW;
    const nh = imgEl.naturalHeight || natSize.h || availH;

    const isRotated = Math.abs(Math.round(rotation / 90) % 2) === 1;
    const testW = isRotated ? nh : nw;
    const testH = isRotated ? nw : nh;
    // 源文件是 Math.min(1, (vw * 0.95) / testW, (vh * 0.85) / testH)：
    // 0.95 / 0.85 的留白已经在 getSize 的 padding 里（b.w - 120 / b.h - 170）；
    // 而 min(1, ...) 的"小图不放大"在 Cylit 旧 fit 语义里没有 —— 旧 fit 是"适应窗口"，
    // 小图也放大，保持这一条以免 100% 的观感与读数跟旧版不一致。
    const fitScale = Math.min(availW / testW, availH / testH);

    const nextW = nw * fitScale;
    const nextH = nh * fitScale;

    if (imgEl !== img) {
      // 相邻预览图：源文件就是直接写这张图的宽高（只有 img 这一张走 lockedW / onSize）
      imgEl.style.width = nextW + 'px';
      imgEl.style.height = nextH + 'px';
      imgEl.style.maxWidth = 'none';
      imgEl.style.maxHeight = 'none';
      return;
    }

    lockedW = nextW;
    lockedH = nextH;
    if (opts.onSize) opts.onSize(nextW, nextH);
  };

  const lockImageDimension = (rotation = 0) => {
    lockImageElementDimension(img, rotation);
  };

  /* ---------- 松手归位（源文件 settleImageToNaturalSize） ---------- */

  const settleImageToNaturalSize = (targetRotation = 0, duration = 420) => {
    if (exiting) return;
    if (!active) return;

    imageRotation = targetRotation;
    imageScale = 1;

    // 源文件这里还带 width / height 的过渡：Cylit 的尺寸落在 .sheet 上（由组件渲染），
    // 留在这张图的 transition 里无副作用，照抄。
    img.style.transition =
      `transform ${duration}ms cubic-bezier(0.22, 0.61, 0.36, 1), width ${duration}ms cubic-bezier(0.22, 0.61, 0.36, 1), height ${duration}ms cubic-bezier(0.22, 0.61, 0.36, 1)`;

    lockImageDimension(targetRotation);

    // 关键修复：旋转完成后自动恢复必须回到屏幕中心，避免沿用放大拖拽时的历史偏移
    imageX = 0;
    imageY = 0;

    updateImageTransform();

    window.setTimeout(() => {
      if (!active) return;
      img.style.transition = 'transform 0.1s ease-out';
    }, duration + 20);
  };

  /* ---------- 边界（源文件 getImageRenderMetrics / clampImagePosition） ---------- */

  const getImageRenderMetrics = () => {
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // 核心改进：直接使用当前锁定的基础像素尺寸，不再内部重复计算 fitScale
    // （源文件是 modalImg.offsetWidth —— 布局宽高，不受 transform 影响；这里加 lockedW 兜底）
    const baseW = img.offsetWidth || lockedW || vw;
    const baseH = img.offsetHeight || lockedH || vh;

    const transformedW = baseW * imageScale;
    const transformedH = baseH * imageScale;

    // 核心修复：平滑边界计算 (基于旋转包围盒)
    const rad = Math.abs(imageRotation) * Math.PI / 180;
    const cos = Math.abs(Math.cos(rad));
    const sin = Math.abs(Math.sin(rad));

    const boundingW = transformedW * cos + transformedH * sin;
    const boundingH = transformedW * sin + transformedH * cos;

    return { vw, vh, transformedW: boundingW, transformedH: boundingH };
  };

  const clampImagePosition = (animate = false) => {
    if (exiting) return { maxX: 0, maxY: 0 };
    const { vw, vh, transformedW, transformedH } = getImageRenderMetrics();
    const maxX = Math.max(0, (transformedW - vw) / 2);
    const maxY = Math.max(0, (transformedH - vh) / 2);

    let changed = false;

    const nextX = Math.min(maxX, Math.max(-maxX, imageX));
    if (nextX !== imageX) {
      imageX = nextX;
      changed = true;
    }

    const nextY = Math.min(maxY, Math.max(-maxY, imageY));
    if (nextY !== imageY) {
      imageY = nextY;
      changed = true;
    }

    if (maxX === 0 && imageX !== 0) {
      imageX = 0;
      changed = true;
    }

    if (maxY === 0 && imageY !== 0) {
      imageY = 0;
      changed = true;
    }

    if (changed) {
      if (animate) {
        img.style.transition = 'transform 0.35s cubic-bezier(0.2, 0, 0.2, 1)';
      }
      updateImageTransform();
      if (animate) {
        window.setTimeout(() => {
          if (!active) return;
          img.style.transition = 'transform 0.1s ease-out';
        }, 350);
      }
    }

    return { maxX, maxY };
  };

  /* ---------- 相邻图片预览（列表长度 > 1 时，左右拖动才会走到） ---------- */

  const getDisplayUrl = (src: string) => {
    if (src && !src.startsWith('data:') && !src.startsWith('blob:') && src.startsWith('http')) {
      return `/api/proxy-image?url=${encodeURIComponent(src)}`;
    }
    return src;
  };

  const createAdjacentImage = (delta: number) => {
    const nextIdx = modalImageIndex + delta;
    if (nextIdx < 0 || nextIdx >= modalImageList.length) return null;

    const node = document.createElement('img');
    node.className = 'adjacent-image';
    node.style.position = 'absolute';
    node.style.top = '50%';
    node.style.left = '50%';
    node.style.transform = `translate(calc(-50% + ${delta * window.innerWidth}px), -50%)`;
    // 源文件复制 modalImg.style.width / height —— Cylit 的这张图没有内联宽高，改用锁定值
    node.style.width = lockedW + 'px';
    node.style.height = lockedH + 'px';
    node.style.objectFit = 'contain';
    node.style.pointerEvents = 'none';
    node.style.zIndex = '5';
    node.style.transformOrigin = 'center center';
    node.style.willChange = 'transform';
    node.style.opacity = '0';
    node.style.transition = 'transform 0.18s ease-out, opacity 0.18s ease-out';

    const src = getDisplayUrl(modalImageList[nextIdx]);
    node.addEventListener('load', () => {
      lockImageElementDimension(node, 0);
      node.dataset.sizeReady = '1';
    }, { once: true });
    node.src = src;
    if (node.complete && node.naturalWidth > 0) {
      requestAnimationFrame(() => {
        lockImageElementDimension(node, 0);
        node.dataset.sizeReady = '1';
      });
    }
    stage.appendChild(node);
    return node;
  };

  const ensureAdjacentImageReady = (imgEl: HTMLImageElement | null, callback: (ok: boolean) => void) => {
    if (!imgEl) {
      callback(false);
      return;
    }

    if ((imgEl.naturalWidth || 0) > 0 && (imgEl.naturalHeight || 0) > 0) {
      lockImageElementDimension(imgEl, 0);
      imgEl.dataset.sizeReady = '1';
      callback(true);
      return;
    }

    const handleLoad = () => {
      lockImageElementDimension(imgEl, 0);
      imgEl.dataset.sizeReady = '1';
      callback(true);
    };

    const handleError = () => {
      callback(false);
    };

    imgEl.addEventListener('load', handleLoad, { once: true });
    imgEl.addEventListener('error', handleError, { once: true });
  };

  const clearAdjacentImages = () => {
    if (adjacentReleaseTimer) {
      clearTimeout(adjacentReleaseTimer);
      adjacentReleaseTimer = null;
    }
    if (nextImgEl) { nextImgEl.remove(); nextImgEl = null; }
    if (prevImgEl) { prevImgEl.remove(); prevImgEl = null; }
  };

  const getPreviewLaneSpacing = () => {
    const { transformedW } = getImageRenderMetrics();
    return Math.max(window.innerWidth * 1.02, transformedW + 48);
  };

  const getPreviewOccupancyRatio = () => {
    const laneSpacing = getPreviewLaneSpacing();
    if (!laneSpacing) return 0;
    return Math.min(1, Math.abs(imageX) / laneSpacing);
  };

  const CLOSE_DRAG_MAX_SCALE = 1.3;
  const CLOSE_SHRINK_THRESHOLD_RATIO = 0.14;
  const CLOSE_SHRINK_SCALE = 0.72;
  const PREVIEW_SWITCH_OVERFLOW_THRESHOLD = 8;
  const PREVIEW_SWITCH_EDGE_TOLERANCE = 6;
  const PREVIEW_DETACH_VERTICAL_THRESHOLD = 110;
  const PREVIEW_VERTICAL_UNLOCK_THRESHOLD = 40;
  const PREVIEW_SPRING_MS = 360;
  const PREVIEW_SPRING_EASE = 'cubic-bezier(0.2, 0.88, 0.24, 1.16)';
  const PREVIEW_SPRING_Y_SHIFT = 18;
  const PREVIEW_SPRING_CLEANUP_MS = 380;

  const buildFloatingImageTransform = (offsetX: number, offsetY = imageY, scale = imageScale, rotation = imageRotation) => {
    return `translate(calc(-50% + ${offsetX}px), calc(-50% + ${offsetY}px)) scale(${scale}) rotate(${rotation}deg)`;
  };

  const setDragBackdropProgress = (progress: number) => {
    const safe = Math.max(0, Math.min(1, Number(progress) || 0));
    const alpha = 0.9 - safe * 0.55;
    stage.style.transition = 'none';
    stage.style.backgroundColor = `rgba(0, 0, 0, ${alpha.toFixed(3)})`;
  };

  const resetDragBackdrop = () => {
    stage.style.transition = 'none';
    stage.style.backgroundColor = '';
  };

  const syncAdjacentImageBox = (el: HTMLImageElement | null) => {
    if (!el) return;
    if (el.dataset.sizeReady === '1') {
      lockImageElementDimension(el, 0);
      return;
    }
    el.style.width = lockedW + 'px';
    el.style.height = lockedH + 'px';
    el.style.maxWidth = 'none';
    el.style.maxHeight = 'none';
  };

  const setAdjacentDirection = (direction: number) => {
    if (direction < 0) {
      if (!prevImgEl) {
        prevImgEl = createAdjacentImage(-1);
        if (prevImgEl) prevImgEl.dataset.attachPending = '1';
      } else if (prevImgEl.dataset.detaching === '1') {
        if (adjacentReleaseTimer) {
          clearTimeout(adjacentReleaseTimer);
          adjacentReleaseTimer = null;
        }
        prevImgEl.dataset.detaching = '0';
        prevImgEl.dataset.attachPending = '1';
      }
      if (nextImgEl) {
        nextImgEl.remove();
        nextImgEl = null;
      }
      syncAdjacentImageBox(prevImgEl);
    } else if (direction > 0) {
      if (!nextImgEl) {
        nextImgEl = createAdjacentImage(1);
        if (nextImgEl) nextImgEl.dataset.attachPending = '1';
      } else if (nextImgEl.dataset.detaching === '1') {
        if (adjacentReleaseTimer) {
          clearTimeout(adjacentReleaseTimer);
          adjacentReleaseTimer = null;
        }
        nextImgEl.dataset.detaching = '0';
        nextImgEl.dataset.attachPending = '1';
      }
      if (prevImgEl) {
        prevImgEl.remove();
        prevImgEl = null;
      }
      syncAdjacentImageBox(nextImgEl);
    } else {
      clearAdjacentImages();
    }
  };

  const renderPreviewTransforms = (animate = false) => {
    const transition = animate
      ? `transform ${PREVIEW_SPRING_MS}ms ${PREVIEW_SPRING_EASE}, opacity ${PREVIEW_SPRING_MS}ms ease-out`
      : 'none';
    const laneSpacing = getPreviewLaneSpacing();
    const rawAbsX = Math.abs(imageX);
    const previewProgress = Math.min(1, Math.max(0, (rawAbsX - 10) / Math.max(1, laneSpacing * 0.32)));
    const previewOpacity = Math.pow(previewProgress, 0.78);
    const previewYOffset = PREVIEW_SPRING_Y_SHIFT * (1 - previewOpacity);

    img.style.transition = transition;

    if (prevImgEl) {
      const isAttachPending = prevImgEl.dataset.attachPending === '1';
      const nodeTransition = isAttachPending
        ? `transform ${PREVIEW_SPRING_MS}ms ${PREVIEW_SPRING_EASE}, opacity ${PREVIEW_SPRING_MS}ms ease-out`
        : transition;
      prevImgEl.style.transition = nodeTransition;
      prevImgEl.style.transform = buildFloatingImageTransform(imageX - laneSpacing, previewYOffset, 1, 0);
      prevImgEl.style.opacity = String(previewOpacity);
      if (isAttachPending) prevImgEl.dataset.attachPending = '0';
    }

    if (nextImgEl) {
      const isAttachPending = nextImgEl.dataset.attachPending === '1';
      const nodeTransition = isAttachPending
        ? `transform ${PREVIEW_SPRING_MS}ms ${PREVIEW_SPRING_EASE}, opacity ${PREVIEW_SPRING_MS}ms ease-out`
        : transition;
      nextImgEl.style.transition = nodeTransition;
      nextImgEl.style.transform = buildFloatingImageTransform(imageX + laneSpacing, previewYOffset, 1, 0);
      nextImgEl.style.opacity = String(previewOpacity);
      if (isAttachPending) nextImgEl.dataset.attachPending = '0';
    }

    updateImageTransform();
  };

  const animatePreviewBack = () => {
    const { vw, vh, transformedW, transformedH } = getImageRenderMetrics();
    const maxX = Math.max(0, (transformedW - vw) / 2);
    const maxY = Math.max(0, (transformedH - vh) / 2);
    imageX = Math.min(maxX, Math.max(-maxX, imageX));
    imageY = Math.min(maxY, Math.max(-maxY, imageY));
    dragPreviewActive = false;
    dragPreviewOverflowX = 0;
    img.style.transition = `transform ${PREVIEW_SPRING_MS}ms ${PREVIEW_SPRING_EASE}`;
    updateImageTransform();
    releaseAdjacentPreview();
    resetDragBackdrop();
    window.setTimeout(() => {
      if (!active) return;
      img.style.transition = 'transform 0.1s ease-out';
    }, PREVIEW_SPRING_CLEANUP_MS);
  };

  const releaseAdjacentPreview = () => {
    if (!prevImgEl && !nextImgEl) return;
    if (adjacentReleaseTimer) {
      clearTimeout(adjacentReleaseTimer);
      adjacentReleaseTimer = null;
    }

    const laneSpacing = getPreviewLaneSpacing();
    const releaseTransition = `transform ${PREVIEW_SPRING_MS}ms ${PREVIEW_SPRING_EASE}, opacity ${PREVIEW_SPRING_MS}ms ease-out`;

    if (prevImgEl) {
      prevImgEl.dataset.detaching = '1';
      prevImgEl.style.transition = releaseTransition;
      prevImgEl.style.transform = buildFloatingImageTransform(-laneSpacing * 1.08, PREVIEW_SPRING_Y_SHIFT, 1, 0);
      prevImgEl.style.opacity = '0';
    }

    if (nextImgEl) {
      nextImgEl.dataset.detaching = '1';
      nextImgEl.style.transition = releaseTransition;
      nextImgEl.style.transform = buildFloatingImageTransform(laneSpacing * 1.08, PREVIEW_SPRING_Y_SHIFT, 1, 0);
      nextImgEl.style.opacity = '0';
    }

    adjacentReleaseTimer = window.setTimeout(() => {
      adjacentReleaseTimer = null;
      clearAdjacentImages();
    }, PREVIEW_SPRING_CLEANUP_MS);
  };

  /* ---------- 切换图片（源文件模块级 switchModalImageByDelta） ---------- */

  const switchModalImageByDelta = (delta: number, options: {
    preserveState?: {
      x: number; y: number; scale: number; rotation: number;
      width: string; height: string; maxWidth: string; maxHeight: string;
    } | null;
    keepHiddenUntilLoad?: boolean;
    restoreTransitionAfterFinalize?: boolean;
    onFinalize?: () => void;
  } = {}) => {
    // 单图（列表长度 <= 1）没有相邻图可切，直接退出
    if (exiting) return false;
    if (!Array.isArray(modalImageList) || modalImageList.length <= 1) return false;
    if (modalImageSwitching) return false;
    const nextIndex = modalImageIndex + delta;
    if (nextIndex < 0 || nextIndex >= modalImageList.length) return false;

    if (!active) return false;

    modalImageIndex = nextIndex;
    const nextSrc = modalImageList[nextIndex];
    currentRawUrl = nextSrc;

    const preserveState = options && options.preserveState ? options.preserveState : null;
    const restoreTransitionAfterFinalize = options.restoreTransitionAfterFinalize !== false;
    if (preserveState) {
      if (typeof preserveState.scale === 'number') imageScale = preserveState.scale;
      if (typeof preserveState.rotation === 'number') imageRotation = preserveState.rotation;
      if (typeof preserveState.x === 'number') imageX = preserveState.x;
      if (typeof preserveState.y === 'number') imageY = preserveState.y;
      if (typeof preserveState.width === 'string') lockedW = parseFloat(preserveState.width) || lockedW;
      if (typeof preserveState.height === 'string') lockedH = parseFloat(preserveState.height) || lockedH;
    } else {
      imageScale = 1;
      imageRotation = 0;
      imageX = 0;
      imageY = 0;
    }

    const displayUrl = getDisplayUrl(nextSrc);

    modalImageSwitching = true;
    img.style.transition = 'none';
    if (!options.keepHiddenUntilLoad) {
      img.style.opacity = '1';
    }
    updateImageTransform();

    const finalizeSwitch = () => {
      img.onload = null;
      img.onerror = null;
      if (!preserveState) {
        lockImageDimension(0);
      } else if (opts.onSize) {
        // preserveState 的 lockedW/H 是从预览节点的 style 反推的，组件拿不到那个节点，这里主动回推一次
        opts.onSize(lockedW, lockedH);
      }
      img.style.transition = 'none';
      updateImageTransform();
      img.style.opacity = '1';

      // 核心修复：立即清除切换状态，允许后续手势
      modalImageSwitching = false;
      previewCommitInProgress = false;

      if (restoreTransitionAfterFinalize) {
        requestAnimationFrame(() => {
          if (!active) return;
          img.style.transition = 'transform 0.1s ease-out';
        });
      }
      if (typeof options.onFinalize === 'function') {
        options.onFinalize();
      }
      // 切换落定：把新下标播给订阅者（左右拖动切图的唯一对外信号；加载失败的分支不播）
      emitIndex(modalImageIndex);
    };

    img.onload = finalizeSwitch;
    img.onerror = () => {
      img.onload = null;
      img.onerror = null;
      modalImageSwitching = false;
      previewCommitInProgress = false;
      img.style.opacity = '1';
      if (typeof options.onFinalize === 'function') {
        options.onFinalize();
      }
    };
    img.src = displayUrl;
    if (img.complete) {
      finalizeSwitch();
    }

    return true;
  };

  const commitPreviewSwitch = (delta: number) => {
    const previewEl = delta > 0 ? nextImgEl : prevImgEl;
    if (!previewEl) return switchModalImageByDelta(delta);

    previewCommitInProgress = true;

    ensureAdjacentImageReady(previewEl, () => {
      if (!active) {
        previewCommitInProgress = false;
        clearAdjacentImages();
        return;
      }

      const laneSpacing = getPreviewLaneSpacing();
      const exitX = delta > 0 ? -laneSpacing : laneSpacing;

      // 重要：在这里再次计算精确的尺寸，以防 loading 过程中窗口变化或 dimension 还没有同步
      lockImageElementDimension(previewEl, 0);

      const preservedSize = {
        x: 0,
        y: 0,
        scale: 1,
        rotation: 0,
        width: previewEl.style.width,
        height: previewEl.style.height,
        maxWidth: 'none',
        maxHeight: 'none',
      };

      img.style.transition = 'transform 0.34s cubic-bezier(0.22, 0.61, 0.36, 1)';
      previewEl.style.transition = 'transform 0.34s cubic-bezier(0.22, 0.61, 0.36, 1), opacity 0.16s ease-out';
      previewEl.style.opacity = '1';
      img.style.transform = `translate(${exitX}px, ${imageY}px) scale(${imageScale}) rotate(${imageRotation}deg)`;
      previewEl.style.transform = buildFloatingImageTransform(0, 0, 1, 0);

      window.setTimeout(() => {
        if (!active) {
          previewCommitInProgress = false;
          clearAdjacentImages();
          return;
        }

        img.style.transition = 'none';
        img.style.opacity = '0';

        // 在调用底部切换函数前，将内部状态重置，防止因为异步加载导致的锁死
        previewCommitInProgress = false;

        switchModalImageByDelta(delta, {
          preserveState: preservedSize,
          keepHiddenUntilLoad: true,
          restoreTransitionAfterFinalize: false,
          onFinalize: () => {
            previewCommitInProgress = false;
            clearAdjacentImages();
            img.style.opacity = '1';
          },
        });
      }, 340);
    });
  };

  /* ---------- 角度解包（源文件 normalizeAngleDelta / normalizeAbsoluteAngle） ---------- */

  const normalizeAngleDelta = (delta: number) => {
    let next = delta;
    while (next > 180) next -= 360;
    while (next < -180) next += 360;
    return next;
  };

  const normalizeAbsoluteAngle = (angle: number) => {
    let next = angle;
    while (next > 180) next -= 360;
    while (next < -180) next += 360;
    return next;
  };

  const getDist = (t1: Pt, t2: Pt) => Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);
  const getAngle = (t1: Pt, t2: Pt) => Math.atan2(t2.clientY - t1.clientY, t2.clientX - t1.clientX) * 180 / Math.PI;

  /* ---------- 手势入口：一套状态机 + 两条输入通道 ----------
   * 状态机只认三样东西（GestureEvent）：当前所有活动指针、本次变化的指针、preventDefault。
   * touch 与 pointer 各自折成它再喂进来 —— 手势数学（1.25 灵敏度 / 捏合中不夹取 / 松手 0.35~0.42s
   * 归位 / 旋转吸附 90° / 下滑关闭阈值）因此只有下面这一份，没有任何一处针对鼠标另写。 */

  const handleTouchStart = (e: GestureEvent) => {
    if (!active || exiting) return;
    if (previewCommitInProgress || modalImageSwitching) {
      e.preventDefault();
      return;
    }
    moved = false;
    if (e.touches.length === 2) {
      isPinching = true;
      isDragging = false; // Disable dragging when pinching
      initialDist = getDist(e.touches[0], e.touches[1]);
      initialAngle = getAngle(e.touches[0], e.touches[1]);
      lastPinchAngle = initialAngle;
      accumulatedAngleDiff = 0;
      rotArmed = false;
      baseScale = imageScale;
      baseRotation = imageRotation;
      img.style.transition = 'none';

      // 记录捏合中心点相对于屏幕中心的初始坐标
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      initialMidX = (e.touches[0].clientX + e.touches[1].clientX) / 2 - vw / 2;
      initialMidY = (e.touches[0].clientY + e.touches[1].clientY) / 2 - vh / 2;
      baseImageX = imageX;
      baseImageY = imageY;

      e.preventDefault();
    } else if (e.touches.length === 1) {
      isDragging = true;
      isPinching = false;
      lastTouchX = e.touches[0].clientX;
      lastTouchY = e.touches[0].clientY;
      dragStartX = e.touches[0].clientX;
      dragStartY = e.touches[0].clientY;
      dragPreviewActive = false;
      dragPreviewOverflowX = 0;
      dragIntent = '';
      lastMoveTime = performance.now();
      dragDismissBaseScale = imageScale;
      clearAdjacentImages();
      resetDragBackdrop();
    }
  };

  const handleTouchMove = (e: GestureEvent) => {
    if (exiting) return;
    if (!active) return;
    if (isPinching && e.touches.length === 2) {
      e.preventDefault();
      moved = true;
      const dist = getDist(e.touches[0], e.touches[1]);
      const angle = getAngle(e.touches[0], e.touches[1]);

      // 灵敏度设置
      const sensitivity = SENS;
      const distRatio = initialDist > 0 ? (dist / initialDist) : 1;
      const newScale = Math.min(Math.max(0.2, baseScale * Math.pow(distRatio, sensitivity)), 12);

      // 旋转增量解包：跨越 ±180° 时避免出现大幅突跳导致的多次旋转
      const stepAngleDiff = normalizeAngleDelta(angle - lastPinchAngle);
      accumulatedAngleDiff += stepAngleDiff;
      lastPinchAngle = angle;

      /* 旋转死区：越过阈值之前不转；越过的那一下把累计量对齐到「刚好越过」，
         所以起转是从 0 连续长出来的，不会一次跳掉阈值那么多度。 */
      if (!rotArmed && Math.abs(accumulatedAngleDiff) >= ROT_DEAD_ZONE) {
        rotArmed = true;
        accumulatedAngleDiff -= Math.sign(accumulatedAngleDiff) * ROT_DEAD_ZONE;
      }
      /* 捏合过程中**不做角度归一化**：一旦在 ±180° 处折返，imageRotation 会突变 358°，
         同一帧的位移补偿拿这个突变去算，画面就是一次明显的跳动。角度连续增长，
         归一化留到松手吸附 90° 的时候做。 */
      const newRotation = rotArmed
        ? baseRotation + accumulatedAngleDiff * sensitivity
        : baseRotation;

      // 计算当前捏合中心点
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const currentMidX = (e.touches[0].clientX + e.touches[1].clientX) / 2 - vw / 2;
      const currentMidY = (e.touches[0].clientY + e.touches[1].clientY) / 2 - vh / 2;

      // 以手指为中心点进行缩放与旋转的位移补偿
      // 1. 计算初始中心点相对于图片偏移的向量
      const dx = initialMidX - baseImageX;
      const dy = initialMidY - baseImageY;

      // 2. 根据缩放比例和角度变化量旋转该向量
      const scaleRatio = newScale / baseScale;
      const deltaRotRad = (newRotation - baseRotation) * Math.PI / 180;
      const cos = Math.cos(deltaRotRad);
      const sin = Math.sin(deltaRotRad);

      const shiftedDx = (dx * cos - dy * sin) * scaleRatio;
      const shiftedDy = (dx * sin + dy * cos) * scaleRatio;

      // 3. 更新图片位置，使得图片上的同一点依然在当前捏合中心下方
      imageX = currentMidX - shiftedDx;
      imageY = currentMidY - shiftedDy;
      imageScale = newScale;
      imageRotation = newRotation;

      updateImageTransform();
      // 旋转/缩放过程中不做硬性边界夹取，避免高频重排导致卡顿与"卡住"
    } else if (isDragging && e.touches.length === 1) {
      moved = true;
      const touch = e.touches[0];
      const { vw, vh, transformedW, transformedH } = getImageRenderMetrics();
      const maxX = Math.max(0, (transformedW - vw) / 2);
      const maxY = Math.max(0, (transformedH - vh) / 2);

      const deltaX = touch.clientX - lastTouchX;
      const deltaY = touch.clientY - lastTouchY;
      const totalDx = touch.clientX - dragStartX;
      const totalDy = touch.clientY - dragStartY;
      lastMoveTime = performance.now();

      if (!dragIntent) {
        /* 意图判定放大到 24px：8px 太近，手指落下的抖动就足以把一次普通拖动判成"往下拽"。 */
        if (Math.abs(totalDx) > 24 || Math.abs(totalDy) > 24) {
          dragIntent = Math.abs(totalDx) > Math.abs(totalDy) * 1.05 ? 'horizontal' : 'vertical';
        }
      }

      const nextX = imageX + deltaX;
      const nextY = imageY + deltaY;
      const clampedX = Math.min(maxX, Math.max(-maxX, nextX));
      const clampedY = Math.min(maxY, Math.max(-maxY, nextY));
      const overflowX = nextX - clampedX;
      const overflowY = nextY - clampedY;
      const dismissScaleEligible = imageScale <= 1.04 || (imageScale <= CLOSE_DRAG_MAX_SCALE && maxY <= PREVIEW_SWITCH_EDGE_TOLERANCE);
      /* 下滑关闭的门槛（用户口径：略微下移不该关掉）：
         要么往下 32px 且明确是竖向，要么 40px 且竖向分量占绝对多数。 */
      const verticalDismissIntent = dismissScaleEligible && totalDy > 32 && Math.abs(totalDy) > Math.abs(totalDx) * 1.6;
      const verticalGestureActive = totalDy > 40 && Math.abs(totalDy) > Math.abs(totalDx) * 1.4;
      const dismissShrinkThreshold = vh * CLOSE_SHRINK_THRESHOLD_RATIO;
      const verticalModeLocked = dragIntent === 'vertical' && totalDy > PREVIEW_VERTICAL_UNLOCK_THRESHOLD;
      const previewOccupancy = getPreviewOccupancyRatio();
      // 如果预览图占比大于 40%，极大幅度增加解除吸附的 Y 轴阈值，防止误触
      const effectiveDetachThreshold = previewOccupancy > 0.4 ? PREVIEW_DETACH_VERTICAL_THRESHOLD * 2.5 : PREVIEW_DETACH_VERTICAL_THRESHOLD;
      const detachPreviewByDownDrag = dragPreviewActive && totalDy > effectiveDetachThreshold;
      const shouldHandleAsVerticalDismiss = dismissScaleEligible && (verticalModeLocked || verticalDismissIntent || verticalGestureActive || detachPreviewByDownDrag);

      if (shouldHandleAsVerticalDismiss) {
        dragIntent = 'vertical';
        if (dragPreviewActive) {
          releaseAdjacentPreview();
        }
        dragPreviewActive = false;
        dragPreviewOverflowX = 0;

        imageX = totalDx;
        imageY = Math.max(0, totalDy * 0.96);

        const shrinkTargetScale = Math.max(0.58, dragDismissBaseScale * CLOSE_SHRINK_SCALE);
        const shrinkRange = Math.max(dismissShrinkThreshold + 32, vh * 0.34);
        const shrinkProgress = Math.min(1, imageY / shrinkRange);

        img.style.transition = 'none';
        imageScale = dragDismissBaseScale + (shrinkTargetScale - dragDismissBaseScale) * shrinkProgress;
        setDragBackdropProgress(shrinkProgress);
        updateImageTransform();

        lastTouchX = touch.clientX;
        lastTouchY = touch.clientY;
        e.preventDefault();
        return;
      }

      setDragBackdropProgress(0);

      const horizontalPreviewIntent = dragPreviewActive || dragIntent === 'horizontal' || (Math.abs(totalDx) > 12 && Math.abs(totalDx) > Math.abs(totalDy) * 0.9);

      if (modalImageList.length > 1 && horizontalPreviewIntent) {
        const nearHorizontalEdge = maxX <= PREVIEW_SWITCH_EDGE_TOLERANCE || Math.abs(imageX) >= (maxX - PREVIEW_SWITCH_EDGE_TOLERANCE);
        const overflowEnough = Math.abs(overflowX) >= PREVIEW_SWITCH_OVERFLOW_THRESHOLD;
        const switchDelta = overflowX < 0 ? 1 : overflowX > 0 ? -1 : 0;
        const canPreview = switchDelta !== 0 && modalImageIndex + switchDelta >= 0 && modalImageIndex + switchDelta < modalImageList.length;
        const allowPreviewSwitch = imageScale <= 1.02 || (nearHorizontalEdge && overflowEnough);

        if (canPreview && allowPreviewSwitch) {
          dragIntent = 'horizontal';
          dragPreviewActive = true;
          dragPreviewOverflowX = overflowX;
          setAdjacentDirection(switchDelta > 0 ? 1 : -1);

          imageX = nextX;
          imageY = nextY;
          renderPreviewTransforms(false);

          lastTouchX = touch.clientX;
          lastTouchY = touch.clientY;
          e.preventDefault();
          return;
        }
      }

      if (dragPreviewActive) {
        dragPreviewActive = false;
        dragPreviewOverflowX = 0;
        releaseAdjacentPreview();
      }

      imageX = nextX;
      imageY = nextY;

      lastTouchX = touch.clientX;
      lastTouchY = touch.clientY;

      img.style.transition = 'none';
      updateImageTransform();
      e.preventDefault();
    }
  };

  const handleTouchEnd = (e: GestureEvent) => {
    if (exiting) return;
    if (!active) return;
    const winH = window.innerHeight;
    const winW = window.innerWidth;

    if (e.touches.length < 2) {
      if (isPinching) {
        isPinching = false;
        // 手动缩放极其小时直接关闭（源文件是 window.history.back()）
        if (imageScale < 0.45) {
          onClose();
          return;
        }

        /* 松手回正：只有转到"接近直角"时才吸附（偏差 <= 12°，视觉上几乎无感）。
           转了一大截（比如 30°）就保持用户转到的角度 —— 以前无条件吸回 90° 的整数倍，
           松手那一下会突然弹回去，看起来就是"跳变"。 */
        const snappedRotation = Math.round(imageRotation / 90) * 90;
        const rotationDelta = Math.abs(snappedRotation - imageRotation);
        const shouldSnap = rotationDelta <= 12;

        if (imageScale < 1) {
          settleImageToNaturalSize(shouldSnap ? snappedRotation : imageRotation, 420);
        } else if (shouldSnap && rotationDelta > 0) {
          // 转到了接近直角：轻微回正（几度，看不出来）
          imageRotation = snappedRotation;
          img.style.transition = 'transform 0.4s cubic-bezier(0.2, 0, 0.2, 1)';
          updateImageTransform();
          clampImagePosition(true);
        } else {
          // 没有旋转，或转了一大截：保持用户转到的角度，只把位移夹回边界
          clampImagePosition(true);
        }
      }
    }

    if (e.touches.length === 0) {
      if (isDragging) {
        isDragging = false;

        const endTouch = e.changedTouches && e.changedTouches[0] ? e.changedTouches[0] : null;
        const endX = endTouch ? endTouch.clientX : lastTouchX;
        const endY = endTouch ? endTouch.clientY : lastTouchY;
        const totalDx = endX - dragStartX;
        const totalDy = endY - dragStartY;
        const vw = window.innerWidth;

        if (dragPreviewActive) {
          const delta = dragPreviewOverflowX < 0 ? 1 : -1;
          const occupancyEnough = getPreviewOccupancyRatio() >= 0.25;
          dragPreviewActive = false;

          if (!modalImageSwitching && occupancyEnough) {
            dragPreviewOverflowX = 0;
            resetDragBackdrop();
            commitPreviewSwitch(delta);
            return;
          }

          dragPreviewOverflowX = 0;
          animatePreviewBack();
          return;
        }

        const { vh, transformedH, transformedW } = getImageRenderMetrics();
        const maxY = Math.max(0, (transformedH - vh) / 2);
        const maxX = Math.max(0, (transformedW - vw) / 2);
        const dismissScaleEligible = imageScale <= 1.04 || (imageScale <= CLOSE_DRAG_MAX_SCALE && maxY <= PREVIEW_SWITCH_EDGE_TOLERANCE);

        const verticalDismissReady = dismissScaleEligible && totalDy > winH * CLOSE_SHRINK_THRESHOLD_RATIO && totalDy > Math.abs(totalDx) * 1.15;
        /* 松手时的关闭判定：要拖到视口 1/4 以上才算"往下甩"，之前 12% 太容易触发 */
        const heavyMoveDown = dismissScaleEligible && imageY > winH * 0.24;
        const outOfBounds = Math.abs(imageX) > maxX || Math.abs(imageY) > maxY || imageScale < 1;
        const heavyMoveOut = Math.abs(imageX) > winW * 0.4 || Math.abs(imageY) > winH * 0.4;

        // 下滑退出判定：增加对放大状态的严格限制
        // 1. 如果图片未放大 (imageScale <= 1.05)，允许下滑退出
        // 2. 如果图片已放大但处于回弹区且下滑距离极大，也允许退出，但逻辑需更保守
        if (verticalDismissReady || heavyMoveDown) {
          onClose();
          return;
        }

        // 强制回弹判定
        if (heavyMoveOut || outOfBounds) {
          resetDragBackdrop();
          imageScale = Math.max(1, imageScale);
          imageY = maxY === 0 ? 0 : Math.min(maxY, Math.max(-maxY, imageY));
          imageX = maxX === 0 ? 0 : Math.min(maxX, Math.max(-maxX, imageX));

          const currentSnapped = Math.round(imageRotation / 90) * 90;
          const dragRotationDelta = Math.abs(currentSnapped - imageRotation);
          /* 同样只在接近直角时回正：大角度旋转保持原样，不然松手会突然弹回 */
          const dragShouldSnapRotation = dragRotationDelta <= 12;

          if (dragShouldSnapRotation) {
            settleImageToNaturalSize(currentSnapped, 420);
          } else {
            imageRotation = currentSnapped;
            img.style.transition = 'transform 0.4s cubic-bezier(0.2, 0, 0.2, 1)';
            updateImageTransform();
            window.setTimeout(() => {
              if (!active) return;
              img.style.transition = 'transform 0.1s ease-out';
            }, 400);
          }
        } else {
          resetDragBackdrop();
          clampImagePosition(true); // 仅微调 X 轴对齐
        }
      }
    }
  };

  const handleTouchCancel = () => {
    if (!active) return;
    isPinching = false;
    isDragging = false;
    dragPreviewActive = false;
    dragPreviewOverflowX = 0;
    previewCommitInProgress = false;
    clearAdjacentImages();
    resetDragBackdrop();
    imageRotation = normalizeAbsoluteAngle(imageRotation);
    clampImagePosition(true);
  };

  /* ---------- 通道 1：touch（原样折包，逻辑零改动） ---------- */

  const fromTouch = (e: TouchEvent): GestureEvent => ({
    touches: Array.from(e.touches || []),
    changedTouches: Array.from(e.changedTouches || []),
    preventDefault: () => e.preventDefault(),
  });

  /* 胶囊按钮等 UI 元素上的按下不算手势：既不该拖动图片，也不该让 stage 抢走指针捕获
     （捕获一开，click 的 target 就变成 stage，一次「点放大」会被当成「点了背景」直接关掉查看器）。 */
  const isUiTarget = (t: EventTarget | null) =>
    t instanceof Element && !!t.closest('.cap, [data-viewer-ui]');

  /** 按下的目标：点到背景才算「点背景关闭」的依据，见 isBackgroundPress */
  let backgroundPress = false;

  const onTouchStart = (e: TouchEvent) => {
    backgroundPress = e.target === stage;
    if (isUiTarget(e.target)) return;
    handleTouchStart(fromTouch(e));
  };
  const onTouchMove = (e: TouchEvent) => handleTouchMove(fromTouch(e));
  const onTouchEnd = (e: TouchEvent) => handleTouchEnd(fromTouch(e));
  const onTouchCancel = () => handleTouchCancel();

  /* ---------- 通道 2：pointer（桌面鼠标拖动） ----------
   * 鼠标没有 touches：自己维护活动指针集合，每次 move / up 生成"当前指针数组 + 本次变化的指针"，
   * 连 preventDefault 一起折成同一个 GestureEvent 交给上面那套状态机。
   * 触摸设备上 pointer 事件同样会派发，所以 pointerType === 'touch' 一律放行给通道 1，避免双份处理。 */

  const pointers = new Map<number, Pt>();
  const pointerList = (): Pt[] => Array.from(pointers.values());

  const fromPointer = (touches: Pt[], changed: Pt[], e: PointerEvent): GestureEvent => ({
    touches,
    changedTouches: changed,
    preventDefault: () => e.preventDefault(),
  });

  const onPointerDown = (e: PointerEvent) => {
    if (e.pointerType === 'touch') return; // 触摸只走通道 1
    if (e.button !== 0) return; // 只认鼠标左键
    if (!active) return;
    backgroundPress = e.target === stage;
    if (isUiTarget(e.target)) return; // 胶囊上按下：不拖图、不抢捕获
    pointers.set(e.pointerId, { clientX: e.clientX, clientY: e.clientY });
    // 捕获到 stage：拖出图片、甚至拖出窗口之后仍然收得到 move / up
    if (stage.setPointerCapture) {
      try { stage.setPointerCapture(e.pointerId); } catch { /* 拿不到捕获也能用，忽略 */ }
    }
    handleTouchStart(fromPointer(pointerList(), [], e));
  };

  const onPointerMove = (e: PointerEvent) => {
    if (e.pointerType === 'touch') return;
    if (!pointers.has(e.pointerId)) return;
    const point = { clientX: e.clientX, clientY: e.clientY };
    pointers.set(e.pointerId, point);
    handleTouchMove(fromPointer(pointerList(), [point], e));
  };

  const onPointerUp = (e: PointerEvent) => {
    if (e.pointerType === 'touch') return;
    if (!pointers.has(e.pointerId)) return;
    const point = { clientX: e.clientX, clientY: e.clientY };
    pointers.delete(e.pointerId);
    if (stage.releasePointerCapture) {
      try { stage.releasePointerCapture(e.pointerId); } catch { /* 忽略 */ }
    }
    // 剩余指针交给状态机：通常为空 → handleTouchEnd 走正常的"拖拽结束"分支
    handleTouchEnd(fromPointer(pointerList(), [point], e));
  };

  const onPointerCancel = (e: PointerEvent) => {
    if (e.pointerType === 'touch') return;
    if (!pointers.delete(e.pointerId)) return;
    if (stage.releasePointerCapture) {
      try { stage.releasePointerCapture(e.pointerId); } catch { /* 忽略 */ }
    }
    if (pointers.size === 0) handleTouchCancel();
  };

  /**
   * 滚轮 / 触控板缩放。
   *
   * 源文件是「一格 ±10%」，那是给鼠标滚轮用的：触控板双指捏合会在一秒内发出几十个 wheel 事件，
   * 每个都乘 10%，瞬间就顶到上限 —— 手感就是「灵敏度过高、一碰就飞」。这里改成按 delta 连续取值：
   * 触控板捏合（浏览器会带 ctrlKey）每单位只给 1%，鼠标滚轮一格（deltaY≈±100）约 12%。
   */
  const onWheel = (e: WheelEvent) => {
    if (!active) return;
    if (isUiTarget(e.target)) return;   // 胶囊上滚动不缩放
    e.preventDefault();
    const d = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
    const clamped = Math.max(-240, Math.min(240, d));
    const perUnit = e.ctrlKey ? 0.01 : 0.0012;

    // 以光标为锚做**增量**补偿：放大时光标下的那一点必须原地不动（否则内容会往视口中心跑）。
    // stage 是 position: fixed; inset: 0，所以 e.clientX / e.clientY 本身就是相对 stage 的坐标；
    // scale 与 rotate 都以图心为原点、彼此可交换，因此这个式子对任意 rotation 都成立。
    const oldScale = imageScale;
    const nextScale = Math.min(Math.max(0.6, oldScale * Math.exp(-clamped * perUnit)), 12);
    if (Math.abs(nextScale - oldScale) < 1e-6) return;

    const k = nextScale / oldScale;
    const vw = stage.clientWidth || window.innerWidth;
    const vh = stage.clientHeight || window.innerHeight;
    // 图心在视口中的绝对坐标（transform 的 translate 相对 .viewer 中心）
    const Px = vw / 2 + imageX;
    const Py = vh / 2 + imageY;
    imageX = e.clientX - k * (e.clientX - Px);
    imageY = e.clientY - k * (e.clientY - Py);
    imageScale = nextScale;

    updateImageTransform();
    clampImagePosition();   // 滚轮缩放没有「松手」，边界得当场夹一次
  };

  stage.addEventListener('touchstart', onTouchStart, { passive: false });
  stage.addEventListener('touchmove', onTouchMove, { passive: false });
  stage.addEventListener('touchend', onTouchEnd);
  stage.addEventListener('touchcancel', onTouchCancel, { passive: true });
  stage.addEventListener('pointerdown', onPointerDown);
  stage.addEventListener('pointermove', onPointerMove);
  stage.addEventListener('pointerup', onPointerUp);
  stage.addEventListener('pointercancel', onPointerCancel);
  stage.addEventListener('wheel', onWheel, { passive: false });

  /* ---------- 对外接口 ---------- */

  const setImage = (w: number, h: number) => {
    natSize = { w: w || 0, h: h || 0 };
    if (!natSize.w || !natSize.h) return;
    lockImageDimension(imageRotation);
    updateImageTransform();
  };

  const zoomBy = (factor: number, cx?: number, cy?: number) => {
    if (!active || !Number.isFinite(factor) || factor <= 0) return;
    const next = Math.min(Math.max(ZOOM_MIN, imageScale * factor), ZOOM_MAX);
    if (Math.abs(next - imageScale) < 1e-6) return;
    const k = next / imageScale;
    if (typeof cx === 'number' && typeof cy === 'number') {
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      imageX += (cx - (vw / 2 + imageX)) * (1 - k);
      imageY += (cy - (vh / 2 + imageY)) * (1 - k);
    }
    imageScale = next;
    img.style.transition = 'transform 0.35s cubic-bezier(0.2, 0, 0.2, 1)';
    updateImageTransform();
    clampImagePosition(true);
    window.setTimeout(() => {
      if (!active) return;
      img.style.transition = 'transform 0.1s ease-out';
    }, 350);
  };

  const reset = () => {
    exiting = false;
    settleImageToNaturalSize(0, 420);
  };

  /** 进入退出动画：先立标志（期间拒绝手势 / 归位 / 切图），再交给 animateTo 写几何 */
  const beginExit = (scale: number, x: number, y: number, ms = 260) => {
    exiting = true;
    animateTo(scale, x, y, ms);
  };

  /**
   * 退出动画：组件按"触发缩略图的矩形"算好目标几何后调这里，图片飞回它原来所在的位置。
   * 只负责写值 + 开过渡（与 settleImageToNaturalSize 同一套过渡的临时开关写法），
   * 不 clamp、也不重锁尺寸 —— 尺寸锁定会改 .sheet，退出动画期间改它只会抖。
   */
  const animateTo = (scale: number, x: number, y: number, ms = 260) => {
    imageScale = scale;
    imageX = x;
    imageY = y;
    imageRotation = 0;
    img.style.transition = `transform ${ms}ms cubic-bezier(0.22, 0.61, 0.36, 1)`;
    updateImageTransform();
    window.setTimeout(() => {
      if (!active) return;
      img.style.transition = 'transform 0.1s ease-out';
    }, ms);
  };

  /**
   * 写入相邻图列表与当前下标。四件事：
   * 1. 列表只留有效 url，空列表退回"当前这张的单图"，保证 modalImageList 永远非空；
   * 2. 清掉手势 / 切换 / 拖拽预览的所有中间态（否则上一张图的状态会漏进新图）；
   * 3. 缩放 / 位移 / 旋转归零，按 100% 重新锁定尺寸（组件的 .sheet 通过 onSize 同步）；
   * 4. 只在 url 真的变了时才写 img.src（挂载时组件已经把 :src 绑好了，重复赋值等于再请求一次）。
   */
  const setGallery = (list: GalleryEntry[], index: number) => {
    const urls: string[] = [];
    for (const item of list || []) {
      const url = item && item.url ? item.url : '';
      if (url) urls.push(url);
    }
    modalImageList = urls.length ? urls : [currentRawUrl || img.getAttribute('src') || ''];
    modalImageIndex = Math.min(Math.max(0, Math.floor(Number(index) || 0)), modalImageList.length - 1);

    isPinching = false;
    isDragging = false;
    dragPreviewActive = false;
    dragPreviewOverflowX = 0;
    dragIntent = '';
    previewCommitInProgress = false;
    modalImageSwitching = false;
    moved = false;
    clearAdjacentImages();
    resetDragBackdrop();

    imageScale = 1;
    imageX = 0;
    imageY = 0;
    imageRotation = 0;

    const nextSrc = modalImageList[modalImageIndex];
    img.style.transition = 'none';
    img.style.opacity = '1';
    if (nextSrc && nextSrc !== currentRawUrl) {
      currentRawUrl = nextSrc;
      img.src = getDisplayUrl(nextSrc);
    }

    lockImageDimension(0);
    updateImageTransform();

    requestAnimationFrame(() => {
      if (!active) return;
      img.style.transition = 'transform 0.1s ease-out';
    });
  };

  const destroy = () => {
    active = false;
    stage.removeEventListener('touchstart', onTouchStart);
    stage.removeEventListener('touchmove', onTouchMove);
    stage.removeEventListener('touchend', onTouchEnd);
    stage.removeEventListener('touchcancel', onTouchCancel);
    stage.removeEventListener('pointerdown', onPointerDown);
    stage.removeEventListener('pointermove', onPointerMove);
    stage.removeEventListener('pointerup', onPointerUp);
    stage.removeEventListener('pointercancel', onPointerCancel);
    stage.removeEventListener('wheel', onWheel);
    pointers.clear();
    if (adjacentReleaseTimer) {
      clearTimeout(adjacentReleaseTimer);
      adjacentReleaseTimer = null;
    }
    clearAdjacentImages();
    img.onload = null;
    img.onerror = null;
    img.style.transition = '';
    img.style.transform = '';
    stage.style.backgroundColor = '';
    listeners.length = 0;
    indexListeners.length = 0;
  };

  return {
    destroy,
    setImage,
    setGallery,
    zoomBy,
    reset,
    animateTo,
    beginExit,
    stepTo: (delta: number) => { switchModalImageByDelta(delta); },
    getScale: () => imageScale,
    getIndex: () => modalImageIndex,
    onChange: (cb: (scale: number) => void) => { listeners.push(cb); },
    onIndexChange: (cb: (index: number) => void) => { indexListeners.push(cb); },
    isGesturing: () => moved,
    isBackgroundPress: () => backgroundPress,
  };
}
