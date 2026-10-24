<script setup lang="ts">
/**
 * 「Chunk 类型分布」：知识库里的 chunk 按类型切成一圈环。
 *
 * 数据**由面板传进来**（GET /knowledge/overview 的 chunks_by_type 与 chunks），这里不发请求 ——
 * 同一份数字三处各取一次，早晚会出现"三块说三个数"。
 *
 * 为什么是环而不是堆叠条：各类之和就是总数，环形天生表达"部分占整体"，
 * 而且中心那块正好放总数，不用再单独做一行合计。
 *
 * ⚠️ **类型必须画全，不能只挑"内容四类"**：后端 knowledge_service.CHUNK_TYPES 是五类
 * （text / unit / formula / figure / table），unit（检索单元）也是 chunk 表里的行。
 * 早先只画四类、分母却用四类之和，于是出现过"中心写 814 总数、图例相加只有 428"的自相矛盾。
 * 现在的规矩：TYPES 决定顺序与前四类配色，表里多出来的未知键排在最后（同样 --text-faint，
 * 名称用键名）——**宁可多一行，不许丢数据**；分母一律用面板给的 total。
 *
 * 配色只用 tokens：正文 --accent / 公式 --ok / 插图 --warn / 表格 --text-dim /
 * 检索单元与未知类型 --text-faint（不另造一套图表色）。某类为 0 就不画扇区，
 * 但图例里照样列着、写 0 ——"没有公式 chunk"本身是诊断信息；
 * 全 0 时画一圈 --divider 的空环、中心写 0，不留空白。
 *
 * 高度按一排三块来定：环 84 + 图例五行（约 100），整块仍压在趋势块以内。
 * 这块是三块里唯一没有右侧控件的：标题行只放图标 + 标题（一行），高度与旁边两块
 * 由 .kb-charts 那条共用 min-height 拉齐，不靠自己凑。
 */
import { computed } from 'vue';
import AppIcon from '@/components/AppIcon.vue';

const props = defineProps<{
  typeCounts: Record<string, number>;
  total: number;
  loading: boolean;
}>();

interface MixType { key: string; label: string; color: string }
interface MixSlice extends MixType { n: number; pct: number }

/** 固定渲染顺序（顺序即扇区顺时针顺序）；后端再多出别的键，就在这四类之后追加。 */
const TYPES: MixType[] = [
  { key: 'text', label: '正文', color: 'var(--accent)' },
  { key: 'formula', label: '公式', color: 'var(--ok)' },
  { key: 'figure', label: '插图', color: 'var(--warn)' },
  { key: 'table', label: '表格', color: 'var(--text-dim)' },
  { key: 'unit', label: '检索单元', color: 'var(--text-faint)' },
];
/** 未知类型的配色与"最后一批"共用一档，不为它们现造 token。 */
const UNKNOWN_COLOR = 'var(--text-faint)';

/** viewBox 120：环半径 44、线宽 13，外侧留 3 给描边。周长用来切 dasharray。 */
const R = 44;
const C = 2 * Math.PI * R;

function fmt(v: unknown): string {
  return String(Math.round(Number(v) || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** 百分比：≥10% 取整、<10% 留一位 —— 表格那类常常只有个位数，抹成 0% 会让人以为没切到。 */
function pctText(v: number): string {
  if (!v) return '0%';
  return (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + '%';
}

/**
 * 分母：**一律用 total**（面板传 overview.chunks），四类之和不再参与百分比 ——
 * 255/814 = 31%，而不是 255/428 = 60%。total 缺失（0）时才退化成各类之和兜底。
 */
const base = computed<number>(() => {
  const total = Math.max(0, Number(props.total) || 0);
  if (total > 0) return total;
  return Object.values(props.typeCounts || {})
    .reduce((a, v) => a + Math.max(0, Number(v) || 0), 0);
});

const slices = computed<MixSlice[]>(() => {
  const src = props.typeCounts || {};
  const known = new Set(TYPES.map((t) => t.key));
  const rows: Array<MixType & { n: number }> = TYPES.map((t) => ({
    ...t, n: Math.max(0, Number(src[t.key]) || 0),
  }));
  // 未知键排在最后：名称用键名、配色走 --text-faint，不丢数据
  for (const [k, v] of Object.entries(src)) {
    if (!known.has(k)) rows.push({ key: k, label: k, color: UNKNOWN_COLOR, n: Math.max(0, Number(v) || 0) });
  }
  const b = base.value;
  return rows.map((r) => ({ ...r, pct: b > 0 ? (r.n / b) * 100 : 0 }));
});

/** 扇区：0 的不进 SVG（否则会留下一段看不见、却吃掉 dash 空白的假分段）。 */
const arcs = computed(() => {
  const rows = slices.value;
  const sum = rows.reduce((a, s) => a + s.n, 0);
  if (!sum) return [] as Array<{ key: string; color: string; dash: string; offset: number }>;
  // 正常数据下 sum === total；后端少给一类时按较大的分母画，环不至于叠出圈外
  const denom = Math.max(base.value, sum);
  let at = 0;
  return rows
    .filter((s) => s.n > 0)
    .map((s) => {
      const len = (s.n / denom) * C;
      const seg = { key: s.key, color: s.color, dash: `${len} ${C - len}`, offset: -at };
      at += len;
      return seg;
    });
});

const totalText = computed(() => (props.loading ? '—' : fmt(props.total)));
</script>

<template>
  <section class="mix">
    <div class="card-head">
      <AppIcon name="layers" :size="15" /><span class="ttl">Chunk 类型分布</span>
    </div>

    <div class="card-body tight">
      <div v-if="loading" class="state">正在读取…</div>

      <div v-else class="mix-wrap">
        <div class="donut">
          <svg viewBox="0 0 120 120" role="img"
               :aria-label="slices.map((s) => s.label + ' ' + s.n + ' 条').join('，')">
            <!-- 底环：全 0 时它就是"还没有数据"的那一圈，有扇区时被盖住当抗锯齿基线 -->
            <circle class="track" cx="60" cy="60" :r="R" />
            <circle
              v-for="a in arcs" :key="a.key" class="arc" cx="60" cy="60" :r="R"
              :stroke="a.color" :stroke-dasharray="a.dash" :stroke-dashoffset="a.offset"
              transform="rotate(-90 60 60)"
            />
          </svg>
          <span class="hub">
            <b class="tnum">{{ totalText }}</b>
            <span class="tiny faint">总数</span>
          </span>
        </div>

        <ul class="legend">
          <li v-for="s in slices" :key="s.key" class="lg-row">
            <i class="dot" :style="{ background: s.n > 0 ? s.color : 'var(--border-strong)' }"></i>
            <span class="nm truncate">{{ s.label }}</span>
            <span class="num tnum">{{ s.n }}</span>
            <span class="pc tnum">{{ pctText(s.pct) }}</span>
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ttl {
  min-width: 0; flex: 0 1 auto;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: calc(13.5px * var(--ts, 1));
}
.mix { min-width: 0; display: flex; flex-direction: column; }
/* 标题行在淡底之外（透明、无发丝线）；分隔交给下面那条淡色圆角底。
   与旁边两块同一套规矩：**不折行** —— 这块右侧没有控件，标题行天然比它们矮一档，
   同高由 .kb-charts 里那条共用 min-height 拉齐（见 KbIndexPanel）。 */
.mix .card-head { flex-wrap: nowrap; column-gap: 8px; border-bottom: 0; }

/* 图区（环 + 图例）整块装进淡色圆角卡：淡底只垫内容，不包标题。 */
/* 内容在淡底里纵向居中：三块等高时这块会比内容高出一截，贴着顶会显得上面挤、下面空。 */
.mix .card-body.tight {
  flex: 1 1 auto;
  display: flex; flex-direction: column; justify-content: center;
  padding: 12px;
  background: var(--bg-sunken);
  border-radius: var(--r-sm);
}

/* 三态与图表共用一块高度：切换时整块不跳。这个数取"有数据那态"的高度 ——
   环 84 与图例五行（5×16.2 + 4×3 ≈ 93）里图例更高，所以取 93。 */
.state {
  flex: 1 1 auto;
  display: flex; align-items: center; justify-content: center;
  min-height: 93px;
  font-size: calc(12.5px * var(--ts, 1)); color: var(--text-faint);
}

/* 环在左、图例在右；窄到放不下时图例整体落到下一行（不夹成两列挤字） */
.mix-wrap { display: flex; align-items: center; justify-content: center; gap: 12px; flex-wrap: wrap; min-width: 0; }

/* 环直径 96 → 84（约 -12%）：整卡仍由趋势卡定高，但环不那么抢眼，图例行也还能并排 */
.donut { position: relative; flex: none; width: 84px; height: 84px; }
.donut svg { display: block; width: 100%; height: 100%; }
.track { fill: none; stroke: var(--divider); stroke-width: 13; }
.arc { fill: none; stroke-width: 13; stroke-linecap: butt; transition: stroke-dasharray var(--dur) var(--ease); }

/* 中心读数：数字走 20px（这是一张图里的读数，不属于指标块那套字号档） */
.hub {
  position: absolute; inset: 0;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 1px; pointer-events: none; line-height: 1;
}
.hub b {
  font-size: calc(20px * var(--ts, 1)); font-weight: 600;
  letter-spacing: -.01em; color: var(--text);
}
.hub .tiny { line-height: 1.2; }

.legend {
  /* 上限 340：单列时淡底有 600+ 宽，让图例一路拉满会把"正文"和"250"拽到屏幕两头，
     中间空出一大片；封顶之后整块居中，"没有大片空白"这件事才算真的成立。
     ⚠️ 封顶只能用 max-width，不能写成 flex-basis: 340px —— 换行是按 flex-basis 算的，
     那样一行放不下（84 + 340 > 淡底内容宽）就直接把图例甩到环下面去。 */
  flex: 1 1 132px; min-width: 0; max-width: 340px;
  display: flex; flex-direction: column; gap: 3px;
  margin: 0; padding: 0; list-style: none;
}
.lg-row { display: flex; align-items: center; gap: 7px; min-width: 0; line-height: 1.35; }
.dot { width: 8px; height: 8px; border-radius: var(--r-pill); flex: none; }
.nm { flex: 1 1 auto; font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); }
.num {
  flex: none; font-size: calc(12px * var(--ts, 1)); color: var(--text);
  font-variant-numeric: tabular-nums;
}
.pc {
  flex: none; min-width: 40px; text-align: right;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
</style>
