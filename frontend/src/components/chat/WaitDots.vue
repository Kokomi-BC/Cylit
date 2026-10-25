<script setup lang="ts">
/**
 * WaitDots —— 「正在深度求索」那三颗点。
 *
 * 几何照抄参考实现（dsh web 的 `chat-wait-lane is-line`）：容器 18×14，三颗 4px 圆点分别钉在
 * x = 0 / 7 / 14，各自沿一条斜线往返 16px，中点缩到 0.55。三颗点错开 1/4、7/12、11/12 个周期，
 * 合起来是「三点并成一条斜线 → 摊开 → 再并回去」，1.25s 一轮、linear（不能缓动，一缓就成一坨）。
 */
</script>

<template>
  <span class="wait-dots" aria-hidden="true"><i /><i /><i /></span>
</template>

<style scoped>
.wait-dots {
  position: relative;
  display: inline-block;
  flex: none;
  width: 18px;
  height: 14px;
}
.wait-dots i {
  position: absolute;
  bottom: 0;
  left: 0;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentColor;
  transform-origin: 50% 100%;
  animation-duration: 1.25s;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
}
.wait-dots i:nth-child(1) { animation-name: dot-line-1; animation-delay: calc(-1.25s / 4); }
.wait-dots i:nth-child(2) { left: 7px; animation-name: dot-line-2; animation-delay: calc(-7 * 1.25s / 12); }
.wait-dots i:nth-child(3) { left: 14px; animation-name: dot-line-3; animation-delay: calc(-11 * 1.25s / 12); }

/* 三条关键帧的数值由 is-line 的变量算出来（travel 8px、centre 7/0/-7、line-x -.866、line-y -1/-.5/.5）：
   0% 与 100% 在同一头，50% 在另一头；25%/75% 只改 scale，translate 因此匀速穿过整段。 */
@keyframes dot-line-1 {
  0%, 100% { translate: 7px 0px; scale: 1; }
  25%, 75% { scale: .55; }
  50% { translate: 7px -16px; scale: 1; }
}
@keyframes dot-line-2 {
  0%, 100% { translate: 6.93px -4px; scale: 1; }
  25%, 75% { scale: .55; }
  50% { translate: -6.93px -12px; scale: 1; }
}
@keyframes dot-line-3 {
  0%, 100% { translate: -.07px -12px; scale: 1; }
  25%, 75% { scale: .55; }
  50% { translate: -13.93px -4px; scale: 1; }
}
</style>
