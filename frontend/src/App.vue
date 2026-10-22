<script setup lang="ts">
/**
 * 应用根组件：多标签外壳（左侧栏 + 论文标签条）/ 路由视图 / Toast。
 *
 * 外壳由 `shellMode` 一个字符串驱动（模板里只读这一处，避免三个 v-if 各写一套条件）：
 *   - `rail`：**左侧栏 + 标签条**。导航页（首页 / 论文库 / 知识库 / 学习 / 笔记 / 设置）走这个形态；
 *   - `tabs`：**只有标签条**。`reader` 用 —— 用户明确要求"论文阅读器不显示侧栏"，
 *     工作区占满整宽（阅读器自己要在有限宽度里排双语列，少 216px 就是多一整列）；
 *   - `bare`：**整套外壳都不显示**。`chat` 用 —— ChatView 自己就是一屏，套一层标签条只会浪费高度。
 *
 * 标签条只装论文标签（`stores/tabs` 的 `isPaperRoute`），导航住在侧栏
 * （`components/AppSidebar.vue`）：改造前导航在首页、顶栏是"什么都能开"的标签条，
 * 于是首页/设置这类页面也被当成标签堆在顶栏里，既关不掉也点不亮。
 */
import { computed } from 'vue';
import { RouterView, useRoute } from 'vue-router';
import AppSidebar from '@/components/AppSidebar.vue';
import ConfirmHost from '@/components/ConfirmHost.vue';
import TabBar from '@/components/TabBar.vue';
import ToastHost from '@/components/ToastHost.vue';
import { routeKey } from '@/stores/tabs';
import { railCollapsed } from '@/stores/shellPrefs';

const route = useRoute();

/** 三种外壳形态，见文件头注释。 */
const shellMode = computed<'rail' | 'tabs' | 'bare'>(() => {
  if (route.name === 'chat') return 'bare';
  if (route.name === 'reader') return 'tabs';
  return 'rail';
});
/** 阅读器：整屏布局且自身不滚动（滚动都在栏内），见 base.css 的 `.app-main.wide` */
const wide = computed(() => route.name === 'reader');

/**
 * 路由视图的 `:key` = 标签键（路由名 + 参数）。
 *
 * **为什么必须加**：vue-router 复用同一个路由记录下的组件实例 —— `/reader/A` 切到 `/reader/B`
 * 只换 `params`，`ReaderView` 不会重建，`onMounted` 里的加载逻辑也就不会跑。表现就是
 * 「点另一个论文的标签，地址栏变了、内容还是上一篇」（用户报的"多标签页切换论文失效"）。
 * 用一个和标签一一对应的 key，切论文 = 换实例 = 重新加载，跟"新开一个标签"的语义完全一致。
 *
 * 只按 `name + params` 计算（不看 query）：同一篇论文里切侧栏页签 / 跳到某一行（query 变化）
 * 不该重建整个阅读器。
 */
const viewKey = computed(() => routeKey(route));
</script>

<template>
  <a class="skip-link" href="#main">跳到主内容</a>
  <div class="shell" :class="[`shell-${shellMode}`, { 'is-rail-collapsed': railCollapsed }]">
    <TabBar v-if="shellMode !== 'bare'" />
    <div class="shell-body">
      <AppSidebar v-if="shellMode === 'rail'" />
      <!-- settings：设置页要通栏（左栏贴主区左缘），单独放开 .app-main 的封顶与左内边距，见 base.css -->
      <main id="main" :class="['app-main', { wide, fill: shellMode === 'bare', settings: route.name === 'settings' }]">
        <RouterView :key="viewKey" />
      </main>
    </div>
  </div>
  <ToastHost />
  <!-- 站内确认框：全局挂一次，任何地方 `await askConfirm({...})`（见 stores/confirm.ts） -->
  <ConfirmHost />
</template>

<style scoped>
/* 外壳 = 两行网格：① 顶栏（标签条，**横跨整宽**）② 内容行（侧栏 + 主区）。
 *
 * 为什么顶栏在网格里、而不是"左列侧栏 + 右列顶栏+内容"：用户要的是顶栏通栏 —— 侧栏从顶栏
 * **下方**才开始（见 `AppSidebar` 的 sticky top: var(--header-h)）。行高 auto 让顶栏保持
 * var(--header-h)，1fr 把剩下的高度给内容行。
 * ⚠️ 滚动仍然发生在**窗口**上，不给内容列单开滚动容器：`router` 的 scrollBehavior、
 * 阅读器进度、各处 `window.scrollY` 都建立在这个前提上。 */
/* ⚠️ 显式写一列 `minmax(0, 1fr)`：不写的话隐式列是 `auto`，会被内容撑成"按内容收缩"——
 * 实测阅读器主区会从整宽缩到 164px（`.shell-body` 里那列 1fr 于是在一个窄容器里解析）。 */
.shell {
  display: grid; grid-template-rows: auto 1fr;
  grid-template-columns: minmax(0, 1fr);
  min-height: 100%;
}
/* bare（`chat`）没有顶栏那一行，单独声明一次，免得内容被塞进 auto 行里按内容高度趴着 */
.shell-bare { grid-template-rows: 1fr; }

/* 内容行：默认单列（阅读器 / chat —— 没有侧栏时主区满宽）。
 * ⚠️ **必须是 grid，不能是 flex 行**：`.app-main` 带 `margin: 0 auto`，在 flex 容器里
 * auto 外边距会让子项放弃拉伸、按内容收缩（实测阅读器主区从整宽缩到 481px）。
 * grid 轨道给 `minmax(0, 1fr)`、项默认 stretch，才和块级流等价。 */
.shell-body { display: grid; grid-template-columns: minmax(0, 1fr); min-width: 0; }
/* ⚠️ `.app-main` 自带 `margin: 0 auto`：在 **grid** 里 auto 外边距同样会取消 stretch，
   主区于是按内容收缩（实测阅读器主区 164px，flex 时代是 481px）。显式给满宽，让 auto 只管居中。 */
.shell-body > .app-main { width: 100%; }
.shell-rail .shell-body { grid-template-columns: var(--rail-w) minmax(0, 1fr); }
/* 收起成图标轨：**轨道一起收**（用户口径：收起要立刻反应，不要动画）。
   只让 `.rail` 自己变窄是不够的 —— 这一列写的是 `var(--rail-w)`（216px），
   主区仍会从 216 起排，右边留一条空带。 */
.shell-rail.is-rail-collapsed .shell-body { grid-template-columns: var(--rail-w-min) minmax(0, 1fr); }

/* <720px 侧栏整栏 display:none —— 但显式轨道不会自己消失，必须同时把这一列收掉，
   否则主区右边会白留一条 64px。 */
@media (max-width: 720px) {
  .shell-rail .shell-body { grid-template-columns: minmax(0, 1fr); }
}
</style>
