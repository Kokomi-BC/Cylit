<script setup lang="ts">
/** 论文详情弹窗（**Paper Brief**：论文快速理解入口）。
 *
 * 两栏：**左封面栏**（封面 / 身份 / 阅读 + 导出）+ **右内容栏**（概览 · 阅读习惯 · 参考文献 · 进度 · 文件）。
 *
 * 「概览」的职责是"快速回答这篇论文是什么"：**论文速览 → 核心创新 → 阅读画像 / 关键词 /
 * 前置知识 → 推荐阅读路径 → 论文档案**。除了速览那一张主卡，其余都是"分组标题 + 内容"
 * 的轻分组 —— 不再一个字段一张大卡片。
 *
 * 边界（用户口径 2026-10-08 / 2026-11）：概览只放**论文本身**。系统信息（大小 / 入库 / 知识库 / 标识）
 * 压到最下面的「论文档案」；阅读行为数据（每日时长折线 + 阅读次数 / 总时长 / 最近打开）单独一个
 * 「阅读习惯」页（用户口径：紧挨概览右侧），任务流水线留在「进度」；
 * 文件产物（PDF / Markdown / ZIP）在「文件」页与左下角的「导出」菜单里。
 *
 * 数据都在打开时现拉（`GET /papers/{id}` + `/progress` + `/outline` + `/tasks` + 一次 Range 取文件大小）：
 * 详情是"看一眼就走"的浮层，不该为了它把整份 reader payload 拉下来；
 * 论文列表里的那一行可能已经旧了（后台任务在跑），所以以这里拉到的那份为准。
 *
 * 五个页签的面板**全都挂载**（用 `v-show`），切页签不丢滚动位置与下载中的状态。
 */

import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { api, assetUrl, downloadBlob } from '@/api/client';
import type { Outline, Paper, TaskInfo } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import ConfirmModal from '@/components/ConfirmModal.vue';
import ReferenceList from '@/components/reader/ReferenceList.vue';
import { openTab } from '@/stores/tabs';
import { reportError, toast } from '@/stores/app';
import { TASK_TYPE_LABEL, fmtRelative, taskStatus } from '@/utils/format';
import { readerTarget } from '@/utils/navigation';
import {
  fmtReadDuration, readDailySeries, readReaderStats, type DailyRead, type ReaderStats,
} from '@/utils/readerStats';

const props = defineProps<{ paperId: string }>();
const emit = defineEmits<{ (e: 'close'): void; (e: 'changed'): void }>();

const router = useRouter();

type TabKey = 'overview' | 'habit' | 'refs' | 'progress' | 'files';
const TABS: ReadonlyArray<{ key: TabKey; label: string; icon: string }> = [
  { key: 'overview', label: '概览', icon: 'doc' },
  { key: 'habit', label: '阅读习惯', icon: 'clock' },
  { key: 'refs', label: '参考文献', icon: 'list' },
  { key: 'progress', label: '进度', icon: 'task' },
  { key: 'files', label: '文件', icon: 'layers' },
];
const tab = ref<TabKey>('overview');

const paper = ref<Paper | null>(null);
const tasks = ref<TaskInfo[]>([]);
/** 目录：只用来算「推荐阅读路径」（`/outline` 是纯结构计算，很便宜）。 */
const outline = ref<Outline>({ items: [], tree: [], count: 0 });
const percent = ref(0);
const sizeBytes = ref(0);
const loading = ref(true);
const error = ref('');
const copied = ref(false);
const downloading = ref('');
const busy = ref('');
const confirmDelete = ref(false);

/* ------------------------------ 载入 ------------------------------ */

async function load() {
  loading.value = true;
  error.value = '';
  try {
    paper.value = await api.get<Paper>(`/papers/${props.paperId}`);
  } catch (err) {
    error.value = (err as Error)?.message ?? String(err);
    loading.value = false;
    return;
  }
  loading.value = false;

  // 进度 / 任务 / 文件大小：任何一个失败都不该把详情页整体判死（都是"附加信息"）
  api.get<{ percent?: number }>(`/papers/${props.paperId}/progress`)
    .then((p) => { percent.value = Number(p?.percent ?? 0) || 0; })
    .catch(() => { /* 没有进度记录就是未读 */ });

  // 本机阅读统计：打开这个弹窗时重读一次（阅读器可能刚更新过）
  localStats.value = readReaderStats(props.paperId);

  api.get<{ items: TaskInfo[] }>('/tasks', { limit: 200 })
    .then((res) => {
      tasks.value = (res?.items || []).filter((t) => t.paper_id === props.paperId);
    })
    .catch(() => { tasks.value = []; });

  // 目录：概览的「推荐阅读路径」用它挑章节（拉不到就整块不显示，不影响别的）
  api.get<Outline>(`/papers/${props.paperId}/outline`)
    .then((o) => { outline.value = o ?? { items: [], tree: [], count: 0 }; })
    .catch(() => { outline.value = { items: [], tree: [], count: 0 }; });

  // 文件大小：后端那个下载路由**只允许 GET**（HEAD 回 405，拿到的 31B 是错误 JSON 的长度），
  // 所以用 `Range: bytes=0-0` 讨一个字节，从 `Content-Range: bytes 0-0/<total>` 里读总大小。
  fetch(assetUrl(`/api/v1/papers/${props.paperId}/file`), { headers: { Range: 'bytes=0-0' } })
    .then((res) => {
      const total = (res.headers.get('content-range') || '').split('/')[1];
      sizeBytes.value = Number(total) || Number(res.headers.get('content-length')) || 0;
    })
    .catch(() => { sizeBytes.value = 0; });
}

onMounted(() => {
  void load();
  void nextTick(syncCoverRatio);
  document.addEventListener('keydown', onKey);
});
onUnmounted(() => document.removeEventListener('keydown', onKey));

// 换了论文（同一个弹窗换个 id）就重新拉：封面比例也要回到占位值重算
watch(() => props.paperId, () => {
  tab.value = 'overview';
  coverRatio.value = A4_RATIO;
  void load();
});

function onKey(ev: KeyboardEvent) {
  if (ev.key !== 'Escape') return;
  // 删除确认浮在上面时，Esc 只该收起确认框（两层同时关掉会让人以为操作没生效）
  if (confirmDelete.value) return;
  emit('close');
}

/* ------------------------------ 展示值 ------------------------------ */

const title = computed(() => paper.value?.title || '加载中…');
const authors = computed(() => paper.value?.authors?.trim() || '未知作者');

/** 右侧「作者与出处」卡用：作者 / 期刊 / 年份各一行（缺的给 `—`，不留空行）。 */
const venueText = computed(() => paper.value?.venue?.trim() || '—');
const yearText = computed(() => (paper.value?.year ? String(paper.value.year) : '—'));

const coverUrl = computed(() => assetUrl(`/api/v1/papers/${props.paperId}/pages/1`));

/**
 * 封面框的宽高比 = **首页图自己的比例**（A4 约 `1 / 1.29`，双栏、扫描件各有不同）。
 *
 * 为什么不能写死 `3 / 4`：论文首页的比例是数据的一部分 —— 写死了，`object-fit: contain`
 * 就只能在框里留出空白边（看着像"被压扁 / 被裁掉"）。这里等图加载完把**真实比例**交给 CSS，
 * 框正好等于图片：不变形、不留白。加载前先用 A4 那一档占位，避免跳版。
 */
const A4_RATIO = '1 / 1.29';
const coverRatio = ref(A4_RATIO);
const coverEl = ref<HTMLImageElement | null>(null);

/** 图已在缓存里时 `load` 可能早于监听挂上，所以挂载后补量一次。 */
function syncCoverRatio() {
  const img = coverEl.value;
  if (img?.naturalWidth && img?.naturalHeight) {
    coverRatio.value = `${img.naturalWidth} / ${img.naturalHeight}`;
  }
}

function onCoverLoad(ev: Event) {
  const img = ev.target as HTMLImageElement;
  if (!img?.naturalWidth || !img?.naturalHeight) return;
  coverRatio.value = `${img.naturalWidth} / ${img.naturalHeight}`;
}

function formatBytes(n: number): string {
  if (!n) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}
const sizeText = computed(() => formatBytes(sizeBytes.value));

/** 入库时间：`2026/10/01`（与图2 的「入库」一致，只要日期不要时分）。 */
const addedText = computed(() => {
  const iso = paper.value?.created_at;
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso)
    : `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}`;
});

const kbText = computed(() => (paper.value?.in_knowledge_base ? '已入库' : '未加入'));

/** 摘要（原文，英文）与标签：都在 `GET /papers/{id}` 那一份里，不需要另开接口。 */
const abstractText = computed(() => (paper.value?.abstract || '').trim());
const tags = computed(() => (paper.value?.tags || []).filter((t) => String(t).trim()));

/* ------------------------------ 元数据扩展（2026-10-07） ------------------------------ */

/** 中文摘要（模型的译文）。 */
const abstractZh = computed(() => (paper.value?.abstract_zh || '').trim());
/** 核心创新：最多取 4 条（再多就不是"速览"了）。 */
const contributions = computed(() => (paper.value?.contributions || [])
  .filter((t) => String(t).trim()).slice(0, 4));
const prereq = computed(() => (paper.value?.prereq || []).filter((t) => String(t).trim()));
const keywordsEn = computed(() => (paper.value?.keywords_en || []).filter((t) => String(t).trim()));
const references = computed(() => paper.value?.references || []);

/* --------------------------- 摘要语言（中文 / English） ---------------------------
 * 摘要是论文最重要的原始信息：**完整显示、不截断**（用户口径 2026-10-08 ——
 * 简约靠"少几个大卡片、少几处重复"，不是靠裁掉摘要）。中英共用同一块区域，
 * 用轻量胶囊切换，默认中文。
 */
const lang = ref<'zh' | 'en'>('zh');
/** 中英都有才值得给切换器（只有一份时切成空白没有意义）。 */
const hasBothLangs = computed(() => Boolean(abstractZh.value && abstractText.value));
/** 当前该显示的那份摘要：选中的语言为空就退回另一份，不给用户一片空白。 */
const briefBody = computed(() => (lang.value === 'en'
  ? (abstractText.value || abstractZh.value)
  : (abstractZh.value || abstractText.value)));

/** 关键词语言（左栏）：中文用 `tags`、英文用 `keywords_en`，同一块区域切换，不并排堆两份。 */
const kwLang = ref<'zh' | 'en'>('zh');
const hasBothKw = computed(() => Boolean(tags.value.length && keywordsEn.value.length));
const keywordList = computed(() => {
  const zh = tags.value;
  const en = keywordsEn.value;
  return kwLang.value === 'en' ? (en.length ? en : zh) : (zh.length ? zh : en);
});

/* --------------------------- 论文属性（右侧最顶部） ---------------------------
 * 用户进概览第一眼要回答的是"这是什么论文"：领域 / 类型 / 难度 / 推荐阅读时间。
 * 一行「16px 图标 + 标签 + 值」，**不做成一张大卡**，缺的项直接不出现。
 * 图标是**一属性一枚、各不相同**的语义图标（Lucide 造型，见 `AppIcon`），不重复用同一个 File。
 */
const attrs = computed<{ k: string; v: string; icon: string }[]>(() => {
  const p = paper.value;
  if (!p) return [];
  const out: { k: string; v: string; icon: string }[] = [];
  if (p.domain) out.push({ k: '领域', v: p.domain, icon: 'tag' });
  if (p.paper_type) out.push({ k: '类型', v: p.paper_type, icon: 'book-open' });
  if (p.difficulty) out.push({ k: '难度', v: p.difficulty, icon: 'gauge' });
  if (p.read_time_min) out.push({ k: '阅读时间', v: `约 ${p.read_time_min} 分钟`, icon: 'task' });
  return out;
});

/** 「论文档案」的键值行：**没值的项不画**（空行比没有更烦人）。
 *  期刊 / 会议 / 年份 / 页数已经在左栏的身份区里，这里不再重复。 */
const archFacts = computed<{ k: string; v: string }[]>(() => {
  const p = paper.value;
  if (!p) return [];
  const out: { k: string; v: string }[] = [];
  const push = (k: string, v: string) => { if (v && v !== '—') out.push({ k, v }); };
  if (references.value.length) push('参考文献', `${references.value.length} 条`);
  push('文件大小', sizeText.value);
  push('入库', addedText.value);
  push('知识库', kbText.value);
  return out;
});

/* --------------------------- 推荐阅读路径（2026-10-08） ---------------------------
 * 从**目录**（`/papers/{id}/outline`，纯结构计算）里挑出读者最该先看的几节：
 * 只认**带章节编号**的标题（`1` / `2.3`）—— 目录里的论文标题、Abstract、Keywords、
 * References、附录段落天然没有编号，正好一次滤掉，不用维护"黑名单"。
 *
 * 一级章节够多（≥3 节）就只列一级（那是论文的骨架）；只有两三节的短文再补上二级，
 * 免得整块只有两行。顺序按**原文顺序**（页码 → 层级 → 编号）——"路径"读起来要是顺着走的。
 */
const PATH_MAX = 5;
const NUM_RE = /^\d+(?:\.\d+)*$/;
/** 去掉标题里重复的编号前缀（`0.1 PROBLEM DEFINITION` → `PROBLEM DEFINITION`）。 */
const stripNumber = (s: string, num: string) => s.replace(new RegExp(`^${num.replace(/\./g, '\\.')}[.\\s、]*`), '').trim();

const readingPath = computed(() => {
  const paperTitle = (paper.value?.title || '').trim().toLowerCase();
  const rows = (outline.value.items || [])
    .filter((it) => NUM_RE.test(String(it.number || '').trim()))
    .filter((it) => Number(it.level) <= 2 && Number(it.page) > 0)
    // 目录第一行常常就是论文标题本身（无编号时已被滤掉；这里兜一层"标题与论文同名"）
    .filter((it) => (it.text || '').trim().toLowerCase() !== paperTitle)
    .map((it) => ({
      key: it.block_id,
      row: it.block_id,
      page: Number(it.page),
      level: Number(it.level),
      number: String(it.number || '').trim(),
      title: stripNumber((it.text_zh || '').trim() || (it.text || '').trim(), String(it.number || '').trim()),
    }))
    .filter((r) => r.title);
  const level1 = rows.filter((r) => r.level === 1);
  const picked = level1.length >= 3 ? level1 : rows;
  return picked
    .slice()
    .sort((a, b) => a.page - b.page || a.level - b.level || a.number.localeCompare(b.number, undefined, { numeric: true }))
    .slice(0, PATH_MAX);
});

/** 还有哪些项是空的 —— 决定"要不要显示补全按钮"。 */
const metaMissing = computed(() => {
  const p = paper.value;
  if (!p) return false;
  return !abstractText.value || !tags.value.length || !abstractZh.value
    || !p.domain || !contributions.value.length || !p.difficulty || !p.read_time_min;
});

/** 左栏身份区那一行补充信息：页数 + 会议/年份（原来在"9 页文档"横幅里，横幅去掉了）。 */
const metaLine = computed(() => {
  const parts: string[] = [];
  if (paper.value?.page_count) parts.push(`${paper.value.page_count} 页`);
  if (paper.value?.venue) parts.push(paper.value.venue);
  if (paper.value?.year) parts.push(String(paper.value.year));
  return parts.join(' · ');
});

/**
 * 两个补全入口（用户口径 2026-10-07）：
 * - `model=false`：**离线**读 PDF 首页与页产物（会议/年份/原文摘要/DOI/参考文献/阅读时间），
 *   不花钱、同步返回，所以直接刷界面；
 * - `model=true`：交给 **agent 读全文**填写（"完整 agent 功能，不用显示细节"）——
 *   它是个后台任务（1~3 分钟），这里只负责排队 + 说一声，跑完刷新卡片即可看到。
 */
const metaBusy = ref('');
async function fillMeta(model: boolean) {
  if (metaBusy.value) return;
  metaBusy.value = model ? 'agent' : 'pdf';
  try {
    if (model) {
      await api.post(`/papers/${props.paperId}/metadata/agent`);
      toast('已在后台开始补全论文信息（agent 会先读这篇论文，约 1~3 分钟）', 'ok', 6200);
      emit('changed');
      return;
    }
    const res = await api.post<{ paper?: Paper }>(`/papers/${props.paperId}/metadata/auto`,
                                                 { model: false });
    if (res?.paper) paper.value = res.paper;
    emit('changed');
  } catch (err) {
    reportError(err, '自动补全失败');
  } finally {
    metaBusy.value = '';
  }
}

/* --------------------------- 阅读统计（本机） ---------------------------
 * 阅读器每进一次记一次"打开"，页面可见期间累加时长（`utils/readerStats.ts`）。
 * 后端没有这两个字段，所以这里明确标注"本机统计"，不让人以为是服务端数据。
 */

const localStats = ref<ReaderStats>(readReaderStats(props.paperId));
const readDuration = computed(() => fmtReadDuration(localStats.value.seconds));
const lastOpenText = computed(() => (localStats.value.lastOpen
  ? fmtRelative(localStats.value.lastOpen)
  : '—'));
const hasLocalStats = computed(() => localStats.value.opens > 0 || localStats.value.seconds > 0);

/* --------------------------- 阅读习惯（本机） ---------------------------
 * 「阅读习惯」页签：上面一条**每日阅读时长**折线，下面是总时长与打开信息。
 *
 * 数据只有一个来源 —— `pm.reader.daily`（`utils/readerStats.ts` 每次落盘时顺手记一笔，
 * 最多留 200 天）。后端没有这条时间序列（`paper.last_read_at` 只有一个时间戳，
 * `read_time_min` 是元数据里**估**的建议时长），所以这里如实写"本机记录"：
 * 换设备 / 清缓存就没了，但它是这个浏览器里真实计时出来的。
 *
 * 7 / 30 天两档都能给：那本账是真的按天存的，不是把一周的量摊到 30 个格子里。
 */
type HabitRange = 7 | 30;
const habitRange = ref<HabitRange>(7);
const habitSeries = computed<DailyRead[]>(() => readDailySeries(habitRange.value));
const habitSum = computed(() => habitSeries.value.reduce((n, d) => n + d.seconds, 0));
const habitPeak = computed(() => habitSeries.value.reduce((n, d) => Math.max(n, d.seconds), 0));

/* 固定 viewBox 600×150、横向拉满 + `preserveAspectRatio="none"` + `non-scaling-stroke`：
 * 跟工作区那张「阅读统计」同一套画法（那张是 300×88，卡窄）。这里取 600 宽是因为详情弹窗
 * 右栏约 700px —— 相近的宽高比让横纵缩放接近 1，圆点不会被拉成椭圆。
 * 纵向按区间峰值归一，全 0 时按 1 分钟铺满（免得除零，也免得几分钟就画成顶到天花板）。 */
const HABIT_W = 600;
const HABIT_H = 150;
const HABIT_PAD = 10;

const habitPoints = computed(() => {
  const list = habitSeries.value;
  const max = Math.max(habitPeak.value, 60);
  const stepX = HABIT_W / Math.max(1, list.length - 1);
  return list.map((d, i) => ({
    date: d.date,
    seconds: d.seconds,
    x: i * stepX,
    y: HABIT_H - HABIT_PAD - (d.seconds / max) * (HABIT_H - HABIT_PAD * 2),
  }));
});
const habitLine = computed(() => habitPoints.value
  .map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' '));
const habitArea = computed(() => {
  const ps = habitPoints.value;
  if (!ps.length) return '';
  const base = HABIT_H - HABIT_PAD;
  return `M${ps[0].x.toFixed(1)} ${base} ${ps.map((p) => `L${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ')}`
    + ` L${ps[ps.length - 1].x.toFixed(1)} ${base} Z`;
});

/** 横轴刻度按**百分比**定位（不用 `space-between`）：30 天里两端对齐要贴边，中间又要压在折线点上 */
const habitTicks = computed(() => {
  const ps = habitPoints.value;
  const n = ps.length;
  if (!n) return [];
  const step = habitRange.value === 7 ? 1 : 7;
  const out: Array<{ key: string; text: string; left: string; shift: string }> = [];
  for (let i = 0; i < n; i += step) {
    out.push({
      key: ps[i].date,
      text: ps[i].date.slice(5).replace('-', '/'),
      left: `${(i / Math.max(1, n - 1)) * 100}%`,
      shift: i === 0 ? 'translateX(0)' : i >= n - 1 ? 'translateX(-100%)' : 'translateX(-50%)',
    });
  }
  return out;
});

/* --------------------------- 处理流程 --------------------------- */

function parseState(): { text: string; tone: string } {
  const s = String(paper.value?.status ?? '');
  if (s === 'parsing') return { text: '进行中', tone: 'active' };
  if (s === 'failed' || s === 'error') return { text: '失败', tone: 'failed' };
  if (paper.value?.page_count) return { text: '已完成', tone: 'done' };
  return { text: '未开始', tone: 'muted' };
}

function translateState(): { text: string; tone: string } {
  const done = paper.value?.translate_done || 0;
  const total = paper.value?.translate_total || 0;
  if (!total) return { text: '未开始', tone: 'muted' };
  if (done >= total) return { text: `已完成（${total} 页）`, tone: 'done' };
  return { text: `${done} / ${total} 页`, tone: 'active' };
}

function kbState(): { text: string; tone: string } {
  return paper.value?.in_knowledge_base
    ? { text: '已入库', tone: 'done' }
    : { text: '未入库', tone: 'muted' };
}

/**
 * 处理流程的**里程碑条**：解析 / 翻译 / 索引 各占进度条的一段，段内再按各自的进度填充
 * （翻译 4/9 页 → 那一段填 44%）。这样一张卡只占一行多一点的宽度和高度：
 * 用户口径是"每个任务作为进度条的一个里程碑"，同时**处理流程的优先级最低** ——
 * 所以它排在最下面、做成最扁的一条，把纵向空间让给作者与出处 / 阅读那两张卡。
 */
const milestones = computed(() => {
  const transDone = paper.value?.translate_done || 0;
  const transTotal = paper.value?.translate_total || 0;
  const parse = parseState();
  const trans = translateState();
  const kb = kbState();
  /** 段内填充：完成=满格；进行中=按真实进度（没有分母时给 0.2，让人看出"在动"）；未开始=空 */
  const frac = (tone: string, own = 0) => {
    if (tone === 'done') return 1;
    if (tone === 'active') return Math.min(1, Math.max(0.2, own));
    return 0;
  };
  return [
    { key: 'parse', label: '解析', ...parse, frac: frac(parse.tone) },
    { key: 'translate', label: '翻译', ...trans, frac: frac(trans.tone, transTotal ? transDone / transTotal : 0) },
    { key: 'kb', label: '索引', ...kb, frac: frac(kb.tone) },
  ];
});

/* --------------------------- 进度 / 活动 --------------------------- */

/** 正在跑的翻译任务（列表 + 进度以它为准：磁盘产物是上一轮的）。 */
const liveTask = computed(() => tasks.value.find(
  (t) => ['TRANSLATE_MD', 'PARSE'].includes(String(t.type))
    && ['pending', 'processing', 'paused'].includes(String(t.status)),
) ?? null);

const mdProgress = computed(() => {
  const done = paper.value?.translate_done || 0;
  const total = paper.value?.translate_total || 0;
  return total ? { done, total, pct: Math.round((done / total) * 100) } : null;
});

const pdfProgress = computed(() => {
  const done = paper.value?.pdf_translate_done || 0;
  const total = paper.value?.pdf_translate_total || 0;
  return total ? { done, total, pct: Math.round((done / total) * 100) } : null;
});

/** 流水线四段（图2 的「进度」页签）：按论文行就能判，不需要额外的阶段接口。 */
const stages = computed(() => {
  const p = paper.value;
  const parsed = !!p?.page_count;
  const translated = !!mdProgress.value && mdProgress.value.done >= mdProgress.value.total;
  return [
    { key: 'parse', label: '解析', done: parsed, active: String(p?.status) === 'parsing' },
    { key: 'translate', label: '翻译', done: translated, active: !!liveTask.value },
    { key: 'index', label: '索引', done: !!p?.in_knowledge_base, active: false },
    { key: 'understand', label: '解读', done: !!p?.in_knowledge_base, active: false },
  ];
});

function fmtTime(iso?: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')} `
    + `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** 最近活动：任务行 + 一条"加入论文库"（有创建时间就补上，任务为空时列表也不是空白）。 */
const activity = computed(() => {
  const rows = [...tasks.value]
    .sort((a, b) => String(b.updated_at || b.created_at || '').localeCompare(String(a.updated_at || a.created_at || '')))
    .slice(0, 3)
    .map((t) => {
      const st = taskStatus(String(t.status));
      const tone = st.cls === 'danger' ? 'failed' : st.cls === 'ok' ? 'done' : 'active';
      const type = String(t.type);
      const icon = type === 'PARSE' ? 'search' : type.startsWith('TRANSLATE') ? 'translate' : 'document';
      return {
        id: t.id,
        icon,
        tone,
        title: TASK_TYPE_LABEL[type] || type,
        text: `${st.label}${t.total_items ? ` · ${t.done_items || 0}/${t.total_items}` : ''}`,
        time: fmtTime(t.updated_at || t.created_at),
      };
    });
  if (paper.value?.created_at) {
    rows.push({
      id: 'added',
      icon: 'upload',
      tone: 'done',
      title: '加入论文库',
      text: '原始 PDF 已保存',
      time: fmtTime(paper.value.created_at),
    });
  }
  // 「打开时间」（用户口径）：本机记的那次打开 —— 后端只有 last_read_at（阅读器写进度时才更新），
  // 这里要的是"什么时候打开看过"，所以用本机统计的这一条更贴近事实。
  if (localStats.value.lastOpen) {
    rows.push({
      id: 'opened',
      icon: 'play',
      tone: 'active',
      title: '打开阅读器',
      text: `累计 ${localStats.value.opens} 次 · 共 ${readDuration.value}`,
      time: fmtTime(localStats.value.lastOpen),
    });
  }
  return rows;
});

/* ------------------------------ 动作 ------------------------------ */

/**
 * 打开阅读器。原文 / 对照 / 翻译是**阅读器内部状态**（它按 `progress.view_mode` 恢复上次那档），
 * 所以概览这一个按钮只负责"进去读"；带锚点时按深链直接落到某一节（推荐阅读路径用）。
 */
function openReader(anchor?: { row?: string; page?: number }) {
  const target = anchor?.row || anchor?.page
    ? readerTarget(props.paperId, anchor?.row ?? '', anchor?.page ?? null)
    : { name: 'reader', params: { paperId: props.paperId } };
  void router.push(openTab(target) as never);
}

interface ArtifactDef {
  key: string;
  label: string;
  icon: string;
  path: string;
  filename: string;
  query?: Record<string, string>;
}

/** 五块产物：与后端**实际有**的导出接口一一对应（没有的不摆，摆了就是骗人）。
 *  入口只有一个 —— 「文件」页签（概览不重复摆一遍下载）。 */
const artifacts = computed<ArtifactDef[]>(() => [
  { key: 'source', label: 'PDF', icon: 'document-pdf', path: `/papers/${props.paperId}/file`, filename: `${props.paperId}.pdf` },
  { key: 'bilingual', label: '对照 Markdown', icon: 'panel', path: `/papers/${props.paperId}/markdown/export`, filename: `${props.paperId}-bilingual.md`, query: { mode: 'bilingual' } },
  { key: 'translation', label: '翻译 Markdown', icon: 'translate', path: `/papers/${props.paperId}/markdown/export`, filename: `${props.paperId}-translation.md`, query: { mode: 'translation' } },
  { key: 'original', label: '原文 Markdown', icon: 'code', path: `/papers/${props.paperId}/markdown/export`, filename: `${props.paperId}-original.md`, query: { mode: 'original' } },
  { key: 'zip', label: '全部打包（含插图）', icon: 'layers', path: `/papers/${props.paperId}/export/zip`, filename: `${props.paperId}.zip` },
]);

/** 左下角「导出」菜单：与「文件」页同一份 `artifacts`，只是换一层入口（下载逻辑完全一样）。 */

/** Markdown / zip 是二进制流，必须走 blob 下载（走 `request()` 会被当 JSON 解析坏掉）。 */
async function download(a: ArtifactDef) {
  if (downloading.value) return;
  downloading.value = a.key;
  try {
    const blob = await downloadBlob(a.path.replace(/^\//, ''), a.query);
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = a.filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch (err) {
    reportError(err, `下载「${a.label}」失败`);
  } finally {
    downloading.value = '';
  }
}

async function copyId() {
  try {
    await navigator.clipboard.writeText(props.paperId);
    copied.value = true;
    setTimeout(() => { copied.value = false; }, 1500);
  } catch {
    toast('复制失败：浏览器没给剪贴板权限', 'warn', 3000);
  }
}

async function doDelete() {
  confirmDelete.value = false;
  busy.value = 'delete';
  try {
    await api.del(`/papers/${props.paperId}`, { hard: false });
    toast('已删除（PDF 移入 data/trash；对话历史与批注已清）', 'ok', 3200);
    emit('changed');
    emit('close');
  } catch (err) {
    reportError(err, '删除失败');
  } finally {
    busy.value = '';
  }
}
</script>

<template>
  <Teleport to="body">
    <div class="pd-mask" @click.self="emit('close')">
      <div class="pd-dialog" role="dialog" aria-modal="true" :aria-label="`论文详情：${title}`">
        <header class="pd-head">
          <h2 class="pd-head-title" :title="title">{{ title }}</h2>
          <button type="button" class="pd-close" title="关闭" @click="emit('close')">
            <AppIcon name="close" :size="16" />
          </button>
        </header>

        <div v-if="loading" class="pd-state">
          <svg class="spinner" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /></svg>
          <span>正在读取论文详情…</span>
        </div>

        <div v-else-if="error" class="pd-state">
          <span class="badge danger">读取失败</span>
          <p class="tiny faint">{{ error }}</p>
          <button class="btn sm" @click="load">
            <AppIcon name="refresh" :size="14" />重试
          </button>
        </div>

        <div v-else class="pd-grid">
          <!-- ---------------- 左：封面 + 身份 + 阅读 / 导出 ---------------- -->
          <aside class="pd-left">
            <div class="pd-cover" :style="{ '--cover-ratio': coverRatio }">
              <img
                ref="coverEl"
                :src="coverUrl"
                :alt="`${title} 首页`"
                loading="lazy"
                decoding="async"
                @load="onCoverLoad"
              />
            </div>

            <div class="pd-identity">
              <h3>
                <span class="pd-id-title" :title="title">{{ title }}</span>
              </h3>
              <p><span>{{ authors }}</span></p>
              <p v-if="metaLine"><span>{{ metaLine }}</span></p>
            </div>

            <!-- 唯一主操作：原文 / 对照 / 翻译是**阅读器内部状态**（阅读器按
                 `progress.view_mode` 恢复上次那档），概览不让人提前选；
                 文件导出统一走「文件」页签，这里不再摆第二个入口。 -->
            <div class="pd-actions">
              <button type="button" class="pd-btn primary" @click="openReader()">
                <AppIcon name="play" :size="16" />阅读
              </button>
            </div>

            <!-- 关键词：论文身份的一部分（放在左侧），中英同一块区域切换 -->
            <div class="pd-kw" v-if="keywordList.length">
              <div class="pd-kw-head">
                <span>关键词</span>
                <div class="seg pd-lang" v-if="hasBothKw" role="group" aria-label="关键词语言">
                  <button type="button" :class="{ active: kwLang === 'zh' }"
                          :aria-pressed="kwLang === 'zh'" @click="kwLang = 'zh'">中文</button>
                  <button type="button" :class="{ active: kwLang === 'en' }"
                          :aria-pressed="kwLang === 'en'" @click="kwLang = 'en'">English</button>
                </div>
              </div>
              <div class="pd-tags">
                <span class="pd-tag" v-for="k in keywordList" :key="k">{{ k }}</span>
              </div>
            </div>
          </aside>

          <!-- ---------------- 右：概览 / 进度 / 文件 ---------------- -->
          <section class="pd-right">
            <div class="pd-tabs" role="tablist" aria-label="论文详情分区">
              <button
                v-for="t in TABS"
                :key="t.key"
                type="button"
                role="tab"
                class="pd-tab"
                :class="{ active: tab === t.key }"
                :aria-selected="tab === t.key"
                @click="tab = t.key"
              >
                <AppIcon :name="t.icon" :size="16" />{{ t.label }}
              </button>
            </div>

            <div class="pd-panel">
              <!-- 概览 -->
              <div v-show="tab === 'overview'" class="pd-overview">
                <!-- 论文属性：概览**最顶部**，一行「图标 + 标签 + 值」。用户第一眼要回答的是
                     "这是什么论文"（领域 / 类型 / 难度 / 读起来要多久）。 -->
                <div class="pd-attrs" v-if="attrs.length">
                  <span class="pd-attr" v-for="a in attrs" :key="a.k" :title="`${a.k}：${a.v}`">
                    <AppIcon :name="a.icon" :size="16" />
                    <i>{{ a.k }}</i>
                    <strong>{{ a.v }}</strong>
                  </span>
                </div>

                <!-- 论文速览：一句话概括 + **完整摘要**（中英切换，不截断）。
                     核心创新在下一段独立成组，不用它顶替摘要。 -->
                <div class="pd-card pd-brief">
                  <div class="pd-card-head">
                    <h3 class="pd-h3"><AppIcon name="align-left" :size="18" />论文速览</h3>
                    <div class="seg pd-lang" v-if="hasBothLangs" role="group" aria-label="摘要语言">
                      <button type="button" :class="{ active: lang === 'zh' }"
                              :aria-pressed="lang === 'zh'" @click="lang = 'zh'">中文</button>
                      <button type="button" :class="{ active: lang === 'en' }"
                              :aria-pressed="lang === 'en'" @click="lang = 'en'">English</button>
                    </div>
                  </div>

                  <p v-if="paper?.one_liner" class="pd-oneliner">{{ paper.one_liner }}</p>

                  <!-- 摘要：完整显示（不 line-clamp、不 max-height），长了就让面板自己滚 -->
                  <p v-if="briefBody" class="pd-brief-text">{{ briefBody }}</p>
                  <p v-else class="pd-abstract-empty">
                    还没有摘要。可以用下面的按钮补全（先离线读 PDF，读不出来再问模型）。
                  </p>

                  <!-- 补全入口：放在卡底、只在确实缺东西时出现（不占首屏的视觉重量） -->
                  <div class="pd-brief-foot" v-if="metaMissing">
                    <span class="pd-hint">论文信息还没补全</span>
                    <span class="spacer" />
                    <button
                      type="button"
                      class="pd-ghost-btn"
                      :disabled="!!metaBusy"
                      title="离线读 PDF 首页与页产物：会议、年份、原文摘要、DOI、参考文献、阅读时间（不花钱）"
                      @click="fillMeta(false)"
                    >
                      <AppIcon name="search" :size="14" />{{ metaBusy === 'pdf' ? '识别中…' : '从论文识别' }}
                    </button>
                    <button
                      type="button"
                      class="pd-ghost-btn"
                      :disabled="!!metaBusy"
                      title="交给 agent 读全文填写：中英文关键词、中文摘要、领域、一句话概括、创新点、前置知识、难度"
                      @click="fillMeta(true)"
                    >
                      <AppIcon name="model" :size="14" />{{ metaBusy === 'agent' ? '已排队…' : '模型补全' }}
                    </button>
                  </div>
                </div>

                <!-- 核心创新：快速理解用，3~4 条、一行一条 -->
                <section class="pd-sec" v-if="contributions.length">
                  <h3 class="pd-h3"><AppIcon name="sparkle" :size="18" />核心创新</h3>
                  <ul class="pd-list">
                    <li v-for="(c, i) in contributions" :key="i">{{ c }}</li>
                  </ul>
                </section>

                <!-- 前置知识：辅助阅读，视觉权重低于速览与创新 -->
                <section class="pd-sec" v-if="prereq.length">
                  <h3 class="pd-h3"><AppIcon name="info" :size="18" />前置知识</h3>
                  <div class="pd-tags quiet">
                    <span class="pd-tag" v-for="(p, i) in prereq" :key="i">{{ p }}</span>
                  </div>
                </section>

                <!-- 推荐阅读路径：从真实目录里挑出的几节，点一行跳进阅读器对应位置。
                     这是**导航**，不是内容，所以做成一行一节的轻列表。 -->
                <section class="pd-sec" v-if="readingPath.length">
                  <h3 class="pd-h3"><AppIcon name="list" :size="18" />推荐阅读路径</h3>
                  <div class="pd-path">
                    <button
                      v-for="s in readingPath"
                      :key="s.key"
                      type="button"
                      class="pd-path-row"
                      :title="`在阅读器里打开：${s.title}`"
                      @click="openReader({ row: s.row, page: s.page })"
                    >
                      <span class="pd-path-no">{{ s.number }}</span>
                      <span class="pd-path-title">{{ s.title }}</span>
                      <span class="pd-path-page">第 {{ s.page }} 页</span>
                      <AppIcon name="chevron-right" :size="16" />
                    </button>
                  </div>
                </section>

                <!-- 处理流程：一条扁的里程碑条（解析 / 翻译 / 索引），回答"现在能不能读" -->
                <section class="pd-sec">
                  <h3 class="pd-h3">
                    <AppIcon name="bolt" :size="18" />处理流程
                    <button type="button" class="pd-icon-link" title="查看进度" @click="tab = 'progress'">
                      <AppIcon name="forward" :size="16" />
                    </button>
                  </h3>

                  <div class="pd-milestones">
                    <div class="pd-ms-bar" role="img" :aria-label="`处理流程：${milestones.map((m) => `${m.label}${m.text}`).join('，')}`">
                      <span
                        v-for="m in milestones"
                        :key="m.key"
                        class="pd-ms-seg"
                        :class="`is-${m.tone}`"
                      ><i :style="{ width: Math.round(m.frac * 100) + '%' }" /></span>
                    </div>
                    <div class="pd-ms-labels">
                      <span v-for="m in milestones" :key="m.key" :class="`is-${m.tone}`">
                        <strong>{{ m.label }}</strong><span :title="m.text">{{ m.text }}</span>
                      </span>
                    </div>
                  </div>
                </section>

                <!-- 论文档案：系统 / 归档信息（参考文献条数、大小、入库、知识库、标识、外部链接）。
                     全部保留，但**视觉权重最低** —— 概览的首屏不该由它们占着。 -->
                <section class="pd-sec pd-arch">
                  <h3 class="pd-h3"><AppIcon name="server" :size="18" />论文档案</h3>

                  <div class="pd-arch-grid" v-if="archFacts.length">
                    <div class="pd-arch-row" v-for="f in archFacts" :key="f.k">
                      <span>{{ f.k }}</span><strong :title="f.v">{{ f.v }}</strong>
                    </div>
                  </div>

                  <div class="pd-arch-row">
                    <span>标识</span>
                    <strong class="mono" :title="paperId"># {{ paperId }}</strong>
                    <button type="button" class="pd-copy" :title="copied ? '已复制' : '复制 paper_id'" @click="copyId">
                      <AppIcon :name="copied ? 'check' : 'copy'" :size="14" />
                    </button>
                  </div>

                  <div class="pd-links" v-if="paper?.doi || paper?.code_url">
                    <a v-if="paper?.doi" class="pd-ghost-btn"
                       :href="`https://doi.org/${paper.doi}`" target="_blank" rel="noopener">
                      <AppIcon name="link" :size="13" />DOI
                    </a>
                    <a v-if="paper?.code_url" class="pd-ghost-btn"
                       :href="paper.code_url" target="_blank" rel="noopener">
                      <AppIcon name="code" :size="13" />代码 / 项目
                    </a>
                  </div>

                  <div class="pd-manage-row">
                    <span class="pd-hint" v-if="paper?.meta_filled_at">
                      信息补全于 {{ fmtRelative(paper.meta_filled_at) }}
                    </span>
                    <span class="spacer" />
                    <button
                      type="button"
                      class="pd-ghost-btn danger"
                      :disabled="busy === 'delete'"
                      @click="confirmDelete = true"
                    >
                      <AppIcon name="trash" :size="14" />删除
                    </button>
                  </div>
                </section>
              </div>

              <!-- 阅读习惯：**每天的阅读时长**（本机那本按天的账）+ 总时长与打开信息。
                   阅读信息（次数 / 时长 / 最近打开）原先挂在「进度」里，用户口径 2026-11 搬到本页签 ——
                   「进度」讲的是处理流程与任务，阅读行为不属于它。 -->
              <div v-show="tab === 'habit'" class="pd-habit">
                <div class="pd-card">
                  <div class="pd-card-head">
                    <h3 class="pd-h3"><AppIcon name="clock" :size="18" />每日阅读时长</h3>
                    <div class="seg" role="group" aria-label="时间范围">
                      <button type="button" :class="{ active: habitRange === 7 }"
                              :aria-pressed="habitRange === 7" @click="habitRange = 7">7 天</button>
                      <button type="button" :class="{ active: habitRange === 30 }"
                              :aria-pressed="habitRange === 30" @click="habitRange = 30">30 天</button>
                    </div>
                  </div>

                  <template v-if="habitSum > 0">
                    <svg
                      class="pd-habit-spark"
                      :viewBox="'0 0 ' + HABIT_W + ' ' + HABIT_H"
                      preserveAspectRatio="none"
                      role="img"
                      :aria-label="'最近 ' + habitRange + ' 天每日阅读时长，峰值 ' + fmtReadDuration(habitPeak)"
                    >
                      <path class="pd-habit-area" :d="habitArea" />
                      <path class="pd-habit-line" :d="habitLine" />
                      <circle
                        v-for="pt in habitPoints"
                        :key="pt.date"
                        class="pd-habit-dot"
                        :cx="pt.x"
                        :cy="pt.y"
                        r="2.2"
                      >
                        <title>{{ pt.date }} · {{ fmtReadDuration(pt.seconds) }}</title>
                      </circle>
                    </svg>
                    <div class="pd-habit-x" aria-hidden="true">
                      <span
                        v-for="tk in habitTicks"
                        :key="tk.key"
                        :style="{ left: tk.left, transform: tk.shift }"
                      >{{ tk.text }}</span>
                    </div>
                  </template>
                  <p v-else class="pd-abstract-empty">
                    最近 {{ habitRange }} 天还没有阅读记录 —— 在阅读器里读一会儿，这里会画出每天读了多久。
                  </p>

                  <div class="pd-habit-foot">
                    <span>峰值 {{ fmtReadDuration(habitPeak) }}</span>
                    <span>{{ habitRange }} 天合计 {{ fmtReadDuration(habitSum) }}</span>
                    <span class="spacer" />
                    <span class="pd-hint">本机记录</span>
                  </div>
                </div>

                <div class="pd-card">
                  <div class="pd-card-head">
                    <h3 class="pd-h3"><AppIcon name="gauge" :size="18" />阅读总览</h3>
                    <span class="pd-hint">本机统计</span>
                  </div>
                  <div class="pd-kv"><span>总阅读时长</span><strong>{{ readDuration }}</strong></div>
                  <div class="pd-kv">
                    <span>阅读次数</span>
                    <strong>{{ localStats.opens ? `${localStats.opens} 次` : '—' }}</strong>
                  </div>
                  <div class="pd-kv"><span>最近打开</span><strong>{{ lastOpenText }}</strong></div>
                  <p v-if="!hasLocalStats" class="pd-hint">这台机器上还没有打开过这篇论文。</p>
                </div>
              </div>

              <!-- 参考文献：离线从页产物的 `ref` 行（或 Markdown 的 References 节）抽出来，
                   原文条目原样保留。卡片与跳转**与阅读器侧栏用的是同一个组件**
                   （`ReferenceList`）：点一条去发表网站（DOI / arXiv / 原文链接，兜底学术搜索）。 -->
              <div v-show="tab === 'refs'" class="pd-refs">
                <p class="pd-hint" v-if="references.length">
                  共 {{ references.length }} 条（按论文里的顺序）。点条目跳到发表网站。
                </p>
                <ReferenceList v-if="references.length" :items="references" />
                <p v-else class="pd-abstract-empty">
                  还没有参考文献。到「概览」的「论文速览」卡点「从论文识别」离线抽一次
                  （读的是解析出来的参考文献块，不花钱）。
                </p>
              </div>

              <!-- 进度 -->
              <div v-show="tab === 'progress'" class="pd-progress">
                <ol class="pd-stages">
                  <li v-for="s in stages" :key="s.key" :class="{ done: s.done, active: s.active }">
                    <span class="pd-stage-dot"><AppIcon :name="s.done ? 'check' : 'more'" :size="12" /></span>
                    <span class="pd-stage-label">{{ s.label }}</span>
                  </li>
                </ol>

                <div v-if="liveTask" class="pd-live">
                  <svg class="spinner" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /></svg>
                  <div>
                    <strong>{{ taskStatus(String(liveTask.status)).label }}</strong>
                    <span>{{ liveTask.message || '正在处理…' }}</span>
                  </div>
                </div>

                <div class="pd-progress-row" v-if="mdProgress">
                  <div class="pd-progress-top">
                    <span>逐页翻译</span>
                    <span class="tnum">{{ mdProgress.done }} / {{ mdProgress.total }}</span>
                  </div>
                  <div class="bar"><i :style="{ width: mdProgress.pct + '%' }" /></div>
                </div>
                <div class="pd-progress-row" v-if="pdfProgress">
                  <div class="pd-progress-top">
                    <span>版面 PDF 翻译</span>
                    <span class="tnum">{{ pdfProgress.done }} / {{ pdfProgress.total }}</span>
                  </div>
                  <div class="bar"><i :style="{ width: pdfProgress.pct + '%' }" /></div>
                </div>
                <p class="tiny faint" v-if="!mdProgress && !pdfProgress">
                  还没有翻译产物。在论文库卡片上点「翻译」即可开始。
                </p>

                <div class="pd-card" v-if="tasks.length">
                  <div class="pd-card-head"><h3 class="pd-h3">任务记录</h3></div>
                  <div class="pd-task-row" v-for="t in tasks.slice(0, 8)" :key="t.id">
                    <span class="badge" :class="taskStatus(String(t.status)).cls">
                      {{ TASK_TYPE_LABEL[String(t.type)] || t.type }}
                    </span>
                    <span class="pd-task-msg truncate" :title="t.message || ''">{{ t.message || '—' }}</span>
                    <span class="spacer" />
                    <span class="tiny faint nowrap">{{ taskStatus(String(t.status)).label }} · {{ fmtTime(t.updated_at || t.created_at) }}</span>
                  </div>
                </div>

                <div class="pd-card pd-activity">
                  <div class="pd-card-head">
                    <h3 class="pd-h3">最近活动</h3>
                  </div>
                  <ol>
                    <li v-for="a in activity" :key="a.id">
                      <span class="pd-act-icon" :class="`is-${a.tone}`"><AppIcon :name="a.icon" :size="16" /></span>
                      <div><strong>{{ a.title }}</strong><span>{{ a.text }}</span></div>
                      <time>{{ a.time }}</time>
                    </li>
                    <li v-if="!activity.length" class="pd-act-empty">任务开始后，进度记录会显示在这里。</li>
                  </ol>
                </div>
              </div>

              <!-- 文件 -->
              <div v-show="tab === 'files'" class="pd-files">
                <button
                  v-for="a in artifacts"
                  :key="a.key"
                  type="button"
                  class="pd-file-row"
                  :disabled="!!downloading"
                  @click="download(a)"
                >
                  <span class="pd-file-icon"><AppIcon :name="a.icon" :size="18" /></span>
                  <span class="pd-file-copy">
                    <strong>{{ a.label }}</strong>
                    <span class="mono tiny">{{ a.filename }}</span>
                  </span>
                  <span class="spacer" />
                  <AppIcon :name="downloading === a.key ? 'refresh' : 'download'" :size="16" :class="{ spin: downloading === a.key }" />
                </button>
                <p class="tiny faint">
                  打包档含原 PDF、三个版本的 Markdown 与正文引用的插图；图片是相对路径，解压后离线可看。
                </p>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>

    <ConfirmModal
      :open="confirmDelete"
      title="删除论文"
      body="会清理索引、知识库记录，以及**属于这篇论文的对话历史、划词解释、高亮与笔记**；PDF 文件移入 data/trash（可从回收站恢复）。"
      :target="title"
      confirm-label="删除"
      danger
      :busy="busy === 'delete'"
      @confirm="doDelete"
      @cancel="confirmDelete = false"
    />
  </Teleport>
</template>

<style scoped>
/* ---------------- 弹窗外壳 ----------------
 * 遮罩与 `Modal.vue` 同一套（`rgba(12,16,24,.45)`）：同一站里两种遮罩深度会看出破绽。
 * 弹窗比通用 Modal 宽且高（内容两栏），圆角 24px（= `--r-lg`，与全站弹窗一致）。 */
.pd-mask {
  position: fixed; inset: 0; z-index: 160;
  display: flex; align-items: center; justify-content: center; padding: 20px;
  background: rgba(12, 16, 24, .45);
}
.pd-dialog {
  position: relative;
  display: flex; flex-direction: column;
  width: min(1040px, 100%); height: min(720px, 100%);
  overflow: hidden;
  background: var(--bg-elev); color: var(--text);
  border: 1px solid var(--border); border-radius: var(--r-lg);
  box-shadow: var(--shadow-flyout);
}
.pd-head {
  flex: none; display: flex; align-items: center; gap: 10px;
  padding: 16px 56px 12px 22px; border-bottom: 1px solid var(--divider);
}
.pd-head-title {
  margin: 0; min-width: 0; font-size: calc(21px * var(--ts, 1)); font-weight: 600; letter-spacing: -.02em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pd-close {
  position: absolute; right: 20px; top: 14px; z-index: 2;
  display: inline-flex; align-items: center; justify-content: center;
  width: 34px; height: 34px; border-radius: 999px;
  color: var(--text-dim); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease), transform var(--dur-fast) var(--ease);
}
.pd-close:hover { background: var(--bg-hover); color: var(--text); }
.pd-close:active { transform: scale(.94); }

.pd-state {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px;
  color: var(--text-dim); font-size: calc(13px * var(--ts, 1));
}

/* ---------------- 两栏骨架 ---------------- */
.pd-grid {
  flex: 1; min-height: 0; display: grid;
  grid-template-columns: minmax(240px, 284px) minmax(0, 1fr);
}
.pd-left {
  min-height: 0; padding: 18px 18px 10px 22px; overflow-y: auto; overscroll-behavior: contain;
  display: flex; flex-direction: column; gap: 14px;
  border-right: 1px solid var(--divider);
  background: color-mix(in srgb, var(--bg) 42%, var(--bg-elev));
}
/* 左栏的每一块都**不参与压缩**（`flex: none`）：左栏内容超出时让它自己滚动，
   而不是把封面/标题挤扁 —— 那正是"页面内容被压缩"的来源。 */
.pd-left > * { flex: none; }
.pd-right {
  min-height: 0; padding: 16px 20px 20px;
  display: flex; flex-direction: column; gap: 12px;
}

/* ---------------- 左栏：封面 ----------------
 * 框的比例 = 首页图自己的比例（`--cover-ratio` 由 `<img @load>` 量出来，见脚本）。
 * 比例对上之后 `object-fit: contain` 既不裁也不留边，论文页原样呈现。 */
.pd-cover {
  width: 100%; aspect-ratio: var(--cover-ratio, 1 / 1.29); overflow: hidden;
  border: 1px solid var(--border); border-radius: 16px;
  background: color-mix(in srgb, var(--text) 4%, transparent);
  box-shadow: var(--shadow-2);
}
.pd-cover img { display: block; width: 100%; height: 100%; object-fit: contain; background: var(--bg-elev); }

.pd-identity { display: grid; gap: 5px; }
.pd-identity h3 {
  display: flex; align-items: flex-start; gap: 6px; margin: 0;
  font-size: calc(18px * var(--ts, 1)); font-weight: 600; line-height: 1.4; letter-spacing: -.02em;
  color: var(--text);
}
.pd-identity h3 :deep(.icon) { margin-top: 2.5px; }
/* 标题本体才是要截断的那个（flex 容器不能自己 line-clamp） */
.pd-id-title {
  min-width: 0;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.pd-identity p {
  display: flex; align-items: center; gap: 6px; margin: 0; min-width: 0;
  font-size: calc(13px * var(--ts, 1)); line-height: 1.65; color: var(--text-dim);
}
.pd-identity p span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pd-copy {
  margin-left: auto; display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; flex: none; border-radius: 6px;
  color: var(--text-dim); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.pd-copy:hover { background: var(--bg-hover); color: var(--text); }

/* ---------------- 左栏：阅读（唯一主操作） ----------------
 * 只有一个按钮：原文 / 对照 / 翻译是**阅读器内部状态**，文件导出是「文件」页签的事 ——
 * 左栏不重复摆第二个入口。按钮占满整栏宽度，是这一栏唯一的强调色。 */
.pd-actions { display: flex; align-items: center; gap: 8px; }
.pd-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  flex: 1; min-width: 0; height: 36px; border-radius: var(--r-sm);
  border: 1px solid var(--border-strong); background: var(--bg-elev); color: var(--text);
  font-family: inherit; font-size: calc(13px * var(--ts, 1)); font-weight: 500; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.pd-btn:hover { background: var(--bg-hover); border-color: var(--border-strong); }
.pd-btn.primary { background: var(--accent); border-color: transparent; color: var(--text-on-accent); }
.pd-btn.primary:hover { background: var(--accent-hover); }

/* ---------------- 左栏：关键词 ----------------
 * 关键词是论文身份的延伸，所以跟封面 / 标题 / 阅读键同一栏；中英同一块区域切换。
 * 与右侧的分组同一条分隔线的语言（`.pd-sec` 的观感），只把标题换成"标签 + 切换器"。 */
.pd-kw-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.spin { animation: spin .85s linear infinite; }

/* ---------------- 右栏：页签 ---------------- */
.pd-tabs {
  display: inline-flex; gap: 4px; padding: 3px; flex: none;
  width: fit-content; max-width: calc(100% - 40px);
  border: 1px solid var(--border); border-radius: 12px;
  background: color-mix(in srgb, var(--text) 4%, transparent);
}
.pd-tab {
  display: inline-flex; align-items: center; gap: 6px;
  min-height: 34px; padding: 5px 14px; border-radius: 9px;
  color: var(--text-dim); font-family: inherit; font-size: calc(15px * var(--ts, 1)); font-weight: 550;
  cursor: pointer; white-space: nowrap;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease), transform var(--dur-fast) var(--ease);
}
.pd-tab:hover { background: var(--bg-hover); color: var(--text); }
.pd-tab:active { transform: scale(.97); }
.pd-tab.active { background: var(--accent); color: var(--text-on-accent); }

.pd-panel {
  flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
  padding-right: 5px; padding-bottom: 4px;
}

/* ---------------- 概览：论文属性（最顶部的一条） ----------------
 * 领域 / 类型 / 难度 / 推荐阅读时间：一行「16px 线性图标 + 值」，横向铺开、自动换行。
 * 它是概览的第一眼信息，所以给一层极淡的底把它托住 —— 但**不是一张大卡片**。
 * 图标只做视觉辅助：中性灰、不抢字的注意力。 */
.pd-attrs {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px;
  padding: 11px 14px; border-radius: var(--r-sm);
  background: color-mix(in srgb, var(--bg) 55%, var(--bg-elev));
}
.pd-attr { display: inline-flex; align-items: center; gap: 7px; min-width: 0; line-height: 20px; }
.pd-attr :deep(.icon) { flex: none; color: var(--text-faint); }
.pd-attr > i { font-style: normal; flex: none; font-size: calc(13px * var(--ts, 1)); font-weight: 400; color: var(--text-dim); }
.pd-attr > strong {
  font-size: calc(15px * var(--ts, 1)); font-weight: 600; letter-spacing: -.01em; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 26ch;
}

/* ---------------- 概览：论文速览（主卡） ----------------
 * 摘要按**正文**排：16px / 行高 1.85 / 常规字重 —— 它是"读的内容"，不是标题。
 * **完整显示**（不 line-clamp、不 max-height、不加省略号），长了就让 `.pd-panel` 自己滚。
 * 简约靠"少几个大卡片"，不靠裁掉摘要。 */
.pd-overview { display: grid; gap: 14px; }
.pd-brief { gap: 12px; }
.pd-brief-text {
  margin: 0; max-width: var(--measure);
  font-size: calc(16px * var(--ts, 1)); line-height: 1.85; font-weight: 400; color: var(--text);
  overflow-wrap: anywhere; white-space: pre-wrap;
}
.pd-abstract-empty { margin: 0; font-size: calc(13px * var(--ts, 1)); color: var(--text-faint); }
.pd-tags { display: flex; flex-wrap: wrap; gap: 4px 6px; }
/* 关键词 / 前置知识的标签：**紧凑但可换行**。
 * 论文关键词常常是一整串英文（`multi-modal fake news detection`），
 * 所以给 `min-height` 而不是固定 `height` —— 一行放不下时标签自己长高一行，
 * 不会溢出左栏，也不会把行高撑得参差不齐。 */
.pd-tag {
  display: inline-flex; align-items: center;
  min-height: 22px; padding: 1px 9px; max-width: 100%;
  border-radius: 999px; background: var(--bg-elev); border: 1px solid var(--border);
  font-size: calc(11.5px * var(--ts, 1)); font-weight: 400; line-height: 1.5; color: var(--text-dim);
  overflow-wrap: anywhere;
}
/* 前置知识：比关键词再轻一档（同一枚标签，去掉描边、底色沉下去） */
.pd-tags.quiet .pd-tag { background: var(--bg-sunken); border-color: transparent; color: var(--text-dim); }

/* 中 / English：轻量胶囊（复用全站 `.seg` 语言，只把它压小一档） */
.pd-lang { height: 28px; padding: 2px; flex: none; }
.pd-lang :deep(button) { height: 24px; padding: 0 11px; font-size: calc(13px * var(--ts, 1)); font-weight: 500; }

/* 补全入口：卡底一条，只在缺信息时出现 */
.pd-brief-foot { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding-top: 2px; }

/* ---------------- 概览：Section 标题（全页统一的一档） ----------------
 * 字号 18 / 字重 600：层级靠**字号 + 间距 + 灰度**拉开，不靠加粗。
 * 图标一律 18px 线性、中性灰，**不再套 36~40px 的圆角底**（那个容器视觉重量太大）。 */
.pd-h3 {
  display: flex; align-items: center; gap: 8px; margin: 0; min-width: 0;
  font-size: calc(18px * var(--ts, 1)); font-weight: 600; letter-spacing: -.02em; color: var(--text);
}
.pd-h3 :deep(.icon) { flex: none; color: var(--text-dim); }

/* ---------------- 概览：轻分组（分组标题 + 内容） ----------------
 * 除了速览那一张卡，其余信息不再各自占一张大卡：一条细分隔线 + 分组标题足够分层，
 * 省下来的纵向空间留给论文内容本身。左栏的「关键词」用同一条分隔线的语言，
 * 但**比右侧 Section 低一档**（它是辅助信息，15px/500，见 `.pd-kw-head h3`）。 */
.pd-sec, .pd-kw { display: grid; gap: 10px; padding-top: 16px; border-top: 1px solid var(--divider); }
.pd-kw-head > h3 {
  display: flex; align-items: center; gap: 8px; margin: 0; flex: none;
  font-size: calc(15px * var(--ts, 1)); font-weight: 500; letter-spacing: -.01em; color: var(--text);
}

/* 推荐阅读路径：一行一节（编号 / 标题 / 页码），点一行跳进阅读器 */
.pd-path { display: grid; gap: 1px; }
.pd-path-row {
  display: flex; align-items: center; gap: 10px; width: 100%;
  min-height: 32px; padding: 5px 9px; border-radius: var(--r-sm);
  border: 0; background: transparent; color: var(--text);
  font-family: inherit; text-align: left; cursor: pointer;
  transition: background var(--dur) var(--ease);
}
.pd-path-row:hover { background: var(--bg-hover); }
.pd-path-row:focus-visible { outline: none; box-shadow: var(--ring); }
.pd-path-no { flex: none; min-width: 32px; font-family: var(--mono); font-size: calc(12.5px * var(--ts, 1)); color: var(--text-faint); }
.pd-path-title { flex: 1; min-width: 0; font-size: calc(14px * var(--ts, 1)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pd-path-page { flex: none; font-size: calc(12.5px * var(--ts, 1)); color: var(--text-faint); }
.pd-path-row :deep(.icon) { flex: none; color: var(--text-faint); }

/* 论文档案：键值两列 + 标识行，视觉权重最低（靠**灰度**降档，不靠缩小字号） */
.pd-arch-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px 18px; }
.pd-arch-row { display: flex; align-items: baseline; gap: 8px; min-width: 0; font-size: calc(13px * var(--ts, 1)); }
.pd-arch-row > span { flex: none; min-width: 58px; color: var(--text-faint); }
.pd-arch-row > strong {
  flex: 1; min-width: 0; font-weight: 500; color: var(--text-dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pd-arch .pd-manage-row { padding-top: 2px; }

/* ---------------- 概览：论文速览的正文与列表 ----------------
 * 一句话概括是**引导句**，不是标题：16px / 500（整句加粗会读成标题）。
 * 核心创新按正文排（15px / 400），比摘要低一档。 */
.pd-oneliner {
  margin: 0; max-width: var(--measure);
  font-size: calc(16px * var(--ts, 1)); line-height: 1.8; font-weight: 500; color: var(--text);
}
.pd-list { margin: 0; padding-left: 20px; display: grid; gap: 6px; }
.pd-list > li { font-size: calc(15px * var(--ts, 1)); font-weight: 400; line-height: 1.8; color: var(--text); }
.pd-links { display: flex; flex-wrap: wrap; gap: 6px; }
.pd-links > a { text-decoration: none; }

/* ---------------- 参考文献页签 ----------------
 * 卡片本体（含跳转图标与样式）在 `ReferenceList` 里 —— 与阅读器侧栏共用同一份
 * （侧栏是窄栏，字号按那边定；这里只留一个列的间距）。 */
.pd-refs { display: grid; gap: 8px; align-content: start; }

.pd-card {
  display: grid; align-content: start; gap: 12px; padding: 14px 16px; border-radius: 21px;
  background: color-mix(in srgb, var(--bg) 55%, var(--bg-elev));
}
.pd-card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.pd-icon-link {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; margin-left: 2px; border-radius: 999px;
  background: transparent; color: var(--text-faint); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease), transform var(--dur) var(--ease);
}
.pd-icon-link:hover { background: var(--bg-hover); color: var(--text); }
.pd-icon-link:active { transform: scale(.94); }

/* ---------------- 处理流程：分段里程碑条（最扁的一条） ----------------
 * 三段等宽（解析 / 翻译 / 索引），每段自己按进度填充：完成=满格、进行中=真实比例（至少 20%）、
 * 未开始=空。段底下同一列宽写标签与状态 —— 只花两行高度。 */
.pd-milestones { display: grid; gap: 7px; }
.pd-ms-bar { display: flex; gap: 3px; height: 8px; }
.pd-ms-seg {
  position: relative; flex: 1 1 0; min-width: 0;
  border-radius: 999px; overflow: hidden;
  background: color-mix(in srgb, var(--text) 12%, transparent);
}
.pd-ms-seg > i {
  display: block; height: 100%; border-radius: 999px; background: var(--accent);
  transition: width var(--dur-slow) var(--ease);
}
/* 进行中：用浅一档的强调色，和"已完成（实心）"区分开 */
.pd-ms-seg.is-active > i { background: color-mix(in srgb, var(--accent) 55%, transparent); }
.pd-ms-labels { display: flex; gap: 3px; }
.pd-ms-labels > span {
  flex: 1 1 0; min-width: 0; display: flex; align-items: baseline; gap: 6px;
  font-size: calc(12.5px * var(--ts, 1)); color: var(--text-dim);
}
.pd-ms-labels > span > strong { font-size: calc(12.5px * var(--ts, 1)); font-weight: 550; color: var(--text); }
.pd-ms-labels > span > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pd-ms-labels > span.is-muted > span { color: var(--text-faint); }
.pd-ms-labels > span.is-failed > span { color: var(--danger); }

/* ---------------- 键值行（进度页的阅读记录） ---------------- */
.pd-kv { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.pd-kv > span { flex: none; width: 72px; font-size: calc(12.5px * var(--ts, 1)); color: var(--text-dim); }
.pd-kv > strong {
  flex: 1; min-width: 0; font-size: calc(14px * var(--ts, 1)); font-weight: 500; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pd-hint { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }

/* ---------------- 阅读习惯页签 ----------------
 * 上：每日阅读时长折线（画法同工作区「阅读统计」—— 拉满 + non-scaling-stroke 顶住线宽）；
 * 下：总量与打开信息。横轴刻度**按百分比定位**：30 天里两端要贴边、中间要压在折线点上，
 * `justify-content: space-between` 做不到这件事。 */
.pd-habit { display: grid; gap: 12px; }
.pd-habit-spark { display: block; width: 100%; height: 150px; overflow: visible; }
.pd-habit-line {
  fill: none; stroke: var(--accent); stroke-width: 1.8;
  stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke;
}
.pd-habit-area { fill: color-mix(in srgb, var(--accent) 12%, transparent); stroke: none; }
.pd-habit-dot { fill: var(--accent); }
.pd-habit-x {
  position: relative; height: 15px;
  font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); font-variant-numeric: tabular-nums;
}
.pd-habit-x > span { position: absolute; top: 0; white-space: nowrap; }
.pd-habit-foot {
  display: flex; align-items: baseline; gap: 14px;
  font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); font-variant-numeric: tabular-nums;
}

.pd-manage-row { display: flex; align-items: center; gap: 8px; }
.pd-ghost-btn {
  display: inline-flex; align-items: center; gap: 6px;
  min-height: 32px; padding: 0 12px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text-dim);
  font-family: inherit; font-size: calc(13px * var(--ts, 1)); font-weight: 500; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.pd-ghost-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); border-color: var(--border-strong); }
.pd-ghost-btn.danger:hover:not(:disabled) { background: var(--danger-soft); color: var(--danger); border-color: color-mix(in srgb, var(--danger) 40%, transparent); }
.pd-ghost-btn:disabled { opacity: .55; cursor: default; }

/* ---------------- 最近活动（进度页：阅读行为与任务的时间线） ---------------- */
.pd-activity ol { display: grid; gap: 0; margin: 0; padding: 0; list-style: none; }
.pd-activity li {
  display: grid; grid-template-columns: 32px minmax(0, 1fr) auto;
  align-items: center; gap: 9px; min-height: 44px;
}
.pd-activity li + li { margin-top: 5px; }
.pd-act-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 32px; height: 32px; border-radius: 11px;
  background: var(--bg-elev); color: var(--text-dim);
}
.pd-act-icon.is-done, .pd-act-icon.is-active { color: var(--text); }
.pd-act-icon.is-failed { color: var(--danger); }
.pd-activity li > div { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.pd-activity li strong { font-size: calc(13px * var(--ts, 1)); font-weight: 550; color: var(--text); }
.pd-activity li span, .pd-activity time { font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); }
.pd-activity time { white-space: nowrap; }
.pd-act-empty { grid-column: 1 / -1; font-size: calc(12.5px * var(--ts, 1)); color: var(--text-dim); }

/* ---------------- 进度页签 ---------------- */
.pd-progress { display: grid; gap: 12px; }
.pd-stages { display: flex; align-items: center; gap: 6px; margin: 0; padding: 0; list-style: none; }
.pd-stages li {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 12px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--bg-elev); color: var(--text-dim);
  font-size: calc(13px * var(--ts, 1));
}
.pd-stages li.done { color: var(--text); border-color: color-mix(in srgb, var(--ok) 34%, var(--border)); }
.pd-stages li.active { background: var(--accent-soft); color: var(--accent-text); border-color: transparent; }
.pd-stage-dot { display: inline-flex; }

.pd-live {
  display: flex; align-items: center; gap: 12px; padding: 12px 14px;
  border-radius: var(--r-sm); background: var(--accent-soft); color: var(--accent-text);
}
.pd-live .spinner { width: 28px; height: 28px; }
.pd-live > div { display: grid; gap: 1px; min-width: 0; }
.pd-live strong { font-size: calc(13px * var(--ts, 1)); font-weight: 600; }
.pd-live span { font-size: calc(12.5px * var(--ts, 1)); opacity: .9; }

.pd-progress-row { display: grid; gap: 5px; }
.pd-progress-top {
  display: flex; align-items: center; font-size: calc(13px * var(--ts, 1)); color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}
.pd-progress-top > span:last-child { margin-left: auto; }

.pd-task-row { display: flex; align-items: center; gap: 8px; min-height: 28px; }
.pd-task-msg { font-size: calc(13px * var(--ts, 1)); color: var(--text-dim); min-width: 0; }

/* ---------------- 文件页签 ---------------- */
.pd-files { display: grid; gap: 8px; }
.pd-file-row {
  display: flex; align-items: center; gap: 12px; width: 100%;
  padding: 12px 14px; border-radius: var(--r-sm); text-align: left;
  border: 1px solid var(--border); background: var(--bg-elev); color: var(--text);
  cursor: pointer;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.pd-file-row:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border-strong)); background: var(--bg-hover);
}
.pd-file-row:disabled { opacity: .6; cursor: default; }
.pd-file-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 36px; height: 36px; flex: none; border-radius: 12px;
  background: var(--bg-sunken); color: var(--text);
}
.pd-file-copy { display: grid; gap: 1px; min-width: 0; }
.pd-file-copy strong { font-size: calc(14px * var(--ts, 1)); font-weight: 550; }
.pd-file-copy span { color: var(--text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ---------------- 窄屏：单栏（封面缩到一条，右侧占满） ---------------- */
@media (max-width: 760px) {
  .pd-dialog { width: calc(100dvw - 20px); height: calc(100dvh - 20px); }
  .pd-grid { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }
  .pd-left {
    flex-direction: row; flex-wrap: wrap; align-items: flex-start; gap: 12px;
    padding: 12px; overflow: hidden;
    border-right: 0; border-bottom: 1px solid var(--divider);
  }
  .pd-cover { width: 62px; flex: none; }
  .pd-identity { flex: 1; min-width: 0; }
  .pd-actions { flex-direction: column; flex: none; }
  /* 关键词换到第二行（窄屏一行放不下四块） */
  .pd-kw { flex: 1 1 100%; padding-top: 10px; }
  .pd-right { padding: 10px 12px 12px; }
  /* 页签在窄屏**换行**：五个页签的 nowrap 宽度会顶穿弹窗（弹窗宽度是 `100dvw - 20px`，
     但子元素的 min-content 比它宽时整张纸会被撑出去，标题栏和关闭键跟着跑出屏幕）。 */
  .pd-tabs { flex-wrap: wrap; width: 100%; max-width: 100%; }
  .pd-arch-grid { grid-template-columns: minmax(0, 1fr); }
}

@media (prefers-reduced-motion: reduce) {
  .spin { animation: none; }
}
</style>
