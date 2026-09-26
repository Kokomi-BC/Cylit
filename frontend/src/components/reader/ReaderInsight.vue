<script setup lang="ts">
/**
 * 解读面板正文：把第二阶段「论文理解」的两份产物拼成**一篇**给读者看。
 *
 * 它挂在**左栏「解读」页签**里（`ReaderSidePanel` 渲染）：一份解读里同时含「论文精读」与
 * 「怎么读这篇论文」，所以"解读"与"阅读地图"共用一个页签就够了（用户口径第二轮返工把
 * 右侧悬浮面板撤销了）。进这个页签时 ReaderView 会自动把左栏放宽 —— 栏太窄时这些内容
 * （小标题、概念卡、表格）会被挤成竖排；想通读长解读可以点页签右上角那枚图标按钮 → 整页版。
 *
 * 三条口径（都是用户原话）：
 * - **前端没有"两路"这个概念**：A/B 是后端的实现切分（A 英文侧技术解剖、B 中文侧阅读地图），
 *   界面只按阅读顺序拼成三块 ——「论文精读」→「相关概念」→「怎么读这篇论文」；
 *   所以这里没有 A/B 切换控件，也不渲染 `label`（"技术解剖（英文侧）"这类字样只在理解页里说）；
 * - 每块都可能缺（模型这次只产出了其中一路）：缺哪块就不渲染哪块，并用**一行淡色说明**讲清楚，
 *   不报错、不留白；
 * - 「相关概念」取 B 路的 `concepts`（**只有 B 路有这个字段**）；块内 `（来源 Chunk: B023）`
 *   只做**纯文本**（可选中、可复制），不再是可点胶囊 —— 那个跳转接口是给知识库显示相关知识用的，
 *   在阅读器里点一下会把读者当前位置弄丢（用户口径，见 R3）。
 *
 * B 路 Markdown 内部自带「相关概念」小节：**照常渲染、不做删节**（它是模型写的一篇连贯文章，
 * 按小节切掉只会留下断头话），所以第 3 块里再出现一次概念是预期内的。
 */
import { computed, onUnmounted, ref, watch } from 'vue';
import { api } from '@/api/client';
import { mdToHtml } from '@/composables/useMarkdown';
import { fmtRelative } from '@/utils/format';
import type { UnderstandingConcept, UnderstandingPayload } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';

const props = defineProps<{
  paperId: string;
  /**
   * 这篇论文的解读任务在跑（ReaderView 按任务列表算出来）。
   *
   * 为什么由父组件告诉它：解读是后台任务（实测 1~2 分钟一次全篇调用），前端轮询是必要的；
   * 但"没有任务也在每 5 秒打一次接口"是白耗 —— 只有确实有任务在跑时才轮询（用户口径）。
   */
  taskRunning: boolean;
}>();

const emit = defineEmits<{
  /** 本组件刚发起过解读任务：父组件立刻刷一次任务列表（否则它不知道有任务在跑了） */
  (e: 'queued'): void;
}>();

/**
 * `GET /papers/{id}/understanding` 的整包形状。
 *
 * 一次请求拿全（后端整包返回 `{paper_id, a, b, done}`，`a`/`b` 是单路对象），
 * 不再像以前那样按 `?path=` 请求两遍：两遍的返回形状还依赖后端是否带 `concepts`，
 * 归一化逻辑本身就是一类 bug 的来源（整包时这里直接取字段，没有二义性）。
 */
interface UnderstandingBundle {
  paper_id?: string;
  a?: UnderstandingPayload | null;
  b?: UnderstandingPayload | null;
  /** 已经生成过的那几路（`['a','b']`）；只在"缺哪块"的说明里用得上 */
  done?: string[];
}

const bundle = ref<UnderstandingBundle>({ a: null, b: null });
const loading = ref(false);
const starting = ref(false);
const error = ref('');
/** 轮询超过上限后置位：文案让用户点「刷新」看结果，而不是一直转圈 */
const timedOut = ref(false);
/** 这次「开始解读」发起过没（空状态里那句"已发起…"用） */
const started = ref(false);

const pathA = computed(() => bundle.value.a ?? null);
const pathB = computed(() => bundle.value.b ?? null);

/** 第 1 块：A 路 markdown = 论文精读（基本信息 / 背景 / 动机 / 贡献 / 方法 / 实验 / 结论） */
const readMarkdown = computed(() => String(pathA.value?.markdown ?? ''));
/** 第 3 块：B 路 markdown = 怎么读这篇论文（阅读地图，内部自带「相关概念」小节） */
const roadmapMarkdown = computed(() => String(pathB.value?.markdown ?? ''));
const readHtml = computed(() => (readMarkdown.value ? mdToHtml(readMarkdown.value) : ''));
const roadmapHtml = computed(() => (roadmapMarkdown.value ? mdToHtml(roadmapMarkdown.value) : ''));

/** 第 2 块：相关概念（B 路独有字段；空数组 = 模型这次没按小节写） */
const concepts = computed<UnderstandingConcept[]>(() => (
  Array.isArray(pathB.value?.concepts) ? pathB.value!.concepts! : []
).filter((c) => Boolean(c && (c.name || c.body_md))));

const hasAny = computed(() => Boolean(
  readMarkdown.value || roadmapMarkdown.value || concepts.value.length,
));
/** 两路都到齐了：轮询的唯一终点（只有一路的论文不该被无限轮询） */
const doneBoth = computed(() => Boolean(readMarkdown.value && roadmapMarkdown.value));

/**
 * 缺块的淡色说明。
 *
 * 只有一截结果时最容易被当成"坏了"（界面上一半内容不见了），所以这里**明说**缺哪块、
 * 以及"点刷新还能再看一次"，而不是留白让用户猜。
 */
const missingHint = computed(() => {
  const noRead = !readMarkdown.value;
  const noRoadmap = !roadmapMarkdown.value;
  if (!noRead && !noRoadmap) return '';
  if (noRead && !noRoadmap) return '还没有生成「论文精读」，先看下面的阅读地图与相关概念（点右上角刷新可再看一次）。';
  if (noRoadmap) return '还没有生成「怎么读这篇论文」，先看「论文精读」也一样（点右上角刷新可再看一次）。';
  return '';
});

const updatedAt = computed(() => String(pathA.value?.updated_at ?? pathB.value?.updated_at ?? ''));
const model = computed(() => String(pathA.value?.model ?? pathB.value?.model ?? ''));

/**
 * 引用核对结果（与理解页同一口径）：某一路的短码有对不上的，就提醒一句来源要自行核对。
 * 文案里**不再出现"这一路"** —— 前端没有"路"这个概念（用户口径）。
 */
const refsWarn = computed(() => {
  const bad = [pathA.value, pathB.value].filter((p) => p && p.refs_ok === false);
  if (!bad.length) return '';
  const n = bad.reduce((sum, p) => sum + (p?.invalid_refs?.length ?? 0), 0);
  return `引用核对：${n} 条来源短码不在清单里，来源请自行核对。`;
});

/* ------------------------------ 载入 ------------------------------ */
async function load(opts: { silent?: boolean } = {}) {
  if (!opts.silent) loading.value = true;
  try {
    const res = await api.get<UnderstandingBundle>(`/papers/${props.paperId}/understanding`);
    bundle.value = { a: res?.a ?? null, b: res?.b ?? null, done: res?.done ?? [] };
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
 * 理解是一次全篇调用（实测 1~2 分钟），所以建完任务要轮询。
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

/**
 * 该不该继续轮询：**有任务在跑**（父组件给的 `taskRunning`，或本组件刚发起过一次）
 * 而且还没拿到完整结果。任务一结束就不再打接口（用户口径：只在有任务时轮询）。
 */
function shouldPoll(): boolean {
  return !doneBoth.value && !timedOut.value && (props.taskRunning || started.value);
}

function schedule() {
  if (timer !== undefined || !shouldPoll()) return;
  timer = window.setTimeout(async () => {
    timer = undefined;
    rounds += 1;
    await load({ silent: true });
    if (doneBoth.value) { started.value = false; rounds = 0; return; }
    if (rounds >= POLL_MAX) { timedOut.value = true; return; }
    schedule();
  }, POLL_MS);
}

/* 任务在跑 → 从这一轮开始轮询；任务收尾 → 已经有结果就停手（继续问只是白打接口），
   什么都还没拿到就再等一会儿（任务列表可能只是慢了一拍）。 */
watch(() => props.taskRunning, (running) => {
  if (running) {
    started.value = true;
    rounds = 0;
    timedOut.value = false;
    schedule();
    return;
  }
  if (hasAny.value) { started.value = false; stopPoll(); return; }
  schedule();
});

async function start() {
  starting.value = true;
  error.value = '';
  timedOut.value = false;
  try {
    // 不带 force：后端幂等，已经有结果的那一路不会重跑（重跑走工具栏「⋯」里的「重新生成解读」）
    await api.post(`/papers/${props.paperId}/understand`, { paths: ['a', 'b'] });
    started.value = true;
    rounds = 0;
    emit('queued');
    schedule();
  } catch (err) {
    // 最常见的失败是"还没有可理解的 chunk"（后端会给出这句人话），原样显示，别再包一层
    error.value = (err as Error)?.message ?? String(err);
  } finally {
    starting.value = false;
  }
}

/* ------------------------------ 相关概念 ------------------------------ */

const filter = ref('');

function matchFilter(c: UnderstandingConcept): boolean {
  const q = filter.value.trim().toLowerCase();
  if (!q) return true;
  return [c.name, c.name_en, c.body_md].some((s) => String(s ?? '').toLowerCase().includes(q));
}

const conceptRows = computed<Array<{ key: string; c: UnderstandingConcept }>>(() => concepts.value
  .filter(matchFilter)
  .map((c) => ({ key: String(c.name || c.name_en || ''), c }))
  .filter((r) => r.key));

/** 概念默认**收起**：一屏摊开 8 段解释会变成一堵墙，名字先给读者扫一遍 */
const expanded = ref<Record<string, boolean>>({});
const isOpen = (key: string) => Boolean(expanded.value[key]);
function toggle(key: string) { expanded.value = { ...expanded.value, [key]: !expanded.value[key] }; }

/** 引用短码的 tooltip：短码之外把 chunk id / 类型 / 页码摊开（核对来源时要用） */
function refTitle(c: UnderstandingConcept, code: string): string {
  const r = (c.refs ?? []).find((x) => x.code === code);
  if (!r) return code;
  return [r.code, r.chunk_id, r.chunk_type, r.page ? `p${r.page}` : ''].filter(Boolean).join(' · ');
}

watch(() => props.paperId, () => {
  bundle.value = { a: null, b: null };
  expanded.value = {};
  filter.value = '';
  started.value = false;
  timedOut.value = false;
  rounds = 0;
  stopPoll();
  void load();
});

void load();
onUnmounted(() => { stopPoll(); });
</script>

<template>
  <div class="insight">
    <div class="row tiny ins-head">
      <span class="muted">解读结果</span>
      <span v-if="updatedAt" class="faint">{{ fmtRelative(updatedAt) }}</span>
      <span class="spacer" />
      <span v-if="model" class="faint truncate ins-model" :title="model">{{ model }}</span>
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
      已等待 4 分钟仍没拿到完整结果，任务可能还在跑 —— 稍后点右上角刷新。
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
      <!-- 发起后要一次全篇调用（实测 1~2 分钟）：说清楚"已经开始了、会自动刷"，
           否则界面还是那四个字，看起来像没点动 -->
      <div v-if="started" class="tiny faint ins-note">已发起解读，会自动刷新。</div>
    </div>

    <template v-else>
      <!-- 缺一块时的淡色说明：不报错、不留白（见 missingHint） -->
      <div v-if="missingHint" class="tiny faint ins-hint">{{ missingHint }}</div>
      <div v-if="refsWarn" class="tiny ins-warn">{{ refsWarn }}</div>

      <!-- ① 论文精读 -->
      <section v-if="readHtml" class="ins-sec">
        <div class="ins-sec-title">论文精读</div>
        <div class="small md ins-md" v-html="readHtml" />
      </section>

      <!-- ② 相关概念（B 路独有字段；模型没按小节写就整块不渲染，不摆一个空壳） -->
      <section v-if="concepts.length" class="ins-sec">
        <div class="row tiny ins-sec-title">
          <span>相关概念</span>
          <span class="badge dim">{{ conceptRows.length }}<template v-if="filter">/{{ concepts.length }}</template></span>
          <span class="spacer" />
          <input
            v-model="filter"
            class="ins-filter"
            placeholder="过滤概念"
            title="按中英文名或解释内容过滤"
          >
        </div>
        <div
          v-for="r in conceptRows"
          :key="r.key"
          class="item concept"
        >
          <button class="concept-head" @click="toggle(r.key)">
            <span class="concept-name truncate" :title="r.c.name">{{ r.c.name }}</span>
            <span v-if="r.c.name_en" class="faint truncate concept-en" :title="r.c.name_en">
              {{ r.c.name_en }}
            </span>
            <span class="spacer" />
            <AppIcon :name="isOpen(r.key) ? 'chevron-up' : 'chevron-down'" :size="13" />
          </button>
          <div v-if="isOpen(r.key)" class="concept-body">
            <div v-if="r.c.body_md" class="small md ins-md" v-html="mdToHtml(r.c.body_md)" />
            <!-- 来源短码：**纯文本**（可选中 / 可复制），不再是点一下跳页的胶囊（R3） -->
            <div v-if="r.c.refs?.length" class="refs">
              <span
                v-for="ref in r.c.refs"
                :key="ref.code + ref.chunk_id"
                class="ref-text"
                :title="refTitle(r.c, ref.code)"
              >来源 Chunk: {{ ref.code }}</span>
            </div>
          </div>
        </div>
        <div v-if="!conceptRows.length" class="empty small">没有匹配「{{ filter }}」的概念</div>
      </section>

      <!-- ③ 怎么读这篇论文（B 路 markdown：它内部自带「相关概念」小节，照常渲染、不删节） -->
      <section v-if="roadmapHtml" class="ins-sec">
        <div class="ins-sec-title">怎么读这篇论文</div>
        <div class="small md ins-md" v-html="roadmapHtml" />
      </section>
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
/* 页签现在可能只有 220px 宽（左栏下限）：头部与各块的小标题行都允许折行，
   否则「解读结果 + 时间 + 模型 + 刷新」会横着顶破栏宽（进页签虽会自动变宽，但不能依赖它）。 */
.ins-head { gap: 6px; margin: 0 0 8px; flex-wrap: wrap; }
.ins-filter {
  min-width: 0; width: 120px; height: 22px; padding: 0 6px; font-size: 11.5px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg); color: var(--text);
}
.ins-note { margin: 4px 0 6px; line-height: 1.5; }
.ins-hint { margin-bottom: 8px; line-height: 1.5; }
.ins-warn { margin-bottom: 8px; color: var(--warn); }
.ins-model { max-width: 160px; }

/* 三块内容：块间用一条淡分割线分开（悬浮面板本来就窄，靠留白分块会看不出边界）；
   第一块之前不画线（上面是面板自己的头部）。 */
.ins-sec + .ins-sec { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--divider); }
.ins-sec-title {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  font-size: 12.5px; font-weight: 600; color: var(--text); margin: 0 0 6px;
}

/* 解读正文：块级元素间距压小一点（面板不算宽），公式可横向滚 */
.ins-md { line-height: 1.65; overflow-wrap: anywhere; }
.ins-md :deep(p) { margin: .4em 0; }
.ins-md :deep(h2), .ins-md :deep(h3), .ins-md :deep(h4) {
  font-size: 12.5px; margin: .8em 0 .3em; color: var(--text);
}
.ins-md :deep(ul), .ins-md :deep(ol) { margin: .3em 0; padding-left: 1.3em; }
.ins-md :deep(.katex-display) { margin: .3em 0; }

/* 概念卡：标题行整行可点（展开/收起） */
.concept { padding: 6px 8px; margin-bottom: 6px; }
.concept-head {
  display: flex; align-items: center; gap: 6px; width: 100%;
  border: 0; background: transparent; color: var(--text); font: inherit;
  padding: 0; cursor: pointer; text-align: left;
}
.concept-name { font-size: 12.5px; font-weight: 600; }
.concept-en { font-size: 11px; max-width: 45%; }
.concept-body { margin-top: 6px; }
.refs { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
/* 来源短码：只是**文字**（等宽、淡色，可选中复制），没有 hover 高亮、没有 pointer 光标 —— 
   任何"像按钮"的视觉都在暗示它可以点，而它已经不能跳页了 */
.ref-text {
  font-size: 11px; font-family: var(--mono); color: var(--text-dim);
  user-select: text; cursor: text;
}
</style>
