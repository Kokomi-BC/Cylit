<script setup lang="ts">
/**
 * 阅读器左栏的「论文解读」内容。
 *
 * 顶部**只有**「标题 + 一枚只有图标的按钮」（用户口径）：没有时间、没有模型名、没有
 * 引用核对提示、**也没有刷新/重新生成**（重跑入口只在论文库的「⋯」菜单里，面板里不再有）。
 * 那枚按钮是"放到浮窗里读"（用户口径 2026-12-07：原来开应用内新标签页，改成浮窗 ——
 * 浮窗可拖动、可拉大、位置记得住，翻正文时还留在原处）：点击只 `emit('open-float')`，
 * 由 ReaderView 弹一张 `FloatingCard`（层级 / 拖动 / 记忆全走那一套，这里不管）。
 * `floating` 为真时本组件就是**浮窗里的内容**：不再画标题行（标题由 `FloatingCard` 给）。
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
import type { UnderstandingRefItem } from '@/api/types';

const props = defineProps<{
  paperId: string;
  /** 这篇论文有解读任务在跑（ReaderView 按任务列表算）：解读据此决定要不要轮询 */
  running: boolean;
  /** 作为浮窗内容渲染：去掉自带的标题行（含那枚入口按钮），标题交给 `FloatingCard` */
  floating?: boolean;
}>();

const emit = defineEmits<{
  /** 本面板刚发起过解读任务：让 ReaderView 立刻刷一次任务列表（轮询据此启动） */
  (e: 'started'): void;
  /** 在浮窗里打开：由 ReaderView 弹一张可拖动的 `FloatingCard` */
  (e: 'open-float'): void;
  /** 正文里点了引用标号（`InsightDoc` 只上报，往哪跳由 ReaderView 决定） */
  (e: 'cite', p: { code: string; block: string; page: number; latex?: string }): void;
  /** 悬浮标号的速览请求（宿主在阅读器那侧弹卡；点击照旧走 cite） */
  (e: 'peek', p: {
    anchor: { x: number; y: number; w: number; h: number };
    page?: number | null;
    rowId?: string | null;
    blockId?: string | null;
    label?: string | null;
    /** 公式类引用带 LaTeX：卡片里直接画那条公式（同行两条引用才分得开） */
    latex?: string | null;
  }): void;
  (e: 'peek-hide'): void;
}>();

/* 阅读器这边任务列表已经在轮询，把 `running` 传进组合式即可（它不会再自己拉 /tasks） */
const { entry, payload, markdown, hasContent, starting, timedOut, running, started, start } = useUnderstanding(
  computed(() => props.paperId),
  { running: computed(() => props.running) },
);

const title = '论文解读';

/**
 * 正文里那些标号的落点：**过期的排前面、有效的排后面**（`InsightDoc` 按编号建表，
 * 后写覆盖先写），于是重翻译过的引用退化到页码，正常的引用落到块上。
 */
const refs = computed<UnderstandingRefItem[]>(() => [
  ...(payload.value?.stale_refs ?? []),
  ...(payload.value?.refs_resolved ?? []),
]);

/** anchor 前缀：与整页（`ins`）不同，即使两个视图同时在 DOM 里也不会撞 id。
 *  ⚠️ 浮窗里那份必须**再换一个**：侧栏那份与解读浮窗会同时挂载，同一个前缀就成了两套重复 id，
 *  浮窗里点目录会 `getElementById` 拿到侧栏那份（滚的是侧栏，浮窗纹丝不动）。 */
const prefix = computed(() => (props.floating ? 'float-insight' : 'pane-insight'));
const outline = computed(() => buildInsightOutline(markdown.value, { prefix: prefix.value }));
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
  <div class="pane" :class="{ 'is-float': props.floating }">
    <!-- 浮窗里不画这一行（标题由 FloatingCard 的标题栏给，再来一条就是重复的「论文解读」） -->
    <div v-if="!props.floating" class="pane-head">
      <span class="pane-title">{{ title }}</span>
      <span class="spacer" />
      <!-- 只有图标、没有可见文字（title / aria-label 供悬停与读屏）。
           用 pip 而不是 external：这枚钮现在**不再离开本页**，是把解读弹成一张浮窗。 -->
      <button
        class="btn icon sm ghost"
        data-insight-open
        :title="`在浮窗中打开《${title}》`"
        :aria-label="`在浮窗中打开《${title}》`"
        @click="emit('open-float')"
      >
        <AppIcon name="pip" :size="13" />
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
      :refs="refs"
      @start="onStart"
      @cite="(c) => emit('cite', c)"
      @peek="(p) => emit('peek', p)"
      @peek-hide="emit('peek-hide')"
    />
  </div>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; min-width: 0; }
.pane-head {
  display: flex; align-items: center; gap: 6px; flex: none;
  margin: 0 0 8px;
}
.pane-title { font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; color: var(--text); }

/* 紧凑目录：标题行整行可点（展开/收起），条目区自己滚（最多 ~34vh，别把正文挤到看不见） */
.toc-compact { flex: none; margin: 0 0 10px; }
.toc-bar {
  display: flex; align-items: center; gap: 5px; width: 100%;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-layer); color: var(--text-dim);
  font: inherit; font-size: calc(11.5px * var(--ts, 1)); padding: 4px 7px; cursor: pointer; text-align: left;
}
.toc-bar:hover { background: var(--bg-hover); color: var(--text); }
.toc-body {
  margin-top: 3px; padding: 3px 4px; max-height: 34vh; overflow-y: auto;
  border: 1px solid var(--divider); border-radius: var(--radius-sm);
}
</style>
