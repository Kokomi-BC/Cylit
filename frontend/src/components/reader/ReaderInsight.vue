<script setup lang="ts">
/**
 * 侧栏「解读」页签：把第二阶段「论文理解」接进阅读器（不再要用户跳到 `/understand/:paperId`）。
 *
 * 为什么放在侧栏而不是再来一个页面：解读是**读这篇论文时随时要瞟一眼**的东西
 * （这个概念是什么、该按什么顺序读），与"在阅读器里翻页"是同一个动作流；理解页仍然保留，
 * 负责全篇通读与引用核对。
 *
 * 三条口径直接落在这里：
 * - 默认看 **B 路（阅读地图）**：中文侧、给第一次读这篇论文的人（与理解页同一默认）；
 * - 切到 **A 路（技术解剖）** 只是换一份 Markdown + 一份 concepts，不重新请求整页；
 * - 「相关概念」跟着**当前阅读页**（`/papers/{id}/related?page=N`）走：当前页那几条置顶并高亮，
 *   其余照旧列在下面，可过滤 —— 接口失败就**静默**退回全篇概念列表（不能因为一个联动接口
 *   抖动就让解读区变空）。
 */
import { computed, onUnmounted, ref, watch } from 'vue';
import { api } from '@/api/client';
import { mdToHtml } from '@/composables/useMarkdown';
import { debounce, fmtRelative } from '@/utils/format';
import type { UnderstandingConcept, UnderstandingPayload, UnderstandingRelated } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';

const props = defineProps<{
  paperId: string;
  /** 阅读器算出来的**当前阅读页**（按滚动位置）：相关概念跟着它走 */
  page: number;
}>();

const emit = defineEmits<{
  (e: 'focus-page', page: number): void;
}>();

/** 两路的展示名与图标（与理解页同一套说法：A 技术解剖 / B 阅读地图） */
const PATHS = [
  { k: 'b' as const, label: '阅读地图', icon: 'layers', title: 'B 路：中文侧 · 相关概念 + 怎么读' },
  { k: 'a' as const, label: '技术解剖', icon: 'formula', title: 'A 路：英文侧 · 方法/证明/实验/复现' },
];
const path = ref<'a' | 'b'>('b');

const data = ref<Record<'a' | 'b', UnderstandingPayload | null>>({ a: null, b: null });
const loading = ref(false);
const starting = ref(false);
const error = ref('');
/** 轮询超过上限后置位：文案让用户点「刷新」看结果，而不是一直转圈 */
const timedOut = ref(false);
/** 这次「开始解读」发起过没（空状态里那句"已发起…"用） */
const started = ref(false);

const current = computed(() => data.value[path.value]);
const hasAny = computed(() => Boolean(data.value.a?.markdown || data.value.b?.markdown));
const currentHtml = computed(() => (current.value?.markdown ? mdToHtml(current.value.markdown) : ''));
const pathLabel = computed(() => PATHS.find((p) => p.k === path.value)?.label ?? path.value);

/**
 * 单路响应原样用；万一后端返回的是整包（`{a, b, done}`，理解页用的那个形状），取出对应那一路。
 *
 * 两种形状都认是有实测原因的：`?path=` 是阶段二新加的用法，而同一个接口不带 path 时返回整包；
 * 只认一种的话，另一形状会让整块面板显示成「还没有解读」（看起来像数据丢了）。
 */
function normalize(res: unknown, key: 'a' | 'b'): UnderstandingPayload | null {
  if (!res || typeof res !== 'object') return null;
  const obj = res as Record<string, any>;
  const one = ('markdown' in obj || 'concepts' in obj) ? obj : obj[key];
  return one && typeof one === 'object' ? (one as UnderstandingPayload) : null;
}

/**
 * 拉取两路理解结果。
 *
 * 为什么要**分两次带 `?path=` 取**：`concepts`（B 路相关概念）挂在单路响应上；
 * 整包响应是既有接口（理解页在用），不保证同样带 `concepts`。两次请求都很小（库里就两行 markdown）。
 */
async function load(opts: { silent?: boolean } = {}) {
  if (!opts.silent) loading.value = true;
  try {
    const [b, a] = await Promise.all([
      api.get(`/papers/${props.paperId}/understanding`, { path: 'b' }),
      api.get(`/papers/${props.paperId}/understanding`, { path: 'a' }),
    ]);
    data.value = { a: normalize(a, 'a'), b: normalize(b, 'b') };
    error.value = '';
  } catch (err) {
    // 静默轮询里的失败不覆盖上一次的结果，也不弹错：网络抖一下不该让解读区变空
    if (!opts.silent) error.value = (err as Error)?.message ?? String(err);
  } finally {
    loading.value = false;
  }
}

/* ------------------------------ 开始解读 + 轮询 ------------------------------ */

/**
 * 理解是一次全篇调用（实测 1~2 分钟，见理解页同处注释），所以建完任务要轮询。
 * 间隔 5s（口径要求 5~6s）；上限 48 轮 = 4 分钟（够跑完一次再留一点余量），到点停手并提示，
 * 不无限转下去。
 */
const POLL_MS = 5000;
const POLL_MAX = 48;
let timer: number | undefined;
let rounds = 0;

function stopPoll() {
  if (timer !== undefined) { window.clearTimeout(timer); timer = undefined; }
}

function poll(restart = false) {
  if (restart) rounds = 0;
  stopPoll();
  timer = window.setTimeout(async () => {
    rounds += 1;
    await load({ silent: true });
    if (data.value.a?.markdown && data.value.b?.markdown) { stopPoll(); timedOut.value = false; return; }
    if (rounds >= POLL_MAX) { stopPoll(); timedOut.value = true; return; }
    poll();
  }, POLL_MS);
}

async function start() {
  starting.value = true;
  error.value = '';
  timedOut.value = false;
  try {
    // 不带 force：后端幂等，已经有结果的那一路不会重跑（重跑要走理解页的「重新理解」）
    await api.post(`/papers/${props.paperId}/understand`, { paths: ['a', 'b'] });
    started.value = true;
    poll(true);
  } catch (err) {
    // 最常见的失败是"还没有可理解的 chunk"（后端会给出这句人话），原样显示，别再包一层
    error.value = (err as Error)?.message ?? String(err);
  } finally {
    starting.value = false;
  }
}

/* ------------------------------ 相关概念（跟随阅读页） ------------------------------ */

const related = ref<UnderstandingConcept[]>([]);
/** 当前页相关概念所在的页（接口回的 page，用来显示「当前页相关 · p7」） */
const relatedPage = ref(0);

/**
 * 页码变化 → 去抖 400ms 再问「这一页有哪些概念」。
 *
 * 去抖是必须的：滚动时页码会一格一格地变（阅读线扫过页头/页尾），不去抖就是一串并发请求，
 * 而它们回来时顺序不保证 —— 界面会闪回上一页的概念（用户要的是"跟着阅读位置"，不是抖）。
 */
const loadRelated = debounce(async (page: number) => {
  if (!page) return;
  try {
    const d = await api.get<UnderstandingRelated>(`/papers/${props.paperId}/related`, { page });
    related.value = Array.isArray(d?.concepts) ? d.concepts : [];
    relatedPage.value = Number(d?.page ?? page);
  } catch {
    // 静默退回"全篇概念列表"：不加高亮、不置顶、不报错（老后端没这个路由时也是这样）
    related.value = [];
    relatedPage.value = 0;
  }
}, 400);

watch(() => props.page, (p) => { void loadRelated(Number(p) || 0); }, { immediate: true });

const filter = ref('');
const conceptsAll = computed<UnderstandingConcept[]>(() => (
  Array.isArray(current.value?.concepts) ? current.value!.concepts! : []
).filter((c) => Boolean(c && (c.name || c.body_md))));

const conceptKey = (c: UnderstandingConcept) => String(c.name || c.name_en || '');

function matchFilter(c: UnderstandingConcept): boolean {
  const q = filter.value.trim().toLowerCase();
  if (!q) return true;
  return [c.name, c.name_en, c.body_md].some((s) => String(s ?? '').toLowerCase().includes(q));
}

/** 概念行：**当前页相关的置顶**（`/related` 那一份，可能与全篇清单是同名同一条），其余按原序 */
interface ConceptRow { key: string; c: UnderstandingConcept; pinned: boolean }

const rows = computed<ConceptRow[]>(() => {
  // `concepts` 为空时走"整段 markdown 当概念区"的兜底分支，这里不参与
  if (!conceptsAll.value.length) return [];
  const out: ConceptRow[] = [];
  const seen = new Set<string>();
  for (const c of related.value) {
    const key = conceptKey(c);
    if (!key || seen.has(key) || !matchFilter(c)) continue;
    seen.add(key);
    out.push({ key, c, pinned: true });
  }
  for (const c of conceptsAll.value) {
    const key = conceptKey(c);
    if (!key || seen.has(key) || !matchFilter(c)) continue;
    seen.add(key);
    out.push({ key, c, pinned: false });
  }
  return out;
});
const pinnedCount = computed(() => rows.value.filter((r) => r.pinned).length);

const expanded = ref<Record<string, boolean>>({});
/** 当前页相关的默认展开：否则"跟随阅读位置"只剩一个徽章，解释还得再点一次 */
const isOpen = (r: ConceptRow) => expanded.value[r.key] ?? r.pinned;
function toggle(r: ConceptRow) { expanded.value = { ...expanded.value, [r.key]: !isOpen(r) }; }

/** 引用胶囊的 tooltip：短码之外把 chunk id / 类型 / 页码都摊开（核对来源时要用） */
function refTitle(c: UnderstandingConcept, code: string): string {
  const r = (c.refs ?? []).find((x) => x.code === code);
  if (!r) return code;
  return [r.code, r.chunk_id, r.chunk_type, r.page ? `p${r.page}` : ''].filter(Boolean).join(' · ');
}

watch(() => props.paperId, () => {
  data.value = { a: null, b: null };
  related.value = [];
  expanded.value = {};
  filter.value = '';
  started.value = false;
  stopPoll();
  void load();
});

void load();
onUnmounted(() => { stopPoll(); loadRelated.cancel(); });
</script>

<template>
  <div class="insight">
    <!-- 路径切换：一个分段控件，不占地方（侧栏宽度本来就紧） -->
    <div class="row tiny ins-head">
      <div class="seg ins-paths">
        <button
          v-for="p in PATHS"
          :key="p.k"
          :class="{ active: path === p.k }"
          :title="p.title"
          @click="path = p.k"
        >
          <AppIcon :name="p.icon" :size="12" />{{ p.label }}
        </button>
      </div>
      <span class="spacer" />
      <button class="btn icon sm ghost" title="刷新解读结果" :disabled="loading" @click="load()">
        <AppIcon name="refresh" :size="13" />
      </button>
    </div>

    <div v-if="error" class="item err">
      <div class="tiny">{{ error }}</div>
      <!-- 只在"确实卡在前置条件"时补这句：网络抖动也挂一句"先建索引"会把人引到错的方向 -->
      <div v-if="/chunk|索引|index/i.test(error)" class="tiny faint">
        前置条件：这篇论文要先建过知识库索引（解读读的是 chunk，不是页图）。
      </div>
    </div>
    <div v-else-if="timedOut" class="tiny faint ins-hint">
      已等待 4 分钟仍没拿到两路结果，任务可能还在跑 —— 稍后点右上角刷新。
    </div>

    <!-- 还没解读：给一句人话 + 一个入口（口径：空状态也要能往下走） -->
    <div v-if="loading && !hasAny" class="empty small">正在读取解读…</div>
    <div v-else-if="!hasAny" class="item">
      <div class="tiny faint">还没有解读</div>
      <div class="tiny faint ins-note">
        解读读的是知识库的 chunk（不是页图），所以需要先对这篇论文建索引。
      </div>
      <button class="btn sm primary" :disabled="starting" @click="start">
        <AppIcon name="sparkle" :size="13" />{{ starting ? '已发起…' : '开始解读' }}
      </button>
      <!-- 发起后两路各要一次全篇调用（实测 1~2 分钟）：说清楚"已经开始了、会自动刷"，
           否则界面还是那四个字，看起来像没点动 -->
      <div v-if="started" class="tiny faint ins-note">已发起解读（两路并发），会自动刷新。</div>
    </div>

    <template v-else-if="hasAny">
      <!-- 这一路还没生成（另一路有）：同样是"还没有"，但说清楚是哪一路 -->
      <div v-if="!current?.markdown" class="item">
        <div class="tiny faint">这一路还没有解读</div>
        <button class="btn sm primary" :disabled="starting" @click="start">
          <AppIcon name="sparkle" :size="13" />{{ starting ? '已发起…' : '开始解读' }}
        </button>
      </div>

      <template v-else>
        <div class="item">
          <div class="row tiny">
            <span class="badge accent">{{ current?.label || pathLabel }}</span>
            <span v-if="current?.updated_at" class="faint">{{ fmtRelative(current?.updated_at) }}</span>
            <span class="spacer" />
            <span v-if="current?.model" class="faint truncate ins-model" :title="current?.model">
              {{ current?.model }}
            </span>
          </div>
          <!-- 引用核对结果：与理解页同一口径，只是这里压缩成一行（内容仍可用，只是来源要留意） -->
          <div v-if="current?.refs_ok === false" class="tiny ins-warn">
            引用不可信：{{ current?.invalid_refs?.length ?? 0 }} 条短码不在本路清单里，来源请自行核对。
          </div>
          <div class="small md ins-md" v-html="currentHtml" />
        </div>

        <div class="row tiny ins-head">
          <span class="muted">相关概念</span>
          <span v-if="conceptsAll.length" class="badge dim">{{ conceptsAll.length }}</span>
          <span class="spacer" />
          <input
            v-if="conceptsAll.length"
            v-model="filter"
            class="ins-filter"
            placeholder="过滤概念"
            title="按中英文名或解释内容过滤"
          >
        </div>

        <!-- concepts 为空（模型没按小节写）→ **把整段 markdown 当概念区展示**，不报错、不空着 -->
        <div v-if="!conceptsAll.length" class="item">
          <div class="tiny faint ins-note">模型没有按小节给出相关概念，这里按整段解读展示。</div>
          <div class="small md ins-md" v-html="currentHtml" />
        </div>

        <template v-else>
          <div v-if="pinnedCount" class="tiny ins-pin">
            <AppIcon name="locate" :size="11" />当前页相关 · p{{ relatedPage || page }}
          </div>
          <div
            v-for="r in rows"
            :key="r.key"
            class="item concept"
            :class="{ pinned: r.pinned }"
          >
            <button class="concept-head" @click="toggle(r)">
              <span class="concept-name truncate" :title="r.c.name">{{ r.c.name }}</span>
              <span v-if="r.c.name_en" class="faint truncate concept-en" :title="r.c.name_en">
                {{ r.c.name_en }}
              </span>
              <span v-if="r.pinned" class="badge accent nowrap">当前页</span>
              <span class="spacer" />
              <AppIcon :name="isOpen(r) ? 'chevron-up' : 'chevron-down'" :size="13" />
            </button>
            <div v-if="isOpen(r)" class="concept-body">
              <div v-if="r.c.body_md" class="small md ins-md" v-html="mdToHtml(r.c.body_md)" />
              <!-- refs 为空就不显示胶囊（口径：没有来源就不摆一排空壳） -->
              <div v-if="r.c.refs?.length" class="refs">
                <button
                  v-for="ref in r.c.refs"
                  :key="ref.code + ref.chunk_id"
                  class="ref-pill"
                  :disabled="!ref.page"
                  :title="refTitle(r.c, ref.code)"
                  @click="emit('focus-page', ref.page as number)"
                >
                  <AppIcon name="link" :size="11" />来源 Chunk: {{ ref.code }}
                </button>
              </div>
            </div>
          </div>
          <div v-if="!rows.length" class="empty small">没有匹配「{{ filter }}」的概念</div>
        </template>
      </template>
    </template>
  </div>
</template>

<style scoped>
.insight { display: flex; flex-direction: column; min-width: 0; }
/* 卡片：与 ReaderSidePanel 的 `.item` 同一观感。那一份是**那个组件的 scoped 样式**，
   跨组件拿不到（scoped 会给选择器加 data 属性），所以这里就地写一份同样的规则。 */
.item {
  border: 1px solid var(--border); border-left: 2px solid var(--border-strong);
  border-radius: var(--radius-sm); background: var(--bg-layer);
  padding: 8px 10px; margin-bottom: 8px;
}
.item.err { border-left-color: var(--danger); }
.ins-head { gap: 6px; margin: 0 0 8px; }
/* 路径分段：拉满可用宽度、两段等分并居中（与搜索页签的范围分段同一套写法） */
.ins-paths { display: flex; flex: 1; min-width: 0; }
.ins-paths button { flex: 1; justify-content: center; }
.ins-filter {
  min-width: 0; width: 96px; height: 22px; padding: 0 6px; font-size: 11.5px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg); color: var(--text);
}
.ins-note { margin: 4px 0 6px; line-height: 1.5; }
.ins-hint { margin-bottom: 8px; }
.ins-warn { margin-top: 4px; color: var(--warn); }
.ins-model { max-width: 120px; }
/* 解读正文：块级元素间距压小一点（侧栏窄，段间距大会被顶得很长），公式可横向滚 */
.ins-md { line-height: 1.62; overflow-wrap: anywhere; }
.ins-md :deep(p) { margin: .35em 0; }
.ins-md :deep(h2), .ins-md :deep(h3), .ins-md :deep(h4) {
  font-size: 12.5px; margin: .7em 0 .25em; color: var(--text);
}
.ins-md :deep(ul), .ins-md :deep(ol) { margin: .3em 0; padding-left: 1.3em; }
.ins-md :deep(.katex-display) { margin: .3em 0; }
.ins-pin { display: flex; align-items: center; gap: 4px; color: var(--accent-text); margin: 0 0 4px; }
/* 概念卡：标题行整行可点（展开/收起），当前页相关的用主题色左边框 + 徽章高亮 */
.concept { padding: 6px 8px; }
.concept.pinned { border-left-color: var(--accent); background: color-mix(in srgb, var(--accent) 6%, var(--bg-layer)); }
.concept-head {
  display: flex; align-items: center; gap: 6px; width: 100%;
  border: 0; background: transparent; color: var(--text); font: inherit;
  padding: 0; cursor: pointer; text-align: left;
}
.concept-name { font-size: 12.5px; font-weight: 600; }
.concept-en { font-size: 11px; max-width: 45%; }
.concept-body { margin-top: 6px; }
.refs { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
/* 引用胶囊：小圆角胶囊 + 等宽短码（与理解页的 ref-chip 同一外观语言，只是更小） */
.ref-pill {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 1px 7px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--border); background: var(--bg-hover);
  color: var(--text-dim); font-size: 10.5px; font-family: var(--mono);
}
.ref-pill:hover:not(:disabled) { border-color: var(--accent); color: var(--accent-text); }
.ref-pill:disabled { opacity: .5; cursor: default; }
</style>
