<script setup lang="ts">
/**
 * 学习页（`/study`）：**占位**，但占位也要讲清楚"为什么这么放"。
 *
 * 系统改造后的四个面：**阅读**（阅读器）· **理解**（理解页）· **知识库**（检索/问答）·
 * **学习**（本页）。前三面都已经能用；学习面需要的是"把理解结果变成可练习的对象"
 * （提取式记忆 / 概念抽查 / 间隔重复），而它依赖两件还没做的事：
 *
 * 1. **概念去重合并**：两篇论文的"随机旋转"是同一个概念，现在分属两条 chunk 引用；
 * 2. **掌握度记录**：谁、什么时候、答对过哪个概念（需要新的表与交互）。
 *
 * 所以本页现在只做一件事：**如实告诉用户它还没做，以及它将来会长什么样**，
 * 并把已经具备的材料（理解页里的相关概念 + 知识库检索）作为入口列出来 ——
 * 不假装有功能，也不给假按钮。
 */
import { computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import { loadPapers, state } from '@/stores/app';
import { openTab } from '@/stores/tabs';

const router = useRouter();
const papers = computed(() => (state.papers ?? []).filter((p) => p.in_knowledge_base));

const PLANNED = [
  { icon: 'layers', label: '概念卡片', hint: '把理解页产出的「相关概念」抽成可复习的卡片（正面概念、背面解释与本路 chunk 引用）' },
  { icon: 'task', label: '抽查与自测', hint: '按概念出题（选中/简答），答完标"会/不会"' },
  { icon: 'refresh', label: '间隔重复', hint: '按掌握度排出今天该复习的概念；跨论文合并同一概念' },
  { icon: 'knowledge', label: '回到证据', hint: '任何一张卡片都能一键跳到它引用的 chunk 原文（复用理解页的跳转）' },
];

onMounted(() => {
  if (!(state.papers ?? []).length) void loadPapers({ limit: 200 }).catch(() => {});
});

async function openUnderstand(id: string) {
  await router.push(openTab({ name: 'understand', params: { paperId: id } }) as never);
}
</script>

<template>
  <div class="study">
    <header class="st-head">
      <h1>学习</h1>
      <p class="muted">
        把"读过的论文"变成"能复述的知识"。本页**尚未实现** —— 下面写清楚它将依赖什么、
        现在可以先做什么，避免留一个点不动的假入口。
      </p>
    </header>

    <section class="card">
      <div class="card-head"><AppIcon name="task" :size="15" />计划中的能力</div>
      <div class="card-body">
        <div v-for="p in PLANNED" :key="p.label" class="plan-row">
          <span class="plan-icon"><AppIcon :name="p.icon" :size="15" /></span>
          <span class="plan-text">
            <span class="plan-label">{{ p.label }}</span>
            <span class="plan-hint">{{ p.hint }}</span>
          </span>
          <span class="badge dim nowrap">未实现</span>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-head"><AppIcon name="layers" :size="15" />现在就能做的两件事</div>
      <div class="card-body">
        <p class="small muted">
          学习面缺的是"组织与记忆"，但**材料已经齐了**：理解页（阅读地图 + 相关概念 + chunk 引用）
          和知识库（跨论文检索与问答）。下面按论文直接进理解页。
        </p>
        <div v-if="!papers.length" class="empty small">
          还没有论文进知识库：先去论文库导入并建索引。
        </div>
        <button
          v-for="p in papers.slice(0, 8)"
          :key="p.id"
          class="paper-row"
          @click="openUnderstand(p.id)"
        >
          <span class="truncate">{{ p.title || p.id }}</span>
          <span class="spacer" />
          <span class="tiny faint nowrap">打开理解页</span>
          <AppIcon name="chevron-right" :size="14" />
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.study { max-width: 860px; margin: 0 auto; padding: 20px 20px 64px; display: flex; flex-direction: column; gap: 12px; }
.st-head h1 { font-size: calc(20px * var(--ts, 1)); margin: 0 0 4px; }
.st-head p { margin: 0; line-height: 1.7; font-size: calc(13px * var(--ts, 1)); }
.plan-row { display: flex; align-items: center; gap: 10px; padding: 8px 4px; }
.plan-icon {
  display: flex; align-items: center; justify-content: center; flex: none;
  width: 30px; height: 30px; border-radius: var(--radius-ctl);
  background: var(--accent-soft); color: var(--accent-text);
}
.plan-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
.plan-label { font-size: calc(13px * var(--ts, 1)); font-weight: 600; }
.plan-hint { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim); line-height: 1.5; }
.paper-row {
  display: flex; align-items: center; gap: 8px; width: 100%; padding: 7px 8px;
  border: 0; border-radius: var(--radius-sm); background: transparent;
  color: var(--text); font: inherit; font-size: calc(13px * var(--ts, 1)); text-align: left; cursor: pointer;
}
.paper-row:hover { background: var(--bg-hover); }
</style>
