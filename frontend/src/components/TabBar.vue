<script setup lang="ts">
/**
 * 应用外壳的**标签条**（顶栏）。
 *
 * 这里**不再放主导航**：导航在首页（`HomeView`）与各页面内部，顶栏只回答
 * "我现在开着哪些页面、在哪一个"。
 *
 * 交互：点标签切换（`RouterLink`，可用中键/Ctrl 点击开新标签）、× 关闭、
 * 双击标签名就地重命名（与浏览器/VS Code 一致，纯本地展示偏好）、
 * 右键菜单（关闭其它 / 关闭全部）、拖拽换序。
 */
import { computed, nextTick, ref } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import {
  HOME_KEY, closeOtherTabs, closeTab, openTab, routeKey, setTabTitle, tabLocation, tabs, tabsState,
} from '@/stores/tabs';

const route = useRoute();
const router = useRouter();

const strip = ref<HTMLElement | null>(null);
const renaming = ref('');
const renameText = ref('');
const menuFor = ref('');
const dragKey = ref('');

/** 当前激活标签：**只认当前路由**。
 *
 * `tabsState.activeKey` 是"持久化 + 挤掉最老标签"用的，不作为高亮依据 ——
 * 以前这里写的是 `tabsState.activeKey || routeKey(route)`，只要持久化里残留一个旧值
 * （上次会话停在首页、或路由还没落定），标签条就会点亮**错的**那个标签，
 * 看起来就是"首页标签始终高亮"（用户报过）。高亮永远跟着地址栏走，才不会说谎。 */
const currentKey = computed(() => routeKey(route));
const canCloseAll = computed(() => tabs.value.length > 1);

async function go(key: string) {
  const tab = tabs.value.find((t) => t.key === key);
  if (tab) await router.push(tabLocation(tab) as never);
}

/** 关标签：关掉了当前这个就跳到邻居。首页不可关闭，所以不会出现"一个都不剩"。 */
async function onClose(key: string, ev: MouseEvent) {
  ev.preventDefault();
  ev.stopPropagation();
  const wasActive = key === currentKey.value;
  const idx = tabs.value.findIndex((t) => t.key === key);
  closeTab(key);
  if (!wasActive) return;
  const next = tabs.value[Math.min(idx, tabs.value.length - 1)];
  if (next) await go(next.key);
  else await router.push(openTab({ name: 'home' }) as never);
}

function startRename(key: string) {
  const tab = tabs.value.find((t) => t.key === key);
  if (!tab) return;
  renaming.value = key;
  renameText.value = tab.title;
  nextTick(() => {
    const el = strip.value?.querySelector<HTMLInputElement>('input.tab-rename');
    el?.focus();
    el?.select();
  });
}

function commitRename() {
  if (renaming.value) setTabTitle(renaming.value, renameText.value);
  renaming.value = '';
}

function onDragStart(key: string, ev: DragEvent) {
  dragKey.value = key;
  ev.dataTransfer?.setData('text/plain', key);
  if (ev.dataTransfer) ev.dataTransfer.effectAllowed = 'move';
}

function onDrop(target: string) {
  const from = dragKey.value;
  dragKey.value = '';
  if (!from || from === target) return;
  const list = tabsState.tabs;
  const fi = list.findIndex((t) => t.key === from);
  const ti = list.findIndex((t) => t.key === target);
  if (fi < 0 || ti < 0) return;
  const [moved] = list.splice(fi, 1);
  list.splice(ti, 0, moved);
}

function onContextMenu(key: string, ev: MouseEvent) {
  ev.preventDefault();
  menuFor.value = menuFor.value === key ? '' : key;
}
</script>

<template>
  <header class="tabbar">
    <!-- 品牌词（用户口径 2026-09-28：原来的图标换成文字「Cylit」）。
         刻意**不加**底色方块：顶栏本来就是一条细标签条，一块实心青绿方块会跟
         「首页」那枚常驻胶囊抢注意力；净文字 + 强调色更像"应用名"而不是一个按钮。 -->
    <span class="tabbar-logo" title="Cylit">Cylit</span>

    <nav ref="strip" class="tab-strip" aria-label="打开的标签页">
      <RouterLink
        v-for="t in tabs"
        :key="t.key"
        class="tab"
        :class="{ active: t.key === currentKey, pinned: t.key === HOME_KEY }"
        :to="tabLocation(t)"
        :title="t.title"
        draggable="true"
        @dragstart="onDragStart(t.key, $event)"
        @dragover.prevent
        @drop="onDrop(t.key)"
        @contextmenu="onContextMenu(t.key, $event)"
      >
        <AppIcon :name="t.icon" :size="13" />
        <input
          v-if="renaming === t.key"
          v-model="renameText"
          class="tab-rename"
          @keyup.enter="commitRename"
          @keyup.esc="renaming = ''"
          @blur="commitRename"
          @click.stop.prevent
        >
        <span
          v-else
          class="tab-title truncate"
          @dblclick.stop.prevent="startRename(t.key)"
        >{{ t.title }}</span>
        <!-- 首页不可关闭：应用总得有个落脚点（store 里也会拒绝关它） -->
        <button
          v-if="t.key !== HOME_KEY"
          class="tab-close"
          title="关闭标签页"
          @click="onClose(t.key, $event)"
        >
          <AppIcon name="close" :size="11" />
        </button>

        <span v-if="menuFor === t.key" class="tab-menu" @click.stop>
          <button :disabled="!canCloseAll" @click="closeOtherTabs(t.key); menuFor = ''">关闭其它标签</button>
          <button v-if="t.key !== HOME_KEY" @click="closeTab(t.key); menuFor = ''">关闭标签</button>
        </span>
      </RouterLink>
    </nav>
  </header>
</template>

<style scoped>
/* 顶栏：一条细标签条。背景必须**不透明**（半透明 + blur 会在顶部形成一层"纱"）。 */
.tabbar {
  position: sticky; top: 0; z-index: 40; height: var(--header-h);
  display: flex; align-items: center; gap: 8px; padding: 0 10px;
  background: var(--bg-elev); border-bottom: 1px solid var(--border);
}
/* 左上角品牌词：**净文字**（用户口径 2026-09-28，原来是一个图标方块）。
   字号/字重比标签页略大一档，读起来是"应用名"而不是一个可点的标签；`user-select: none` +
   右侧留 8px，让它和第一个标签之间有一道自然的呼吸位。 */
.tabbar-logo {
  flex: none; display: flex; align-items: center;
  padding: 0 8px 0 3px;
  font-size: 14.5px; font-weight: 600; letter-spacing: -.02em; line-height: 1;
  color: var(--accent-text);
  user-select: none;
}
.tab-strip {
  display: flex; align-items: center; gap: 2px; min-width: 0; flex: 1;
  overflow-x: auto; scrollbar-width: none;
}
.tab-strip::-webkit-scrollbar { display: none; }
.tab {
  position: relative;
  display: inline-flex; align-items: center; gap: 5px;
  /* 右内边距 6px：关闭按钮自己还有 3px 余量（17px 盒子装 11px 图标），
   * 于是"图标左边缘"和"× 字形右边缘"到胶囊边缘的距离都是 9px —— 肉眼看才是居中的。 */
  height: 28px; padding: 0 6px 0 9px; max-width: 190px; flex: none;
  border-radius: var(--radius-sm); color: var(--text-dim);
  font-size: 12.5px; font-weight: 500; line-height: 1; white-space: nowrap;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.tab:hover { background: var(--bg-hover); color: var(--text); text-decoration: none; }
.tab.active { background: var(--bg); color: var(--accent-text); font-weight: 600; box-shadow: var(--shadow-1); }
/* 常驻的「首页」标签（= 被删掉的 🏠 按钮的替身）**任何状态下都特殊显示**（用户口径）：
 *   - 平时（没选中）：淡强调色小胶囊 + 强调色图标/文字 —— 一眼看出"这是回首页的入口"，
 *     而不是一枚普通的灰字标签；它没有 ×，所以左右内边距必须对称（否则内容偏右，用户报过）；
 *   - 落在首页时：同一块底色，再加一圈强调色内描边 + 加粗，"你在这里"照样看得出来。
 * ⚠️ 底色一律用**强调色系**（--accent-soft），不是内容标签激活时那块中性灰胶囊（--bg）——
 *    两者不混，所以不会重演"首页看起来一直是被选中的标签"（用户早先报过的问题）。 */
.tab.pinned {
  padding: 0 9px; font-weight: 600;
  background: var(--accent-soft); color: var(--accent-text);
}
.tab.pinned:hover { background: color-mix(in srgb, var(--accent) 20%, transparent); color: var(--accent-text); }
.tab.pinned.active {
  background: var(--accent-soft);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 45%, transparent);
}
.tab-title { max-width: 130px; cursor: default; }
.tab-rename {
  width: 120px; height: 20px; padding: 0 4px; font-size: 12.5px;
  border-radius: var(--r-xs); border-color: var(--accent);
}
.tab-close {
  display: inline-flex; align-items: center; justify-content: center;
  width: 17px; height: 17px; padding: 0; border: 0; border-radius: var(--r-xs);
  background: transparent; color: var(--text-faint); cursor: pointer; flex: none;
}
.tab-close:hover { background: var(--bg-pressed); color: var(--text); }
/* 标签右键菜单：挂在标签内绝对定位，点空白处由标签条的 click 收起 */
.tab-menu {
  position: absolute; top: 30px; left: 4px; z-index: 50;
  display: flex; flex-direction: column; min-width: 132px; padding: 4px;
  background: var(--bg-elev); border: 1px solid var(--border);
  border-radius: var(--r-sm); box-shadow: var(--shadow-2);
}
.tab-menu button {
  border: 0; background: transparent; color: var(--text); text-align: left;
  padding: 6px 9px; border-radius: var(--r-xs); font-size: 12.5px; cursor: pointer;
}
.tab-menu button:hover:not(:disabled) { background: var(--bg-hover); }
.tab-menu button:disabled { opacity: .45; cursor: default; }
</style>
