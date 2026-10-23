<script setup lang="ts">
/**
 * 模型分区 · **消耗统计**（三个数字 + 多系列折线 + 分模型排行）。
 *
 * ## 为什么不再是卡片
 * 用户口径（2026-12）：**折线图移除卡片样式**。这里与「字体设置 / 主题预设 / 高亮效果」
 * 那些裸块同一个观感：区块标题 + 内容，只用留白与 1px 细线分区，不描边、不投影、不填底。
 * 唯独 `grid-column: 1 / -1` 保留：折线需要横向像素，落在两列网格里也必须独占一行。
 *
 * ## 数据契约（后端 GET /models/usage/stats，多系列）
 * · `window.points` 与每条 `series.points` **逐位对齐**（24 / 7 / 14 / 30 个点）；
 * · `cost` 是 **{币种: 金额} 字典** —— 人民币与美元**永不相加**，费用视图按币种画多条线；
 * · 缺价格的调用只进 `unpriced`（"不知道多少钱"），**不是 0 元**，所以
 *   "全都没配价"时费用视图给一句话而不是一条 0 线（0 线会被读成"花了 0 元"）；
 * · 筛选走**查询参数**（vendor / model / group），后端 `filters` 永远是窗口内全量，
 *   所以下拉框不会把自己筛没。
 *
 * ## 画法（手写内联 SVG，不引图表库）
 * 坐标系用**像素**（容器宽度由 ResizeObserver 实测 → viewBox 与 CSS 像素 1:1），
 * 描边不被拉伸，hover 的 x→桶映射就是一次除法。纵轴从 0 起，刻度按"好看数"取整；
 * 系列颜色由 `series.key` 哈希进一张固定调色板：**同一台机器刷新不变色**。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '@/api/client';
import type { ModelUsageStats, UsageFilterModel, UsagePoint, UsageSeries } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';

type WindowKind = '24h' | '7d' | '14d' | '30d';
type ChartMode = 'usage' | 'token' | 'cost' | 'requests' | 'vendor';
/** 折线画的是哪个量：用量(tokens_total) / 费用 / 请求数。token 模式仍按"用量"取总量。 */
type Metric = 'usage' | 'cost' | 'requests';

const WINDOWS: Array<{ id: WindowKind; label: string }> = [
  { id: '24h', label: '24 小时' },
  { id: '7d', label: '7 天' },
  { id: '14d', label: '14 天' },
  { id: '30d', label: '30 天' },
];

const MODES: Array<{ id: ChartMode; label: string; hint: string }> = [
  { id: 'usage', label: '用量', hint: '各系列总 Token（面积 + 折线）' },
  { id: 'token', label: 'Token', hint: '同一批系列拆成输入 / 输出两层堆叠' },
  { id: 'cost', label: '费用', hint: '按币种画线：人民币与美元各一条，绝不相加' },
  { id: 'requests', label: '请求', hint: '各系列请求次数' },
  { id: 'vendor', label: '厂商对比', hint: '按厂商聚合（group=vendor）' },
];

/* ---------------------------- 查询状态 ---------------------------- */

const stats = ref<ModelUsageStats | null>(null);
const loading = ref(false);
const win = ref<WindowKind>('7d');
const mode = ref<ChartMode>('usage');
const vendorSel = ref('all');
const modelSel = ref<string[]>([]);
/** 「厂商对比」的子切换：厂商既可能比用量、也可能比费用 */
const vendorMetric = ref<'usage' | 'cost'>('usage');
/** 被图例点掉的系列 / 币种（前端隐藏，不影响后端查询；换窗口或改筛选时重置） */
const hiddenSeries = ref<string[]>([]);
const hiddenCur = ref<string[]>([]);

const group = computed<'model' | 'vendor'>(() => (mode.value === 'vendor' ? 'vendor' : 'model'));

const metric = computed<Metric>(() => {
  if (mode.value === 'vendor') return vendorMetric.value === 'cost' ? 'cost' : 'usage';
  if (mode.value === 'cost') return 'cost';
  if (mode.value === 'requests') return 'requests';
  return 'usage';
});
const costMode = computed<boolean>(() => metric.value === 'cost');

let seq = 0;
async function load(): Promise<void> {
  const token = ++seq;
  loading.value = true;
  try {
    const data = await api.get<ModelUsageStats>('/models/usage/stats', {
      window: win.value,
      vendor: vendorSel.value || 'all',
      model: group.value === 'vendor' ? 'all' : (modelSel.value.length ? modelSel.value.join(',') : 'all'),
      group: group.value,
    });
    if (token !== seq) return;
    stats.value = data && data.window && Array.isArray(data.series) ? data : null;
    /* 换了窗口 / 筛选 / 分组：上一批隐藏的系列已经不存在了，图例一律复位成"全显示" */
    hiddenSeries.value = [];
    hiddenCur.value = [];
  } catch {
    if (token !== seq) return;
    stats.value = null;   // 接口未就绪 / 网络失败：只是没数，不是错误
  } finally {
    if (token === seq) loading.value = false;
  }
}

function resetAndLoad(): void {
  hiddenSeries.value = [];
  hiddenCur.value = [];
  void load();
}

watch([win, group, modelSel], resetAndLoad, { deep: true });
watch(vendorSel, () => {
  /* 换厂商后旧选择可能不属于这家：先收窄，再查（否则查询参数会把结果筛成空） */
  const allowed = new Set(vendorModels.value.map((m) => m.key));
  if (vendorSel.value !== 'all') modelSel.value = modelSel.value.filter((k) => allowed.has(k));
  resetAndLoad();
});
defineExpose({ reload: load });

/* ---------------------------- 数字格式 ---------------------------- */

function trim1(v: number): string {
  const s = (Math.round(v * 10) / 10).toFixed(1);
  return s.endsWith('.0') ? s.slice(0, -2) : s;
}

/** 万 / 亿 的紧凑写法（12400 → 1.2万）：数字块与坐标轴共用，大数不撑破格子。 */
function fmtN(v: number): string {
  const n = Number(v) || 0;
  if (n >= 1e8) return trim1(n / 1e8) + '亿';
  if (n >= 1e4) return trim1(n / 1e4) + '万';
  return String(Math.round(n));
}

const CURRENCY_SYMBOL: Record<string, string> = { CNY: '¥', USD: '$', EUR: '€', JPY: '¥' };

function symOf(cur: string): string {
  const code = String(cur || '').toUpperCase();
  return CURRENCY_SYMBOL[code] || code + ' ';
}

/** 小数位补够了再削掉多余的 0（0.050 → 0.05）：小额不要四舍五入成 ¥0.00（那是假账），
 *  也别留一串没意义的尾零。 */
function trimZeros(s: string): string {
  return s.includes('.') ? s.replace(/(\.\d\d\d*?)0+$/, '$1') : s;
}

/** 金额：小于 1 元给 3 位、更小给 4 位 —— 小额调用不要被四舍五入成 ¥0.00（那是假账）。 */
function fmtMoney(v: number): string {
  const n = Number(v) || 0;
  if (!n) return '0';
  if (n >= 1e6) return trim1(n / 1e6) + 'M';
  if (n >= 1e4) return trim1(n / 1e3) + 'k';
  if (n >= 1000) return String(Math.round(n));
  if (n >= 100) return n.toFixed(1);
  if (n >= 1) return n.toFixed(2);
  if (n >= 0.1) return trimZeros(n.toFixed(2));
  if (n >= 0.01) return trimZeros(n.toFixed(3));
  return trimZeros(n.toFixed(4));
}

/** 纵轴金额刻度：小数位数**由步长决定**（步长 0.5 → 两位），否则同一根轴上
 *  会出现 "2.00 / 0.500 / 0" 这种参差不齐的写法。 */
function fmtAxisMoney(v: number, step: number): string {
  const d = step >= 1 ? (Number.isInteger(step) ? 0 : 2) : (step >= 0.1 ? 2 : (step >= 0.01 ? 3 : 4));
  return (Number(v) || 0).toFixed(d);
}

/** 计数类（请求数）的刻度必须落在整数上：0.5 次请求没有意义。 */
const COUNT_STEPS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000];

function countScale(max: number): { max: number; step: number } {
  const want = Math.max(0, Number(max) || 0) / 4;
  const step = COUNT_STEPS.find((s) => s >= want) || Math.ceil(want);
  return { max: step * 4, step };
}

function splitN(v: number): { n: string; u: string } {
  const n = Number(v) || 0;
  if (n >= 1e8) return { n: trim1(n / 1e8), u: '亿' };
  if (n >= 1e4) return { n: trim1(n / 1e4), u: '万' };
  return { n: String(Math.round(n)), u: '' };
}

/* ---------------------------- 调色板 ---------------------------- */

/** 8 色固定调色板（青绿 / 橙 / 紫 / 绿 / 蓝 / 红 / 琥珀 / 洋红），按 series.key 哈希取色。 */
const SERIES_HUES = ['#0f7b8a', '#c2410c', '#6d5bd0', '#2f8f46', '#0f6fbf', '#b3261e', '#a06a00', '#a03e8c'];

function hashKey(key: string): number {
  let h = 5381;
  for (let i = 0; i < key.length; i += 1) h = ((h << 5) + h + key.charCodeAt(i)) >>> 0;
  return h;
}

/** color-mix 把色相往正文色上拉 24%：浅色主题下自动加深、深色主题下自动提亮，
 *  换皮肤（含 night 这类深色皮肤）不需要再维护第二套调色板。 */
function mix(hue: string): string {
  return 'color-mix(in srgb, ' + hue + ' 76%, var(--text))';
}

function seriesColor(key: string): string {
  return mix(SERIES_HUES[hashKey(String(key)) % SERIES_HUES.length]);
}

/** 币种色固定（CNY 青绿 / USD 紫），与系列色同源但互不干扰：费用视图里币种就是"系列"。 */
function currencyColor(cur: string): string {
  const code = String(cur || '').toUpperCase();
  if (code === 'CNY') return mix(SERIES_HUES[0]);
  if (code === 'USD') return mix(SERIES_HUES[2]);
  return mix(SERIES_HUES[1]);
}

/* ---------------------------- 数据视图 ---------------------------- */

const points = computed(() => stats.value?.window?.points ?? []);
const granularity = computed<'hour' | 'day'>(() => (stats.value?.window?.granularity === 'hour' ? 'hour' : 'day'));
const allSeries = computed<UsageSeries[]>(() => stats.value?.series ?? []);
const nPts = computed<number>(() => points.value.length);
const viewBox = computed<string>(() => '0 0 ' + boxW.value + ' ' + H);

const visibleSeries = computed<UsageSeries[]>(() => allSeries.value.filter((s) => !hiddenSeries.value.includes(s.key)));

const hasRecords = computed<boolean>(() =>
  allSeries.value.some((s) => (Number(s.totals?.requests) || 0) > 0 || (Number(s.totals?.tokens_total) || 0) > 0));

/** 窗口内一条记录都没有时用哪句话：全空库 / 被筛选筛空是两回事。 */
const emptyText = computed<string>(() =>
  (stats.value?.filters?.models?.length ?? 0) > 0 ? '该筛选条件下没有调用记录' : '该时间范围没有调用记录');

/** 有金额的币种（费用视图的"系列"）。顺序固定：CNY → USD → 其余按字母。 */
const currencies = computed<string[]>(() => {
  const set = new Set<string>();
  for (const s of visibleSeries.value) {
    for (const c of Object.keys(s.totals?.cost ?? {})) set.add(c);
  }
  const rank = (c: string): number => (c === 'CNY' ? 0 : c === 'USD' ? 1 : 2);
  return [...set].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
});

const activeCurrencies = computed<string[]>(() => currencies.value.filter((c) => !hiddenCur.value.includes(c)));

/** 这个币种在窗口内（或某个桶）真正算出来的钱：全 0 时不画线，见文件头的"不画 0 线"。 */
function costAt(idx: number | null, cur: string): number {
  let total = 0;
  for (const s of visibleSeries.value) {
    const v = idx == null ? s.totals?.cost?.[cur] : s.points?.[idx]?.cost?.[cur];
    total += Number(v) || 0;
  }
  return Math.round(total * 1e6) / 1e6;
}

const costSum = computed<number>(() => activeCurrencies.value.reduce((a, c) => a + costAt(null, c), 0));

/** 缺价调用次数（只算可见系列）：这是"算不出钱"的量，与"0 元"不同。 */
const unpricedCount = computed<number>(() =>
  visibleSeries.value.reduce((a, s) => a + (Number(s.totals?.unpriced) || 0), 0));

const costEmpty = computed<boolean>(() => costMode.value && hasRecords.value && costSum.value <= 0 && unpricedCount.value > 0);
const unpricedNote = computed<string>(() =>
  (costMode.value && costSum.value > 0 && unpricedCount.value > 0)
    ? unpricedCount.value + ' 次调用未计费'
    : '');

function sumMetric(idx: number | null, m: Metric): number {
  let total = 0;
  for (const s of visibleSeries.value) {
    if (idx == null) total += m === 'requests' ? Number(s.totals?.requests) || 0 : Number(s.totals?.tokens_total) || 0;
    else {
      const p = s.points?.[idx];
      total += m === 'requests' ? Number(p?.requests) || 0 : Number(p?.tokens_total) || 0;
    }
  }
  return total;
}

function sumTokens(idx: number | null, field: 'tokens_in' | 'tokens_out'): number {
  let total = 0;
  for (const s of visibleSeries.value) {
    const v = idx == null ? s.totals?.[field] : s.points?.[idx]?.[field];
    total += Number(v) || 0;
  }
  return total;
}

/* ---------------------------- 筛选下拉 ---------------------------- */

const vendors = computed(() => stats.value?.filters?.vendors ?? []);
const allModels = computed<UsageFilterModel[]>(() => stats.value?.filters?.models ?? []);

/** 模型选项随厂商收窄：filters.vendors[].models 就是这家在窗口内出现过的身份键。 */
const vendorModels = computed<UsageFilterModel[]>(() => {
  if (vendorSel.value === 'all') return allModels.value;
  const v = vendors.value.find((x) => x.id === vendorSel.value);
  if (!v) return allModels.value;
  const keys = new Set(v.models);
  return allModels.value.filter((m) => keys.has(m.key));
});

const modelOpen = ref(false);
const modelBtnText = computed<string>(() => {
  const n = modelSel.value.length;
  if (!n) return '全部模型';
  if (n === 1) {
    const one = allModels.value.find((m) => m.key === modelSel.value[0]);
    return one?.label || '1 个模型';
  }
  return n + ' 个模型';
});

function toggleModelPop(): void {
  if (group.value === 'vendor') return;
  modelOpen.value = !modelOpen.value;
}

function toggleModel(key: string): void {
  modelSel.value = modelSel.value.includes(key)
    ? modelSel.value.filter((k) => k !== key)
    : [...modelSel.value, key];
}

function onDocDown(ev: MouseEvent): void {
  if (!modelOpen.value) return;
  const el = ev.target as HTMLElement | null;
  if (el && el.closest('[data-model-pop]')) return;
  if (el && el.closest('[data-model-btn]')) return;
  modelOpen.value = false;
}

/* ---------------------------- 几何 ---------------------------- */

/** 图表高度：比原来那 100px 高一点（多了纵轴刻度与多系列图例），仍是紧凑档。 */
const H = 132;
const padL = 46;   // 纵轴刻度
const padR = 8;
const padT = 10;
const padB = 16;
const innerH = H - padT - padB;
const baseY = padT + innerH;

const box = ref<HTMLDivElement | null>(null);
const boxW = ref(560);
let ro: ResizeObserver | null = null;

function measure(): void {
  const el = box.value;
  if (el) boxW.value = Math.max(240, Math.round(el.clientWidth) || 560);
}

const innerW = computed<number>(() => Math.max(1, boxW.value - padL - padR));

function xAt(i: number): number {
  const n = nPts.value;
  if (n <= 1) return padL + innerW.value / 2;
  return padL + (innerW.value * i) / (n - 1);
}

function pointValue(p: UsagePoint | undefined, m: Metric): number {
  if (!p) return 0;
  if (m === 'requests') return Number(p.requests) || 0;
  return Number(p.tokens_total) || 0;
}

/** 峰值 → "好看"的刻度上限（4 等分），全 0 时取 1，于是线正好贴基线而不是 NaN。 */
const scale = computed<{ max: number; step: number }>(() => {
  let m = 0;
  const n = nPts.value;
  if (costMode.value) {
    for (const c of activeCurrencies.value) for (let i = 0; i < n; i += 1) m = Math.max(m, costAt(i, c));
  } else if (mode.value === 'token') {
    for (const s of visibleSeries.value) {
      for (const p of s.points || []) m = Math.max(m, (Number(p.tokens_in) || 0) + (Number(p.tokens_out) || 0));
    }
  } else {
    for (const s of visibleSeries.value) {
      for (const p of s.points || []) m = Math.max(m, pointValue(p, metric.value));
    }
  }
  if (!(m > 0)) return { max: 1, step: 0.25 };
  if (metric.value === 'requests') return countScale(m);
  const raw = m / 4;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  return { max: step * 4, step };
});

function yAt(v: number): number {
  const max = scale.value.max || 1;
  return padT + innerH * (1 - Math.max(0, Number(v) || 0) / max);
}

/* ---------------------------- 图层 ---------------------------- */

interface DrawLayer {
  id: string;
  color: string;
  vals: number[];
  line: string;
  area: string;
  fill: number;
  w: number;
}

function buildLayer(id: string, color: string, vals: number[], fill: number, w: number): DrawLayer {
  const steps: string[] = [];
  vals.forEach((v, i) => {
    steps.push((i ? 'L' : 'M') + xAt(i).toFixed(2) + ' ' + yAt(v).toFixed(2));
  });
  const line = steps.join(' ');
  let area = '';
  if (fill > 0 && vals.length) {
    const base = baseY.toFixed(2);
    const body = steps.map((s) => 'L' + s.slice(1)).join('');
    area = 'M' + xAt(0).toFixed(2) + ' ' + base + body
      + 'L' + xAt(vals.length - 1).toFixed(2) + ' ' + base + 'Z';
  }
  return { id, color, vals, line, area, fill, w };
}

const layers = computed<DrawLayer[]>(() => {
  const n = nPts.value;
  if (!n) return [];
  const out: DrawLayer[] = [];
  if (costMode.value) {
    for (const cur of activeCurrencies.value) {
      const vals = Array.from({ length: n }, (_, i) => costAt(i, cur));
      if (!vals.some((v) => v > 0)) continue;   // 全 0：不画（那是"没算出来"，不是"0 元"）
      out.push(buildLayer('cur:' + cur, currencyColor(cur), vals, 0, 1.7));
    }
    return out;
  }
  if (mode.value === 'token') {
    for (const s of visibleSeries.value) {
      const col = seriesColor(s.key);
      const ins = (s.points || []).map((p) => Number(p.tokens_in) || 0);
      const stack = (s.points || []).map((p) => (Number(p.tokens_in) || 0) + (Number(p.tokens_out) || 0));
      out.push(buildLayer(s.key + ':in', col, ins, 0.3, 1.2));
      out.push(buildLayer(s.key + ':out', col, stack, 0.14, 1.2));
    }
    return out;
  }
  for (const s of visibleSeries.value) {
    const vals = (s.points || []).map((p) => pointValue(p, metric.value));
    out.push(buildLayer(s.key, seriesColor(s.key), vals, mode.value === 'usage' ? 0.18 : 0, 1.7));
  }
  return out;
});

/** 纵轴刻度（5 条，含 0）：文案随单位变 —— Token 用万/亿、费用带币种符号、请求数就是次数。 */
const yTicks = computed<Array<{ y: number; label: string; v: number }>>(() => {
  const { step } = scale.value;
  const single = costMode.value && activeCurrencies.value.length === 1 ? activeCurrencies.value[0] : '';
  const list: Array<{ y: number; label: string; v: number }> = [];
  for (let i = 0; i <= 4; i += 1) {
    const v = step * i;
    let label: string;
    if (costMode.value) label = (single ? symOf(single) : '') + fmtAxisMoney(v, step);
    else label = fmtN(v);
    list.push({ y: yAt(v), label, v });
  }
  return list.reverse();
});

/** x 轴只标 起 / 中 / 止（hour → HH:00，day → MM/DD，后端 label 已经是这个形状）。 */
const xTicks = computed<Array<{ i: number; x: number; label: string; anchor: string }>>(() => {
  const n = nPts.value;
  if (!n) return [];
  const idx = n <= 3 ? points.value.map((_, i) => i) : [...new Set([0, Math.floor((n - 1) / 2), n - 1])];
  return idx.map((i) => ({
    i,
    x: xAt(i),
    label: tickLabel(points.value[i]?.label || '', points.value[i]?.key || ''),
    anchor: i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle'),
  }));
});

function tickLabel(label: string, key: string): string {
  if (label) return label;
  if (granularity.value === 'hour') return String(key).slice(11, 13) + ':00';
  const s = String(key);
  return s.length >= 10 ? s.slice(5, 7) + '/' + s.slice(8, 10) : s;
}

/** 纵轴单位文案（图表左上角说明 + 验收要看的东西）。 */
const unitText = computed<string>(() => {
  if (costMode.value) {
    const list = activeCurrencies.value;
    if (!list.length) return '金额';
    return list.join(' / ');
  }
  if (mode.value === 'requests') return '次请求';
  return 'Token';
});

/* ---------------------------- hover ---------------------------- */

const hoverIdx = ref(-1);

function onMove(ev: MouseEvent): void {
  const el = box.value;
  const n = nPts.value;
  if (!el || !n) return;
  const r = el.getBoundingClientRect();
  // 容器被压窄时 svg 是等比缩放的，先把鼠标位置换算回坐标系像素
  const mx = (ev.clientX - r.left) * (r.width ? boxW.value / r.width : 1);
  const i = n <= 1 ? 0 : Math.round(((mx - padL) / innerW.value) * (n - 1));
  hoverIdx.value = Math.max(0, Math.min(n - 1, i));
}

function onLeave(): void { hoverIdx.value = -1; }

const hoverActive = computed<boolean>(() => hoverIdx.value >= 0 && nPts.value > 0);
const hoverX = computed<number>(() => xAt(hoverIdx.value));

const hoverLabel = computed<string>(() => {
  const p = points.value[hoverIdx.value];
  if (!p) return '';
  return granularity.value === 'hour' ? p.key + ':00' : p.key;
});

interface HoverRow {
  key: string;
  label: string;
  sub: string;
  /** 这一条背后合并了几个模型配置（>1 才在浮层里点出来，见文件头「同名即合并」） */
  merged: number;
  color: string;
  req: string;
  tin: string;
  tout: string;
  total: string;
  cost: string;
  sort: number;
}

const hoverRows = computed<HoverRow[]>(() => {
  const idx = hoverIdx.value;
  if (idx < 0) return [];
  const rows: HoverRow[] = visibleSeries.value.map((s) => {
    const cell = s.points?.[idx];
    const costDict = cell?.cost ?? {};
    const codes = Object.keys(costDict);
    const costText = codes.length
      ? codes.map((c) => symOf(c) + fmtMoney(Number(costDict[c]) || 0)).join(' / ')
      : ((Number(cell?.unpriced) || 0) > 0 ? '未计费' : '—');
    const sortVal = costMode.value
      ? Number(costDict[activeCurrencies.value[0]] || 0)
      : pointValue(cell, metric.value);
    return {
      key: s.key,
      label: s.label,
      /* group=vendor 时厂商名就在 label 里，副标题不再编一个假模型名 */
      sub: group.value === 'vendor'
        ? (s.kind === 'vendor' ? '厂商合计' : '厂商')
        : (s.provider_label || s.provider || ''),
      merged: (s.model_config_ids?.length ?? 0),
      color: seriesColor(s.key),
      req: fmtN(Number(cell?.requests) || 0),
      tin: fmtN(Number(cell?.tokens_in) || 0),
      tout: fmtN(Number(cell?.tokens_out) || 0),
      total: fmtN(Number(cell?.tokens_total) || 0),
      cost: costText,
      sort: Number(sortVal) || 0,
    };
  });
  return rows.sort((a, b) => b.sort - a.sort || a.label.localeCompare(b.label));
});

/* ---------------------------- 图例 ---------------------------- */

interface LegendItem {
  id: string;
  label: string;
  color: string;
  hidden: boolean;
  kind: 'series' | 'currency';
  text: string;
  /** 合并了几个配置（>1 时图标后面缀一个 ×N，见文件头「同名即合并」） */
  merged?: number;
}

const legendItems = computed<LegendItem[]>(() => {
  const out: LegendItem[] = [];
  if (costMode.value) {
    for (const c of currencies.value) {
      out.push({
        id: 'cur:' + c, label: c, color: currencyColor(c),
        hidden: hiddenCur.value.includes(c), kind: 'currency', text: symOf(c) + fmtMoney(costAt(null, c)),
      });
    }
  }
  for (const s of allSeries.value) {
    out.push({
      id: s.key, label: displayName(s), color: seriesColor(s.key),
      hidden: hiddenSeries.value.includes(s.key), kind: 'series', text: '',
      merged: (s.model_config_ids?.length ?? 0),
    });
  }
  return out;
});

function toggleLegend(item: LegendItem): void {
  if (item.kind === 'currency') {
    const code = item.id.slice(4);
    hiddenCur.value = hiddenCur.value.includes(code)
      ? hiddenCur.value.filter((c) => c !== code)
      : [...hiddenCur.value, code];
    return;
  }
  hiddenSeries.value = hiddenSeries.value.includes(item.id)
    ? hiddenSeries.value.filter((k) => k !== item.id)
    : [...hiddenSeries.value, item.id];
}

function showAll(): void {
  hiddenSeries.value = [];
  hiddenCur.value = [];
}

/* ---------------------------- 三个数字 ---------------------------- */

interface NumBlock { k: string; label: string; n: string; u: string; sub: string }

const todayLabel = computed<string>(() => (granularity.value === 'hour' ? '最近 1 小时' : '今日'));

const blocks = computed<NumBlock[]>(() => {
  const last = nPts.value - 1;
  if (costMode.value) {
    const fmt = (pick: (c: string) => number): { n: string; u: string } => {
      const parts = activeCurrencies.value
        .map((c) => ({ c, v: pick(c) }))
        .filter((p) => p.v > 0);
      if (!parts.length) return { n: '未计费', u: '' };
      const first = parts[0];
      const rest = parts.slice(1);
      return {
        n: symOf(first.c) + fmtMoney(first.v),
        u: rest.length ? ' / ' + rest.map((p) => symOf(p.c) + fmtMoney(p.v)).join(' / ') : '',
      };
    };
    const total = fmt((c) => costAt(null, c));
    const today = last >= 0 ? fmt((c) => costAt(last, c)) : { n: '—', u: '' };
    const req = splitN(sumMetric(null, 'requests'));
    return [
      { k: 'total', label: '费用合计', n: total.n, u: total.u, sub: unpricedCount.value ? unpricedCount.value + ' 次未计费' : '' },
      { k: 'today', label: todayLabel.value, n: today.n, u: today.u, sub: '' },
      { k: 'requests', label: '请求数', n: req.n, u: req.u, sub: '' },
    ];
  }
  const m = metric.value;
  const tot = splitN(sumMetric(null, m));
  const day = last >= 0 ? splitN(sumMetric(last, m)) : { n: '—', u: '' };
  const req = splitN(sumMetric(null, 'requests'));
  return [
    {
      k: 'total',
      label: mode.value === 'requests' ? '请求总量' : (mode.value === 'token' ? 'Token 总量' : '总量'),
      n: tot.n, u: tot.u,
      sub: mode.value === 'token'
        ? '入 ' + fmtN(sumTokens(null, 'tokens_in')) + ' · 出 ' + fmtN(sumTokens(null, 'tokens_out'))
        : '',
    },
    { k: 'today', label: todayLabel.value, n: day.n, u: day.u, sub: '' },
    { k: 'requests', label: '请求数', n: req.n, u: req.u, sub: '' },
  ];
});

/* ---------------------------- 分模型排行 ---------------------------- */

interface RankRow { key: string; name: string; text: string; share: number; unpriced: number }

/** 系列身份是「自定义名称 + 供应商」，所以**同名不同供应商会并存两条**。
 *  出现重名时把供应商缀在名字后面（只在真的重名时缀，避免每条都拖一截尾巴）；
 *  图例与排行共用这一套，两边看到的叫法必须一致。 */
const labelCounts = computed<Record<string, number>>(() => {
  const out: Record<string, number> = {};
  for (const s of allSeries.value) out[s.label] = (out[s.label] || 0) + 1;
  return out;
});
function displayName(s: { label: string; provider_label?: string; provider?: string | null }): string {
  const dup = (labelCounts.value[s.label] || 0) > 1;
  if (!dup) return s.label;
  const prov = s.provider_label || s.provider || '';
  return prov ? s.label + ' · ' + prov : s.label;
}

const rankAll = computed<RankRow[]>(() => {
  const rows = visibleSeries.value.map((s) => {
    let value = 0;
    let text = '';
    if (costMode.value) {
      const codes = activeCurrencies.value.filter((c) => (Number(s.totals?.cost?.[c]) || 0) > 0);
      text = codes.length
        ? codes.map((c) => symOf(c) + fmtMoney(Number(s.totals?.cost?.[c]) || 0)).join(' / ')
        : '未计费';
      value = Number(s.totals?.cost?.[activeCurrencies.value[0]]) || 0;
    } else if (metric.value === 'requests') {
      value = Number(s.totals?.requests) || 0;
      text = fmtN(value);
    } else {
      value = Number(s.totals?.tokens_total) || 0;
      text = fmtN(value);
    }
    return {
      key: s.key, name: displayName(s), text, value,
      unpriced: costMode.value ? (Number(s.totals?.unpriced) || 0) : 0,
    };
  }).filter((r) => r.value > 0 || r.unpriced > 0);
  rows.sort((a, b) => b.value - a.value || a.name.localeCompare(b.name));
  const sum = rows.reduce((s, r) => s + r.value, 0);
  return rows.map((r) => ({ key: r.key, name: r.name, text: r.text, share: sum ? r.value / sum : 0, unpriced: r.unpriced }));
});

const RANK_LIMIT = 4;
const rankExpanded = ref(false);
const rankVisible = computed(() => (rankExpanded.value ? rankAll.value : rankAll.value.slice(0, RANK_LIMIT)));
const rankHidden = computed(() => Math.max(0, rankAll.value.length - RANK_LIMIT));

const rankTitle = computed<string>(() => {
  if (costMode.value) return group.value === 'vendor' ? '分厂商费用' : '分模型费用';
  if (mode.value === 'requests') return group.value === 'vendor' ? '分厂商请求' : '分模型请求';
  return group.value === 'vendor' ? '分厂商消耗' : '分模型消耗';
});

/* ---------------------------- 其它 ---------------------------- */

const headDesc = computed<string>(() => {
  const w = WINDOWS.find((x) => x.id === win.value)?.label || win.value;
  if (group.value === 'vendor') return '近 ' + w + ' · 按厂商聚合';
  return '近 ' + w + (modelSel.value.length ? ' · 已筛模型' : '');
});

const chartLabel = computed<string>(() =>
  '近 ' + win.value + ' 的' + (group.value === 'vendor' ? '厂商' : '模型') + unitText.value + '趋势');

/** "去模型配置里填单价"：真的滚到下面那块（不是假按钮），找不到就什么都不做。 */
function gotoPricing(): void {
  const el = document.querySelector('.types-block') || document.querySelector('[data-type-card]');
  if (el instanceof HTMLElement) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

onMounted(() => {
  document.addEventListener('mousedown', onDocDown);
  measure();
  if (typeof ResizeObserver === 'undefined' || !box.value) return;
  ro = new ResizeObserver(measure);
  ro.observe(box.value);
});

onBeforeUnmount(() => {
  ro?.disconnect();
  ro = null;
  document.removeEventListener('mousedown', onDocDown);
});
</script>

<template>
  <section
    class="usage" data-usage-chart
    :data-mode="mode" :data-window="win" :data-group="group" :data-vendor="vendorSel"
  >
    <header class="usage-head">
      <h3 class="usage-title"><AppIcon name="gauge" :size="14" /><span>消耗统计</span></h3>
      <p class="usage-desc">{{ headDesc }}</p>
      <span class="spacer"></span>
      <div class="seg modes" role="group" aria-label="图表类型">
        <button
          v-for="m in MODES" :key="m.id" type="button"
          :class="{ active: mode === m.id }" :data-mode-btn="m.id"
          :title="m.hint" :aria-pressed="mode === m.id"
          @click="mode = m.id"
        >{{ m.label }}</button>
      </div>
      <div class="seg win" role="group" aria-label="时间范围">
        <button
          v-for="w in WINDOWS" :key="w.id" type="button"
          :class="{ active: win === w.id }" :data-window-btn="w.id"
          @click="win = w.id"
        >{{ w.label }}</button>
      </div>
    </header>

    <div class="usage-filters">
      <label class="fld">
        <span class="fld-k">厂商</span>
        <select class="sel" v-model="vendorSel" data-vendor-select aria-label="厂商筛选">
          <option value="all">全部厂商</option>
          <option v-for="v in vendors" :key="v.id" :value="v.id">{{ v.label }}</option>
        </select>
      </label>

      <div class="fld">
        <span class="fld-k">模型</span>
        <button
          type="button" class="btn sm msel-btn" data-model-btn
          :disabled="group === 'vendor'" :aria-expanded="modelOpen"
          :title="group === 'vendor' ? '厂商对比按厂商聚合，模型筛选在这里不生效' : '按模型多选（走查询参数）'"
          @click="toggleModelPop"
        >
          <span class="truncate">{{ modelBtnText }}</span>
          <AppIcon :name="modelOpen ? 'chevron-up' : 'chevron-down'" :size="13" />
        </button>
        <div class="msel-pop" v-if="modelOpen" data-model-pop>
          <div class="mp-head">
            <span class="mp-count">{{ vendorModels.length }} 个模型</span>
            <span class="spacer"></span>
            <button type="button" class="lnk" data-model-clear @click="modelSel = []">全选</button>
          </div>
          <label class="mp-row" v-for="o in vendorModels" :key="o.key" :data-model-option="o.key">
            <input type="checkbox" :checked="modelSel.includes(o.key)" @change="toggleModel(o.key)" />
            <span class="mp-label truncate" :title="o.label">{{ o.label }}</span>
            <span class="mp-prov truncate">{{ o.provider_label }}</span>
          </label>
          <p class="mp-empty" v-if="!vendorModels.length">这家厂商在窗口内没有模型</p>
        </div>
      </div>

      <span class="spacer"></span>

      <!-- 厂商对比：既要比用量、也要比费用，所以这一档多一枚紧凑子切换 -->
      <div class="seg sub" v-if="mode === 'vendor'" role="group" aria-label="厂商对比指标">
        <button type="button" :class="{ active: vendorMetric === 'usage' }" data-vendor-metric="usage" @click="vendorMetric = 'usage'">用量</button>
        <button type="button" :class="{ active: vendorMetric === 'cost' }" data-vendor-metric="cost" @click="vendorMetric = 'cost'">费用</button>
      </div>
    </div>

    <p class="usage-note" v-if="loading && !stats">载入中…</p>

    <!-- 窗口内一条记录都没有：一行字，不占屏 -->
    <div class="usage-empty" v-else-if="!hasRecords" data-empty="none">
      <AppIcon name="gauge" :size="13" /><span>{{ emptyText }}</span>
    </div>

    <div class="usage-body" v-else>
      <div class="nums">
        <div class="num-block" v-for="b in blocks" :key="b.k" :data-num="b.k">
          <span class="num-line">
            <b class="n" data-num-text>{{ b.n }}</b><i class="u" v-if="b.u">{{ b.u }}</i>
          </span>
          <span class="k">{{ b.label }}</span>
          <span class="sub" v-if="b.sub">{{ b.sub }}</span>
        </div>
      </div>

      <div class="chart-col">
        <!-- 有记录但一条价都算不出：给一句话，**不画 0 线**（0 线会被读成"花了 0 元"） -->
        <div class="chart-empty" v-if="costEmpty" data-empty="cost">
          <AppIcon name="warning" :size="13" />
          <span>这些调用都还没有配置单价，费用无法计算。</span>
          <button type="button" class="lnk" data-goto-price @click="gotoPricing">去模型配置里填单价</button>
        </div>

        <template v-else>
          <p class="chart-unit" data-y-unit>
            <span>单位：{{ unitText }}</span>
            <span class="cu-sub" v-if="mode === 'token'">深色层 = 输入，浅色层 = 输出</span>
            <span class="spacer"></span>
            <span class="cu-note" v-if="unpricedNote" data-unpriced>{{ unpricedNote }}</span>
          </p>

          <div
            class="chart" ref="box" @mousemove="onMove" @mouseleave="onLeave"
            :aria-label="chartLabel" :data-series-count="visibleSeries.length"
          >
            <svg :width="boxW" :height="H" :viewBox="viewBox" role="img">
              <!-- 纵轴刻度（含 0）：文案随单位变（Token 万/亿、费用带币种符号、请求数就是次数） -->
              <g class="y-axis">
                <template v-for="t in yTicks" :key="'y' + t.v">
                  <line class="gl" :x1="padL" :y1="t.y" :x2="boxW - padR" :y2="t.y" />
                  <text class="ytick" :x="padL - 6" :y="t.y + 3" text-anchor="end" data-y-tick>{{ t.label }}</text>
                </template>
              </g>

              <path
                class="area" v-for="l in layers" :key="'a' + l.id"
                v-show="!!l.area" :d="l.area"
                :style="{ fill: l.color, fillOpacity: l.fill }" :data-layer="l.id"
              />
              <path
                class="ln" v-for="l in layers" :key="'l' + l.id"
                :d="l.line" :style="{ stroke: l.color }" :stroke-width="l.w"
              />

              <g v-if="hoverActive">
                <line class="guide" :x1="hoverX" :y1="padT" :x2="hoverX" :y2="baseY" />
                <circle
                  v-for="l in layers" :key="'h' + l.id"
                  class="hp" :cx="hoverX" :cy="yAt(l.vals[hoverIdx] || 0)" r="3.2"
                  :style="{ stroke: l.color }"
                />
              </g>

              <line class="axis" :x1="padL" :y1="baseY" :x2="boxW - padR" :y2="baseY" />
              <text
                class="xtick" v-for="t in xTicks" :key="'x' + t.i"
                :x="t.x" :y="H - 3" :text-anchor="t.anchor"
              >{{ t.label }}</text>
            </svg>

            <div
              class="tip" v-if="hoverActive && hoverRows.length"
              :class="{ flip: hoverX > boxW * 0.52 }" :style="{ left: hoverX + 'px' }"
              data-hover-tip
            >
              <div class="tp-head">{{ hoverLabel }}</div>
              <div class="tp-row" v-for="r in hoverRows" :key="r.key" :data-hover-row="r.key">
                <span class="tp-dot" :style="{ background: r.color }"></span>
                <div class="tp-main">
                  <div class="tp-line1">
                    <span class="tp-name truncate" :title="r.label">{{ r.label }}</span>
                    <span class="tp-cost" :class="{ none: r.cost === '未计费' || r.cost === '—' }">{{ r.cost }}</span>
                  </div>
                  <div class="tp-line2">
                    <span class="tp-prov">{{ r.sub }}</span>
                    <span class="tp-merged" v-if="r.merged > 1" data-hover-merged>合并 {{ r.merged }} 个配置</span>
                    <span>请求 {{ r.req }}</span>
                    <span>入 {{ r.tin }}</span>
                    <span>出 {{ r.tout }}</span>
                    <span>总 {{ r.total }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="legend" data-legend>
            <button
              type="button" class="lg-item" v-for="it in legendItems" :key="it.id"
              :class="{ off: it.hidden }" :data-legend-item="it.id" :data-hidden="it.hidden ? '1' : '0'"
              :title="it.hidden ? '点一下显示这条' : '点一下隐藏这条'"
              @click="toggleLegend(it)"
            >
              <span class="lg-dot" :style="{ background: it.color }"></span>
              <span class="lg-label truncate">{{ it.label }}</span>
              <span class="lg-merged" v-if="it.merged && it.merged > 1" data-legend-merged>×{{ it.merged }}</span>
              <span class="lg-val" v-if="it.text">{{ it.text }}</span>
            </button>
            <button type="button" class="lnk lg-reset" v-if="hiddenSeries.length || hiddenCur.length" @click="showAll">全部显示</button>
          </div>
        </template>
      </div>

      <div class="rank">
        <div class="rank-head">
          <span class="rank-title">{{ rankTitle }}</span>
          <span class="spacer"></span>
          <button
            class="rank-more" v-if="rankHidden > 0 || rankExpanded" type="button"
            :aria-expanded="rankExpanded" @click="rankExpanded = !rankExpanded"
          >
            <AppIcon :name="rankExpanded ? 'chevron-up' : 'chevron-down'" :size="12" />
            <span>{{ rankExpanded ? '收起' : '展开全部' }}</span>
            <span class="rm-count" v-if="!rankExpanded">（还有 {{ rankHidden }} 个）</span>
          </button>
        </div>
        <template v-if="rankVisible.length">
          <!-- 键用 series.key（= 自定义名称）：**同名即合并**成一条，所以排行榜里不会出现两行一样的名字；
               想分开就改名（上游 model_name 不参与合并） -->
          <div class="rank-row" v-for="r in rankVisible" :key="r.key">
            <span class="rk-name truncate" :title="r.name">{{ r.name }}</span>
            <span class="rk-n">{{ r.text }}</span>
            <span class="rk-p">{{ Math.round(r.share * 100) }}%</span>
            <span class="rk-bar"><i :style="{ width: Math.max(3, r.share * 100) + '%' }" /></span>
          </div>
        </template>
        <div class="rank-none" v-else>{{ costMode ? '没有可计算的费用' : '没有记录' }}</div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 裸块：不描边、不投影、不填底 —— 与「字体设置 / 主题预设 / 高亮效果」同一个观感。
   仍然整宽独占一行（折线需要横向像素）。 */
.usage {
  grid-column: 1 / -1;
  display: flex; flex-direction: column; gap: 10px;
  min-width: 0; padding: 0; border: 0; background: transparent; box-shadow: none;
  container-type: inline-size;
}

/* 顶部：标题 + 一句说明 + 图表类型（紧凑分段，一行放得下） */
.usage-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
.usage-title {
  display: inline-flex; align-items: center; gap: 7px; margin: 0;
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 650; color: var(--text); white-space: nowrap;
}
.usage-desc {
  margin: 0; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
  white-space: nowrap; min-width: 0; overflow: hidden; text-overflow: ellipsis;
}
.icon { display: block; }

/* 分段控件统一压一档高度：这一行要跟标题同高，不能把整块顶高 */
.seg.modes, .seg.win, .seg.sub { height: 26px; }
.seg.modes button, .seg.win button, .seg.sub button {
  height: 20px; padding: 0 9px; font-size: calc(11.5px * var(--ts, 1));
}
.seg.modes button:focus-visible, .seg.win button:focus-visible, .seg.sub button:focus-visible {
  outline: none; box-shadow: var(--ring);
}

/* 筛选行：厂商下拉 + 模型多选 +（厂商对比的子切换）+ 时间范围 */
.usage-filters { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
.fld { position: relative; display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
.fld-k { flex: none; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
.sel {
  height: 26px; max-width: 190px; min-width: 0; padding: 0 8px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text);
  font-family: inherit; font-size: calc(12px * var(--ts, 1)); cursor: pointer;
}
.sel:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.msel-btn {
  height: 26px; max-width: 210px; min-width: 0; display: inline-flex; align-items: center; gap: 6px;
  font-size: calc(12px * var(--ts, 1)); font-weight: 500;
}
.msel-btn:disabled { opacity: .55; cursor: not-allowed; }

/* 模型多选浮层：绝对定位在按钮下方，不推挤筛选行 */
.msel-pop {
  position: absolute; z-index: 30; top: calc(100% + 4px); left: 34px; width: 280px; max-height: 296px;
  display: flex; flex-direction: column; gap: 2px; padding: 6px;
  border: 1px solid var(--border-strong); border-radius: var(--r-sm);
  background: var(--bg-elev); box-shadow: var(--shadow-2); overflow-y: auto;
}
.mp-head { display: flex; align-items: center; gap: 8px; padding: 2px 6px 6px; border-bottom: 1px solid var(--divider); }
.mp-count { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }
.mp-row {
  display: flex; align-items: center; gap: 8px; min-height: 26px; padding: 2px 6px;
  border-radius: var(--r-xs); cursor: pointer;
}
.mp-row:hover { background: var(--bg-hover); }
.mp-row input { flex: none; }
.mp-label { flex: 1 1 auto; min-width: 0; font-size: calc(12px * var(--ts, 1)); color: var(--text); }
.mp-prov { flex: none; font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); max-width: 88px; }
.mp-empty { margin: 0; padding: 6px; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }

.lnk {
  border: 0; background: transparent; padding: 0; white-space: nowrap; font: inherit;
  color: var(--accent-text); font-size: calc(11.5px * var(--ts, 1)); cursor: pointer;
}
.lnk:hover { text-decoration: underline; }
.lnk:focus-visible { outline: none; box-shadow: var(--ring); border-radius: 4px; }

.usage-note { margin: 0; font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.usage-empty {
  display: flex; align-items: center; gap: 8px;
  color: var(--text-faint); font-size: calc(12px * var(--ts, 1)); line-height: 1.3;
}

/* 桌面：三指标（自然宽）| 图表列（自适应）| 排行（定宽） */
.usage-body { display: flex; align-items: stretch; gap: 16px; min-width: 0; }

/* 三指标横排，只用一条 1px 竖线分隔。分隔线跟着**数字块自身的高度**走（align-items: center），
   否则它会被 body 拉成整列那么高，看着像表格的竖线。 */
.nums { display: flex; flex: none; align-items: center; }
.num-block {
  display: flex; flex-direction: column; justify-content: center; gap: 1px;
  padding: 0 14px; min-width: 0; align-self: center;
}
.num-block:first-child { padding-left: 0; }
.num-block + .num-block { border-left: 1px solid var(--divider); }
.num-line { display: inline-flex; align-items: baseline; gap: 2px; white-space: nowrap; }
.num-block .n {
  font-family: var(--display);
  font-size: calc(22px * var(--ts, 1)); font-weight: 650; line-height: 1.15;
  letter-spacing: -.02em; font-variant-numeric: tabular-nums; color: var(--text);
}
.num-block .u {
  font-style: normal; font-size: calc(11.5px * var(--ts, 1)); font-weight: 600;
  color: var(--text-dim); font-variant-numeric: tabular-nums; white-space: nowrap;
}
.num-block .k {
  font-size: calc(11px * var(--ts, 1)); line-height: 1.3;
  color: var(--text-faint); white-space: nowrap;
}
.num-block .sub {
  font-size: calc(10.5px * var(--ts, 1)); line-height: 1.3;
  color: var(--text-faint); font-variant-numeric: tabular-nums; white-space: nowrap;
}

/* 图表列：单位说明 + 图 + 图例，纵向排；宽度吃掉中间那块 */
.chart-col { display: flex; flex-direction: column; gap: 4px; flex: 1 1 320px; min-width: 0; }
.chart-unit {
  display: flex; align-items: center; gap: 8px; margin: 0; min-width: 0;
  font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); white-space: nowrap;
}
.cu-sub { color: var(--text-faint); opacity: .9; overflow: hidden; text-overflow: ellipsis; }
.cu-note { color: var(--warn); font-variant-numeric: tabular-nums; }

/* 图表：容器即坐标系（svg 的 CSS 像素 = 计算像素），hover 只需要减 left */
.chart { position: relative; width: 100%; min-width: 0; }
.chart svg { display: block; max-width: 100%; }

.gl { stroke: var(--divider); stroke-width: 1; stroke-dasharray: 2 3; }
.axis { stroke: var(--divider); stroke-width: 1; }
.area { stroke: none; }
.ln { fill: none; stroke-linecap: round; stroke-linejoin: round; }
.guide { stroke: var(--text-faint); stroke-width: 1; stroke-dasharray: 3 3; opacity: .6; }
.hp { fill: var(--bg-elev); stroke-width: 1.6; }
.ytick, .xtick { font-size: calc(10px * var(--ts, 1)); fill: var(--text-faint); font-family: var(--mono); }
.ytick { font-variant-numeric: tabular-nums; }

/* hover 浮层：每个可见系列一行（两行式：名字 + 费用 / 厂商 + 五个数），贴边时翻到游标左侧 */
.tip {
  position: absolute; z-index: 4; top: 4px; pointer-events: none;
  transform: translateX(12px);
  display: flex; flex-direction: column; gap: 3px;
  width: max-content; min-width: 208px; max-width: 300px;
  padding: 6px 9px 7px; border-radius: var(--r-sm);
  background: var(--bg-elev); border: 1px solid var(--border); box-shadow: var(--shadow-2);
}
.tip.flip { transform: translateX(calc(-100% - 12px)); }
.tp-head {
  font-size: calc(10.5px * var(--ts, 1)); font-family: var(--mono);
  color: var(--text-faint); font-variant-numeric: tabular-nums; padding-bottom: 2px;
  border-bottom: 1px solid var(--divider);
}
.tp-row { display: flex; align-items: flex-start; gap: 6px; min-width: 0; }
.tp-dot { flex: none; width: 7px; height: 7px; margin-top: 4px; border-radius: 50%; }
.tp-main { min-width: 0; flex: 1 1 auto; display: flex; flex-direction: column; gap: 1px; }
.tp-line1 { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.tp-name { flex: 1 1 auto; min-width: 0; font-size: calc(11.5px * var(--ts, 1)); font-weight: 600; color: var(--text); }
.tp-cost { flex: none; font-size: calc(11px * var(--ts, 1)); color: var(--text); font-variant-numeric: tabular-nums; }
.tp-cost.none { color: var(--text-faint); font-weight: 500; }
.tp-line2 {
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
  font-size: calc(10.5px * var(--ts, 1)); color: var(--text-dim); font-variant-numeric: tabular-nums;
}
.tp-prov { color: var(--text-faint); }
/* 合并提示：说明这条线背后是几个配置加成的一条（用户口径：同名即合并） */
.tp-merged { color: var(--accent-text); }

/* 图例：可换行，绝不出现横向滚动条 */
.legend { display: flex; align-items: center; gap: 4px 12px; flex-wrap: wrap; overflow: hidden; min-width: 0; }
.lg-item {
  display: inline-flex; align-items: center; gap: 5px; max-width: 210px; min-width: 0;
  padding: 0; border: 0; border-radius: 4px; background: transparent; color: var(--text-dim);
  font: inherit; cursor: pointer;
}
.lg-item:hover { color: var(--text); }
.lg-item:focus-visible { outline: none; box-shadow: var(--ring); }
.lg-item.off { opacity: .42; }
.lg-dot { flex: none; width: 8px; height: 8px; border-radius: 2px; }
.lg-label { font-size: calc(11px * var(--ts, 1)); min-width: 0; }
.lg-item.off .lg-label { text-decoration: line-through; }
.lg-val { font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); font-variant-numeric: tabular-nums; }
/* 图例上的合并角标：×2 表示这条线由两个同名配置合并而来 */
.lg-merged {
  flex: none; font-size: calc(10px * var(--ts, 1)); line-height: 1.2;
  padding: 0 3px; border-radius: 4px; color: var(--accent-text);
  background: var(--accent-soft); font-variant-numeric: tabular-nums;
}
.lg-reset { margin-left: 2px; }

/* 缺价空态：一句话 + 一个真能滚过去的链接，不是假按钮 */
.chart-empty {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 10px 0; color: var(--text-dim); font-size: calc(12px * var(--ts, 1)); line-height: 1.4;
}

/* 排行：名字 / 数值 / 百分比一行，占比条贴行底 */
.rank { flex: 0 1 268px; min-width: 0; display: flex; flex-direction: column; }
.rank-head { display: flex; align-items: center; gap: 8px; min-width: 0; min-height: 14px; margin-bottom: 4px; }
.rank-title {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: calc(11px * var(--ts, 1)); font-weight: 650; color: var(--text-dim); letter-spacing: .02em;
}
.rank-row {
  position: relative; display: grid; min-width: 0;
  grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; column-gap: 8px;
  height: 22px; padding-bottom: 3px;
}
.rank-row + .rank-row { margin-top: 2px; }
.rk-name { font-size: calc(12px * var(--ts, 1)); line-height: 1.2; color: var(--text); }
.rk-n {
  font-size: calc(11.5px * var(--ts, 1)); line-height: 1.2;
  color: var(--text-dim); font-variant-numeric: tabular-nums; white-space: nowrap;
}
.rk-p {
  font-size: calc(11px * var(--ts, 1)); line-height: 1.2; min-width: 26px; text-align: right;
  color: var(--text-faint); font-variant-numeric: tabular-nums;
}
.rk-bar {
  position: absolute; left: 0; right: 0; bottom: 0; height: 3px; overflow: hidden;
  border-radius: var(--r-pill); background: var(--bg-subtle);
}
.rk-bar > i { display: block; height: 100%; border-radius: var(--r-pill); background: var(--accent); }
.rank-none { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.rank-more {
  display: inline-flex; flex: 0 0 auto; align-items: center; gap: 3px;
  padding: 0; border: 0; border-radius: 4px; background: transparent;
  color: var(--accent-text); font: inherit;
  font-size: calc(11px * var(--ts, 1)); line-height: 1.2; cursor: pointer; white-space: nowrap;
  transition: color var(--dur) var(--ease);
}
.rank-more:hover { text-decoration: underline; }
.rank-more:focus-visible { outline: none; box-shadow: var(--ring); }

/* 容器窄于 900：三指标 → 图表 → 排行 纵向堆叠（设置页内容宽度随侧栏收展变化，
   所以查**容器**而不是视口） */
@container (max-width: 900px) {
  .usage-body { flex-direction: column; align-items: stretch; gap: 10px; }
  .chart-col { flex: 0 0 auto; }
  .rank { flex: 0 0 auto; display: grid; row-gap: 4px; column-gap: 16px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .rank-head { grid-column: 1 / -1; min-height: 0; margin-bottom: 0; }
  .rank-row + .rank-row { margin-top: 0; }
  .rank-none { grid-column: 1 / -1; }
  .rm-count { display: none; }
}
@container (max-width: 560px) {
  .num-block { flex: 1 1 0; padding: 0 10px; }
  .num-block .n { font-size: calc(19px * var(--ts, 1)); }
  .num-block .u, .num-block .sub { font-size: calc(10.5px * var(--ts, 1)); }
  .msel-pop { left: 0; width: min(260px, 92cqw); }
}
</style>
