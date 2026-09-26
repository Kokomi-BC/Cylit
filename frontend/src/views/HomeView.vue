<script setup lang="ts">
/**
 * 首页：应用外壳的落地页（顶栏改成标签条后，**导航就住在这里**）。
 *
 * 三件事：
 * 1. 主导航 —— 按改造后的四个面组织：**阅读 / 理解 / 知识库 / 学习**，
 *    外加工具面（笔记 / 模型 / 任务 / 设置）。点一下在**新标签**里打开；
 * 2. 继续阅读：最近读过的论文，点一下直接开阅读器标签；
 * 3. 最近导入：刚放进来的论文（方便接着解析/翻译）。
 */
import { computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import { loadPapers, state } from '@/stores/app';
import { openTab } from '@/stores/tabs';
import { fmtRelative, paperStatus } from '@/utils/format';
import type { Paper } from '@/api/types';

const router = useRouter();

/** 四个面（改造后的主结构）—— 顺序就是流水线顺序：读 → 懂 → 查 → 练 */
const FACES = [
  { name: 'library', label: '阅读', icon: 'library', hint: '论文库与阅读器：导入 / 解析 / 两路翻译 / 中英对照精读' },
  { name: 'understand', label: '理解', icon: 'layers', hint: '第二阶段：阅读地图 + 技术解剖，结果引用到 chunk' },
  { name: 'knowledge', label: '知识库', icon: 'knowledge', hint: '跨论文检索与问答（含插图/公式/表格），带证据' },
  { name: 'study', label: '学习', icon: 'task', hint: '把理解结果变成可复习的概念（占位，写明计划）' },
];

const TOOLS = [
  { name: 'notes', label: '笔记', icon: 'note', hint: '批注、笔记与高亮' },
  { name: 'models', label: '模型', icon: 'model', hint: '生成 / 向量 / 视觉模型配置' },
  { name: 'tasks', label: '任务', icon: 'task', hint: '队列、进度与失败重试' },
  { name: 'settings', label: '设置', icon: 'settings', hint: '参数、索引管理与搜索记录' },
];

const papers = computed<Paper[]>(() => state.papers ?? []);
const recentRead = computed(() => papers.value
  .filter((p) => p.last_read_at)
  .sort((a, b) => String(b.last_read_at).localeCompare(String(a.last_read_at)))
  .slice(0, 4));
const recentAdded = computed(() => [...papers.value]
  .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')))
  .slice(0, 4));
/** 理解/学习是**按论文**的：首页给出的入口要挑一篇，所以列最近读过的几篇 */
const forUnderstand = computed(() => (recentRead.value.length ? recentRead.value : recentAdded.value));

onMounted(() => {
  if (!papers.value.length && !state.loading.papers) void loadPapers({ limit: 200 }).catch(() => {});
});

async function openRoute(name: string) {
  // 理解页是 `/understand/:paperId`：没有论文可挑时直接回论文库
  if (name === 'understand') {
    const target = forUnderstand.value[0];
    if (!target) return openRoute('library');
    return openUnderstand(target.id);
  }
  await router.push(openTab({ name }) as never);
}
async function openPaper(id: string) {
  await router.push(openTab({ name: 'reader', params: { paperId: id } }) as never);
}
async function openUnderstand(id: string) {
  await router.push(openTab({ name: 'understand', params: { paperId: id } }) as never);
}
</script>

<template>
  <div class="home">
    <div class="home-hero">
      <h1>PaperMind</h1>
      <p class="muted">论文导入 → 解析 → 两路并发翻译 → <strong>阅读</strong> ·
        <strong>理解</strong> · <strong>知识库</strong> · <strong>学习</strong>。<br>
        下面每个入口都会在<strong>新的标签页</strong>里打开，可以随时切回来。</p>
    </div>

    <div class="grid cols-2 nav-grid">
      <button
        v-for="n in FACES"
        :key="n.name"
        class="card hoverable nav-card"
        @click="openRoute(n.name)"
      >
        <span class="nav-icon"><AppIcon :name="n.icon" :size="18" /></span>
        <span class="nav-text">
          <span class="nav-label">{{ n.label }}</span>
          <span class="nav-hint">{{ n.hint }}</span>
        </span>
        <AppIcon name="chevron-right" :size="15" />
      </button>
    </div>

    <div class="grid cols-4 nav-grid tool-grid">
      <button
        v-for="n in TOOLS"
        :key="n.name"
        class="card hoverable nav-card sm"
        @click="openRoute(n.name)"
      >
        <span class="nav-icon"><AppIcon :name="n.icon" :size="16" /></span>
        <span class="nav-text">
          <span class="nav-label">{{ n.label }}</span>
          <span class="nav-hint">{{ n.hint }}</span>
        </span>
      </button>
    </div>

    <div class="grid cols-2">
      <section class="card">
        <div class="card-head">
          <AppIcon name="play" :size="15" />继续阅读
          <span class="spacer" />
          <button class="btn sm ghost" @click="openRoute('library')">全部论文</button>
        </div>
        <div class="card-body">
          <div v-if="!recentRead.length" class="empty small">还没有阅读记录</div>
          <button
            v-for="p in recentRead"
            :key="p.id"
            class="paper-row"
            @click="openPaper(p.id)"
          >
            <span class="truncate">{{ p.title || p.id }}</span>
            <span class="spacer" />
            <span class="tiny faint nowrap">{{ fmtRelative(p.last_read_at) }}</span>
          </button>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <AppIcon name="layers" :size="15" />去理解 / 去学习
          <span class="spacer" />
          <button class="btn sm ghost" @click="openRoute('study')">学习页</button>
        </div>
        <div class="card-body">
          <div v-if="!forUnderstand.length" class="empty small">还没有可理解的论文</div>
          <button
            v-for="p in forUnderstand"
            :key="`u-${p.id}`"
            class="paper-row"
            @click="openUnderstand(p.id)"
          >
            <span class="truncate">{{ p.title || p.id }}</span>
            <span class="spacer" />
            <span class="tiny faint nowrap">理解页</span>
            <AppIcon name="chevron-right" :size="14" />
          </button>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.home { max-width: 1040px; margin: 0 auto; padding: 22px 20px 64px; }
.home-hero { margin-bottom: 18px; }
/* 首页自己的文案用**主题色**（用户口径）：标题实心强调色，副标题用同一支色的浅一档 ——
   与全站"只有一支强调色"的规则一致，也不会让整页文字都变成链接色。 */
.home-hero h1 { font-size: 24px; margin-bottom: 6px; color: var(--accent-text); }
.home-hero p {
  font-size: 13px; line-height: 1.7; margin: 0;
  color: color-mix(in srgb, var(--accent-text) 68%, var(--text-dim));
}
.home-hero strong { color: var(--accent-text); font-weight: 600; }
.nav-grid { margin-bottom: 12px; }
.tool-grid .nav-card { padding: 10px 11px; }
.tool-grid .nav-icon { width: 28px; height: 28px; }
/* 导航卡：整块可点，左侧图标 + 标题/说明 + 右箭头 */
.nav-card {
  display: flex; align-items: center; gap: 11px; text-align: left; width: 100%;
  padding: 13px 14px; cursor: pointer; font: inherit; color: var(--text);
}
.nav-icon {
  display: flex; align-items: center; justify-content: center; flex: none;
  width: 34px; height: 34px; border-radius: var(--radius-ctl);
  background: var(--accent-soft); color: var(--accent-text);
}
.nav-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
.nav-label { font-weight: 600; font-size: 13.5px; }
.nav-hint { font-size: 11.5px; color: var(--text-dim); }
.paper-row {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 7px 8px; border: 0; border-radius: var(--radius-sm);
  background: transparent; color: var(--text); font: inherit; font-size: 13px;
  text-align: left; cursor: pointer;
}
.paper-row:hover { background: var(--bg-hover); }
</style>
