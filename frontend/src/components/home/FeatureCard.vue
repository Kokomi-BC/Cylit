<!--
  首页 2×2 里的一张功能卡。
  单独成组件，是因为四张卡的内部结构**必须完全一样**（等宽等高、三段式：图标行 / 文字 / CTA）：
  任何一张手写结构走样，表现就是"同排卡片的标题和 CTA 不齐"——上一版就是这么翻车的。

  整卡是一个 <button>：右下角那枚 CTA 只是**文案**，不再做成第二个可点元素
  （<button> 里嵌 <button>/<a> 在 HTML 里不合法，点击行为也会互相打架）。
  右上角那枚圆形描边钮同理是纯装饰，它的 hover 态由整卡 hover 触发。
-->
<script setup lang="ts">
import AppIcon from '@/components/AppIcon.vue';

defineProps<{
  icon: string;
  label: string;
  hint: string;
  cta: string;
  /** 首卡点一下主题色（--accent-soft / --accent-text），其余用 --bg-sunken */
  accent?: boolean;
}>();
const emit = defineEmits<{ (e: 'open'): void }>();
</script>

<template>
  <button type="button" class="fcard" @click="emit('open')">
    <span class="top">
      <span class="ico" :class="{ accent }"><AppIcon :name="icon" :size="19" /></span>
      <span class="go"><AppIcon name="arrow-ne" :size="14" /></span>
    </span>
    <span class="body">
      <span class="title">{{ label }}</span>
      <span class="hint">{{ hint }}</span>
    </span>
    <span class="cta">{{ cta }}</span>
  </button>
</template>

<style scoped>
/* 卡片本体：全站 token，不用全局 .card（那条 .card + .card 的 margin-top 会把同排卡片顶歪） */
.fcard {
  display: flex; flex-direction: column; gap: 14px;
  width: 100%; height: 100%; padding: 20px 22px; margin: 0;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elev);
  box-shadow: var(--shadow-1); color: var(--text);
  font: inherit; text-align: left; cursor: pointer; appearance: none;
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease),
              border-color var(--dur) var(--ease);
}
.fcard:hover { transform: translateY(-1px); box-shadow: var(--shadow-2); }
.fcard:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }

.top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
/* 图标方片的 11px 是设计稿定值：全站最小的圆角 token 是 --radius-sm(14px)，比卡片的 14px 还小的
   一档只存在于这枚 38px 方片上，写死比新造一个 token 更省事（四张卡共用这一处）。 */
.ico {
  display: grid; place-items: center; flex: none; width: 38px; height: 38px;
  border-radius: 11px; background: var(--bg-sunken); color: var(--text-dim);
  transition: color var(--dur) var(--ease);
}
.ico.accent { background: var(--accent-soft); color: var(--accent-text); }
.fcard:hover .ico:not(.accent) { color: var(--accent); }

/* 32px 正圆描边钮：只有描边和箭头，不填色 —— 它表达"可以进去"，不该抢标题的注意力 */
.go {
  display: grid; place-items: center; flex: none; width: 32px; height: 32px;
  border: 1px solid var(--border); border-radius: 50%; color: var(--text-faint);
  transition: border-color var(--dur) var(--ease), color var(--dur) var(--ease);
}
.fcard:hover .go { border-color: var(--border-strong); color: var(--accent); }

.body { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.title { font-size: calc(17.5px * var(--ts, 1)); font-weight: 620; letter-spacing: -.025em; }
.hint { font-size: calc(13.5px * var(--ts, 1)); line-height: 1.65; color: var(--text-dim); }

/* margin-top: auto 让四张卡（等宽等高）里的 CTA 底边对齐 —— 描述长度不一时尤其明显 */
.cta {
  margin-top: auto; padding-top: 2px;
  font-size: calc(13px * var(--ts, 1)); font-weight: 600; color: var(--accent-text);
}
@media (prefers-reduced-motion: reduce) {
  .fcard, .fcard:hover { transform: none; }
}
</style>
