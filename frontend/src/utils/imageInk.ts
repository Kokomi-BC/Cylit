/**
 * 阅读器的「手写批注」：文档形状、命中判定与绘制。
 *
 * 坐标系：**所在页面块的归一化坐标**（0..1，左上为原点）。页面块会随字号 / 视图切换 /
 * 侧栏开合变宽变高，也会从平板换到电脑 —— 只有归一化坐标能跟着版面走
 * （与后端 `image_marks.py` 同一套约束）。
 *
 * 一个点存成 `[x, y, pressure?]`：`pressure` 是**落笔轻重**（0..1），
 * 触摸/鼠标没有笔压时由**速度**折算（写得慢＝重）。存进文档而不是渲染时再算 ——
 * 撤销/重开/换设备之后，笔迹粗细必须和画的时候一模一样。
 *
 * 画笔观感照 Apple 便签：粗细随轻重收放、圆头圆角、荧光笔是**叠加变暗**（multiply）。
 */

export type InkTool = 'hand' | 'pen' | 'marker' | 'laser' | 'select' | 'eraser';
/** 笔迹种类：普通笔 / 荧光笔（与后端 `TOOLS` 一致，别改字面量）。
 * 激光笔**故意不在这里**：它只活在内存里，永远不进批注文档（也就不必让后端认识它）。 */
export type InkKind = 'pen' | 'highlighter';
export type InkPoint = [number, number, number?];

export interface InkStroke {
  id: string;
  tool: InkKind;
  /** `#rrggbb` */
  color: string;
  /** 笔画粗细：**页面块宽度的比例**（0.004 ≈ 1000px 宽的版面上 4px） */
  w: number;
  pts: InkPoint[];
}

export interface InkNote {
  id: string;
  /** 框（归一化 `[x0,y0,x1,y1]`）：自动框住的正文行，或一处点选 */
  rect: [number, number, number, number];
  text: string;
  /** 这段字对应哪一行（`data-row-id`）：只作溯源，不参与渲染 */
  row_id?: string | null;
  /** 对应的 note 表行 id：侧栏能改/删同一条笔记（没同步成功时为 null） */
  note_id?: string | null;
  at?: string | null;
}

export interface InkDoc {
  v: 1;
  strokes: InkStroke[];
  notes: InkNote[];
}

/** 三档粗细（细 / 中 / 粗）：存的是"占版面宽度的比例"，所以窗口多宽都一样粗 */
export const WIDTH_STEPS = [
  { key: 'thin', label: '细', pen: 0.0025, marker: 0.014 },
  { key: 'mid', label: '中', pen: 0.0042, marker: 0.02 },
  { key: 'bold', label: '粗', pen: 0.0072, marker: 0.028 },
] as const;
export type WidthKey = (typeof WIDTH_STEPS)[number]['key'];

export const PEN_COLOR = '#1b1b1f';
export const MARKER_COLOR = '#ffd60a';
/** 激光笔默认色：色板第一个（红）—— 激光的用处是"指给人看"，默认就该最扎眼 */
export const LASER_DEFAULT_COLOR = '#e8384f';

/** 与后端一致的上下限（脏数据在两边都截断，谁也别信） */
export const W_MIN = 0.0005;
export const W_MAX = 0.06;

export function emptyDoc(): InkDoc {
  return { v: 1, strokes: [], notes: [] };
}

export function cloneDoc(doc: InkDoc): InkDoc {
  return {
    v: 1,
    strokes: doc.strokes.map((s) => ({ ...s, pts: s.pts.map((p) => [...p] as InkPoint) })),
    notes: doc.notes.map((n) => ({ ...n, rect: [...n.rect] as InkNote['rect'] })),
  };
}

export function hasContent(doc: InkDoc): boolean {
  return doc.strokes.length > 0 || doc.notes.length > 0;
}

export function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : Number.isFinite(v) ? v : 0;
}

/** 笔迹在屏幕上的**直径**（px）：文档存的是比例，这里按当前版面宽度换算 */
export function strokeWidthPx(stroke: InkStroke, widthPx: number): number {
  return Math.max(0.6, stroke.w * widthPx);
}

/**
 * 落笔轻重：有笔压用笔压，没有就**按速度反推**（写得慢 = 重 = 粗）。
 *
 * `speed` 单位 px/ms。阈值按触屏/鼠标的实际手感取：<0.35 几乎算静止（最粗），
 * >2.2 是快速划过（最细）。曲线开方让"中等速度"落在中间偏粗的位置 —— 线性映射
 * 会让正常书写普遍偏细，用小字写批注时几乎看不见。
 */
export function pressureOf(rawPressure: number, speed: number, isPen: boolean): number {
  if (isPen && rawPressure > 0.02) return clamp01(rawPressure);
  const t = clamp01(1 - speed / 2.2);
  return clamp01(0.22 + 0.78 * Math.sqrt(t));
}

interface Vec { x: number; y: number; r: number }

/** 半径：笔按轻重收放（0.42~1.25 倍），荧光笔恒定（它要像一根扁平的高亮条） */
function radiusAt(stroke: InkStroke, point: InkPoint, widthPx: number): number {
  const half = strokeWidthPx(stroke, widthPx) / 2;
  if (stroke.tool === 'highlighter') return half;
  const pressure = point.length > 2 ? clamp01(point[2] as number) : 0.62;
  return half * (0.42 + 0.83 * pressure);
}

function dedupe(pts: Vec[]): Vec[] {
  const out: Vec[] = [];
  for (const p of pts) {
    const last = out[out.length - 1];
    if (last && Math.hypot(p.x - last.x, p.y - last.y) < 0.35) {
      // 同一点反复上报（触摸抖动）：保留半径更大的那次，别让粗细跳变
      if (p.r > last.r) last.r = p.r;
      continue;
    }
    out.push({ ...p });
  }
  return out;
}

/** 半径做一次滑动平均：真机上压力读数有明显毛刺，不平滑会画出一串小鼓包 */
function smoothRadius(pts: Vec[]): void {
  const radii = pts.map((p) => p.r);
  for (let i = 0; i < pts.length; i += 1) {
    let sum = 0;
    let count = 0;
    for (let k = -1; k <= 1; k += 1) {
      const r = radii[i + k];
      if (r !== undefined) { sum += r; count += 1; }
    }
    pts[i].r = sum / count;
  }
}

/**
 * 画一笔：把点串铺成一条**两侧带宽度**的带子（填充多边形）+ 两端圆头。
 *
 * 为什么不 `lineTo` 描边：描边的线宽对整条线是恒定的，画不出"按压力收放"的笔锋；
 * 便签的手感正来自这个收放，所以自己算轮廓。
 */
export function paintStroke(ctx: CanvasRenderingContext2D, stroke: InkStroke,
                            widthPx: number, heightPx: number): void {
  // x 用宽、y 用高（版面不是正方形，两个方向不能共用一个倍数）；半径按宽度的比例走
  const raw = stroke.pts.map((p) => ({
    x: clamp01(p[0]) * widthPx,
    y: clamp01(p[1]) * heightPx,
    r: radiusAt(stroke, p, widthPx),
  }));
  const pts = dedupe(raw);
  if (pts.length < 2) return;
  smoothRadius(pts);

  const isMarker = stroke.tool === 'highlighter';
  ctx.save();
  ctx.fillStyle = stroke.color;
  ctx.globalAlpha = isMarker ? 0.36 : 1;

  const left: Vec[] = [];
  const right: Vec[] = [];
  for (let i = 0; i < pts.length; i += 1) {
    const prev = pts[Math.max(0, i - 1)];
    const next = pts[Math.min(pts.length - 1, i + 1)];
    const dx = next.x - prev.x;
    const dy = next.y - prev.y;
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len;
    const ny = dx / len;
    left.push({ x: pts[i].x + nx * pts[i].r, y: pts[i].y + ny * pts[i].r, r: 0 });
    right.push({ x: pts[i].x - nx * pts[i].r, y: pts[i].y - ny * pts[i].r, r: 0 });
  }

  ctx.beginPath();
  ctx.moveTo(left[0].x, left[0].y);
  for (let i = 1; i < left.length; i += 1) ctx.lineTo(left[i].x, left[i].y);
  for (let i = right.length - 1; i >= 0; i -= 1) ctx.lineTo(right[i].x, right[i].y);
  ctx.closePath();
  ctx.fill();

  // 圆头：荧光笔要"一根直条"，所以只有普通笔加圆头
  if (!isMarker) {
    for (const p of [pts[0], pts[pts.length - 1]]) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  ctx.restore();
}

/** 激光笔的"白心"占整笔宽度的比例（用户口径 2026-12-14：中间白、两端彩色，像照在纸上的一束光） */
const LASER_CORE_RATIO = 0.42;

/**
 * 画一笔激光：**两遍**画同一条带子 —— 先彩色（带一圈柔光），再缩到 42% 宽盖一层白心。
 *
 * 为什么是两遍而不是描边：paintStroke 是自己算轮廓填充的（粗细随落笔轻重收放），
 * 描边只能在恒宽折线上做，会把这层手感抹掉。同一笔用两个宽度叠画，收放完全一致。
 * 柔光仍走 shadowBlur（0 偏移）：激光要像"照在纸上的一束光"，本体亮、四周一圈同色辉光。
 */
export function paintLaserStroke(ctx: CanvasRenderingContext2D, stroke: InkStroke,
                                 widthPx: number, heightPx: number): void {
  ctx.save();
  ctx.shadowColor = stroke.color;
  ctx.shadowBlur = Math.max(2, strokeWidthPx(stroke, widthPx) * 2.5);
  ctx.shadowOffsetX = 0;
  ctx.shadowOffsetY = 0;
  paintStroke(ctx, stroke, widthPx, heightPx);
  ctx.shadowBlur = 0;      /* shadow 是画布状态：留着会把后面画的一切一起糊掉 */
  ctx.restore();
  /* 白心：不发光、纯白、窄一圈 —— 两侧留下的就是那 42% 的彩色边 */
  ctx.save();
  paintStroke(ctx, { ...stroke, w: stroke.w * LASER_CORE_RATIO, color: '#ffffff' }, widthPx, heightPx);
  ctx.restore();
}

/** 画整份文档（不含正在画的那一笔） */
export function paintDoc(ctx: CanvasRenderingContext2D, doc: InkDoc,
                         widthPx: number, heightPx: number): void {
  for (const s of doc.strokes) paintStroke(ctx, s, widthPx, heightPx);
}

function distToSegment(px: number, py: number, ax: number, ay: number,
                       bx: number, by: number): number {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / len2;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

/**
 * 橡皮命中：返回被擦到的笔迹下标（没有则 -1）。
 *
 * 便签的橡皮是**整笔删除**（不是擦掉一小段）：撤销栈按"整笔"记，用户也更容易理解
 * "擦一下少一笔"。命中半径加一点宽容量，手指比笔尖粗。
 */
export function hitStroke(doc: InkDoc, nx: number, ny: number,
                          radiusPx: number, widthPx: number, heightPx: number): number {
  const px = clamp01(nx) * widthPx;
  const py = clamp01(ny) * heightPx;
  for (let i = doc.strokes.length - 1; i >= 0; i -= 1) {
    const s = doc.strokes[i];
    const tol = strokeWidthPx(s, widthPx) / 2 + radiusPx;
    const pts = s.pts;
    for (let k = 1; k < pts.length; k += 1) {
      const ax = clamp01(pts[k - 1][0]) * widthPx;
      const ay = clamp01(pts[k - 1][1]) * heightPx;
      const bx = clamp01(pts[k][0]) * widthPx;
      const by = clamp01(pts[k][1]) * heightPx;
      if (distToSegment(px, py, ax, ay, bx, by) <= tol) return i;
    }
  }
  return -1;
}
