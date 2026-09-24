<script setup lang="ts">
/**
 * 首页：应用外壳的落地页（顶栏改成标签条后，**导航就住在这里**）。
 *
 * 三件事：
 * 1. 主导航（论文库 / 知识库 / 笔记 / 模型 / 任务 / 设置）—— 点一下在**新标签**里打开；
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

const NAV = [
  { name: 'library', label: '论文库', icon: 'library', hint: '导入 / 解析 / 翻译 / 阅读' },
  { name: 'knowledge', label: '知识库', icon: 'knowledge', hint: '向量索引与检索问答' },
  { name: 'notes', label: '笔记', icon: 'note', hint: '批注、笔记与高亮' },
  { name: 'models', label: '模型', icon: 'model', hint: '生成 / 向量 / 视觉模型配置' },
  { name: 'tasks', label: '任务', icon: 'task', hint: '队列、进度与失败重试' },
  { name: 'settings', label: '设置', icon: 'settings', hint: '数据目录、裁图与健康检查' },
];

const papers = computed<Paper[]>(() => state.papers ?? []);
const recentRead = computed(() => papers.value
  .filter((p) => p.last_read_at)
  .sort((a, b) => String(b.last_read_at).localeCompare(String(a.last_read_at)))
  .slice(0, 4));
const recentAdded = computed(() => [...papers.value]
  .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')))
  .slice(0, 4));

onMounted(() => {
  if (!papers.value.length && !state.loading.papers) void loadPapers({ limit: 200 }).catch(() => {});
});

async function openRoute(name: string) {
  await router.push(openTab({ name }) as never);
}
async function openPaper(id: string) {
  await router.push(openTab({ name: 'reader', params: { paperId: id } }) as never);
}
</script>

<template>
  <div class="home">
    <div class="home-hero">
      <h1>PaperMind</h1>
      <p class="muted">论文导入 → 解析 → 逐页翻译 → 中英对照阅读 / 知识库问答。<br>
        下面每个入口都会在<strong>新的标签页</strong>里打开，可以随时切回来。</p>
    </div>

    <div class="grid cols-3 nav-grid">
      <button
        v-for="n in NAV"
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
          <AppIcon name="add" :size="15" />最近导入
          <span class="spacer" />
          <button class="btn sm ghost" @click="openRoute('library')">去导入</button>
        </div>
        <div class="card-body">
          <div v-if="!recentAdded.length" class="empty small">还没有导入论文</div>
          <button
            v-for="p in recentAdded"
            :key="p.id"
            class="paper-row"
            @click="openPaper(p.id)"
          >
            <span class="truncate">{{ p.title || p.id }}</span>
            <span class="spacer" />
            <span class="badge dim nowrap">{{ paperStatus(p.status).label }}</span>
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
