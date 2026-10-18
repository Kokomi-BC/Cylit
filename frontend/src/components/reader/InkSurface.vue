<script setup lang="ts">
/**
 * 一页正文上的「手写批注层」（Apple 便签的手感）。
 *
 * 为什么一页一层：笔迹要**跟着版面走**。整段正文的高度可能几万像素（一块 canvas 放不下），
 * 而"页"是最自然的锚点 —— 一页一个批注文档（key = page:页号）、一块画布。
 *
 * 三种工具共用同一块画布：
 *   - 画笔 / 荧光笔：按落笔轻重收放的带子（见 utils/imageInk.ts），荧光笔走 multiply；
 *   - 橡皮：整笔删除（撤销栈按整笔记）；
 *   - 自动选择：点一下正文，**框住那一行**，再问"要不要加笔记"。
 *
 * 非批注模式下这一层完全不拦截事件（pointer-events: none），所以正常划词、点链接都不受影响。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import {
  clamp01, hitStroke, paintDoc, paintStroke, pressureOf,
  type InkNote, type InkStroke,
} from '@/utils/imageInk';
import {
  colorFor, commitInk, createSideNote, deleteSideNote, ensureInkLoaded, inkKey,
  inkState, inkTool, inkView, pageKey, updateSideNote, widthFor,
} from '@/composables/useReaderInk';

const props = defineProps<{ page: number; enabled: boolean }>();

const root = ref<HTMLElement | null>(null);
const baseCv = ref<HTMLCanvasElement | null>(null);
const markerCv = ref<HTMLCanvasElement | null>(null);
const liveCv = ref<HTMLCanvasElement | null>(null);
const capture = ref<HTMLElement | null>(null);

/** 这份批注属于「这一页 × 当前视图」（用户口径：每个视图各存一份） */
const docKey = computed(() => pageKey(props.page));
const state = computed(() => inkState(docKey.value));
const size = ref({ w: 0, h: 0 });
const near = ref(false);

/* ------------------------------ 画布 ------------------------------ */

function dpr(): number {
  return Math.min(2, (typeof window !== 'undefined' && window.devicePixelRatio) || 1);
}

function clearCv(cv: HTMLCanvasElement | null) {
  const ctx = cv?.getContext('2d');
  if (!cv || !ctx) return null;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.scale(dpr(), dpr());
  return ctx;
}

function repaint() {
  /* ⚠️ 这里**不能**再判 props.enabled：退出批注模式后墨迹要**留着看得见**
   * （用户口径 2026-10-18："画笔模式切换到普通模式要保留墨迹"）。
   * 画布只在视口附近才分配/重绘，所以「留着」的代价只有眼前这几页。 */
  if (!near.value) return;
  const w = size.value.w;
  const h = size.value.h;
  if (!w || !h) return;
  // 荧光笔只在真用过时才占一块整页画布
  if (hasMarkerStrokes()) ensureSize(markerCv.value, w, h, dpr());
  const pen = clearCv(baseCv.value);
  const marker = clearCv(markerCv.value);
  clearCv(liveCv.value);
  if (pen) paintDoc(pen, { ...state.value.doc, strokes: state.value.doc.strokes.filter((s) => s.tool === 'pen') }, w, h);
  if (marker) paintDoc(marker, { ...state.value.doc, strokes: state.value.doc.strokes.filter((s) => s.tool === 'highlighter') }, w, h);
}

function paintLive(stroke: InkStroke | null) {
  const ctx = clearCv(liveCv.value);
  if (!ctx || !stroke) return;
  paintStroke(ctx, stroke, size.value.w, size.value.h);
}

function hasMarkerStrokes(): boolean {
  return inkState(docKey.value).doc.strokes.some((s) => s.tool === 'highlighter');
}

/**
 * 画布的**离屏缓冲区**很贵：2× dpr 的整页画布 ≈ 20MB，一页三块、22 页就是 1GB ——
 * 平板一进批注模式就卡住（实测进入瞬间分配 2.57 亿像素）。
 * 所以：只在「批注模式 + 视口附近」才真的分配，其它时候保持 1×1。
 */
function ensureSize(cv: HTMLCanvasElement | null, w: number, h: number, ratio: number): void {
  if (!cv) return;
  const cw = Math.max(1, Math.round(w * ratio));
  const ch = Math.max(1, Math.round(h * ratio));
  if (cv.width === cw && cv.height === ch) return;   // 尺寸没变：别重新分配（会清空 + 重排）
  cv.width = cw;
  cv.height = ch;
  cv.style.width = w + 'px';
  cv.style.height = h + 'px';
}

/** 退出批注模式：把三块画布缩到 1×1，立刻把几十 MB 还给系统 */
function releaseCanvases(): void {
  for (const cv of [baseCv.value, markerCv.value, liveCv.value]) {
    if (!cv) continue;
    cv.width = 1;
    cv.height = 1;
    cv.style.width = '0px';
    cv.style.height = '0px';
  }
}

function sizeCanvases() {
  const el = root.value;
  if (!el) return;
  if (!near.value) return;   // 见 ensureSize 的说明
  const w = el.clientWidth;
  const h = el.clientHeight;
  if (!w || !h) return;
  size.value = { w, h };
  const ratio = dpr();
  ensureSize(baseCv.value, w, h, ratio);
  ensureSize(liveCv.value, w, h, ratio);
  if (hasMarkerStrokes()) ensureSize(markerCv.value, w, h, ratio);
  repaint();
}

/* ------------------------------ 指针 ------------------------------ */

let drawing: { stroke: InkStroke; lastAt: number; lastX: number; lastY: number } | null = null;
let erasing = false;

function toLocal(clientX: number, clientY: number): { x: number; y: number } {
  const el = root.value;
  if (!el) return { x: 0, y: 0 };
  const r = el.getBoundingClientRect();
  return {
    x: clamp01((clientX - r.left) / Math.max(1, r.width)),
    y: clamp01((clientY - r.top) / Math.max(1, r.height)),
  };
}

function beginStroke(ev: PointerEvent, kind: 'pen' | 'highlighter') {
  const pt = toLocal(ev.clientX, ev.clientY);
  const stroke: InkStroke = {
    id: Math.random().toString(36).slice(2, 10),
    tool: kind,
    color: colorFor(kind),
    w: widthFor(kind),
    pts: [[pt.x, pt.y, pressureOf(ev.pressure, 0.9, ev.pointerType === 'pen')]],
  };
  drawing = { stroke, lastAt: ev.timeStamp || performance.now(), lastX: ev.clientX, lastY: ev.clientY };
  inkKey.value = docKey.value;
  paintLive(stroke);
}

function extendStroke(ev: PointerEvent) {
  if (!drawing) return;
  const dx = ev.clientX - drawing.lastX;
  const dy = ev.clientY - drawing.lastY;
  // 1.1px 以内当抖动丢掉：触摸屏"静止手抖"会攒出几百个重复点，白白变粗
  if (Math.hypot(dx, dy) < 1.1) return;
  const pt = toLocal(ev.clientX, ev.clientY);
  const now = ev.timeStamp || performance.now();
  const speed = Math.hypot(dx, dy) / Math.max(1, now - drawing.lastAt);
  drawing.stroke.pts.push([pt.x, pt.y, pressureOf(ev.pressure, speed, ev.pointerType === 'pen')]);
  drawing.lastAt = now;
  drawing.lastX = ev.clientX;
  drawing.lastY = ev.clientY;
  paintLive(drawing.stroke);
}

function endStroke() {
  const active = drawing;
  drawing = null;
  paintLive(null);
  if (!active || active.stroke.pts.length < 2) return;
  commitInk(docKey.value, { ...state.value.doc, strokes: [...state.value.doc.strokes, active.stroke] });
}

function eraseAt(ev: PointerEvent) {
  const pt = toLocal(ev.clientX, ev.clientY);
  const idx = hitStroke(state.value.doc, pt.x, pt.y, 12, size.value.w, size.value.h);
  if (idx < 0) return;
  commitInk(docKey.value, {
    ...state.value.doc,
    strokes: state.value.doc.strokes.filter((_, i) => i !== idx),
  });
}

function onPointerDown(ev: PointerEvent) {
  if (!props.enabled) return;
  if (ev.button !== undefined && ev.button > 0) return;
  (ev.currentTarget as HTMLElement).setPointerCapture?.(ev.pointerId);
  const tool = inkTool.value;
  if (tool === 'pen' || tool === 'marker') {
    beginStroke(ev, tool === 'marker' ? 'highlighter' : 'pen');
    return;
  }
  if (tool === 'eraser') {
    erasing = true;
    eraseAt(ev);
    return;
  }
}

/** 双击正文 = 框住这一行、问要不要加笔记（工具栏里不再放「文字选择」，见 toolbars 的说明） */
function onDoubleClick(ev: MouseEvent) {
  if (!props.enabled) return;
  if (inkTool.value === 'hand') return;
  selectAt(ev as unknown as PointerEvent);
}

function onPointerMove(ev: PointerEvent) {
  if (!props.enabled) return;
  if (drawing) { extendStroke(ev); return; }
  if (erasing) eraseAt(ev);
}

function onPointerUp() {
  if (drawing) endStroke();
  erasing = false;
}

/* ------------------------------ 自动选择 → 加笔记 ------------------------------ */

const pending = ref<{ rect: [number, number, number, number]; rowId: string | null } | null>(null);
/** 选中之后先问一句（用户口径："收到点击后再选择是否添加笔记"） */
const drafting = ref(false);
const draft = ref('');
const draftEl = ref<HTMLTextAreaElement | null>(null);
const editing = ref<InkNote | null>(null);
const editText = ref('');

function normRect(box: { left: number; top: number; right: number; bottom: number }): [number, number, number, number] {
  const el = root.value;
  if (!el) return [0, 0, 0.02, 0.02];
  const r = el.getBoundingClientRect();
  return [
    clamp01((box.left - r.left) / Math.max(1, r.width)),
    clamp01((box.top - r.top) / Math.max(1, r.height)),
    clamp01((box.right - r.left) / Math.max(1, r.width)),
    clamp01((box.bottom - r.top) / Math.max(1, r.height)),
  ];
}

const SELECTABLE = '[data-row-id], .row-pair, .md-doc p, .md-doc li, .md-doc h1, .md-doc h2, .md-doc h3, .md-doc h4, p, li, h1, h2, h3, h4, h5, h6, td, figcaption, .media-box';

function selectAt(ev: PointerEvent) {
  editing.value = null;
  const layer = capture.value;
  // 命中测试要"穿透"自己这层捕获层（否则 elementFromPoint 永远返回它自己）
  if (layer) layer.style.pointerEvents = 'none';
  const under = document.elementFromPoint(ev.clientX, ev.clientY) as HTMLElement | null;
  if (layer) layer.style.pointerEvents = '';
  const host = (under?.closest?.(SELECTABLE) as HTMLElement | null) ?? under;
  const box = host?.getBoundingClientRect?.();
  const frame = root.value?.getBoundingClientRect();
  // 框必须落在这一页里（点到侧栏/别的页时退化成"点一处打点"）
  const inside = !!(box && frame && box.top >= frame.top - 2 && box.bottom <= frame.bottom + 2);
  if (box && inside) {
    const rowId = host?.getAttribute('data-row-id')
      ?? host?.closest('[data-row-id]')?.getAttribute('data-row-id') ?? null;
    pending.value = { rect: normRect(box), rowId };
  } else {
    const pt = toLocal(ev.clientX, ev.clientY);
    pending.value = {
      rect: [clamp01(pt.x - 0.015), clamp01(pt.y - 0.02), clamp01(pt.x + 0.015), clamp01(pt.y + 0.02)],
      rowId: null,
    };
  }
  drafting.value = false;
  draft.value = '';
}

function cancelSelect() {
  pending.value = null;
  drafting.value = false;
  draft.value = '';
}

async function savePending() {
  const sel = pending.value;
  const text = draft.value.trim();
  if (!sel || !text) { cancelSelect(); return; }
  const note: InkNote = {
    id: Math.random().toString(36).slice(2, 10),
    rect: sel.rect,
    text,
    row_id: sel.rowId,
    note_id: null,
    at: new Date().toISOString(),
  };
  commitInk(docKey.value, { ...state.value.doc, notes: [...state.value.doc.notes, note] });
  cancelSelect();
  // 同步进 note 表（侧栏「笔记」页签读它）；拿到 id 再回填，之后改/删才能对上
  const noteId = await createSideNote(text, sel.rowId);
  if (!noteId) return;
  commitInk(docKey.value, {
    ...state.value.doc,
    notes: state.value.doc.notes.map((n) => (n.id === note.id ? { ...n, note_id: noteId } : n)),
  });
}

function openNote(note: InkNote) {
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
  commitInk(docKey.value, {
    ...state.value.doc,
    notes: state.value.doc.notes.map((n) => (n.id === note.id ? { ...n, text } : n)),
  });
  if (note.note_id) void updateSideNote(note.note_id, text);
  editing.value = null;
}

function deleteNote(note: InkNote) {
  commitInk(docKey.value, {
    ...state.value.doc,
    notes: state.value.doc.notes.filter((n) => n.id !== note.id),
  });
  if (note.note_id) void deleteSideNote(note.note_id);
  editing.value = null;
}

function rectStyle(rect: [number, number, number, number]) {
  return {
    left: rect[0] * 100 + '%',
    top: rect[1] * 100 + '%',
    width: Math.max(0.004, rect[2] - rect[0]) * 100 + '%',
    height: Math.max(0.004, rect[3] - rect[1]) * 100 + '%',
  };
}

/* ------------------------------ 生命周期 ------------------------------ */

let ro: ResizeObserver | null = null;
let io: IntersectionObserver | null = null;

onMounted(async () => {
  await nextTick();
  // 墨迹在普通模式下也要显示：进了视口就分配画布 + 拉这一页的笔迹
  if (near.value) {
    sizeCanvases();
    void ensureInkLoaded(docKey.value);
  }
  if (typeof ResizeObserver !== 'undefined' && root.value) {
    let lastW = 0;
    let lastH = 0;
    ro = new ResizeObserver(() => {
      const el = root.value;
      if (!el) return;
      // 版面高度在图片懒加载时会连续变几次：变动太小就别重画
      if (Math.abs(el.clientWidth - lastW) < 2 && Math.abs(el.clientHeight - lastH) < 2) return;
      lastW = el.clientWidth;
      lastH = el.clientHeight;
      sizeCanvases();
    });
    ro.observe(root.value);
  }
  if (typeof IntersectionObserver !== 'undefined' && root.value) {
    io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) { near.value = true; continue; }
        // 离视口远了就把画布还回去：一页两块 2× dpr 画布 ≈ 40MB，翻十几页就吃掉几百 MB
        if (near.value && !drawing && !erasing) {
          near.value = false;
          releaseCanvases();
        }
      }
    }, { rootMargin: '600px 0px' });
    io.observe(root.value);
  } else {
    near.value = true;
  }
});

onBeforeUnmount(() => {
  ro?.disconnect();
  io?.disconnect();
  ro = null;
  io = null;
});

watch(() => state.value.version, () => { if (near.value) repaint(); });
watch(near, (on) => {
  if (!on) return;
  void ensureInkLoaded(docKey.value);
  sizeCanvases();
});
/* 进入批注：确保画布尺寸/数据就绪；**退出批注不释放**（墨迹要留着看见），
 * 内存由「滚远了才回收」那条（IntersectionObserver）兜住。 */
watch(() => props.enabled, (on) => {
  if (!on) return;
  void ensureInkLoaded(docKey.value);
  sizeCanvases();
});
// 切视图（原文/译文/对照）= 换一份批注：重新拉那一份并重画
watch(inkView, () => {
  if (!near.value) return;
  void ensureInkLoaded(docKey.value);
  sizeCanvases();
});
// 点了「添加笔记」就直接能打字（少一次点击；平板/触屏上也一样）
watch(drafting, async (on) => {
  if (!on) return;
  await nextTick();
  draftEl.value?.focus();
});
</script>

<template>
  <div ref="root" class="ink-layer" :class="{ active: enabled }">
    <canvas ref="baseCv" class="ink" />
    <!-- 荧光笔走正片叠底：像马克笔一样"叠加变暗"，不会把正文抹白 -->
    <canvas ref="markerCv" class="ink ink-marker" />
    <canvas ref="liveCv" class="ink ink-live" :class="{ 'ink-marker': inkTool === 'marker' }" />
    <div
      v-if="enabled"
      ref="capture"
      class="ink-capture"
      :class="{ draw: inkTool === 'pen' || inkTool === 'marker', erase: inkTool === 'eraser', passthrough: inkTool === 'hand' }"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
      @dblclick="onDoubleClick"
    />

    <div v-if="pending" class="sel" :style="rectStyle(pending.rect)" />
    <div
      v-if="pending"
      class="note-bar"
      :style="{ left: ((pending.rect[0] + pending.rect[2]) / 2) * 100 + '%', top: pending.rect[1] * 100 + '%' }"
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
      :style="{ left: ((pending.rect[0] + pending.rect[2]) / 2) * 100 + '%', top: pending.rect[3] * 100 + '%' }"
      @pointerdown.stop
    >
      <textarea ref="draftEl" v-model="draft" class="note-input" rows="3" placeholder="写点什么…（自动保存）" />
    </div>

    <button
      v-for="note in state.doc.notes"
      :key="note.id"
      class="note-dot"
      :style="{ left: note.rect[2] * 100 + '%', top: note.rect[1] * 100 + '%' }"
      :title="note.text"
      @pointerdown.stop
      @click.stop="openNote(note)"
    >
      <AppIcon name="note" :size="12" />
    </button>
    <div
      v-if="editing"
      class="note-card"
      :style="{ left: ((editing.rect[0] + editing.rect[2]) / 2) * 100 + '%', top: editing.rect[3] * 100 + '%' }"
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
</template>

<style scoped>
/* 整层默认"透明"：不拦截事件、不占位，正文照常划词与点击 */
.ink-layer { position: absolute; inset: 0; z-index: 2; pointer-events: none; }
.ink { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
.ink-marker { mix-blend-mode: multiply; }
.ink-capture { position: absolute; inset: 0; pointer-events: auto; touch-action: none; }
.ink-capture.draw { cursor: crosshair; }
.ink-capture.erase { cursor: cell; }
/* 手：放行。批注模式下也要能正常划词/点链接 —— 只有真的拿笔时才挡事件 */
.ink-capture.passthrough { pointer-events: none; }

.sel {
  position: absolute; border: 1.5px solid #3b82f6; border-radius: 3px;
  background: rgba(59, 130, 246, .12); pointer-events: none;
}
.note-bar {
  position: absolute; transform: translate(-50%, -100%);
  display: flex; align-items: center; gap: 6px; padding: 4px;
  border-radius: 10px; white-space: nowrap; z-index: 6; pointer-events: auto;
  background: color-mix(in srgb, #2b2f36 88%, transparent);
  border: 1px solid rgba(255, 255, 255, .12);
  box-shadow: 0 8px 24px rgba(0, 0, 0, .28);
  backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
}
.nb {
  height: 26px; padding: 0 10px; cursor: pointer;
  border: 0; border-radius: 7px; background: transparent; color: rgba(255, 255, 255, .84);
  font-size: 12.5px; font-weight: 600;
}
.nb:hover { background: rgba(255, 255, 255, .14); color: #fff; }
.nb.primary { background: #3b82f6; color: #fff; }
.nb.primary:hover { background: #4b8df8; }
.note-edit, .note-card {
  position: absolute; z-index: 6; width: 260px; padding: 6px; pointer-events: auto;
  border-radius: 10px; transform: translate(-50%, 6px);
  background: color-mix(in srgb, #23262c 94%, transparent);
  border: 1px solid rgba(255, 255, 255, .12);
  box-shadow: 0 10px 28px rgba(0, 0, 0, .32);
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
  transform: translate(-50%, -50%); z-index: 5; pointer-events: auto;
  border: 1px solid rgba(255, 255, 255, .5); border-radius: 999px;
  background: #f6c445; color: #3a2c00; box-shadow: 0 3px 10px rgba(0, 0, 0, .3);
}
.note-actions { display: flex; align-items: center; gap: 4px; margin-top: 6px; }
.note-actions .spacer { flex: 1; }
</style>
