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
  LASER_DEFAULT_COLOR, WIDTH_STEPS, clamp01, cloneDoc, emptyDoc,
  type InkDoc, type InkNote, type InkStroke, type InkTool, type WidthKey,
} from '@/utils/imageInk';

/**
 * 笔的偏好（颜色 / 粗细）**记在本机**：下次进来直接还是上次那支。
 * 只记"最后用过的那一档"，不另做"常用色"面板 —— 用户口径：不另外排版，
 * 打开菜单时选中态就落在上次那个颜色 / 粗细上（见 `activeInkColor`）。
 */
const INK_PREFS_KEY = 'pm.ink.prefs';
interface InkPrefs {
  pen?: string; marker?: string; laser?: string; width?: WidthKey; tool?: InkTool;
  /** 每支笔"上一次自选的颜色"：只用来画那枚空心圆里的填充，不参与笔色判定 */
  custom?: Partial<Record<'pen' | 'marker' | 'laser', string>>;
}
function loadInkPrefs(): InkPrefs {
  try {
    const raw = localStorage.getItem(INK_PREFS_KEY);
    return raw ? (JSON.parse(raw) as InkPrefs) : {};
  } catch { return {}; }    /* 隐私模式 / 坏数据：当没有 */
}
const inkPrefs = loadInkPrefs();
/** 工具切换时由阅读器调一次（颜色/粗细那两个 setter 里已经各自调过了） */
export function persistInkPrefs(): void { saveInkPrefs(); }

function saveInkPrefs(): void {
  try {
    localStorage.setItem(INK_PREFS_KEY, JSON.stringify({
      pen: inkColor.value, marker: inkMarkerColor.value, laser: inkLaserColor.value,
      width: inkWidth.value,
      custom: { pen: customColors.pen, marker: customColors.marker, laser: customColors.laser },
      /* 连上次拿的是哪支笔一起记：下次打开工具栏就停在它上面（橡皮不记，退回画笔那档） */
      tool: inkTool.value === 'eraser' ? lastDrawTool.value : inkTool.value,
    }));
  } catch { /* 忽略 */ }
}

/**
 * 色板：**只留 5 个**（用户口径"精简默认颜色数量"）—— 黑 / 红 / 橙 / 绿 / 蓝。
 * 想要别的颜色点旁边的取色器（原生 `<input type="color">`），不用在色板里越堆越多。
 */
/** 红 → 橙 → 黄 → 绿 → 青 → 蓝 → 紫（用户口径：按这个顺序排；自选颜色摆在最前，见模板） */
export const INK_COLORS = ['#e8384f', '#f08c1a', '#e8c518', '#2aa775', '#20b2c8', '#2f6fed', '#8b5cf6'];

/**
 * 荧光笔**自己的一排色**（用户口径：荧光笔用另一套颜色菜单）：
 * 都是高亮纸常用的荧光色、比墨色浅一档 —— 荧光笔本来就是盖一层淡色，
 * 拿墨黑/正红去涂会把正文盖死。选中的那一档与墨色分开记（`inkMarkerColor`）。
 */
export const MARKER_COLORS = ['#ffd60a', '#a3e635', '#5ad2f4', '#ff9ecb', '#ffb020'];

/**
 * 老数据迁移：两套色板分开之前，荧光笔用的是墨色那排（里面有**黑**）。
 * 存在本机的 `marker` 若正好是墨色之一，说明它来自旧的共享色板 —— 换成荧光笔默认色，
 * 否则荧光笔打开时会出现"选中黑 / 一个都没选中"这种怪状态（用户口径：荧光笔不该有黑色）。
 * 用户在取色器里自选的颜色（不在墨色表里）原样保留。
 */
function migrateMarkerColor(value: string): string {
  return INK_COLORS.includes(value) ? MARKER_COLORS[0] : value;
}

/** 批注模式开关：开着时正文上盖一层可画的画布（关掉就完全回到普通阅读） */
export const inkOn = ref(false);
export const inkTool = ref<InkTool>(
  inkPrefs.tool === 'marker' ? 'marker' : inkPrefs.tool === 'laser' ? 'laser' : 'pen',
);
export const inkWidth = ref<WidthKey>(inkPrefs.width ?? 'mid');
/**
 * 普通笔的颜色。
 * 默认取色板的第一个（红）：色板里已经没有黑色了（用户口径：红橙黄绿青蓝紫），
 * 老数据里那个默认黑也一并映射到它，否则打开菜单会「一个色点都不亮」。
 */
export const inkColor = ref(inkPrefs.pen === '#1b1b1f' || !inkPrefs.pen ? INK_COLORS[0] : inkPrefs.pen);
/** 荧光笔的颜色（用户口径：荧光笔也要能换色） */
export const inkMarkerColor = ref(migrateMarkerColor(inkPrefs.marker || MARKER_COLORS[0]));
/** 激光笔的颜色：与画笔/荧光笔一样各记各的（换工具时颜色不会串） */
export const inkLaserColor = ref(inkPrefs.laser || LASER_DEFAULT_COLOR);
/**
 * 每支笔"上一次自选的颜色"（用户口径 2026-12-14：那枚图标要成**空心圆**，圆内填自选色）。
 * 只用来画图标，不参与笔色判定 —— 笔色还是 inkColor / inkMarkerColor / inkLaserColor 那三个。
 */
export const customColors = reactive<Record<'pen' | 'marker' | 'laser', string>>({
  pen: inkPrefs.custom?.pen || '',
  marker: inkPrefs.custom?.marker || '',
  laser: inkPrefs.custom?.laser || '',
});

/** 进橡皮之前拿的是哪支笔（橡皮清空本页之后切回它，用户口径第 3 条） */
export const lastDrawTool = ref<'pen' | 'marker' | 'laser'>('pen');
/** 当前视图（原始 / 译文 / 对照）：文档按视图分开存，所以它一变，笔迹就换一份 */
export const inkView = ref<'original' | 'translation' | 'bilingual'>('original');
/** 最后一次动笔的那份文档键：工具栏的撤销/重做作用于它 */
export const inkKey = ref<string | null>(null);


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
  clearLaser();          // 激光笔迹是按论文的临时笔迹，换论文就一起丢掉
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

/** 激光笔的粗细：与画笔共用同一张三档表（用户口径：二级菜单是同一套） */
export function widthForLaser(): number {
  return widthFor('pen');
}

export function colorFor(kind: 'pen' | 'highlighter'): string {
  return kind === 'highlighter' ? inkMarkerColor.value : inkColor.value;
}

/** 当前工具正在用的颜色（二级菜单里的"选中态"按它算） */
export function activeInkColor(): string {
  if (inkTool.value === 'marker') return inkMarkerColor.value;
  if (inkTool.value === 'laser') return inkLaserColor.value;
  return inkColor.value;
}

/**
 * 换颜色。`fromCustom` = 这一次是「自选色」（取色面板 / 系统取色器）来的：
 * 顺手把它记进 customColors，那枚空心圆里就填它；点色板上的圆点则不覆盖。
 */
export function setInkColor(value: string, fromCustom = false): void {
  if (inkTool.value === 'marker') {
    inkMarkerColor.value = value;
    if (fromCustom) customColors.marker = value;
  } else if (inkTool.value === 'laser') {
    inkLaserColor.value = value;
    if (fromCustom) customColors.laser = value;
  } else {
    inkColor.value = value;
    if (fromCustom) customColors.pen = value;
  }
  saveInkPrefs();          /* 每换一次色就落盘：下次打开菜单直接选中它 */
}

/** 换粗细（同样落盘） */
export function setInkWidth(value: WidthKey): void {
  inkWidth.value = value;
  saveInkPrefs();
}

/** 清空**这一页**的笔迹（笔记保留）：橡皮二次点击 → 滑块 → 这里 */
export function clearPageInk(key: string | null): void {
  if (!key) return;
  commitInk(key, { ...inkState(key).doc, strokes: [] });
}

/* ------------------------------ 激光笔（只活在内存里） ------------------------------
 * 用户口径：激光笔**不落盘、不进撤销栈** —— 它是"指给人看"的临时笔迹。
 * 所以这一段只碰内存，一行也不许调 commitInk / scheduleInkSave（后端 image_marks 里
 * 永远看不到它，Ctrl+Z 也不会"撤销掉激光笔"）。
 *
 * 计时**全局一份**（不是每笔各一个）：用户口径"整体统一计时"—— 后一笔落笔会把
 * 所有页面的激光笔迹一起拉回不透明、并重新数 1.5 秒。
 */
/** 停笔多久开始淡出（用户口径 2026-12-14：2s → 1.5s） */
const LASER_HOLD_MS = 1500;
/** 淡出本身也走 1.5s 线性（同上） */
const LASER_FADE_MS = 1500;
/** 按 pageKey 分开存：一页一份，切页/切视图各清各的 */
const laserPages = new Map<string, InkStroke[]>();
/** 激光层的整体不透明度（0..1）：所有页共用一份，画布只把它写到 style.opacity 上 */
export const laserAlpha = ref(1);
/** 笔迹版本号：加 / 清之后自增，画布照它重画（Map 本身不是响应式的） */
export const laserVersion = ref(0);

export function laserStrokesOf(key: string): InkStroke[] {
  let list = laserPages.get(key);
  if (!list) { list = []; laserPages.set(key, list); }
  return list;
}

let laserAt = 0;
let laserHold: ReturnType<typeof setTimeout> | null = null;
let laserRaf = 0;

/** 全模块唯一的淡出时钟：一个 hold 定时器 + 一个 rAF，有笔迹才起，清空就停 */
function stopLaserClock(): void {
  if (laserHold) { clearTimeout(laserHold); laserHold = null; }
  if (laserRaf) { cancelAnimationFrame(laserRaf); laserRaf = 0; }
}

function laserFadeTick(): void {
  laserRaf = 0;
  const elapsed = performance.now() - laserAt;
  if (elapsed >= LASER_HOLD_MS + LASER_FADE_MS) {
    clearLaser();
    return;
  }
  // 只在 1.5–3.0 秒这一段算比例（前面恒 1，由 hold 定时器挡住，不烧帧）
  laserAlpha.value = Math.max(0, Math.min(1, 1 - (elapsed - LASER_HOLD_MS) / LASER_FADE_MS));
  laserRaf = requestAnimationFrame(laserFadeTick);
}

/**
 * 续命的最小间隔：pointermove 能有 120Hz，每来一次就 clearTimeout+setTimeout 纯属浪费；
 * 200ms 的粒度对"手在动就不该淡"够用（淡出本身是 1500ms 起步的）。
 */
const LASER_TOUCH_GAP_MS = 200;
let laserTouchAt = 0;

/**
 * 落笔 / 继续画：立刻恢复满不透明，并**重新**计时（含正在淡出的那一段）。
 * `force` 留给"落笔 / 抬手"这种必须生效的时刻；书写途中的续命走 200ms 节流。
 */
export function touchLaser(force = false): void {
  const now = performance.now();
  if (!force && now - laserTouchAt < LASER_TOUCH_GAP_MS) return;
  laserTouchAt = now;
  stopLaserClock();
  laserAt = now;
  laserAlpha.value = 1;
  laserHold = setTimeout(() => {
    laserHold = null;
    laserRaf = requestAnimationFrame(laserFadeTick);
  }, LASER_HOLD_MS);
}

export function addLaserStroke(key: string, stroke: InkStroke): void {
  laserStrokesOf(key).push(stroke);
  laserVersion.value += 1;
  touchLaser(true);      // 抬手：倒计时从"这一笔结束"重新数满 1.5 秒
}

/** 全部页清掉（4 秒到点 / 换论文）：alpha 回 1、时钟停掉 */
export function clearLaser(): void {
  stopLaserClock();
  laserPages.clear();
  laserAlpha.value = 1;
  laserVersion.value += 1;
}

/** 只清一页（切视图 / 滚远回收 / 组件卸载）：别的页的激光笔迹还亮着 */
export function dropLaserPage(key: string): void {
  const list = laserPages.get(key);
  if (!list || !list.length) return;
  list.length = 0;
  laserVersion.value += 1;
  let alive = false;
  laserPages.forEach((l) => { if (l.length) alive = true; });
  if (alive) return;
  stopLaserClock();
  laserAlpha.value = 1;
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
