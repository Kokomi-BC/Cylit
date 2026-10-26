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
  /* 提示永远在最上层（弹窗 250 / 菜单 300 / 右键 360 都不会盖住它）。
     ⚠️ 为什么是 10100 而不是 400：阅读器的大图查看器要压过**整条浮层序列**
     （ReaderView 的 layerZ 从 60 起、每次聚焦 +1、没有上限），它现在是 10000；
     大图开着时照样会弹提示（另存图片 / 复制成功），所以 toast 必须比它**再高一档**。 */
  z-index: 10100; display: flex; flex-direction: column; align-items: flex-end; gap: 10px;
  /* ⚠️ 宽度必须**给死**，不能用 max-width + 子项 width:100%：宿主是 fixed + right，
     宽度靠"收缩适应"，子项的 100% 就会算到错的基准上（实测卡片右边缘越出视口 2px）。 */
  width: min(420px, calc(100vw - 36px));
  pointer-events: none;
}
/* 卡片本体：**主题色**的浅底 + 同色系描边 + 浮层投影（用户口径 2026-12-02）。
   底子仍是 `--bg-elev`（深浅两档都能读），只掺 7% 主色 —— 换肤时整张卡跟着换色调，
   而不是一张与主题无关的白纸。 ⚠️ 别直接把底色写成主色：那条绿/橙上的正文会糊成一片。 */
.toast {
  pointer-events: auto;
  display: flex; align-items: flex-start; gap: 10px;
  width: 100%; padding: 13px 13px 14px 14px;
  border: 1px solid color-mix(in srgb, var(--accent) 26%, var(--border));
  border-radius: calc(var(--radius-lg) + 2px);
  background: color-mix(in srgb, var(--accent) 7%, var(--bg-elev));
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
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 650;
  /* 标题用主题色（正文仍走 --text-dim）：一行彩色标题就把这张卡"归到主题里" */
  color: var(--accent-text);
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
 *   ① 进：从右边滑进来 + 轻微放大（28px 位移，不大动干戈）；
 *   ② 出：**慢慢**往回滑 + 淡出（用户口径 2026-12-02：原来是"到点就没了"）——
 *      留 260ms，眼睛跟得上"它要走了"，不会觉得是被谁一下拔掉；
 *   ③ 中间那张被关掉后，剩下的**平滑上移**补位 —— 少了这条，列表会"跳"一下。
 * ⚠️ 离场**不要**改成 position:absolute（曾经这么写过）：一脱离布局，剩下的卡片瞬间补位，
 *    而它自己还停在半路 → 看起来就是"闪一下消失"。留在文档流里，靠 .toast-move 补位才顺。
 * ⚠️ 时长比全站 --dur（180ms）略长：提示条是"看一眼"的东西，退场太快反而像出错闪一下就没了。 */
.toast-enter-active { transition: opacity 220ms var(--ease), transform 260ms cubic-bezier(.2, .9, .3, 1.05); }
.toast-leave-active { transition: opacity 260ms var(--ease), transform 260ms var(--ease); }
.toast-enter-from { opacity: 0; transform: translateX(28px) scale(.96); }
.toast-leave-to { opacity: 0; transform: translateX(36px) scale(.97); }
.toast-move { transition: transform 260ms var(--ease); }
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
/* 键盘/读屏用户的减动效：内容照旧出现，只是不做位移 */

@media (max-width: 640px) {
  /* 窄屏：贴满两侧留 12px，别再让 max-width 把它裁成一条窄条 */
  .toast-host { left: 12px; right: 12px; width: auto; align-items: stretch; }
}
</style>
