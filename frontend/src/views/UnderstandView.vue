<script setup lang="ts">
/**
 * 理解页（`/understand/:paperId`）：论文解读的**整页版**，与阅读器右侧悬浮面板同一套结构。
 *
 * 为什么这页还留着：理解是**全篇**视角（方法链条、阅读顺序、相关概念都是跨页的），
 * 阅读器面板只够"瞟一眼"。但"内容怎么组织"两边必须一致 —— 前端侧**没有"两路"这个概念**
 * （用户口径），所以这里也不再是 A/B 并排，而是按阅读顺序拼成三块：
 * 「论文精读」← `a.markdown` → 「相关概念」← `b.concepts` → 「怎么读这篇论文」← `b.markdown`。
 * 缺哪块就不渲染哪块，并用一行淡色说明讲清楚（不报错、不留白）；`label`
 * （"技术解剖（英文侧）"这类字样）不显示。
 *
 * 引用短码只做**纯文本**：`/related` 与 chunk 跳转是给知识库用的（用户口径），
 * 所以原来右侧那个「引用来源（点一下跳到原文）」的胶囊栏整块下线 —— 引用本身仍在正文里，
 * 概念卡上的短码也保留（可选中、可复制，tooltip 里带 chunk_id / 类型 / 页码供核对）。
 *
 * 轮询口径与悬浮面板一致：**有理解任务在跑时才轮询**（没有任务还一直打接口是白耗），
 * 5s 一轮、48 轮上限。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import { api } from '@/api/client';
import { mdToHtml } from '@/composables/useMarkdown';
import { fmtRelative } from '@/utils/format';
import { isTaskRunning } from '@/utils/tasks';
import { confirmRegenerateInsight } from '@/utils/understanding';
import { loadPapers, state } from '@/stores/app';
import type { TaskInfo, UnderstandingConcept, UnderstandingPayload } from '@/api/types';

/** `GET /papers/{id}/understanding` 的整包形状（与阅读器面板同一份契约，一次请求拿全）。 */
interface UnderstandingBundle {
  paper_id?: string;
  a?: UnderstandingPayload | null;
  b?: UnderstandingPayload | null;
  done?: string[];
}

const route = useRoute();
const paperId = computed(() => String(route.params.paperId ?? ''));

const bundle = ref<UnderstandingBundle>({ a: null, b: null });
const loading = ref(false);
const starting = ref(false);
const error = ref('');
/** 轮询超过上限后置位：让用户点「刷新」看结果，而不是一直等 */
const timedOut = ref(false);
/** 本页刚发起过解读：任务列表还没反映出来时也要继续轮询 */
const started = ref(false);
/** 任务列表里有没有理解任务在跑 —— 轮询只看它（口径：只在有任务时轮询） */
const running = ref(false);

const paper = computed(() => (state.papers ?? []).find((p) => p.id === paperId.value));
const pathA = computed(() => bundle.value.a ?? null);
const pathB = computed(() => bundle.value.b ?? null);

const readMarkdown = computed(() => String(pathA.value?.markdown ?? ''));
const roadmapMarkdown = computed(() => String(pathB.value?.markdown ?? ''));
const readHtml = computed(() => (readMarkdown.value ? mdToHtml(readMarkdown.value) : ''));
const roadmapHtml = computed(() => (roadmapMarkdown.value ? mdToHtml(roadmapMarkdown.value) : ''));

/** 相关概念（B 路独有字段；模型没按小节写时是空数组） */
const concepts = computed<UnderstandingConcept[]>(() => (
  Array.isArray(pathB.value?.concepts) ? pathB.value!.concepts! : []
).filter((c) => Boolean(c && (c.name || c.body_md))));

const hasAny = computed(() => Boolean(
  readMarkdown.value || roadmapMarkdown.value || concepts.value.length,
));
const doneBoth = computed(() => Boolean(readMarkdown.value && roadmapMarkdown.value));

/** 缺块的淡色说明（只有一块结果时最容易被当成"坏了"，所以明说缺哪块） */
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

/** 引用核对：某一块的来源短码有对不上的就提醒（文案里不再出现"这一路"） */
const invalidCodes = computed(() => [
  ...(pathA.value?.invalid_refs ?? []), ...(pathB.value?.invalid_refs ?? []),
]);
const missingConceptRefs = computed(() => Boolean(pathB.value?.missing_concept_refs) && !concepts.value.length);

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

/** 引用短码的 tooltip：短码之外把 chunk id / 类型 / 页码摊开（核对来源时要用） */
function refTitle(c: UnderstandingConcept, code: string): string {
  const r = (c.refs ?? []).find((x) => x.code === code);
  if (!r) return code;
  return [r.code, r.chunk_id, r.chunk_type, r.page ? `p${r.page}` : ''].filter(Boolean).join(' · ');
}

/* ------------------------------ 载入 ------------------------------ */
async function load(opts: { silent?: boolean } = {}) {
  if (!opts.silent) loading.value = true;
  try {
    const d = await api.get<UnderstandingBundle>(`/papers/${paperId.value}/understanding`);
    bundle.value = { a: d?.a ?? null, b: d?.b ?? null, done: d?.done ?? [] };
    error.value = '';
  } catch (err) {
    // 静默轮询的失败不覆盖已有结果：网络抖一下不该让整页变空
    if (!opts.silent) error.value = (err as Error)?.message ?? String(err);
  } finally {
    loading.value = false;
  }
}

/** 任务列表：判断"现在到底有没有理解任务在跑"（轮询与提示都看它）。
 *  ⚠️ 这里**不自己排下一轮轮询**：排期只由 `schedule()` 与 `watch(running)` 负责，
 *  否则"拉任务"和"拉结果"两条路都去排期，轮次上限会被绕过（定时器互相盖住，`rounds` 就不准了）。 */
async function loadTasks() {
  try {
    const d = await api.get<{ items: TaskInfo[] }>('/tasks', { paper_id: paperId.value, limit: 20 });
    running.value = (d.items ?? []).some((t) => (
      t.type === 'UNDERSTAND_A' || t.type === 'UNDERSTAND_B'
    ) && isTaskRunning(t));
  } catch {
    running.value = false;
  }
}

/* ------------------------------ 轮询 ------------------------------ */
const POLL_MS = 5000;
const POLL_MAX = 48;
let timer: number | undefined;
let rounds = 0;

function stopPoll() {
  if (timer !== undefined) { window.clearTimeout(timer); timer = undefined; }
}
function shouldPoll(): boolean {
  return !doneBoth.value && !timedOut.value && (running.value || started.value);
}
function schedule() {
  if (timer !== undefined || !shouldPoll()) return;
  timer = window.setTimeout(async () => {
    timer = undefined;
    rounds += 1;
    await Promise.all([load({ silent: true }), loadTasks()]);
    if (doneBoth.value) { started.value = false; rounds = 0; return; }
    if (rounds >= POLL_MAX) { timedOut.value = true; return; }
    schedule();
  }, POLL_MS);
}

/* 任务在跑 → 开始轮询；任务收尾 → 已经有结果就停手，什么都没有就再等一会儿（列表可能慢一拍） */
watch(running, (r) => {
  if (r) {
    started.value = true;
    rounds = 0;
    timedOut.value = false;
    schedule();
    return;
  }
  if (hasAny.value) { started.value = false; stopPoll(); return; }
  schedule();
});

/**
 * 「开始解读」（`force=false`，后端幂等）/「重新生成解读」（`force=true`，先二次确认）。
 *
 * 失败一律**原样**显示后端 `error.message`：最常见的 400 是那句可执行的人话
 * "这篇论文还没有可理解的 chunk：先建索引（知识库页）再来理解"，包一层前缀会把它淹掉。
 */
async function start(force = false) {
  if (force && !(await confirmRegenerateInsight(paperId.value, paper.value?.title))) return;
  starting.value = true;
  error.value = '';
  timedOut.value = false;
  try {
    await api.post(`/papers/${paperId.value}/understand`, { paths: ['a', 'b'], force });
    started.value = true;
    rounds = 0;
    await loadTasks();
    schedule();
  } catch (err) {
    error.value = (err as Error)?.message ?? String(err);
  } finally {
    starting.value = false;
  }
}

onMounted(async () => {
  if (!(state.papers ?? []).length) void loadPapers({ limit: 200 }).catch(() => {});
  await load();
  await loadTasks();
  schedule();
});
onUnmounted(() => stopPoll());
watch(paperId, () => {
  bundle.value = { a: null, b: null };
  filter.value = '';
  started.value = false;
  timedOut.value = false;
  rounds = 0;
  stopPoll();
  void load();
  void loadTasks().then(() => schedule());
});
</script>

<template>
  <div class="understand">
    <header class="ug-head">
      <div class="ug-title">
        <h1>{{ paper?.title || paperId }}</h1>
        <p class="muted tiny">
          论文解读（第二阶段）· 按阅读顺序拼成一篇：<strong>论文精读</strong> →
          <strong>相关概念</strong> → <strong>怎么读这篇论文</strong>。
          小节由模型按论文内容决定；引用以 chunk 为单位，按纯文本保留（可选中、可复制）。
          <template v-if="updatedAt"> · 更新于 {{ fmtRelative(updatedAt) }}</template>
          <template v-if="model"> · {{ model }}</template>
        </p>
      </div>
      <span class="spacer" />
      <button class="btn sm ghost" :disabled="loading" @click="load()">
        <AppIcon name="refresh" :size="14" />刷新
      </button>
      <!-- 有解读 = 重跑（先二次确认）；没有 = 第一次跑（不带 force，后端幂等） -->
      <button v-if="hasAny" class="btn sm" :disabled="starting" @click="start(true)">
        <AppIcon name="sparkle" :size="14" />{{ starting ? '已发起…' : '重新生成解读' }}
      </button>
      <button v-else class="btn sm primary" :disabled="starting" @click="start(false)">
        <AppIcon name="sparkle" :size="14" />{{ starting ? '已发起…' : '开始解读' }}
      </button>
    </header>

    <div v-if="error" class="banner err">
      <div>{{ error }}</div>
      <!-- 只在"确实卡在前置条件"时补这句：网络抖动也挂一句"先建索引"会把人引到错的方向 -->
      <div v-if="/chunk|索引|index/i.test(error)" class="tiny">
        前置条件：这篇论文要先建过知识库索引（解读读的是 chunk，不是页图）。
      </div>
    </div>
    <div v-else-if="running || started" class="banner info">
      解读任务在跑（两路并发，实测 1~2 分钟），结果会自动刷新。
    </div>
    <div v-if="timedOut" class="banner warn">
      已等待 4 分钟仍没拿到完整结果，任务可能还在跑 —— 点右上角「刷新」。
    </div>

    <div v-if="loading && !hasAny" class="card empty-block">
      <AppIcon name="sparkle" :size="18" />
      <p class="muted">正在读取解读…</p>
    </div>

    <!-- 还没解读：空状态也要能往下走（与悬浮面板同一套空状态口径） -->
    <div v-else-if="!hasAny" class="card empty-block">
      <AppIcon name="sparkle" :size="18" />
      <p class="muted">这篇论文还没有解读。</p>
      <p class="tiny faint">
        前置条件：这篇论文要先建过知识库索引（解读读的是 chunk，不是页图）。
        如果还没建，先去知识库页对这篇论文建索引，再回来点「开始解读」。
      </p>
    </div>

    <template v-else>
      <!-- 缺一块时的淡色说明：不报错、不留白 -->
      <div v-if="missingHint" class="banner info tiny">{{ missingHint }}</div>
      <div v-if="invalidCodes.length" class="banner err">
        <strong>引用不可信</strong>：{{ invalidCodes.length }} 条来源短码不在清单里（{{ invalidCodes.join(', ') }}）。
        内容仍然可用，但"来源"要自行核对。
      </div>
      <div v-else-if="missingConceptRefs" class="banner warn">
        「相关概念」一节里没有找到 chunk 引用 —— 相关概念按口径必须给出来源短码。
      </div>

      <!-- ① 论文精读 -->
      <section v-if="readHtml" class="card ug-sec">
        <div class="card-head">
          <AppIcon name="layers" :size="15" />论文精读
          <span class="spacer" />
          <span class="tiny faint nowrap">
            {{ readMarkdown.length }} 字 · 引用 {{ pathA?.refs?.length ?? 0 }} 条
          </span>
        </div>
        <div class="card-body md" v-html="readHtml" />
      </section>

      <!-- ② 相关概念（B 路独有字段；空就不渲染，不摆空壳） -->
      <section v-if="concepts.length" class="card ug-sec">
        <div class="card-head">
          <AppIcon name="quote" :size="15" />相关概念
          <span class="badge dim">{{ conceptRows.length }}<template v-if="filter">/{{ concepts.length }}</template></span>
          <span class="spacer" />
          <input
            v-model="filter"
            class="ug-filter"
            placeholder="过滤概念"
            title="按中英文名或解释内容过滤"
          >
        </div>
        <div class="card-body">
          <article v-for="r in conceptRows" :key="r.key" class="concept">
            <div class="concept-head">
              <span class="concept-name">{{ r.c.name }}</span>
              <span v-if="r.c.name_en" class="faint concept-en">{{ r.c.name_en }}</span>
            </div>
            <div v-if="r.c.body_md" class="md" v-html="mdToHtml(r.c.body_md)" />
            <!-- 来源短码：**纯文本**（可选中 / 可复制），不再是点一下跳页的胶囊 -->
            <div v-if="r.c.refs?.length" class="refs">
              <span
                v-for="ref in r.c.refs"
                :key="ref.code + ref.chunk_id"
                class="ref-text"
                :title="refTitle(r.c, ref.code)"
              >来源 Chunk: {{ ref.code }}</span>
            </div>
          </article>
          <div v-if="!conceptRows.length" class="muted small">没有匹配「{{ filter }}」的概念。</div>
        </div>
      </section>

      <!-- ③ 怎么读这篇论文（B 路 markdown：内部自带「相关概念」小节，照常渲染、不删节） -->
      <section v-if="roadmapHtml" class="card ug-sec">
        <div class="card-head">
          <AppIcon name="document" :size="15" />怎么读这篇论文
          <span class="spacer" />
          <span class="tiny faint nowrap">
            {{ roadmapMarkdown.length }} 字 · 引用 {{ pathB?.refs?.length ?? 0 }} 条
          </span>
        </div>
        <div class="card-body md" v-html="roadmapHtml" />
      </section>
    </template>
  </div>
</template>

<style scoped>
.understand { max-width: 1180px; margin: 0 auto; padding: 18px 20px 64px; }
.ug-head { display: flex; align-items: flex-end; gap: 10px; margin-bottom: 12px; }
.ug-title h1 { font-size: 19px; margin: 0 0 4px; }
.ug-title p { margin: 0; line-height: 1.6; max-width: 860px; }
.banner { padding: 9px 11px; border-radius: var(--radius-sm); font-size: 12.5px; margin-bottom: 10px; line-height: 1.55; }
.banner.err { background: color-mix(in srgb, var(--danger, #c0392b) 12%, transparent); }
.banner.warn { background: color-mix(in srgb, var(--warn, #b8860b) 12%, transparent); }
.banner.info { background: var(--accent-soft); }
.empty-block { padding: 22px; text-align: center; display: flex; flex-direction: column; gap: 6px; align-items: center; }
/* 三块内容：卡片之间留一个间距（整页比悬浮面板宽，不必再画分割线） */
.ug-sec { margin-bottom: 12px; }
.ug-sec .card-body { line-height: 1.75; overflow-wrap: anywhere; }
.ug-filter {
  height: 24px; padding: 0 8px; font-size: 12px; font-weight: 400;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg); color: var(--text);
}
/* 概念卡：标题一行 + 解释 + 纯文本来源短码 */
.concept { padding: 9px 0; border-bottom: 1px solid var(--divider); }
.concept:last-child { border-bottom: 0; padding-bottom: 0; }
.concept-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.concept-name { font-size: 13px; font-weight: 600; }
.concept-en { font-size: 11.5px; }
.refs { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
/* 来源短码：只是**文字**（等宽、淡色，可选中复制）—— 任何"像按钮"的视觉都在暗示它能点，
   而它已经不能跳页了（用户口径：引用保留纯文本）。 */
.ref-text { font-size: 11px; font-family: var(--mono); color: var(--text-dim); user-select: text; cursor: text; }
</style>
