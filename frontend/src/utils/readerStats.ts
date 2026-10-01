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
  return next;
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
