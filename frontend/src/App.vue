<script setup lang="ts">
/** 应用根组件：顶栏（标签条）/ 路由视图 / Toast；阅读器使用通栏布局。
 *
 * 外壳 = **多标签页**：`TabBar` 显示当前打开的路由位置，导航入口在首页（`HomeView`）。
 * 标签状态由 `stores/tabs` 持有（localStorage 持久化），路由变化时自动登记（见 router）。
 */
import { computed } from 'vue';
import { RouterView, useRoute } from 'vue-router';
import TabBar from '@/components/TabBar.vue';
import ToastHost from '@/components/ToastHost.vue';
import { routeKey } from '@/stores/tabs';

const route = useRoute();
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
  <TabBar />
  <main id="main" :class="['app-main', { wide }]">
    <RouterView :key="viewKey" />
  </main>
  <ToastHost />
</template>
