<script setup lang="ts">
/**
 * 阅读器左栏的「论文解读」/「阅读地图」内容 —— 两个页签共用本组件，只有 `section` 不同。
 *
 * 顶部**只有**「标题 + 一枚只有图标的按钮」（用户口径）：没有时间、没有模型名、没有
 * 引用核对提示、**也没有刷新/重新生成**（重跑入口只在论文库的「⋯」菜单里，面板里不再有）。
 * 那枚按钮是"到整页去读"：整页在本应用里**新开一个标签页**（TabBar），不是浏览器标签页 ——
 * 所以点击只 `emit('open-page')`，由 ReaderView 统一 `router.push(openTab(...))`
 * （`openTab` 会改响应式标签状态，必须在点击回调里调，渲染期调会死循环）。
 */
import { computed } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import InsightDoc from '@/components/insight/InsightDoc.vue';
import { useUnderstanding } from '@/composables/useUnderstanding';

const props = defineProps<{
  paperId: string;
  section: 'read' | 'map';
  /** 这篇论文有理解任务在跑（ReaderView 按任务列表算）：解读据此决定要不要轮询 */
  running: boolean;
}>();

const emit = defineEmits<{
  /** 本面板刚发起过解读任务：让 ReaderView 立刻刷一次任务列表（轮询据此启动） */
  (e: 'started'): void;
  /** 打开整页版：由 ReaderView 新开一个应用内标签页 */
  (e: 'open-page'): void;
}>();

/* 阅读器这边任务列表已经在轮询，把 `running` 传进组合式即可（它不会再自己拉 /tasks） */
const { entry, pathA, pathB, concepts, hasAny, starting, timedOut, running, started, start } = useUnderstanding(
  computed(() => props.paperId),
  { running: computed(() => props.running) },
);

const title = computed(() => (props.section === 'map' ? '阅读地图' : '论文解读'));
const markdown = computed(() => String(
  (props.section === 'map' ? pathB.value?.markdown : pathA.value?.markdown) ?? '',
));

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
    <InsightDoc
      :section="section"
      :markdown="markdown"
      :concepts="concepts"
      :empty="!hasAny"
      :loading="entry.loading"
      :starting="starting"
      :running="running || started"
      :error="entry.error"
      :timed-out="timedOut"
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
</style>
