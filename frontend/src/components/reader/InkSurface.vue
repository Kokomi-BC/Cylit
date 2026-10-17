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
  MARKER_COLOR, PEN_COLOR, clamp01, hitStroke, paintDoc, paintStroke, pressureOf,
  type InkNote, type InkStroke,
} from '@/utils/imageInk';
import {
  commitInk, ensureInkLoaded, inkPage, inkState, inkTool, widthFor,
} from '@/composables/useReaderInk';

const props = defineProps<{ page: number; enabled: boolean }>();

const root = ref<HTMLElement | null>(null);
const baseCv = ref<HTMLCanvasElement | null>(null);
const markerCv = ref<HTMLCanvasElement | null>(null);
const liveCv = ref<HTMLCanvasElement | null>(null);
const capture = ref<HTMLElement | null>(null);

const state = computed(() => inkState(props.page));
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
  const pen = clearCv(baseCv.value);
  const marker = clearCv(markerCv.value);
  clearCv(liveCv.value);
  if (!near.value) return;
  const w = size.value.w;
  const h = size.value.h;
  if (!w || !h) return;
  if (pen) paintDoc(pen, { ...state.value.doc, strokes: state.value.doc.strokes.filter((s) => s.tool === 'pen') }, w, h);
  if (marker) paintDoc(marker, { ...state.value.doc, strokes: state.value.doc.strokes.filter((s) => s.tool === 'highlighter') }, w, h);
}

function paintLive(stroke: InkStroke | null) {
  const ctx = clearCv(liveCv.value);
  if (!ctx || !stroke) return;
  paintStroke(ctx, stroke, size.value.w, size.value.h);
}

function sizeCanvases() {
  const el = root.value;
  if (!el) return;
  const w = el.clientWidth;
  const h = el.clientHeight;
  if (!w || !h) return;
  size.value = { w, h };
  const ratio = dpr();
  for (const cv of [baseCv.value, markerCv.value, liveCv.value]) {
    if (!cv) continue;
    cv.width = Math.max(1, Math.round(w * ratio));
    cv.height = Math.max(1, Math.round(h * ratio));
    cv.style.width = w + 'px';
    cv.style.height = h + 'px';
  }
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
    color: kind === 'highlighter' ? MARKER_COLOR : PEN_COLOR,
    w: widthFor(kind),
    pts: [[pt.x, pt.y, pressureOf(ev.pressure, 0.9, ev.pointerType === 'pen')]],
  };
  drawing = { stroke, lastAt: ev.timeStamp || performance.now(), lastX: ev.clientX, lastY: ev.clientY };
  inkPage.value = props.page;
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
  commitInk(props.page, { ...state.value.doc, strokes: [...state.value.doc.strokes, active.stroke] });
}

function eraseAt(ev: PointerEvent) {
  const pt = toLocal(ev.clientX, ev.clientY);
  const idx = hitStroke(state.value.doc, pt.x, pt.y, 12, size.value.w, size.value.h);
  if (idx < 0) return;
  commitInk(props.page, {
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
  if (tool === 'select') selectAt(ev);
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

function savePending() {
  const sel = pending.value;
  const text = draft.value.trim();
  if (!sel || !text) { cancelSelect(); return; }
  const note: InkNote = {
    id: Math.random().toString(36).slice(2, 10),
    rect: sel.rect,
    text,
    row_id: sel.rowId,
    at: new Date().toISOString(),
  };
  commitInk(props.page, { ...state.value.doc, notes: [...state.value.doc.notes, note] });
  cancelSelect();
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
  commitInk(props.page, {
    ...state.value.doc,
    notes: state.value.doc.notes.map((n) => (n.id === note.id ? { ...n, text } : n)),
  });
  editing.value = null;
}

function deleteNote(note: InkNote) {
  commitInk(props.page, {
    ...state.value.doc,
    notes: state.value.doc.notes.filter((n) => n.id !== note.id),
  });
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
  sizeCanvases();
  void ensureInkLoaded(props.page);
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
      if (entries.some((e) => e.isIntersecting)) near.value = true;
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
watch(near, (on) => { if (on) sizeCanvases(); });
watch(() => props.enabled, (on) => { if (on) { void ensureInkLoaded(props.page); sizeCanvases(); } });
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
