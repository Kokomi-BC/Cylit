/** 路由（history 模式，base = /app/；后端对 /app/* 做"文件存在则给文件，否则回退 index.html"）。
 *
 * 应用是**多标签页外壳**：顶栏是标签条（`components/TabBar.vue`），导航入口在首页
 * （`views/HomeView.vue`）。`meta.tab` 给标签条提供默认标题与图标，视图加载完数据后
 * 可以用 `setTabTitle()` 把标题换成论文名这类更具体的信息。
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router';
import { rememberScroll, savedScroll } from '@/stores/scrollMemory';
import { activateRoute, routeKey, tabHash } from '@/stores/tabs';

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

/**
 * 等内容长到够高再滚。
 *
 * 视图是异步填数据的（设置页要等 `/settings`、论文库要等论文列表）：滚早了目标位置还不存在，
 * 浏览器会把 scrollTop 夹到当前高度，回来看到的就是"滚到一半"。这里最多等 ~600ms，
 * 高度够了就立刻滚；一直不够（数据没来）也照样返回，不至于把导航卡住。
 */
async function restoreScroll(top: number): Promise<{ top: number; behavior: 'auto' }> {
  for (let i = 0; i < 24; i += 1) {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    if (max >= top - 2) break;
    await new Promise((resolve) => { setTimeout(resolve, 50); });
  }
  return { top, behavior: 'auto' };
}

export const router = createRouter({
  history: createWebHistory('/app/'),
  routes,
  scrollBehavior(to, from, saved) {
    // 阅读器自己管理滚动位置（服务端权威进度），不参与全局滚动恢复
    if (to.name === 'reader') return false;
    // 浏览器前进/后退：浏览器给的位置最准
    if (saved) return saved;
    const key = routeKey(to);
    const top = savedScroll(key);
    if (to.hash) {
      // 带锚点的路由有两种来路：
      //   ① **深链**（`/app/settings#kb`，或在别处 `openTab({hash})`）：hash 是"要去哪一区"，
      //      交给页面自己滚 —— 这里返回 `{top:0}` 会跟它抢，落点变成随机的；
      //   ② **标签切回来**：这个 hash 是标签自己记下的（见 `activateRoute`），此时该回到上次的位置。
      // 判据：标签记的 hash 与 `to.hash` 一致 = ②（深链那次标签还没登记/记的是别的区）。
      if (tabHash(key) !== to.hash) return false;
      return top ? restoreScroll(top) : false;
    }
    // 切标签回来：回到**这个标签**上次看到的地方（没记过 = 顶部）
    return top ? restoreScroll(top) : { top: 0 };
  },
});

router.beforeEach((to, from) => {
  // 记下**当前页面**滚到哪：此刻 DOM 还是旧页面，等 `afterEach` 就晚了（v-if 已经把旧视图拆了）。
  // 阅读器里窗口不滚动（它有自己的滚动容器），这里记到的 0 会被 `rememberScroll` 直接忽略。
  rememberScroll(routeKey(from), window.scrollY);
});

router.afterEach((to) => {
  // 标签条跟着路由走：当前位置一定有对应标签，且它是激活的那个
  activateRoute(to);
  const title = (to.meta?.title as string) ?? 'Cylit';
  document.title = `${title} · Cylit`;
});
