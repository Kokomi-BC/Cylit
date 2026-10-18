/** 路由（history 模式，base 取自 Vite 的 `base`；后端对 `<base>/*` 做"文件存在则给文件，否则回退 index.html"）。
 *
 * 用 `import.meta.env.BASE_URL` 而不是写死字符串：`base` 一改（`/app/` → `/cylit/`）
 * 这里不用跟着改，少一处能对不上的地方。
 *
 * 应用是**多标签页外壳**：顶栏是标签条（`components/TabBar.vue`），导航入口在首页
 * （`views/HomeView.vue`）。标签条的默认标题与图标在 `stores/tabs.ts` 的 `TAB_META` 里，
 * 视图加载完数据后可以用 `setTabTitle()` 把标题换成论文名这类更具体的信息。
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router';
import { rememberScroll, savedScroll } from '@/stores/scrollMemory';
import { activateRoute, routeKey, tabHash } from '@/stores/tabs';

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/home' },
  { path: '/home', name: 'home', component: () => import('@/views/HomeView.vue'), meta: { title: '首页' } },
  { path: '/library', name: 'library', component: () => import('@/views/LibraryView.vue'), meta: { title: '论文库' } },
  { path: '/reader/:paperId', name: 'reader', component: () => import('@/views/ReaderView.vue'), meta: { title: '阅读器' } },
  // 「论文解读」整页（A 路论文精读）。原先的 `/readmap`（阅读地图 + 相关概念）
  // 已随该功能一起删除，只剩这一路。
  {
    path: '/understand/:paperId', name: 'understand',
    component: () => import('@/views/UnderstandView.vue'),
    meta: { title: '论文解读' },
  },
  { path: '/knowledge', name: 'knowledge', component: () => import('@/views/KnowledgeView.vue'), meta: { title: '知识库' } },
  { path: '/study', name: 'study', component: () => import('@/views/StudyView.vue'), meta: { title: '学习' } },
  { path: '/notes', name: 'notes', component: () => import('@/views/NotesView.vue'), meta: { title: '笔记' } },
  // 模型管理并进了设置页的「模型」区。老标签页 / 老书签仍会打到 /models，
  // 保留 name 是为了让 localStorage 里那批旧标签还能解析出标题，路径本身直接重定向到对应分区。
  { path: '/models', name: 'models', redirect: '/settings#models' },
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
  // 必须跟着 vite.config.ts 的 `base` 走 —— 写死字符串的话，
  // 改 base 时漏掉这一处，应用内所有跳转都会拼出旧前缀
  // （实测：base 改成 /cylit/ 后这里还是 /app/，于是点进阅读器地址栏变成
  //  /app/reader/xxx，再被 nginx 301 回 /cylit/…，路由 prefix 对不上就白屏）。
  history: createWebHistory(import.meta.env.BASE_URL),
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
