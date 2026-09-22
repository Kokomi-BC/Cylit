<script setup lang="ts">
/** 顶部导航：品牌 + 路由入口 + 后端健康指示灯。 */
import { computed, onMounted, ref } from 'vue';
import { RouterLink, useRoute } from 'vue-router';
import { loadHealth, state } from '@/stores/app';
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

const health = ref<Record<string, any> | null>(null);
onMounted(async () => { health.value = await loadHealth(); });

const healthCls = computed(() => {
  const h = state.health;
  if (!h) return '';
  if (h.error) return 'bad';
  return h.database?.ok ? 'ok' : 'bad';
});
const healthTitle = computed(() => {
  const h = state.health;
  if (!h) return '未检测';
  if (h.error) return `后端异常：${h.error}`;
  return `数据库 ${h.database?.backend ?? '?'} · 任务池 ${h.task_runner?.running ?? 0} 运行中 · 数据目录 ${h.data_dir ?? ''}`;
});
const active = (name: string) => route.name === name
  || (name === 'library' && route.name === 'reader');
</script>

<template>
  <header class="app-header">
    <div class="brand">
      <span class="logo"><AppIcon name="library" :size="14" /></span>PaperMind
    </div>
    <nav class="nav">
      <RouterLink v-for="n in nav" :key="n.name" :to="{ name: n.name }"
                  :class="{ active: active(n.name) }">
        <AppIcon :name="n.icon" :size="15" />{{ n.label }}
      </RouterLink>
    </nav>
    <div class="header-right">
      <span class="tiny faint" :title="String(route.meta?.title ?? '')">{{ route.meta?.title }}</span>
      <span class="health-dot" :class="healthCls" :title="healthTitle" />
    </div>
  </header>
</template>

<style scoped>
.app-header {
  position: sticky; top: 0; z-index: 40; height: var(--header-h);
  display: flex; align-items: center; gap: 4px; padding: 0 14px;
  background: color-mix(in srgb, var(--bg-elev) 92%, transparent);
  backdrop-filter: saturate(1.4) blur(8px);
  border-bottom: 1px solid var(--border);
}
.brand { display: flex; align-items: center; gap: 8px; font-weight: 700; letter-spacing: .2px; margin-right: 10px; }
/* 品牌标记：中性单色方形（WinUI 观感），不用渐变 */
.brand .logo {
  display: flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; border-radius: 4px;
  background: var(--accent); color: var(--text-on-accent);
}
.nav { display: flex; align-items: center; gap: 2px; flex: 1; overflow-x: auto; scrollbar-width: none; }
.nav::-webkit-scrollbar { display: none; }
.nav a {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 11px; border-radius: var(--radius-sm); color: var(--text-dim);
  font-weight: 500; white-space: nowrap;
}
.nav a:hover { background: var(--bg-hover); color: var(--text); text-decoration: none; }
.nav a.active { background: var(--accent-soft); color: var(--accent-text); font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 8px; }
.health-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-faint); }
.health-dot.ok { background: var(--ok); }
.health-dot.bad { background: var(--danger); }
</style>
