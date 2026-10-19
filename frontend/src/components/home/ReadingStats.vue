<!--
  首页底部左列「阅读统计」卡。
  在多标签外壳里它只是首页的一块，不占标签、自己不发请求以外的事。

  数据口径（**只有一处**）：`GET /home/summary`（backend/services/home/home_service.py）。
  后端**没有**"阅读时长 / 日均时长 / 连续天数"这类字段 —— `reading_progress` 只存"读到哪"，
  `paper.last_read_at` 只有一个时间戳，`paper.read_time_min` 是元数据里**估**出来的建议时长。
  所以这里不编时长：三枚数字换成接口里真有的量（已解读篇数 / 笔记条数 / 标注处数），
  并把设计稿里的 7 / 30 / 90 天分段控件**整个去掉** —— 没有时间切片的数据就别给切片的入口。
  同理没有时间序列，折线图也一并去掉，卡片收成纯数字（少画一条假曲线）。

  本机确实另有一本"按天读了多少秒"的账（utils/readerStats.ts），但它是**单浏览器**的、
  换设备就没了，混进这张卡会让人以为那是服务端口径，所以不用。
  概览一个数字都没有（新库）或请求失败时整张卡不渲染，不留空壳。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { api } from '@/api/client';
import type { HomeSummary } from '@/api/types';

const summary = ref<HomeSummary | null>(null);

onMounted(async () => {
  try {
    summary.value = await api.get<HomeSummary>('/home/summary');
  } catch {
    summary.value = null;      // 首页不该因为一个统计数字弹错
  }
});

const metrics = computed(() => {
  const s = summary.value;
  if (!s) return [];
  return [
    { key: 'understandings', label: '已解读篇数', value: s.understandings, unit: '篇' },
    { key: 'notes', label: '笔记条数', value: s.notes, unit: '条' },
    { key: 'highlights', label: '标注处数', value: s.highlights, unit: '处' },
  ];
});

/** 三个数全是 0 等于"还没有任何阅读记录"，这块没有信息量 → 整张卡不渲染 */
const show = computed(() => metrics.value.some((m) => m.value > 0));
</script>

<template>
  <section v-if="show" class="stats" aria-labelledby="home-stats-title">
    <header class="head">
      <span class="ico"><AppIcon name="gauge" :size="17" /></span>
      <div class="htext">
        <h2 id="home-stats-title">阅读统计</h2>
        <p>记录阅读节奏，了解自己的阅读习惯。</p>
      </div>
    </header>
    <dl class="nums">
      <div v-for="m in metrics" :key="m.key" class="num">
        <dt>{{ m.label }}</dt>
        <dd>{{ m.value }}<em>{{ m.unit }}</em></dd>
      </div>
    </dl>
  </section>
</template>

<style scoped>
.stats {
  display: flex; flex-direction: column; gap: 16px;
  height: 100%; padding: 16px 18px 18px;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elev);
  box-shadow: var(--shadow-1);
}
.head { display: flex; align-items: center; gap: 10px; }
.ico {
  display: grid; place-items: center; flex: none; width: 38px; height: 38px;
  border-radius: 11px; background: var(--bg-sunken); color: var(--text-dim);
}
.htext { min-width: 0; }
.htext h2 { margin: 0; font-size: calc(17px * var(--ts, 1)); font-weight: 620; letter-spacing: -.02em; }
.htext p { margin: 3px 0 0; font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint); }

/* 三枚数字一排：卡宽只有 1.25fr，纵排会把卡撑得很高。
   没有折线图之后这一卡比右列两张列表矮一截，而底部三列是**等高**的（stretch），
   所以数字块用 margin: auto 0 在剩余高度里垂直居中 —— 空白被分到上下两侧，
   不会再出现"卡下三分之一整片空着"的观感。不靠加内容填高度。 */
.nums { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: auto 0; }
.num { min-width: 0; }
.num dt { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.num dd {
  margin: 6px 0 0; font-size: calc(26px * var(--ts, 1)); font-weight: 600; line-height: 1.1;
  letter-spacing: -.03em; color: var(--text); font-variant-numeric: tabular-nums;
}
.num dd em {
  margin-left: 4px; font-size: calc(12px * var(--ts, 1)); font-weight: 400;
  font-style: normal; letter-spacing: 0; color: var(--text-faint);
}
</style>
