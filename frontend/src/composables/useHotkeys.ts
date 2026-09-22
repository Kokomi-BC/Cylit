/** 键盘快捷键（输入框内不拦截）。`map` 的键用 `KeyboardEvent.key`，可带 `ctrl+` 前缀。 */
import { onMounted, onUnmounted } from 'vue';

export type HotkeyHandler = (ev: KeyboardEvent) => void;

export function useHotkeys(map: Record<string, HotkeyHandler>, enabled: () => boolean = () => true) {
  function onKey(ev: KeyboardEvent) {
    if (!enabled()) return;
    const target = ev.target as HTMLElement | null;
    if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return;
    const combo = `${ev.ctrlKey ? 'ctrl+' : ''}${ev.key}`;
    const fn = map[combo] ?? map[ev.key];
    if (!fn) return;
    ev.preventDefault();
    fn(ev);
  }
  onMounted(() => window.addEventListener('keydown', onKey));
  onUnmounted(() => window.removeEventListener('keydown', onKey));
}
