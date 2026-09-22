/** 路由（history 模式，base = /app/；后端对 /app/* 做"文件存在则给文件，否则回退 index.html"）。 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router';

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/library' },
  { path: '/library', name: 'library', component: () => import('@/views/LibraryView.vue'), meta: { title: '论文库' } },
  { path: '/reader/:paperId', name: 'reader', component: () => import('@/views/ReaderView.vue'), meta: { title: '阅读器' } },
  { path: '/knowledge', name: 'knowledge', component: () => import('@/views/KnowledgeView.vue'), meta: { title: '知识库' } },
  { path: '/notes', name: 'notes', component: () => import('@/views/NotesView.vue'), meta: { title: '笔记' } },
  { path: '/models', name: 'models', component: () => import('@/views/ModelsView.vue'), meta: { title: '模型' } },
  { path: '/tasks', name: 'tasks', component: () => import('@/views/TasksView.vue'), meta: { title: '任务' } },
  { path: '/settings', name: 'settings', component: () => import('@/views/SettingsView.vue'), meta: { title: '设置' } },
  { path: '/:pathMatch(.*)*', redirect: '/library' },
];

export const router = createRouter({
  history: createWebHistory('/app/'),
  routes,
  scrollBehavior(to, from, saved) {
    // 阅读器自己管理滚动位置（服务端权威进度），不参与全局滚动恢复
    if (to.name === 'reader') return false;
    return saved ?? { top: 0 };
  },
});

router.afterEach((to) => {
  const title = (to.meta?.title as string) ?? 'PaperMind';
  document.title = `${title} · PaperMind`;
});
