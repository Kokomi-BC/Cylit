<script setup lang="ts">
/**
 * 大图查看器（毛玻璃胶囊 + 手写批注）。
 *
 * 三件事合在一个组件里，因为它们共用同一套几何：
 *   ① **看**：缩放（按钮 / 滚轮 / 双指）、拖动、旋转 90°、点背景或 Esc 关闭；
 *   ② **画**：手 / 画笔 / 荧光笔 / 自动选择 / 橡皮，粗细三档，撤销重做；
 *   ③ **存**：笔迹与图片笔记按 `(paper_id, 图片锚点)` 存一份文档，
 *      每次改动防抖自动保存（用户口径："默认保存"，没有保存按钮）。
 *
 * 坐标系（关键）：笔迹一律存**图片自身的归一化坐标**（0..1），见 `utils/imageInk.ts`。
 * 缩放、旋转、换设备都不会让笔迹跑位 —— 这里只负责把屏幕像素映射回归一化坐标。
 *
 * 胶囊版式：**顶部**放工具（画），**底部**放缩放（看）。底部顺序照参考图：
 * 放大 / 百分比 / 缩小 / 旋转 / 关闭。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { api } from '@/api/client';
import { reportError } from '@/stores/app';
import type { Block, ZoomTarget } from '@/api/types';
import {
  MARKER_COLOR, PEN_COLOR, WIDTH_STEPS, clamp01, cloneDoc, docKey, emptyDoc,
  hitStroke, paintDoc, paintStroke, pressureOf, snapToBlock,
  type ImageNote, type InkDoc, type InkStroke, type InkTool, type PageGeometry,
  type WidthKey,
} from '@/utils/imageInk';

const props = defineProps<{
  paperId: string;
  target: ZoomTarget | null;
  /** 这张图所在页的 PDF 点尺寸（自动选择字要用它把点换算成页面坐标） */
  pageWidth?: number | null;
  pageHeight?: number | null;
  /** 这张图在页面上的裁切区域（pt）；缺省 = 这张图就是整页 */
  region?: number[] | null;
  /** 该页的文字块（`block.bbox`，pt）：点到哪块就框哪块 */
  blocks?: Block[] | null;
}>();

const emit = defineEmits<{ (e: 'close'): void }>();

/* ------------------------------ 视口：缩放 / 平移 / 旋转 ------------------------------ */

const stage = ref<HTMLElement | null>(null);
const sheet = ref<HTMLElement | null>(null);
const imgEl = ref<HTMLImageElement | null>(null);
const penCv = ref<HTMLCanvasElement | null>(null);
const markerCv = ref<HTMLCanvasElement | null>(null);
const liveCv = ref<HTMLCanvasElement | null>(null);

/** 图片原始像素（只用来算宽高比 / 画布尺寸） */
const nat = ref({ w: 0, h: 0 });
/** 舞台可用尺寸 */
const box = ref({ w: 0, h: 0 });
const z = ref(1);
const tx = ref(0);
const ty = ref(0);
const rot = ref(0);

const Z_MIN = 0.6;
const Z_MAX = 6;
const Z_STEPS = [0.6, 0.8, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6];

/** 适应窗口的倍率：留出上下两条胶囊的空白；旋转 90° 时宽高互换 */
const fit = computed(() => {
  const b = box.value;
  const n = nat.value;
  if (!b.w || !b.h || !n.w || !n.h) return 1;
  const cw = Math.max(160, b.w - 120);
  const ch = Math.max(160, b.h - 200);
  return rot.value % 180 === 0
    ? Math.min(cw / n.w, ch / n.h)
    : Math.min(cw / n.h, ch / n.w);
});
const sheetW = computed(() => Math.max(1, nat.value.w * fit.value));
const sheetH = computed(() => Math.max(1, nat.value.h * fit.value));
const pct = computed(() => Math.round(z.value * 100));
const sheetStyle = computed(() => ({
  width: `${sheetW.value}px`,
  height: `${sheetH.value}px`,
  // 变换顺序：先转、再缩放、最后平移（CSS 从右往左算）—— 平移因此是**屏幕像素**
  transform: `translate3d(${tx.value}px, ${ty.value}px, 0) scale(${z.value}) rotate(${rot.value}deg)`,
}));

function clampPan() {
  const b = box.value;
  const wide = rot.value % 180 !== 0;
  const visW = (wide ? sheetH.value : sheetW.value) * z.value;
  const visH = (wide ? sheetW.value : sheetH.value) * z.value;
  const mx = Math.max(0, (visW - b.w) / 2);
  const my = Math.max(0, (visH - b.h) / 2);
  tx.value = Math.min(mx, Math.max(-mx, tx.value));
  ty.value = Math.min(my, Math.max(-my, ty.value));
}

function resetView() {
  z.value = 1;
  tx.value = 0;
  ty.value = 0;
}

/** 以某个屏幕点为锚缩放（滚轮 / 双指都走这里）：锚点下的画面不动 */
function zoomAt(next: number, px?: number, py?: number) {
  const el = stage.value;
  const nz = Math.min(Z_MAX, Math.max(Z_MIN, next));
  if (!el || Math.abs(nz - z.value) < 1e-4) return;
  const rect = el.getBoundingClientRect();
  const cx = px ?? rect.left + rect.width / 2;
  const cy = py ?? rect.top + rect.height / 2;
  const ax = rect.left + rect.width / 2 + tx.value;   // 图片中心在屏幕上的位置
  const ay = rect.top + rect.height / 2 + ty.value;
  const k = nz / z.value;
  tx.value += (cx - ax) * (1 - k);
  ty.value += (cy - ay) * (1 - k);
  z.value = nz;
  clampPan();
}

function zoomStep(dir: number) {
  const next = dir > 0
    ? Z_STEPS.find((s) => s > z.value + 1e-6) ?? Z_MAX
    : [...Z_STEPS].reverse().find((s) => s < z.value - 1e-6) ?? Z_MIN;
  zoomAt(next);
}

function rotate() {
  rot.value = (rot.value + 90) % 360;
  resetView();
}

function onWheel(ev: WheelEvent) {
  // 图片查看器里滚轮**就是缩放**（没有页面可滚），拦掉浏览器自己的滚动
  ev.preventDefault();
  zoomAt(z.value * (ev.deltaY < 0 ? 1.12 : 1 / 1.12), ev.clientX, ev.clientY);
}

/* ------------------------------ 画布：笔迹 ------------------------------ */

const tool = ref<InkTool>('hand');
const widthKey = ref<WidthKey>('mid');
const doc = ref<InkDoc>(emptyDoc());
const past = ref<InkDoc[]>([]);
const future = ref<InkDoc[]>([]);
const loading = ref(false);
const saving = ref(false);

const drawMode = computed(() => tool.value === 'pen' || tool.value === 'marker' || tool.value === 'eraser');

function dpr(): number {
  return Math.min(2, (typeof window !== 'undefined' && window.devicePixelRatio) || 1);
}

function sizeCanvases() {
  const ratio = dpr();
  const w = Math.max(1, Math.round(sheetW.value * ratio));
  const h = Math.max(1, Math.round(sheetH.value * ratio));
  for (const cv of [penCv.value, markerCv.value, liveCv.value]) {
    if (!cv) continue;
    cv.width = w;
    cv.height = h;
    cv.style.width = `${sheetW.value}px`;
    cv.style.height = `${sheetH.value}px`;
  }
  repaint();
}

function clearCv(cv: HTMLCanvasElement | null) {
  const ctx = cv?.getContext('2d');
  if (!cv || !ctx) return null;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.scale(dpr(), dpr());
  return ctx;
}

/** 整份文档重画：普通笔与荧光笔分两层（荧光笔要 CSS `mix-blend-mode: multiply` 才像马克笔） */
function repaint() {
  const pen = clearCv(penCv.value);
  const marker = clearCv(markerCv.value);
  clearCv(liveCv.value);
  const w = sheetW.value;
  const h = sheetH.value;
  if (pen) paintDoc(pen, { ...doc.value, strokes: doc.value.strokes.filter((s) => s.tool === 'pen') }, w, h);
  if (marker) paintDoc(marker, { ...doc.value, strokes: doc.value.strokes.filter((s) => s.tool === 'highlighter') }, w, h);
}

/** 正在画的那一笔单独一层：免得每动一下就重画整场（荧光笔重画还会闪） */
function paintLive(stroke: InkStroke | null) {
  const ctx = clearCv(liveCv.value);
  if (!ctx || !stroke) return;
  paintStroke(ctx, stroke, sheetW.value, sheetH.value);
}

/* ------------------------------ 自动保存 ------------------------------ */

const key = computed(() => (props.target?.url ? docKey(props.target.url) : ''));
let saveTimer: ReturnType<typeof setTimeout> | null = null;
let dirty = false;
let chain: Promise<void> = Promise.resolve();

function scheduleSave() {
  if (!key.value || !props.paperId) return;
  dirty = true;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { void flush(); }, 700);
}

async function flush() {
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  if (!dirty || !key.value || !props.paperId) return;
  const payload = cloneDoc(doc.value);
  const paperId = props.paperId;
  const anchor = key.value;
  dirty = false;
  saving.value = true;
  // 串行：两次保存并发的话，先到的可能是旧文档，后到的被覆盖 → 画的东西凭空少一笔
  chain = chain.then(async () => {
    try {
      await api.put('/annotations/image-marks', { paper_id: paperId, key: anchor, doc: payload });
    } catch (err) {
      dirty = true;
      reportError(err, '批注保存失败');
    } finally {
      saving.value = false;
    }
  });
  await chain;
}

/* ------------------------------ 文档读写 ------------------------------ */

function sanitize(raw: any): InkDoc {
  const strokes = Array.isArray(raw?.strokes)
    ? raw.strokes.filter((s: any) => s && Array.isArray(s.pts) && s.pts.length >= 2)
      .map((s: any) => ({
        id: String(s.id ?? Math.random().toString(36).slice(2)),
        tool: s.tool === 'highlighter' ? 'highlighter' : 'pen',
        color: typeof s.color === 'string' ? s.color : PEN_COLOR,
        w: Number(s.w) || 0.004,
        pts: s.pts.map((p: any) => [clamp01(Number(p?.[0])), clamp01(Number(p?.[1])),
          p?.length > 2 ? clamp01(Number(p[2])) : undefined].filter((v: any, i: number) => i < 2 || v !== undefined)),
      }))
    : [];
  const notes = Array.isArray(raw?.notes)
    ? raw.notes.filter((n: any) => n && String(n.text || '').trim() && Array.isArray(n.rect) && n.rect.length >= 4)
      .map((n: any) => ({
        id: String(n.id ?? Math.random().toString(36).slice(2)),
        rect: n.rect.slice(0, 4).map((v: any) => clamp01(Number(v) || 0)) as ImageNote['rect'],
        text: String(n.text),
        block_id: n.block_id ?? null,
        at: n.at ?? null,
      }))
    : [];
  return { v: 1, strokes, notes };
}

async function loadDoc() {
  doc.value = emptyDoc();
  past.value = [];
  future.value = [];
  if (!key.value || !props.paperId) { repaint(); return; }
  loading.value = true;
  try {
    const res = await api.get<{ doc?: InkDoc }>('/annotations/image-marks',
      { paper_id: props.paperId, key: key.value });
    doc.value = sanitize(res?.doc);
  } catch (err) {
    reportError(err, '批注读取失败');
    doc.value = emptyDoc();
  } finally {
    loading.value = false;
    repaint();
  }
}

/** 一次改动 = 一张撤销快照（笔迹与笔记共用同一条撤销栈，用户只认"上一步"） */
function commit(next: InkDoc) {
  past.value.push(cloneDoc(doc.value));
  if (past.value.length > 60) past.value.shift();
  future.value = [];
  doc.value = next;
  repaint();
  scheduleSave();
}

function undo() {
  const prev = past.value.pop();
  if (!prev) return;
  future.value.push(cloneDoc(doc.value));
  doc.value = prev;
  repaint();
  scheduleSave();
}

function redo() {
  const next = future.value.pop();
  if (!next) return;
  past.value.push(cloneDoc(doc.value));
  doc.value = next;
  repaint();
  scheduleSave();
}

/* ------------------------------ 指针：绘制 / 擦除 / 平移 / 缩放 ------------------------------ */

const pointers = new Map<number, { x: number; y: number }>();
let panFrom: { x: number; y: number; tx: number; ty: number } | null = null;
let drawing: { stroke: InkStroke; lastAt: number; lastX: number; lastY: number } | null = null;
let pinchFrom: { dist: number; z: number } | null = null;
let erasing = false;

/** 屏幕点 → 图片归一化坐标（旋转了 90° 也能算对：先反着转回去） */
function toImage(clientX: number, clientY: number): { x: number; y: number } {
  const el = sheet.value;
  if (!el) return { x: 0, y: 0 };
  const rect = el.getBoundingClientRect();
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;
  const dx = clientX - cx;
  const dy = clientY - cy;
  const rad = (-rot.value * Math.PI) / 180;
  const ux = dx * Math.cos(rad) - dy * Math.sin(rad);
  const uy = dx * Math.sin(rad) + dy * Math.cos(rad);
  const w = el.clientWidth * z.value || 1;
  const h = el.clientHeight * z.value || 1;
  return { x: clamp01(ux / w + 0.5), y: clamp01(uy / h + 0.5) };
}

function pointPressure(ev: PointerEvent, dx: number, dy: number): number {
  const now = ev.timeStamp || performance.now();
  const dt = drawing ? Math.max(1, now - drawing.lastAt) : 16;
  const speed = Math.hypot(dx, dy) / dt;
  return pressureOf(ev.pressure, speed, ev.pointerType === 'pen');
}

function currentWidth(kind: 'pen' | 'highlighter'): number {
  const step = WIDTH_STEPS.find((s) => s.key === widthKey.value) ?? WIDTH_STEPS[1];
  return kind === 'highlighter' ? step.marker : step.pen;
}

function beginStroke(ev: PointerEvent, kind: 'pen' | 'highlighter') {
  const pt = toImage(ev.clientX, ev.clientY);
  const stroke: InkStroke = {
    id: Math.random().toString(36).slice(2, 10),
    tool: kind,
    color: kind === 'highlighter' ? MARKER_COLOR : PEN_COLOR,
    w: currentWidth(kind),
    pts: [[pt.x, pt.y, pressureOf(ev.pressure, 0.9, ev.pointerType === 'pen')]],
  };
  drawing = { stroke, lastAt: ev.timeStamp || performance.now(), lastX: ev.clientX, lastY: ev.clientY };
  paintLive(stroke);
}

function extendStroke(ev: PointerEvent) {
  if (!drawing) return;
  const dx = ev.clientX - drawing.lastX;
  const dy = ev.clientY - drawing.lastY;
  // 1.1px 以内当抖动丢掉：触摸屏的"静止手抖"会攒出几百个重复点，白白变粗
  if (Math.hypot(dx, dy) < 1.1) return;
  const pt = toImage(ev.clientX, ev.clientY);
  drawing.stroke.pts.push([pt.x, pt.y, pointPressure(ev, dx, dy)]);
  drawing.lastAt = ev.timeStamp || performance.now();
  drawing.lastX = ev.clientX;
  drawing.lastY = ev.clientY;
  paintLive(drawing.stroke);
}

function endStroke() {
  const active = drawing;
  drawing = null;
  paintLive(null);
  if (!active || active.stroke.pts.length < 2) return;
  commit({ ...doc.value, strokes: [...doc.value.strokes, active.stroke] });
}

function eraseAt(ev: PointerEvent) {
  const pt = toImage(ev.clientX, ev.clientY);
  const el = sheet.value;
  if (!el) return;
  const radius = 14 / Math.max(0.2, z.value);
  const idx = hitStroke(doc.value, pt.x, pt.y, radius, el.clientWidth, el.clientHeight);
  if (idx < 0) return;
  const next = { ...doc.value, strokes: doc.value.strokes.filter((_, i) => i !== idx) };
  commit(next);
}

function onPointerDown(ev: PointerEvent) {
  if (ev.button !== undefined && ev.button > 0) return;
  (ev.currentTarget as HTMLElement).setPointerCapture?.(ev.pointerId);
  pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });

  if (pointers.size >= 2) {
    // 两指落下 = 双指缩放，正在画的那一笔**丢掉**（Apple 便签也是这个口径）
    drawing = null;
    paintLive(null);
    panFrom = null;
    erasing = false;
    const [a, b] = [...pointers.values()];
    pinchFrom = { dist: Math.max(24, Math.hypot(a.x - b.x, a.y - b.y)), z: z.value };
    return;
  }

  // 手写笔落笔时**不需要先切工具**：笔尖就是画笔（平板上最常用的一条捷径）
  const active = tool.value === 'hand' && ev.pointerType === 'pen' ? 'pen' : tool.value;

  if (active === 'pen' || active === 'marker') {
    beginStroke(ev, active === 'marker' ? 'highlighter' : 'pen');
    return;
  }
  if (active === 'eraser') {
    erasing = true;
    eraseAt(ev);
    return;
  }
  if (active === 'select') {
    selectAt(ev);
    return;
  }
  panFrom = { x: ev.clientX, y: ev.clientY, tx: tx.value, ty: ty.value };
}

function onPointerMove(ev: PointerEvent) {
  if (!pointers.has(ev.pointerId)) return;
  pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });

  if (pointers.size >= 2 && pinchFrom) {
    const [a, b] = [...pointers.values()];
    const dist = Math.max(24, Math.hypot(a.x - b.x, a.y - b.y));
    const midX = (a.x + b.x) / 2;
    const midY = (a.y + b.y) / 2;
    zoomAt(pinchFrom.z * (dist / pinchFrom.dist), midX, midY);
    return;
  }
  if (drawing) { extendStroke(ev); return; }
  if (erasing) { eraseAt(ev); return; }
  if (panFrom) {
    tx.value = panFrom.tx + (ev.clientX - panFrom.x);
    ty.value = panFrom.ty + (ev.clientY - panFrom.y);
    clampPan();
  }
}

function onPointerUp(ev: PointerEvent) {
  pointers.delete(ev.pointerId);
  if (pointers.size < 2) pinchFrom = null;
  if (drawing) endStroke();
  erasing = false;
  panFrom = null;
}

function onDoubleClick(ev: MouseEvent) {
  if (tool.value !== 'hand') return;
  zoomAt(z.value > 1.05 ? 1 : 2, ev.clientX, ev.clientY);
}

/* ------------------------------ 自动选择字 + 图片笔记 ------------------------------ */

const geometry = computed<PageGeometry | null>(() => {
  const w = Number(props.pageWidth) || 0;
  const h = Number(props.pageHeight) || 0;
  if (!w || !h) return null;
  const region = props.region && props.region.length >= 4
    ? [Number(props.region[0]), Number(props.region[1]),
      Number(props.region[2]), Number(props.region[3])] as [number, number, number, number]
    : null;
  return { widthPt: w, heightPt: h, region };
});

const pending = ref<{ rect: [number, number, number, number]; blockId: string | null } | null>(null);
/** 选中之后**先问一句**（用户口径："收到点击后再选择是否添加笔记"），点「添加笔记」才给输入框 */
const drafting = ref(false);
const draft = ref('');
const editing = ref<ImageNote | null>(null);
const editText = ref('');

function selectAt(ev: PointerEvent) {
  const pt = toImage(ev.clientX, ev.clientY);
  const geo = geometry.value;
  if (!geo) {
    // 没有页面几何（纯插图、Markdown 里的图）：退化成"点一处打点"，同样能挂笔记
    pending.value = {
      rect: [clamp01(pt.x - 0.02), clamp01(pt.y - 0.02), clamp01(pt.x + 0.02), clamp01(pt.y + 0.02)],
      blockId: null,
    };
  } else {
    const snap = snapToBlock(pt.x, pt.y, geo, props.blocks);
    pending.value = { rect: snap.rect, blockId: snap.blockId };
  }
  draft.value = '';
  drafting.value = false;
  editing.value = null;
}

function cancelSelect() {
  pending.value = null;
  drafting.value = false;
  draft.value = '';
}

function savePending() {
  const sel = pending.value;
  const text = draft.value.trim();
  if (!sel || !text) { cancelSelect(); return; }
  const note: ImageNote = {
    id: Math.random().toString(36).slice(2, 10),
    rect: sel.rect,
    text,
    block_id: sel.blockId,
    at: new Date().toISOString(),
  };
  commit({ ...doc.value, notes: [...doc.value.notes, note] });
  cancelSelect();
}

function openNote(note: ImageNote) {
  pending.value = null;
  drafting.value = false;
  editing.value = note;
  editText.value = note.text;
}

function saveNoteEdit() {
  const note = editing.value;
  if (!note) return;
  const text = editText.value.trim();
  if (!text) { deleteNote(note); return; }
  commit({
    ...doc.value,
    notes: doc.value.notes.map((n) => (n.id === note.id ? { ...n, text } : n)),
  });
  editing.value = null;
}

function deleteNote(note: ImageNote) {
  commit({ ...doc.value, notes: doc.value.notes.filter((n) => n.id !== note.id) });
  editing.value = null;
}

/** 框在屏幕上的四条边（%）：给条子/笔记卡定位用（都挂在被缩放的图层里，故用百分比） */
function rectBox(rect: [number, number, number, number]) {
  return {
    left: `${rect[0] * 100}%`,
    top: `${rect[1] * 100}%`,
    width: `${Math.max(0.01, rect[2] - rect[0]) * 100}%`,
    height: `${Math.max(0.01, rect[3] - rect[1]) * 100}%`,
  };
}

/* ------------------------------ 生命周期 ------------------------------ */

let ro: ResizeObserver | null = null;

function measure() {
  const el = stage.value;
  if (!el) return;
  box.value = { w: el.clientWidth, h: el.clientHeight };
  clampPan();
}

function onImgLoad() {
  const img = imgEl.value;
  if (!img) return;
  nat.value = { w: img.naturalWidth || 1, h: img.naturalHeight || 1 };
  measure();
  sizeCanvases();
}

watch([sheetW, sheetH], () => { if (nat.value.w) sizeCanvases(); });

watch(() => [props.paperId, props.target?.url], () => {
  resetView();
  nat.value = { w: 0, h: 0 };
  doc.value = emptyDoc();
  past.value = [];
  future.value = [];
  pending.value = null;
  drafting.value = false;
  editing.value = null;
  void loadDoc();
});

function close() {
  void flush();
  emit('close');
}

function onKey(ev: KeyboardEvent) {
  if (ev.key === 'Escape') {
    if (editing.value) { editing.value = null; return; }
    if (pending.value) { cancelSelect(); return; }
    close();
    return;
  }
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'z') {
    ev.preventDefault();
    if (ev.shiftKey) redo(); else undo();
  }
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'y') {
    ev.preventDefault();
    redo();
  }
}

onMounted(async () => {
  measure();
  document.addEventListener('keydown', onKey);
  if (typeof ResizeObserver !== 'undefined' && stage.value) {
    ro = new ResizeObserver(() => measure());
    ro.observe(stage.value);
  }
  await loadDoc();
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKey);
  ro?.disconnect();
  ro = null;
  void flush();
});

const TOOLS: Array<{ key: InkTool; icon: string; label: string }> = [
  { key: 'hand', icon: 'hand', label: '拖动 / 缩放' },
  { key: 'pen', icon: 'pen', label: '画笔' },
  { key: 'marker', icon: 'marker', label: '荧光笔' },
  { key: 'select', icon: 'text-select', label: '自动选择文字（再决定是否加笔记）' },
  { key: 'eraser', icon: 'eraser', label: '橡皮（整笔擦除）' },
];

function pickTool(next: InkTool) {
  tool.value = next;
  pending.value = null;
  drafting.value = false;
  editing.value = null;
}

const dotSize = (key: WidthKey) => (key === 'thin' ? 5 : key === 'mid' ? 7 : 9);
const invScale = computed(() => 1 / Math.max(0.2, z.value));
</script>

<template>
  <Teleport to="body">
    <div ref="stage" class="viewer" @wheel.prevent="onWheel">
      <div class="sheet" ref="sheet" :style="sheetStyle">
        <img
          ref="imgEl"
          class="shot"
          :src="target?.url || ''"
          :alt="target?.caption || '插图'"
          draggable="false"
          @load="onImgLoad"
        >
        <!-- 三层画布：普通笔 / 荧光笔（multiply）/ 正在画的一笔 -->
        <canvas ref="penCv" class="ink" />
        <canvas ref="markerCv" class="ink ink-marker" />
        <canvas ref="liveCv" class="ink ink-live" :class="{ 'ink-marker': tool === 'marker' }" />
        <div
          class="capture"
          :class="{ 'cursor-draw': drawMode, 'cursor-erase': tool === 'eraser' }"
          @pointerdown="onPointerDown"
          @pointermove="onPointerMove"
          @pointerup="onPointerUp"
          @pointercancel="onPointerUp"
          @dblclick="onDoubleClick"
        />

        <!-- 已选中的框 + 「添加笔记」条 -->
        <div v-if="pending" class="sel" :style="rectBox(pending.rect)" />
        <div
          v-if="pending"
          class="note-bar"
          :style="{ left: `${(pending.rect[0] + pending.rect[2]) / 2 * 100}%`, top: `${pending.rect[1] * 100}%`, transform: `translate(-50%, -100%) scale(${invScale})` }"
          @pointerdown.stop
        >
          <template v-if="drafting">
            <button class="nb primary" @click="savePending">保存</button>
          </template>
          <template v-else>
            <button class="nb primary" @click="drafting = true">添加笔记</button>
          </template>
          <button class="nb" @click="cancelSelect">取消</button>
        </div>
        <div
          v-if="pending && drafting"
          class="note-edit"
          :style="{ left: `${(pending.rect[0] + pending.rect[2]) / 2 * 100}%`, top: `${pending.rect[3] * 100}%`, transform: `translate(-50%, 0) scale(${invScale})` }"
          @pointerdown.stop
        >
          <textarea v-model="draft" class="note-input" rows="3" placeholder="写点什么…（自动保存）" />
        </div>

        <!-- 已存的笔记：图上的小圆点，点开是卡片 -->
        <button
          v-for="note in doc.notes"
          :key="note.id"
          class="note-dot"
          :style="{ left: `${note.rect[2] * 100}%`, top: `${note.rect[1] * 100}%`, transform: `translate(-50%, -50%) scale(${invScale})` }"
          :title="note.text"
          @pointerdown.stop
          @click.stop="openNote(note)"
        >
          <AppIcon name="note" :size="12" />
        </button>
        <div
          v-if="editing"
          class="note-card"
          :style="{ left: `${(editing.rect[0] + editing.rect[2]) / 2 * 100}%`, top: `${editing.rect[3] * 100}%`, transform: `translate(-50%, 8px) scale(${invScale})` }"
          @pointerdown.stop
        >
          <textarea v-model="editText" class="note-input" rows="3" />
          <div class="note-actions">
            <button class="nb" @click="deleteNote(editing)">删除</button>
            <span class="spacer" />
            <button class="nb" @click="editing = null">取消</button>
            <button class="nb primary" @click="saveNoteEdit">保存</button>
          </div>
        </div>
      </div>

      <!-- 顶部胶囊：工具 -->
      <div class="cap cap-tools">
        <button
          v-for="item in TOOLS"
          :key="item.key"
          class="tbtn"
          :class="{ on: tool === item.key }"
          :title="item.label"
          @click="pickTool(item.key)"
        >
          <AppIcon :name="item.icon" :size="18" />
        </button>
        <span class="cap-sep" />
        <button
          v-for="step in WIDTH_STEPS"
          :key="step.key"
          class="wbtn"
          :class="{ on: widthKey === step.key }"
          :title="`笔迹粗细：${step.label}`"
          @click="widthKey = step.key"
        >
          <i class="wdot" :style="{ width: `${dotSize(step.key)}px`, height: `${dotSize(step.key)}px` }" />
        </button>
        <template v-if="drawMode">
          <span class="cap-sep" />
          <button class="tbtn" :disabled="!past.length" title="撤销" @click="undo">
            <AppIcon name="undo" :size="18" />
          </button>
          <button class="tbtn" :disabled="!future.length" title="重做" @click="redo">
            <AppIcon name="redo" :size="18" />
          </button>
        </template>
      </div>

      <!-- 底部胶囊：缩放（与参考图一致：放大 / 百分比 / 缩小 / 旋转 / 关闭） -->
      <div class="cap cap-zoom">
        <button class="zbtn" title="放大" @click="zoomStep(1)">
          <AppIcon name="zoom-in" :size="18" />
        </button>
        <button class="zpct" title="恢复 100%（适应窗口）" @click="resetView">{{ pct }}%</button>
        <button class="zbtn" title="缩小" @click="zoomStep(-1)">
          <AppIcon name="zoom-out" :size="18" />
        </button>
        <button class="zbtn" title="旋转 90°" @click="rotate">
          <AppIcon name="rotate" :size="18" />
        </button>
        <button class="zbtn zclose" title="关闭" @click="close">
          <AppIcon name="close" :size="18" />
        </button>
      </div>

      <div v-if="target?.caption" class="cap-caption">{{ target.caption }}</div>
      <div v-if="loading || saving" class="cap-status">{{ loading ? '读取批注…' : '保存中…' }}</div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 整屏：**深底**（参考图是黑底居中看图）+ 进入动画。图片本身保持白底（论文页/插图是白的） */
.viewer {
  position: fixed; inset: 0; z-index: 240; overflow: hidden;
  display: flex; align-items: center; justify-content: center;
  background: #0d0f13;
  touch-action: none; user-select: none; -webkit-user-select: none;
  animation: viewer-in 160ms var(--ease, ease-out);
}
@keyframes viewer-in { from { opacity: 0; } to { opacity: 1; } }

/* 图片容器：尺寸 = 适应窗口的尺寸，缩放/旋转全交给 transform（画布跟着一起动，笔迹不会错位） */
.sheet {
  position: relative; flex: none;
  transform-origin: center center;
  will-change: transform;
  background: #fff;
  box-shadow: 0 18px 60px rgba(0, 0, 0, .55);
}
.shot { display: block; width: 100%; height: 100%; object-fit: fill; -webkit-user-drag: none; }
.ink { position: absolute; inset: 0; pointer-events: none; }
/* 荧光笔 = 马克笔：正片叠底叠在图上（透明处不参与混合，所以不会把图抹灰） */
.ink-marker { mix-blend-mode: multiply; }
.ink-live { pointer-events: none; }
.capture { position: absolute; inset: 0; touch-action: none; }
.cursor-draw { cursor: crosshair; }
.cursor-erase { cursor: cell; }

/* ---------------- 毛玻璃胶囊 ---------------- */
/* 参考图的口径：半透明壳 + 大模糊 + 亮描边 + 柔投影，按钮是壳里的实心圆片。
   深底上要**偏冷灰**（纯黑壳会糊成一块），亮描边给一层"玻璃厚度"。 */
.cap {
  position: absolute; left: 50%; transform: translateX(-50%);
  display: flex; align-items: center; gap: 2px;
  padding: 6px; border-radius: 999px;
  background: color-mix(in srgb, #2b2f36 62%, transparent);
  border: 1px solid rgba(255, 255, 255, .10);
  box-shadow: 0 10px 30px rgba(0, 0, 0, .45), inset 0 1px 0 rgba(255, 255, 255, .08);
  backdrop-filter: blur(28px) saturate(1.8);
  -webkit-backdrop-filter: blur(28px) saturate(1.8);
}
.cap-tools { top: max(14px, env(safe-area-inset-top)); max-width: calc(100vw - 24px); flex-wrap: wrap; justify-content: center; }
.cap-zoom { bottom: max(18px, env(safe-area-inset-bottom)); }
.cap-sep { width: 1px; height: 20px; margin: 0 4px; background: rgba(255, 255, 255, .16); }

.tbtn, .zbtn, .wbtn {
  width: 34px; height: 34px; flex: none; padding: 0; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: 999px; background: transparent; color: rgba(255, 255, 255, .78);
  transition: background 120ms ease, color 120ms ease, transform 120ms ease;
}
.tbtn:hover, .zbtn:hover, .wbtn:hover { background: rgba(255, 255, 255, .10); color: #fff; }
.tbtn.on, .wbtn.on { background: rgba(255, 255, 255, .20); color: #fff; }
.tbtn:disabled { opacity: .32; cursor: default; background: transparent; }
.tbtn:active, .zbtn:active { transform: scale(.94); }
.wbtn { width: 26px; }
.wdot { display: block; border-radius: 999px; background: currentColor; }
.zpct {
  min-width: 62px; height: 34px; padding: 0 6px; cursor: pointer;
  border: 0; border-radius: 999px; background: transparent; color: #fff;
  font: 600 13px/1 var(--font-num, ui-monospace, monospace); letter-spacing: .2px;
}
.zpct:hover { background: rgba(255, 255, 255, .10); }
/* 关闭：参考图里那枚**红色**圆片（红底白叉），是唯一的警示色，别处不用 */
.zclose { background: rgba(198, 61, 61, .92); color: #fff; }
.zclose:hover { background: #d04545; color: #fff; }

/* ---------------- 选择框 / 笔记 ---------------- */
.sel {
  position: absolute; border: 1.5px solid #3b82f6; border-radius: 3px;
  background: rgba(59, 130, 246, .14); pointer-events: none;
}
.note-bar {
  position: absolute; display: flex; align-items: center; gap: 6px;
  padding: 4px; border-radius: 10px; transform-origin: bottom center;
  background: rgba(32, 35, 42, .92); border: 1px solid rgba(255, 255, 255, .12);
  box-shadow: 0 8px 24px rgba(0, 0, 0, .45);
  backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
  white-space: nowrap; z-index: 3;
}
.nb {
  height: 28px; padding: 0 10px; cursor: pointer;
  border: 0; border-radius: 7px; background: transparent; color: rgba(255, 255, 255, .82);
  font-size: 12.5px; font-weight: 600;
}
.nb:hover { background: rgba(255, 255, 255, .12); color: #fff; }
.nb.primary { background: #3b82f6; color: #fff; }
.nb.primary:hover { background: #4b8df8; }
.note-edit {
  position: absolute; z-index: 3; width: 240px;
  transform-origin: top center; padding: 6px; border-radius: 10px;
  background: rgba(32, 35, 42, .95); border: 1px solid rgba(255, 255, 255, .12);
  box-shadow: 0 10px 28px rgba(0, 0, 0, .5);
}
.note-input {
  width: 100%; box-sizing: border-box; resize: none;
  padding: 7px 8px; border-radius: 8px; border: 1px solid rgba(255, 255, 255, .14);
  background: rgba(255, 255, 255, .06); color: #f2f4f8;
  font: inherit; font-size: 12.5px; line-height: 1.5;
}
.note-input:focus { outline: none; border-color: #3b82f6; }
.note-dot {
  position: absolute; width: 22px; height: 22px; padding: 0; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid rgba(255, 255, 255, .5); border-radius: 999px;
  background: #f6c445; color: #3a2c00; box-shadow: 0 3px 10px rgba(0, 0, 0, .4);
  z-index: 2;
}
.note-card {
  position: absolute; z-index: 4; width: 250px; transform-origin: top center;
  padding: 8px; border-radius: 12px;
  background: rgba(32, 35, 42, .96); border: 1px solid rgba(255, 255, 255, .12);
  box-shadow: 0 12px 32px rgba(0, 0, 0, .55);
}
.note-actions { display: flex; align-items: center; gap: 4px; margin-top: 6px; }
.note-actions .spacer { flex: 1; }

/* 图注 / 状态：贴近胶囊的弱提示，不要抢图 */
.cap-caption {
  position: absolute; left: 50%; transform: translateX(-50%);
  bottom: calc(max(18px, env(safe-area-inset-bottom)) + 62px);
  max-width: min(760px, calc(100vw - 48px));
  padding: 0 4px; color: rgba(255, 255, 255, .58); font-size: 12.5px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  pointer-events: none;
}
.cap-status {
  position: absolute; right: 16px; bottom: 16px;
  color: rgba(255, 255, 255, .45); font-size: 12px; pointer-events: none;
}
@media (max-width: 640px) {
  .cap-tools { gap: 0; padding: 5px; }
  .tbtn, .zbtn { width: 32px; height: 32px; }
  .zpct { min-width: 52px; }
}
</style>
