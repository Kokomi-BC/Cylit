<script setup lang="ts">
/**
 * Chunk 元数据（设置页「索引管理」里的可折叠诊断区）。
 *
 * 它是什么：一篇论文被切成的一条条 chunk —— id / 类型 / 页码 / tokens / 预览。
 * 它回答的是"这个索引到底切成了什么"，不是读正文的地方；正文只在「查看」弹窗里出现。
 *
 * 数据从哪来：props 只给论文下拉（id / title / chunks），明细自己取 ——
 * `/knowledge/chunks`（元数据，按后端规划不下发全文）与 `/knowledge/chunk`（单条全文）。
 * 分页走后端 offset / limit，总数用 total；父组件刷新时调 expose 出去的 reload。
 *
 * 为什么这么排：
 * - **常显，不折叠**：这几行不是"翻出来才看"的东西 —— 进到索引管理就该看见这篇论文被切成了
 *   什么。所以卡头只留标题与一句摘要（N 条 · K 类），卡体（筛选 / 表格 / 分页）直接摊开，
 *   没有任何展开 / 收起的入口。
 * - 筛选一行 → 表格一片 → 分页一行：常用的顺序从上到下；表格里**不放正文**，一放正文行高
 *   就散，浏览就从"对比列"变成"读文章"了。
 * - 摘要必须和表格一致：卡头那句话就是"这次查询到底命中多少"的结论，所以它和表格是同一次
 *   请求的结果（挂载即按预填的论文取一次），而不是另算一份。
 */
import { computed, onMounted, ref, watch } from 'vue';
import { api } from '@/api/client';
import type { ChunkMeta } from '@/api/types';
import { reportError, toast } from '@/stores/app';
import { copyText } from '@/utils/format';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';

interface PaperOption { id: string; title: string; chunks: number }

const props = defineProps<{ papers: PaperOption[] }>();

/**
 * `/knowledge/chunk` 的返回形状，对照 backend/services/knowledge/knowledge_service.py 的
 * `chunk_detail`：向量库那条正文 + MySQL 里补齐的 tokens / index_version，另有一批公式条目
 * 专用字段（latex / anchor_* / formula_number …）。
 *
 * 这里**只声明本组件真正渲染的那几项**：后端是动态 JSON，少声明只是不渲染，错声明才是谎。
 * ⚠️ 它**不返回 hash**（`_chunk_row_meta` 查了，但返回值里没带出来），所以弹窗里的 hash
 * 从列表行带入 —— 两个接口说的必须是同一条 chunk，不另编。
 */
interface ChunkDetail {
  chunk_id: string;
  paper_id: string;
  paper_title: string;
  block_id: string | null;
  page: number;
  section: string | null;
  chunk_type: string;
  lang: string;
  content: string;
  chars: number;
  tokens: number | null;
  index_version: number | null;
}

/** 「去原文」交给父组件的最小信息（与既有 openSource 的口径一致：论文 + 块锚点 + 页码）。 */
interface ChunkSource { id: string; paper_id: string; block_id: string; page: number }

/** 列表接口的返回：total 是后端正在并行加的字段，所以三个都按"可能没有"声明。 */
interface ChunkListPayload { items?: ChunkMeta[]; count?: number; total?: number }

const emit = defineEmits<{ (e: 'open-source', chunk: ChunkSource): void }>();

/** 四类 chunk（与后端 knowledge_service.CHUNK_TYPES 同口径）。 */
const CHUNK_TYPES = [
  { key: 'text', label: '正文' },
  { key: 'formula', label: '公式' },
  { key: 'figure', label: '插图' },
  { key: 'table', label: '表格' },
];
/** 每页条数：默认 10（这页是诊断用的明细表，一屏看十行比看五行的翻页更省事）。 */
const PAGE_SIZES = [10, 20, 50, 100];

const paperId = ref('');
const typeKey = ref('');
const keyword = ref('');
const pageSize = ref(PAGE_SIZES[0]);
const offset = ref(0);

const items = ref<ChunkMeta[]>([]);
const total = ref(0);
/** 后端给没给 total：没给就不能承诺"一共几条"，摘要与页脚都退成"本页"口径。 */
const totalKnown = ref(true);
/** 分页是否可用：关键词被后端忽略时过滤只作用于当前页，翻页会翻出未命中的行。 */
const pagingOn = ref(true);
const localFiltered = ref(false);
const loading = ref(false);
const error = ref('');

/** 用户自己动过论文下拉后就不再自动预填 —— 否则选回「选择论文…」会被立刻抢回去。 */
let userPicked = false;

function chunkTypeLabel(key?: string | null): string {
  return CHUNK_TYPES.find((t) => t.key === key)?.label || key || '—';
}

function langLabel(lang?: string | null): string {
  return lang === 'any' ? '通用' : lang === 'zh' ? '中' : '原';
}

function errText(err: unknown): string {
  const msg = (err as { message?: string } | null)?.message;
  return msg || '请求失败';
}

/** 关键词只看 id 与预览文本（与 placeholder 同口径：正文不在列表里，不参与过滤）。 */
function matchesKeyword(row: ChunkMeta, kw: string): boolean {
  const k = kw.toLowerCase();
  if (!k) return true;
  return (row.id || '').toLowerCase().indexOf(k) >= 0
    || (row.preview || '').toLowerCase().indexOf(k) >= 0;
}

/**
 * 取一页列表。
 *
 * `q` / `offset` / `total` 是这一轮**后加的**参数，所以这里留了两个兜底 —— 老部署、或某个环境
 * 还没跟上时，界面照样得说人话，而不是把"没拿到的东西"编出来：
 *
 * 1. `total` 缺失 → 只能按"本页读到的范围"兜（offset + 本页条数），**绝不**按 limit 去猜还有几页；
 *    摘要与页脚同时退成"本页"口径，并明说后端没给 total。列表里的 `count` 不能拿来兜 ——
 *    后端的 count 就是本页条数（= items.length），拿它当总数等于谎报"这篇只有一页"。
 * 2. `q` 被忽略 → 本页再过滤一遍。后端的 q 是"id 或 content_preview 的大小写不敏感子串"，
 *    与下面 matchesKeyword 同口径，所以"有没有被忽略"靠返回行自己说话（全命中就是有生效）。
 *    真被忽略时后端仍会把未命中的行算进 total，页码与条数就会互相矛盾，
 *    所以此时**关掉分页**（只在当前页过滤，并写清这一点），而不是假装分页还准。
 */
async function load(nextOffset = offset.value, retried = false): Promise<void> {
  if (!paperId.value) {
    items.value = [];
    total.value = 0;
    error.value = '';
    loading.value = false;
    return;
  }
  loading.value = true;
  error.value = '';
  const kw = keyword.value.trim();
  try {
    const data = await api.get<ChunkListPayload>('/knowledge/chunks', {
      paper_id: paperId.value,
      chunk_type: typeKey.value || undefined,
      q: kw || undefined,
      offset: nextOffset,
      limit: pageSize.value,
    });
    const raw = data?.items || [];
    const rawTotal = data?.total;
    const serverTotal = typeof rawTotal === 'number' && Number.isFinite(rawTotal) ? rawTotal : null;
    const ignored = kw.length > 0 && raw.length > 0 && !raw.every((r) => matchesKeyword(r, kw));
    const rows = ignored ? raw.filter((r) => matchesKeyword(r, kw)) : raw;

    items.value = rows;
    total.value = ignored ? rows.length : (serverTotal ?? nextOffset + raw.length);
    totalKnown.value = serverTotal !== null && !ignored;
    localFiltered.value = ignored;
    pagingOn.value = !ignored;
    offset.value = nextOffset;

    // 数据变少了（换了筛选 / 别处删了 chunk）之后当前页可能已经空了 —— 退回第一页，不留在空页上。
    if (!rows.length && nextOffset > 0 && !retried) await load(0, true);
  } catch (err) {
    items.value = [];
    total.value = 0;
    error.value = errText(err);
  } finally {
    loading.value = false;
  }
}

function reload(): void {
  void load(offset.value);
}

function applyFilters(): void {
  void load(0);
}

function onPaper(): void {
  userPicked = true;
  void load(0);
}

function onPageSize(): void {
  void load(0);
}

const page = computed(() => Math.floor(offset.value / pageSize.value) + 1);
const pages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
const typeCount = computed(() => new Set(items.value.map((r) => r.chunk_type)).size);

/** 页码条：> 7 页折叠成 首、…、当前 ±1、…、尾；0 表示省略号（模板里画三个点）。 */
const pageItems = computed<number[]>(() => {
  const n = pages.value;
  const cur = page.value;
  if (n <= 7) return Array.from({ length: n }, (_, i) => i + 1);
  const out: number[] = [1];
  const from = Math.max(2, cur - 1);
  const to = Math.min(n - 1, cur + 1);
  if (from > 2) out.push(0);
  for (let i = from; i <= to; i += 1) out.push(i);
  if (to < n - 1) out.push(0);
  out.push(n);
  return out;
});

const headSummary = computed<string>(() => {
  if (loading.value) return '读取中…';
  if (error.value) return '读取失败';
  if (!paperId.value) return '未选择论文';
  const known = totalKnown.value && pagingOn.value;
  const scope = known ? '' : (localFiltered.value ? '本页命中 ' : '本页 ');
  const n = known ? total.value : items.value.length;
  return scope + n + ' 条' + (typeCount.value ? ' · ' + typeCount.value + ' 类' : '');
});

function goPage(n: number): void {
  if (n < 1 || n > pages.value || n === page.value) return;
  void load((n - 1) * pageSize.value);
}

/** 单元格放不下完整 id：title 补上 id + hash + 索引版本（诊断用的三件套）。 */
function idTitle(row: ChunkMeta): string {
  return row.id + '\n' + 'hash ' + (row.hash || '—') + '\n' + '索引版本 ' + (row.index_version ?? '—');
}

function copyId(text?: string | null): void {
  const value = String(text || '');
  if (!value) return;
  copyText(value).then(() => toast('已复制', 'ok', 1600)).catch((err) => reportError(err, '复制失败'));
}

/* --------------------------------- 详情 --------------------------------- */

const detailOpen = ref(false);
const detailLoading = ref(false);
const detailError = ref('');
const detail = ref<ChunkDetail | null>(null);
/** 点「查看」时那一行列表数据：正文还没回来、或正文接口失败时，来源信息仍然完整可读。 */
const detailRow = ref<ChunkMeta | null>(null);

const detailTitle = computed<string>(() => {
  const t = detail.value?.chunk_type || detailRow.value?.chunk_type;
  return t ? 'Chunk 正文 · ' + chunkTypeLabel(t) : 'Chunk 正文';
});
const detailId = computed<string>(() => detail.value?.chunk_id || detailRow.value?.id || '');
const sourceChunk = computed<ChunkSource | null>(() => {
  const d = detail.value;
  if (d) {
    return { id: d.chunk_id, paper_id: d.paper_id, block_id: d.block_id || '', page: Number(d.page) || 0 };
  }
  const r = detailRow.value;
  if (r) return { id: r.id, paper_id: r.paper_id, block_id: r.block_id, page: Number(r.page) || 0 };
  return null;
});

async function fetchDetail(chunkId: string): Promise<void> {
  if (!chunkId) return;
  detailLoading.value = true;
  detailError.value = '';
  try {
    detail.value = await api.get<ChunkDetail>('/knowledge/chunk', { chunk_id: chunkId });
  } catch (err) {
    detail.value = null;
    detailError.value = errText(err);
  } finally {
    detailLoading.value = false;
  }
}

function openDetail(row: ChunkMeta): void {
  detailRow.value = row;
  detail.value = null;
  detailError.value = '';
  detailOpen.value = true;
  void fetchDetail(row.id);
}

function closeDetail(): void {
  detailOpen.value = false;
}

/** 路由由父组件跳（与既有 openSource 同一个动作），这里只把"哪一条"交出去。 */
function goSource(): void {
  const src = sourceChunk.value;
  if (!src) return;
  emit('open-source', src);
  detailOpen.value = false;
}

/* -------------------------------- 预填 / 刷新 -------------------------------- */

/** 挂载时挑第一篇「有 chunk」的论文预填（与既有行为一致），没有就停在"请选择论文"。 */
function pickDefault(list: PaperOption[]): void {
  const first = list.find((p) => Number(p.chunks) > 0);
  if (!first) return;
  paperId.value = first.id;
  void load(0);
}

onMounted(() => {
  pickDefault(Array.isArray(props.papers) ? props.papers : []);
});

// 论文列表在父组件里是异步取的：晚到了再预填一次；用户动过下拉就不再插手。
watch(() => props.papers, (list) => {
  if (userPicked || paperId.value) return;
  pickDefault(Array.isArray(list) ? list : []);
});

defineExpose({ reload });
</script>

<template>
  <div class="card chunk-card">
    <!-- 单根节点，而且模板里**不能有兄弟注释**：Vue 的根节点就是这层 div，父组件给的 class
         （面板的 .kb-chunk = 单列 order:4）才能落下来。之前这里是 "section + Modal 的 Teleport"
         两根，class 继承不了，chunk 卡拿到默认 order 就跑到最前面去了。
         Modal 自己 Teleport 到 body，留在根内不影响它挂到哪 —— 但不要把它移到 div 外面。 -->
    <!-- 卡头不是开关：这张卡常显，卡体（筛选 / 表格 / 分页）直接摊开，不提供展开 / 收起入口。
         右侧那句摘要是这次查询的真实结论（N 条 · K 类），与下面的表格同源。 -->
    <div class="card-head chunk-head">
      <AppIcon name="list" :size="15" />
      <span class="nowrap">Chunk 元数据</span>
      <span class="spacer"></span>
      <span class="tiny faint head-sum">{{ headSummary }}</span>
    </div>

    <div class="card-body tight">
      <!-- 筛选行：论文 → 类型 → 关键词 → 每页条数，从粗到细 -->
      <div class="filters">
        <select v-model="paperId" class="f-paper" aria-label="论文" @change="onPaper">
          <option value="">选择论文…</option>
          <option v-for="p in papers" :key="p.id" :value="p.id">
            {{ p.chunks ? p.title + '（' + p.chunks + ' 条）' : p.title }}
          </option>
        </select>
        <select v-model="typeKey" aria-label="类型" @change="applyFilters">
          <option value="">全部类型</option>
          <option v-for="t in CHUNK_TYPES" :key="t.key" :value="t.key">{{ t.label }}</option>
        </select>
        <label class="search-field">
          <AppIcon name="search" :size="14" />
          <input
            v-model="keyword" type="search" placeholder="搜索 chunk id / 预览文本"
            aria-label="搜索 chunk" @change="applyFilters" @keydown.enter.prevent="applyFilters"
          />
        </label>
        <select v-model.number="pageSize" aria-label="每页条数" @change="onPageSize">
          <option v-for="n in PAGE_SIZES" :key="n" :value="n">{{ n }} 条/页</option>
        </select>
      </div>

      <div v-if="loading" class="empty-state">
        <svg class="spinner" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /></svg>
        <div class="es-hint">正在读取 chunk…</div>
      </div>

      <div v-else-if="error" class="empty-state">
        <div class="es-icon is-error"><AppIcon name="error" :size="20" /></div>
        <div class="es-title">读取失败</div>
        <div class="es-hint">{{ error }}</div>
        <div class="es-actions">
          <button class="btn sm" @click="reload"><AppIcon name="refresh" :size="13" />重试</button>
        </div>
      </div>

      <div v-else-if="!paperId" class="empty-state">
        <div class="es-icon"><AppIcon name="list" :size="20" /></div>
        <div class="es-title">请先选择论文</div>
        <div class="es-hint">chunk 按论文查询：在上面挑一篇，就能看到它被切成了哪些块。</div>
      </div>

      <div v-else-if="!items.length" class="empty-state">
        <div class="es-icon"><AppIcon name="search" :size="20" /></div>
        <div class="es-title">没有符合条件的 chunk</div>
        <div class="es-hint">这篇论文可能尚未建索引，也可能是关键词 / 类型筛选把结果排空了。</div>
      </div>

      <template v-else>
        <div class="tbl-wrap">
          <table class="data fixed chunk-table">
            <thead>
              <tr>
                <th class="w-id">chunk id</th>
                <th class="w-type">类型</th>
                <th class="num w-page">页</th>
                <th class="num w-tokens">tokens</th>
                <th class="col-prev w-prev">预览</th>
                <th class="w-ops">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in items" :key="c.id">
                <td>
                  <div class="row nowrap id-cell">
                    <span class="mono tiny truncate cell-id" :title="idTitle(c)">{{ c.id }}</span>
                    <button class="btn ghost sm icon" title="复制 chunk id" @click="copyId(c.id)">
                      <AppIcon name="copy" :size="13" />
                    </button>
                  </div>
                </td>
                <td>
                  <div class="row nowrap type-cell">
                    <span class="badge accent">{{ chunkTypeLabel(c.chunk_type) }}</span>
                    <span class="badge dim">{{ langLabel(c.lang) }}</span>
                  </div>
                </td>
                <td class="num">{{ c.page }}</td>
                <td class="num">{{ c.tokens }}</td>
                <td class="col-prev">
                  <div class="small clamp-2 cell-prev" :title="c.preview || ''">{{ c.preview || '—' }}</div>
                </td>
                <td class="ops">
                  <button class="btn sm" title="查看完整正文与来源信息" @click="openDetail(c)">
                    <AppIcon name="document" :size="13" />查看
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="!totalKnown" class="tiny faint note">
          后端未返回 total：条数按本页已读到的范围显示，暂不分页。
        </div>
        <div v-else-if="!pagingOn" class="tiny faint note">
          后端尚未支持关键词过滤，已在前端对当前页再筛了一遍（因此不分页）。
        </div>
        <div v-else-if="pages > 1" class="pager">
          <button
            class="btn sm ghost icon" :disabled="page === 1" title="上一页" aria-label="上一页"
            @click="goPage(page - 1)"
          >
            <AppIcon name="chevron-left" :size="14" />
          </button>
          <template v-for="(p, i) in pageItems" :key="i">
            <span v-if="p === 0" class="tiny faint pager-gap">…</span>
            <button
              v-else class="btn sm pager-num" :class="{ on: p === page }"
              :aria-current="p === page ? 'page' : undefined" @click="goPage(p)"
            >{{ p }}</button>
          </template>
          <button
            class="btn sm ghost icon" :disabled="page === pages" title="下一页" aria-label="下一页"
            @click="goPage(page + 1)"
          >
            <AppIcon name="chevron-right" :size="14" />
          </button>
          <span class="spacer"></span>
          <span class="tiny faint nowrap">第 {{ page }} / {{ pages }} 页 · 共 {{ total }} 条</span>
        </div>
      </template>
    </div>

    <!-- 详情：正文与来源信息（路由跳转交给父组件） -->
    <Modal :open="detailOpen" :title="detailTitle" @close="closeDetail">
      <div v-if="detailLoading" class="detail-loading">
        <svg class="spinner" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /></svg>
        <span class="small faint">正在读取正文…</span>
      </div>

      <div v-else-if="detailError" class="detail-fail">
        <div class="small detail-err">{{ detailError }}</div>
        <button class="btn sm" @click="fetchDetail(detailId)">
          <AppIcon name="refresh" :size="13" />重试
        </button>
      </div>

      <template v-else>
        <div class="detail-text small">{{ detail?.content || '（向量库里这条没有正文）' }}</div>
        <div class="meta">
          <div class="meta-row"><span class="meta-k">论文</span><span class="meta-v">{{ detail?.paper_title || '—' }}</span></div>
          <div class="meta-row"><span class="meta-k">页码</span><span class="meta-v tnum">{{ detail ? detail.page : '—' }}</span></div>
          <div class="meta-row"><span class="meta-k">章节</span><span class="meta-v">{{ detail?.section || '—' }}</span></div>
          <div class="meta-row"><span class="meta-k">类型</span><span class="meta-v">{{ chunkTypeLabel(detail?.chunk_type || detailRow?.chunk_type) }}</span></div>
          <div class="meta-row"><span class="meta-k">语言</span><span class="meta-v">{{ langLabel(detail?.lang || detailRow?.lang) }}</span></div>
          <div class="meta-row"><span class="meta-k">tokens</span><span class="meta-v tnum">{{ detail ? (detail.tokens ?? '—') : (detailRow?.tokens ?? '—') }}</span></div>
          <div class="meta-row"><span class="meta-k">hash</span><span class="meta-v mono tiny">{{ detailRow?.hash || '—' }}</span></div>
          <div class="meta-row">
            <span class="meta-k">索引版本</span>
            <span class="meta-v tnum">{{ detail?.index_version ?? detailRow?.index_version ?? '—' }}</span>
          </div>
          <div class="meta-row"><span class="meta-k">id</span><span class="meta-v mono tiny break">{{ detailId || '—' }}</span></div>
          <div class="meta-row">
            <span class="meta-k">字符数</span>
            <span class="meta-v tnum">{{ detail ? detail.chars : '—' }}</span>
          </div>
        </div>
      </template>

      <template #footer>
        <button class="btn sm" :disabled="!detailId" @click="copyId(detailId)">
          <AppIcon name="copy" :size="13" />复制 ID
        </button>
        <button class="btn sm primary" :disabled="!sourceChunk" @click="goSource">
          <AppIcon name="locate" :size="13" />去原文
        </button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
/* 常显卡：卡头就是一句标题 + 一句摘要（不再是可以点的按钮，所以没有 hover / focus 态）。
   下面的 .card-body.tight 是全局类，这里只补"摘要不要撑破卡头"那一笔。 */
.card.chunk-card { min-width: 0; }
.card-head.chunk-head .icon { color: var(--text-faint); }
.head-sum { min-width: 0; overflow: hidden; text-overflow: ellipsis; }

/* 筛选行：能并排就并排，放不下就折行（每个控件自己给最小宽度，绝不撑破卡片） */
.filters {
  display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-2);
  padding: 0 4px 10px;
}
.filters > select { max-width: 200px; min-width: 0; }
.filters > select.f-paper { max-width: 300px; }
.filters .search-field { flex: 1 1 200px; min-width: 0; max-width: 300px; }
.filters .search-field input { width: 100%; min-width: 0; }

/* 表格：固定列宽 + 宽度不够时**卡片内部**横向滚动（页面本身永不横向溢出） */
.tbl-wrap { overflow-x: auto; border-radius: var(--r-sm); }
.chunk-table { min-width: 680px; }
.chunk-table th { white-space: nowrap; }
/* 与论文表同一档行高：全局 8/11 → 6/10（表头同一套），字号仍走全局 13px / 12px */
.chunk-table th,
.chunk-table td { padding: 6px 10px; }
/* 徽章高度收一档（22 → 20），文字仍是 11.5px */
.chunk-table .badge { height: 20px; padding: 0 9px; }
.w-id { width: 292px; }
.w-type { width: 132px; }
.w-page { width: 60px; }
.w-tokens { width: 76px; }
.w-ops { width: 88px; }
.row.id-cell, .row.type-cell { gap: 6px; }
.cell-id { display: block; min-width: 0; max-width: 240px; }
.cell-prev { max-width: 340px; }
td.ops { vertical-align: middle; }

.note { padding: 6px 4px 2px; line-height: 1.5; }

/* 分页：数字等宽，页码用 pill（选中态借 .btn.on 的强调浅底） */
.pager { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; padding: 8px 4px 2px; }
.pager-num {
  min-width: 30px; padding: 0 8px;
  font-variant-numeric: tabular-nums; color: var(--text-dim);
}
.pager-gap { padding: 0 2px; }

/* 详情弹窗 */
.detail-loading { display: flex; align-items: center; gap: 10px; padding: 6px 0 10px; }
.detail-fail { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; padding: 4px 0 8px; }
.detail-err { color: var(--danger); line-height: 1.55; overflow-wrap: anywhere; }
.detail-text {
  white-space: pre-wrap; overflow-wrap: anywhere;
  max-height: 46vh; overflow: auto;
  padding: 12px 14px; border-radius: var(--r-sm);
  background: var(--bg-sunken); border: 1px solid var(--border);
  line-height: 1.65; color: var(--text);
}
.meta {
  display: grid; grid-template-columns: 76px minmax(0, 1fr);
  gap: 6px 12px; align-items: start; margin-top: 14px;
}
.meta-row { display: contents; }
.meta-k { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.meta-v {
  min-width: 0; font-size: calc(12.5px * var(--ts, 1));
  color: var(--text-dim); overflow-wrap: anywhere;
}
.meta-v.tnum { font-variant-numeric: tabular-nums; }
.meta-v.break { word-break: break-all; }

/* 错误态的空状态图标：用危险色，与"没有数据"区分开 */
.empty-state .es-icon.is-error {
  background: var(--danger-soft); color: var(--danger); border-color: transparent;
}

@media (max-width: 1024px) {
  .w-id { width: 260px; }
  .cell-id { max-width: 208px; }
}

@media (max-width: 860px) {
  .chunk-table { min-width: 520px; }
  .w-id { width: 216px; }
  .cell-id { max-width: 168px; }
  .cell-prev { max-width: 220px; }
}

@media (max-width: 560px) {
  /* 窄屏撤掉预览列（最宽、也最次要）：正文仍在「查看」里，不必靠横向滚动去读一行字 */
  .chunk-table .col-prev { display: none; }
  .chunk-table { min-width: 0; }
  .w-id { width: 150px; }
  .cell-id { max-width: 110px; }
  .filters > select { flex: 1 1 140px; max-width: none; }
  .filters .search-field { flex: 1 1 100%; max-width: none; }
  .meta { grid-template-columns: 64px minmax(0, 1fr); }
}
</style>
