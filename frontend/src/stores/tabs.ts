/**
 * 多标签页状态（应用外壳的核心）。
 *
 * 设计：
 * - **一个标签 = 一个路由位置**（`routeKey` = `name + params`）；同一篇论文重复打开不会开两个标签。
 * - 顶栏只显示标签，不再放主导航 —— 导航移到首页（`HomeView`）和各页面内部。
 * - 状态存 `localStorage`，刷新/重开浏览器后标签还在；论文标题这类"异步才知道"的信息
 *   由视图加载完数据后调 `setTabTitle` 回填。
 * - 首页是**普通标签**（首次使用默认打开一个），不做特殊固定，用户可以随手关掉。
 */

import { computed, reactive } from 'vue';
import { forgetScroll } from '@/stores/scrollMemory';
import type { RouteLocationNormalizedLoaded, RouteLocationRaw } from 'vue-router';

export interface AppTab {
  key: string;
  name: string;
  params: Record<string, string>;
  query: Record<string, string>;
  /** 记住的锚点（设置页的 #kb 这种「看到哪一区」）。与 query 同一个口径：点标签切回来要还原 */
  hash: string;
  title: string;
  icon: string;
}

const STORAGE_KEY = 'pm.shell.tabs';
const MAX_TABS = 12;

/** 路由名 → 默认标签标题 / 图标（视图加载后可用 setTabTitle 覆盖标题）。 */
const TAB_META: Record<string, { title: string; icon: string }> = {
  home: { title: '首页', icon: 'home' },
  library: { title: '论文库', icon: 'library' },
  reader: { title: '论文', icon: 'document' },
  // 解读整页（`/understand/:paperId`）。原 `/readmap`（阅读地图）已删除。
  // 图标 2026-10-15 从通用 `sparkle`（AI 星星）换成 `insight`（正文 + 放大镜）：
  // 与首页那张卡同一枚，别只改一边。
  understand: { title: '论文解读', icon: 'insight' },
  knowledge: { title: '知识库', icon: 'knowledge' },
  notes: { title: '笔记', icon: 'note' },
  models: { title: '模型', icon: 'model' },
  // 「任务」页已并入设置（`/tasks` 现在重定向到 `/settings#tasks`）。这一条**只留给
  // localStorage 里的旧标签**解析标题与图标 —— 打开它会重定向，不会再有新标签用它。
  tasks: { title: '任务', icon: 'task' },
  settings: { title: '设置', icon: 'settings' },
};

/** 首页标签的键：**不可关闭**（应用总得有个落脚点），也不参与"挤掉最老标签"。 */
export const HOME_KEY = 'home';

function isHome(key: string): boolean {
  return key === HOME_KEY;
}

/** 路由位置 → 稳定的标签键（同名 + 同参数视为同一个标签）。 */
export function routeKey(route: Pick<RouteLocationNormalizedLoaded, 'name' | 'params'>): string {
  const name = String(route.name ?? '');
  const params = Object.entries(route.params ?? {})
    .map(([k, v]) => `${k}=${String(v)}`).sort().join(',');
  return params ? `${name}:${params}` : name;
}

function str(v: unknown): string {
  return v === undefined || v === null ? '' : String(v);
}

export const tabsState = reactive<{
  tabs: AppTab[];
  activeKey: string;
}>({ tabs: [], activeKey: '' });

export const tabs = computed(() => tabsState.tabs);
export const activeKey = computed(() => tabsState.activeKey);

function persist(): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tabsState.tabs));
    localStorage.setItem(`${STORAGE_KEY}.active`, tabsState.activeKey);
  } catch { /* 隐私模式忽略 */ }
}

/** 从 localStorage 恢复（App 启动时调一次）。 */
export function restoreTabs(): void {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    if (Array.isArray(raw)) {
      tabsState.tabs = raw
        .filter((t) => t && typeof t.key === 'string' && typeof t.name === 'string')
        .map((t) => ({
          key: String(t.key), name: String(t.name),
          params: (t.params ?? {}) as Record<string, string>,
          query: (t.query ?? {}) as Record<string, string>,
          hash: String(t.hash || ''),
          title: String(t.title || TAB_META[String(t.name)]?.title || '未命名'),
          icon: String(t.icon || TAB_META[String(t.name)]?.icon || 'document'),
        }))
        .slice(0, MAX_TABS);
    }
    tabsState.activeKey = localStorage.getItem(`${STORAGE_KEY}.active`) || '';
  } catch {
    tabsState.tabs = [];
    tabsState.activeKey = '';
  }
  // 首次使用（没有历史标签）：给一个首页
  if (!tabsState.tabs.length) {
    tabsState.tabs = [{ key: HOME_KEY, name: 'home', params: {}, query: {}, hash: '', ...TAB_META.home }];
  }
  // 首页永远在（用户上一条口径：首页不可关闭），且固定在最左边
  const home = tabsState.tabs.find((t) => t.key === HOME_KEY)
    ?? { key: HOME_KEY, name: 'home', params: {}, query: {}, hash: '', ...TAB_META.home };
  tabsState.tabs = [home, ...tabsState.tabs.filter((t) => t.key !== HOME_KEY)];
}

/**
 * 打开（或激活）一个标签。视图里跳转一律走它，这样"打开"这件事只在一处定义。
 * 返回可直接交给 `router.push` 的目标（保留视图自己传的 query）。
 *
 * ``query`` 的语义：标签记住"上次在这个位置看的是什么"（`?side=terms&row=…`），
 * 点标签切回来时由 `tabLocation` 还原。但**显式重新打开**（论文库点「阅读」）算"从头看"，
 * 于是这里把该标签记住的 query 清掉 —— 否则会莫名其妙跳回上次的侧栏页签/定位行。
 */
export function openTab(target: RouteLocationRaw & { name?: string; params?: Record<string, string> }): RouteLocationRaw {
  const name = String(target.name ?? '');
  const hash = String((target as { hash?: string }).hash ?? '');
  const params: Record<string, string> = {};
  for (const [k, v] of Object.entries(target.params ?? {})) params[k] = str(v);
  const meta = TAB_META[name] ?? { title: '未命名', icon: 'document' };
  const key = routeKey({ name, params } as RouteLocationNormalizedLoaded);
  const exists = tabsState.tabs.find((t) => t.key === key);
  if (exists) {
    // 只在**真的非空**时清空：无条件 `= {}` 每次都赋新对象，Vue 会认为"变了"，
    // 于是一旦有人在渲染期调 openTab（曾被当成 `:to="fn()"` 用），就是
    // "渲染 → 改状态 → 再渲染"的死循环。这里顺手把这颗雷拆掉。
    if (Object.keys(exists.query).length) exists.query = {};
    // hash 同理：显式传了就以它为准（知识库点「索引管理」），没传就清掉
    if (exists.hash !== hash) exists.hash = hash;
    // "从头看"也包括滚动位置：显式重新打开一个标签，不该回到上次滚到的地方
    forgetScroll(key);
  } else {
    tabsState.tabs.push({
      key, name, params, query: {}, hash, title: meta.title, icon: meta.icon,
    });
    if (tabsState.tabs.length > MAX_TABS) {
      // 挤掉最老的**非当前、非首页**标签（当前这篇与首页永远保留）
      const victim = tabsState.tabs.find(
        (t) => t.key !== key && t.key !== tabsState.activeKey && !isHome(t.key));
      if (victim) tabsState.tabs.splice(tabsState.tabs.indexOf(victim), 1);
    }
  }
  tabsState.activeKey = key;
  persist();
  return target;
}

/** 关闭标签；首页不可关闭（返回 `null` 表示拒绝）。 */
export function closeTab(key: string): AppTab | null {
  if (isHome(key)) return null;
  const idx = tabsState.tabs.findIndex((t) => t.key === key);
  if (idx < 0) return null;
  const [removed] = tabsState.tabs.splice(idx, 1);
  forgetScroll(key);
  if (tabsState.activeKey === key) {
    const next = tabsState.tabs[Math.min(idx, tabsState.tabs.length - 1)];
    tabsState.activeKey = next?.key ?? '';
  }
  persist();
  return removed ?? null;
}

/** 关掉「除当前与首页之外」的全部标签。 */
export function closeOtherTabs(key: string): void {
  for (const t of tabsState.tabs) if (t.key !== key) forgetScroll(t.key);
  tabsState.tabs = tabsState.tabs.filter((t) => t.key === key || isHome(t.key));
  tabsState.activeKey = key;
  persist();
}

/** 视图加载完数据后回填标题（论文标题、笔记数等异步信息）。 */
export function setTabTitle(key: string, title: string): void {
  const tab = tabsState.tabs.find((t) => t.key === key);
  const clean = String(title || '').trim();
  if (!tab || !clean || tab.title === clean) return;
  tab.title = clean;
  persist();
}

/** 把"当前位置"标成激活（浏览器前进/后退、外部链接进来时用），并记住这个位置带的 query。 */
export function activateRoute(route: RouteLocationNormalizedLoaded): void {
  const key = routeKey(route);
  // 只留标量 query（数组/对象型参数对阅读器没意义，还会让 `tabLocation` 的类型变复杂）
  const query: Record<string, string> = {};
  for (const [k, v] of Object.entries(route.query ?? {})) {
    const s = str(v);
    if (s) query[k] = s;
  }
  const tab = tabsState.tabs.find((t) => t.key === key);
  if (tab) {
    tab.query = query;
    // 记住锚点：设置页切到「索引管理」之后切走再切回来，还停在那一个分区
    tab.hash = String(route.hash || '');
    tabsState.activeKey = key;
  } else {
    const name = String(route.name ?? '');
    const meta = TAB_META[name] ?? { title: '未命名', icon: 'document' };
    const params: Record<string, string> = {};
    for (const [k, v] of Object.entries(route.params ?? {})) params[k] = str(v);
    tabsState.tabs.push({
      key, name, params, query, hash: String(route.hash || ''), title: meta.title, icon: meta.icon,
    });
    tabsState.activeKey = key;
  }
  persist();
}

/** 标签的目标位置（点标签 / 拖拽后跳转用）：带上这个标签上次的 query 与锚点。 */
export function tabLocation(tab: AppTab): RouteLocationRaw {
  const query = tab.query && Object.keys(tab.query).length ? { ...tab.query } : undefined;
  return { name: tab.name, params: tab.params, query, hash: tab.hash || undefined };
}

/** 某个标签记住的锚点（'' = 没记过）。滚动恢复用它区分「标签切回来」与「深链打开」。 */
export function tabHash(key: string): string {
  return tabsState.tabs.find((t) => t.key === key)?.hash ?? '';
}
