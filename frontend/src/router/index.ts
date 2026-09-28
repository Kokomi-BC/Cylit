/** 路由（history 模式，base = /app/；后端对 /app/* 做"文件存在则给文件，否则回退 index.html"）。
 *
 * 应用是**多标签页外壳**：顶栏是标签条（`components/TabBar.vue`），导航入口在首页
 * （`views/HomeView.vue`）。`meta.tab` 给标签条提供默认标题与图标，视图加载完数据后
 * 可以用 `setTabTitle()` 把标题换成论文名这类更具体的信息。
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router';
import { activateRoute } from '@/stores/tabs';

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/home' },
  { path: '/home', name: 'home', component: () => import('@/views/HomeView.vue'), meta: { title: '首页' } },
  { path: '/library', name: 'library', component: () => import('@/views/LibraryView.vue'), meta: { title: '论文库' } },
  { path: '/reader/:paperId', name: 'reader', component: () => import('@/views/ReaderView.vue'), meta: { title: '阅读器' } },
  // 解读的两个整页视图**共用一个组件**（`section` 决定画哪一份）：`/understand` = 论文解读（A 路），
  // `/readmap` = 阅读地图 + 相关概念（B 路）。两个**路由名**是必须的：标签键只看 name + params，
  // 用同一个路由加 query 是开不出第二个应用内标签的。
  {
    path: '/understand/:paperId', name: 'understand',
    component: () => import('@/views/UnderstandView.vue'),
    props: { section: 'read' }, meta: { title: '论文解读' },
  },
  {
    path: '/readmap/:paperId', name: 'readmap',
    component: () => import('@/views/UnderstandView.vue'),
    props: { section: 'map' }, meta: { title: '阅读地图' },
  },
  { path: '/knowledge', name: 'knowledge', component: () => import('@/views/KnowledgeView.vue'), meta: { title: '知识库' } },
  { path: '/study', name: 'study', component: () => import('@/views/StudyView.vue'), meta: { title: '学习' } },
  { path: '/notes', name: 'notes', component: () => import('@/views/NotesView.vue'), meta: { title: '笔记' } },
  { path: '/models', name: 'models', component: () => import('@/views/ModelsView.vue'), meta: { title: '模型' } },
  { path: '/tasks', name: 'tasks', component: () => import('@/views/TasksView.vue'), meta: { title: '任务' } },
  { path: '/settings', name: 'settings', component: () => import('@/views/SettingsView.vue'), meta: { title: '设置' } },
  { path: '/:pathMatch(.*)*', redirect: '/home' },
];

export const router = createRouter({
  history: createWebHistory('/app/'),
  routes,
  scrollBehavior(to, from, saved) {
    // 阅读器自己管理滚动位置（服务端权威进度），不参与全局滚动恢复
    if (to.name === 'reader') return false;
    // 带锚点的路由（如 `/app/settings#kb`）：交给页面自己滚到分区。
    // 这里若返回 `{top: 0}`，就会和页面的锚点滚动**抢**，落点取决于谁后执行 —— 随机。
    if (to.hash) return false;
    return saved ?? { top: 0 };
  },
});

router.afterEach((to) => {
  // 标签条跟着路由走：当前位置一定有对应标签，且它是激活的那个
  activateRoute(to);
  const title = (to.meta?.title as string) ?? 'Cylit';
  document.title = `${title} · Cylit`;
});
