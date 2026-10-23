<script setup lang="ts">
/**
 * 「索引存储状态」：一张卡把两侧存储（MySQL chunk 元数据 / LanceDB 向量）与维数一次讲清。
 *
 * 数据全部由父组件持有（它负责校验、清理、改论文范围），这张卡只显示现状、发两个意图：
 * 「校验数据」和「清理孤儿元数据」。手动校验的确认框与请求都留在父组件——只有那边知道
 * 当前范围是全库还是单篇，卡片自己发请求会和父组件手里的结论各说各话。
 *
 * 一致性只认 status.consistent：后端做的是**全库 id 集合对账**（MySQL 有、向量库没有的
 * 元数据，以及向量库有、MySQL 没有的向量都算差），不是数条数。两侧数字凑巧相等完全
 * 可能并不一致（向量重建后旧元数据还留着），所以这里绝不拿 chunks === vector.count 当判据。
 * 由此，consistent 为 null（单篇视图拿不到全库 id 集合）时只能说"未知"，并点明仅全库可校验。
 *
 * 排版按"先库、再维数、最后差异"读：两行存储靠分隔线连成一段，不套第二层卡片；
 * 差异与故障只在真出现时多占一行，正常状态下一屏看完。
 */
import { computed } from 'vue';
import AppIcon from '@/components/AppIcon.vue';

/** index_status 里本卡用到的字段（后端是动态 JSON，边界处按需声明；全部可选便于父组件按需给）。 */
interface VectorStatus {
  available: boolean;
  count?: number | null;
  error?: string | null;
}
interface KbIndexStatus {
  paper_id?: string | null;
  chunks?: number | null;
  chunks_with_hash?: number | null;
  vector?: VectorStatus | null;
  orphan_chunks?: number | null;
  missing_vectors?: number | null;
  consistent?: boolean | null;
  model_dim?: number | null;
  store_dim?: number | null;
  dim_match?: boolean | null;
  index_versions?: Record<string, number> | null;
}

const props = withDefaults(defineProps<{
  status: KbIndexStatus | null;
  loading: boolean;
  /** 父组件正在跑的动作键；'reconcile' 时两个按钮都禁掉，避免叠加校验。 */
  busy: string;
  /** 最近一次校验（对账）的时间，父组件给；没有就显示 —。 */
  checkedAt?: string;
}>(), { checkedAt: '' });

const emit = defineEmits<{ (e: 'reconcile', fix: boolean): void }>();

function num(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : 0;
}
function fmtNum(v: number): string {
  return String(Math.round(num(v))).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

const st = computed<KbIndexStatus | null>(() => props.status);
const vector = computed<VectorStatus | null>(() => st.value?.vector ?? null);
const vectorOk = computed(() => vector.value?.available === true);
const chunks = computed(() => num(st.value?.chunks));
const vectorCount = computed(() => num(vector.value?.count));

/** 全库对账结论：true / false / null（没跑过对账，或当前是单篇视图）。 */
const consistent = computed<boolean | null>(
  () => (typeof st.value?.consistent === 'boolean' ? st.value.consistent : null),
);

const orphans = computed<number | null>(
  () => (st.value?.orphan_chunks === null || st.value?.orphan_chunks === undefined ? null : num(st.value.orphan_chunks)),
);
const missing = computed<number | null>(
  () => (st.value?.missing_vectors === null || st.value?.missing_vectors === undefined ? null : num(st.value.missing_vectors)),
);

const badge = computed<{ cls: string; text: string }>(() => {
  if (!st.value) return { cls: 'dim', text: '未知' };
  if (consistent.value === true) return { cls: 'ok', text: '一致' };
  if (consistent.value === false) return { cls: 'danger', text: '不一致' };
  return { cls: 'dim', text: '未知' };
});

/** MySQL 一行右侧的状态徽章：结论来自 consistent；具体原因（待清理几条 / 未对账）留在 title。 */
const mysqlBadge = computed<{ cls: string; text: string }>(() => {
  if (!st.value) return { cls: 'dim', text: '未知' };
  if (consistent.value === true) return { cls: 'ok', text: '一致' };
  if (consistent.value === false) return { cls: mysqlWarn.value ? 'warn' : 'danger', text: '不一致' };
  return { cls: 'dim', text: '未对账' };
});

/** LanceDB 一行右侧的状态徽章：只报向量库本身可用不可用，不替它说一致性。 */
const vectorBadge = computed<{ cls: string; text: string }>(() => {
  if (!st.value) return { cls: 'dim', text: '未知' };
  return vectorOk.value ? { cls: 'ok', text: '可用' } : { cls: 'danger', text: '不可用' };
});

/** MySQL 一侧的读数：只说 id 集合对账的结论，不承诺"数据完全一致"。 */
const mysqlSub = computed(() => {
  if (!st.value) return '—';
  if (consistent.value === true) return 'id 集合对账一致';
  if (orphans.value) return `${fmtNum(orphans.value)} 条元数据待清理`;
  if (consistent.value === false) return '与向量库不一致';
  return '未对账';
});
const mysqlWarn = computed(() => consistent.value === false && !!orphans.value);

const ldbSub = computed(() => {
  if (!st.value) return '—';
  return vectorOk.value ? '可用' : '不可用';
});

const dimText = (v: unknown): string => (v === null || v === undefined || v === '' ? '—' : fmtNum(Number(v)));

/** 优先用后端的 dim_match；只给了两个维数时自己比一次，两个都没有就不猜。 */
const dimMatch = computed<boolean | null>(() => {
  const s = st.value;
  if (!s) return null;
  if (typeof s.dim_match === 'boolean') return s.dim_match;
  if (s.model_dim !== null && s.model_dim !== undefined && s.store_dim !== null && s.store_dim !== undefined) {
    return Number(s.model_dim) === Number(s.store_dim);
  }
  return null;
});
const dimBadge = computed<{ cls: string; text: string }>(() => {
  if (dimMatch.value === true) return { cls: 'ok', text: '维度一致' };
  if (dimMatch.value === false) return { cls: 'danger', text: '维度不符，需重建索引' };
  return { cls: 'dim', text: '未知' };
});

/** 差异明细：只报后端给出数的项，没有数就不编。 */
const diffText = computed(() => {
  const parts: string[] = [];
  if (orphans.value) parts.push(`孤儿元数据 ${fmtNum(orphans.value)} 条`);
  if (missing.value) parts.push(`孤儿向量 ${fmtNum(missing.value)} 条`);
  return parts.length ? parts.join(' · ') : '两侧 id 集合不一致';
});

/** 两侧差异 reconcile(fix) 都会清：orphans 走 chunk 元数据批量删除，missing 走 vector_store.delete_chunks
 *  （knowledge_service.reconcile）。所以任一侧有数就给清理按钮；两侧都是 null = 当前视图没有对账数，不给。 */
const canClean = computed(
  () => consistent.value === false && ((orphans.value ?? 0) > 0 || (missing.value ?? 0) > 0),
);

/** 按钮文案只说这次真会清掉什么：哪一侧是 0 就不提它。 */
const cleanLabel = computed(() => {
  const o = orphans.value ?? 0;
  const m = missing.value ?? 0;
  const parts: string[] = [];
  if (o > 0) parts.push(`元数据 ${fmtNum(o)}`);
  if (m > 0) parts.push(`向量 ${fmtNum(m)}`);
  return parts.length ? `清理差异（${parts.join(' · ')}）` : '清理差异';
});

const busyReconcile = computed(() => props.busy === 'reconcile');

function check(): void { emit('reconcile', false); }
function clean(): void { emit('reconcile', true); }
</script>

<template>
  <!-- 标题在块外（透明），淡色圆角底只垫内容 —— 与设置页其余区块同一套语言 -->
  <section class="storage">
    <div class="storage-head">
      <AppIcon name="server" :size="15" /><span class="ttl">索引存储状态</span>
      <span class="badge" :class="badge.cls">{{ badge.text }}</span>
      <span class="spacer"></span>
      <button class="btn sm ghost" type="button" :disabled="busyReconcile" @click="check">
        <AppIcon name="refresh" :size="14" />{{ busyReconcile ? '校验中…' : '校验数据' }}
      </button>
    </div>

    <div class="storage-body">
      <p class="state-line tiny faint" v-if="loading && !st">正在读取索引状态…</p>
      <p class="state-line tiny faint" v-else-if="!st">暂无索引状态，请先在上方刷新或校验一次。</p>

      <template v-else>
        <!-- 两行存储：同一条分隔线语言，不套第二层卡片。
             两行（连下面的维数行）共用一套列：左 = 主名 + 副名，中间留白，右 = 值列 + 状态徽章。
             对齐来自列宽而不是每行各自的 margin，读数才落在同一条竖线上。 -->
        <div class="stores">
          <div class="store">
            <div class="store-l">
              <span class="store-k">MySQL</span>
              <span class="store-sub">关系库</span>
            </div>
            <span class="store-v"><b class="n">{{ fmtNum(chunks) }}</b><i class="u">条</i></span>
            <span class="badge" :class="mysqlBadge.cls" :title="mysqlSub">{{ mysqlBadge.text }}</span>
          </div>
          <div class="store">
            <div class="store-l">
              <span class="store-k">LanceDB</span>
              <span class="store-sub">向量库</span>
            </div>
            <span class="store-v">
              <b class="n" v-if="vectorOk">{{ fmtNum(vectorCount) }}</b><i class="u" v-if="vectorOk">条</i>
              <b class="n" v-else>—</b>
            </span>
            <span class="badge" :class="vectorBadge.cls" :title="ldbSub">{{ vectorBadge.text }}</span>
          </div>
        </div>

        <div class="dim-row">
          <div class="dim-text">
            <span class="store-k">维数</span>
            <span class="dim-nums">
              模型 <b class="n">{{ dimText(st.model_dim) }}</b> 维
              <span class="arrow">↔</span>
              库 <b class="n">{{ dimText(st.store_dim) }}</b> 维
            </span>
          </div>
          <span class="badge" :class="dimBadge.cls">{{ dimBadge.text }}</span>
        </div>

        <p class="tiny faint check-line">最近校验：{{ checkedAt || '—' }}</p>

        <!-- 不一致：差异一行 + 清理动作；一致或未知时这一行根本不占位 -->
        <div class="diff-row" v-if="consistent === false">
          <span class="small">{{ diffText }}</span>
          <button class="btn sm" type="button" v-if="canClean" :disabled="busyReconcile" @click="clean">
            <AppIcon name="trash" :size="13" />{{ cleanLabel }}
          </button>
        </div>
        <p class="tiny faint" v-else-if="consistent === null">单篇视图拿不到全库 id 集合：仅全库可校验。</p>

        <p class="danger-line" v-if="vector && !vectorOk">向量库不可用：{{ vector.error || '后端未返回原因' }}</p>
      </template>
    </div>
  </section>
</template>

<style scoped>
.ttl { font-size: calc(13.5px * var(--ts, 1)); }
/* 标题行：透明、在块外；内容底：淡色圆角（--bg-sunken + --r-sm），与设置页其它区块一致 */
.storage-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; row-gap: 6px; margin-bottom: 8px; }
.storage-head .btn { flex: none; }
.storage-body { padding: 12px 14px; border-radius: var(--r-sm); background: var(--bg-sunken); }

.state-line { margin: 0; padding: 8px 0; line-height: 1.5; }

/* 两行存储 + 维数行共用同一套列：1fr 留白 | 值列定宽 88px | 徽章列。
   徽章列给下限 72px，两行的条目数才落在同一个 x 上（不是各自 margin-left:auto 飘过去的）。 */
.store, .dim-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 88px minmax(72px, auto);
  align-items: center;
  gap: 10px;
}

/* 两行存储连成一段：行间距 5px，行间一条 --divider */
.stores { display: flex; flex-direction: column; }
.store { padding: 4px 0; }
.store + .store { border-top: 1px solid var(--divider); }

.store-l { display: flex; align-items: baseline; gap: 6px; min-width: 0; }
.store-k {
  font-size: calc(13px * var(--ts, 1)); font-weight: 600; color: var(--text);
  white-space: nowrap;
}
/* 副名：只交代"这是哪一种库"，状态交给右侧徽章，不再和它抢同一句结论 */
.store-sub {
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.store-v {
  text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums;
}
.store-v .n {
  font-size: calc(15px * var(--ts, 1)); font-weight: 650; line-height: 1.15;
  color: var(--text); font-variant-numeric: tabular-nums;
}
.u {
  font-style: normal; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim);
  margin-left: 4px;
}

.store .badge, .dim-row .badge { justify-self: end; }

/* 维数一行：左边"维数"，读数右对齐到同一个值列，徽章落进同一个徽章列 */
.dim-row {
  padding: 5px 0 0; margin-top: 4px; border-top: 1px solid var(--divider);
}
.dim-text {
  grid-column: 1 / span 2;
  display: flex; align-items: baseline; justify-content: space-between; gap: 10px;
  min-width: 0;
  font-size: calc(12px * var(--ts, 1)); color: var(--text-dim);
}
.dim-nums { font-variant-numeric: tabular-nums; white-space: nowrap; }
.dim-nums .n {
  font-size: calc(12px * var(--ts, 1)); font-weight: 650; color: var(--text);
  font-variant-numeric: tabular-nums;
}
.arrow { color: var(--text-faint); margin: 0 3px; }

.check-line { margin: 4px 0 0; }

.diff-row {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin-top: 6px;
}
.diff-row .small { min-width: 0; color: var(--danger); }
.diff-row .btn { flex: none; }

.danger-line {
  margin: 6px 0 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.5;
  color: var(--danger); overflow-wrap: anywhere;
}

/* 窄屏只动两处：差异行的清理按钮独占一行；三行仍共用同一套列（1fr 会自己收窄，
   纵向不会被撑出横向滚动）。 */
@media (max-width: 560px) {
  .diff-row .btn { width: 100%; }
}
</style>
