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
  <AppHeader />
  <main :class="['app-main', { wide }]">
    <RouterView v-slot="{ Component }">
      <Suspense>
        <component :is="Component" />
        <template #fallback>
          <div class="empty">正在加载页面…</div>
        </template>
      </Suspense>
    </RouterView>
  </main>
  <ToastHost />
</template>
