<script setup lang="ts">
/**
 * 知识库：**跨论文知识检索与浏览工作台**。
 *
 * 定位（用户口径）：既不是第二个阅读器，也不是设置页 ——
 * 完整阅读 / 双语对照 / 公式细看 / 定位都回 `/reader/:paperId`，索引管理在 `/settings#kb`。
 * 所以这一页只有三件东西：**搜索框、结果、浏览**，没有任何统计卡与管理表。
 *
 * 两个模式：
 * - **搜索**：一段技术描述或关键词 → `/rag/similar`（向量 + 词法 → RRF → 重排）。
 *   范围 `全部 / 本论文`；"全部"下可勾「排除所选论文」= 跨文献比对。命中插图/表格/公式时出缩略图。
 * - **浏览**：按论文 + 类型 + 语言翻 `/knowledge/chunks`；选「插图」时是**图库网格**。
 *   每条可「展开全文」（`/knowledge/chunk`，**只这一条**，不做中英对照）。
 *
 * 搜索记录是浏览器式的（`utils/searchHistory`，本机 localStorage，管理入口在设置页 `#history`）。
 */

import { computed, nextTick, onMounted, onUnmounted, ref, toRefs } from 'vue';
import { useRouter } from 'vue-router';
import { api } from '@/api/client';
import type {
  KnowledgeChunkDetail, KnowledgeChunkRow, KnowledgeOverview, SimilarItem, SimilarResult,
} from '@/api/types';
import { reportError, state, toast, withLoading } from '@/stores/app';
import { kbState } from '@/stores/knowledge';
import { openTab } from '@/stores/tabs';
import { readerTarget } from '@/utils/navigation';
import {
  addSearch, clearHistory, loadHistory, removeSearch, subscribeHistory, type SearchEntry,
} from '@/utils/searchHistory';
import AppIcon from '@/components/AppIcon.vue';

/** 四类 chunk（与后端 knowledge_service.CHUNK_TYPES 一致）+ 展示用名词与图标。 */
const CHUNK_TYPES = [
  { key: 'text', label: '正文', icon: 'document' },
  { key: 'formula', label: '公式', icon: 'formula' },
  { key: 'figure', label: '插图', icon: 'image' },
  { key: 'table', label: '表格', icon: 'table' },
];
const LANG_OPTIONS = [
  { key: 'zh', label: '中文' },
  { key: 'en', label: '原文' },
];
/** 返回条数（后端融合 + 重排后取前 N）。 */
const TOP_K = 6;
/** 浏览一次拉多少条（后端上限 500；200 条够翻，也不至于一屏塞死）。 */
const BROWSE_LIMIT = 200;

type KbPaper = KnowledgeOverview['items'][number];

const router = useRouter();

/* ------------------------------ 页面状态 ------------------------------ */

/**
 * 输入、结果、浏览筛选、展开的全文都放在 `stores/knowledge`（模块级 reactive）里：
 * 外壳按"标签键"重建视图，切走再切回来组件是新的 —— 状态放组件里就会丢（用户报过）。
 * 用 `toRefs` 取出来，模板照旧写 `mode` / `result`，脚本里照旧 `.value`。
 */
const {
  mode, query, scope, paperId, excludeSelf, result, error,
  bPaperId, bType, bLang, chunks, detailId, detail,
} = toRefs(kbState);

/** 纯瞬时的界面状态留在组件里：忙碌标记、历史下拉开合/高亮。 */
const busy = ref(false);
const overview = ref<KnowledgeOverview | null>(null);

const papers = computed<KbPaper[]>(() => overview.value?.items || []);

/** 页头右侧的轻量状态（**不是统计卡**）：库里有什么，直接决定你怎么读检索结果。 */
const statusText = computed(() => {
  const o = overview.value;
  if (!o) return state.loading['kb-overview'] ? '正在读取…' : '—';
  const vector = (o.vector_store || {}) as Record<string, any>;
  return `库内 ${o.papers_in_kb} 篇 · ${o.chunks} chunk · 向量库${vector.available ? '可用' : '不可用'}`;
});
const statusTitle = computed(() => {
  const o = overview.value;
  if (!o) return '';
  const byType = o.chunks_by_type || {};
  const vector = (o.vector_store || {}) as Record<string, any>;
  return `论文 ${o.papers_in_kb}/${o.papers_total} 篇已纳入 · 正文 ${byType.text || 0} / 公式 ${byType.formula || 0}`
    + ` / 插图 ${byType.figure || 0} / 表格 ${byType.table || 0}`
    + (vector.available ? '' : ` · 向量库不可用：${vector.error || '未返回原因'}`);
});
/** 库里一条 chunk 都没有：搜索与浏览都给"去建索引"的明确去处。 */
const libraryEmpty = computed(() => !!overview.value && !overview.value.chunks);

function setMode(next: 'search' | 'browse') {
  mode.value = next;
  if (next === 'browse' && bPaperId.value && !chunks.value.length) void loadChunks();
}

/* ------------------------------ 搜索 ------------------------------ */

/**
 * 界面范围 → 后端 scope：
 * `本论文` → paper；`全部` + 勾了排除 → cross（跨文献比对，要带上被排除的那篇）；
 * 其余 → all。勾选框只在"全部"下出现（"本论文 + 排除本篇"必然空）。
 */
const effectiveScope = computed<'all' | 'cross' | 'paper'>(() => {
  if (scope.value === 'paper') return 'paper';
  return (excludeSelf.value && paperId.value) ? 'cross' : 'all';
});

async function runSearch(): Promise<void> {
  const text = query.value.trim();
  if (!text) { toast('请先输入要检索的内容', 'warn'); return; }
  if (effectiveScope.value !== 'all' && !paperId.value) {
    toast(scope.value === 'paper' ? '请先选择论文' : '勾了「排除所选论文」，请先选择要排除的论文', 'warn');
    return;
  }
  busy.value = true;
  error.value = '';
  result.value = null;
  const scopeUsed = effectiveScope.value;
  try {
    const payload: Record<string, unknown> = { query: text, scope: scopeUsed, top_k: TOP_K };
    if (scopeUsed !== 'all') payload.paper_id = paperId.value;
    const data = (await api.post<SimilarResult>('/rag/similar', payload)) || {};
    result.value = data;
    // 成功搜索才写记录（失败/被拒不入历史，免得记录里全是没结果的词）
    addSearch({
      q: text, mode: 'search', scope: scopeUsed,
      paper_id: scopeUsed === 'all' ? null : paperId.value,
      hits: (data.items || []).length,
    });
    refreshHistory();
    histOpen.value = false;
    histIndex.value = -1;
  } catch (err) {
    error.value = (err as any)?.message ?? String(err);
    reportError(err, '检索失败');
  } finally {
    busy.value = false;
  }
}

/** 检索漏斗：`向量 20 + 词法 20 → 12 → 6 · qwen3-vl-rerank`（完整版进 tooltip）。 */
const simMeta = computed(() => {
  const r = result.value?.retrieval;
  if (!r) return null;
  const model = r.rerank?.model || '—';
  return {
    short: `${r.vector_hits ?? 0}+${r.lexical_hits ?? 0} → ${r.fused ?? 0} → ${r.returned ?? 0} · ${model}`,
    full: `向量命中 ${r.vector_hits ?? 0} · 词法命中 ${r.lexical_hits ?? 0}`
      + ` · RRF 融合 ${r.fused ?? 0} · 返回 ${r.returned ?? 0} · 重排 ${model}`
      + (r.rerank?.degraded ? '（已降级）' : ''),
  };
});

function scoreOf(item: SimilarItem, key: string): string {
  const value = item?.scores?.[key];
  return value === null || value === undefined ? '—' : Number(value).toFixed(3);
}

/** 一行里只给 `rrf / rerank`，召回名次这类细节放 tooltip（不占版面）。 */
function scoreTitle(item: SimilarItem): string {
  const parts = [`RRF ${scoreOf(item, 'rrf')}`, `重排 ${scoreOf(item, 'rerank')}`];
  if (item?.ranks?.vector_rank) parts.push(`向量 #${item.ranks.vector_rank}`);
  if (item?.ranks?.lexical_rank) parts.push(`词法 #${item.ranks.lexical_rank}`);
  return parts.join(' · ');
}

/* ---------------------------- 搜索记录（浏览器式） ---------------------------- */

const history = ref<SearchEntry[]>([]);
const histOpen = ref(false);
const histIndex = ref(-1);
let unsubscribeHistory: (() => void) | null = null;

function refreshHistory() {
  history.value = loadHistory();
}

/** 下拉里显示最近 10 条；已经打了字就按子串过滤（浏览器的做法）。 */
const shownHistory = computed<SearchEntry[]>(() => {
  const needle = query.value.trim().toLowerCase();
  const list = needle
    ? history.value.filter((e) => e.q.toLowerCase().includes(needle))
    : history.value;
  return list.slice(0, 10);
});

/** 记录的"范围"文案（本论文 / 跨文献排除某篇）。 */
function entryScope(entry: SearchEntry): string {
  if (entry.scope === 'paper') return '本论文';
  if (entry.scope === 'cross') return '排除本篇';
  return '全部';
}

function onSearchKey(ev: KeyboardEvent): void {
  const list = shownHistory.value;
  if (histOpen.value && list.length && (ev.key === 'ArrowDown' || ev.key === 'ArrowUp')) {
    ev.preventDefault();
    histIndex.value = ev.key === 'ArrowDown'
      ? (histIndex.value + 1) % list.length
      : (histIndex.value <= 0 ? list.length - 1 : histIndex.value - 1);
    return;
  }
  if (ev.key === 'Escape') { histOpen.value = false; histIndex.value = -1; return; }
  if (ev.key === 'Enter') {                       // Ctrl/⌘+Enter 与 Enter 都是提交
    ev.preventDefault();
    const picked = histOpen.value ? list[histIndex.value] : undefined;
    if (picked) { void applyHistory(picked); return; }
    void runSearch();
  }
}

/** 点/选中一条历史：把当时的范围一起还原，然后立刻重跑。 */
async function applyHistory(entry: SearchEntry): Promise<void> {
  query.value = entry.q;
  if (entry.scope === 'paper') { scope.value = 'paper'; excludeSelf.value = false; }
  else if (entry.scope === 'cross') { scope.value = 'all'; excludeSelf.value = true; }
  else { scope.value = 'all'; excludeSelf.value = false; }
  if (entry.paper_id) paperId.value = entry.paper_id;
  histOpen.value = false;
  histIndex.value = -1;
  await runSearch();
}

function dropHistory(entry: SearchEntry): void {
  removeSearch(entry);
  refreshHistory();
  toast('已删除这条搜索记录', 'ok', 1800);
}

function clearAllHistory(): void {
  if (!history.value.length) return;
  if (!confirm(`清空全部 ${history.value.length} 条搜索记录？\n\n只影响本机浏览器里的这份记录，`
    + '论文、索引、笔记都不受影响。')) return;
  clearHistory();
  refreshHistory();
  histIndex.value = -1;
  toast('已清空搜索记录', 'ok', 2200);
}

/* ------------------------------ 浏览 ------------------------------ */

/** 「插图」= 图库网格；其余类型用紧凑列表（列表里也带小缩略图）。 */
const isGallery = computed(() => bType.value === 'figure');

async function loadChunks(): Promise<void> {
  if (!bPaperId.value) { chunks.value = []; return; }
  closeDetail();
  await withLoading('kb-chunks', async () => {
    const data = await api.get<{ items: KnowledgeChunkRow[] }>('/knowledge/chunks', {
      paper_id: bPaperId.value,
      chunk_type: bType.value || undefined,
      lang: bLang.value || undefined,
      limit: BROWSE_LIMIT,
    });
    chunks.value = data.items || [];
  });
}

/* ------------------------------ 展开全文 ------------------------------ */

const detailBusy = ref(false);

/** 点同一条再点一次 = 收起（按钮文案跟着变）。 */
async function toggleDetail(chunkId: string): Promise<void> {
  if (detailId.value === chunkId) { closeDetail(); return; }
  detailId.value = chunkId;
  detail.value = null;
  detailBusy.value = true;
  try {
    detail.value = await api.get<KnowledgeChunkDetail>('/knowledge/chunk', { chunk_id: chunkId });
    await nextTick();
    document.getElementById('kb-detail')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  } catch (err) {
    detailId.value = '';
    reportError(err, '展开全文失败');
  } finally {
    detailBusy.value = false;
  }
}

function closeDetail(): void {
  detail.value = null;
  detailId.value = '';
}

/* ------------------------------ 小工具 ------------------------------ */

function typeMeta(key?: string | null) {
  return CHUNK_TYPES.find((t) => t.key === key) || { key: key || '', label: key || '—', icon: 'document' };
}

function langShort(lang?: string | null): string {
  const key = lang || 'en';
  // `any` = 语言无关（公式原子行）：两种语言检索都能召回，徽标显示「通用」
  if (key === 'any') return '通用';
  return key === 'zh' ? '中' : '原';
}

/** 双语句：`中` / `原` / `通用` / `中+原`（两路都召回同一条时标注）。 */
function langLabel(item: SimilarItem): string {
  const langs = item?.langs?.length ? item.langs : [item?.lang || 'en'];
  if (langs.includes('any')) return '通用';
  return langs.map((l) => (l === 'zh' ? '中' : '原')).join('+');
}

/**
 * 跳回原文：**唯一入口**，统一回 Reader 定位到对应 block。
 *
 * ⚠️ 必须在点击回调里调（`openTab` 会改响应式状态）：`:to="fn()"` 那种写法会让
 * "渲染 → 改状态 → 再渲染"死循环，本页搜索曾经必定卡死。
 */
function openSource(item: { paper_id?: string | null; block_id?: string | null; page?: number | null }) {
  void router.push(openTab(readerTarget(item.paper_id ?? '', item.block_id, item.page)) as never);
}

/** 去设置页的「索引管理」建索引（深链到那一段）。 */
function goIndexSettings() {
  void router.push(openTab({ name: 'settings', hash: '#kb' }) as never);
}

/* ------------------------------ 载入 ------------------------------ */

async function refreshOverview(): Promise<void> {
  await withLoading('kb-overview', async () => {
    const data = await api.get<KnowledgeOverview>('/knowledge/overview');
    overview.value = data;
    // 默认挑一篇"已纳入知识库且有 chunk"的论文（浏览模式与"本论文/排除本篇"共用）
    const preferred = (data.items || []).find((p) => p.in_knowledge_base && p.chunks > 0)
      || (data.items || []).find((p) => p.chunks > 0);
    if (preferred) {
      if (!paperId.value) paperId.value = preferred.id;
      if (!bPaperId.value) bPaperId.value = preferred.id;
    }
  });
}

onMounted(async () => {
  refreshHistory();
  unsubscribeHistory = subscribeHistory(refreshHistory);
  await refreshOverview();
  // 上次是浏览模式、筛选还在、但条目已经被清掉（例如中途重建过索引）→ 补一次；
  // 有数据就直接渲染 store 里的（切走再切回来不重复请求）
  if (mode.value === 'browse' && bPaperId.value && !chunks.value.length) await loadChunks();
});

onUnmounted(() => {
  unsubscribeHistory?.();
  unsubscribeHistory = null;
});
</script>

<template>
  <div class="kb">
    <!-- 右上角一行库规模小字（不是统计卡）：全库检索时"库里有什么"直接影响怎么读结果 -->
    <div class="kb-status" v-if="!libraryEmpty">
      <span class="tiny faint nowrap" :title="statusTitle">{{ statusText }}</span>
    </div>

    <!-- 搜索主页式的头部：标题 + 模式 + 大搜索框 + 控件一行，全部居中 -->
    <section class="hero" :class="{ compact: mode === 'browse' }">
      <h1 class="hero-title">知识库</h1>
      <div class="seg" role="tablist" aria-label="知识库模式">
        <button :class="{ active: mode === 'search' }" @click="setMode('search')">搜索</button>
        <button :class="{ active: mode === 'browse' }" @click="setMode('browse')">浏览</button>
      </div>

      <template v-if="mode === 'search'">
        <div class="search-wrap">
          <div class="search-field hero-search">
            <AppIcon name="search" :size="17" />
            <input
              v-model="query"
              type="text"
              placeholder="描述一段技术，或输入关键词…"
              aria-label="检索知识库"
              @focus="histOpen = true"
              @click="histOpen = true"
              @blur="histOpen = false"
              @keydown="onSearchKey"
            >
            <button v-if="query" class="btn icon sm ghost" title="清空" @click="query = ''">
              <AppIcon name="close" :size="13" />
            </button>
          </div>

          <!-- 最近搜索：聚焦/点击即出，↑↓ 选、Enter 重跑、单条可删 -->
          <div class="hist" v-if="histOpen && shownHistory.length">
            <button
              v-for="(e, i) in shownHistory"
              :key="e.ts + e.q"
              type="button"
              class="hist-item"
              :class="{ active: i === histIndex }"
              @mousedown.prevent="applyHistory(e)"
            >
              <AppIcon name="search" :size="13" />
              <span class="truncate">{{ e.q }}</span>
              <span class="tiny faint nowrap">{{ entryScope(e) }}</span>
              <span class="tiny faint nowrap" v-if="e.hits !== null">{{ e.hits }} 条</span>
              <span
                class="hist-del"
                title="删除这条记录"
                @mousedown.prevent.stop="dropHistory(e)"
              ><AppIcon name="close" :size="12" /></span>
            </button>
            <div class="hist-foot">
              <span class="spacer"></span>
              <button type="button" class="btn sm ghost" @mousedown.prevent="clearAllHistory">清空记录</button>
            </div>
          </div>
        </div>

        <div class="hero-actions">
          <div class="seg">
            <button :class="{ active: scope === 'all' }" @click="scope = 'all'">全部</button>
            <button :class="{ active: scope === 'paper' }" @click="scope = 'paper'">本论文</button>
          </div>
          <label class="row tiny" v-if="scope === 'all'"
                 title="跨文献比对：库里别的论文里谁在做类似的事">
            <input type="checkbox" v-model="excludeSelf">排除所选论文
          </label>
          <select
            v-if="scope === 'paper' || excludeSelf"
            v-model="paperId"
            :title="scope === 'paper' ? '在选中的这篇论文里检索' : '检索时排除这篇论文'"
            style="min-width:220px"
          >
            <option value="">选择论文…</option>
            <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
          </select>
          <button class="btn primary" :disabled="busy" title="Ctrl/⌘ + Enter" @click="runSearch">
            <AppIcon name="search" :size="15" />{{ busy ? '检索中…' : '检索' }}
          </button>
        </div>
      </template>

      <div class="hero-actions" v-else>
        <select v-model="bPaperId" @change="loadChunks" style="min-width:220px">
          <option value="">选择论文…</option>
          <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
        </select>
        <select v-model="bType" @change="loadChunks">
          <option value="">全部类型</option>
          <option v-for="t in CHUNK_TYPES" :key="t.key" :value="t.key">{{ t.label }}</option>
        </select>
        <select v-model="bLang" @change="loadChunks">
          <option value="">全部语言</option>
          <option v-for="l in LANG_OPTIONS" :key="l.key" :value="l.key">{{ l.label }}</option>
        </select>
      </div>
    </section>

    <div class="kb-body">
      <div class="empty-state" v-if="libraryEmpty">
        <div class="es-title">知识库还是空的</div>
        <div class="es-actions">
          <button class="btn primary sm" @click="goIndexSettings">
            <AppIcon name="settings" :size="14" />去「设置 → 索引管理」建索引
          </button>
        </div>
      </div>

      <!-- ------------------------------ 搜索 ------------------------------ -->
      <template v-else-if="mode === 'search'">
        <template v-if="result">
          <div class="result-head" :title="simMeta ? simMeta.full : ''">
            {{ (result.items || []).length }} 条<template v-if="simMeta"> · {{ simMeta.short }}</template>
          </div>

          <div v-for="note in (result.retrieval?.notes || [])" :key="note" class="note">
            <span class="badge warn">提示</span><span class="small">{{ note }}</span>
          </div>

          <div v-for="(it, i) in (result.items || [])" :key="it.chunk_id" class="hit">
            <div class="hit-row">
              <img v-if="it.image_url" class="thumb" :src="it.image_url" loading="lazy"
                   :alt="`${typeMeta(it.chunk_type).label}裁图`">
              <div class="hit-main">
                <div class="hit-head">
                  <span class="hit-idx">{{ i + 1 }}</span>
                  <span class="hit-title" :title="it.paper_title || it.paper_id">
                    {{ it.paper_title || it.paper_id }}
                  </span>
                  <span class="badge">p{{ it.page }}</span>
                  <span class="badge accent">{{ typeMeta(it.chunk_type).label }}·{{ langLabel(it) }}</span>
                  <span class="hit-sec" v-if="it.section" :title="it.section">{{ it.section }}</span>
                  <span class="spacer"></span>
                  <span class="hit-score" :title="scoreTitle(it)">
                    {{ scoreOf(it, 'rrf') }} / {{ scoreOf(it, 'rerank') }}
                  </span>
                  <button class="btn sm ghost" title="回阅读器定位到这一段" @click="openSource(it)">
                    <AppIcon name="locate" :size="13" />原文
                  </button>
                </div>
                <div class="hit-text">{{ it.caption || it.snippet }}</div>
              </div>
            </div>
          </div>

          <div class="empty" v-if="!(result.items || []).length">没有找到内容。</div>
        </template>

        <div class="small error-line" v-if="error">检索失败：{{ error }}</div>
      </template>

      <!-- ------------------------------ 浏览 ------------------------------ -->
      <template v-else>
        <!-- 展开全文：只当前这一条（中英对照/完整阅读回 Reader） -->
        <section class="card detail" id="kb-detail" v-if="detailId">
          <div class="card-head">
            <span class="badge accent" v-if="detail">
              {{ typeMeta(detail.chunk_type).label }}·{{ langShort(detail.lang) }}
            </span>
            <span class="tiny faint truncate" v-if="detail">
              {{ detail.paper_title || detail.paper_id }} · p{{ detail.page }}
              <template v-if="detail.section"> · {{ detail.section }}</template>
              <template v-if="detail.tokens"> · {{ detail.tokens }} tok</template>
            </span>
            <span class="spacer"></span>
            <button class="btn sm" v-if="detail"
                    @click="openSource({ paper_id: detail.paper_id, block_id: detail.block_id, page: detail.page })">
              <AppIcon name="locate" :size="14" />原文
            </button>
            <button class="btn sm ghost" @click="closeDetail">
              <AppIcon name="close" :size="14" />收起
            </button>
          </div>
          <div class="card-body">
            <div class="empty" v-if="!detail">正在读取…</div>
            <template v-else>
              <img v-if="detail.image_url" class="detail-img" :src="detail.image_url"
                   :alt="`${typeMeta(detail.chunk_type).label}裁图`">
              <div class="full">{{ detail.content }}</div>
            </template>
          </div>
        </section>

        <!-- 图库网格：选「插图」时按图翻 -->
        <div class="gallery" v-if="chunks.length && isGallery">
          <figure class="fig-card" v-for="c in chunks" :key="c.id" :class="{ active: detailId === c.id }">
            <button type="button" class="fig-thumb" :title="c.caption || c.preview"
                    @click="toggleDetail(c.id)">
              <img v-if="c.image_url" :src="c.image_url" loading="lazy"
                   :alt="`插图 p${c.page}`">
              <span v-else class="tiny faint">无裁图</span>
            </button>
            <figcaption>
              <div class="tiny faint">
                p{{ c.page }}<template v-if="c.section"> · {{ c.section }}</template> · {{ langShort(c.lang) }}
              </div>
              <div class="fig-cap">{{ c.caption || c.preview }}</div>
              <div class="row nowrap fig-ops">
                <button class="btn sm" @click="toggleDetail(c.id)">
                  <AppIcon name="document" :size="13" />{{ detailId === c.id ? '收起' : '展开全文' }}
                </button>
                <button class="btn sm ghost" title="回阅读器定位到这一段" @click="openSource(c)">
                  <AppIcon name="locate" :size="13" />原文
                </button>
              </div>
            </figcaption>
          </figure>
        </div>

        <!-- 紧凑列表：正文 / 公式 / 表格 -->
        <section class="chunks" v-else-if="chunks.length">
          <div v-for="c in chunks" :key="c.id" class="chunk-row" :class="{ active: detailId === c.id }">
            <img v-if="c.image_url" class="thumb sm" :src="c.image_url" loading="lazy"
                 :alt="`${typeMeta(c.chunk_type).label}裁图`">
            <div class="hit-main">
              <div class="hit-head">
                <span class="badge accent">{{ typeMeta(c.chunk_type).label }}·{{ langShort(c.lang) }}</span>
                <span class="badge">p{{ c.page }}</span>
                <span class="hit-sec" v-if="c.section" :title="c.section">{{ c.section }}</span>
                <span class="spacer"></span>
                <button class="btn sm ghost" @click="toggleDetail(c.id)">
                  <AppIcon name="document" :size="13" />{{ detailId === c.id ? '收起' : '展开全文' }}
                </button>
                <button class="btn sm ghost" title="回阅读器定位到这一段" @click="openSource(c)">
                  <AppIcon name="locate" :size="13" />原文
                </button>
              </div>
              <div class="hit-text">{{ c.caption || c.preview }}</div>
            </div>
          </div>
        </section>

        <div class="empty" v-else-if="state.loading['kb-chunks']">正在读取…</div>
        <div class="empty" v-else-if="!bPaperId">选一篇论文</div>
        <div class="empty" v-else>没有符合条件的 chunk</div>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* ------------------------------ 骨架：居中一列（搜索主页式） ------------------------------ */
.kb { max-width: 880px; margin: 0 auto; }
.kb-status { display: flex; justify-content: flex-end; }

.hero {
  display: flex; flex-direction: column; align-items: center; gap: 12px;
  padding: 40px 0 4px;
}
/* 浏览模式不需要大段留白（没有搜索框可聚焦） */
.hero.compact { padding: 6px 0 4px; }
.hero-title { margin: 0; font-size: 22px; color: var(--accent-text); }
.hero-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: center; }
.kb-body { margin-top: 26px; }

/* 大号搜索框：`.search-field` 自带 32px 高与圆角，这里只把它放大到"主页"尺寸 */
.search-wrap { position: relative; width: min(620px, 100%); }
.search-field.hero-search { display: flex; width: 100%; height: 46px; padding: 0 16px; }
.search-field.hero-search .icon { color: var(--text-dim); }
.search-field.hero-search input { flex: 1; width: auto; min-width: 0; font-size: 14.5px; }

/* 最近搜索：浮在搜索框下方（不推挤布局），键盘与鼠标都能用 */
.hist {
  position: absolute; top: calc(100% + 6px); left: 0; right: 0; z-index: 30;
  padding: 5px; display: flex; flex-direction: column; gap: 1px;
  background: var(--bg-elev); border: 1px solid var(--border-strong);
  border-radius: var(--radius); box-shadow: var(--shadow-flyout);
}
.hist-item {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 7px 10px; border: 0; border-radius: var(--radius-ctl);
  background: transparent; color: var(--text); cursor: pointer;
  font-family: inherit; font-size: 12.5px; text-align: left;
}
.hist-item .icon { color: var(--text-faint); }
.hist-item:hover, .hist-item.active { background: var(--bg-hover); }
.hist-del {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: var(--r-xs); color: var(--text-faint); flex: none;
}
.hist-del:hover { background: var(--bg-pressed); color: var(--danger); }
.hist-foot {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 6px 2px; border-top: 1px solid var(--divider); margin-top: 3px;
}

/* ------------------------------ 结果：扁平一列 ------------------------------ */
.result-head {
  font-size: 11.5px; color: var(--text-faint); font-variant-numeric: tabular-nums;
  margin-bottom: 2px;
}
.error-line { margin-top: 10px; color: var(--danger); }

.hit { padding: 10px 0; border-top: 1px solid var(--divider); }
.hit:first-of-type { border-top: 0; }
.hit-row { display: flex; align-items: flex-start; gap: 10px; }
.hit-main { min-width: 0; flex: 1; }
.hit-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.hit-idx { min-width: 14px; text-align: right; color: var(--text-faint); font-size: 11px; }
/* 标题用强调色：像搜索结果里的链接一样，一眼能点 */
.hit-title { font-weight: 600; font-size: 13.5px; color: var(--accent-text);
  max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hit-sec { font-size: 11px; color: var(--text-faint); max-width: 200px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.hit-score { font-family: var(--mono); font-size: 11px; color: var(--text-faint);
  font-variant-numeric: tabular-nums; }
.hit-text { margin-top: 2px; font-size: 12px; color: var(--text-dim); line-height: 1.55;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

/* 插图 / 表格 / 公式的缩略图（拿不到裁图就不显示，不留空框） */
.thumb {
  flex: none; width: 112px; height: 84px; object-fit: contain;
  background: var(--bg-sunken); border: 1px solid var(--border); border-radius: var(--r-sm);
}
.thumb.sm { width: 88px; height: 66px; }

/* 提示一条 */
.note { display: flex; align-items: flex-start; gap: 6px; padding: 5px 0; }

/* ------------------------------ 图库网格 ------------------------------ */
.gallery {
  display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
}
.fig-card { margin: 0; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.fig-card.active .fig-thumb { border-color: var(--accent); box-shadow: var(--ring); }
.fig-thumb {
  display: flex; align-items: center; justify-content: center;
  width: 100%; aspect-ratio: 4 / 3; padding: 4px;
  background: var(--bg-sunken); border: 1px solid var(--border);
  border-radius: var(--radius); cursor: pointer; overflow: hidden;
}
.fig-thumb img { max-width: 100%; max-height: 100%; object-fit: contain; }
.fig-thumb:hover { border-color: color-mix(in srgb, var(--accent) 34%, var(--border)); }
.fig-cap {
  font-size: 11.5px; color: var(--text-dim); line-height: 1.45;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.fig-ops { gap: 6px; margin-top: 2px; }

/* ------------------------------ 紧凑列表 ------------------------------ */
.chunk-row {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 10px 0; border-top: 1px solid var(--divider);
}
.chunk-row:first-child { border-top: 0; }

/* ------------------------------ 展开全文 ------------------------------ */
.detail { scroll-margin-top: calc(var(--header-h) + 12px); }
.detail-img {
  display: block; max-width: 100%; max-height: 360px; object-fit: contain;
  margin: 0 auto 12px; background: var(--bg-sunken);
  border: 1px solid var(--border); border-radius: var(--radius);
}
/* 全文用等宽 + 保留换行：chunk 里可能是 Markdown / LaTeX，不做渲染也不吞空白 */
.full {
  white-space: pre-wrap; word-break: break-word;
  font-size: 12.5px; line-height: 1.65; color: var(--text);
  background: var(--bg-sunken); border-radius: var(--r-sm); padding: 10px 12px;
  max-height: 46vh; overflow: auto;
}
</style>
