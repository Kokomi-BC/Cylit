<script setup lang="ts">
/** 顶部导航：品牌 + 路由入口。
 * 阅读器路由下在最左侧多一个「返回论文库」入口（返回是全局导航，放在整页最顶上）。
 * 右上角不挂状态文字/小圆点（后端健康在「设置 → 健康检查」里看）。
 */
import { computed } from 'vue';
import { RouterLink, useRoute } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';

const route = useRoute();
const nav = [
  { name: 'library', label: '论文库', icon: 'library' },
  { name: 'knowledge', label: '知识库', icon: 'knowledge' },
  { name: 'notes', label: '笔记', icon: 'note' },
  { name: 'models', label: '模型', icon: 'model' },
  { name: 'tasks', label: '任务', icon: 'task' },
  { name: 'settings', label: '设置', icon: 'settings' },
];

const active = (name: string) => route.name === name
  || (name === 'library' && route.name === 'reader');

/** 阅读器等详情页需要在页头给一个明确的「返回」。 */
const showBack = computed(() => route.name === 'reader');
</script>

<template>
  <header class="app-header">
    <RouterLink v-if="showBack" class="btn icon back" :to="{ name: 'library' }" title="返回论文库">
      <AppIcon name="back" :size="15" />
    </RouterLink>
    <div class="brand">
      <span class="logo"><AppIcon name="library" :size="15" /></span>PaperMind
    </div>
    <nav class="nav" aria-label="主导航">
      <RouterLink v-for="n in nav" :key="n.name" :to="{ name: n.name }"
                  :class="{ active: active(n.name) }">
        <AppIcon :name="n.icon" :size="15" />{{ n.label }}
      </RouterLink>
    </nav>
  </header>
</template>

<style scoped>
/* 顶栏：单行不换行；导航本体是一个圆角胶囊组，而不是一排散落的链接。
 * 背景必须**不透明**：半透明 + backdrop blur 会在页面最顶部形成一层"纱/遮罩"，
 * 底下如果正好有内容滚动还会透出半截字形。 */
.app-header {
  position: sticky; top: 0; z-index: 40; height: var(--header-h);
  display: flex; align-items: center; gap: 12px; padding: 0 14px;
  background: var(--bg-elev);
  border-bottom: 1px solid var(--border);
}
/* 返回入口：只在详情页出现，占位与导航胶囊同高 */
.app-header .back { flex: none; margin-right: 2px; }
.brand {
  display: flex; align-items: center; gap: 8px; font-weight: 700;
  letter-spacing: -.015em; font-size: 15px; white-space: nowrap;
}
/* 品牌标记：中性单色圆角块（不用渐变） */
.brand .logo {
  display: flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; border-radius: var(--r-sm);
  background: var(--accent); color: var(--text-on-accent);
}
.nav {
  display: flex; align-items: center; gap: 2px; min-width: 0;
  overflow-x: auto; scrollbar-width: none;
  padding: 3px; border-radius: var(--radius-ctl);
  background: var(--bg-sunken); border: 1px solid var(--border);
}
.nav::-webkit-scrollbar { display: none; }
/* 高度写死为 30px：保证 6 个入口完全等高（之前 33.7px 的“半像素差”就来自行高） */
.nav a {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  height: 30px; padding: 0 13px;
  border-radius: var(--radius-ctl); color: var(--text-dim);
  font-weight: 500; font-size: 13px; line-height: 1; white-space: nowrap;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.nav a :deep(.icon) { display: block; }
.nav a:hover { background: var(--bg-hover); color: var(--text); text-decoration: none; }
.nav a.active {
  background: var(--bg-elev); color: var(--accent-text); font-weight: 600;
  box-shadow: var(--shadow-1);
}
</style>
