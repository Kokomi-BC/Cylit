<script setup lang="ts">
/**
 * 知识库：概览 / 纳入移出 / 触发 EMBED+INDEX / 索引状态 / chunk 元数据 / 相似检索 / RAG 问答。
 *
 * 界面口径（用户口径：**能少说就少说**）：卡片标题只留名词，说明文字（"向量+词法+重排"
 * 这类自夸徽章、重复的状态徽章、重复的刷新按钮）一律去掉；低频/重复的操作按钮收进
 * 「⋯」或直接取消（下拉即查）；信息密度靠**一行内的徽章**而不是三段小字。
 */

import { computed, onMounted, ref } from 'vue';
import { RouterLink, useRouter } from 'vue-router';
import { api } from '@/api/client';
import type {
  ChunkMeta, KnowledgeOverview, RagAnswer, RagSource,
} from '@/api/types';
import {
  loadPapers, paperTitle, reportError, state, toast, withLoading,
} from '@/stores/app';
import { openTab } from '@/stores/tabs';
import {
  copyText, paperStatus, pct, truncate,
} from '@/utils/format';
import { readerTarget } from '@/utils/navigation';
import { mdToHtml } from '@/composables/useMarkdown';
import Menu from '@/components/Menu.vue';
import AppIcon from '@/components/AppIcon.vue';

/** 四类 chunk（与后端 knowledge_service.CHUNK_TYPES 一致）。 */
const CHUNK_TYPES = [
  { key: 'text', label: '正文' },
  { key: 'formula', label: '公式' },
  { key: 'figure', label: '插图' },
  { key: 'table', label: '表格' },
];

type KbPaper = KnowledgeOverview['items'][number];

/** 向量库状态（`GET /knowledge/index/status` 的 vector 字段）。 */
interface VectorStatus {
  available: boolean;
  count?: number;
  error?: string;
}

/** 索引状态响应的形状（后端动态 JSON，边界处按需声明）。 */
interface KnowledgeIndexStatus {
  paper_id?: string;
  chunks: number;
  chunks_with_hash: number;
  index_versions: Record<string, number>;
  vector: VectorStatus;
  /** 全库对账：MySQL 有、向量库没有的旧 chunk 元数据条数 */
  orphan_chunks?: number | null;
  /** 全库对账：向量库有、MySQL 没有的条数 */
  missing_vectors?: number | null;
  consistent?: boolean | null;
}

const router = useRouter();
const overview = ref<KnowledgeOverview | null>(null);
const indexStatus = ref<KnowledgeIndexStatus | null>(null);
const chunks = ref<ChunkMeta[]>([]);
const busyKey = ref('');

/** 同 Menu 组件的 items 结构（结构类型，无需导出）。 */
interface RowAction {
  key: string;
  label: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
}

/** 行内只留「建索引」；重建 / 移除这类低频且不可逆的操作收进「⋯」菜单。
 *  ⚠️ 不再有单独的「纳入知识库」开关：**建索引即纳入**（用户口径：合并两个说法），
 *  退出知识库 = 「移除」（连索引一起清掉，两侧不会长期不一致）。 */
function rowMenu(p: KbPaper): RowAction[] {
  return [
    { key: 'rebuild', label: '重建索引', icon: 'refresh', disabled: busyKey.value === `idx:${p.id}` },
    {
      key: 'remove',
      label: '从知识库移除',
      icon: 'link-off',
      danger: true,
      disabled: busyKey.value === `remove:${p.id}` || !p.chunks,
    },
  ];
}

function onRowAction(p: KbPaper, key: string) {
  if (key === 'rebuild') { runIndex(p.id, true); return; }
  if (key === 'remove') removeFromKb(p);
}

// chunk 元数据查询条件（后端要求 paper_id 必填）
const chunkPaperId = ref('');
const chunkType = ref('');
const chunkLimit = ref(50);

// 索引状态：空字符串 = 全部知识库
const statusPaperId = ref('');

// 问答面板（RAG）
const ragScope = ref<'current_paper' | 'knowledge_base'>('current_paper');
const ragPaperId = ref('');
const question = ref('');
const ragResult = ref<RagAnswer | null>(null);
const ragError = ref('');
const asking = ref(false);

const papers = computed<KbPaper[]>(() => overview.value?.items || []);
const chunksByType = computed<Record<string, number>>(() => overview.value?.chunks_by_type || {});
const chunksByLang = computed<Record<string, number>>(() => overview.value?.chunks_by_lang || {});
const vectorStore = computed<Record<string, any> | null>(() => overview.value?.vector_store || null);
const embeddingModels = computed<Array<{ id: string; label: string }>>(
  () => (overview.value?.embedding_models || []) as Array<{ id: string; label: string }>);

const vectorAvailable = computed(() => !!vectorStore.value?.available);
const vectorCount = computed<string | number>(() => {
  const s = indexStatus.value;
  if (!s) return '—';
  const v = s.vector || { available: false };
  return v.available ? (v.count ?? 0) : '不可用';
});
/** 索引一致（判定依据是**对账**，不是"两个数字是否相等"：MySQL 里可能留着
 *  向量已被重建丢掉的旧元数据，那种情况直接比条数就会误报成"不一致"）。 */
const statusMatch = computed<boolean | null>(() => {
  const s = indexStatus.value;
  if (!s) return null;
  const v = s.vector || { available: false };
  if (!v.available) return false;
  if (typeof s.consistent === 'boolean') return s.consistent;
  return Number(s.chunks || 0) === Number(v.count || 0);
});
const statusClass = computed(() => {
  if (statusMatch.value === null) return '';
  return statusMatch.value ? 'ok' : 'danger';
});
const statusText = computed(() => {
  if (statusMatch.value === null) return '未知';
  if (statusMatch.value) return '一致';
  const orphans = indexStatus.value?.orphan_chunks || 0;
  return orphans ? `有 ${orphans} 条旧元数据` : '不一致';
});
const orphanChunks = computed(() => Number(indexStatus.value?.orphan_chunks || 0));

/** 概览第三张卡：类型计数压成一行纯文本（原来四个徽章＋两个徽章太吵）。 */
const chunkTypeText = computed(() => CHUNK_TYPES
  .map((t) => `${t.label} ${chunksByType.value[t.key] || 0}`).join(' · '));

function titleOf(paperId?: string | null): string {
  if (!paperId) return '（未知论文）';
  const hit = papers.value.find((p) => p.id === paperId);
  return hit?.title || paperTitle(paperId);
}

function versionOf(paperId: string): string | number {
  const map = (overview.value?.index_versions || {}) as Record<string, number | null | undefined>;
  const v = map[paperId];
  return v === undefined || v === null ? '—' : v;
}

function chunkTypeLabel(key?: string | null): string {
  return CHUNK_TYPES.find((t) => t.key === key)?.label || key || '—';
}

/** 概览 + 论文列表（titleOf 依赖 papers 缓存）。 */
async function refresh(): Promise<void> {
  await withLoading('kb-overview', async () => {
    const data = await api.get<KnowledgeOverview>('/knowledge/overview');
    overview.value = data;
    if (!ragPaperId.value) {
      const first = (data.items || []).find((p) => p.in_knowledge_base) || (data.items || [])[0];
      if (first) ragPaperId.value = first.id;
    }
  });
}

async function loadStatus(): Promise<void> {
  await withLoading('kb-status', async () => {
    const query: Record<string, unknown> = statusPaperId.value ? { paper_id: statusPaperId.value } : {};
    indexStatus.value = await api.get<KnowledgeIndexStatus>('/knowledge/index/status', query);
  });
}

async function loadChunks(): Promise<void> {
  if (!chunkPaperId.value) {
    chunks.value = [];
    toast('请先选择论文', 'warn');
    return;
  }
  await withLoading('kb-chunks', async () => {
    const data = await api.get<{ items: ChunkMeta[]; count?: number }>('/knowledge/chunks', {
      paper_id: chunkPaperId.value,
      chunk_type: chunkType.value || undefined,
      limit: chunkLimit.value,
    });
    chunks.value = data.items || [];
  });
}

/** 首次进入时挑一篇「已纳入知识库且有 chunk」的论文预填 chunk 查询。 */
async function bootstrapQueries(): Promise<void> {
  const items = papers.value || [];
  const preferred = items.find((p) => p.in_knowledge_base && p.chunks > 0)
    || items.find((p) => p.chunks > 0);
  if (preferred) {
    chunkPaperId.value = preferred.id;
    await loadChunks();
  }
  await loadStatus();
}

async function refreshAll(): Promise<void> {
  try {
    await refresh();
    await bootstrapQueries();
  } catch { /* withLoading 已提示 */ }
}

onMounted(async () => {
  try { await loadPapers(); } catch { /* 论文列表失败不阻塞本页 */ }
  await refreshAll();
});

/* --------------------------- 移除 / 对账 --------------------------- */

/** 从知识库移除：清向量 + 清 chunk 元数据（论文与译文不动）。 */
async function removeFromKb(paper: KbPaper): Promise<void> {
  if (!confirm(`从知识库移除《${paper.title}》？\n\n会清掉它的 ${paper.chunks} 条 chunk 与向量`
    + '（论文、译文、笔记、高亮都不受影响）。需要时可以重新「建立索引」加回来。')) return;
  busyKey.value = `remove:${paper.id}`;
  try {
    const res = await api.post<{ removed_chunks?: number }>('/knowledge/remove', { paper_id: paper.id });
    toast(`已从知识库移除（清 ${res?.removed_chunks ?? 0} 条 chunk）`, 'ok', 3200);
    await refresh();
  } catch (err) {
    reportError(err, '移除失败');
  } finally {
    busyKey.value = '';
  }
}

/** 对账：清掉 MySQL 里那些"向量已经不在了"的旧 chunk 元数据。 */
async function reconcileNow(fix: boolean): Promise<void> {
  busyKey.value = 'reconcile';
  try {
    const res = await api.post<{ orphans?: number; missing?: number; removed_metadata?: number }>(
      '/knowledge/reconcile', { fix });
    if (fix) {
      toast(`已清理 ${res?.removed_metadata ?? 0} 条孤儿元数据`
        + (res?.missing ? `、${res.missing} 条孤儿向量` : ''), 'ok', 3600);
    } else {
      toast(`对账完成：孤儿元数据 ${res?.orphans ?? 0} 条、孤儿向量 ${res?.missing ?? 0} 条`,
        res?.orphans || res?.missing ? 'warn' : 'ok', 4200);
    }
    await refresh();
  } catch (err) {
    reportError(err, '对账失败');
  } finally {
    busyKey.value = '';
  }
}

/* ------------------------------ 索引 ------------------------------ */

async function runIndex(paperId: string, rebuild: boolean): Promise<void> {
  busyKey.value = `idx:${paperId}`;
  try {
    const task = await api.post<{ id?: string } | null>('/knowledge/index', { paper_id: paperId, rebuild: !!rebuild });
    toast(`已创建索引任务（${rebuild ? '重建' : '建索引'}）：${task?.id || paperId}`, 'ok');
    await refresh();
    if (statusPaperId.value === paperId || !statusPaperId.value) await loadStatus();
    if (chunkPaperId.value === paperId) await loadChunks();
  } catch (err) {
    reportError(err, rebuild ? '触发重建索引失败' : '触发建索引失败');
  } finally {
    busyKey.value = '';
  }
}

function copyId(text: string): void {
  copyText(text).then(() => toast('已复制', 'ok', 1600)).catch((err) => reportError(err, '复制失败'));
}

/* ---------------------------- 问答面板 ---------------------------- */

async function ask(): Promise<void> {
  const text = question.value.trim();
  if (!text) { toast('请输入问题', 'warn'); return; }
  if (ragScope.value === 'current_paper' && !ragPaperId.value) {
    toast('请先选择「当前论文」范围', 'warn');
    return;
  }
  asking.value = true;
  ragError.value = '';
  ragResult.value = null;
  try {
    const payload: { scope: string; question: string; paper_id?: string } = {
      scope: ragScope.value, question: text,
    };
    if (ragScope.value === 'current_paper') payload.paper_id = ragPaperId.value;
    const data = await api.post<RagAnswer>('/rag/query', payload);
    ragResult.value = data || {};
    if (data && data.implemented === false) {
      toast('RAG 问答尚未实现', 'warn', 5200);
    }
  } catch (err) {
    ragError.value = (err as any)?.message || String(err);
    reportError(err, '问答请求失败');
  } finally {
    asking.value = false;
  }
}

/* ---------------------------- 跳回原文 ---------------------------- */

/**
 * 跳回原文：**唯一入口**。带块锚点 + 页码，阅读器做三级解析（行 id → block_rows → 按页），
 * 见 `utils/navigation.ts`。
 *
 * ⚠️ 必须在点击回调里调（`openTab` 会改响应式状态）：`:to="fn()"` 那种写法会让
 * "渲染 → 改状态 → 再渲染"死循环，本页搜索曾经必定卡死。
 */
function openSource(item: { paper_id?: string | null; block_id?: string | null; page?: number | null }) {
  void router.push(openTab(readerTarget(item.paper_id ?? '', item.block_id, item.page)) as never);
}

function sourceSnippet(source: RagSource): string {
  return source?.snippet || source?.preview || source?.text || '（无片段）';
}

function jsonPreview(value: unknown): string {
  try { return truncate(JSON.stringify(value), 300); } catch { return String(value); }
}

/* ------------------------- 跨文献技术相似检索（/rag/similar） ------------------------- */

interface SimilarItem {
  chunk_id: string;
  paper_id: string;
  paper_title?: string;
  block_id?: string;
  page?: number;
  section?: string | null;
  chunk_type?: string;
  lang?: string;
  langs?: string[];
  snippet?: string;
  scores?: Record<string, number | null>;
  ranks?: Record<string, number | null>;
}

interface SimilarResult {
  implemented?: boolean;
  query?: string;
  scope?: string;
  items?: SimilarItem[];
  retrieval?: Record<string, any>;
}

const simQuery = ref('');
const simScope = ref<'cross' | 'paper' | 'all'>('cross');
const simPaperId = ref('');
const simBusy = ref(false);
const simResult = ref<SimilarResult | null>(null);
const simError = ref('');

/** 找相似：跨文献 = 排除所选论文（"我这篇的做法，别人怎么做的"）。 */
async function findSimilar(): Promise<void> {
  const text = simQuery.value.trim();
  if (!text) { toast('请先描述一段技术', 'warn'); return; }
  if (simScope.value !== 'all' && !simPaperId.value) {
    toast('请先选择论文', 'warn'); return;
  }
  simBusy.value = true;
  simError.value = '';
  simResult.value = null;
  try {
    const payload: Record<string, unknown> = {
      query: text, scope: simScope.value, top_k: 6,
    };
    if (simPaperId.value) payload.paper_id = simPaperId.value;
    simResult.value = (await api.post<SimilarResult>('/rag/similar', payload)) || {};
  } catch (err) {
    simError.value = (err as any)?.message || String(err);
    reportError(err, '相似检索失败');
  } finally {
    simBusy.value = false;
  }
}

function scoreOf(item: SimilarItem, key: string): string {
  const value = item?.scores?.[key];
  return value === null || value === undefined ? '—' : Number(value).toFixed(3);
}

/** 双语句：`中` / `原` / `中+原`（两行都被召回时标注） */
function langLabel(item: SimilarItem): string {
  const langs = item?.langs?.length ? item.langs : [item?.lang || 'en'];
  return langs.map((l) => (l === 'zh' ? '中' : '原')).join('+');
}

/** 一行里只给 `rrf / rerank`，召回名次这类细节放 tooltip（不占版面）。 */
function scoreTitle(item: SimilarItem): string {
  const parts = [`RRF ${scoreOf(item, 'rrf')}`, `重排 ${scoreOf(item, 'rerank')}`];
  if (item?.ranks?.vector_rank) parts.push(`向量 #${item.ranks.vector_rank}`);
  if (item?.ranks?.lexical_rank) parts.push(`词法 #${item.ranks.lexical_rank}`);
  return parts.join(' · ');
}

/** 检索漏斗：`向量 20 + 词法 20 → 融合 12 → 返回 6 · qwen3-vl-rerank`（完整版进 title）。 */
const simMeta = computed(() => {
  const r = simResult.value?.retrieval;
  if (!r) return null;
  const model = r.rerank?.model || '—';
  return {
    short: `${r.vector_hits ?? 0}+${r.lexical_hits ?? 0} → ${r.fused ?? 0} → ${r.returned ?? 0} · ${model}`,
    full: `向量命中 ${r.vector_hits ?? 0} · 词法命中 ${r.lexical_hits ?? 0}`
      + ` · RRF 融合 ${r.fused ?? 0} · 返回 ${r.returned ?? 0} · 重排 ${model}`
      + (r.rerank?.degraded ? '（已降级）' : ''),
  };
});

/** 问答回答的元信息（模型 / 输出 token / 上下文），压成一行。 */
const ragMeta = computed(() => {
  const r = ragResult.value;
  if (!r || r.implemented !== true) return '';
  const bits: string[] = [];
  if (r.usage?.model) bits.push(String(r.usage.model));
  if (r.usage?.completion_tokens) bits.push(`输出 ${r.usage.completion_tokens} tok`);
  if (r.context) {
    bits.push(`上下文 ${r.context.chars} 字`
      + (r.context.truncated ? `（截断 ${r.context.truncated} 条）` : ''));
  }
  return bits.join(' · ');
});
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>知识库</h1>
      </div>
      <div class="grow"></div>
      <div class="page-actions">
        <button class="btn icon" title="刷新概览与索引状态" @click="refreshAll" :disabled="state.loading['kb-overview']">
          <AppIcon name="refresh" :size="15" />
        </button>
      </div>
    </div>

    <div class="grid cols-4" v-if="overview">
      <div class="card stat">
        <div class="n">{{ overview.papers_total }}</div>
        <div class="k">论文总数</div>
      </div>
      <div class="card stat">
        <div class="n">{{ overview.papers_in_kb }}</div>
        <div class="k">已纳入知识库（{{ pct(overview.papers_in_kb, overview.papers_total) }}%）</div>
      </div>
      <div class="card stat">
        <div class="n">{{ overview.chunks }}</div>
        <div class="k">chunk 总数</div>
        <div class="tiny faint" style="margin-top:6px">{{ chunkTypeText }}</div>
        <div class="row wrap" style="margin-top:4px">
          <span class="badge accent">中文 {{ chunksByLang.zh || 0 }}</span>
          <span class="badge">原文 {{ chunksByLang.en || 0 }}</span>
        </div>
      </div>
      <div class="card stat">
        <div class="n" :style="vectorAvailable ? '' : 'color:var(--danger)'">
          {{ vectorAvailable ? '可用' : '不可用' }}
        </div>
        <div class="k">向量库</div>
        <div class="tiny faint" style="margin-top:6px">
          {{ vectorAvailable ? `LanceDB ${vectorStore?.chunks ?? 0} 条` : (vectorStore?.error || '未检测') }}
        </div>
      </div>
    </div>
    <div class="card empty" v-else-if="state.loading['kb-overview']">正在读取知识库概览…</div>
    <div class="card empty" v-else>暂无概览数据，点右上角「刷新」重试。</div>

    <div class="card" style="margin-top:14px" v-if="overview">
      <div class="card-head">
        <span>论文</span>
      </div>
      <div class="card-body tight">
        <table class="data fixed" v-if="papers.length">
          <thead>
            <tr>
              <th class="w-paper">论文</th><th class="w-status">状态</th><th class="w-kb">知识库</th>
              <th class="num w-num">chunks</th><th class="w-ver">索引版本</th><th class="w-ops">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in papers" :key="p.id">
              <td>
                <RouterLink class="truncate" :to="{ name: 'reader', params: { paperId: p.id } }"
                            :title="p.title">{{ p.title }}</RouterLink>
                <div class="tiny faint mono truncate" :title="p.id">{{ p.id }}</div>
              </td>
              <td><span class="badge" :class="paperStatus(p.status).cls">{{ paperStatus(p.status).label }}</span></td>
              <td>
                <span class="badge" :class="p.in_knowledge_base ? 'accent' : ''">
                  {{ p.in_knowledge_base ? '已纳入' : '未纳入' }}
                </span>
              </td>
              <td class="num">{{ p.chunks }}</td>
              <td class="mono tiny">{{ versionOf(p.id) }}</td>
              <td class="ops">
                <div class="row nowrap" style="gap: 6px">
                  <button class="btn sm" :disabled="busyKey === 'idx:' + p.id" @click="runIndex(p.id, false)">
                    <AppIcon name="layers" :size="14" />建索引
                  </button>
                  <Menu :items="rowMenu(p)" up :label="`更多操作：${p.title}`"
                        @select="onRowAction(p, $event)" />
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="empty" v-else>还没有论文，先去「论文库」导入并解析。</div>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <div class="card-head">
        <span>索引状态</span>
        <span class="badge" :class="statusClass">{{ statusText }}</span>
        <span class="spacer"></span>
        <select v-model="statusPaperId" @change="loadStatus" style="min-width:220px">
          <option value="">全部知识库</option>
          <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
        </select>
      </div>
      <div class="card-body">
        <div class="grid cols-3" v-if="indexStatus">
          <div class="stat">
            <div class="n">{{ indexStatus.chunks }}</div>
            <div class="k">MySQL chunk 数</div>
          </div>
          <div class="stat">
            <div class="n" :style="statusMatch ? '' : 'color:var(--danger)'">{{ vectorCount }}</div>
            <div class="k">LanceDB 条数</div>
          </div>
          <div class="stat">
            <div class="n">
              <span class="badge" :class="statusClass">{{ statusText }}</span>
            </div>
            <div class="k">
              <template v-if="statusMatch">两侧一致</template>
              <template v-else-if="orphanChunks">
                MySQL 多出 {{ orphanChunks }} 条旧元数据（向量已被重建丢弃）
                <button class="btn sm" :disabled="busyKey === 'reconcile'"
                        @click="reconcileNow(true)">清理</button>
              </template>
              <template v-else>两侧不一致，需重建索引</template>
            </div>
          </div>
        </div>
        <div class="empty" v-else>{{ state.loading['kb-status'] ? '正在读取索引状态…' : '暂无索引状态。' }}</div>

        <div class="row wrap tiny" style="margin-top:10px" v-if="indexStatus">
          <span class="faint">索引版本</span>
          <span class="badge" v-for="(v, pid) in (indexStatus.index_versions || {})" :key="pid">
            {{ titleOf(pid) }} v{{ v }}
          </span>
          <span class="faint" v-if="!Object.keys(indexStatus.index_versions || {}).length">（无）</span>
          <span class="faint" style="margin-left:10px">embedding</span>
          <span class="badge" v-for="m in embeddingModels" :key="m.id" :title="m.id">{{ m.label }}</span>
          <span class="faint" v-if="!embeddingModels.length">（未记录）</span>
          <span class="spacer"></span>
          <button class="btn sm ghost" :disabled="busyKey === 'reconcile'"
                  title="只比对 MySQL chunk 元数据与向量库，报告差异，不改任何东西"
                  @click="reconcileNow(false)">对账</button>
        </div>
        <div class="small" style="margin-top:8px;color:var(--danger)"
             v-if="indexStatus && indexStatus.vector && !indexStatus.vector.available">
          向量库不可用：{{ indexStatus.vector.error || '未返回原因' }}
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <div class="card-head">
        <span>Chunk 元数据</span>
        <span class="badge" v-if="chunks.length">{{ chunks.length }} 条</span>
        <span class="spacer"></span>
        <select v-model="chunkPaperId" @change="loadChunks" style="min-width:200px">
          <option value="">选择论文…</option>
          <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
        </select>
        <select v-model="chunkType" @change="loadChunks">
          <option value="">全部类型</option>
          <option v-for="t in CHUNK_TYPES" :key="t.key" :value="t.key">{{ t.label }}</option>
        </select>
        <select v-model.number="chunkLimit" @change="loadChunks">
          <option :value="20">20 条</option>
          <option :value="50">50 条</option>
          <option :value="100">100 条</option>
          <option :value="200">200 条</option>
        </select>
      </div>
      <div class="card-body tight">
        <table class="data" v-if="chunks.length">
          <thead>
            <tr>
              <th>chunk</th><th>类型</th><th class="num">页</th><th>章节</th>
              <th>preview</th><th class="num">tokens</th><th class="w-ops">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in chunks" :key="c.id">
              <td class="mono tiny truncate" style="max-width:240px"
                  :title="`${c.id}\nhash ${c.hash || '—'}\n索引版本 ${c.index_version}`">{{ c.id }}</td>
              <td>
                <div class="row nowrap" style="gap:4px">
                  <span class="badge accent">{{ chunkTypeLabel(c.chunk_type) }}</span>
                  <span class="badge" :class="{ accent: c.lang === 'zh' }">{{ c.lang === 'zh' ? '中' : '原' }}</span>
                </div>
              </td>
              <td class="num">{{ c.page }}</td>
              <td class="tiny truncate" style="max-width:140px" :title="c.section || ''">{{ c.section || '—' }}</td>
              <td class="small clamp-2" style="max-width:320px" :title="c.preview || ''">{{ c.preview || '—' }}</td>
              <td class="num">{{ c.tokens }}</td>
              <td class="ops">
                <div class="row nowrap">
                  <button class="btn sm" title="跳到这一段原文" @click="openSource(c)">
                    <AppIcon name="locate" :size="14" />原文
                  </button>
                  <button class="btn sm ghost" title="复制 chunk id" @click="copyId(c.id)">
                    <AppIcon name="copy" :size="14" />
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="empty" v-else-if="state.loading['kb-chunks']">正在读取 chunk…</div>
        <div class="empty" v-else-if="!chunkPaperId">请选择论文。</div>
        <div class="empty" v-else>没有符合条件的 chunk（该论文可能尚未建索引）。</div>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <div class="card-head">
        <span>跨文献技术相似检索</span>
        <span class="spacer"></span>
        <span class="tiny faint" v-if="simMeta" :title="simMeta.full">{{ simMeta.short }}</span>
      </div>
      <div class="card-body">
        <div class="row wrap">
          <div class="seg">
            <button :class="{ active: simScope === 'cross' }" title="排除所选论文：我这篇的做法，别人怎么做的"
                    @click="simScope = 'cross'">跨文献</button>
            <button :class="{ active: simScope === 'paper' }" title="只在所选论文内检索"
                    @click="simScope = 'paper'">本论文</button>
            <button :class="{ active: simScope === 'all' }" title="不排除任何论文"
                    @click="simScope = 'all'">全库</button>
          </div>
          <select v-if="simScope !== 'all'" v-model="simPaperId" style="min-width:240px">
            <option value="">选择论文…</option>
            <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
          </select>
          <span class="spacer"></span>
          <button class="btn primary sm" :disabled="simBusy" @click="findSimilar">
            <AppIcon name="search" :size="14" />{{ simBusy ? '检索中…' : '找相似' }}
          </button>
        </div>

        <textarea v-model="simQuery" class="wide" rows="2"
                  placeholder="描述一段技术，例如「无需校准数据的在线向量量化」"
                  @keydown.ctrl.enter="findSimilar"></textarea>

        <div style="margin-top:10px" v-if="simResult">
          <div v-for="note in (simResult.retrieval?.notes || [])" :key="note" class="note">
            <span class="badge warn">提示</span><span class="small">{{ note }}</span>
          </div>

          <div v-for="(it, i) in (simResult.items || [])" :key="it.chunk_id" class="hit">
            <div class="hit-head">
              <span class="hit-idx">{{ i + 1 }}</span>
              <span class="hit-title" :title="it.paper_title || it.paper_id">
                {{ it.paper_title || it.paper_id }}
              </span>
              <span class="badge">p{{ it.page }}</span>
              <span class="badge" :class="{ accent: (it.lang || 'en') === 'zh' }">
                {{ chunkTypeLabel(it.chunk_type) }}·{{ langLabel(it) }}
              </span>
              <span class="hit-sec" v-if="it.section" :title="it.section">{{ it.section }}</span>
              <span class="spacer"></span>
              <span class="hit-score" :title="scoreTitle(it)">
                {{ scoreOf(it, 'rrf') }} / {{ scoreOf(it, 'rerank') }}
              </span>
              <button class="btn sm ghost" title="跳到这一段原文" @click="openSource(it)">
                <AppIcon name="locate" :size="13" />原文
              </button>
            </div>
            <div class="hit-text">{{ it.snippet }}</div>
          </div>
          <div class="empty" v-if="!(simResult.items || []).length">没有找到相似内容（索引可能还没建）。</div>
        </div>
        <div class="small" style="margin-top:10px;color:var(--danger)" v-else-if="simError">
          相似检索失败：{{ simError }}
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <div class="card-head">
        <span>问答（RAG）</span>
        <span class="spacer"></span>
      </div>
      <div class="card-body">
        <div class="row wrap">
          <div class="seg">
            <button :class="{ active: ragScope === 'current_paper' }" @click="ragScope = 'current_paper'">本论文</button>
            <button :class="{ active: ragScope === 'knowledge_base' }" @click="ragScope = 'knowledge_base'">全库</button>
          </div>
          <select v-if="ragScope === 'current_paper'" v-model="ragPaperId" style="min-width:240px">
            <option value="">选择论文…</option>
            <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
          </select>
          <span class="spacer"></span>
          <button class="btn primary sm" :disabled="asking" @click="ask">
            <AppIcon name="forward" :size="14" />{{ asking ? '请求中…' : '发送' }}
          </button>
        </div>

        <textarea v-model="question" class="wide" rows="2"
                  placeholder="针对论文提问，例如「核心贡献是什么？」"
                  @keydown.ctrl.enter="ask"></textarea>

        <div style="margin-top:10px" v-if="ragResult">
          <template v-if="ragResult.implemented === false">
            <div class="note"><b>RAG 问答尚未实现</b>
              <span class="small">{{ ragResult.message || '后端未返回说明文案。' }}</span>
            </div>
          </template>

          <template v-else-if="ragResult.implemented === true">
            <div class="md" v-html="mdToHtml(ragResult.answer || '')"></div>
            <div class="row wrap tiny" style="margin-top:8px">
              <span class="badge" :class="ragResult.grounded ? 'ok' : 'warn'">
                {{ ragResult.grounded ? '引用可校验' : '有引用未通过校验' }}
              </span>
              <span class="badge warn" v-if="ragResult.insufficient">库内文献不足</span>
              <span class="faint">来源 {{ (ragResult.sources || []).length }} 条</span>
              <span class="faint" v-if="ragMeta">· {{ ragMeta }}</span>
            </div>

            <div v-for="p in (ragResult.validation?.problems || [])" :key="p" class="note">
              <span class="badge warn">校验</span><span class="small">{{ p }}</span>
            </div>
            <div v-for="n in (ragResult.validation?.notes || [])" :key="n" class="note">
              <span class="badge">提示</span><span class="small">{{ n }}</span>
            </div>

            <div v-for="(s, i) in (ragResult.sources || [])" :key="s.chunk_id || i" class="hit">
              <div class="hit-head">
                <span class="badge accent">{{ s.label || ('S' + (i + 1)) }}</span>
                <span class="hit-title" :title="s.paper_title || titleOf(s.paper_id)">
                  {{ s.paper_title || titleOf(s.paper_id) }}
                </span>
                <span class="badge">p{{ s.page }}</span>
                <span class="badge" :class="{ accent: s.lang === 'zh' }">
                  {{ chunkTypeLabel(s.chunk_type) }}·{{ s.lang === 'zh' ? '中' : '原' }}
                </span>
                <span class="spacer"></span>
                <span class="hit-score" v-if="s.score !== null && s.score !== undefined"
                      title="重排得分">重排 {{ Number(s.score).toFixed(3) }}</span>
                <button class="btn sm ghost" title="跳到这一段原文" @click="openSource(s)">
                  <AppIcon name="locate" :size="13" />原文
                </button>
              </div>
              <div class="hit-text">{{ sourceSnippet(s) }}</div>
            </div>
          </template>

          <template v-else>
            <div class="note">
              <span class="badge warn">响应异常</span>
              <span class="small">后端未返回 implemented 字段。</span>
              <div class="tiny mono" style="margin-top:4px">{{ jsonPreview(ragResult) }}</div>
            </div>
          </template>
        </div>

        <div class="small" style="margin-top:10px;color:var(--danger)" v-else-if="ragError">
          问答失败：{{ ragError }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 论文归属表：固定列宽，徽章不会被拆成两行 */
.w-paper { width: 300px; }
.w-status { width: 92px; }
.w-kb { width: 88px; }
.w-num { width: 76px; }
.w-ver { width: 84px; }
.w-ops { width: 150px; }
td a.truncate { display: block; }

/* 卡头里的下拉不撑满整行（卡片头是 flex，select 容易被拉长） */
.card-head select { max-width: 300px; }

/**
 * 命中条目（相似检索 / RAG 来源）：**扁平分隔线列表**，不做卡片套卡片 ——
 * 一屏能看六条，而不是三条（用户口径：内容太冗余）。
 */
.hit { padding: 7px 0; border-top: 1px solid var(--divider); }
.hit:first-child { border-top: 0; }
.hit-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.hit-idx { min-width: 14px; text-align: right; color: var(--text-faint); font-size: 11px; }
.hit-title { font-weight: 600; font-size: 12.5px; max-width: 280px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.hit-sec { font-size: 11px; color: var(--text-faint); max-width: 190px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.hit-score { font-family: var(--mono); font-size: 11px; color: var(--text-faint);
  font-variant-numeric: tabular-nums; }
.hit-text { margin-top: 2px; font-size: 12px; color: var(--text-dim); line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

/* 提示 / 校验一条（中性底色 + 语义左边框，比整块卡片轻） */
.note { display: flex; align-items: flex-start; gap: 6px; padding: 5px 0; }

/* 整行宽的输入框（`.card-body` 是块级，textarea 默认按 `cols` 只有 ~20 字宽） */
.wide { width: 100%; display: block; margin-top: 8px; }
</style>
