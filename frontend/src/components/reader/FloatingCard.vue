<script setup lang="ts">
/**
 * 轻量浮层（阅读器的**笔记**与 **AI 解释**共用，《修改计划》第五步）。
 *
 * 为什么要它：东侧原来那块编辑/解释用的是**居中大弹窗**（`Modal`）—— 它把整页盖住，
 * 用户看不见自己标的到底是哪一段，改完还得回想"我刚写的对应哪句话"。
 * 浮层要的是：**锚在高亮附近**（视线不用离开原文）、不遮挡大面积正文、观感与侧栏一致。
 *
 * 三条行为约定（用户口径）：
 * - **尺寸小**：默认 340px 宽、圆角 12px、`--shadow-flyout`，与侧栏卡片同一门语言；
 * - **贴边回拉**：先按锚点右下放，越出视口就翻到上方/左侧（`place()`），永远不会跑到屏幕外；
 * - **锚定**：`anchored=false`（默认）时由**父组件**在鼠标移开时关掉它（本组件只管画 + 提供按钮）；
 *   点那枚图钉切锚定，之后移开也不消失。
 *
 * 第四条（用户口径 2026-10-07）：**可以按住标题栏拖**。拖过之后就不再做"贴边回拉"了 ——
 * 用户把它挪到哪儿就是哪儿，内容变长/锚点变化都不许把它弹回去（`dragged`），
 * 直到下次打开才回到自动定位。
 * 标题栏上**不写 `title`**（用户口径 2026-10-08："移除浏览器悬浮 toast 内容"）：
 * "按住这里可以拖动浮层"这种说明交给光标形状（`cursor: grab`）就够，不必再弹一条浏览器提示。
 *
 * 第五条（用户口径 2026-10-13）：标题栏**之上**还有一条 14px 的独立拖动条（data-float-grab，
 * 中间一枚 36×4 的药丸），底部两个角换成**圆角小把手**（类 iOS）且卡片一打开就渲染 ——
 * 两者对笔记与解释浮层同时生效。
 *
 * 本组件**不持有业务状态**：谁开、开在哪、里面放什么，全由父组件给。
 */
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch, watchEffect } from 'vue';
import { clearDocSelection, lockFloatSelection, releaseFloatSelection } from '@/utils/floatDrag';
import AppIcon from '@/components/AppIcon.vue';

const props = withDefaults(defineProps<{
  open: boolean;
  /** 锚点（一般给高亮/选区的 `getBoundingClientRect()`）：浮层贴在它右下 */
  anchor?: { x: number; y: number; w?: number; h?: number } | null;
  title?: string;
  /** 锚定模式：移开不自动消失（笔记默认关、AI 解释默认开） */
  anchored?: boolean;
  width?: number;
  /** 浮层本体可滚的最大高度（内容多时内部滚，不把页面顶长） */
  maxHeight?: number;
  /** 记住用户摆过的位置（每张卡一个键：`note` / `explain`）。空 = 不记忆，每次都贴锚点 */
  persistKey?: string;
  /** 这一次打开**居中**（无视记忆与锚点）。用户口径 2026-11-21：笔记的「打开浮层」默认落在页面中间 */
  centerOnOpen?: boolean;
  /** 不要那枚手动的锚定按钮（用户口径 2026-11-22：锚定改为"点过浮层就自动锚定"，按钮位让给「定位」） */
  hideAnchor?: boolean;
  /** 这张浮层属于哪一类文字：非空时在外壳写行内 `--ts` = 分类全局倍数 × 本卡字号倍数。
   *  为什么要分类：正文 / 对话 / 笔记 / 侧栏 / 界面各有各的全局缩放，浮层得自己声明是哪一类，
   *  否则它只能继承**宿主页面**的倍数 —— 而宿主是谁完全看这张卡浮在哪。空 = 不写（保持原样继承）。 */
  textCat?: '' | 'body' | 'chat' | 'note' | 'side' | 'ui';
  /** 标题栏给 A− / A+ 两枚字号钮（只有笔记卡的**查看态**才传：编辑态那行已被色点 / ✓ 占满） */
  zoomable?: boolean;
  /** 这张卡现在是**焦点卡**（最上层那张）。不是的话底角指示器会**大幅减淡** ——
   *  多卡同屏时视觉重心只该有一处（用户口径 2026-11-28）。 */
  focused?: boolean;
  /** 标题文字的颜色（空 = 用默认）。笔记卡在**查看态**用它把「笔记」染成高亮色，
   *  于是标题行本身就是"这条笔记是哪一色"的指示器（用户口径 2026-11-28 九轮）。 */
  titleColor?: string;
}>(), { anchor: null, title: '', anchored: false, width: 340, maxHeight: 420, persistKey: '', centerOnOpen: false, hideAnchor: false, textCat: '', zoomable: false, focused: true, titleColor: '' });

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'toggle-anchor'): void;
}>();

const el = ref<HTMLElement | null>(null);
const pos = ref({ left: 0, top: 0 });

/** 贴边回拉：优先放锚点右下；右边越界就往左挪，下边越界就翻到锚点上方。
 *  **用户拖过之后就不再自动摆** —— 见 `dragged`。 */
async function place() {
  if (!props.open || dragged.value) return;
  await nextTick();
  const a = props.anchor;
  const box = el.value;
  if (!a || !box) return;
  const w = box.offsetWidth || props.width;
  const h = box.offsetHeight || 120;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let left = a.x - 8;
  if (left + w > vw - 10) left = Math.max(10, vw - w - 10);
  if (left < 10) left = 10;
  let top = a.y + (a.h ?? 0) + 8;
  if (top + h > vh - 12) top = Math.max(10, a.y - h - 8);   // 放不下就翻到上方
  pos.value = { left: Math.round(left), top: Math.round(top) };
}

/* ------------------------------ 拖动（按住标题栏） ------------------------------ */
/** 用户挪过它了：之后不再自动摆位（内容变长、锚点变化都不弹回去），下次打开才复位。 */
const dragged = ref(false);
const drag = reactive({ on: false, sx: 0, sy: 0, ox: 0, oy: 0, w: 0, h: 0 });
/* 抓手/标题带的按下态（`.dragging`）与角把手的按下态（`.is-on`）**直接挂 DOM**：
   绑在模板上会在按下与松手那一刻把整张卡片（含插槽里的编辑区）重渲染一遍，
   几十毫秒的尖峰正好吃掉抓手补间的头几帧 —— 表现就是「点抓手时的动画不够细腻」。 */
const grabEl = ref<HTMLElement | null>(null);
const headEl = ref<HTMLElement | null>(null);
const brEl = ref<HTMLElement | null>(null);
const blEl = ref<HTMLElement | null>(null);

function markDragging(on: boolean) {
  grabEl.value?.classList.toggle('dragging', on);
  headEl.value?.classList.toggle('dragging', on);
}
/** 手势期间的落点（**普通对象**，不是响应式）：每帧只写 DOM，松手才同步回 `pos` —— 整个手势零次重渲染。
 *  另外 `pointermove` 在一帧里可能来好几次（平板 120Hz），所以用 rAF 合并成每帧一次。 */
const live = { left: 0, top: 0 };
let moveRaf = 0;
let moveAt: { x: number; y: number } | null = null;

/* 手势期间**每帧**重写一遍几何（不是「只跟着 pointermove 写」）：
   · pointermove 一帧可能来好几次（平板 120Hz），逐条写等于让样式重算排队；
   · 更要紧的是**防被覆盖** —— 手势期间若父组件因为别的原因重渲染一次（比如回答还在流式输出），
     Vue 会按 vnode 里那份**旧的** pos 把 left/top 写回去。Vue 的补丁跑在微任务里、早于下一帧的 rAF，
     所以这里每帧再写一遍就把那一下盖回去，用户看不到任何跳动。 */
function startHeadLoop() {
  if (!moveRaf) moveRaf = requestAnimationFrame(headLoopTick);
}

function stopHeadLoop() {
  if (moveRaf) { cancelAnimationFrame(moveRaf); moveRaf = 0; }
}

function headLoopTick() {
  moveRaf = 0;
  if (!drag.on) return;
  applyHeadMove();
  moveRaf = requestAnimationFrame(headLoopTick);
}

/* 拖动/缩放期间锁住全文选区：PDF 文本层是真实 DOM（markdown 侧不是），
   指针扫过时浏览器会顺手起一个选区（用户报的「拖顶部时 PDF 里被选中」）。
   只改 body 的 user-select，松手立刻还原。 */
function lockSelect() { lockFloatSelection(true); }
function unlockSelect() { lockFloatSelection(false); }

function onHeadDown(ev: PointerEvent) {
  // 标题栏上的控件（图钉 / 关闭 / 色点 / 取色器）不参与拖动，否则点它们会顺手把卡片挪一下
  if ((ev.target as HTMLElement | null)?.closest?.('button, label, input')) return;
  // 只认左键：右键按在标题栏上不该起一次拖动（它还会顺手把编辑框里的选区清掉）
  if (ev.button !== 0) return;
  const head = ev.currentTarget as HTMLElement | null;
  lockSelect();
  drag.on = true;
  markDragging(true);
  drag.sx = ev.clientX; drag.sy = ev.clientY;
  drag.ox = pos.value.left; drag.oy = pos.value.top;
  const node = el.value;
  /* 尺寸**只量一次**：以前每次 pointermove 都读 offsetWidth/offsetHeight，等于每帧强制同步一次布局
     （底下还有整篇正文 / PDF 时尤其贵）—— 这是掉帧的另一半来源。 */
  drag.w = node?.offsetWidth || props.width;
  drag.h = node?.offsetHeight || 120;
  live.left = drag.ox; live.top = drag.oy;
  moveAt = { x: ev.clientX, y: ev.clientY };
  startHeadLoop();
  /* 合成事件（自检探针）没有真指针，捕获会抛 NotFoundError —— 拖不动的是"捕获"，
     位移本身不依赖它，所以这里吞掉就行。 */
  try { head?.setPointerCapture?.(ev.pointerId); } catch { /* 没有真指针 */ }
  ev.preventDefault();
}

function onHeadMove(ev: PointerEvent) {
  if (!drag.on) return;
  moveAt = { x: ev.clientX, y: ev.clientY };
  startHeadLoop();
}

/** 用**最新**的指针位置重算一遍并写 left/top（每帧一次，见 headLoopTick） */
function applyHeadMove() {
  const at = moveAt;
  if (!at) return;
  clearDocSelection();
  // 夹在视口里：至少留一条边能抓得住（不然拖出去就再也点不到关闭了）
  const left = Math.min(Math.max(drag.ox + (at.x - drag.sx), 8 - drag.w + 48), window.innerWidth - 48);
  const top = Math.min(Math.max(drag.oy + (at.y - drag.sy), 4), window.innerHeight - 32);
  live.left = Math.round(left); live.top = Math.round(top);
  const node = el.value;
  if (node) {
    node.style.left = live.left + 'px';
    node.style.top = live.top + 'px';
  }
  /* 只有真的挪动了才算「用户摆过了」（纯点一下标题栏不该把卡片钉在原地） */
  if (!dragged.value && (live.left !== drag.ox || live.top !== drag.oy)) dragged.value = true;
}

function onHeadUp(ev: PointerEvent) {
  if (!drag.on) return;
  stopHeadLoop();
  /* 快速一划时「最后一帧 + 松手」会挤在一起：补算一次，否则那一下被整条丢掉 */
  if (moveAt) applyHeadMove();
  moveAt = null;
  drag.on = false;
  /* 类名与闸门同步撤（`.dragging` 只影响抓手药丸、闸门只影响 PDF 文字层，没有文档级重算） */
  markDragging(false);
  unlockSelect();
  /* 拖动与落定**是同一种写法**（left/top）：没有 transform 要还原、没有合成层要拆，
     松手那一帧不需要重绘 —— 所以不会「拖完闪一下」（用户口径 2026-11-24）。 */
  const node = el.value;
  if (node) {
    node.style.left = live.left + 'px';
    node.style.top = live.top + 'px';
  }
  pos.value = { left: live.left, top: live.top };
  try { (ev.currentTarget as HTMLElement | null)?.releasePointerCapture?.(ev.pointerId); } catch { /* 同上 */ }
  savePos();
}

/* ------------------------------ 记住摆过的位置 ------------------------------
 * 用户口径 2026-11-21：笔记浮层每次都在右下角打开，而不是它上次被拖到的位置。
 * 根因是「每次打开都把 dragged 复位 + 重跑 place()」，没锚点时自然落到 fallbackXY。
 * 这里把**用户摆过的坐标**单独记下来（按 persistKey 分开存）：下次打开先恢复它，
 * 没摆过才走 place() 贴锚点 —— dragged 复位只管"这一轮要不要贴锚点"，与记忆互不影响。
 */
const posKey = () => (props.persistKey ? `cylit:float:${props.persistKey}` : '');

/* ------------------------------ 字号倍数（A− / A+） ------------------------------
 * 为什么只写一个 CSS 变量：缩放**只该改字号**，不许碰卡片的宽高与位置 —— 拖动/拉角的数学
 * 全是"按下那一刻的 px 快照 + 指针增量"，字体一变就重排，量出来的 offsetWidth 会跟着跳。
 * 所以倍数落成一个 CSS 变量，由**浏览器**去算字号（`.np-view` / `.np-edit` 读 `var(--ts, 1)`）：
 * 点按钮只改一个变量，手势路径与这次缩放都不产生组件重渲染，和拖动/拉角那套零重渲染写法一致。
 * ⚠️ 变量只挂在**正文区**（`.fc-body`）上，不挂外壳：挂外壳会把标题「笔记」和头部图标一起放大，
 * 而用户要的是"笔记正文变大，头一行原样"。 */
const F_MIN = 0.8;
const F_MAX = 1.5;
const F_STEP = 0.05;
const fontScale = ref(1);

/** 越界 / 非数字（老记录没这个字段，也可能被手改过）一律当 1 —— 读的那一侧必须自己兜住。 */
function clampF(v: unknown): number {
  if (typeof v !== 'number' || !Number.isFinite(v)) return 1;
  return Math.round(Math.min(F_MAX, Math.max(F_MIN, v)) * 100) / 100;
}

/** 点一下加减：**立刻落盘**。字号是单击行为，没有"手势结束"这个时机可以等。 */
function stepFont(dir: -1 | 1) {
  fontScale.value = clampF(fontScale.value + dir * F_STEP);
  savePos();
}

/* ------------------------------ 「更多」二级菜单 ------------------------------
 * 把「编辑 / 放大 / 缩小 / 还原」收进一枚 ⋯：四枚钮并排会把标题行挤满，而它们都是低频动作。
 * 菜单**挂在外壳里**而不是 Teleport 到 body —— 外壳故意不写 overflow:hidden（只有内层 .float-card 裁正文），
 * 所以菜单不会被裁；同时指针仍在卡片区域内，父组件的「鼠标移出就收卡片」不会被误触发。 */
const moreOpen = ref(false);
const moreTop = ref(0);
const moreLeft = ref(0);
const moreEl = ref<HTMLElement | null>(null);
const moreBtnEl = ref<HTMLElement | null>(null);

function closeMore() {
  moreOpen.value = false;
}

/** 打开后量一次真实尺寸再落位：下方放不下就翻到按钮上方（卡片可能正贴着屏幕底边）。 */
async function toggleMore() {
  if (moreOpen.value) { closeMore(); return; }
  moreOpen.value = true;
  await nextTick();
  const shell = el.value;
  const btn = moreBtnEl.value;
  const menu = moreEl.value;
  if (!shell || !btn || !menu) return;
  const sb = shell.getBoundingClientRect();
  const bb = btn.getBoundingClientRect();
  const mh = menu.offsetHeight;
  const mw = menu.offsetWidth;
  moreTop.value = Math.round(bb.bottom + 4 + mh > window.innerHeight - 8
    ? bb.top - sb.top - mh - 4
    : bb.bottom - sb.top + 4);
  // **居中**：菜单中线对准按钮中线（用户口径 2026-11-28 十轮）——
  // 原来是右对齐按钮右边缘，与换色菜单的居中一左一右，看着像两套东西。
  // 越出卡片就往内夹，但始终留在卡片里（外壳不裁，出界会飘在正文上）。
  const center = bb.left + bb.width / 2 - sb.left;
  const left = Math.min(Math.max(6, center - mw / 2), Math.max(6, sb.width - mw - 6));
  moreLeft.value = Math.round(left);
}

function onMoreDocDown(ev: PointerEvent) {
  const target = ev.target as HTMLElement | null;
  if (target?.closest?.('[data-fc-more], [data-fc-more-menu]')) return;
  closeMore();
}
function onMoreKey(ev: KeyboardEvent) {
  if (ev.key !== 'Escape') return;
  ev.stopPropagation();          // 别让全局 Esc（收浮层）顺手把整张卡也收了
  closeMore();
}
watch(moreOpen, (open) => {
  if (open) {
    document.addEventListener('pointerdown', onMoreDocDown, true);
    document.addEventListener('keydown', onMoreKey, true);
  } else {
    document.removeEventListener('pointerdown', onMoreDocDown, true);
    document.removeEventListener('keydown', onMoreKey, true);
  }
});

/** textCat 非空才写 `--ts`：解释浮层 / 对话浮窗不传，它们的继承链与以前一模一样。
 *  ⚠️ 行内 style 里只放这一个变量：外壳的 left/top/width/height 仍由 applyShellDom 直接写 DOM，
 *  Vue 的 patchStyle 只增删它自己那几个键，不会把命令式写进去的几何抹掉。 */
const bodyVars = computed(() => (props.textCat
  ? { '--ts': `calc(var(--ts-${props.textCat}, 1) * ${fontScale.value})` }
  : null));

type SavedBox = { left: number; top: number; w?: number; h?: number; s?: number; f?: number };

/** 读回上次摆过的位置**与尺寸**（老记录只有 left/top，w/h 缺失就按默认尺寸打开）。 */
function readSavedPos(): SavedBox | null {
  const key = posKey();
  if (!key) return null;
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const v = JSON.parse(raw) as { left?: unknown; top?: unknown; w?: unknown; h?: unknown; f?: unknown };
    if (typeof v.left !== 'number' || typeof v.top !== 'number') return null;
    const box: SavedBox = { left: v.left, top: v.top };
    if (typeof v.w === 'number' && v.w > 0) box.w = v.w;
    if (typeof v.h === 'number' && v.h > 0) box.h = v.h;
    const s = (v as { s?: unknown }).s;
    if (typeof s === 'number' && s > 0) box.s = s;
    /* 字号倍数也在这份记录里（每张卡各记各的）。读出来**先钳一次范围**：
       手改过 localStorage、或者以后收窄了区间，都不该让一张卡用 0.3 的字号打开。 */
    box.f = clampF(v.f);
    return box;
  } catch { return null; }
}

const bodyEl = ref<HTMLElement | null>(null);
let bodySaveTimer = 0;

/** 正文滚动：停手 250ms 才写一次（滚动本身没必要逐像素落盘）。 */
function onBodyScroll() {
  if (!props.persistKey || bodySaveTimer) return;
  bodySaveTimer = window.setTimeout(() => { bodySaveTimer = 0; savePos(); }, 250);
}

/** 把正文滚回上次那一行：内容高度是**逐帧撑开**的（图片 / 公式 / 展开态），所以补两拍再滚一次。 */
function restoreBodyScroll(y: number) {
  const apply = () => { const b = bodyEl.value; if (b) b.scrollTop = y; };
  void nextTick().then(() => {
    apply();
    requestAnimationFrame(apply);
    window.setTimeout(apply, 160);
  });
}

function savePos() {
  const key = posKey();
  if (!key) return;
  try {
    const box: SavedBox = { left: pos.value.left, top: pos.value.top };
    /* 用户拉过的大小一起记（用户口径 2026-11-26）：只记位置的话，下次打开又缩回按内容估的默认尺寸。
       没拉过时这两个还是 null，就不写进记录 —— 那种情况本来就该按内容自适应。 */
    if (sizeW.value !== null) box.w = sizeW.value;
    if (sizeH.value !== null) box.h = sizeH.value;
    /* 正文读到哪也一起记（用户口径 2026-11-26）：长笔记滚到一半，下次打开不该回到开头。 */
    const s = bodyEl.value?.scrollTop ?? 0;
    if (s > 0) box.s = Math.round(s);
    /* 字号倍数与位置 / 尺寸同一份记录（正好每个 persistKey 一张卡）：点击时立刻写，下次打开原样恢复。
       默认 1 不写 —— 没缩过的卡片不必多一个字段。 */
    if (fontScale.value !== 1) box.f = fontScale.value;
    localStorage.setItem(key, JSON.stringify(box));
  } catch { /* 隐私模式忽略 */ }
}

/** 恢复时按当前视口夹一次：窗口变小/换屏后不要飞到屏幕外（拖动时同一口径：至少留 48×32 在屏内） */
function clampToViewport(p: { left: number; top: number }): { left: number; top: number } {
  const box = el.value;
  const w = box?.offsetWidth || props.width;
  const left = Math.min(Math.max(p.left, 8 - w + 48), window.innerWidth - 48);
  const top = Math.min(Math.max(p.top, 4), window.innerHeight - 32);
  return { left: Math.round(left), top: Math.round(top) };
}

watch(() => [props.open, props.anchor?.x, props.anchor?.y, props.anchor?.h] as const,
  () => { void place(); }, { immediate: true });
watch(() => props.open, (v) => {
  if (!v) { closeMore(); return; }
  /* 尺寸也按卡记忆：上次拉大过的框，再打开还是那么大；没记录才回到调用方给的默认宽高。
     夹一次视口 —— 换到小屏上之后，记下来的大框不该整个超出屏幕。 */
  const saved = readSavedPos();
  /* 字号跟着同一份记录恢复（clampF 已在 readSavedPos 里钳过范围，坏数据就是 1）。
     必须在 centerOnOpen 那个提前 return **之前** —— 居中打开的卡片同样要带回自己的字号。 */
  fontScale.value = saved?.f ?? 1;
  sizeW.value = saved?.w ? Math.min(saved.w, window.innerWidth - 16) : null;
  sizeH.value = saved?.h ? Math.min(saved.h, window.innerHeight - 16) : null;
  if (props.centerOnOpen) {
    /* 「打开浮层」按钮：这次**直接摆到页面正中**（既不贴锚点、也不沿用上次拖到的角落） */
    dragged.value = true;
    void nextTick().then(() => {
      const box = el.value;
      const w = box?.offsetWidth || props.width;
      const h = box?.offsetHeight || 240;
      pos.value = clampToViewport({
        left: (window.innerWidth - w) / 2,
        top: (window.innerHeight - h) / 2,
      });
    });
    return;
  }
  if (saved?.s) restoreBodyScroll(saved.s);
  if (saved) {
    /* 摆过：沿用上次的坐标，并且**不再贴锚点**（dragged 同时挡住后面 anchor 变化的 place()） */
    dragged.value = true;
    pos.value = { left: saved.left, top: saved.top };
    /* 连补几拍**再落一次同一个坐标**：卡片打开的那几帧里，内容的尺寸还在变（图片 / 公式 /
     *  展开态），任何一次按内容算的落位（place / 夹取）都可能把它挪走一点点。用户看到的
     *  就是「位置不在原来那儿了」（用户口径 2026-11-26）。落三次的成本可以忽略。 */
    const settle = () => {
      if (!dragged.value) return;
      pos.value = clampToViewport({ left: saved.left, top: saved.top });
    };
    void nextTick().then(() => {
      settle();
      requestAnimationFrame(settle);
    });
    window.setTimeout(settle, 180);
    return;
  }
  dragged.value = false;          // 从没摆过：仍然贴锚点，保持初次体验
  void place();
});

/** 内容尺寸变了（切换查看/编辑、流式回答变长）也要重新夹一次 */
let ro: ResizeObserver | null = null;
watch(el, (node) => {
  ro?.disconnect();
  if (node && typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => { void place(); });
    ro.observe(node);
  }
});
onBeforeUnmount(() => {
  ro?.disconnect();
  releaseFloatSelection();
  document.removeEventListener('pointerdown', onMoreDocDown, true);
  document.removeEventListener('keydown', onMoreKey, true);
  if (bodySaveTimer) { window.clearTimeout(bodySaveTimer); bodySaveTimer = 0; }
});

/* -------------------------------- 右下角拖动放大 -------------------------------- */
/** 用户拉过大小后就不再每次打开都用默认尺寸（下次打开复位）。
 *  手柄**卡片一打开就给**（用户口径 2026-10-13 改了 2026-10-10 那条）：笔记卡默认不锚定，
 *  只在锚定态给手柄 = 默认看不见把手。拉一下大小本身就置 dragged（见 onResizeDown），
 *  所以不锚定也不会被 place() 弹回去。 */
const sizeW = ref<number | null>(null);
const sizeH = ref<number | null>(null);
const rs = reactive({ on: false, corner: 'br' as 'br' | 'bl', sx: 0, sy: 0, sw: 0, sh: 0, sl: 0, st: 0, vw: 0, vh: 0 });
/** 手势期间的尺寸（普通对象，与 `live` 同一手法）：每帧只写 DOM */
const rlive = { w: 0, h: 0, left: 0 };
let rsRaf = 0;
let rsAt: { x: number; y: number } | null = null;

function startResizeLoop() {
  if (!rsRaf) rsRaf = requestAnimationFrame(resizeLoopTick);
}

function stopResizeLoop() {
  if (rsRaf) { cancelAnimationFrame(rsRaf); rsRaf = 0; }
}

function resizeLoopTick() {
  rsRaf = 0;
  if (!rs.on) return;
  applyResizeMove();
  rsRaf = requestAnimationFrame(resizeLoopTick);
}

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

function onResizeDown(ev: PointerEvent, corner: 'br' | 'bl') {
  const box = el.value;
  if (!box) return;
  if (ev.button !== 0) return;      // 同上：只认左键
  lockSelect();
  rs.on = true;
  rs.corner = corner;
  (corner === 'br' ? brEl : blEl).value?.classList.add('is-on');
  rs.sx = ev.clientX; rs.sy = ev.clientY;
  rs.sw = box.offsetWidth; rs.sh = box.offsetHeight;
  rs.sl = pos.value.left; rs.st = pos.value.top;
  /* 视口也只量一次：每帧读 innerWidth/innerHeight 等于每帧问一次布局 */
  rs.vw = window.innerWidth; rs.vh = window.innerHeight;
  rlive.w = rs.sw; rlive.h = rs.sh; rlive.left = rs.sl;
  rsAt = { x: ev.clientX, y: ev.clientY };
  startResizeLoop();
  /* 先落一个 sizeH：`.sized` 当场生效（正文区改成纵向 flex），否则要等松手才切、内容跳一下 */
  sizeH.value = rs.sh;
  /* 拉了大小就算"用户自己摆过了"：否则 `ResizeObserver` 一量到尺寸变了就调 `place()`，
     把我们刚拉出来的宽度又夹回去（拉大时手感和打架一样）。 */
  dragged.value = true;
  // 合成事件（自检探针）没有真指针，捕获会抛 NotFoundError —— 与标题栏拖动同一处理
  try { (ev.currentTarget as HTMLElement | null)?.setPointerCapture?.(ev.pointerId); } catch { /* 没有真指针 */ }
  ev.preventDefault();
  ev.stopPropagation();
}

function onResizeMove(ev: PointerEvent) {
  if (!rs.on) return;
  rsAt = { x: ev.clientX, y: ev.clientY };
  startResizeLoop();
}

/** 用**最新**的指针位置重算一遍并写 DOM（每帧一次，见 resizeLoopTick） */
function applyResizeMove() {
  const at = rsAt;
  if (!at || !rs.on) return;
  /* 手势期间逐帧清选区（只在真有选区时才动）：抓手是 `user-select: none`，
     浏览器的锚点会跳到卡片正文上，拖一下就划出一片卡片自己的文字。 */
  clearDocSelection();
  const dx = at.x - rs.sx;
  const dy = at.y - rs.sy;
  /* ⚠️ 下限**不能**直接用 MIN_W / MIN_H：短笔记（比如只写了一行）初始高度是内容高，
     可能只有 60~120px。写死 160 的话，手指一碰下角，高度当场被夹到 160 ——
     用户看到的就是「拖动还没开始，卡片先自己长高一截」（用户口径 2026-11-25）。
     所以下限取 min(常量, 按下那一刻的真实尺寸)：绝不会在第 0 帧跳一下，
     而小卡本来也没有再缩的余地（已经比最小高度还小）。上限同理，
     否则卡片贴近屏幕底部时 maxH < 当前高度，第一帧反而会往回缩。 */
  const minW = Math.min(260, rs.sw);
  const minH = Math.min(160, rs.sh);
  const maxH = Math.max(minH, rs.vh - rs.st - 10);
  if (rs.corner === 'br') {
    const maxW = Math.max(minW, rs.vw - rs.sl - 10);
    rlive.w = Math.round(clamp(rs.sw + dx, minW, maxW));
    rlive.left = rs.sl;
  } else {
    /* 左下角：**右边界钉住不动**（人拉的是左边那条边）—— 宽度反向变化，`left` 跟着推。
       否则拉左角时卡片会整体往右跑，手感完全不对。 */
    const right = rs.sl + rs.sw;
    const w = Math.round(clamp(rs.sw - dx, minW, Math.max(minW, right - 8)));
    rlive.w = w;
    rlive.left = Math.round(right - w);
  }
  rlive.h = Math.round(clamp(rs.sh + dy, minH, maxH));
  const node = el.value;
  if (node) {
    node.style.left = rlive.left + 'px';
    node.style.width = rlive.w + 'px';
    node.style.height = rlive.h + 'px';
    /* max-height 必须一起写：外壳的 max-height 默认是 props.maxHeight，
       不覆盖的话拖高会被旧上限夹住，看起来就是"拖不动" */
    node.style.maxHeight = rlive.h + 'px';
  }
}

function onResizeUp(ev: PointerEvent) {
  if (!rs.on) return;
  stopResizeLoop();
  if (rsAt) applyResizeMove();
  rsAt = null;
  rs.on = false;
  brEl.value?.classList.remove('is-on');
  blEl.value?.classList.remove('is-on');
  unlockSelect();
  /* 同步回响应式状态：值与刚才写进 DOM 的一模一样，Vue 那次 patch 是空操作 */
  pos.value = { left: rlive.left, top: rs.st };
  sizeW.value = rlive.w;
  sizeH.value = rlive.h;
  try { (ev.currentTarget as HTMLElement | null)?.releasePointerCapture?.(ev.pointerId); } catch { /* 同上 */ }
  savePos();
}

/**
 * 外壳几何由这个 effect **直接写 DOM**，模板里不再绑 `:style`。
 *
 * 两个理由：模板绑定意味着 `pos` / `sizeW` / `sizeH` 一变整块组件就重渲染（手势里就是每帧一次），
 * 而完全不绑又会让手势期间偶发的重渲染按**旧值**把 left/top 写回去。写到 DOM 上两头都躲开了。
 * `flush: 'sync'` 是关键：打开、落位这些一次性变化必须在这一帧绘制前就落到样式上，不能等下一拍。
 */
function applyShellDom() {
  const node = el.value;
  if (!node) return;
  const h = sizeH.value;
  if (!drag.on && !rs.on) {
    node.style.left = pos.value.left + 'px';
    node.style.top = pos.value.top + 'px';
  }
  if (!rs.on) {
    node.style.width = (sizeW.value ?? props.width) + 'px';
    node.style.height = h === null ? '' : h + 'px';
    node.style.maxHeight = (h ?? props.maxHeight) + 'px';
  }
}
watchEffect(applyShellDom, { flush: 'sync' });
</script>

<template>
  <!-- 外层外壳（用户口径 2026-11-21 三轮）：fixed 与 left/top/width/height 都挂在它身上，
       **故意不写 overflow:hidden** —— 否则底角那两枚"悬浮在卡片外面"的指示器会被裁掉。
       内层 .float-card 才是带圆角 / 描边 / 裁切的那张卡（正文照旧被裁）。 -->
  <div
    v-if="props.open"
    ref="el"
    class="float-shell"
    :class="{ 'fc-unfocused': !props.focused }"
    data-float-card
  >
    <div class="float-card" :class="{ sized: sizeH !== null }">
      <!-- 顶部**浮起**的拖动条（用户口径 2026-10-13 二轮："自然融合，不要另起一行"）：
           position:absolute 贴在卡片顶端，中间一枚 34×4 的药丸，不占一行、不带底色。
           为什么要抓手：头一行挤着色点 / 铅笔 / 垃圾桶，真正"空白可抓"的地方太少。
           它和 .fc-head 走**同一套**处理器（叠加，不是替代）—— 标题栏照旧能拖。
           同样**不写 title**（用户口径 2026-10-08：不要浏览器悬浮提示），只靠光标形状说话。 -->
      <div
        class="fc-grab"
        ref="grabEl"
        data-float-grab
        @pointerdown="onHeadDown"
        @pointermove="onHeadMove"
        @pointerup="onHeadUp"
        @pointercancel="onHeadUp"
      >
        <span class="fc-grab-pill" />
      </div>

      <!-- ⚠️ 标题栏里可能有**可点的东西**（笔记框的色点 / 取色器）：
           `label` / `input` 也要排掉，否则点取色器会顺手把卡片拖一下。 -->
      <div
        class="fc-head"
        ref="headEl"
        data-float-head
        @pointerdown="onHeadDown"
        @pointermove="onHeadMove"
        @pointerup="onHeadUp"
        @pointercancel="onHeadUp"
      >
        <!-- 标题**前面**的图标位（2026-10-10：笔记框要照目标站放一枚笔记图标）。
             不传则为空，解释浮层不受影响。 -->
        <slot name="head-lead" />
        <span class="fc-title" :style="props.titleColor ? { color: props.titleColor } : undefined">{{ props.title }}</span>
        <!-- 标题**之后**、按钮之前的位置（2026-10-10 复查：笔记框的色条要跟「笔记」并排，
             但「笔记」和左边那枚图标必须在**最左侧** —— 所以色点排在标题右边，而不是它左边）。 -->
        <slot name="head-mid" />
        <span class="spacer" />
        <!-- 头部附加内容（2026-10-10：笔记框要照目标站把「✓ 保存」放在标题行上）。
             没传这个插槽时渲染为空，**解释浮层那边完全不受影响**。 -->
        <slot name="head-extra" />
        <!-- ⋯ 排在父组件的动作之后、✕ 之前：最右那枚永远是关闭，不该被它挤走。
             字号与「编辑」一起收进这里（用户口径 2026-11-26）—— 五枚钮并排必然把标题行挤成一团，
             而放大/缩小/还原/编辑都是低频动作。只有查看态的笔记卡要它（`zoomable`），
             别的浮层头部与以前完全一致；它们也没有字号状态，给了按钮也无从下手。 -->
        <button
          v-if="props.zoomable"
          ref="moreBtnEl"
          type="button"
          class="fc-btn"
          :class="{ on: moreOpen }"
          :aria-expanded="moreOpen"
          title="更多"
          data-fc-more
          @click="toggleMore"
        >
          <AppIcon name="more" :size="13" />
        </button>

        <button
          v-if="!props.hideAnchor"
          type="button"
          class="fc-btn"
          :class="{ on: props.anchored }"
          :title="props.anchored ? '已锚定：鼠标移开也不关' : '锚定（鼠标移开也不关）'"
          data-float-anchor
          @click="emit('toggle-anchor')"
        >
          <AppIcon name="locate" :size="13" />
        </button>
        <!-- 头部**最右**（锚定之后、✕ 之前）：笔记框的删除键放这里（用户口径 2026-11-20）——
             删除与「✓ 保存 / 锚定」之间隔开一小段，免得点删除时误触保存。
             ⚠️ 只有父组件传了才渲染：解释浮层不传，头部与以前完全一致。 -->
        <span v-if="$slots['head-tail']" class="fc-tail"><slot name="head-tail" /></span>
        <button type="button" class="fc-btn" title="关闭" data-float-close @click="emit('close')">
          <AppIcon name="close" :size="13" />
        </button>
      </div>

      <div ref="bodyEl" class="fc-body" :style="bodyVars" @scroll.passive="onBodyScroll">
        <slot />
      </div>

      <div v-if="$slots.footer" class="fc-foot">
        <slot name="footer" />
      </div>

    </div>

    <!-- 底角指示器（用户口径 2026-11-21 三轮）：**画在卡片外面**，由这层透明遮罩承载 ——
         `.fc-grips` 铺满外壳且 pointer-events:none（不挡卡片的任何交互），
         两枚把手各自 pointer-events:auto、并向外偏移 7px，弧线于是浮在圆角外沿（真正的 iOS 观感）。
         缩放数学仍是"按下时的尺寸快照 + 指针增量"，1:1 跟随指针；这 7px 的外偏移不参与换算。 -->
    <div class="fc-grips">
      <div
        class="fc-resize br"
        ref="brEl"
        data-float-resize
        aria-label="拖动调整大小"
        @pointerdown="onResizeDown($event, 'br')"
        @pointermove="onResizeMove"
        @pointerup="onResizeUp"
        @pointercancel="onResizeUp"
      >
        <!-- 45° 的一段弧，**两端是圆头**（`stroke-linecap: round`）—— CSS 边框只能画 90° 直角端点，
             所以从"整圈描边 + 扇形裁剪"换成一条 SVG path（用户口径 2026-11-28 十轮）。
             `vector-effect="non-scaling-stroke"`：描边宽度永远按 CSS 像素算，不随 viewBox 缩放。 -->
        <svg class="fc-grip" viewBox="0 0 100 100" aria-hidden="true" focusable="false">
          <path d="M92.5 67.6 A46 46 0 0 1 67.6 92.5" vector-effect="non-scaling-stroke" />
        </svg>
      </div>
      <div
        class="fc-resize bl"
        ref="blEl"
        data-float-resize-bl
        aria-label="拖动调整大小"
        @pointerdown="onResizeDown($event, 'bl')"
        @pointermove="onResizeMove"
        @pointerup="onResizeUp"
        @pointercancel="onResizeUp"
      >
        <svg class="fc-grip" viewBox="0 0 100 100" aria-hidden="true" focusable="false">
          <path d="M32.4 92.5 A46 46 0 0 1 7.5 67.6" vector-effect="non-scaling-stroke" />
        </svg>
      </div>
    </div>

    <!-- 二级菜单：**一排圆钮**（用户口径 2026-11-26 五轮："紧凑一点，编辑也用圆形按钮"）。
         左边是调用方塞进来的动作（笔记卡的「编辑」），右边是字号：缩小 | 百分比 | 放大。
         一排就够：四样东西都是"点一下"的图标动作，分两排只是把菜单撑高、还多一条分隔线。
         百分比留在中间常显 —— 只写在 :title 里等于要用户悬停才知道自己调到了哪儿。 -->
    <div
      v-if="moreOpen"
      ref="moreEl"
      class="fc-more"
      data-fc-more-menu
      role="menu"
      aria-label="更多操作"
      :style="{ top: moreTop + 'px', left: moreLeft + 'px' }"
    >
      <slot name="more-items" :close="closeMore" />
      <span v-if="$slots['more-items']" class="fc-more-vsep" aria-hidden="true" />
      <button
        type="button"
        class="fc-btn"
        role="menuitem"
        :disabled="fontScale <= F_MIN"
        :title="`缩小字号（当前 ${Math.round(fontScale * 100)}%）`"
        aria-label="缩小字号"
        data-fc-zoom-out
        @click="stepFont(-1)"
      >
        <AppIcon name="zoom-out" :size="13" />
      </button>
      <span
        class="fc-more-pct"
        :class="{ on: fontScale !== 1 }"
        :title="`当前字号 ${Math.round(fontScale * 100)}%`"
      >{{ Math.round(fontScale * 100) }}%</span>
      <button
        type="button"
        class="fc-btn"
        role="menuitem"
        :disabled="fontScale >= F_MAX"
        :title="`放大字号（当前 ${Math.round(fontScale * 100)}%）`"
        aria-label="放大字号"
        data-fc-zoom-in
        @click="stepFont(1)"
      >
        <AppIcon name="zoom-in" :size="13" />
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 观感与侧栏卡片同一门语言：同一个圆角档、同一门阴影、同一个边框色。
   阴影用 `--shadow-flyout`（比侧栏的 `--shadow-2` 重一档）—— 它是浮在正文上的，要能一眼看出"这是浮层"。
   ⚠️ 这里原来写的是 `--shadow-3`，而那个名字**从来没定义过**：声明被整条丢掉，等于一直没有阴影。 */
.float-shell {
  /* fixed 定位 + 尺寸都归外壳；它同时是底角指示器的定位上下文 */
  position: fixed; z-index: 60;
  display: flex; flex-direction: column;
  /* 把手静止时的描边色（两张浮层共用同一套，用户口径 2026-11-21）。
     ⚠️ 圆角变量 --fc-radius 在这里**故意不给默认值**：笔记卡要在 ReaderView 里覆盖成 16px，
     而 [data-note-popover] 与 .float-shell 是**同权重**的选择器（类/属性 + 作用域属性各一），
     谁赢只看样式注入顺序 —— 那等于把"笔记卡圆角悄悄回到 12px"做成一个偶发 bug。
     默认值改为写在使用处：var(--fc-radius, 12px)，两头都不必跟谁抢优先级。 */
  --fc-handle: color-mix(in srgb, var(--text) 22%, transparent);
}
.float-card {
  /* 吃满外壳：拉过高度外壳才有确定高度，卡片跟着长；没拉过时由内容撑 */
  position: relative;   /* 卡片顶端那条浮起的拖动条（.fc-grab）的定位上下文 */
  flex: 1 1 auto; min-height: 0;
  display: flex; flex-direction: column;
  /* 圆角读外壳上的 --fc-radius（默认 12、笔记卡 16）；底角那两道弧读的是**同一个值**，
     所以"卡片圆角改了、弧没跟着改"这件事从结构上就不可能再发生。 */
  border: 1px solid var(--border-strong); border-radius: var(--fc-radius, 12px);
  background: var(--bg-elev); box-shadow: var(--shadow-flyout);
  overflow: hidden;
}
/* 顶部拖动条（用户口径第二轮："最好能自然融合，而不是另起一行"）：
   **浮**在标题带顶端（absolute），不占一行高度、没有自己的底色 —— 药丸看起来就是标题带
   上沿的一枚抓手，而不是"多出来的一条"。标题带给的 padding-top 让按钮从 12px 才开始，
   所以这 12px 抓手区不会盖住标题栏上的按钮（拖动落空 = 拖标题带，行为不变）。
   光标 / 不可选中 / touch-action 与标题栏完全一致（同一套拖动处理器）。 */
.fc-grab {
  position: absolute; top: 0; left: 0; right: 0;
  display: flex; align-items: flex-start; justify-content: center;
  height: 12px; padding-top: 4px; z-index: 2;
  cursor: grab; user-select: none; touch-action: none;
}
.fc-grab.dragging { cursor: grabbing; }
/* 药丸：拖动中**变宽 + 变亮**（与底角那圈弧同一门「按下就给反馈」的语言）。
   只动 width 与背景色，**不动 transform**：本项目踩过「缩放 + 半透明描边 → 边缘反复重采样 → 闪」的坑，
   这一条同样不引入合成层位移。220ms 带轻微过冲，松手自然回落（状态一撤就是反向过渡）。 */
.fc-grab-pill {
  width: 34px; height: 4px; border-radius: 2px;
  background: color-mix(in srgb, var(--text) 19%, transparent);
  transition: width 220ms cubic-bezier(.22, 1.2, .36, 1),
              background-color 220ms var(--ease);
}
/* 抓手反馈与对话浮窗**完全同源**（用户口径 2026-11-24）：按下跑同一条三段关键帧，
   34 → 52 冲过头 → 43 回坐 → 46 停住；松手动画移除后由上面那条 transition 平滑送回 34。
   两边若各用一套（这边 transition、那边关键帧），观感就会"明显不一致"——这是上一轮的教训。 */
.fc-grab.dragging .fc-grab-pill,
.fc-grab:active .fc-grab-pill {
  /* 纯 transition（34→46 / 220ms / 回弹曲线过冲一次），与对话浮窗完全同一套。 */
  width: 46px;
  background: color-mix(in srgb, var(--text) 34%, transparent);
}

.fc-head {
  display: flex; align-items: center; gap: 4px; flex: none;
  /* 上 12px 留给浮在顶端的抓手药丸（见 .fc-grab）：按钮从 12px 才开始，不会被抓手盖住 */
  padding: 12px 8px 6px 12px;
  border-bottom: 1px solid var(--divider);
  background: color-mix(in srgb, var(--text) 3%, transparent);
  /* 标题栏就是"把手"：光标与不可选中都按拖动来给，免得拖的时候把标题拖蓝 */
  cursor: grab; user-select: none; touch-action: none;
}
.fc-head.dragging { cursor: grabbing; }
.fc-title { font-size: 11.5px; font-weight: 600; color: var(--text-dim); }
/* 头部图标钮：**圆形 + 中性淡底**（用户口径 2026-11-21："笔记浮层的按钮样式与对话样式统一"）。
   参考那套是"灰底圆钮"，不是"透明方角"也不是"强调色方块" —— 两张浮层（笔记 / Cyli 解释）
   共用这一条，所以它们的锚定 / 关闭 / 笔 / 垃圾桶才会长得一样。 */
.fc-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 25px; height: 25px; flex: none; border: 0; border-radius: 50%;
  background: color-mix(in srgb, var(--text) 6%, transparent); color: var(--text-dim);
  cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease),
              transform var(--dur-fast) var(--ease);
}
.fc-btn:hover { background: var(--bg-hover); color: var(--text); }
.fc-btn:active { transform: scale(.94); }
.fc-btn.on { background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); }
/* 字号钮到头了（0.8 / 1.5）：淡下去并撤掉悬停反馈 —— 悬停还变亮的话，"点不动"会像卡住。
   只管 disabled 的钮：头部其它键（锚定 / ✕ / 铅笔）永远不禁用，外观与以前一致。 */
.fc-btn:disabled { opacity: .4; cursor: default; }
.fc-btn:disabled:hover { background: color-mix(in srgb, var(--text) 6%, transparent); color: var(--text-dim); }
/* 头部最右那格（head-tail）：与锚定之间再多一小段间距 —— 删除键和「保存 / 锚定」不能挨着 */
/* 头部最右那格（删除键）。**只靠父级的 gap 与邻居分开**：这里再叠一个 margin-left，
   就变成"删除前 6px、删除后 6px"，与左边的铅笔/锚定不一致（用户口径 2026-11-21 报的间隔不匀）。
   `:empty` 那条是同一个问题的另一半：父组件即使只在**编辑态**才渲染里面的按钮，
   也照样会传这个插槽 —— 空 span 会作为 flex 子项吃掉两个 gap，查看态下 ✕ 就被推远了。 */
.fc-tail { display: inline-flex; align-items: center; gap: 2px; flex: none; }
.fc-tail:empty { display: none; }

/* 「更多」二级菜单：**一排圆钮**，坐标由 toggleMore 量好后写进行内 style。
   宽度按内容给（max-content）：一排固定的 25px 钮 + 读数，没有会被撑开的伸缩项 ——
   之前那版是"定宽 + flex:1 读数"，才需要一个 176px 的死尺寸。 */
.fc-more {
  position: absolute; z-index: 2;
  width: max-content;
  display: flex; align-items: center; gap: 2px;
  padding: 4px;
  background: var(--bg-elev);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-ctl);
  /* 比卡片那层 --shadow-flyout 轻一档：它只是贴着头部的小菜单，阴影太重会像另一张卡片浮上来 */
  box-shadow: var(--shadow-2);
}
/* 调用方塞进来的那枚（笔记卡的「编辑」）：插槽节点带的是父组件的 scope，
   本组件的 `.fc-btn` 到不了它们身上，只能从这一侧用 `:slotted` 复制同一套圆钮尺寸。 */
.fc-more :slotted(button) {
  display: inline-flex; align-items: center; justify-content: center;
  width: 25px; height: 25px; flex: none;
  border: 0; border-radius: 50%;
  background: color-mix(in srgb, var(--text) 6%, transparent);
  color: var(--text-dim);
  cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.fc-more :slotted(button):hover:not(:disabled) { background: var(--bg-hover); color: var(--text); }
/* 竖分隔线：动作（编辑）与字号是两件事，一排里靠一条 16px 的细线分开，比空一段距离更清楚 */
.fc-more-vsep { width: 1px; height: 16px; margin: 0 3px; background: var(--divider); flex: none; }
/* 读数：等宽数字，宽度留够三位（100%），加减时两枚钮不会左右抽动；
   偏离 100% 时转强调色，一眼看出这张卡被单独调过。 */
.fc-more-pct {
  min-width: 38px; flex: none;
  text-align: center;
  font-size: calc(11px * var(--ts, 1));
  font-variant-numeric: tabular-nums;
  color: var(--text-faint);
  white-space: nowrap;
  transition: color var(--dur-fast) var(--ease);
}
.fc-more-pct.on { color: var(--accent-text); font-weight: 600; }

.fc-body { flex: 1 1 auto; padding: 10px 12px; overflow: auto; min-height: 0;
  /* 卡片默认字号也要读 --ts：插槽里没自己写字号的段落（解释浮层的过程行等）才会跟着缩放 */
  font-size: calc(12.5px * var(--ts, 1)); }
/* 用户把卡片拉大之后（`sizeH` 有值 → `.sized`）正文区改成**纵向 flex**：
   这样里面那层"本该吃满"的内容（笔记卡的可编辑区）可以用 `flex: 1 1 auto` 长高，
   而不是拉大之后下面空出一大块点不动的白。没拉过尺寸时保持原样（高度随内容），
   免得默认尺寸的卡片被撑出一条空白。 */
.float-card.sized .fc-body { display: flex; flex-direction: column; }
/* ---- 透明遮罩层：底角指示器的载体（用户口径 2026-11-21 三轮）----
   铺满外壳（inset: 0）只为拿一个「以卡片盒子为坐标系」的定位上下文 —— 这样把手写负偏移
   就等于「落在卡片外面」。它自己 pointer-events: none，**卡片照常可点、可拖、可滚**，
   只有两枚把手各自重新打开事件。
   ⚠️ 这一层**没有底色、也没有阴影**：卡片外那一圈必须还是「什么都没有」，
   否则屏幕上会凭空多出一块灰（用户口径：卡片外面多一块灰）。 */
.fc-grips { position: absolute; inset: 0; pointer-events: none; }
/* ---- 底角指示器：浮在卡片**外侧**的一道细短弧 ----
   **三处同一套**：笔记卡 / Cyli 解释卡（这里）与对话浮窗（ReaderView 的
   `.reader-aside > .float-grip`）。⚠️ 数字照抄，改一处必须改另一处。
   颜色：静止 22% 的 --text（--fc-handle）；悬停 / 拖动（.is-on）升 --accent。
   粗细：静止 2.2px、悬停 2.9px、拖动 3.4px（关键帧 2.2 → 3.6 → 3.2 → 3.4）。
   浓淡：**不是焦点卡**（多卡同屏时在下面那张）降到 0.42 透明度，悬停 / 拖动回到 1 ——
     淡入淡出走 260ms 过渡，不是瞬间切换（用户口径 2026-11-28）。
   几何：一切都由 `--fc-gap`（弧离卡片边缘的距离，现为 5px）推出来，与卡片圆角**同心** ——
      · 热区 20×20，bottom 与 right/left = -gap → 落在卡片**外侧**；
      · 画的是**整圈**描边环（边长 = (圆角 + gap) × 2 + 描边），环心落在卡片圆角的圆心上，
        再用 `clip-path` 只留 60°（整圆的 1/6）扇形 —— 半径大、弧度缓、长度短。
        边长必须大于半径，否则 CSS 会按盒子尺寸把圆角**缩比夹小**
        （12px 与 16px 的卡会得到一样大的弧）；半径增量必须等于 gap，弧心才落在
        卡片圆角的圆心（cardRight - R, cardBottom - R）上。
   长度：就是卡片圆角那一段自然弧长（半径 = 圆角）。不再用 mask 去切 ——
        conic mask 与 transform 同时运动会触发离屏合成，用户报过闪烁，整条已移除。
   拖动反馈：**只加粗描边**（位置一动不动）。原来还有一条 push 位移关键帧把把手朝拖动方向
        translate 4px —— 那会让手按着的把手与真正跟手的卡片边缘错开 4px，
        手感就是「我拖了 4px，它才动 0px」，已整条删除（用户口径 2026-11-22）。
   只描外两条边，不填充、不带底色/阴影。
   ⚠️ 颜色不走 --border-strong / --line：token 表里**没有 --line**，写了等于整条声明被丢掉。 */
.fc-resize {
  --fc-gap: 5px;   /* 用户口径：7px 还嫌远，再收到 5px（弧贴着卡片外侧）。
                      ⚠️ 对话浮窗那两枚把手不读这个变量（坐标由 ReaderView 的
                      FLOAT_GRIP_GAP 在 JS 里算），改这里必须同步改那个常量。 */
  /* 外沿离卡片 `--fc-gap`；弧半径**不再**跟着 gap 涨（否则圆角越大那道弧越夸张）——
     半径恒等于卡片自己的圆角，弧是贴着圆角往外平移出来的平行弧。 */
  position: absolute; bottom: calc(-1 * var(--fc-gap));
  width: 20px; height: 20px;
  pointer-events: auto; touch-action: none; z-index: 2;
  transition: bottom var(--dur) var(--ease), right var(--dur) var(--ease),
              left var(--dur) var(--ease);
}
.fc-resize .fc-grip {
  position: absolute;
  /* SVG：viewBox 100，弧半径 46（留出圆头的余量）→ 元素边长 = 半径 / 0.46 = 半径 × 2.174 */
  --fc-grip-box: calc((var(--fc-radius, 12px) + var(--fc-gap)) * 2.1739);
  width: var(--fc-grip-box);
  height: var(--fc-grip-box);
  /* 弧心落在卡片圆角的圆心上：把手盒右下角 = 卡片角 + gap，往回退 gap + 圆角 - 半边 */
  right: calc(var(--fc-gap) + var(--fc-radius, 12px) - var(--fc-grip-box) / 2);
  bottom: calc(var(--fc-gap) + var(--fc-radius, 12px) - var(--fc-grip-box) / 2);
  overflow: visible;
  pointer-events: none;
  opacity: 1;
  /* 浓淡渐变 + 松手后的平滑回位（动画一摘，浏览器按这里从当前值过渡回去） */
  transition: opacity 280ms var(--ease),
              transform 520ms cubic-bezier(.33, 0, .2, 1);
}
.fc-resize .fc-grip path {
  fill: none;
  stroke: var(--fc-handle);
  stroke-width: 2.2px;
  /* **两端圆头**：CSS 边框画出来的弧只能是直角端点（用户口径 2026-11-28 十轮） */
  stroke-linecap: round;
  transition: stroke 200ms var(--ease),
              stroke-width 460ms cubic-bezier(.33, 0, .2, 1);
}
.fc-resize.br { right: calc(-1 * var(--fc-gap)); cursor: nwse-resize; }
/* 拖动中：把手命中区**不做缓动** —— 卡片边缘是 1:1 跟手的，把手若带 transition 会拖后一拍，
   读起来就是"我拖了，指示器慢半拍才跟到"（用户口径 2026-11-28 三轮）。 */
.fc-resize.is-on { transition: none; }
/* 左下角是**镜像**：弧走左下、着力点镜像，否则两个角看起来一样、放大方向也会反。
   弧本身由 SVG 的 path 决定形状，这里只需把它挪到左下角。 */
.fc-resize.bl { left: calc(-1 * var(--fc-gap)); cursor: nesw-resize; }
.fc-resize.bl .fc-grip {
  right: auto;
  left: calc(var(--fc-gap) + var(--fc-radius, 12px) - var(--fc-grip-box) / 2);
}
/* 不是焦点卡时底角指示器**大幅减淡**（用户口径 2026-11-28）：只剩常态的四分之一，
   小到"知道它在，但不抢注意力"；鼠标真移到角上（要拉了）或正在拖，立刻回到常态。 */
.fc-unfocused .fc-resize .fc-grip { opacity: .24; }
.fc-unfocused .fc-resize:hover .fc-grip,
.fc-unfocused .fc-resize.is-on .fc-grip { opacity: 1; }
.fc-resize:hover .fc-grip path { stroke: var(--accent); stroke-width: 2.9px; }
/* 拖动中：**只让弧自己顺着方向轻轻让一下 + 加粗**，命中区（把手盒）一动不动 ——
   上一版动的是把手本身，于是"卡片边缘 1:1 跟手、把手却被推走 4px"，手感成了"我拖了它没动"。
   ⚠️ 必须排在 :hover 之后：两者权重相同，胜负只看先后。 */
.fc-resize.is-on .fc-grip {
  animation: fc-grip-glide 380ms cubic-bezier(.25, 1.1, .35, 1) both;
}
.fc-resize.bl.is-on .fc-grip {
  animation: fc-grip-glide-bl 380ms cubic-bezier(.25, 1.1, .35, 1) both;
}
.fc-resize.is-on .fc-grip path {
  stroke: var(--accent);
  stroke-width: 3.6px;
  animation: fc-grip-thicken 340ms cubic-bezier(.25, 1.1, .35, 1) both;
}
/* 拖动时**描边加粗**的动画（用户口径：不要缩放弹簧，改成加粗动画）——
   只动 border-*-width 与颜色，不动 transform：缩放 + 圆角 + 半透明描边会让边缘反复重采样，
   那正是「闪烁」的来源。右下 / 左下要分开写，因为描边落在不同的两条边上。 */
/* 顺着拖动方向的弹性：**只作用在画出来的那道弧上**（::before），
   **不移动把手命中区** —— 上一版动的是把手本身，于是"卡片边缘 1:1 跟手、把手却被推走 4px"，
   手感就成了"我拖了它没动"。现在命中区纹丝不动，弧自己往外蹬一下再回来。 */
/* 拖动时的手感（用户口径 2026-11-28 三轮）：**出去就停住**，别在拖的过程中自己缩回来 ——
   上一版把 keyframes 尽头写成 0（想让它"有去有回"），结果手指还按着、弧先遛回原位，
   看着像指示器不跟手。回位改由**松手之后**的 transition 负责（见 `.fc-resize::before`）。
   节奏：380ms 一路让到 4px 再轻轻回落到 3.6px（末段那点回落就是"灵动"的来源），
   缓动 `cubic-bezier(.25, 1.1, .35, 1)`：起手快、末端软，像被手带出去一样。 */
@keyframes fc-grip-glide {
  0% { transform: translate(0, 0); }
  52% { transform: translate(4px, 4px); }
  78% { transform: translate(3.3px, 3.3px); }
  100% { transform: translate(3.6px, 3.6px); }
}
@keyframes fc-grip-glide-bl {
  0% { transform: translate(0, 0); }
  52% { transform: translate(-4px, 4px); }
  78% { transform: translate(-3.3px, 3.3px); }
  100% { transform: translate(-3.6px, 3.6px); }
}
/* 两个角只剩 glide 的方向不同 —— 加粗一套就够（动的是 SVG 的 stroke-width） */
@keyframes fc-grip-thicken {
  0% { stroke-width: 2.2px; }
  50% { stroke-width: 3.9px; }
  78% { stroke-width: 3.3px; }
  100% { stroke-width: 3.6px; }
}
/* 减少动效：关掉加粗 / 让位动画，颜色与粗细的静态反馈照旧 ——
   这样「按下 → 变粗变亮」在任何系统设置下都还在。 */
@media (prefers-reduced-motion: reduce) {
  .fc-resize .fc-grip, .fc-resize .fc-grip path { transition: none; transform: none; }
  .fc-resize:hover .fc-grip, .fc-resize.is-on .fc-grip { animation: none; transform: none; }
  .fc-grab-pill { transition: none; }
}
.fc-foot {
  display: flex; align-items: center; gap: 6px; flex: none;
  padding: 8px 12px; border-top: 1px solid var(--divider);
  background: var(--bg-layer);
}
</style>