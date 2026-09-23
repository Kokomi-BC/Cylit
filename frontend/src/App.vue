<script setup lang="ts">
/** 应用根组件：组装头部 / 路由视图 / Toast；阅读器使用通栏布局。 */
import { computed } from 'vue';
import { RouterView, useRoute } from 'vue-router';
import AppHeader from '@/components/AppHeader.vue';
import ToastHost from '@/components/ToastHost.vue';

const route = useRoute();
const wide = computed(() => route.name === 'reader');
</script>

<template>
  <a class="skip-link" href="#main">跳到主内容</a>
  <AppHeader />
  <main id="main" :class="['app-main', { wide }]">
    <RouterView />
  </main>
  <ToastHost />
</template>
