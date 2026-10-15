/** 阅读统计（**本机**）：打开次数 / 累计时长 / 上次打开时间。
 *
 * 为什么记在本机而不是后端：`reading_progress` 表里只有"读到哪、怎么读"（页码、进度百分比、
 * 视图模式…），**没有次数与时长**；给它们加字段要动数据库迁移与写接口，而这两个数目前的
 * 用途只是详情页上给人看一眼 —— 不值得让服务端承担。键按论文分：`pm.reader.stats.<paperId>`。
 *
 * 口径（与界面上那行"本机统计"一致）：
 * - `opens`：**打开阅读器**的次数（同一次会话里刷新页面算两次，这是"打开次数"的字面含义）；
 * - `seconds`：**只在页面可见时**累加（切到别的标签页不算在读），由阅读器每 30 秒落一次盘；
 * - `lastOpen`：最后一次打开阅读器的时间（ISO）。
 */

export interface ReaderStats {
  opens: number;
  seconds: number;
  lastOpen: string | null;
}

const EMPTY: ReaderStats = { opens: 0, seconds: 0, lastOpen: null };

function storageKey(paperId: string): string {
  return `pm.reader.stats.${paperId}`;
}

export function readReaderStats(paperId: string): ReaderStats {
  if (!paperId || typeof localStorage === 'undefined') return { ...EMPTY };
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey(paperId)) || 'null');
    if (!raw || typeof raw !== 'object') return { ...EMPTY };
    return {
      opens: Number(raw.opens) || 0,
      seconds: Number(raw.seconds) || 0,
      lastOpen: typeof raw.lastOpen === 'string' ? raw.lastOpen : null,
    };
  } catch {
    return { ...EMPTY };      // 隐私模式 / 存坏了：当作没有统计
  }
}

function writeStats(paperId: string, next: ReaderStats): void {
  try {
    localStorage.setItem(storageKey(paperId), JSON.stringify(next));
  } catch {
    /* 存不下也不影响阅读 */
  }
}

/** 打开了一次阅读器：次数 +1，并记下这次的时间。 */
export function noteReaderOpen(paperId: string): ReaderStats {
  const cur = readReaderStats(paperId);
  const next: ReaderStats = { ...cur, opens: cur.opens + 1, lastOpen: new Date().toISOString() };
  writeStats(paperId, next);
  return next;
}

/** 累加阅读时长（秒）。调用方自己保证只累加"页面可见"的那一段。 */
export function addReaderSeconds(paperId: string, seconds: number): ReaderStats {
  const cur = readReaderStats(paperId);
  const next: ReaderStats = { ...cur, seconds: Math.max(0, Math.round(cur.seconds + seconds)) };
  writeStats(paperId, next);
  addDailySeconds(seconds);          // 同一笔时长顺手记进"按天"那本账（见下面）
  return next;
}

/* ═══════════════════════ 每日阅读时长（本机） ═══════════════════════
 * 首页底部那张「阅读统计」折线图要的是**按天**的量。后端**没有**这个数据：
 * `reading_progress` 表只有"读到哪"（页码 / 进度 / 视图模式…），`paper.last_read_at` 只有
 * 一个时间戳，`paper.read_time_min` 是**元数据里估出来的"建议阅读时长"**（不是实际读了多久）。
 * 实际时长一直只记在本机 —— 所以这里在**同一份计时**上再记一份按天的流水：
 * `addReaderSeconds()` 落盘时顺手记一笔。口径与 `seconds` 完全相同（只在页面可见时累加、
 * 每 30 秒一次，见 ReaderView 的 `flushReadSeconds`），**不是第二套计时**。
 *
 * 两个已知边界（写在这里，免得以后被当成 bug 查）：
 * 1. 数据只属于**这个浏览器**：换设备 / 清缓存就没了（服务端没有对应字段可存）；
 * 2. 跨零点的那一段（最多 30 秒）记在落盘当天，不做拆分。
 */
const DAILY_KEY = 'pm.reader.daily';
/** 只留最近这些天：键是按天的，不清理会一直长 */
const DAILY_KEEP_DAYS = 200;

export interface DailyRead {
  /** 本地时区的 `YYYY-MM-DD` */
  date: string;
  seconds: number;
}

/** 本地时区的"今天是哪天"。**不用 `toISOString()`** —— 那是 UTC，晚上 8 点之后会串到明天。 */
export function dayKey(d: Date = new Date()): string {
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${m}-${day}`;
}

function readDailyMap(): Record<string, number> {
  if (typeof localStorage === 'undefined') return {};
  try {
    const raw = JSON.parse(localStorage.getItem(DAILY_KEY) || 'null');
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
    const out: Record<string, number> = {};
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
      if (/^\d{4}-\d{2}-\d{2}$/.test(k)) out[k] = Math.max(0, Math.round(Number(v) || 0));
    }
    return out;
  } catch {
    return {};        // 隐私模式 / 存坏了：当作没有记录（图表走空状态）
  }
}

/** 累加"今天"的阅读时长（秒）。由 `addReaderSeconds` 调用，不需要在视图里手动调。 */
export function addDailySeconds(seconds: number): void {
  if (!(seconds > 0) || typeof localStorage === 'undefined') return;
  const map = readDailyMap();
  const today = dayKey();
  map[today] = (map[today] ?? 0) + Math.round(seconds);
  const floor = dayKey(new Date(Date.now() - DAILY_KEEP_DAYS * 86400000));
  const pruned: Record<string, number> = {};
  for (const [k, v] of Object.entries(map)) if (k >= floor) pruned[k] = v;   // 日期字符串可直接比大小
  try {
    localStorage.setItem(DAILY_KEY, JSON.stringify(pruned));
  } catch {
    /* 存不下也不影响阅读 */
  }
}

/** 最近 `days` 天（含今天，从早到晚）。**没有记录的那天给 0**，不用缺失值顶替。 */
export function readDailySeries(days: number): DailyRead[] {
  const map = readDailyMap();
  const out: DailyRead[] = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i -= 1) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - i);
    const key = dayKey(d);
    out.push({ date: key, seconds: map[key] ?? 0 });
  }
  return out;
}

/** 区间汇总。**日均的口径固定为"总时长 ÷ 区间天数"**（含没有阅读的那几天），
 *  与右下角那行辅助说明一致；不要换成"只按有记录的天数平均"，否则切换区间会前后矛盾。 */
export function summarizeDaily(series: DailyRead[]): { total: number; activeDays: number } {
  let total = 0;
  let activeDays = 0;
  for (const d of series) {
    total += d.seconds;
    if (d.seconds > 0) activeDays += 1;
  }
  return { total, activeDays };
}

/** 时长文案：不足一分钟不硬凑数字（"0 分钟"看着像坏了）。 */
export function fmtReadDuration(seconds: number): string {
  if (!seconds || seconds < 60) return seconds ? '不足 1 分钟' : '—';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
}
