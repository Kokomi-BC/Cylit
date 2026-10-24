<script setup lang="ts">
/**
 * 应用外壳的**论文标签条**（顶栏）。
 *
 * 这里只装一类东西：**按论文打开的标签**（`reader` / `understand`，见 `stores/tabs.ts` 的
 * `isPaperRoute`）。导航页（首页 / 论文库 / 知识库 / 学习 / 笔记 / 设置）不再进标签条，
 * 它们的入口是左侧栏（`components/AppSidebar.vue`）—— 所以这里既没有品牌词，也没有
 * 常驻的「首页」胶囊：两者都在侧栏里，重复一遍只是占地方。
 *
 * 版面（用户口径 2026-12）：品牌 + **圆角**托盘（横向可滚、两端浮动圆钮、滚轮横滑）+ 最右一枚「+」。
 * 标签与托盘都是圆角矩形（不再是全圆胶囊——圆到头的形状在顶栏里像一排按钮，读起来散）。
 * 「+」是打开论文库的固定入口（导入论文在那边），常驻最右：一枚标签都没有时，它是这一行唯一的动作。
 * 仍然没有固定的「首页」胶囊、没有「AI 对话」胶囊 —— 其余入口一律在左侧栏。
 * 交互沿用原有实现：点标签切换、× 关闭、双击重命名、右键菜单（关闭其它 / 关闭标签）、拖拽换序。
 *
 * ⚠️ 「一个都不剩」必须显式处理：改造前首页是**不可关闭**的兜底标签，现在没有兜底了 ——
 * 关掉最后一枚之后要 `/home`，否则会停在刚被关掉的那个 `/reader/xxx` 上白屏。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import { BRAND_ICON } from '@/utils/brand';
import {
  closeOtherTabs, closeTab, routeKey, tabLocation, tabs, tabsState,
} from '@/stores/tabs';

const route = useRoute();
const router = useRouter();


const strip = ref<HTMLElement | null>(null);

const menuFor = ref('');
const dragKey = ref('');

/** 当前激活标签：**只认当前路由**。
 *
 * 高亮永远跟着地址栏走，不看 `tabsState.activeKey`：停在导航页时后者是空串（见 `activateRoute`），
 * 而一旦持久化里残留一个旧值，标签条就会点亮**错的**那枚（用户报过"首页标签始终高亮"）。
 * 导航页下 `currentKey` 是 `home`/`library`/`settings` 这类名字，没有任何标签与之相等 —— 全不亮，正确。 */
const currentKey = computed(() => routeKey(route));
const canCloseAll = computed(() => tabs.value.length > 1);

/* --------------------------------- 溢出与滚动 ---------------------------------
 * 与阅读器侧栏那套页签**同一份约定**（`ReaderView` 的 `.side-tabs` + `.side-scroll-btn`），
 * 用户口径就是"照侧栏那两个箭头来"：两枚圆钮**浮在托盘的两端之外的位置**（不是并排堆在一侧），
 * 哪一侧还有内容就只在那侧出现。
 */
const overflowing = ref(false);
/** 左/右还藏着内容 → 那一侧浮出圆箭头 */
const canLeft = ref(false);
const canRight = ref(false);
/** 两端圆钮占掉的宽度：滚动落位时要把它让开（与 `.tab-strip` 的 `scroll-padding` 对齐） */
const EDGE_INSET = 30;

function syncScrollState() {
  const el = strip.value;
  if (!el) return;
  overflowing.value = el.scrollWidth - el.clientWidth > 1;
  canLeft.value = el.scrollLeft > 1;
  canRight.value = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
}

/** 点箭头：翻一屏的 70%（至少 120px）。
 *
 * ⚠️ 用**瞬时**滚动（直接改 `scrollLeft`），不要 `behavior: 'smooth'`：补间由合成器推进，
 * 无头 + 虚拟时间下根本不走（实测"点了按钮纹丝不动"），真实浏览器也会受"减少动效"影响。 */
function scrollStep(dir: -1 | 1) {
  const el = strip.value;
  if (!el) return;
  const max = el.scrollWidth - el.clientWidth;
  const step = Math.max(120, el.clientWidth * 0.7);
  el.scrollLeft = Math.max(0, Math.min(max, el.scrollLeft + dir * step));
  syncScrollState();          // 程序化滚动后立刻更新按钮显隐，不依赖异步派发的 scroll 事件
}

/** 滚轮 → 横向滑动。**只在真的溢出时接管**，否则页面本身的滚动会被平白吃掉。
 *  与侧栏一致：只把"竖直滚轮"转成横向；纯横向的触控板滑动交给浏览器原生处理。 */
function onWheel(ev: WheelEvent) {
  const el = strip.value;
  if (!el || !overflowing.value) return;
  if (Math.abs(ev.deltaY) <= Math.abs(ev.deltaX)) return;
  ev.preventDefault();
  el.scrollTo({ left: el.scrollLeft + ev.deltaY, behavior: 'instant' });
  syncScrollState();
}

/** 把激活标签滚进视野：按需滚最小距离（不用 `scrollIntoView`，它会把整页也带着滚）。
 *  可视窗口两端各让开一枚圆钮的宽度，免得激活的那枚正好落在按钮底下。 */
function revealActive() {
  const el = strip.value;
  const active = el?.querySelector<HTMLElement>('.tab.active');
  if (!el || !active) return;
  const a = active.getBoundingClientRect();
  const s = el.getBoundingClientRect();
  const left = s.left + EDGE_INSET;
  const right = s.right - EDGE_INSET;
  if (a.left < left) el.scrollLeft -= left - a.left;
  else if (a.right > right) el.scrollLeft += a.right - right;
}

let ro: ResizeObserver | null = null;

onMounted(() => {
  const el = strip.value;
  syncScrollState();
  if (el && typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => syncScrollState());
    ro.observe(el);
  }
  window.addEventListener('resize', syncScrollState);
});
onUnmounted(() => {
  ro?.disconnect();
  window.removeEventListener('resize', syncScrollState);
});

/** 切标签 / 开关标签之后：先让激活的那枚可见，再刷新按钮状态 */
watch([currentKey, () => tabs.value.length], () => {
  void nextTick(() => { revealActive(); syncScrollState(); });
});

async function go(key: string) {
  const tab = tabs.value.find((t) => t.key === key);
  if (tab) await router.push(tabLocation(tab) as never);
}

/** 关标签：关掉了当前这枚就跳到邻居；一枚都不剩就回首页（没有首页标签兜底了）。 */
async function onClose(key: string, ev: MouseEvent) {
  ev.preventDefault();
  ev.stopPropagation();
  const wasActive = key === currentKey.value;
  const idx = tabs.value.findIndex((t) => t.key === key);
  closeTab(key);
  if (!wasActive) return;
  const next = tabs.value[Math.min(idx, tabs.value.length - 1)];
  if (next) await go(next.key);
  else await router.push({ name: 'home' });
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
    <!-- 品牌（图标 + Cylit）：用户口径 2026-11 要它回到顶栏最左，侧栏随之变成纯导航。
         PNG 自带连续圆角 + 透明四角，不要再叠 CSS 圆角（会切掉曲率）。 -->
    <RouterLink class="tabbar-logo" :to="{ name: 'home' }" title="回到工作区">
      <img class="tabbar-mark" :src="BRAND_ICON" alt="" />
      Cylit
    </RouterLink>
    <!-- 滚动按钮 + 标签托盘（两枚圆钮**浮在托盘两端**，哪一侧还藏着内容就只在那侧出现，
         不是两枚并排堆在同一侧）。滚轮在这一整块里都是横向滑动。 -->
    <!-- 一枚论文标签都没有时不渲染托盘（并排的空胶囊只会让人以为坏了）；
         但「+」不参与这个 v-if：空标签条下它是唯一可点的东西。 -->
    <div v-if="tabs.length" class="tabbar-nav" @wheel="onWheel">
      <button
        v-if="canLeft"
        class="nav-scroll left" type="button" title="向左滚动标签" aria-label="向左滚动标签"
        @click="scrollStep(-1)"
      ><AppIcon name="chevron-left" :size="13" /></button>

      <nav ref="strip" class="tab-strip" aria-label="打开的论文标签" @scroll="syncScrollState">
        <RouterLink
          v-for="t in tabs"
          :key="t.key"
          class="tab"
          :class="{ active: t.key === currentKey }"
          :to="tabLocation(t)"
          :title="t.title"
          draggable="true"
          @dragstart="onDragStart(t.key, $event)"
          @dragover.prevent
          @drop="onDrop(t.key)"
          @contextmenu="onContextMenu(t.key, $event)"
        >
          <AppIcon :name="t.icon" :size="13" />
          <span class="tab-title truncate">{{ t.title }}</span>
          <button class="tab-close" title="关闭标签页" @click="onClose(t.key, $event)">
            <AppIcon name="close" :size="11" />
          </button>

          <span v-if="menuFor === t.key" class="tab-menu" @click.stop>
            <button :disabled="!canCloseAll" @click="closeOtherTabs(t.key); menuFor = ''">关闭其它标签</button>
            <button @click="closeTab(t.key); menuFor = ''">关闭标签</button>
          </span>
        </RouterLink>

      </nav>

      <button
        v-if="canRight"
        class="nav-scroll right" type="button" title="向右滚动标签" aria-label="向右滚动标签"
        @click="scrollStep(1)"
      ><AppIcon name="chevron-right" :size="13" /></button>
    </div>

    <!-- 「+」= 打开论文库（导入论文的入口在那边）。常驻最右：托盘先收缩自己，它不会被挤掉。 -->
    <RouterLink
      class="tabbar-new" :to="{ name: 'library' }" data-tabbar-new
      title="打开论文库（导入论文）" aria-label="打开论文库"
    >
      <svg class="tabbar-plus" viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
        <path d="M8 3v10M3 8h10" fill="none" stroke="currentColor"
              stroke-width="1.7" stroke-linecap="round" />
      </svg>
    </RouterLink>
  </header>
</template>

<style scoped>
/* 顶栏：一条细标签条。背景必须**不透明**（半透明 + blur 会在顶部形成一层"纱"）。 */
.tabbar {
  position: sticky; top: 0; z-index: 40; height: var(--header-h);
  display: flex; align-items: center; gap: 8px; padding: 0 10px;
  background: var(--bg-elev); border-bottom: 1px solid var(--border);
}

/* 左上角品牌：图标 + 文字（用户口径 2026-11 从侧栏搬回顶栏）。
   字号/字重比标签略大一档，读起来是"应用名"而不是一枚可点的标签；user-select: none 让它不可选。 */
.tabbar-logo {
  flex: none; display: flex; align-items: center; gap: 7px;
  padding: 0 6px 0 3px;
  font-size: calc(15px * var(--ts, 1)); font-weight: 600; letter-spacing: -.02em; line-height: 1;
  color: var(--accent-text); user-select: none;
  /* 品牌就是"回工作区"的入口（用户口径 2026-11），所以它得是链接：不要下划线，hover 提一档亮度 */
  text-decoration: none; border-radius: var(--r-xs);
}
.tabbar-logo:hover { color: var(--accent); text-decoration: none; }
.tabbar-logo:focus-visible { outline: none; box-shadow: var(--ring); }
.tabbar-mark { display: block; width: 19px; height: 19px; flex: none; }


/* 滚动按钮 + 托盘这一整块：滚轮在这里就是横向滑动。`position: relative` 是给两枚浮动圆钮
   当定位基准的；`flex: 0 1 auto` + `min-width: 0` 让它在标签变多时**先收缩**（托盘横向滚），
   紧跟其后的「+」是 `flex: none`，因此永远不会被标签挤掉。 */
.tabbar-nav { position: relative; display: flex; align-items: center; flex: 0 1 auto; min-width: 0; }

/* 两枚「翻页」圆钮：**浮在托盘两端**、不占布局（哪一侧还有内容才在那一侧出现，
   所以它们的出现不会让标签跳位）。与阅读器侧栏那枚 `.side-scroll-btn` **同一配方、
   同样不画描边** —— 半透明 + 模糊，标签滚到它底下时文字照样透出来。 */
.nav-scroll {
  position: absolute; top: 50%; transform: translateY(-50%); z-index: 2;
  width: 24px; height: 24px; padding: 0; border: 0; border-radius: 8px;
  display: inline-flex; align-items: center; justify-content: center;
  color: var(--text-dim); cursor: pointer;
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--text) 10%, transparent),
    color-mix(in srgb, var(--text) 6%, transparent));
  backdrop-filter: blur(48px) saturate(2.6);
  -webkit-backdrop-filter: blur(48px) saturate(2.6);
  box-shadow: var(--shadow-flyout);
}
.nav-scroll:hover { color: var(--accent-text); }
.nav-scroll:focus-visible { outline: none; box-shadow: var(--ring); }
.nav-scroll.left { left: 4px; }
.nav-scroll.right { right: 4px; }

/* 标签托盘 = **一枚圆角托盘**（用户口径 2026-12：圆形胶囊改圆角胶囊）：
 * ① 托盘（--bg-sunken）② 抬起的白块（--bg-elev + --shadow-1）③ 文字。
 * 与 base.css 的 `.seg`、设置页的分段控件**同一门语言、同一组颜色**，只是圆角一档（10 / 7）。
 * 宽度跟着内容走（标签少时不会拉成一根长条），放不下就横向滚；
 * `3px + 32px` 让托盘正好 38px —— 与阅读器里那枚页签开关同高。 */
.tab-strip {
  display: flex; align-items: center; gap: 3px;
  flex: 0 1 auto; width: fit-content; max-width: 100%; min-width: 0;
  padding: 3px; border: 0; border-radius: 10px;
  background: var(--bg-sunken);
  overflow-x: auto; scrollbar-width: none;
  /* 两端各让出一枚浮动圆钮的位置：滚进视野的标签不会被压在按钮底下 */
  scroll-padding: 0 32px;
}
.tab-strip::-webkit-scrollbar { display: none; }
.tab {
  position: relative;
  display: inline-flex; align-items: center; gap: 6px;
  /* 右内边距 8px：关闭按钮自己还有 3.5px 的余量（18px 盒子装 11px 图标），
   * 于是「图标左边缘」和「× 字形右边缘」到胶囊边缘的距离都是 ~12px —— 肉眼看才是居中的。 */
  height: 32px; padding: 0 8px 0 12px; max-width: 200px; flex: none;
  /* 圆角**胶囊**（7px，比 10px 的托盘小一档 —— 嵌在里面才是"同心的两层"）；
     标题那枚 span 带 .truncate（overflow: hidden），行高只给 1em 时字体的
     ascent + descent 高出内容盒，上下缘被切掉，就是用户报的「胶囊把字裁了」。 */
  border-radius: 7px; color: var(--text-dim);
  font-size: calc(13px * var(--ts, 1)); font-weight: 500; line-height: 1.45; white-space: nowrap;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
/* 没选中**不画底**：托盘本身已经有浅底，再给每枚叠一层只会糊成一片。 */
.tab:hover { background: color-mix(in srgb, var(--text) 9%, transparent); color: var(--text); text-decoration: none; }
.tab.active { background: var(--bg-elev); color: var(--accent-text); font-weight: 600; box-shadow: var(--shadow-1); }
.tab-title { max-width: 130px; cursor: default; }

.tab-close {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; padding: 0; border: 0; border-radius: 6px;
  background: transparent; color: var(--text-faint); cursor: pointer; flex: none;
}
.tab-close:hover { background: var(--bg-pressed); color: var(--text); }

/* 最右的「+」：打开论文库。与托盘同一门语言（圆角方块），但**不铺底**——
   它是动作不是状态，压在浅底托盘旁边再叠一层浅底只会显得像第二枚标签。 */
.tabbar-new {
  flex: none; display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; margin-left: 2px;
  border-radius: 8px; color: var(--text-dim); text-decoration: none;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.tabbar-new:hover { background: var(--bg-hover); color: var(--accent-text); text-decoration: none; }
.tabbar-new:focus-visible { outline: none; box-shadow: var(--ring); }
.tabbar-plus { display: block; }


/* 标签右键菜单：挂在标签内绝对定位，点空白处由标签条的 click 收起 */
.tab-menu {
  position: absolute; top: 36px; left: 4px; z-index: 50;
  display: flex; flex-direction: column; min-width: 132px; padding: 4px;
  background: var(--bg-elev); border: 1px solid var(--border);
  border-radius: var(--r-sm); box-shadow: var(--shadow-2);
}
.tab-menu button {
  border: 0; background: transparent; color: var(--text); text-align: left;
  padding: 6px 9px; border-radius: var(--r-xs); font-size: calc(12.5px * var(--ts, 1)); cursor: pointer;
}
.tab-menu button:hover:not(:disabled) { background: var(--bg-hover); }
.tab-menu button:disabled { opacity: .45; cursor: default; }
</style>
