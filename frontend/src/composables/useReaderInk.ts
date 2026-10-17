/**
 * 阅读器的手写批注状态（**全局单例**：一个阅读器只可能有一份）。
 *
 * 为什么放模块级而不是组件内：笔迹挂在**每一页**的页块上（一份文档一块画布），
 * 而工具栏（顶部主胶囊）与各个页块的画布是**兄弟组件** —— 撤销/重做、当前笔、颜色、粗细
 * 必须共用同一份状态，否则工具栏按不到画布上的那一笔。
 *
 * 文档键 = page:<页号>-<视图>：**每个视图各存一份**（用户口径）。三套视图的页块高度/行数
 * 完全不同（原文只有英文、对照是双栏），共用一份坐标必然错位。
 *
 * 持久化：每份文档一条 image_mark 记录，改动后防抖 700ms 覆盖写；笔记同时写进 note 表
 * （这样侧栏「笔记」页签里也能看到、并能点回那一行）。
 */
import { reactive, ref } from 'vue';
import { api } from '@/api/client';
import { reportError } from '@/stores/app';
import {
  MARKER_COLOR, WIDTH_STEPS, clamp01, cloneDoc, emptyDoc,
  type InkDoc, type InkNote, type InkTool, type WidthKey,
} from '@/utils/imageInk';

/** 批注模式开关：开着时正文上盖一层可画的画布（关掉就完全回到普通阅读） */
export const inkOn = ref(false);
export const inkTool = ref<InkTool>('pen');
export const inkWidth = ref<WidthKey>('mid');
/** 普通笔的颜色（荧光笔固定是马克笔黄：深色笔迹叠不出荧光感） */
export const inkColor = ref('#1b1b1f');
/** 当前视图（原始 / 译文 / 对照）：文档按视图分开存，所以它一变，笔迹就换一份 */
export const inkView = ref<'original' | 'translation' | 'bilingual'>('original');
/** 最后一次动笔的那份文档键：工具栏的撤销/重做作用于它 */
export const inkKey = ref<string | null>(null);

/** 便签同款色板：黑白蓝红绿黄紫橙（深色底上也读得清） */
export const INK_COLORS = [
  '#1b1b1f', '#8e8e93', '#2f6fed', '#e8384f',
  '#2aa775', '#f0a11a', '#8b5cf6', '#e2683c',
];

export interface PageInk {
  doc: InkDoc;
  past: InkDoc[];
  future: InkDoc[];
  /** 内容版本号：画布靠它知道该重画 */
  version: number;
  loaded: boolean;
  loading: boolean;
  dirty: boolean;
}

const pages = new Map<string, PageInk>();
let paperId = '';
let saveTimer: ReturnType<typeof setTimeout> | null = null;
/** 串行保存：并发的两次 PUT 谁先到不确定，先到的旧文档会把后到的新文档盖掉 */
let saveChain: Promise<void> = Promise.resolve();
/** 笔记写进 note 表之后，让阅读器重新拉一次侧栏（侧栏的数据源是 reader payload） */
let notesListener: (() => void) | null = null;

export function setInkNotesListener(fn: (() => void) | null): void {
  notesListener = fn;
}

/** 文档键：一页 × 一个视图 = 一份批注 */
export function pageKey(page: number): string {
  return 'page:' + page + '-' + (inkView.value || 'original');
}

function newState(): PageInk {
  return reactive({
    doc: emptyDoc(), past: [], future: [], version: 0,
    loaded: false, loading: false, dirty: false,
  }) as PageInk;
}

export function inkState(key: string): PageInk {
  let state = pages.get(key);
  if (!state) { state = newState(); pages.set(key, state); }
  return state;
}

/** 换论文时把整份状态清掉（笔迹是按论文存的，串了就会画到别的论文上） */
export function resetInk(paper: string): void {
  paperId = paper;
  pages.clear();
  inkKey.value = null;
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
}

export function sanitizeDoc(raw: any): InkDoc {
  const strokes = Array.isArray(raw?.strokes)
    ? raw.strokes
      .filter((s: any) => s && Array.isArray(s.pts) && s.pts.length >= 2)
      .map((s: any) => ({
        id: String(s.id ?? Math.random().toString(36).slice(2)),
        tool: s.tool === 'highlighter' ? 'highlighter' : 'pen',
        color: typeof s.color === 'string' ? s.color : '#1b1b1f',
        w: Number(s.w) || 0.004,
        pts: s.pts.map((p: any) => {
          const point: [number, number, number?] = [clamp01(Number(p?.[0])), clamp01(Number(p?.[1]))];
          if (p?.length > 2 && p[2] !== null && p[2] !== undefined) point.push(clamp01(Number(p[2])));
          return point;
        }),
      }))
    : [];
  const notes = Array.isArray(raw?.notes)
    ? raw.notes
      .filter((n: any) => n && String(n.text || '').trim() && Array.isArray(n.rect) && n.rect.length >= 4)
      .map((n: any) => ({
        id: String(n.id ?? Math.random().toString(36).slice(2)),
        rect: n.rect.slice(0, 4).map((v: any) => clamp01(Number(v) || 0)) as InkNote['rect'],
        text: String(n.text),
        row_id: n.row_id ?? n.block_id ?? null,
        // 对应的 note 表行 id：有了它，侧栏里改/删这条笔记才能同步回来
        note_id: n.note_id ?? null,
        at: n.at ?? null,
      }))
    : [];
  return { v: 1, strokes, notes };
}

export async function ensureInkLoaded(key: string): Promise<void> {
  const state = inkState(key);
  if (state.loaded || state.loading || !paperId) return;
  state.loading = true;
  try {
    const res = await api.get<{ doc?: InkDoc }>('/annotations/image-marks',
      { paper_id: paperId, key });
    state.doc = sanitizeDoc(res?.doc);
  } catch (err) {
    reportError(err, '批注读取失败');
  } finally {
    state.loaded = true;
    state.loading = false;
    state.version += 1;
  }
}

export function scheduleInkSave(): void {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { void flushInk(); }, 700);
}

export async function flushInk(): Promise<void> {
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  const paper = paperId;
  if (!paper) return;
  const jobs: Array<{ key: string; doc: InkDoc }> = [];
  pages.forEach((state, key) => {
    if (!state.dirty) return;
    state.dirty = false;
    jobs.push({ key, doc: cloneDoc(state.doc) });
  });
  if (!jobs.length) return;
  saveChain = saveChain.then(async () => {
    for (const job of jobs) {
      try {
        await api.put('/annotations/image-marks', { paper_id: paper, key: job.key, doc: job.doc });
      } catch (err) {
        // 存失败就把脏标记放回去，下一次改动/退出时再试（不静悄悄丢笔迹）
        const state = pages.get(job.key);
        if (state) state.dirty = true;
        reportError(err, '批注保存失败');
      }
    }
  });
  await saveChain;
}

/** 一次改动 = 一张撤销快照（笔迹与笔记共用同一条栈，用户只认"上一步"） */
export function commitInk(key: string, next: InkDoc): void {
  const state = inkState(key);
  state.past.push(cloneDoc(state.doc));
  if (state.past.length > 60) state.past.shift();
  state.future = [];
  state.doc = next;
  state.version += 1;
  state.dirty = true;
  inkKey.value = key;
  scheduleInkSave();
}

export function undoInk(key: string | null): void {
  if (!key) return;
  const state = inkState(key);
  const prev = state.past.pop();
  if (!prev) return;
  state.future.push(cloneDoc(state.doc));
  state.doc = prev;
  state.version += 1;
  state.dirty = true;
  scheduleInkSave();
}

export function redoInk(key: string | null): void {
  if (!key) return;
  const state = inkState(key);
  const next = state.future.pop();
  if (!next) return;
  state.past.push(cloneDoc(state.doc));
  state.doc = next;
  state.version += 1;
  state.dirty = true;
  scheduleInkSave();
}

export function canUndo(key: string | null): boolean {
  return !!key && inkState(key).past.length > 0;
}

export function canRedo(key: string | null): boolean {
  return !!key && inkState(key).future.length > 0;
}

/** 当前笔的粗细（比例）：荧光笔单独一档，比普通笔宽得多 */
export function widthFor(kind: 'pen' | 'highlighter'): number {
  const step = WIDTH_STEPS.find((s) => s.key === inkWidth.value) ?? WIDTH_STEPS[1];
  return kind === 'highlighter' ? step.marker : step.pen;
}

export function colorFor(kind: 'pen' | 'highlighter'): string {
  return kind === 'highlighter' ? MARKER_COLOR : inkColor.value;
}

/* ------------------------------ 笔记 ↔ 侧栏 ------------------------------ */

/** 在 note 表里建一条笔记（侧栏「笔记」页签读的就是它）。失败不阻塞画布：回 null。 */
export async function createSideNote(text: string, rowId: string | null): Promise<string | null> {
  if (!paperId) return null;
  try {
    const note = await api.post<{ id: string }>('/notes', {
      paper_id: paperId,
      block_id: rowId,
      content_md: text,
      // `ink` 标记来源：以后要按来源过滤/换样式时有据可依
      tags: ['ink'],
    });
    notesListener?.();
    return note?.id ?? null;
  } catch (err) {
    reportError(err, '笔记同步到侧栏失败');
    return null;
  }
}

export async function updateSideNote(noteId: string, text: string): Promise<void> {
  try {
    await api.put('/notes/' + noteId, { content_md: text });
    notesListener?.();
  } catch (err) {
    reportError(err, '笔记同步到侧栏失败');
  }
}

export async function deleteSideNote(noteId: string): Promise<void> {
  try {
    await api.del('/notes/' + noteId);
    notesListener?.();
  } catch (err) {
    reportError(err, '笔记同步到侧栏失败');
  }
}
