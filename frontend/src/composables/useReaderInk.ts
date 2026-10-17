/**
 * 阅读器的手写批注状态（**全局单例**：一个阅读器只可能有一份）。
 *
 * 为什么放模块级而不是组件内：笔迹挂在**每一页**的页块上（`page:<页号>` 一个文档），
 * 而工具栏（顶部胶囊）与各个页块的画布是**兄弟组件** —— 撤销/重做、当前笔、粗细
 * 必须共用同一份状态，否则工具栏按不到画布上的那一笔。
 *
 * 持久化：每一页一条 `image_mark` 记录（`key = page:<页号>`），改动后防抖 700ms 覆盖写。
 * 用户口径是"默认保存"，所以没有保存按钮；切页/退出阅读器时补一次 flush。
 */
import { reactive, ref } from 'vue';
import { api } from '@/api/client';
import { reportError } from '@/stores/app';
import {
  WIDTH_STEPS, clamp01, cloneDoc, emptyDoc,
  type InkDoc, type InkNote, type InkTool, type WidthKey,
} from '@/utils/imageInk';

/** 批注模式开关：开着时正文上盖一层可画的画布（关掉就完全回到普通阅读） */
export const inkOn = ref(false);
export const inkTool = ref<InkTool>('pen');
export const inkWidth = ref<WidthKey>('mid');
/** 最后一次动笔的那一页：工具栏的撤销/重做作用于它 */
export const inkPage = ref<number | null>(null);

export interface PageInk {
  doc: InkDoc;
  past: InkDoc[];
  future: InkDoc[];
  /** 内容版本号：画布靠它知道该重画（文档对象本身不大，但重画要节流） */
  version: number;
  loaded: boolean;
  loading: boolean;
  dirty: boolean;
}

const pages = new Map<number, PageInk>();
let paperId = '';
let saveTimer: ReturnType<typeof setTimeout> | null = null;
/** 串行保存：并发的两次 PUT 谁先到不确定，先到的旧文档会把后到的新文档盖掉 */
let saveChain: Promise<void> = Promise.resolve();

function newState(): PageInk {
  return reactive({
    doc: emptyDoc(), past: [], future: [], version: 0,
    loaded: false, loading: false, dirty: false,
  }) as PageInk;
}

export function inkState(page: number): PageInk {
  let state = pages.get(page);
  if (!state) { state = newState(); pages.set(page, state); }
  return state;
}

/** 换论文时把整份状态清掉（笔迹是按论文存的，串了就会画到别的论文上） */
export function resetInk(paper: string): void {
  paperId = paper;
  pages.clear();
  inkPage.value = null;
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
        at: n.at ?? null,
      }))
    : [];
  return { v: 1, strokes, notes };
}

export async function ensureInkLoaded(page: number): Promise<void> {
  const state = inkState(page);
  if (state.loaded || state.loading || !paperId) return;
  state.loading = true;
  try {
    const res = await api.get<{ doc?: InkDoc }>('/annotations/image-marks',
      { paper_id: paperId, key: `page:${page}` });
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
  pages.forEach((state, page) => {
    if (!state.dirty) return;
    state.dirty = false;
    jobs.push({ key: `page:${page}`, doc: cloneDoc(state.doc) });
  });
  if (!jobs.length) return;
  saveChain = saveChain.then(async () => {
    for (const job of jobs) {
      try {
        await api.put('/annotations/image-marks', { paper_id: paper, key: job.key, doc: job.doc });
      } catch (err) {
        // 存失败就把脏标记放回去，下一次改动/退出时再试（不要静悄悄丢笔迹）
        const page = Number(job.key.split(':')[1]);
        const state = pages.get(page);
        if (state) state.dirty = true;
        reportError(err, '批注保存失败');
      }
    }
  });
  await saveChain;
}

/** 一次改动 = 一张撤销快照（笔迹与笔记共用同一条栈，用户只认"上一步"） */
export function commitInk(page: number, next: InkDoc): void {
  const state = inkState(page);
  state.past.push(cloneDoc(state.doc));
  if (state.past.length > 60) state.past.shift();
  state.future = [];
  state.doc = next;
  state.version += 1;
  state.dirty = true;
  inkPage.value = page;
  scheduleInkSave();
}

export function undoInk(page: number | null): void {
  if (page === null) return;
  const state = inkState(page);
  const prev = state.past.pop();
  if (!prev) return;
  state.future.push(cloneDoc(state.doc));
  state.doc = prev;
  state.version += 1;
  state.dirty = true;
  scheduleInkSave();
}

export function redoInk(page: number | null): void {
  if (page === null) return;
  const state = inkState(page);
  const next = state.future.pop();
  if (!next) return;
  state.past.push(cloneDoc(state.doc));
  state.doc = next;
  state.version += 1;
  state.dirty = true;
  scheduleInkSave();
}

export function canUndo(page: number | null): boolean {
  return page !== null && inkState(page).past.length > 0;
}

export function canRedo(page: number | null): boolean {
  return page !== null && inkState(page).future.length > 0;
}

/** 当前笔的粗细（比例）：荧光笔单独一档，比普通笔宽得多 */
export function widthFor(kind: 'pen' | 'highlighter'): number {
  const step = WIDTH_STEPS.find((s) => s.key === inkWidth.value) ?? WIDTH_STEPS[1];
  return kind === 'highlighter' ? step.marker : step.pen;
}
