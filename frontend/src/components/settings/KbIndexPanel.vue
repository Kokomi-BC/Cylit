<script setup lang="ts">
/**
 * 设置页「索引管理」的**编排层**：只负责共享取数、跨卡片动作与版式，内容全部交给子组件。
 *
 * 分工：
 * - 自己取两块**共享**数据 —— `GET /knowledge/overview`（论文 / chunk / 向量模型）与
 *   `GET /knowledge/index/status`（全库对账、维数）。这两份是上面指标行与论文表
 *   共同的输入，取一次比两处各取一次更不可能出现"两张表说两个数"。
 * - 两个写操作（建/重建索引、从知识库移除）也留在这里：只有这一层同时拿得到
 *   overview 与 status，改完能一次刷新两块；子组件只管把点击原样发上来，不发请求。
 *   「索引存储状态」卡（含校验 / 清理差异）**已搬到「系统信息」**，这里不再有那份 UI。
 * - 趋势图、chunk 明细自己取数（各自的窗口参数 / 分页只有它们自己知道），检索配置
 *   自己读写 `/settings` —— 那三块与本层零耦合，所以这里连它们的字段都不声明。
 *
 * 一致性的判据只有 `status.consistent`（后端按 id 集合对账）。**绝不用 chunks === vector.count**
 * 代替：换 embedding 模型时向量表会整体重建，旧元数据还留在 MySQL 里，两侧条数完全可能
 * 相等却并不一致。`consistent` 为 null（还没取到）时只能说"未知"，不猜。
 *
 * 列数用 **container query** 而不是 media query：这块挂在设置页右栏里，窗格宽度 ≠ 视口宽度，
 * 用视口判断会在侧栏收起时排错。断点只有 1040（主体两栏）、900 / 640（图表行三档）、
 * 860 / 560（指标行）这几处。
 */
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { api } from '@/api/client';
import type { KnowledgeOverview } from '@/api/types';
import { isLoading, reportError, toast, withLoading } from '@/stores/app';
import { askConfirm, type ConfirmOptions } from '@/stores/confirm';
import { openTab } from '@/stores/tabs';
import { readerTarget } from '@/utils/navigation';
import AppIcon from '@/components/AppIcon.vue';
import KbTrendCard from './KbTrendCard.vue';
import KbChunkMixCard from './KbChunkMixCard.vue';
import KbLatencyCard from './KbLatencyCard.vue';
import PaperIndexTable from './PaperIndexTable.vue';
import ChunkMetaCard from './ChunkMetaCard.vue';
import RagConfigCard from './RagConfigCard.vue';

/**
 * 指标行副行（"chunk 总数"卡里那串）只列四类内容 chunk：这一格在 313px 宽里
 * 连四类都是刚好塞满，加第五类会把它截成"表格 1…"。分布卡不靠这张表取数（见 typeCounts）。
 */
const CHUNK_TYPES = [
  { key: 'text', label: '正文' },
  { key: 'formula', label: '公式' },
  { key: 'figure', label: '插图' },
  { key: 'table', label: '表格' },
];

type KbPaper = KnowledgeOverview['items'][number];

/** `embedding_models` 的一条：后端 `_model_labels` 除 label 外还给了 model_name 与 dim。 */
interface EmbeddingModel { id: string; label: string; model_name?: string | null; dim?: number | null }

/** 概览响应在本层的视角：只把 `embedding_models` 收窄成带 dim 的条目。 */
interface KbOverview extends KnowledgeOverview { embedding_models: EmbeddingModel[] }

/** 向量库状态（`index_status.vector`）。 */
interface VectorStatus { available: boolean; count?: number | null; error?: string | null }

/** `GET /knowledge/index/status`（不带 paper_id，即全库）里本层用到的字段。 */
interface IndexStatus {
  chunks: number;
  chunks_with_hash: number;
  index_versions: Record<string, number>;
  vector: VectorStatus | null;
  model_dim?: number | null;
  store_dim?: number | null;
  dim_match?: boolean | null;
  orphan_chunks?: number | null;
  missing_vectors?: number | null;
  consistent?: boolean | null;
}

const router = useRouter();
const overview = ref<KbOverview | null>(null);
const indexStatus = ref<IndexStatus | null>(null);
/** 最近一次**刷新**的时间（本地时钟），工具条展示用。 */
const refreshedAt = ref('');
/** 行级动作键：`idx:<paperId>` / `remove:<paperId>`。 */
const busyKey = ref('');
/** 整页刷新进行中（含两张自取数卡的重载，它们不占 state.loading）。 */
const refreshing = ref(false);

const trendCard = ref<InstanceType<typeof KbTrendCard> | null>(null);
const chunkCard = ref<InstanceType<typeof ChunkMetaCard> | null>(null);
const latencyCard = ref<InstanceType<typeof KbLatencyCard> | null>(null);

const overviewLoading = computed<boolean>(() => isLoading('setkb-overview'));
const statusLoading = computed<boolean>(() => isLoading('setkb-status'));
const refreshBusy = computed<boolean>(
  () => refreshing.value || overviewLoading.value || statusLoading.value || !!busyKey.value,
);

/* ------------------------------ 指标行派生 ------------------------------ */

const papers = computed<KbPaper[]>(() => overview.value?.items || []);
const versions = computed<Record<string, number>>(() => overview.value?.index_versions || {});
/** chunk 卡只要三列（它的下拉与摘要都只用这三项）。 */
const chunkPapers = computed(() => papers.value.map((p) => ({ id: p.id, title: p.title, chunks: p.chunks })));

const chunkTypeText = computed<string>(() => CHUNK_TYPES
  .map((t) => {
    const n = overview.value?.chunks_by_type?.[t.key];
    return `${t.label} ${Number(n) || 0}`;
  })
  .join(' · '));

/**
 * Chunk 类型分布卡要的是"按类型分开的条数"，不是拼成一句话的那串。
 * **原样透传整份 chunks_by_type**：早先只挑四类，unit（检索单元）被丢掉，
 * 于是卡上出现"中心 814、图例相加 428"的自相矛盾 —— 面板不再替它做减法，
 * 顺序与配色交给卡片（未知键它会自己决定放哪）。
 */
const typeCounts = computed<Record<string, number>>(() => {
  const src = overview.value?.chunks_by_type || {};
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(src)) out[k] = Math.max(0, Number(v) || 0);
  return out;
});

/** 判定只用 status.consistent：没取到 → null（未知），取到布尔就照抄。 */
const consistency = computed<boolean | null>(() => {
  const s = indexStatus.value;
  return s && typeof s.consistent === 'boolean' ? s.consistent : null;
});
const consistencyText = computed<string>(
  () => (consistency.value === null ? '未知' : consistency.value ? '一致' : '不一致'),
);
const consistencyClass = computed<string>(
  () => (consistency.value === null ? 'dim' : consistency.value ? 'ok' : 'danger'),
);
/** 只有真正"一致"才给对勾；未知与不一致都值得先看一眼。 */
const consistencyIcon = computed<string>(() => (consistency.value === true ? 'check' : 'warning'));

/** 后端 label 形如 `Qwen3-VL（2560 维）`：名字当主值、维数进副行，所以在这里拆开。 */
function splitLabel(label?: string | null): { name: string; dim: number | null } {
  const text = String(label ?? '').trim();
  const m = text.match(/^(.+?)（\s*(\d+)\s*维\s*）$/);
  return m ? { name: m[1].trim(), dim: Number(m[2]) } : { name: text, dim: null };
}

const embeddingModel = computed<EmbeddingModel | null>(
  () => (overview.value?.embedding_models || [])[0] || null,
);
const embeddingName = computed<string>(() => {
  const m = embeddingModel.value;
  if (!m) return '未配置';
  return splitLabel(m.label).name || m.id;
});
const embeddingDim = computed<number | null>(() => {
  const m = embeddingModel.value;
  if (!m) return null;
  return m.dim || splitLabel(m.label).dim || null;
});
const embeddingSub = computed<string>(() => {
  if (!embeddingModel.value) return '去「模型」页绑定';
  return embeddingDim.value ? `${embeddingDim.value} 维 embedding` : '维数未声明';
});

/* -------------------------------- 取数 -------------------------------- */

/** 本地时钟 HH:MM:SS（不做时区换算：这是"刚才几点刷的"，不是服务端时间戳）。 */
function clockNow(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 两块共享数据各自 try/catch：一块失败不能把另一块连带打掉（withLoading 已 reportError）。 */
async function loadOverview(): Promise<void> {
  try {
    await withLoading('setkb-overview', async () => {
      overview.value = await api.get<KbOverview>('/knowledge/overview');
    });
  } catch { /* 已提示；status 与子组件照常展示 */ }
}

async function loadStatus(): Promise<void> {
  try {
    await withLoading('setkb-status', async () => {
      indexStatus.value = await api.get<IndexStatus>('/knowledge/index/status');
      refreshedAt.value = clockNow();
    });
  } catch { /* 同上 */ }
}

/** 刷新：两块共享数据 + 三张自取数卡（趋势 / chunk 摘要 / 检索耗时），互不阻塞，谁失败谁自己提示。 */
async function refreshAll(): Promise<void> {
  if (refreshBusy.value) return;
  refreshing.value = true;
  try {
    await Promise.allSettled([
      loadOverview(),
      loadStatus(),
      Promise.resolve(trendCard.value?.reload()),
      Promise.resolve(chunkCard.value?.reload()),
      Promise.resolve(latencyCard.value?.reload()),
    ]);
  } finally {
    refreshing.value = false;
  }
}

onMounted(() => {
  void Promise.allSettled([loadOverview(), loadStatus()]);
});

/* ------------------------------- 写操作 ------------------------------- */

/**
 * 写索引前要问的话：建索引 / 重建索引两支共用一份结构，只有文案不同 ——
 * 两个动作最终都会 POST /knowledge/index 往向量库里写东西，所以都要过一次确认。
 */
function indexConfirmOptions(paper: KbPaper, rebuild: boolean): ConfirmOptions {
  return rebuild
    ? {
      title: `重建索引《${paper.title}》？`,
      body: '会重新切块并重写它的向量（旧向量被替换），过程中这一篇可能检索不到。'
        + '已经生成的中文译文与笔记不受影响。',
      confirmLabel: '重建索引',
      danger: false,
    }
    : {
      title: `为《${paper.title}》建索引？`,
      body: '会切块并写入向量库，建完这篇就能被检索与问答引用。'
        + '已有译文与笔记不受影响。',
      confirmLabel: '建索引',
      danger: false,
    };
}

/**
 * 建索引 / 重建索引：先确认，再 POST 一个任务，然后把两块共享数据刷新（任务排上了，数字可能已变）。
 *
 * 确认框按"移除"那一支的做法走 askConfirm：取消就整支退出 —— 不发请求、不置 busyKey，
 * 按钮不会闪一下 loading。
 */
async function runIndex(paper: KbPaper, rebuild: boolean): Promise<void> {
  const ok = await askConfirm(indexConfirmOptions(paper, rebuild));
  if (!ok) return;
  busyKey.value = `idx:${paper.id}`;
  try {
    const task = await api.post<{ id?: string } | null>('/knowledge/index', {
      paper_id: paper.id, rebuild,
    });
    toast(`已创建索引任务（${rebuild ? '重建' : '建索引'}）：${task?.id || paper.id}`, 'ok');
    await Promise.allSettled([loadOverview(), loadStatus()]);
  } catch (err) {
    reportError(err, rebuild ? '触发重建索引失败' : '触发建索引失败');
  } finally {
    busyKey.value = '';
  }
}

/** 从知识库移除：只清 chunk 与向量，论文 / 译文 / 笔记 / 高亮都不动。 */
async function removeFromKb(paper: KbPaper): Promise<void> {
  const ok = await askConfirm({
    title: `从知识库移除《${paper.title}》？`,
    body: `会清掉它的 ${paper.chunks} 条 chunk 与向量（论文、译文、笔记、高亮都不受影响）。`
      + '需要时可以重新「建索引」加回来。',
    confirmLabel: '移除',
    danger: true,
  });
  if (!ok) return;
  busyKey.value = `remove:${paper.id}`;
  try {
    const res = await api.post<{ removed_chunks?: number }>('/knowledge/remove', { paper_id: paper.id });
    toast(`已从知识库移除（清 ${res?.removed_chunks ?? 0} 条 chunk）`, 'ok', 3200);
    await Promise.allSettled([loadOverview(), loadStatus()]);
  } catch (err) {
    reportError(err, '移除失败');
  } finally {
    busyKey.value = '';
  }
}

/* ------------------------------- 跳转 ------------------------------- */

/**
 * ⚠️ `openTab` 会改响应式状态，**只能在点击回调里调**；写成 `:to="fn()"` 就是
 * "渲染 → 改状态 → 再渲染"的死循环（知识库页已经这么炸过一次）。
 */
function openPaper(paper: KbPaper): void {
  void router.push(openTab(readerTarget(paper.id)));
}

/** chunk 卡发上来的最小信息与 readerTarget 的三参数一一对应（论文 + 块锚点 + 页码）。 */
function openSource(item: { paper_id?: string | null; block_id?: string | null; page?: number | null }): void {
  void router.push(openTab(readerTarget(item.paper_id ?? '', item.block_id, item.page)));
}
</script>

<template>
  <div class="kb-panel">
    <!-- ① 工具条：只有一个刷新（业务入口不在这里，设置页不是内容页） -->
    <div class="kb-bar">
      <span class="tiny faint nowrap">上次刷新 {{ refreshedAt || '—' }}</span>
      <button class="btn sm ghost" :disabled="refreshBusy" title="刷新概览、索引状态、趋势与 chunk 摘要"
              @click="refreshAll">
        <AppIcon name="refresh" :size="14" />刷新
      </button>
    </div>

    <!-- ② 指标带：四格并排、格间一条竖发丝线；整条是垫在内容下面的淡色圆角卡（--bg-sunken + --r-sm），
         不是白卡 —— 无描边、无阴影、无 --bg-elev 白底 -->
    <div class="kb-metrics">
      <div class="kb-metric">
        <div class="m-head">
          <span class="m-icon"><AppIcon name="knowledge" :size="15" /></span>
          <span class="m-label">论文总数</span>
        </div>
        <div class="m-value">{{ overview ? overview.papers_total : '—' }}</div>
        <div class="m-sub truncate">已纳入知识库 {{ overview ? overview.papers_in_kb : 0 }} 篇</div>
      </div>

      <div class="kb-metric">
        <div class="m-head">
          <span class="m-icon"><AppIcon name="layers" :size="15" /></span>
          <span class="m-label">chunk 总数</span>
        </div>
        <div class="m-value">{{ overview ? overview.chunks : '—' }}</div>
        <div class="m-sub truncate" :title="chunkTypeText">{{ chunkTypeText }}</div>
      </div>

      <div class="kb-metric">
        <div class="m-head">
          <span class="m-icon"><AppIcon :name="consistencyIcon" :size="15" /></span>
          <span class="m-label">索引一致性</span>
        </div>
        <div class="m-value">
          <span class="badge" :class="consistencyClass">{{ consistencyText }}</span>
        </div>
        <div class="m-sub truncate">MySQL 与 LanceDB 按 id 集合对账</div>
      </div>

      <div class="kb-metric">
        <div class="m-head">
          <span class="m-icon"><AppIcon name="model" :size="15" /></span>
          <span class="m-label">向量模型</span>
        </div>
        <div class="m-value txt truncate" :title="embeddingName">{{ embeddingName }}</div>
        <div class="m-sub truncate">{{ embeddingSub }}</div>
      </div>
    </div>

    <!-- ③ 图表行：一排三张小图（趋势 → 类型分布 → 耗时），宽度不够就降到两列 / 一列 -->
    <div class="kb-charts">
      <KbTrendCard ref="trendCard" />
      <KbChunkMixCard :type-counts="typeCounts" :total="overview ? overview.chunks : 0"
                      :loading="overviewLoading" />
      <KbLatencyCard ref="latencyCard" />
    </div>

    <!-- ④ 主体：≥1040 两栏（右栏粘住），否则单列且顺序为 论文表 → 检索配置 → chunk -->
    <div class="kb-main">
      <div class="kb-col kb-left">
        <PaperIndexTable class="kb-paper" :items="papers" :versions="versions"
                         :loading="overviewLoading" :busy-key="busyKey"
                         @index="runIndex($event, false)" @rebuild="runIndex($event, true)"
                         @remove="removeFromKb" @open="openPaper" />
        <ChunkMetaCard ref="chunkCard" class="kb-chunk" :papers="chunkPapers"
                       @open-source="openSource" />
      </div>
      <div class="kb-col kb-right">
        <RagConfigCard class="kb-rag" />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 容器查询的锚点：这块的可用宽度由父级右栏决定，和视口无关。 */
.kb-panel {
  container-type: inline-size;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

/* 整块面板只认一个纵向节奏（10px）：卡间距、指标行、图卡行、主体两栏、右栏堆叠全用同一个数。
   子组件的卡头内边距也在这里统一收一档 —— 用 :deep 限定在 .kb-panel 以内，不动全局 .card-head。 */
.kb-panel :deep(.card-head) { padding: 9px 14px; }

/* ① 工具条：右对齐，不占一整行高度 */
.kb-bar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  min-width: 0;
}

/* ② 指标带 = 一条淡色圆角卡（--bg-sunken + --r-sm），四格的数值都在这条卡**里面**。
   它只是"垫在内容下面的淡底"，不是白卡：没有描边、没有阴影、没有 --bg-elev 白底 ——
   设置页的层级口径是标题在卡外透明、淡底只包内容，这里没有标题行，所以只有这一条底。
   gap 必须是 0：有 gap 时相邻格的左边线会被劈成两半，看着像断开的刻度。 */
.kb-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0;
  align-items: stretch;
  min-width: 0;
  background: var(--bg-sunken);
  border-radius: var(--r-sm);
}
/* 竖发丝线只给"前面还有格子"的那几格，第一格不加（否则整条最左会多出一条竖线）。 */
.kb-metrics > .kb-metric + .kb-metric { border-left: 1px solid var(--divider); }

/* 860 两列、560 一列：换行后每行第一格不能带左边线，第二行起补一条横线，
   否则上下两行会连成一片，四格的读序也断了。 */
@container (max-width: 860px) {
  .kb-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .kb-metrics > .kb-metric + .kb-metric { border-left: 0; }
  .kb-metrics > .kb-metric:nth-child(even) { border-left: 1px solid var(--divider); }
  .kb-metrics > .kb-metric:nth-child(n + 3) { border-top: 1px solid var(--divider); }
}
@container (max-width: 560px) {
  .kb-metrics { grid-template-columns: minmax(0, 1fr); }
  .kb-metrics > .kb-metric + .kb-metric { border-left: 0; }
  .kb-metrics > .kb-metric:nth-child(n + 2) { border-top: 1px solid var(--divider); }
}

/* base.css 的 `.card + .card` 会在竖向堆叠时多顶 12px（弹性列自带 gap）。
   指标带与图表行已经不含 .card，这条只剩主体里那几张卡要清。
   选择器按 DOM 结构写，display:contents 的单列态同样命中。 */
.kb-col > .card + .card { margin-top: 0; }

/* 一格：图标 24 + 主值 20 + 副行 11 三行，格内上下留白取 10（全局 .card-body 是 16）。
   整条稳定在 ≈96：10 + 24 + 6 + 24 + 6 + 16.5 + 10 ≈ 96.5。 */
.kb-metric {
  min-width: 0;
  display: grid;
  gap: 6px;
  align-content: start;
  padding: 10px 14px;
}

.m-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
.m-icon {
  width: 24px; height: 24px; flex: none;
  display: grid; place-items: center;
  border-radius: 8px;
  background: var(--accent-soft);
  color: var(--accent-text);
}
.m-label {
  min-width: 0;
  font-size: calc(12.5px * var(--ts, 1));
  color: var(--text-dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.m-value {
  min-width: 0;
  font-size: calc(20px * var(--ts, 1));
  font-weight: 600;
  line-height: 1.2;
  letter-spacing: -.01em;
  font-variant-numeric: tabular-nums;
}
.m-value .badge { vertical-align: middle; }
/* 字符串型主值（向量模型名 / 「未配置」）：22px 是给数字的档位，模型名放不下就会截成
   "qwen3-vl-embed…"。降一档到 15px + 单行省略，全名交给 title —— 换行会把这一格撑高，
   同排四格立刻不齐，所以宁可行尾省略也不折行。 */
.m-value.txt {
  font-size: calc(15px * var(--ts, 1));
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.m-sub {
  min-width: 0;
  font-size: calc(11px * var(--ts, 1));
  line-height: 1.5;
  color: var(--text-faint);
}

/* ③ 图表行：一排三块（都不再是卡片）。≥900 三列；640~900 两列（第三张落到下一行）；<640 一列。
   三块等高（align-items: stretch），降到两列时第三块单独一行也只是自己高。
   三列时每张约 (956 - 24) / 3 ≈ 310，趋势图因此要跟着收成"小图"（见 KbTrendCard）。 */
.kb-charts {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
  align-items: stretch;
  min-width: 0;
}
/* 三块标题行同高：趋势 / 耗时各带一枚 24px 的 seg，加上卡头内距 9 + 9 正好 42；
   分布卡没有右侧控件，自身只有标题那一行（≈39）—— 差的就是这 3px。锁同一个 min-height，
   三块标题行与下面三条淡底就一起齐平，**任何宽度下都成立**（所以不再挂在容器查询里）。
   原值是 70：那是"标题行会折成两行"时的预留；现在标题行被约束成一行（见各卡的 .card-head），
   两行预留反而会把三块一起顶高一整行，所以降到 42。 */
.kb-charts :deep(.card-head) { min-height: 42px; }
@container (min-width: 640px) {
  .kb-charts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@container (min-width: 900px) {
  .kb-charts { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
.kb-charts > * { min-width: 0; }

/* ④ 主体：窄窗格单列，三个卡片用 order 排成 论文表 → 检索配置 → chunk。
   列容器在单列态用 display:contents 把自己的两张卡提上来，才能被 order 重排。 */
.kb-main {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
  align-items: start;
  min-width: 0;
}
.kb-col { display: contents; }
.kb-paper { order: 1; }
.kb-rag { order: 2; }
.kb-chunk { order: 3; }

/* ≥1040：左栏吃掉剩余宽度，右栏定宽 340 并粘在设置页滚动容器顶部。 */
@container (min-width: 1040px) {
  .kb-main { grid-template-columns: minmax(0, 1fr) 340px; }
  .kb-col {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
  }
  .kb-right { position: sticky; top: 0; }
}
</style>
