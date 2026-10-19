<script setup lang="ts">
/**
 * 常驻左侧栏 —— 应用的**主导航**。
 *
 * 为什么导航住在这里：标签条被收窄成"只装论文标签"之后，导航页不再进标签条，必须有一个常驻、
 * 随时可达的入口。分工是：左侧栏回答"我在哪一区"，顶栏（`TabBar`）回答"我开着哪几篇论文"。
 * 模型 / 任务两个页面早已并入设置页，所以这里**不单列**它们（`/models`、`/tasks` 只是重定向）。
 *
 * ⚠️ 导航一律 `router.push({ name, hash })`，**不要改成 `openTab()`**：`openTab` 的语义是"登记一个标签"，
 * 而导航页不登记标签（见 `stores/tabs.ts` 的 `isPaperRoute`）。改造前首页那些卡片是用 `openTab`
 * 开标签的，改造后那样写会在顶栏堆一串"点了不亮、关了又回来"的导航标签。
 *
 * 「论文解读」是按论文的，不是一个固定页面：点它时挑一篇最该看的（最近读过 → 否则最近导入的），
 * 没有论文就直接去论文库（跟 `HomeView` 原来的 `firstToUnderstand` / `openUnderstand` 同一套口径）。
 */
import { computed, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import { loadPapers, state } from '@/stores/app';
import { lastNavHash } from '@/stores/navMemory';
import { railCollapsed, toggleRail } from '@/stores/shellPrefs';
import { isPaperRoute, openTab } from '@/stores/tabs';
import type { RouteLocationRaw } from 'vue-router';
import type { Paper } from '@/api/types';

const route = useRoute();
const router = useRouter();

interface RailItem {
  /** 路由名（`understand` 是特殊情况：它还要带论文 id） */
  key: string;
  label: string;
  icon: string;
}

/** 第一组：读与学（顺序即分组）。 */
const PRIMARY: RailItem[] = [
  { key: 'home', label: '工作区', icon: 'home' },
  { key: 'library', label: '论文库', icon: 'library' },
  { key: 'understand', label: '论文解读', icon: 'insight' },
  { key: 'knowledge', label: '知识库', icon: 'knowledge' },
  { key: 'study', label: '学习', icon: 'study' },
];

/** 第二组：记与问。 */
const SECONDARY: RailItem[] = [
  { key: 'notes', label: '笔记', icon: 'note' },
  { key: 'chat', label: 'AI 对话', icon: 'chat' },
];

/** 设置单独顶到最下面（`margin-top: auto`）。 */
const SETTINGS: RailItem = { key: 'settings', label: '设置', icon: 'settings' };

const papers = computed<Paper[]>(() => state.papers ?? []);
const recentRead = computed(() => papers.value
  .filter((p) => p.last_read_at)
  .sort((a, b) => String(b.last_read_at).localeCompare(String(a.last_read_at))));
const recentAdded = computed(() => [...papers.value]
  .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? ''))));
const firstToUnderstand = computed(() => (recentRead.value[0] ?? recentAdded.value[0] ?? null));

onMounted(() => {
  // 侧栏要能"挑一篇论文去解读"，所以论文列表得在这里自己保证加载（用户可能直接从深链进来）
  if (!papers.value.length && !state.loading.papers) void loadPapers({ limit: 200 }).catch(() => {});
});

/**
 * 高亮判定：路由名等于项名，且当前**不在论文路由里**。
 * 论文页（reader / understand）下没有任何侧栏项高亮 —— 那时注意力在论文里，导航不该抢戏。
 */
function isActive(item: RailItem): boolean {
  if (isPaperRoute(String(route.name ?? ''))) return false;
  return route.name === item.key;
}

async function go(item: RailItem): Promise<void> {
  if (item.key === 'understand') {
    const target = firstToUnderstand.value;
    const to: RouteLocationRaw = target
      ? openTab({ name: 'understand', params: { paperId: target.id } })
      : { name: 'library' };      // 没有论文可解读：去论文库挑一篇，而不是点了没反应
    await router.push(to as never);
    return;
  }
  // 回到这一项**上次停的那一区**（设置页的 #kb 这类锚点）：没记忆过就是空 hash = 默认分区。
  // 记忆由 `router.afterEach` 写进 `stores/navMemory` —— 导航页不再是标签，标签那份 hash 记忆
  // （`stores/tabs`）对它就没有载体了，所以单开一层。
  await router.push({ name: item.key, hash: lastNavHash(item.key) });
}
</script>

<template>
  <aside class="rail" :class="{ collapsed: railCollapsed }" aria-label="主导航">
    <!-- 品牌与「收起 / 展开侧栏」那枚钮都住在顶栏（`TabBar`）：用户口径 2026-11 把 Cylit 搬回
         标签页栏，侧栏就只留导航，顶部不再有品牌行 —— 这里不要再加回一份。 -->
    <nav class="rail-nav">
      <button
        v-for="item in PRIMARY"
        :key="item.key"
        type="button"
        class="rail-item"
        :class="{ active: isActive(item) }"
        :title="item.label"
        :aria-current="isActive(item) ? 'page' : undefined"
        @click="go(item)"
      >
        <AppIcon :name="item.icon" :size="18" />
        <span class="rail-label">{{ item.label }}</span>
      </button>

      <div class="rail-sep" aria-hidden="true"></div>

      <button
        v-for="item in SECONDARY"
        :key="item.key"
        type="button"
        class="rail-item"
        :class="{ active: isActive(item) }"
        :title="item.label"
        :aria-current="isActive(item) ? 'page' : undefined"
        @click="go(item)"
      >
        <AppIcon :name="item.icon" :size="18" />
        <span class="rail-label">{{ item.label }}</span>
      </button>
    </nav>

    <!-- 底部一行：设置 + 收起 / 展开侧栏（用户口径 2026-11：这枚钮归侧栏底部，顶栏不留） -->
    <div class="rail-foot">
      <button
        type="button"
        class="rail-item"
        :class="{ active: isActive(SETTINGS) }"
        :title="SETTINGS.label"
        :aria-current="isActive(SETTINGS) ? 'page' : undefined"
        @click="go(SETTINGS)"
      >
        <AppIcon :name="SETTINGS.icon" :size="18" />
        <span class="rail-label">{{ SETTINGS.label }}</span>
      </button>
      <button
        class="rail-toggle"
        type="button"
        :title="railCollapsed ? '展开侧栏' : '收起侧栏'"
        :aria-label="railCollapsed ? '展开侧栏' : '收起侧栏'"
        :aria-expanded="!railCollapsed"
        @click="toggleRail"
      >
        <AppIcon :name="railCollapsed ? 'chevron-right' : 'chevron-left'" :size="14" />
      </button>
    </div>
  </aside>
</template>

<style scoped>
/* 侧栏：**固定不滚动**（sticky + 100vh），右边一条 1px 发丝分隔。
 * 宽度走 `--rail-w`（base.css 里定义 216px，<1024px 收成 64px 图标轨）。
 * ⚠️ 页面滚动仍然交给窗口（不是给右侧内容列单开一个滚动容器）：`scrollBehavior` 的
 * 位置恢复与阅读器的进度都建立在 `window.scrollY` 上，换成内层滚动会全部失效。 */
.rail {
  /* 顶栏通栏之后，侧栏从顶栏**下方**开始（top = --header-h），高度扣掉那一条；
     sticky 要求它比轨道矮，所以 align-self: start（grid 项默认 stretch）。
     两档宽度都走令牌（`base.css` 的 `--rail-w` / `--rail-w-min`）；过渡只给 width，
     内容不做额外动画 —— 展开/收起是"一次宽度变化"，不是元素进场。 */
  position: sticky; top: var(--header-h); z-index: 30; align-self: start;
  width: var(--rail-w);
  height: calc(100vh - var(--header-h));
  height: calc(100dvh - var(--header-h));
  transition: width 160ms var(--ease);
  display: flex; flex-direction: column;
  padding: 14px 10px;
  background: var(--bg-elev);
  border-right: 1px solid var(--border);
}

.rail-nav { display: flex; flex-direction: column; gap: 2px; min-height: 0; overflow-y: auto; }

.rail-item {
  display: flex; align-items: center; gap: 10px;
  height: 40px; padding: 0 12px; border-radius: 10px;
  background: transparent; color: var(--text-dim);
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 500; line-height: 1;
  text-align: left; white-space: nowrap; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.rail-item:hover { background: var(--bg-hover); color: var(--text); }
.rail-item.active { background: var(--accent-soft); color: var(--accent-text); font-weight: 600; }
.rail-item:focus-visible { outline: none; box-shadow: var(--ring); }
.rail-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; }

/* 两组之间的发丝分隔线（比描边更淡的那一档） */
.rail-sep { flex: none; height: 1px; margin: 8px 10px; background: var(--divider); }

/* 底部一行顶到最下面：设置（占满剩余宽度）+ 收起 / 展开那枚 24px 圆钮 */
.rail-foot { margin-top: auto; display: flex; align-items: center; gap: 4px; }
.rail-foot .rail-item { flex: 1 1 auto; min-width: 0; }
.rail-toggle {
  flex: none; display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; padding: 0; border: 0; border-radius: 999px;
  background: transparent; color: var(--text-faint); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.rail-toggle:hover { background: var(--bg-hover); color: var(--text); }
.rail-toggle:focus-visible { outline: none; box-shadow: var(--ring); }

/* ---- 手动收起成的**图标轨**（≥1024px 才看得到效果；更窄的由下面的媒体查询接管） ----
 * 文字一律 display:none（不是 opacity：挤着排会把 40px 的行高撑歪），全称退进 title 提示。 */
.rail.collapsed { width: var(--rail-w-min); }
.rail.collapsed .rail-label { display: none; }
.rail.collapsed .rail-item { justify-content: center; gap: 0; padding: 0; }
.rail.collapsed .rail-sep { margin: 8px 8px; }
/* 64px 里"设置 + 圆钮"一行放不下：改成上下两行、各自居中 */
.rail.collapsed .rail-foot { flex-direction: column; gap: 6px; }

/* ---- 窄屏：<1024px **强制**图标轨（手动展开也不管用，见 `stores/shellPrefs` 的注释） ----
 * 这几条与上面 `.rail.collapsed` 那份必须保持一致；宽度不用管：base.css 的同档媒体查询
 * 已经把 `--rail-w` 指向 `--rail-w-min`。 */
@media (max-width: 1024px) {
  .rail { padding: 14px 8px; }
  .rail-label { display: none; }
  .rail-item { justify-content: center; gap: 0; padding: 0; }
  .rail-sep { margin: 8px 8px; }
}

/* <720px：整体隐藏（本轮不做汉堡抽屉，标签条仍可用；App.vue 同时把那一列收掉） */
@media (max-width: 720px) {
  .rail { display: none; }
}
</style>
