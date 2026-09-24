<script setup lang="ts">
/**
 * 知识库：概览 / 纳入移出 / 触发 EMBED+INDEX / 索引状态 / chunk 元数据。
 *
 * 检索与 RAG 问答后端尚未实现，`POST /rag/query` 返回 `{implemented: false, message}`，
 * 界面对此给出显式中文提示，不静默失败。
 */

import { computed, onMounted, ref } from 'vue';
import { RouterLink } from 'vue-router';
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
}

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

/** 行内只留「建索引」；重建与纳入/移出这类低频操作收进「⋯」菜单。 */
function rowMenu(p: KbPaper): RowAction[] {
  return [
    { key: 'rebuild', label: '重建索引', icon: 'refresh', disabled: busyKey.value === `idx:${p.id}` },
    {
      key: 'kb',
      label: p.in_knowledge_base ? '移出知识库' : '纳入知识库',
      icon: p.in_knowledge_base ? 'link-off' : 'link',
      disabled: busyKey.value === `kb:${p.id}`,
    },
  ];
}

function onRowAction(p: KbPaper, key: string) {
  if (key === 'rebuild') { runIndex(p.id, true); return; }
  if (key === 'kb') toggleKb(p);
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
const embeddingModels = computed<string[]>(() => overview.value?.embedding_models || []);

const vectorAvailable = computed(() => !!vectorStore.value?.available);
const vectorCount = computed<string | number>(() => {
  const s = indexStatus.value;
  if (!s) return '—';
  const v = s.vector || { available: false };
  return v.available ? (v.count ?? 0) : '不可用';
});
/** MySQL chunk 数 与 LanceDB 条数 是否一致；null = 尚不清楚。 */
const statusMatch = computed<boolean | null>(() => {
  const s = indexStatus.value;
  if (!s) return null;
  const v = s.vector || { available: false };
  if (!v.available) return false;
  return Number(s.chunks || 0) === Number(v.count || 0);
});
const statusClass = computed(() => {
  if (statusMatch.value === null) return '';
  return statusMatch.value ? 'ok' : 'danger';
});
const statusText = computed(() => {
  if (statusMatch.value === null) return '未知';
  return statusMatch.value ? '一致' : '不一致';
});

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

function indexStatusLabel(): string {
  const s = vectorStore.value;
  if (!s) return '未检测';
  return s.available ? '可用' : '不可用';
}

function indexStatusHint(): string {
  const s = vectorStore.value;
  if (!s) return '尚未获取向量库状态';
  if (!s.available) return s.error || '向量库不可用';
  return `LanceDB 条数 ${s.chunks ?? 0}`;
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

/* --------------------------- 纳入 / 移出 --------------------------- */

async function toggleKb(paper: KbPaper): Promise<void> {
  busyKey.value = `kb:${paper.id}`;
  try {
    await api.post('/knowledge/papers', {
      paper_id: paper.id,
      include: !paper.in_knowledge_base,
    });
    toast(paper.in_knowledge_base ? '已移出知识库' : '已纳入知识库', 'ok', 2200);
    await refresh();
  } catch (err) {
    reportError(err, '更新知识库归属失败');
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

/** 来源 → 阅读器标签页（跳回原文；已开着就激活它）。 */
function sourceTo(source: RagSource) {
  return openTab({
    name: 'reader',
    params: { paperId: source.paper_id ?? '' },
    query: source.block_id ? { block: source.block_id } : {},
  });
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
    toast('请先选择论文', 'warn');
    return;
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
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>知识库</h1>
      </div>
      <div class="grow"></div>
      <div class="page-actions">
        <button class="btn icon" title="刷新知识库概览" @click="refreshAll" :disabled="state.loading['kb-overview']">
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
        <div class="k">chunk 总数（双语行）</div>
        <div class="row wrap" style="margin-top:6px">
          <span class="badge" v-for="t in CHUNK_TYPES" :key="t.key">
            {{ t.label }} {{ chunksByType[t.key] || 0 }}
          </span>
        </div>
        <div class="row wrap" style="margin-top:4px">
          <span class="badge accent">中文 {{ chunksByLang.zh || 0 }}</span>
          <span class="badge">原文 {{ chunksByLang.en || 0 }}</span>
        </div>
      </div>
      <div class="card stat">
        <div class="n" :style="vectorAvailable ? '' : 'color:var(--danger)'">{{ indexStatusLabel() }}</div>
        <div class="k">向量库状态</div>
        <div class="tiny faint" style="margin-top:4px">{{ indexStatusHint() }}</div>
      </div>
    </div>
    <div class="card empty" v-else-if="state.loading['kb-overview']">正在读取知识库概览…</div>
    <div class="card empty" v-else>暂无概览数据，点右上角「刷新」重试。</div>

    <div class="card" style="margin-top:14px" v-if="overview">
      <div class="card-head">
        <span>论文与知识库归属</span>
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
        <button class="btn icon" title="刷新索引状态" @click="loadStatus" :disabled="state.loading['kb-status']">
          <AppIcon name="refresh" :size="15" />
        </button>
      </div>
      <div class="card-body">
        <div class="grid cols-4" v-if="indexStatus">
          <div class="stat">
            <div class="n">{{ indexStatus.chunks }}</div>
            <div class="k">MySQL chunk 数</div>
          </div>
          <div class="stat">
            <div class="n">{{ indexStatus.chunks_with_hash }}</div>
            <div class="k">含 content_hash</div>
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
              {{ statusMatch ? '两侧条数一致' : '两侧条数不一致，需重建索引' }}
            </div>
          </div>
        </div>
        <div class="empty" v-else>{{ state.loading['kb-status'] ? '正在读取索引状态…' : '暂无索引状态。' }}</div>

        <div class="row wrap tiny" style="margin-top:10px" v-if="indexStatus">
          <span class="faint">索引版本：</span>
          <span class="badge" v-for="(v, pid) in (indexStatus.index_versions || {})" :key="pid">
            {{ titleOf(pid) }} · v{{ v }}
          </span>
          <span class="faint" v-if="!Object.keys(indexStatus.index_versions || {}).length">（无）</span>
          <span class="spacer"></span>
          <span class="faint">embedding 模型：</span>
          <span class="badge" v-for="m in embeddingModels" :key="m">{{ m }}</span>
          <span class="faint" v-if="!embeddingModels.length">（未记录）</span>
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
        <button class="btn" @click="loadChunks" :disabled="state.loading['kb-chunks']">
          <AppIcon name="search" :size="15" />查询
        </button>
      </div>
      <div class="card-body tight">
        <table class="data" v-if="chunks.length">
          <thead>
            <tr>
              <th>chunk</th><th>类型</th><th>语言</th><th class="num">页</th><th>章节</th>
              <th>preview</th><th class="num">tokens</th><th>hash</th>
              <th class="num">版本</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in chunks" :key="c.id">
              <td class="mono tiny truncate" style="max-width:220px" :title="c.id">{{ c.id }}</td>
              <td><span class="badge accent">{{ c.chunk_type }}</span></td>
              <td><span class="badge" :class="{ accent: c.lang === 'zh' }">{{ c.lang === 'zh' ? '中文' : '原文' }}</span></td>
              <td class="num">{{ c.page }}</td>
              <td class="tiny truncate" style="max-width:140px" :title="c.section || ''">{{ c.section || '—' }}</td>
              <td class="small clamp-2" style="max-width:320px" :title="c.preview || ''">{{ c.preview || '—' }}</td>
              <td class="num">{{ c.tokens }}</td>
              <td class="mono tiny" :title="c.hash || ''">{{ (c.hash || '—').slice(0, 10) }}</td>
              <td class="num">{{ c.index_version }}</td>
              <td>
                <div class="row wrap">
                  <RouterLink class="btn sm"
                              :to="{ name: 'reader', params: { paperId: c.paper_id }, query: { block: c.block_id } }">
                    <AppIcon name="locate" :size="14" />原文
                  </RouterLink>
                  <button class="btn sm ghost" @click="copyId(c.id)">
                    <AppIcon name="copy" :size="14" />复制 ID
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="empty" v-else-if="state.loading['kb-chunks']">正在读取 chunk…</div>
        <div class="empty" v-else-if="!chunkPaperId">请选择论文后查询 chunk 元数据。</div>
        <div class="empty" v-else>没有符合条件的 chunk（该论文可能尚未建索引）。</div>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <div class="card-head">
        <span>跨文献技术相似检索</span>
        <span class="badge ok">向量 + 词法 + 重排</span>
        <span class="spacer"></span>
      </div>
      <div class="card-body">
        <div class="row wrap" style="margin-top:10px">
          <div class="seg">
            <button :class="{ active: simScope === 'cross' }" @click="simScope = 'cross'">跨文献（排除所选）</button>
            <button :class="{ active: simScope === 'paper' }" @click="simScope = 'paper'">只在该篇内</button>
            <button :class="{ active: simScope === 'all' }" @click="simScope = 'all'">全部知识库</button>
          </div>
          <select v-if="simScope !== 'all'" v-model="simPaperId" style="min-width:240px">
            <option value="">选择论文…</option>
            <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
          </select>
        </div>

        <div class="col" style="margin-top:10px">
          <textarea v-model="simQuery" rows="2"
                    placeholder="描述一段技术，例如「无需校准数据的在线向量量化」——去别的论文里找谁在做类似的事"></textarea>
          <div class="row wrap">
            <button class="btn primary sm" :disabled="simBusy" @click="findSimilar">
              <AppIcon name="search" :size="14" />{{ simBusy ? '检索中…' : '找相似' }}
            </button>
            <span class="tiny faint" v-if="simResult?.retrieval">
              向量 {{ simResult.retrieval.vector_hits }} / 词法 {{ simResult.retrieval.lexical_hits }}
              → 融合 {{ simResult.retrieval.fused }} → 返回 {{ simResult.retrieval.returned }}
              · 重排 {{ simResult.retrieval.rerank?.model || '—' }}
            </span>
          </div>
        </div>

        <div style="margin-top:12px" v-if="simResult">
          <div v-for="note in (simResult.retrieval?.notes || [])" :key="note"
               class="annotation-item manual">
            <div class="row wrap">
              <span class="badge warn">提示</span>
              <span class="small">{{ note }}</span>
            </div>
          </div>

          <div v-for="(it, i) in (simResult.items || [])" :key="it.chunk_id"
               class="annotation-item" style="margin-top:8px">
            <div class="row wrap tiny">
              <span class="badge accent">{{ i + 1 }}</span>
              <span class="badge">{{ it.paper_title || it.paper_id }}</span>
              <span class="badge">p{{ it.page }}</span>
              <span class="badge" :class="{ accent: (it.lang || 'en') === 'zh' }">{{ langLabel(it) }}</span>
              <span class="badge">{{ it.chunk_type }}</span>
              <span class="spacer"></span>
              <span class="mono faint">rrf {{ scoreOf(it, 'rrf') }} · rerank {{ scoreOf(it, 'rerank') }}</span>
              <RouterLink :to="sourceTo(it)">跳回原文</RouterLink>
            </div>
            <div class="small clamp-2" style="margin-top:4px">{{ it.snippet }}</div>
            <div class="tiny faint">
              向量 #{{ it.ranks?.vector_rank ?? '—' }} · 词法 #{{ it.ranks?.lexical_rank ?? '—' }}
              <span v-if="it.section"> · {{ it.section }}</span>
            </div>
          </div>
          <div class="empty" v-if="!(simResult.items || []).length">没有找到相似内容（索引可能还没建）。</div>
        </div>
        <div class="small" style="margin-top:12px;color:var(--danger)" v-else-if="simError">
          相似检索失败：{{ simError }}
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <div class="card-head">
        <span>问答（RAG）</span>
        <span class="badge ok">混合检索 + 引用校验</span>
        <span class="spacer"></span>
      </div>
      <div class="card-body">
        <div class="row wrap" style="margin-top:10px">
          <div class="seg">
            <button :class="{ active: ragScope === 'current_paper' }" @click="ragScope = 'current_paper'">当前论文</button>
            <button :class="{ active: ragScope === 'knowledge_base' }" @click="ragScope = 'knowledge_base'">全部知识库</button>
          </div>
          <select v-if="ragScope === 'current_paper'" v-model="ragPaperId" style="min-width:240px">
            <option value="">选择论文…</option>
            <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
          </select>
        </div>

        <div class="col" style="margin-top:10px">
          <textarea v-model="question" rows="3"
                    placeholder="针对论文提问，例如「这篇论文的核心贡献是什么？」"></textarea>
          <div class="row wrap">
            <button class="btn primary sm" :disabled="asking" @click="ask">
              <AppIcon name="forward" :size="14" />{{ asking ? '请求中…' : '发送' }}
            </button>
          </div>
        </div>

        <div style="margin-top:12px" v-if="ragResult">
          <template v-if="ragResult.implemented === false">
            <div class="annotation-item manual">
              <div class="row wrap">
                <b>RAG 问答尚未实现</b>
                <span class="spacer"></span>
              </div>
              <p class="small" style="margin:6px 0 0">
                {{ ragResult.message || '后端未返回说明文案。' }}
              </p>
            </div>
          </template>

          <template v-else-if="ragResult.implemented === true">
            <div class="card">
              <div class="card-body">
                <div class="md" v-html="mdToHtml(ragResult.answer || '')"></div>
                <div class="row wrap tiny" style="margin-top:8px">
                  <span class="badge" :class="ragResult.grounded ? 'ok' : 'warn'">
                    {{ ragResult.grounded ? '引用可校验' : '存在未通过校验的引用' }}
                  </span>
                  <span class="badge warn" v-if="ragResult.insufficient">库内文献不足</span>
                  <span class="faint">来源 {{ (ragResult.sources || []).length }} 条</span>
                  <span class="faint" v-if="ragResult.usage?.model">
                    · {{ ragResult.usage.model }}
                    <template v-if="ragResult.usage.completion_tokens">
                      · 输出 {{ ragResult.usage.completion_tokens }} tok
                    </template>
                    <template v-if="ragResult.usage.reasoning_tokens">
                      （推理 {{ ragResult.usage.reasoning_tokens }}）
                    </template>
                  </span>
                  <span class="faint" v-if="ragResult.context">
                    · 上下文 {{ ragResult.context.chars }} 字<template
                      v-if="ragResult.context.truncated">，截断 {{ ragResult.context.truncated }} 条</template>
                  </span>
                </div>

                <div v-for="p in (ragResult.validation?.problems || [])" :key="p"
                     class="annotation-item manual" style="margin-top:8px">
                  <div class="row wrap">
                    <span class="badge warn">校验</span>
                    <span class="small">{{ p }}</span>
                  </div>
                </div>
                <div v-for="n in (ragResult.validation?.notes || [])" :key="n"
                     class="annotation-item manual" style="margin-top:6px">
                  <div class="row wrap">
                    <span class="badge">提示</span>
                    <span class="small">{{ n }}</span>
                  </div>
                </div>

                <div v-for="(s, i) in (ragResult.sources || [])" :key="s.chunk_id || i"
                     class="annotation-item" style="margin-top:8px">
                  <div class="row wrap tiny">
                    <span class="badge accent">{{ s.label || ('S' + (i + 1)) }}</span>
                    <span class="badge">{{ s.paper_title || titleOf(s.paper_id) }}</span>
                    <span class="badge">p{{ s.page }}</span>
                    <span class="badge" :class="{ accent: s.lang === 'zh' }">
                      {{ s.lang === 'zh' ? '中' : '原' }}
                    </span>
                    <span class="spacer"></span>
                    <span class="mono faint" v-if="s.score !== null && s.score !== undefined">
                      rerank {{ Number(s.score).toFixed(3) }}
                    </span>
                    <RouterLink :to="sourceTo(s)">跳回原文</RouterLink>
                  </div>
                  <div class="small clamp-2" style="margin-top:4px">{{ sourceSnippet(s) }}</div>
                </div>
              </div>
            </div>
          </template>

          <template v-else>
            <div class="annotation-item">
              <div class="row wrap">
                <span class="badge warn">响应异常</span>
                <span class="small">后端未返回 implemented 字段。</span>
              </div>
              <div class="tiny mono" style="margin-top:4px">{{ jsonPreview(ragResult) }}</div>
            </div>
          </template>
        </div>

        <div class="small" style="margin-top:12px;color:var(--danger)" v-else-if="ragError">
          问答失败：{{ ragError }}
        </div>
        <div class="empty" v-else>还没有提问。</div>
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

/* `.annotation-item`：问答 / 手动批注的条目标记，用中性左边框 + 语义色区分。 */
.annotation-item { border-left: 3px solid var(--border-strong); background: var(--bg-sunken); border-radius: var(--r-sm); padding: 10px 12px; margin-bottom: 8px; }
.annotation-item.manual { border-left-color: var(--accent); }
.annotation-item.qa { border-left-color: var(--ok); }
</style>
