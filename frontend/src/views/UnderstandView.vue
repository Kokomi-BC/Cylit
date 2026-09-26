<script setup lang="ts">
/**
 * 理解页（`/understand/:paperId`）：把第二阶段的**两路理解结果**摆在一起看。
 *
 * 为什么单独一页而不是塞进阅读器：理解是**全篇**视角（方法链条、阅读顺序、相关概念都是跨页的），
 * 与"逐页对照精读"是两种不同的动作；阅读器那边只在侧栏给一个「相关知识」入口跳到本页对应的概念。
 *
 * 三条用户口径直接落在界面上：
 * - ⑤ 理解是第二阶段：顶部按钮明确"开始理解/重新理解"，与翻译分开；
 * - ⑦ 结果是自由 Markdown：**原样渲染**，不假设小节标题，页面上不出现任何"固定栏目"的空位；
 * - ④ chunk 是引用基准单位：底部把 `来源 Chunk: B023` 解析成**可点的 chunk 胶囊**，
 *   点一下跳到阅读器那一页（`?page=N`），引用不可信时明确标红（`refs_ok=false`）。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import { api } from '@/api/client';
import { mdToHtml } from '@/composables/useMarkdown';
import { loadPapers, state } from '@/stores/app';
import { openTab } from '@/stores/tabs';

/** 后端 `understanding` 结果（与 `understanding_service.to_dict` 一一对应） */
interface RefItem { code: string; chunk_id: string; chunk_type?: string | null; page?: number | null }
interface Understanding {
  paper_id: string;
  path: 'a' | 'b';
  label: string;
  markdown: string;
  refs: string[];
  refs_resolved: RefItem[];
  invalid_refs: string[];
  cross_road_refs: string[];
  refs_ok: boolean;
  missing_concept_refs: boolean;
  model?: string;
  updated_at?: string;
  prompt_tokens?: number;
  completion_tokens?: number;
  inventory_size?: number;
}
interface BothUnderstanding { a: Understanding | Record<string, never>; b: Understanding | Record<string, never> }

const route = useRoute();
const router = useRouter();
const paperId = computed(() => String(route.params.paperId ?? ''));

const data = ref<BothUnderstanding>({ a: {}, b: {} });
const loading = ref(false);
const starting = ref(false);
const path = ref<'a' | 'b'>('b');   // 默认看 B（中文阅读地图：第一次读论文的人更需要它）
const error = ref('');

const paper = computed(() => (state.papers ?? []).find((p) => p.id === paperId.value));
const current = computed<Understanding | null>(() => {
  const got = data.value[path.value];
  return got && (got as Understanding).markdown ? (got as Understanding) : null;
});
const html = computed(() => (current.value ? mdToHtml(current.value.markdown) : ''));

const PATHS = [
  { k: 'b' as const, label: '阅读地图', hint: '中文侧 · 相关概念 + 怎么读', icon: 'layers' },
  { k: 'a' as const, label: '技术解剖', hint: '英文侧 · 方法/证明/实验/复现', icon: 'formula' },
];

function done(p: 'a' | 'b') {
  return Boolean((data.value[p] as Understanding).markdown);
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    data.value = await api.get<BothUnderstanding>(`/papers/${paperId.value}/understanding`);
  } catch (err: any) {
    error.value = err?.message ?? String(err);
  } finally {
    loading.value = false;
  }
}

async function start(force = false) {
  starting.value = true;
  error.value = '';
  try {
    await api.post(`/papers/${paperId.value}/understand`, { paths: ['a', 'b'], force });
    // 理解是一次全篇调用（实测 1~2 分钟），先轮询着看结果
    void poll();
  } catch (err: any) {
    error.value = err?.message ?? String(err);
  } finally {
    starting.value = false;
  }
}

let timer: number | undefined;
function poll() {
  window.clearTimeout(timer);
  timer = window.setTimeout(async () => {
    await load();
    if (!done('a') || !done('b')) poll();
  }, 6000);
}

function openChunk(ref_: RefItem) {
  if (!ref_?.page) return;
  void router.push(openTab({
    name: 'reader', params: { paperId: paperId.value }, query: { page: String(ref_.page) },
  }) as never);
}

onMounted(async () => {
  if (!(state.papers ?? []).length) void loadPapers({ limit: 200 }).catch(() => {});
  await load();
  // 已经有任一路完成就先把不轮询；都没有才轮询（可能正在跑）
  if (!done('a') && !done('b')) poll();
});
onUnmounted(() => window.clearTimeout(timer));
watch(paperId, () => { void load(); });
</script>

<template>
  <div class="understand">
    <header class="ug-head">
      <div class="ug-title">
        <h1>{{ paper?.title || paperId }}</h1>
        <p class="muted tiny">
          论文理解（第二阶段）· 两路各做不同的事：<strong>阅读地图</strong>与<strong>技术解剖</strong>。
          结果不是固定模板 —— 小节由模型按论文内容决定；引用以 chunk 为单位，可点回原文。
        </p>
      </div>
      <span class="spacer" />
      <button class="btn sm ghost" :disabled="loading" @click="load">
        <AppIcon name="refresh" :size="14" />刷新
      </button>
      <button class="btn sm" :disabled="starting" @click="start(done('a') || done('b'))">
        <AppIcon name="play" :size="14" />{{ (done('a') || done('b')) ? '重新理解' : '开始理解' }}
      </button>
    </header>

    <div v-if="error" class="banner err">{{ error }}</div>
    <div v-if="starting" class="banner info">已创建理解任务（两路并发），结果会在 1~2 分钟内出现…</div>

    <div class="ug-tabs">
      <button
        v-for="p in PATHS"
        :key="p.k"
        class="ug-tab"
        :class="{ active: path === p.k }"
        @click="path = p.k"
      >
        <AppIcon :name="p.icon" :size="14" />
        <span class="ug-tab-label">{{ p.label }}</span>
        <span class="ug-tab-hint">{{ p.hint }}</span>
        <span class="badge" :class="done(p.k) ? 'ok' : 'dim'">{{ done(p.k) ? '已生成' : '未生成' }}</span>
      </button>
    </div>

    <div v-if="!current" class="card empty-block">
      <AppIcon name="layers" :size="18" />
      <p class="muted">这一路还没有理解结果。</p>
      <p class="tiny faint">
        前置条件：这篇论文**已经建过知识库索引**（理解读的是 chunk，不是页图）。
        如果还没建，先去知识库页对这篇论文建索引，再回来点「开始理解」。
      </p>
    </div>

    <template v-else>
      <div v-if="!current.refs_ok" class="banner err">
        <strong>引用不可信</strong>：{{ current.invalid_refs.length }} 条引用的短码不在本路清单里<template
          v-if="current.invalid_refs.length"
        >（{{ current.invalid_refs.join(', ') }}）</template>，另有
        {{ current.cross_road_refs.length }} 条跨路引用。内容仍然可用，但"来源"要自行核对。
      </div>
      <div v-else-if="current.missing_concept_refs" class="banner warn">
        「相关概念」一节里没有找到 chunk 引用 —— 相关概念按口径必须给出本路短码。
      </div>

      <div class="ug-body">
        <article class="card ug-md">
          <div class="card-head">
            <AppIcon name="layers" :size="15" />{{ current.label }}
            <span class="spacer" />
            <span class="tiny faint nowrap">
              {{ current.markdown.length }} 字 · 引用 {{ current.refs.length }} 条
              · 材料 {{ current.inventory_size ?? 0 }} 块<template v-if="current.model"> · {{ current.model }}</template>
            </span>
          </div>
          <div class="card-body md-body" v-html="html" />
        </article>

        <aside class="card ug-refs">
          <div class="card-head">
            <AppIcon name="quote" :size="15" />引用来源（点一下跳到原文）
          </div>
          <div class="card-body">
            <p v-if="!current.refs_resolved.length" class="muted small">这一路没有可解析的引用。</p>
            <button
              v-for="r in current.refs_resolved"
              :key="r.code"
              class="ref-chip"
              :title="r.chunk_id"
              @click="openChunk(r)"
            >
              <span class="ref-code">{{ r.code }}</span>
              <span class="ref-meta">{{ r.chunk_type }}<template v-if="r.page"> · p{{ r.page }}</template></span>
              <AppIcon name="chevron-right" :size="13" />
            </button>
          </div>
        </aside>
      </div>
    </template>
  </div>
</template>

<style scoped>
.understand { max-width: 1180px; margin: 0 auto; padding: 18px 20px 64px; }
.ug-head { display: flex; align-items: flex-end; gap: 10px; margin-bottom: 12px; }
.ug-title h1 { font-size: 19px; margin: 0 0 4px; }
.ug-title p { margin: 0; line-height: 1.6; max-width: 780px; }
.banner { padding: 9px 11px; border-radius: var(--radius-sm); font-size: 12.5px; margin-bottom: 10px; }
.banner.err { background: color-mix(in srgb, var(--danger, #c0392b) 12%, transparent); }
.banner.warn { background: color-mix(in srgb, var(--warn, #b8860b) 12%, transparent); }
.banner.info { background: var(--accent-soft); }
.ug-tabs { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.ug-tab {
  display: flex; align-items: center; gap: 8px; padding: 9px 12px; cursor: pointer;
  border: 1px solid var(--border); border-radius: var(--radius-ctl);
  background: transparent; color: var(--text); font: inherit; text-align: left;
}
.ug-tab.active { border-color: var(--accent-text); background: var(--accent-soft); }
.ug-tab-label { font-weight: 600; font-size: 13px; }
.ug-tab-hint { font-size: 11.5px; color: var(--text-dim); }
.empty-block { padding: 22px; text-align: center; display: flex; flex-direction: column; gap: 6px; align-items: center; }
.ug-body { display: grid; grid-template-columns: minmax(0, 1fr) 250px; gap: 12px; align-items: start; }
@media (max-width: 900px) { .ug-body { grid-template-columns: minmax(0, 1fr); } }
.ug-md .md-body { max-height: none; }
.ug-refs { position: sticky; top: 10px; }
.ref-chip {
  display: flex; align-items: center; gap: 7px; width: 100%; margin-bottom: 5px;
  padding: 6px 8px; border: 0; border-radius: var(--radius-sm);
  background: var(--bg-hover); color: var(--text); font: inherit; font-size: 12px;
  cursor: pointer; text-align: left;
}
.ref-chip:hover { background: var(--accent-soft); }
.ref-code { font-family: var(--mono, ui-monospace, monospace); font-weight: 600; color: var(--accent-text); }
.ref-meta { color: var(--text-dim); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
