/**
 * 多标签页状态（应用外壳的核心）。
 *
 * 设计（"标签条只装论文"改版后）：
 * - **只有论文类路由才进标签条**（`reader` / `understand`，见 `isPaperRoute`）。首页、论文库、
 *   知识库、学习、笔记、设置、AI 对话、回收站这些**导航页不登记标签** —— 它们由左侧栏
 *   （`components/AppSidebar.vue`）直接 `router.push` 切换，顶栏只回答"我开着哪几篇论文"。
 * - 一个标签 = 一个论文路由位置（`routeKey` = `name + params`）；同一篇论文重复打开不会开两个标签。
 * - 状态存 `localStorage`，刷新/重开浏览器后论文标签还在；论文标题这类"异步才知道"的信息
 *   由视图加载完数据后调 `setTabTitle` 回填。
 * - `tabsState.activeKey` 只表示"当前激活的**论文**标签"：停在导航页时它是空串，
 *   导航页不该被任何一枚论文标签点亮（以前首页会因此长亮，用户报过）。
 */

import { computed, reactive } from 'vue';
import { forgetScroll } from '@/stores/scrollMemory';
import type { RouteLocationNormalizedLoaded, RouteLocationRaw } from 'vue-router';

export interface AppTab {
  key: string;
  name: string;
  params: Record<string, string>;
  query: Record<string, string>;
  /** 记住的锚点（阅读器里"看到哪一节"）。与 query 同一个口径：点标签切回来要还原 */
  hash: string;
  title: string;
  icon: string;
}

const STORAGE_KEY = 'pm.shell.tabs';
const MAX_TABS = 12;

/** 论文类路由：唯一会进标签条的一类（按论文打开，天然是"多开"的）。 */
export const PAPER_ROUTES = ['reader', 'understand'] as const;

/**
 * 导航页路由：走侧栏切换、**不登记标签**。
 * 导出是为了让别处（侧栏、外壳）判断"这条路线属于导航还是论文"，不必各写一份名单。
 */
export const NAV_ROUTES: ReadonlySet<string> = new Set([
  'home', 'library', 'knowledge', 'study', 'notes', 'settings', 'chat', 'trash',
]);

/** 这个路由名是不是"按论文打开"的（= 该不该进标签条）。 */
export function isPaperRoute(name: string): boolean {
  return (PAPER_ROUTES as readonly string[]).includes(name);
}

/** 论文路由 → 默认标签标题 / 图标（视图加载后可用 setTabTitle 覆盖标题）。 */
const TAB_META: Record<string, { title: string; icon: string }> = {
  reader: { title: '论文', icon: 'document' },
  // 解读整页（`/understand/:paperId`）。图标与首页那张卡同一枚（`insight`：正文 + 放大镜），
  // 别只改一边。
  understand: { title: '论文解读', icon: 'insight' },
};

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
        // 旧版本把首页 / 论文库 / 设置这些导航页也存成了标签：升级后必须清掉，
        // 否则顶栏会冒出一批"点了不亮、关了又回来"的僵尸标签。
        .filter((t) => isPaperRoute(String(t.name)))
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
    const saved = localStorage.getItem(`${STORAGE_KEY}.active`) || '';
    // 恢复出来的激活键必须真的在里面（被上面那条过滤掉时置空 = "没有激活的论文标签"）
    tabsState.activeKey = tabsState.tabs.some((t) => t.key === saved) ? saved : '';
  } catch {
    tabsState.tabs = [];
    tabsState.activeKey = '';
  }
  // 空列表是**正常状态**（用户可能只开着导航页），不补任何默认标签。
}

/**
 * 打开（或激活）一个论文标签。视图里跳转论文一律走它，这样"打开"这件事只在一处定义。
 * 返回可直接交给 `router.push` 的目标（保留视图自己传的 query）。
 *
 * ``query`` 的语义：标签记住"上次在这个位置看的是什么"（`?side=terms&row=…`），
 * 点标签切回来时由 `tabLocation` 还原。但**显式重新打开**（论文库点「阅读」）算"从头看"，
 * 于是这里把该标签记住的 query 清掉 —— 否则会莫名其妙跳回上次的侧栏页签/定位行。
 *
 * ⚠️ 非论文路由（导航页：首页 / 论文库 / 知识库 / 学习 / 笔记 / 设置 / AI 对话）**不登记标签**：
 * 直接把目标原样返回，由调用方的 `router.push` 完成跳转 —— 顶栏里不该出现"设置"这种标签。
 * 导航页的调用点一律直接写 `router.push({ name })`（见 `AppSidebar`），这条只是兜底。
 */
export function openTab(target: RouteLocationRaw & { name?: string; params?: Record<string, string> }): RouteLocationRaw {
  const name = String(target.name ?? '');
  if (!isPaperRoute(name)) return target;
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
      // 挤掉最老的**非当前**标签（当前这篇永远保留）
      const victim = tabsState.tabs.find((t) => t.key !== key && t.key !== tabsState.activeKey);
      if (victim) tabsState.tabs.splice(tabsState.tabs.indexOf(victim), 1);
    }
  }
  tabsState.activeKey = key;
  persist();
  return target;
}

/** 关闭标签；返回被关掉的那枚（`null` = 没有这个标签）。 */
export function closeTab(key: string): AppTab | null {
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

/** 关掉除这一枚之外的全部标签。 */
export function closeOtherTabs(key: string): void {
  for (const t of tabsState.tabs) if (t.key !== key) forgetScroll(t.key);
  tabsState.tabs = tabsState.tabs.filter((t) => t.key === key);
  tabsState.activeKey = key;
  persist();
}

/** 视图加载完数据后回填标题（论文标题这类异步信息）。 */
export function setTabTitle(key: string, title: string): void {
  const tab = tabsState.tabs.find((t) => t.key === key);
  const clean = String(title || '').trim();
  if (!tab || !clean || tab.title === clean) return;
  tab.title = clean;
  persist();
}

/**
 * 把"当前位置"标成激活（浏览器前进/后退、深链进来时用），并记住这个位置带的 query。
 *
 * ⚠️ 导航页**不登记标签**：遇到非论文路由就把 `activeKey` 置空（= 当前没有激活的论文标签）。
 * 但**不要**顺手清掉某枚标签记住的 query/hash —— 那些属于那篇论文，切回标签时还要用。
 */
export function activateRoute(route: RouteLocationNormalizedLoaded): void {
  const name = String(route.name ?? '');
  if (!isPaperRoute(name)) {
    if (tabsState.activeKey) {
      tabsState.activeKey = '';
      persist();
    }
    return;
  }
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
    // 记住锚点：切走再切回同一篇论文时还停在原来的位置
    tab.hash = String(route.hash || '');
    tabsState.activeKey = key;
  } else {
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
