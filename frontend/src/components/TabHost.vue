<script setup lang="ts">
/**
 * 论文标签宿主：**打开过的论文标签全部留在 DOM 里**，切标签只切显示（`display`）。
 *
 * 为什么不用 `KeepAlive`：Vue 的 KeepAlive 是把整棵 DOM **摘下来**（脱离文档）再插回去。
 * 实测那样切回上一篇：接口请求是省掉了（0 条），但正文出现仍是 1.9 s，PDF 文字层还要 4 s 才铺满 ——
 * 因为插回时浏览器把滚动位置归零、页图按新视口重排了一遍。用户等的那一段一点没省。
 *
 * 这里的做法是"永不卸载"：标签一旦打开就挂在文档里，非激活的只 `display: none`。
 * 浏览器因此保留布局与滚动位置，切回来是一帧的事（实测见 docs/dev-log）。
 *
 * 代价与约束（都必须照顾到）：
 * · **内存**：每份含一整棵阅读器（PDF 视图 + 会话）。上限 `MAX_LIVE`，超了先丢最久没看过的那份；
 * · **路由**：多个实例同时存在时 `useRoute()` 必须各看各的 —— 这里按实例 `provide(routeLocationKey)`，
 *   每个视图拿到的是**自己标签**的那份 location（只有激活的那个会被路由变化刷新）；
 * · **可见性**：宿主 `provide(TAB_ACTIVE)`，视图据此摘掉页面级监听、停掉阅读计时（见 `useTabActive`）。
 */
import {
  computed, defineComponent, h, nextTick, onBeforeUnmount, provide, ref, watch, type Component,
} from 'vue';
import {
  routeLocationKey, useRoute, useRouter,
  type RouteLocationNormalizedLoaded, type RouteLocationRaw,
} from 'vue-router';
import { isPaperRoute, routeKey, tabs } from '@/stores/tabs';
import { TAB_ACTIVE } from '@/stores/tabHost';

/** 同时留在 DOM 里的论文标签上限 */
const MAX_LIVE = 4;

interface LiveView {
  key: string;
  /** 这个标签自己的路由位置（冻结快照：不会跟着别的标签跳） */
  loc: Record<string, unknown>;
  /** 最近一次成为激活标签的时间，用来淘汰 */
  seen: number;
}

const route = useRoute();
const router = useRouter();

const live = ref<LiveView[]>([]);

function isPaper(): boolean {
  return isPaperRoute(String(route.name ?? ''));
}

function snapshot(key: string): LiveView {
  return {
    key,
    loc: {
      name: route.name,
      params: { ...route.params },
      query: { ...route.query },
      hash: route.hash,
      path: route.path,
      fullPath: route.fullPath,
      meta: route.meta,
    },
    seen: Date.now(),
  };
}

/** 激活标签：论文路由 = 它的标签键；导航页 = 空（所有论文视图都藏起来，交给 RouterView 渲染） */
const activeKey = computed(() => (isPaper() ? routeKey(route) : ''));

watch(
  () => route.fullPath,
  () => {
    if (!isPaper()) return;
    const key = routeKey(route);
    const cur = live.value.find((e) => e.key === key);
    if (cur) {
      // **就地改写**而不是换对象：provide 出去的 routeLocationKey 就是这个对象，
      // 换引用等于把这个视图的路由换成另一份（子组件的 watch 也就跟着断了）
      Object.assign(cur.loc, snapshot(key).loc);
      cur.seen = Date.now();
    } else {
      live.value.push(snapshot(key));
    }
    // 上限：丢了最久没看的那些（当前这个永不丢）
    while (live.value.length > MAX_LIVE) {
      const victim = live.value
        .filter((e) => e.key !== key)
        .sort((a, b) => a.seen - b.seen)[0];
      if (!victim) break;
      live.value = live.value.filter((e) => e !== victim);
    }
  },
  { immediate: true },
);

/** 标签被关掉 → 对应的常驻视图一起销毁（否则关掉的论文还在内存里挂着）。
 *  ⚠️ 不能直接 `watch(tabs)`：那是个 computed，返回的**是同一个数组对象**（`splice` 原地改），
 *  按引用比较根本不会触发。改成看"开着的标签键串"。 */
watch(
  () => tabs.value.map((t) => t.key).join('|'),
  () => {
    const open = new Set(tabs.value.map((t) => t.key));
    const kept = live.value.filter((e) => open.has(e.key));
    if (kept.length !== live.value.length) live.value = kept;
  },
);

/* 切标签**只切显示**，不给页面做进入动画（用户口径 2026-12-11：「我指的是指示器的滑块动画，
 *  而不是页面的动画」）。整棵阅读器淡入 / 位移会让人以为"页面又加载了一次"，
 *  而这个宿主的全部理由恰恰是"永不卸载、切回来是一帧"—— 两种手感是冲突的。
 *  指示器那一枚滑块在标签条上（components/TabBar.vue），与内容无关。 */

/** 真正渲染一个标签的那棵视图树（provide 只在 per-view 组件作用域里才成立） */
const TabView = defineComponent({
  props: {
    loc: { type: Object, required: true },
    active: { type: Boolean, default: false },
  },
  setup(props) {
    // 直接把这个标签自己的 location 交出去（它和路由 install 时 provide 的是同一类东西：
    // 一个可响应的 normalized location）。这样子组件里的 `useRoute()` 看到的是**本标签**的。
    provide(routeLocationKey, props.loc as unknown as RouteLocationNormalizedLoaded);
    provide(TAB_ACTIVE, computed(() => props.active));
    const comp = computed<Component | null>(() => {
      const resolved = router.resolve(props.loc as RouteLocationRaw);
      const record = resolved.matched[resolved.matched.length - 1];
      return (record?.components?.default as Component) ?? null;
    });
    return () => (comp.value ? h(comp.value) : null);
  },
});

defineExpose({ live });
onBeforeUnmount(() => { live.value = []; });
</script>

<template>
  <div class="tab-host">
    <div
      v-for="e in live"
      :key="e.key"
      class="tab-host-pane"
      :class="{ on: e.key === activeKey }"
      :data-tab-pane="e.key"
    >
      <component :is="TabView" :loc="e.loc" :active="e.key === activeKey" />
    </div>
  </div>
</template>

<style scoped>
/* display: contents 是必需的：面板只是一个"开关"，不能让阅读器多出一层盒子
   （阅读器的高度链 100%/flex 都建立在外壳给的容器上）。 */
.tab-host { display: contents; }
.tab-host-pane { display: contents; }
/* 非激活的标签整棵隐藏，但**不卸载**：布局、滚动位置、页图都留着 */
.tab-host-pane:not(.on) { display: none; }
</style>
