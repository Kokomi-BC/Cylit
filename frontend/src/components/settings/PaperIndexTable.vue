<script setup lang="ts">
/**
 * 设置页「论文索引管理」表格 —— 按论文逐条管理索引（建索引 / 重建 / 从知识库移除）。
 *
 * 数据从哪来：**全部来自 props**，父组件（编排层）读 `GET /knowledge/overview` 拿到
 * `items`（id/title/status/in_knowledge_base/chunks）与 `index_versions`，再经
 * `GET /knowledge/index/status` 补索引状态。本组件**不发任何请求**：它不知道
 * `/knowledge/index`、`/knowledge/remove` 长什么样，也不负责确认框 ——
 * 所有副作用（含 `askConfirm`）都交给父组件。这样同一张表可以被别处复用，
 * 而"点了到底发生什么"永远只有一个地方说了算。
 *
 * 为什么这么排：
 * - **一行只留一个主操作** + 一个溢出菜单。索引管理里移除是低频且有破坏性的，摆成一排
 *   同权重按钮会让误点代价变高。主操作按行判断语义：没有 chunk 就是「建索引」，已经有
 *   chunk 就是「重建索引」——同一行不出现两个"接下来该点哪个"的答案，也不让用户在
 *   主按钮和菜单之间猜差别；菜单因此只剩「从知识库移除」，没 chunk 时干脆不渲染。
 * - 筛选与分页都是**纯前端**：概览接口本来就一次给全量（论文数量级在内存里毫无压力），
 *   再为搜索翻页去请求后端，只会换来加载态闪烁和竞态。
 * - 状态筛选的选项**从真实数据里长出来**（`paperStatus()` 的文案 + 这批论文里实际出现的
 *   status 值），不写死一份可能与后端脱节的枚举 —— 后端加了新状态，这里自动跟上。
 * - 索引版本列是**诊断用**的窄列，没有版本就显示 `—`，不假装有值。
 * - 「最近索引」同样只读诊断：它显示的是**后端本地时间**的原样切片。不能走
 *   `fmtDate()`——那个函数会 `new Date()` 再取本地字段，等于把"服务器本地"重新解释成
 *   "浏览器本地"，跨时区时同一个时间会显示成两个值；这里只做字符串比较与截取。
 */
import { computed, ref, watch } from 'vue';
import type { KnowledgeOverview } from '@/api/types';
import { PAPER_STATUS, paperStatus } from '@/utils/format';
import Menu from '@/components/Menu.vue';
import AppIcon from '@/components/AppIcon.vue';

type KbPaper = KnowledgeOverview['items'][number];

// 与 Menu 组件的 items 同构（结构类型，无需导出）
interface MenuItem {
  key: string;
  label: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
}

const props = defineProps<{
  items: KbPaper[];
  /** 论文 id → 该篇的最大 chunk 索引版本（父组件取自 overview.index_versions） */
  versions: Record<string, number>;
  loading: boolean;
  /** 形如 `idx:<paperId>` / `remove:<paperId>`；只用来点亮当前行 */
  busyKey: string;
}>();

const emit = defineEmits<{
  (e: 'index', paper: KbPaper): void;
  (e: 'rebuild', paper: KbPaper): void;
  (e: 'remove', paper: KbPaper): void;
  (e: 'open', paper: KbPaper): void;
}>();

const PAGE_SIZES = [10, 20, 50];
/** 超过这个页数才折叠（7 枚以内的页码一次全摊开更好点）。 */
const COLLAPSE_OVER = 7;
/** 页码列表里的"省略号"占位（0 不可能是页号）。 */
const GAP = 0;

const KB_ALL = 'all';
const KB_IN = 'kb:in';
const KB_OUT = 'kb:out';
const STATUS_PREFIX = 'st:';

const query = ref('');
const statusFilter = ref(KB_ALL);
const pageSize = ref(PAGE_SIZES[0]);
const page = ref(1);

/** 这批论文里**实际出现**的 status，按 PAPER_STATUS 的声明顺序排，未知值跟在后头。 */
const presentStatuses = computed<string[]>(() => {
  const seen: string[] = [];
  for (const item of props.items) {
    const s = item.status || '';
    if (s && !seen.includes(s)) seen.push(s);
  }
  const known = Object.keys(PAPER_STATUS).filter((s) => seen.includes(s));
  const unknown = seen.filter((s) => !(s in PAPER_STATUS));
  // `indexed` 的文案就是「已入库」，与 in_knowledge_base 那一维的选项同名；两条同名选项
  // 只会让人不知道该选哪个，所以状态组里去掉它，「在不在知识库」统一由 kb 两项表达。
  return [...known, ...unknown].filter((s) => s !== 'indexed');
});

const filterOptions = computed<Array<{ value: string; label: string }>>(() => [
  { value: KB_ALL, label: '全部' },
  { value: KB_IN, label: '已入库' },
  { value: KB_OUT, label: '未入库' },
  ...presentStatuses.value.map((s) => ({ value: `${STATUS_PREFIX}${s}`, label: paperStatus(s).label })),
]);

function matches(paper: KbPaper): boolean {
  const f = statusFilter.value;
  if (f === KB_IN && !paper.in_knowledge_base) return false;
  if (f === KB_OUT && paper.in_knowledge_base) return false;
  if (f.startsWith(STATUS_PREFIX) && paper.status !== f.slice(STATUS_PREFIX.length)) return false;
  const q = query.value.trim().toLowerCase();
  if (!q) return true;
  return paper.title.toLowerCase().includes(q) || paper.id.toLowerCase().includes(q);
}

const filtered = computed<KbPaper[]>(() => props.items.filter(matches));
const isFiltered = computed(() => !!query.value.trim() || statusFilter.value !== KB_ALL);

const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize.value)));
const rows = computed<KbPaper[]>(() =>
  filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value));

/**
 * 页码：≤7 页全摊开；超过就折叠成「首、…、当前±1、…、尾」。
 * 省略号只代表**至少两页**——夹了一页也用「…」是骗人的，那种情况直接把那一页写出来。
 */
const pageNumbers = computed<number[]>(() => {
  const total = pageCount.value;
  const cur = page.value;
  if (total <= COLLAPSE_OVER) return Array.from({ length: total }, (_, i) => i + 1);
  const out: number[] = [1];
  const start = Math.max(2, cur - 1);
  const end = Math.min(total - 1, cur + 1);
  if (start === 3) out.push(2);
  else if (start > 3) out.push(GAP);
  for (let n = start; n <= end; n += 1) out.push(n);
  if (end === total - 2) out.push(total - 1);
  else if (end < total - 2) out.push(GAP);
  out.push(total);
  return out;
});

watch([query, statusFilter, pageSize], () => {
  page.value = 1;
});
watch(pageCount, (n) => {
  if (page.value > n) page.value = n;
});

/** 当前年份只用来判断「同年就可以省掉年份」；它只参与比较，不参与换算。 */
const THIS_YEAR = String(new Date().getFullYear());
/** `YYYY-MM-DDTHH:mm...`（容忍末尾还有秒/毫秒/时区后缀，但只取到分）。 */
const ISO_LOCAL = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/;

/**
 * 「最近索引」文案：同年 `MM-DD HH:mm`，跨年 `YYYY-MM-DD`（年份不许丢）。
 * 纯字符串处理，绝不经过 `Date`：后端给的就是它自己的本地时间，转一道只会错位。
 */
function indexedText(raw?: string | null): string {
  if (!raw) return '—';
  const m = ISO_LOCAL.exec(raw);
  if (!m) return raw;
  const [, y, mo, d, h, mi] = m;
  return y === THIS_YEAR ? `${mo}-${d} ${h}:${mi}` : `${y}-${mo}-${d}`;
}

function versionText(paper: KbPaper): string {
  const v = props.versions[paper.id];
  return v === undefined || v === null ? '—' : `v${v}`;
}

/** 入库维度只有两个文案；论文状态徽章要与它比对，所以文案只写这一份。 */
const KB_LABEL = { in: '已入库', out: '未入库' } as const;

function kbLabel(paper: KbPaper): string {
  return paper.in_knowledge_base ? KB_LABEL.in : KB_LABEL.out;
}

/** 状态列的一枚徽章：文案与配色都由这里给出，模板不再自己拼中文。 */
interface StatusBadge {
  key: 'status' | 'kb';
  label: string;
  cls: string;
}

/**
 * 状态列这一行要渲染的徽章（1 枚或 2 枚）—— 判断只写在这一处。
 *
 * `indexed` 的文案本身就是「已入库」，和右邻的入库徽章一字不差；两枚一样的词并排，
 * 人会以为其中一枚另有含义。所以**文案撞车时只留入库徽章**（它还带着 accent/dim 的
 * 强弱差别，比论文状态那枚更该活下来）。文案不同（解析中 / 失败 / 已解析…）说明两者
 * 是不同维度，两枚都留 —— 例如未入库行的「已解析」+「未入库」。
 */
function statusBadges(paper: KbPaper): StatusBadge[] {
  const kb = kbLabel(paper);
  const st = paperStatus(paper.status);
  const out: StatusBadge[] = [];
  if (st.label !== kb) out.push({ key: 'status', label: st.label, cls: st.cls });
  out.push({ key: 'kb', label: kb, cls: paper.in_knowledge_base ? 'accent' : 'dim' });
  return out;
}

function isIdxBusy(paper: KbPaper): boolean {
  return props.busyKey === `idx:${paper.id}`;
}

/** 有 chunk 才有"移除"可言；没有 chunk 时菜单整个不渲染（见模板），不留一个只有禁用项的菜单。 */
function rowMenu(): MenuItem[] {
  return [{ key: 'remove', label: '从知识库移除', icon: 'trash', danger: true }];
}

function onMenu(key: string, paper: KbPaper) {
  if (key === 'remove') emit('remove', paper);
}

function mainLabel(paper: KbPaper): string {
  return paper.chunks === 0 ? '建索引' : '重建索引';
}

/** 主操作按行决定语义：没 chunk = 建，有 chunk = 重建。行上永远只有一个明确的下一步。 */
function onMain(paper: KbPaper) {
  if (paper.chunks === 0) emit('index', paper);
  else emit('rebuild', paper);
}

function menuLabel(paper: KbPaper): string {
  return `《${paper.title}》的更多操作`;
}
</script>

<template>
  <div class="card pit">
    <div class="card-head pit-head">
      <span class="pit-title">论文索引管理</span>
      <span class="spacer"></span>
      <div class="search-field pit-search">
        <AppIcon name="search" :size="15" />
        <input
          v-model="query"
          type="search"
          aria-label="搜索论文标题"
          placeholder="搜索论文标题…"
          :disabled="!items.length"
          autocomplete="off"
        />
      </div>
      <select
        v-model="statusFilter"
        class="pit-filter"
        aria-label="按入库状态或论文状态筛选"
        :disabled="!items.length"
      >
        <option v-for="opt in filterOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
      </select>
    </div>

    <div class="card-body tight">
      <p v-if="isFiltered && items.length" class="pit-hits tiny faint tnum">
        共 {{ items.length }} 篇，命中 {{ filtered.length }} 篇
      </p>

      <!-- 首次加载（还没有任何数据可显示）才让位给一句加载文案：
           刷新时保留旧表格，避免整块闪一下、页码跳回顶部。 -->
      <p v-if="loading && !items.length" class="empty">正在读取论文索引…</p>
      <p v-else-if="!items.length" class="empty">还没有论文，先去论文库导入并解析。</p>
      <p v-else-if="!filtered.length" class="empty">没有匹配的论文。</p>

      <div v-else class="tbl-wrap">
        <!-- 兜底横向滚动：列再多也只是**卡内**滚，绝不把列硬裁掉（.pit 的 overflow:hidden 只裁圆角） -->
        <table class="data fixed" aria-label="论文索引管理">
          <thead>
            <tr>
              <th scope="col">论文</th>
              <th scope="col" class="col-status">状态</th>
              <th scope="col" class="col-chunks num">chunks</th>
              <th scope="col" class="col-ver">索引版本</th>
              <th scope="col" class="col-indexed">最近索引</th>
              <th scope="col" class="col-ops">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="paper in rows" :key="paper.id">
              <td class="pit-cell-title">
                <button
                  type="button"
                  class="pit-open truncate"
                  :title="paper.title"
                  @click="emit('open', paper)"
                >{{ paper.title }}</button>
                <span class="pit-id tiny faint mono truncate" :title="paper.id">{{ paper.id }}</span>
              </td>
              <td>
                <div class="pit-status">
                  <span
                    v-for="b in statusBadges(paper)"
                    :key="b.key"
                    class="badge"
                    :class="b.cls"
                  >{{ b.label }}</span>
                </div>
              </td>
              <td class="num">
                <span :class="{ faint: !paper.chunks }">{{ paper.chunks }}</span>
              </td>
              <td class="col-ver">
                <span class="tnum" :class="{ faint: versionText(paper) === '—' }">{{ versionText(paper) }}</span>
              </td>
              <td class="col-indexed">
                <span
                  class="tnum"
                  :class="{ faint: !paper.indexed_at }"
                  :title="paper.indexed_at || undefined"
                >{{ indexedText(paper.indexed_at) }}</span>
              </td>
              <td class="ops">
                <div class="pit-ops">
                  <button
                    type="button"
                    class="btn sm pit-main"
                    :disabled="isIdxBusy(paper)"
                    :title="`为《${paper.title}》${mainLabel(paper)}`"
                    @click="onMain(paper)"
                  >
                    {{ isIdxBusy(paper) ? '排队中…' : mainLabel(paper) }}
                  </button>
                  <Menu
                    v-if="paper.chunks > 0"
                    :items="rowMenu()"
                    :label="menuLabel(paper)"
                    size="sm"
                    @select="(key) => onMenu(key, paper)"
                  />
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="filtered.length" class="pit-foot">
      <span class="pit-count tiny faint tnum">共 {{ filtered.length }} 条</span>
      <span class="spacer"></span>
      <nav v-if="pageCount > 1" class="seg sq pit-pages" aria-label="分页">
        <template v-for="(n, i) in pageNumbers" :key="i">
          <span v-if="n === 0" class="pit-gap" aria-hidden="true">…</span>
          <button
            v-else
            type="button"
            :class="{ active: n === page }"
            :aria-current="n === page ? 'page' : undefined"
            :title="`第 ${n} 页`"
            @click="page = n"
          >{{ n }}</button>
        </template>
      </nav>
      <label class="pit-size tiny faint">
        每页
        <select v-model.number="pageSize" class="pit-size-sel" aria-label="每页条数">
          <option v-for="n in PAGE_SIZES" :key="n" :value="n">{{ n }}</option>
        </select>
        条
      </label>
    </div>
  </div>
</template>

<style scoped>
/* 这张卡自己做容器查询的锚点：列可见性按**卡片自身宽度**判断，与视口无关 ——
   设置页右栏、侧栏收起、窗口大小都会让卡片宽度和视口对不上，用 @media 一定排错。
   overflow: hidden 只负责裁圆角；表格溢出的部分交给 .tbl-wrap 横向滚动。 */
.pit { overflow: hidden; container-type: inline-size; min-width: 0; }

/* 卡头：标题 + 弹性留白 + 检索控件；窄屏允许换行，绝不挤成一坨 */
.pit-head { flex-wrap: wrap; row-gap: 8px; }
.pit-title { font-weight: 600; }

/* 带图标的搜索框复用全局 .search-field，只把宽度按这一页的需求改到 240px 左右 */
.pit-search { flex: 0 0 auto; }
.pit-search input { width: 240px; }
.pit-filter { width: 132px; flex: 0 0 auto; }

.pit-hits { margin: 0 0 6px; padding: 0 4px; }

/* 行高中收紧一档：全局 8/11 → 6/10（表头同一套）。字号仍是全局 13px / 表头 12px。
   选择器带 .pit 前缀并落在本组件的 scoped 上，只作用于这张表。 */
.pit table.data th,
.pit table.data td { padding: 6px 10px; }

/* 状态徽章收一档高度（22 → 20），文字仍是 11.5px，不低于 11px 的下限 */
.pit-status .badge { height: 20px; padding: 0 9px; }

/* 卡内横向滚动兜底（与 ChunkMetaCard 的 .tbl-wrap 同一做法）：任何极端宽度下都是滚，
   不会出现"列被切掉、按钮点不到"。overscroll-behavior-x 防滚到头时把整页一起带走。 */
.tbl-wrap { overflow-x: auto; overscroll-behavior-x: contain; border-radius: var(--r-sm); }
.pit table.data { min-width: 480px; }

/* 表格：列可见性按容器宽度分三档（见文件末尾 @container）；列宽写死保持不变，
   宁可整列让位，也不把状态徽章 / 论文标题压到一起。 */
.col-status { width: 168px; }
.col-chunks { width: 76px; }
.col-ver { width: 84px; }
.col-indexed { width: 118px; }
.col-ops { width: 120px; }
/* 数字列表头跟着右对齐，和下面的 .num 单元格同一根竖线 */
th.col-chunks { text-align: right; }

.tnum { font-variant-numeric: tabular-nums; }

/* 论文列：标题是可点的一行，id 在下面一行；两者都按单元格宽度截断 */
.pit-cell-title > .pit-open,
.pit-cell-title > .pit-id { display: block; max-width: 100%; box-sizing: border-box; }
.pit-open {
  width: 100%; padding: 3px 4px; margin: 0 0 1px;
  border: 1px solid transparent; border-radius: var(--r-xs);
  background: transparent; color: var(--text);
  font: inherit; font-weight: 600; text-align: left; cursor: pointer;
  transition: background var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.pit-open:hover { background: var(--bg-hover); border-color: var(--border); }
.pit-open:focus-visible { outline: none; box-shadow: var(--ring); }

/* 状态下 1–2 枚徽章（文案撞车时只剩入库那枚）：168px 里塞得下就并排，塞不下就换行（不撑宽表格） */
.pit-status { display: flex; flex-wrap: wrap; gap: 5px; }

/* 操作列：主操作可压缩（忙碌文案变长时用省略号收尾），菜单按钮固定宽度 */
.pit-ops { display: flex; align-items: center; gap: 6px; min-width: 0; }
.pit-ops .pit-main {
  flex: 1 1 auto; min-width: 0; padding: 0 8px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 操作列右侧留白收窄一档：120px 要同时放「建索引」与⋯触发按钮，按全局 11px 会差几像素 */
td.ops { padding-right: 8px; padding-left: 6px; }
.pit-ops :deep(.menu-root) { flex: 0 0 auto; display: inline-flex; }

/* 底部：左计数、右页码与每页条数 */
.pit-foot {
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
  padding: 8px 14px; border-top: 1px solid var(--divider);
}
.pit-count { white-space: nowrap; }
.pit-gap { padding: 0 4px; color: var(--text-faint); font-size: calc(12px * var(--ts, 1)); }
.pit-size { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.pit-size-sel { width: 70px; height: var(--control-h-sm); font-size: calc(12px * var(--ts, 1)); }

/* 列可见性全部按**卡片宽度**分档（容器查询，锚点是 .pit 的 container-type）：
   ≥780 全列；620~780 让位「最近索引」；520~620 再让位「索引版本」；<520 只留
   论文 / 状态 / chunks / 操作，再窄就由 .tbl-wrap 横向滚动兜底。
   th 与 td 共用同一批类名，所以一条规则同时命中表头与表体。 */
@container (max-width: 779px) {
  .col-indexed { display: none; }
}
@container (max-width: 619px) {
  .col-ver { display: none; }
}

/* 窄视口：检索控件与页码区不再强行同排 */
@media (max-width: 860px) {
  .pit-foot { gap: 6px; }
}

/* 560：搜索框独占整行（排在最后），其它按钮各自可换行 */
@media (max-width: 560px) {
  .pit-head { row-gap: 6px; }
  .pit-search { order: 9; width: 100%; }
  .pit-search input { width: 100%; }
  .pit-filter { width: 118px; }
  .pit-foot { gap: 6px 8px; }
  .pit-ops .pit-main { padding: 0 7px; }
}
</style>
