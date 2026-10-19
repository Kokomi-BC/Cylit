<!--
  首页底部的两张"最近…"列表卡（最近打开的论文 / 最近创建的笔记）。
  两块的差别只有一处：次行是**一行**（作者）还是**两行摘要**（笔记正文），所以合成一个组件，
  免得两个文件里各写一遍省略号规则、又慢慢长歪。

  数据全部由 HomeView 算好传进来（行文案、相对时间都在那边拼），这里只管画和发事件：
  卡片本身不是按钮（卡头还有"全部"这条链），只有列表行是按钮 —— 嵌套按钮在 HTML 里不合法。
-->
<script setup lang="ts">
import AppIcon from '@/components/AppIcon.vue';

/** 行数据由 HomeView 拼好传进来；类型留在这里，两处结构必须一致 */
interface ListRow {
  id: string;
  title: string;
  /** 次行：论文是作者，笔记是正文摘要；空串则整行不占位 */
  sub: string;
  /** 相对时间（"2 分钟前"），空串则不显示 */
  time: string;
}

withDefaults(defineProps<{
  icon: string;
  title: string;
  rows: ListRow[];
  rowIcon?: string;
  moreLabel?: string;
  /** true = 次行允许两行（-webkit-line-clamp: 2），false = 单行省略 */
  clampSub?: boolean;
}>(), { rowIcon: 'document' });

const emit = defineEmits<{ (e: 'select', id: string): void; (e: 'more'): void }>();
</script>

<template>
  <section class="rcard">
    <header class="head">
      <span class="hico"><AppIcon :name="icon" :size="17" /></span>
      <h2>{{ title }}</h2>
      <button v-if="moreLabel" type="button" class="more" @click="emit('more')">{{ moreLabel }}</button>
    </header>
    <ul class="list">
      <li v-for="r in rows" :key="r.id">
        <button type="button" class="row" @click="emit('select', r.id)">
          <span class="rico"><AppIcon :name="rowIcon" :size="17" /></span>
          <span class="rtext">
            <span class="rtitle">{{ r.title }}</span>
            <span v-if="r.sub" class="rsub" :class="{ clamp: clampSub }">{{ r.sub }}</span>
          </span>
          <span v-if="r.time" class="rtime">{{ r.time }}</span>
        </button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.rcard {
  display: flex; flex-direction: column; gap: 10px;
  height: 100%; padding: 16px 18px 10px;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elev);
  box-shadow: var(--shadow-1);
}
.head { display: flex; align-items: center; gap: 10px; }
.hico {
  display: grid; place-items: center; flex: none; width: 38px; height: 38px;
  border-radius: 11px; background: var(--bg-sunken); color: var(--text-dim);
}
.head h2 { flex: 1; min-width: 0; margin: 0; font-size: calc(17px * var(--ts, 1)); font-weight: 620; letter-spacing: -.02em; }
.more {
  flex: none; padding: 0; border: 0; background: none; appearance: none; cursor: pointer;
  font-family: inherit; font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; color: var(--accent-text);
}
.more:hover { text-decoration: underline; }
.more:focus-visible { outline: none; box-shadow: var(--ring); border-radius: 6px; }

.list { display: flex; flex-direction: column; margin: 0; padding: 0; list-style: none; }
.row {
  display: flex; align-items: center; gap: 10px; width: 100%;
  padding: 9px 8px; margin: 0; border: 0; border-radius: var(--radius-sm);
  background: none; color: var(--text); font: inherit; text-align: left; cursor: pointer; appearance: none;
  transition: background var(--dur) var(--ease);
}
.row:hover { background: var(--bg-hover); }
.row:focus-visible { outline: none; box-shadow: var(--ring); }
/* 34px 方片配 10px 圆角：比卡头的 11px 再小一档，缩略图标才不显得"圆得过头" */
.rico {
  display: grid; place-items: center; flex: none; width: 34px; height: 34px;
  border-radius: 10px; background: var(--bg-sunken); color: var(--text-faint);
}
.rtext { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
.rtitle {
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rsub {
  font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 两行摘要：笔记正文压成两行，比单行更能看出"这条笔记写了什么" */
.rsub.clamp {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; white-space: normal;
}
.rtime { flex: none; align-self: flex-start; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
</style>
