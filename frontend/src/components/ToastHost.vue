<script setup lang="ts">
/** Toast 宿主：统一承载成功/警告/错误提示。
 *
 * 观感（用户口径 2026-12-02）：照 Element UI 的 Notification 那套 —— 右上角一张卡片，
 * 图标 + 标题 + 正文 + 右上角 ✕，换成**本系统主题色**（颜色只从令牌取，不写死红绿），
 * 圆角比全站的胶囊档再大一点（--radius-lg + 6px）。
 *
 * 为什么不再用「顶部居中胶囊」：那是 2026-10-03 的口径（居中的理由是"视线扫得到"），
 * 但它横在视口顶部中央，正好压在阅读器主工具栏上（当年被迫把整条 toast 设成不吃指针事件，
 * 于是连关闭按钮都点不了）。挪到右上角之后这两个毛病一起消失：卡片能收点击，也不再挡中间。
 *
 * 级别只由**图标颜色 + 标题**表达，左侧色条仍然不画（用户口径 2026-10-03 定下的规矩）。
 */
import { dismissToast, state } from '@/stores/app';
import AppIcon from '@/components/AppIcon.vue';

/** 提示级别 → 图标（全部走 AppIcon，不使用 emoji）。 */
const ICON: Record<string, string> = {
  ok: 'check_circle', success: 'check_circle', warn: 'warning', warning: 'warning',
  error: 'error', danger: 'error',
};

/** 提示级别 → 标题：store 里只有 message，标题按级别派生（Element 那套也有标题行）。 */
const TITLE: Record<string, string> = {
  ok: '成功', success: '成功', warn: '注意', warning: '注意',
  error: '出错了', danger: '出错了',
};

function iconOf(kind?: string | null): string {
  return ICON[String(kind ?? '')] || 'info';
}

function titleOf(kind?: string | null): string {
  return TITLE[String(kind ?? '')] || '提示';
}
</script>

<template>
  <div class="toast-host">
    <TransitionGroup name="toast">
      <div v-for="t in state.toasts" :key="t.id" :class="['toast', t.kind]">
        <span class="toast-ico"><AppIcon :name="iconOf(t.kind)" :size="16" /></span>
        <div class="toast-body">
          <div class="toast-title">{{ titleOf(t.kind) }}</div>
          <div class="toast-text">{{ t.message }}</div>
        </div>
        <button class="toast-x" type="button" aria-label="关闭提示" @click="dismissToast(t.id)">
          <AppIcon name="close" :size="13" />
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
/* 右上角一列，从标签条下方起：--header-h 让开顶栏，--toast-drop 仍留给页面
   （阅读器会把它设成"悬浮胶囊底边 + 8px"，于是提示落在胶囊下面）。
   pointer-events:none 挂在宿主上 —— 它是一整列隐形容器，只有卡片本体该收点击。 */
.toast-host {
  position: fixed; top: calc(var(--header-h) + 16px + var(--toast-drop, 0px));
  right: 18px;
  z-index: 200; display: flex; flex-direction: column; align-items: flex-end; gap: 10px;
  /* ⚠️ 宽度必须**给死**，不能用 max-width + 子项 width:100%：宿主是 fixed + right，
     宽度靠"收缩适应"，子项的 100% 就会算到错的基准上（实测卡片右边缘越出视口 2px）。 */
  width: min(420px, calc(100vw - 36px));
  pointer-events: none;
}
/* 卡片本体：实底 + 细描边 + 浮层投影（比玻璃更符合"通知"的语义：它要能一眼读清）。 */
.toast {
  pointer-events: auto;
  display: flex; align-items: flex-start; gap: 10px;
  width: 100%; padding: 13px 13px 14px 14px;
  border: 1px solid var(--border);
  border-radius: calc(var(--radius-lg) + 6px);
  background: var(--bg-elev);
  box-shadow: var(--shadow-flyout);
  font-size: calc(13px * var(--ts, 1)); line-height: 1.55;
}
/* 图标底色取**自己颜色**的浅调：换肤、换级别都跟着走，不必为四档各写一条背景 */
.toast-ico {
  flex: none; display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; margin-top: 1px;
  border-radius: 999px;
  color: var(--accent-text);
  background: color-mix(in srgb, currentColor 13%, transparent);
}
.toast-body { flex: 1 1 auto; min-width: 0; }
.toast-title {
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 650; color: var(--text);
}
.toast-text {
  margin-top: 2px;
  font-size: calc(12.5px * var(--ts, 1)); color: var(--text-dim);
  word-break: break-word;
}
.toast-x {
  flex: none; width: 22px; height: 22px; padding: 0; margin-top: 1px;
  display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: 999px; background: transparent;
  color: var(--text-dim); cursor: pointer;
  transition: background 120ms ease, color 120ms ease;
}
.toast-x:hover { background: var(--bg-sunken); color: var(--text); }
.toast.ok .toast-ico, .toast.success .toast-ico { color: var(--ok); }
.toast.warn .toast-ico, .toast.warning .toast-ico { color: var(--warn); }
.toast.error .toast-ico, .toast.danger .toast-ico { color: var(--danger); }

/* ------------------------------ 动画 ------------------------------
 * 三件事一起做，才有"通知"那种利落感：
 *   ① 进：从右边滑进来 + 轻微放大（24px 位移，不大动干戈）；
 *   ② 出：往回滑 + 淡出（它就在右边，往上飘会像"掉下来"）；
 *   ③ 中间那张被关掉后，剩下的**平滑上移**补位 —— 少了这条，列表会"跳"一下。
 * 时长/缓动全部吃全站令牌（--dur / --ease），跟其它浮层一个节奏。 */
.toast-enter-active { transition: opacity var(--dur) var(--ease), transform var(--dur) var(--ease); }
.toast-leave-active {
  transition: opacity 140ms var(--ease), transform 140ms var(--ease);
  position: absolute;          /* 离场时脱离布局，下面的卡片立刻补位 */
  right: 18px;
}
.toast-enter-from { opacity: 0; transform: translateX(28px) scale(.96); }
.toast-leave-to { opacity: 0; transform: translateX(20px) scale(.98); }
.toast-move { transition: transform var(--dur) var(--ease); }
/* 图标入场的微弹：只动图标本身，卡片几何不变（不然一行字会跟着抖） */
@keyframes toast-ico-pop {
  0% { transform: scale(.6); opacity: 0; }
  60% { transform: scale(1.08); opacity: 1; }
  100% { transform: scale(1); opacity: 1; }
}
.toast-enter-active .toast-ico { animation: toast-ico-pop 320ms var(--ease) both; }

@media (prefers-reduced-motion: reduce) {
  .toast-enter-active, .toast-leave-active, .toast-move { transition-duration: 1ms; }
  .toast-enter-active .toast-ico { animation: none; }
}

@media (max-width: 640px) {
  /* 窄屏：贴满两侧留 12px，别再让 max-width 把它裁成一条窄条 */
  .toast-host { left: 12px; right: 12px; width: auto; align-items: stretch; }
}
</style>
