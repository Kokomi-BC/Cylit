<script setup lang="ts">
/** Toast 宿主：统一承载成功/警告/错误提示（点击可关闭）。
 *
 * 位置与观感（用户口径 2026-10-03）：**页面顶部居中**（原来在右下角，视线扫不到），
 * 走全站那套"三层胶囊 + 毛玻璃"语言；**左侧那条颜色竖线删掉**，级别只由图标颜色表达。
 */
import { dismissToast, state } from '@/stores/app';
import AppIcon from '@/components/AppIcon.vue';

/** 提示级别 → 图标（全部走 AppIcon，不使用 emoji）。 */
const ICON: Record<string, string> = {
  ok: 'check_circle', success: 'check_circle', warn: 'warning', warning: 'warning',
  error: 'error', danger: 'error',
};

function iconOf(kind?: string | null): string {
  return ICON[String(kind ?? '')] || 'info';
}
</script>

<template>
  <div class="toast-host">
    <TransitionGroup name="toast">
      <div v-for="t in state.toasts" :key="t.id" :class="['toast', t.kind]" @click="dismissToast(t.id)">
        <div class="toast-pill">
          <AppIcon :name="iconOf(t.kind)" :size="15" class="toast-ico" />
          <span class="toast-text">{{ t.message }}</span>
        </div>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
/* 顶部居中：落在**标签条下方**（`--header-h`），不压住应用的标签页。
 * `pointer-events:none` 挂在宿主机上 —— 它横跨整个页面顶部，只有胶囊本体该收点击。 */
.toast-host {
  --toast-glass-top: 86%;
  --toast-glass-bottom: 74%;
  position: fixed; top: calc(var(--header-h) + 12px); left: 50%; transform: translateX(-50%);
  z-index: 200; display: flex; flex-direction: column; align-items: center; gap: 8px;
  max-width: min(560px, calc(100vw - 32px));
  pointer-events: none;
}
/* 三层结构（与 `.seg` / 工具条胶囊同一门语言）：
 *   ① 毛玻璃托盘 —— 渐变本体 + `backdrop-filter` + 柔光投影，**不画描边**（描边在深色下会变成发光环）；
 *   ② 抬起的白色 pill（`--bg-elev` + `--shadow-1`，就是 `.seg` 选中项那一层）；
 *   ③ 图标 / 文字。
 * 托盘用玻璃而不是 `--bg-sunken`：toast 悬浮在正文之上，浅底会把底下的字透出一条灰带。 */
.toast {
  pointer-events: auto; cursor: pointer;
  padding: 3px;                                   /* = .seg 托盘的 3px 内边距 */
  border: 0; border-radius: var(--radius-lg);
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) var(--toast-glass-top), transparent),
    color-mix(in srgb, var(--bg-elev) var(--toast-glass-bottom), transparent));
  backdrop-filter: blur(44px) saturate(2.4);
  -webkit-backdrop-filter: blur(44px) saturate(2.4);
  box-shadow: var(--shadow-flyout);
}
.toast-pill {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 7px 13px;
  border-radius: calc(var(--radius-lg) - 3px);      /* 套在托盘里，半径按内边距递减 */
  background: var(--bg-elev); box-shadow: var(--shadow-1);
  font-size: 13px; line-height: 1.5;
}
.toast-ico { flex: none; margin-top: 1px; color: var(--accent-text); }
.toast-text { min-width: 0; word-break: break-word; }
.toast.ok .toast-ico { color: var(--ok); }
.toast.warn .toast-ico { color: var(--warn); }
.toast.error .toast-ico { color: var(--danger); }
.toast-enter-active, .toast-leave-active { transition: all var(--dur) var(--ease); }
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translateY(-8px); }
</style>
