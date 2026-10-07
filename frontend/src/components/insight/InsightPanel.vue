<script setup lang="ts">
/**
 * 阅读器左栏的「论文解读」内容。
 *
 * 顶部**只有**「标题 + 一枚只有图标的按钮」（用户口径）：没有时间、没有模型名、没有
 * 引用核对提示、**也没有刷新/重新生成**（重跑入口只在论文库的「⋯」菜单里，面板里不再有）。
 * 那枚按钮是"到整页去读"：整页在本应用里**新开一个标签页**（TabBar），不是浏览器标签页 ——
 * 所以点击只 `emit('open-page')`，由 ReaderView 统一 `router.push(openTab(...))`
 * （`openTab` 会改响应式状态，必须在点击回调里调，渲染期调会死循环）。
 *
 * 标题下面是一份**紧凑目录**（用户口径：左栏这个页签"没有目录"）：同一份大纲数据，
 * 一行一条、缩进更小、点击**在左栏内**滚动（`scrollIntoView` 会落到最近的滚动祖先
 * `.panel-body` 上），默认展开、可折叠。这里**不**复刻整页那套两栏结构 —— 左栏本身就是一栏。
 *
 * ⚠️ 2026-10-02：原「阅读地图」页签随该功能一起删除，本组件不再有 `section`。
 */
import { computed, ref } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import InsightDoc from '@/components/insight/InsightDoc.vue';
import InsightToc from '@/components/insight/InsightToc.vue';
import { useUnderstanding } from '@/composables/useUnderstanding';
import { buildInsightOutline } from '@/utils/insightOutline';

const props = defineProps<{
  paperId: string;
  /** 这篇论文有解读任务在跑（ReaderView 按任务列表算）：解读据此决定要不要轮询 */
  running: boolean;
}>();

const emit = defineEmits<{
  /** 本面板刚发起过解读任务：让 ReaderView 立刻刷一次任务列表（轮询据此启动） */
  (e: 'started'): void;
  /** 打开整页版：由 ReaderView 新开一个应用内标签页 */
  (e: 'open-page'): void;
}>();

/* 阅读器这边任务列表已经在轮询，把 `running` 传进组合式即可（它不会再自己拉 /tasks） */
const { entry, markdown, hasContent, starting, timedOut, running, started, start } = useUnderstanding(
  computed(() => props.paperId),
  { running: computed(() => props.running) },
);

const title = '论文解读';

/** anchor 前缀：与整页（`ins`）不同，即使两个视图同时在 DOM 里也不会撞 id */
const prefix = 'pane-insight';
const outline = computed(() => buildInsightOutline(markdown.value, { prefix }));
const showToc = computed(() => hasContent.value && outline.value.length > 0);
/** 默认**展开**（用户口径）：目录在这里就是"读之前先扫一眼要读什么" */
const tocOpen = ref(true);

/** 点目录：滚到对应小节。左栏的滚动容器是 `.panel-body`，`scrollIntoView` 会自己找到它。 */
function select(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function onStart() {
  if (await start()) emit('started');
}
</script>

<template>
  <div class="pane">
    <div class="pane-head">
      <span class="pane-title">{{ title }}</span>
      <span class="spacer" />
      <!-- 只有图标、没有可见文字（title / aria-label 供悬停与读屏） -->
      <button
        class="btn icon sm ghost"
        data-insight-open
        :title="`在新标签页打开《${title}》`"
        :aria-label="`在新标签页打开《${title}》`"
        @click="emit('open-page')"
      >
        <AppIcon name="external" :size="13" />
      </button>
    </div>

    <!-- 紧凑目录：默认展开、可折叠；一行一条，自己在小框里滚（不把正文顶走） -->
    <div v-if="showToc" class="toc-compact">
      <button class="toc-bar" type="button" :aria-expanded="tocOpen" @click="tocOpen = !tocOpen">
        <AppIcon name="list" :size="12" />
        <span>目录</span>
        <span class="spacer" />
        <AppIcon :name="tocOpen ? 'chevron-up' : 'chevron-down'" :size="12" />
      </button>
      <div v-if="tocOpen" class="toc-body">
        <InsightToc compact :items="outline" @select="select" />
      </div>
    </div>

    <InsightDoc
      :markdown="markdown"
      :empty="!hasContent"
      :loading="entry.loading"
      :starting="starting"
      :running="running || started"
      :error="entry.error"
      :timed-out="timedOut"
      :anchor-prefix="prefix"
      :paper-id="props.paperId"
      @start="onStart"
    />
  </div>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; min-width: 0; }
.pane-head {
  display: flex; align-items: center; gap: 6px; flex: none;
  margin: 0 0 8px;
}
.pane-title { font-size: 12.5px; font-weight: 600; color: var(--text); }

/* 紧凑目录：标题行整行可点（展开/收起），条目区自己滚（最多 ~34vh，别把正文挤到看不见） */
.toc-compact { flex: none; margin: 0 0 10px; }
.toc-bar {
  display: flex; align-items: center; gap: 5px; width: 100%;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-layer); color: var(--text-dim);
  font: inherit; font-size: 11.5px; padding: 4px 7px; cursor: pointer; text-align: left;
}
.toc-bar:hover { background: var(--bg-hover); color: var(--text); }
.toc-body {
  margin-top: 3px; padding: 3px 4px; max-height: 34vh; overflow-y: auto;
  border: 1px solid var(--divider); border-radius: var(--radius-sm);
}
</style>
