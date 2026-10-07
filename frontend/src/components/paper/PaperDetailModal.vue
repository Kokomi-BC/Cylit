<script setup lang="ts">
/** 论文详情弹窗（移植自参照项目 retain-pdf 的 BookDetailDialog，图2 那一屏）。
 *
 * 两栏：**左封面栏**（封面 / 身份 / 对照阅读 · 查看原版 / 文件下载）+ **右内容栏**（概览 · 进度 · 文件）。
 *
 * 数据都在打开时现拉（`GET /papers/{id}` + `/progress` + `/tasks` + 一次 HEAD 取文件大小）：
 * 详情是"看一眼就走"的浮层，不该为了它把整份 reader payload 拉下来；
 * 论文列表里的那一行可能已经旧了（后台任务在跑），所以以这里拉到的那份为准。
 *
 * 三个页签的面板**全都挂载**（用 `v-show`），切页签不丢滚动位置与下载中的状态。
 */

import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { api, assetUrl, downloadBlob } from '@/api/client';
import type { Paper, TaskInfo } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import ConfirmModal from '@/components/ConfirmModal.vue';
import { openTab } from '@/stores/tabs';
import { reportError, toast } from '@/stores/app';
import { TASK_TYPE_LABEL, fmtRelative, taskStatus } from '@/utils/format';
import { fmtReadDuration, readReaderStats, type ReaderStats } from '@/utils/readerStats';

const props = defineProps<{ paperId: string }>();
const emit = defineEmits<{ (e: 'close'): void; (e: 'changed'): void }>();

const router = useRouter();

type TabKey = 'overview' | 'refs' | 'progress' | 'files';
const TABS: ReadonlyArray<{ key: TabKey; label: string; icon: string }> = [
  { key: 'overview', label: '概览', icon: 'document' },
  { key: 'refs', label: '参考文献', icon: 'list' },
  { key: 'progress', label: '进度', icon: 'task' },
  { key: 'files', label: '文件', icon: 'layers' },
];
const tab = ref<TabKey>('overview');

const paper = ref<Paper | null>(null);
const tasks = ref<TaskInfo[]>([]);
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
  document.addEventListener('keydown', onKey);
});
onUnmounted(() => document.removeEventListener('keydown', onKey));

// 换了论文（同一个弹窗换个 id）就重新拉
watch(() => props.paperId, () => { tab.value = 'overview'; void load(); });

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

/** 摘要与标签：都在 `GET /papers/{id}` 那一份里（`abstract` / `tags`），不需要另开接口。 */
const abstractText = computed(() => (paper.value?.abstract || '').trim());
const tags = computed(() => (paper.value?.tags || []).filter((t) => String(t).trim()));

/* ------------------------------ 元数据扩展（2026-10-07） ------------------------------ */

/** 中文摘要（模型的译文）。 */
const abstractZh = computed(() => (paper.value?.abstract_zh || '').trim());
const contributions = computed(() => (paper.value?.contributions || []).filter((t) => String(t).trim()));
const prereq = computed(() => (paper.value?.prereq || []).filter((t) => String(t).trim()));
const keywordsEn = computed(() => (paper.value?.keywords_en || []).filter((t) => String(t).trim()));
const references = computed(() => paper.value?.references || []);

/** 「论文信息」卡上的键值行：**没值的项不画**（空行比没有更烦人）。 */
const metaFacts = computed<{ k: string; v: string }[]>(() => {
  const p = paper.value;
  if (!p) return [];
  const out: { k: string; v: string }[] = [];
  if (p.domain) out.push({ k: '领域', v: p.domain });
  if (p.difficulty) out.push({ k: '难度', v: p.difficulty });
  if (p.paper_type) out.push({ k: '类型', v: p.paper_type });
  if (p.read_time_min) out.push({ k: '阅读时间', v: `约 ${p.read_time_min} 分钟` });
  if (references.value.length) out.push({ k: '参考文献', v: `${references.value.length} 条` });
  return out;
});

/** 每一项的来源（`pdf` / `model` / `agent` / `manual`）——鼠标停在字段上才看得到。 */
function sourceOf(key: string): string {
  const s = paper.value?.meta_sources?.[key] || '';
  return {
    pdf: '从论文里读出来的', model: '模型读抽样内容填的',
    agent: 'agent 读全文填的', manual: '手动填的',
  }[s] || '';
}

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
const readOpens = computed(() => localStats.value.opens);
const readDuration = computed(() => fmtReadDuration(localStats.value.seconds));
const lastOpenText = computed(() => (localStats.value.lastOpen
  ? fmtRelative(localStats.value.lastOpen)
  : '—'));
const hasLocalStats = computed(() => localStats.value.opens > 0 || localStats.value.seconds > 0);

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

/** 打开阅读器：顺手把视图模式记进进度（阅读器读它决定首屏是原文还是对照）。 */
async function openReader(viewMode: 'bilingual' | 'original') {
  try {
    await api.put(`/papers/${props.paperId}/progress`, { view_mode: viewMode });
  } catch {
    /* 记不上也该让人读得到：阅读器有本地偏好兜底 */
  }
  void router.push(openTab({ name: 'reader', params: { paperId: props.paperId } }) as never);
}

interface ArtifactDef {
  key: string;
  label: string;
  icon: string;
  path: string;
  filename: string;
  query?: Record<string, string>;
}

/** 五块产物：与后端**实际有**的导出接口一一对应（没有的不摆，摆了就是骗人）。 */
const artifacts = computed<ArtifactDef[]>(() => [
  { key: 'source', label: '原始 PDF', icon: 'document', path: `/papers/${props.paperId}/file`, filename: `${props.paperId}.pdf` },
  { key: 'bilingual', label: '对照 Markdown', icon: 'panel', path: `/papers/${props.paperId}/markdown/export`, filename: `${props.paperId}-bilingual.md`, query: { mode: 'bilingual' } },
  { key: 'translation', label: '译文 Markdown', icon: 'translate', path: `/papers/${props.paperId}/markdown/export`, filename: `${props.paperId}-translation.md`, query: { mode: 'translation' } },
  { key: 'original', label: '原文 Markdown', icon: 'code', path: `/papers/${props.paperId}/markdown/export`, filename: `${props.paperId}-original.md`, query: { mode: 'original' } },
  { key: 'zip', label: '打包（含插图）', icon: 'layers', path: `/papers/${props.paperId}/export/zip`, filename: `${props.paperId}.zip` },
]);

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
          <!-- ---------------- 左：封面 + 身份 + 按钮 + 文件下载 ---------------- -->
          <aside class="pd-left">
            <div class="pd-cover">
              <img :src="coverUrl" :alt="`${title} 首页`" loading="lazy" decoding="async" />
            </div>

            <div class="pd-identity">
              <h3>
                <AppIcon name="document" :size="13" />
                <span class="pd-id-title" :title="title">{{ title }}</span>
              </h3>
              <p><AppIcon name="note" :size="13" /><span>{{ authors }}</span></p>
              <p v-if="metaLine"><AppIcon name="document" :size="13" /><span>{{ metaLine }}</span></p>
              <p>
                <AppIcon name="link" :size="13" /><span class="mono"># {{ paperId }}</span>
                <button type="button" class="pd-copy" :title="copied ? '已复制' : '复制 paper_id'" @click="copyId">
                  <AppIcon :name="copied ? 'check' : 'copy'" :size="12" />
                </button>
              </p>
            </div>

            <div class="pd-buttons">
              <button type="button" class="pd-btn primary" @click="openReader('bilingual')">
                <AppIcon name="panel" :size="16" />对照阅读
              </button>
              <button type="button" class="pd-btn" @click="openReader('original')">
                <AppIcon name="document" :size="16" />查看原版
              </button>
            </div>

            <div class="pd-downloads">
              <header><AppIcon name="download" :size="13" />文件下载</header>
              <div class="pd-download-grid">
                <button
                  v-for="a in artifacts"
                  :key="a.key"
                  type="button"
                  class="pd-download-btn"
                  :disabled="!!downloading"
                  :aria-busy="downloading === a.key"
                  :title="`下载${a.label}`"
                  @click="download(a)"
                >
                  <AppIcon :name="downloading === a.key ? 'refresh' : a.icon" :size="14" :class="{ spin: downloading === a.key }" />
                </button>
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
                <AppIcon :name="t.icon" :size="14" />{{ t.label }}
              </button>
            </div>

            <div class="pd-panel">
              <!-- 概览 -->
              <div v-show="tab === 'overview'" class="pd-overview">
                <!-- 摘要 + 标签（原来是"9 页文档"那条黑色横幅：用户口径——那行大数字没有信息量，
                     换成这篇论文**自己的内容**：摘要与标签） -->
                <div class="pd-card pd-abstract">
                  <div class="pd-card-head">
                    <div class="pd-card-title">
                      <span><AppIcon name="note" :size="20" /></span>
                      <h3>摘要</h3>
                    </div>
                    <div class="pd-abstract-actions" v-if="metaMissing">
                      <button
                        type="button"
                        class="pd-ghost-btn"
                        :disabled="!!metaBusy"
                        title="离线读 PDF 首页与页产物：会议、年份、原文摘要、DOI、参考文献、阅读时间（不花钱）"
                        @click="fillMeta(false)"
                      >
                        <AppIcon name="sparkle" :size="13" />{{ metaBusy === 'pdf' ? '识别中…' : '从论文识别' }}
                      </button>
                      <button
                        type="button"
                        class="pd-ghost-btn"
                        :disabled="!!metaBusy"
                        title="交给 agent 读全文填写：中英文关键词、中文摘要、领域、一句话概括、创新点、前置知识、难度"
                        @click="fillMeta(true)"
                      >
                        <AppIcon name="model" :size="13" />{{ metaBusy === 'agent' ? '已排队…' : '模型补全' }}
                      </button>
                    </div>
                  </div>

                  <p v-if="abstractText" class="pd-abstract-text">{{ abstractText }}</p>
                  <p v-else class="pd-abstract-empty">
                    还没有摘要。可以用右上角那两个按钮补全（先离线读 PDF，读不出来再问模型）。
                  </p>

                  <div class="pd-tags" v-if="tags.length">
                    <span class="pd-tag" v-for="t in tags" :key="t">{{ t }}</span>
                  </div>
                  <p v-else class="pd-abstract-empty">还没有标签。</p>
                </div>

                <!-- 论文信息：元数据扩展（一句话概括 / 领域·难度·类型·阅读时间 / 中文摘要 /
                     中英关键词 / 创新点 / 前置知识 / DOI·代码）。**没值的项不画**。 -->
                <div class="pd-card pd-meta">
                  <div class="pd-card-head">
                    <div class="pd-card-title">
                      <span><AppIcon name="info" :size="20" /></span>
                      <h3>论文信息</h3>
                    </div>
                    <span class="pd-hint" v-if="paper?.meta_filled_at">
                      最近补全 {{ fmtRelative(paper.meta_filled_at) }}
                    </span>
                  </div>

                  <p v-if="paper?.one_liner" class="pd-oneliner">{{ paper.one_liner }}</p>

                  <div class="pd-facts" v-if="metaFacts.length">
                    <span v-for="f in metaFacts" :key="f.k" class="pd-fact">
                      <i>{{ f.k }}</i>{{ f.v }}
                    </span>
                  </div>

                  <div class="pd-block" v-if="contributions.length">
                    <h4>核心创新点<em v-if="sourceOf('contributions')">· {{ sourceOf('contributions') }}</em></h4>
                    <ul class="pd-list">
                      <li v-for="(c, i) in contributions" :key="i">{{ c }}</li>
                    </ul>
                  </div>

                  <div class="pd-block" v-if="abstractZh">
                    <h4>中文摘要<em v-if="sourceOf('abstract_zh')">· {{ sourceOf('abstract_zh') }}</em></h4>
                    <p class="pd-abstract-text">{{ abstractZh }}</p>
                  </div>

                  <div class="pd-block" v-if="keywordsEn.length">
                    <h4>英文关键词</h4>
                    <div class="pd-tags">
                      <span class="pd-tag" v-for="k in keywordsEn" :key="k">{{ k }}</span>
                    </div>
                  </div>

                  <div class="pd-block" v-if="prereq.length">
                    <h4>前置知识</h4>
                    <div class="pd-tags">
                      <span class="pd-tag" v-for="(p, i) in prereq" :key="i">{{ p }}</span>
                    </div>
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

                  <p v-if="!paper?.one_liner && !metaFacts.length && !contributions.length
                           && !abstractZh && !keywordsEn.length && !prereq.length"
                     class="pd-abstract-empty">
                    还没有生成这些信息。点右上角「模型补全」让 agent 读一遍这篇论文
                    （过程在后台，跑完回来看）。
                  </p>
                </div>

                <div class="pd-stats">
                  <div class="pd-stat">
                    <span class="pd-stat-icon"><AppIcon name="server" :size="23" /></span>
                    <div><span>大小</span><strong>{{ sizeText }}</strong></div>
                  </div>
                  <div class="pd-stat">
                    <span class="pd-stat-icon"><AppIcon name="task" :size="23" /></span>
                    <div><span>入库</span><strong>{{ addedText }}</strong></div>
                  </div>
                  <div class="pd-stat">
                    <span class="pd-stat-icon"><AppIcon name="knowledge" :size="23" /></span>
                    <div><span>知识库</span><strong>{{ kbText }}</strong></div>
                  </div>
                  <div class="pd-stat">
                    <span class="pd-stat-icon"><AppIcon name="document" :size="23" /></span>
                    <div><span>篇幅</span><strong>{{ paper?.page_count ? `${paper.page_count} 页` : '—' }}</strong></div>
                  </div>
                </div>

                <!-- 两张并列的键值卡（各占一半，高度自然接近 → 不留空洞） -->
                <div class="pd-main-grid">
                  <div class="pd-card">
                    <div class="pd-card-head">
                      <div class="pd-card-title">
                        <span><AppIcon name="quote" :size="20" /></span>
                        <h3>作者与出处</h3>
                      </div>
                    </div>
                    <div class="pd-kv">
                      <span>作者</span><strong :title="authors">{{ authors }}</strong>
                    </div>
                    <div class="pd-kv">
                      <span>期刊 / 会议</span><strong :title="venueText">{{ venueText }}</strong>
                    </div>
                    <div class="pd-kv"><span>年份</span><strong>{{ yearText }}</strong></div>
                  </div>

                  <div class="pd-card">
                    <div class="pd-card-head">
                      <div class="pd-card-title">
                        <span><AppIcon name="task" :size="20" /></span>
                        <h3>阅读</h3>
                      </div>
                      <!-- 这两个数后端没有字段（只有"读到哪"），所以是本机统计，标出来别让人误会 -->
                      <span class="pd-hint">本机统计</span>
                    </div>
                    <div class="pd-kv"><span>阅读次数</span><strong>{{ readOpens ? `${readOpens} 次` : '—' }}</strong></div>
                    <div class="pd-kv"><span>阅读时长</span><strong>{{ readDuration }}</strong></div>
                    <div class="pd-kv"><span>最近打开</span><strong>{{ lastOpenText }}</strong></div>
                    <p v-if="!hasLocalStats" class="pd-hint">这台机器上还没有打开过这篇论文。</p>

                    <div class="pd-manage-row">
                      <button
                        type="button"
                        class="pd-ghost-btn danger"
                        :disabled="busy === 'delete'"
                        @click="confirmDelete = true"
                      >
                        <AppIcon name="trash" :size="14" />删除
                      </button>
                    </div>
                  </div>
                </div>

                <!-- 处理流程：**优先级最低**，所以排最后、做成最扁的一条（全宽 + 分段里程碑条） -->
                <div class="pd-card pd-pipeline">
                  <div class="pd-card-head">
                    <div class="pd-card-title">
                      <span><AppIcon name="sparkle" :size="20" /></span>
                      <h3>处理流程</h3>
                    </div>
                    <button type="button" class="pd-icon-link" title="查看进度" @click="tab = 'progress'">
                      <AppIcon name="forward" :size="15" />
                    </button>
                  </div>

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
                </div>

                <div class="pd-card pd-activity">
                  <div class="pd-card-head">
                    <div class="pd-card-title">
                      <span><AppIcon name="task" :size="20" /></span>
                      <h3>最近活动</h3>
                    </div>
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

              <!-- 参考文献：离线从页产物的 `ref` 行（或 Markdown 的 References 节）抽出来，
                   原文条目原样保留 —— 结构化字段是后补的，可能不全。 -->
              <div v-show="tab === 'refs'" class="pd-refs">
                <p class="pd-hint" v-if="references.length">
                  共 {{ references.length }} 条。条目原文按论文里的顺序排列。
                </p>
                <ol class="pd-list pd-ref-list" v-if="references.length">
                  <li v-for="r in references" :key="r.id">
                    <span class="pd-ref-no">{{ r.order_no }}</span>
                    <span class="pd-ref-text">{{ r.raw }}</span>
                  </li>
                </ol>
                <p v-else class="pd-abstract-empty">
                  还没有参考文献。到「概览」点右上角「从论文识别」离线抽一次
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
                  <div class="pd-card-head"><div class="pd-card-title"><h3>任务记录</h3></div></div>
                  <div class="pd-task-row" v-for="t in tasks.slice(0, 8)" :key="t.id">
                    <span class="badge" :class="taskStatus(String(t.status)).cls">
                      {{ TASK_TYPE_LABEL[String(t.type)] || t.type }}
                    </span>
                    <span class="pd-task-msg truncate" :title="t.message || ''">{{ t.message || '—' }}</span>
                    <span class="spacer" />
                    <span class="tiny faint nowrap">{{ taskStatus(String(t.status)).label }} · {{ fmtTime(t.updated_at || t.created_at) }}</span>
                  </div>
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
  margin: 0; min-width: 0; font-size: 16px; font-weight: 700; letter-spacing: -.01em;
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
  color: var(--text-dim); font-size: 13px;
}

/* ---------------- 两栏骨架 ---------------- */
.pd-grid {
  flex: 1; min-height: 0; display: grid;
  grid-template-columns: minmax(240px, 284px) minmax(0, 1fr);
}
.pd-left {
  min-height: 0; padding: 20px 18px 20px 22px; overflow-y: auto; overscroll-behavior: contain;
  display: flex; flex-direction: column; gap: 16px;
  border-right: 1px solid var(--divider);
  background: color-mix(in srgb, var(--bg) 42%, var(--bg-elev));
}
.pd-right {
  min-height: 0; padding: 16px 20px 20px;
  display: flex; flex-direction: column; gap: 12px;
}

/* ---------------- 左栏：封面 ---------------- */
.pd-cover {
  width: 100%; aspect-ratio: 3 / 4; overflow: hidden;
  border: 1px solid var(--border); border-radius: 16px;
  background: color-mix(in srgb, var(--text) 4%, transparent);
  box-shadow: var(--shadow-2);
}
.pd-cover img { display: block; width: 100%; height: 100%; object-fit: contain; object-position: top center; background: var(--bg-elev); }

.pd-identity { display: grid; gap: 5px; }
.pd-identity h3 {
  display: flex; align-items: flex-start; gap: 6px; margin: 0;
  font-size: 15px; font-weight: 650; line-height: 1.35; letter-spacing: -.015em;
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
  font-size: 11.5px; color: var(--text-dim);
}
.pd-identity p span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pd-copy {
  margin-left: auto; display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; flex: none; border-radius: 6px;
  color: var(--text-dim); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.pd-copy:hover { background: var(--bg-hover); color: var(--text); }

.pd-buttons { display: flex; flex-direction: column; gap: 10px; }
.pd-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  width: 100%; height: 34px; border-radius: var(--r-sm);
  border: 1px solid var(--border-strong); background: var(--bg-elev); color: var(--text);
  font-family: inherit; font-size: 13px; font-weight: 500; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.pd-btn:hover { background: var(--accent); border-color: transparent; color: var(--text-on-accent); }
.pd-btn.primary { background: var(--accent); border-color: transparent; color: var(--text-on-accent); }
.pd-btn.primary:hover { background: var(--accent-hover); }

/* ---------------- 左栏：文件下载 ---------------- */
.pd-downloads {
  display: grid; gap: 7px; padding: 9px 10px 10px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: color-mix(in srgb, var(--bg-elev) 76%, transparent);
}
.pd-downloads > header {
  display: flex; align-items: center; gap: 6px;
  font-size: 11px; font-weight: 600; color: var(--text-dim);
}
.pd-download-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 6px; }
.pd-download-btn {
  display: flex; align-items: center; justify-content: center;
  height: 34px; border-radius: 9px;
  border: 1px solid var(--border); background: var(--bg-elev); color: var(--text);
  cursor: pointer;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease),
              transform var(--dur) var(--ease);
}
.pd-download-btn:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border-strong));
  background: var(--bg-hover); transform: translateY(-1px);
}
.pd-download-btn:active:not(:disabled) { transform: scale(.96); }
.pd-download-btn:disabled { opacity: .5; cursor: default; }
.spin { animation: spin .85s linear infinite; }

/* ---------------- 右栏：页签 ---------------- */
.pd-tabs {
  display: inline-flex; gap: 4px; padding: 3px; flex: none;
  width: fit-content; max-width: calc(100% - 40px);
  border: 1px solid var(--border); border-radius: 12px;
  background: color-mix(in srgb, var(--text) 4%, transparent);
}
.pd-tab {
  display: inline-flex; align-items: center; gap: 5px;
  min-height: 30px; padding: 4px 13px; border-radius: 8px;
  color: var(--text-dim); font-family: inherit; font-size: 12px; font-weight: 600;
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

/* ---------------- 概览：摘要 + 标签 ----------------
 * 这一块原来是"9 页文档"那条黑色横幅（用户口径：大数字没信息量）——
 * 换成论文自己的内容。摘要是长文本，按 `--measure` 收行宽，别铺满整栏。 */
.pd-overview { display: grid; gap: 12px; }
.pd-abstract { gap: 10px; }
.pd-abstract-actions { display: flex; align-items: center; gap: 6px; flex: none; }
.pd-abstract-text {
  margin: 0; max-width: var(--measure);
  font-size: 13px; line-height: 1.72; color: var(--text);
  /* 摘要在有的论文里很长：收 6 行，想看全的点「编辑元数据」或进阅读器 */
  display: -webkit-box; -webkit-line-clamp: 6; -webkit-box-orient: vertical; overflow: hidden;
}
.pd-abstract-empty { margin: 0; font-size: 12px; color: var(--text-faint); }
.pd-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.pd-tag {
  display: inline-flex; align-items: center; height: 24px; padding: 0 11px;
  border-radius: 999px; background: var(--bg-elev); border: 1px solid var(--border);
  font-size: 11.5px; color: var(--text-dim);
}

/* ---------------- 概览：论文信息（元数据扩展，2026-10-07） ----------------
 * 与摘要卡同一套观感：一句话概括放大一档，其余走"小标签 + 小标题"。
 * 字段来源（pdf / model / agent / manual）写在 `h4` 里的 `<em>` 上，弱化到几乎只是备注。 */
.pd-meta { gap: 10px; }
.pd-oneliner {
  margin: 0; max-width: var(--measure);
  font-size: 13.5px; line-height: 1.6; font-weight: 560; color: var(--text);
}
.pd-facts { display: flex; flex-wrap: wrap; gap: 6px; }
.pd-fact {
  display: inline-flex; align-items: center; gap: 6px; height: 24px; padding: 0 10px;
  border-radius: 999px; background: color-mix(in srgb, var(--accent) 9%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--border));
  font-size: 11.5px; color: var(--text);
}
.pd-fact > i { font-style: normal; color: var(--text-dim); }
.pd-block { display: grid; gap: 5px; }
.pd-block > h4 {
  margin: 0; display: flex; align-items: center; gap: 6px;
  font-size: 11.5px; font-weight: 620; color: var(--text-dim);
}
.pd-block > h4 > em { font-style: normal; font-weight: 400; color: var(--text-faint); }
.pd-list { margin: 0; padding-left: 18px; display: grid; gap: 3px; }
.pd-list > li { font-size: 12.5px; line-height: 1.65; color: var(--text); }
.pd-links { display: flex; flex-wrap: wrap; gap: 6px; }
.pd-links > a { text-decoration: none; }

/* 参考文献：一条一段（原文可能很长，按 --measure 收行宽），序号在左。 */
.pd-ref-list { padding-left: 0; list-style: none; max-width: calc(var(--measure) + 40px); }
.pd-ref-list > li { display: flex; gap: 8px; align-items: flex-start; }
.pd-ref-no {
  flex: none; min-width: 22px; text-align: right; padding-top: 1px;
  font-family: var(--mono); font-size: 11px; color: var(--text-faint);
}
.pd-ref-text { font-size: 12.5px; line-height: 1.65; color: var(--text); overflow-wrap: anywhere; }

/* ---------------- 概览：信息卡（大小 / 入库 / 知识库 / 篇幅） ---------------- */
.pd-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.pd-stat {
  display: flex; align-items: center; gap: 12px; min-width: 0; min-height: 76px;
  padding: 11px 13px; border-radius: 19px;
  background: color-mix(in srgb, var(--bg) 72%, var(--bg-elev));
}
.pd-stat-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 44px; height: 44px; flex: none; border-radius: 14px;
  background: var(--bg-elev); color: var(--text); box-shadow: var(--shadow-1);
}
.pd-stat > div { display: grid; gap: 2px; min-width: 0; }
.pd-stat > div > span { font-size: 10.5px; color: var(--text-dim); }
.pd-stat > div > strong {
  font-size: 15px; font-weight: 620; letter-spacing: -.025em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* ---------------- 概览：主网格两张卡 ---------------- */
/* 主网格：两张**等宽、等高**的键值卡（作者与出处 / 阅读）。
   等高（默认 stretch）比"各自内容高度"更省地方：两卡行数相近，留白落在卡内，
   不会在两卡之间裂出一条空洞。处理流程不在这里 —— 它是下面那条独立的全宽扁条。 */
.pd-main-grid {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px; align-items: stretch;
}
.pd-card {
  display: grid; align-content: start; gap: 11px; padding: 13px; border-radius: 21px;
  background: color-mix(in srgb, var(--bg) 55%, var(--bg-elev));
}
.pd-card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.pd-card-title { display: inline-flex; align-items: center; gap: 8px; }
.pd-card-title > span {
  display: inline-flex; align-items: center; justify-content: center;
  width: 38px; height: 38px; border-radius: 13px;
  background: var(--bg-elev); color: var(--text);
}
.pd-card-title h3 { margin: 0; font-size: 14px; font-weight: 620; letter-spacing: -.025em; }
.pd-icon-link {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border-radius: 999px;
  background: var(--bg-elev); color: var(--text); cursor: pointer;
  transition: transform var(--dur) var(--ease);
}
.pd-icon-link:active { transform: scale(.94); }

/* ---------------- 处理流程：分段里程碑条（最扁的一条，全宽） ----------------
 * 三段等宽（解析 / 翻译 / 索引），每段自己按进度填充：完成=满格、进行中=真实比例（至少 20%）、
 * 未开始=空。段底下同一列宽写标签与状态 —— 一张卡只花两行高度，把纵向空间让给上面两张卡。 */
.pd-pipeline { gap: 9px; }
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
  font-size: 10.5px; color: var(--text-dim);
}
.pd-ms-labels > span > strong { font-size: 11.5px; font-weight: 620; color: var(--text); }
.pd-ms-labels > span > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pd-ms-labels > span.is-muted > span { color: var(--text-faint); }
.pd-ms-labels > span.is-failed > span { color: var(--danger); }

/* ---------------- 键值行（作者与出处 / 阅读） ---------------- */
.pd-kv { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.pd-kv > span { flex: none; width: 66px; font-size: 11px; color: var(--text-dim); }
.pd-kv > strong {
  flex: 1; min-width: 0; font-size: 12.5px; font-weight: 600; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pd-hint { font-size: 10.5px; color: var(--text-faint); }

.pd-manage-row { display: flex; align-items: center; gap: 8px; }
.pd-ghost-btn {
  display: inline-flex; align-items: center; gap: 6px;
  min-height: 32px; padding: 0 12px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text-dim);
  font-family: inherit; font-size: 12px; font-weight: 500; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.pd-ghost-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); border-color: var(--border-strong); }
.pd-ghost-btn.danger:hover:not(:disabled) { background: var(--danger-soft); color: var(--danger); border-color: color-mix(in srgb, var(--danger) 40%, transparent); }
.pd-ghost-btn:disabled { opacity: .55; cursor: default; }

/* ---------------- 概览：最近活动 ---------------- */
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
.pd-activity li strong { font-size: 12px; font-weight: 600; color: var(--text); }
.pd-activity li span, .pd-activity time { font-size: 11px; color: var(--text-dim); }
.pd-activity time { white-space: nowrap; }
.pd-act-empty { grid-column: 1 / -1; font-size: 11.5px; color: var(--text-dim); }

/* ---------------- 进度页签 ---------------- */
.pd-progress { display: grid; gap: 12px; }
.pd-stages { display: flex; align-items: center; gap: 6px; margin: 0; padding: 0; list-style: none; }
.pd-stages li {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 12px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--bg-elev); color: var(--text-dim);
  font-size: 12px;
}
.pd-stages li.done { color: var(--text); border-color: color-mix(in srgb, var(--ok) 34%, var(--border)); }
.pd-stages li.active { background: var(--accent-soft); color: var(--accent-text); border-color: transparent; }
.pd-stage-dot { display: inline-flex; }

.pd-live {
  display: flex; align-items: center; gap: 12px; padding: 12px 14px;
  border-radius: var(--r-sm); background: var(--accent-soft); color: var(--accent-text);
}
.pd-live .spinner { width: 20px; height: 20px; }
.pd-live > div { display: grid; gap: 1px; min-width: 0; }
.pd-live strong { font-size: 12.5px; font-weight: 650; }
.pd-live span { font-size: 11.5px; opacity: .9; }

.pd-progress-row { display: grid; gap: 5px; }
.pd-progress-top {
  display: flex; align-items: center; font-size: 12px; color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}
.pd-progress-top > span:last-child { margin-left: auto; }

.pd-task-row { display: flex; align-items: center; gap: 8px; min-height: 28px; }
.pd-task-msg { font-size: 12px; color: var(--text-dim); min-width: 0; }

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
.pd-file-copy strong { font-size: 13px; font-weight: 600; }
.pd-file-copy span { color: var(--text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ---------------- 窄屏：单栏（封面缩到一条，右侧占满） ---------------- */
@media (max-width: 760px) {
  .pd-dialog { width: calc(100dvw - 20px); height: calc(100dvh - 20px); }
  .pd-grid { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }
  .pd-left {
    flex-direction: row; align-items: flex-start; gap: 12px;
    padding: 12px; overflow: hidden;
    border-right: 0; border-bottom: 1px solid var(--divider);
  }
  .pd-cover { width: 62px; flex: none; }
  .pd-buttons { flex-direction: row; }
  .pd-downloads { display: none; }
  .pd-right { padding: 10px 12px 12px; }
  .pd-main-grid, .pd-stats { grid-template-columns: minmax(0, 1fr); }
}

@media (prefers-reduced-motion: reduce) {
  .spin { animation: none; }
}
</style>
