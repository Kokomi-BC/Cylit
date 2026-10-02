<script setup lang="ts">
/**
 * 设置页「知识库与索引」面板。
 *
 * 这四块原本长在「知识库」页上（用户口径：那页第一眼全是设置）—— 从这一轮起搬到设置页：
 * **概览统计 / 论文索引表 / 索引状态与对账 / chunk 元数据**。它们都是"管理与诊断"，
 * 不属于读者要用的功能；知识库页正在按新规划重做成「搜索 + 浏览」工作台。
 *
 * 数据面完全复用既有接口：`/knowledge/overview`、`/knowledge/index/status`、
 * `/knowledge/chunks`、`/knowledge/index`、`/knowledge/remove`、`/knowledge/reconcile`。
 * 这里**只读 + 触发任务**，不碰论文与译文。
 *
 * ⚠️ 加载键统一加 `setkb-` 前缀：`state.loading` 是全页共享的，知识库页同时开着时
 * 不该互相点亮对方的"加载中"。
 */
import { computed, onMounted, ref } from 'vue';
import { RouterLink, useRouter } from 'vue-router';
import { api } from '@/api/client';
import type { ChunkMeta, KnowledgeOverview } from '@/api/types';
import {
  loadPapers, paperTitle, reportError, state, toast, withLoading,
} from '@/stores/app';
import { openTab } from '@/stores/tabs';
import { copyText, paperStatus, pct } from '@/utils/format';
import { askConfirm } from '@/stores/confirm';
import { readerTarget } from '@/utils/navigation';
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

/** 同 Menu 组件的 items 结构（结构类型，无需导出）。 */
interface RowAction {
  key: string;
  label: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
}

const router = useRouter();
const overview = ref<KnowledgeOverview | null>(null);
const indexStatus = ref<KnowledgeIndexStatus | null>(null);
const chunks = ref<ChunkMeta[]>([]);
const busyKey = ref('');

// chunk 元数据查询条件（后端要求 paper_id 必填）
const chunkPaperId = ref('');
const chunkType = ref('');
const chunkLimit = ref(50);

// 索引状态：空字符串 = 全部知识库
const statusPaperId = ref('');

/* ------------------------------ 派生值 ------------------------------ */

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

/** 概览里的类型计数压成一行纯文本（四个徽章太吵）。 */
const chunkTypeText = computed(() => CHUNK_TYPES
  .map((t) => `${t.label} ${chunksByType.value[t.key] || 0}`).join(' · '));
/** 语种构成同理：一行文本，不留徽章（`通用` = 语言无关的公式原子行）。 */
const chunkLangText = computed(() => `中文 ${chunksByLang.value.zh || 0} · 原文 ${chunksByLang.value.en || 0}`
  + ` · 通用 ${chunksByLang.value.any || 0}`);

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
  if (key === 'rebuild') { void runIndex(p.id, true); return; }
  if (key === 'remove') void removeFromKb(p);
}

/* ------------------------------ 加载 ------------------------------ */

async function refreshOverview(): Promise<void> {
  await withLoading('setkb-overview', async () => {
    overview.value = await api.get<KnowledgeOverview>('/knowledge/overview');
  });
}

async function loadStatus(): Promise<void> {
  await withLoading('setkb-status', async () => {
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
  await withLoading('setkb-chunks', async () => {
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
    await refreshOverview();
    await bootstrapQueries();
  } catch { /* withLoading 已提示 */ }
}

onMounted(async () => {
  try { await loadPapers(); } catch { /* 论文列表失败不阻塞本面板 */ }
  await refreshAll();
});

/* --------------------------- 移除 / 对账 / 索引 --------------------------- */

/** 从知识库移除：清向量 + 清 chunk 元数据（论文与译文不动）。 */
async function removeFromKb(paper: KbPaper): Promise<void> {
  if (!(await askConfirm({
    title: `从知识库移除《${paper.title}》？`,
    body: `会清掉它的 ${paper.chunks} 条 chunk 与向量（论文、译文、笔记、高亮都不受影响）。`
      + '需要时可以重新「建立索引」加回来。',
    confirmLabel: '移除',
  }))) return;
  busyKey.value = `remove:${paper.id}`;
  try {
    const res = await api.post<{ removed_chunks?: number }>('/knowledge/remove', { paper_id: paper.id });
    toast(`已从知识库移除（清 ${res?.removed_chunks ?? 0} 条 chunk）`, 'ok', 3200);
    await refreshOverview();
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
    await refreshOverview();
  } catch (err) {
    reportError(err, '对账失败');
  } finally {
    busyKey.value = '';
  }
}

async function runIndex(paperId: string, rebuild: boolean): Promise<void> {
  busyKey.value = `idx:${paperId}`;
  try {
    const task = await api.post<{ id?: string } | null>('/knowledge/index', { paper_id: paperId, rebuild });
    toast(`已创建索引任务（${rebuild ? '重建' : '建索引'}）：${task?.id || paperId}`, 'ok');
    await refreshOverview();
    if (statusPaperId.value === paperId || !statusPaperId.value) await loadStatus();
    if (chunkPaperId.value === paperId) await loadChunks();
  } catch (err) {
    reportError(err, rebuild ? '触发重建索引失败' : '触发建索引失败');
  } finally {
    busyKey.value = '';
  }
}

/* ---------------------------- 行内小动作 ---------------------------- */

function copyId(text: string): void {
  copyText(text).then(() => toast('已复制', 'ok', 1600)).catch((err) => reportError(err, '复制失败'));
}

/**
 * 跳回原文：带块锚点 + 页码，阅读器做三级解析（行 id → block_rows → 按页），
 * 见 `utils/navigation.ts`。
 *
 * ⚠️ 必须在点击回调里调（`openTab` 会改响应式状态）：`:to="fn()"` 那种写法会让
 * "渲染 → 改状态 → 再渲染"死循环，知识库页曾因此必定卡死。
 */
function openSource(item: { paper_id?: string | null; block_id?: string | null; page?: number | null }) {
  void router.push(openTab(readerTarget(item.paper_id ?? '', item.block_id, item.page)) as never);
}
</script>

<template>
  <div class="kb-panel">
    <!-- 概览 -->
    <section class="card">
      <div class="card-head">
        <AppIcon name="knowledge" :size="15" />概览
        <span class="spacer"></span>
        <button class="btn icon" title="刷新概览与索引状态" @click="refreshAll"
                :disabled="state.loading['setkb-overview']">
          <AppIcon name="refresh" :size="15" />
        </button>
      </div>
      <div class="card-body">
        <div class="stats" v-if="overview">
          <div class="stat cell">
            <div class="n">{{ overview.papers_total }}</div>
            <div class="k">论文总数</div>
          </div>
          <div class="stat cell">
            <div class="n">{{ overview.papers_in_kb }}</div>
            <div class="k">已纳入知识库（{{ pct(overview.papers_in_kb, overview.papers_total) }}%）</div>
          </div>
          <div class="stat cell">
            <div class="n">{{ overview.chunks }}</div>
            <div class="k">chunk 总数</div>
            <div class="tiny faint">{{ chunkTypeText }}</div>
            <div class="tiny faint">{{ chunkLangText }}</div>
          </div>
          <div class="stat cell">
            <div class="n" :style="vectorAvailable ? '' : 'color:var(--danger)'">
              {{ vectorAvailable ? '可用' : '不可用' }}
            </div>
            <div class="k">向量库</div>
            <div class="tiny faint truncate" :title="String(vectorStore?.error || '')">
              {{ vectorAvailable ? `LanceDB ${vectorStore?.chunks ?? 0} 条` : (vectorStore?.error || '未检测') }}
            </div>
          </div>
        </div>
        <div class="empty" v-else>{{ state.loading['setkb-overview'] ? '正在读取知识库概览…' : '暂无概览数据。' }}</div>
      </div>
    </section>

    <!-- 论文索引表 -->
    <section class="card">
      <div class="card-head">
        <AppIcon name="layers" :size="15" />论文索引
        <span class="badge" v-if="papers.length">{{ papers.length }} 篇</span>
        <span class="spacer"></span>
        <span class="tiny faint">建索引 = 纳入知识库</span>
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
    </section>

    <!-- 索引状态与对账 -->
    <section class="card">
      <div class="card-head">
        <AppIcon name="server" :size="15" />索引状态
        <span class="badge" :class="statusClass">{{ statusText }}</span>
        <span class="spacer"></span>
        <select v-model="statusPaperId" @change="loadStatus" style="min-width:220px">
          <option value="">全部知识库</option>
          <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
        </select>
      </div>
      <div class="card-body">
        <template v-if="indexStatus">
          <div class="stats">
            <div class="stat cell">
              <div class="n">{{ indexStatus.chunks }}</div>
              <div class="k">MySQL chunk 数</div>
            </div>
            <div class="stat cell">
              <div class="n" :style="statusMatch ? '' : 'color:var(--danger)'">{{ vectorCount }}</div>
              <div class="k">LanceDB 条数</div>
            </div>
            <div class="stat cell">
              <div class="n"><span class="badge" :class="statusClass">{{ statusText }}</span></div>
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

          <div class="row wrap tiny meta">
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
               v-if="indexStatus.vector && !indexStatus.vector.available">
            向量库不可用：{{ indexStatus.vector.error || '未返回原因' }}
          </div>
        </template>
        <div class="empty" v-else>{{ state.loading['setkb-status'] ? '正在读取索引状态…' : '暂无索引状态。' }}</div>
      </div>
    </section>

    <!-- chunk 元数据 -->
    <section class="card">
      <div class="card-head">
        <AppIcon name="list" :size="15" />Chunk 元数据
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
                  <span class="badge" :class="{ accent: c.lang === 'zh' }">{{
                    c.lang === 'any' ? '通用' : c.lang === 'zh' ? '中' : '原' }}</span>
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
        <div class="empty" v-else-if="state.loading['setkb-chunks']">正在读取 chunk…</div>
        <div class="empty" v-else-if="!chunkPaperId">请选择论文。</div>
        <div class="empty" v-else>没有符合条件的 chunk（该论文可能尚未建索引）。</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
/**
 * 统计行：**不嵌套卡片**（用户口径：卡片套卡片太吵）。
 * 一格 = 数字 + 说明，格与格之间用竖分隔线，整行都长在同一张卡的 body 里。
 */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.stats .cell {
  padding: 0 0 0 16px;
  border-left: 1px solid var(--divider);
  min-width: 0;
}
.stats .cell:first-child { padding-left: 0; border-left: 0; }
.meta { margin-top: 12px; }

/* 论文索引表：固定列宽，徽章不会被拆成两行 */
.w-paper { width: 300px; }
.w-status { width: 92px; }
.w-kb { width: 88px; }
.w-num { width: 76px; }
.w-ver { width: 84px; }
.w-ops { width: 150px; }
td a.truncate { display: block; }

/* 卡头里的下拉不撑满整行（卡片头是 flex，select 容易被拉长） */
.card-head select { max-width: 300px; }
</style>
