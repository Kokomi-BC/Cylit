<script setup lang="ts">
/** 设置 · 外观：主题皮肤切换（移植自参照项目 retain-pdf 的 ThemeAppearancePanel）。
 *
 * 注册表驱动：面板只读 `listThemesBySeries()`，加皮肤不必改本文件。
 * 真值：`html[data-theme]` + localStorage（见 `@/theme/theme`）。
 *
 * 色块（swatch）用的颜色来自注册表的 `preview`，**只是缩略图**，
 * 与 CSS 里真正生效的令牌是两处值 —— 改皮肤配色时两边都要动（预览骗人比没有预览更糟）。
 */

import { onMounted, onUnmounted, ref } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { getTheme, listThemesBySeries, setTheme, THEME_CHANGE_EVENT } from '@/theme/theme';

const groups = listThemesBySeries();
const active = ref(getTheme());

function choose(id: string) {
  active.value = setTheme(id);
}

/** 另一个标签页改了皮肤时跟一下（storage 事件不保证有，自家事件总会有）。 */
function onThemeEvent(ev: Event) {
  const detail = (ev as CustomEvent<{ theme?: string }>).detail;
  active.value = detail?.theme || getTheme();
}

onMounted(() => window.addEventListener(THEME_CHANGE_EVENT, onThemeEvent));
onUnmounted(() => window.removeEventListener(THEME_CHANGE_EVENT, onThemeEvent));
</script>

<template>
  <div id="theme-appearance-panel" class="theme-appearance">
    <div
      v-for="g in groups"
      :key="g.series"
      class="theme-appearance-group"
      :data-theme-series="g.series"
    >
      <h3 class="theme-appearance-group-title">{{ g.label }}</h3>
      <div class="theme-appearance-grid" role="radiogroup" :aria-label="`${g.label}主题`">
        <button
          v-for="t in g.themes"
          :key="t.id"
          :id="`theme-option-${t.id}`"
          type="button"
          role="radio"
          :aria-checked="active === t.id"
          class="theme-option"
          :class="{ 'is-selected': active === t.id }"
          :data-theme-option="t.id"
          :data-theme-group="t.group"
          @click="choose(t.id)"
        >
          <span class="theme-option-swatch" :style="{ background: t.preview.bg }" aria-hidden="true">
            <span class="theme-option-swatch-paper" :style="{ background: t.preview.paper }">
              <span class="theme-option-swatch-bar" :style="{ background: t.preview.accent }" />
              <span class="theme-option-swatch-line" :style="{ background: t.preview.ink }" />
              <span class="theme-option-swatch-line short" :style="{ background: t.preview.ink }" />
            </span>
            <span class="theme-option-swatch-dot" :style="{ background: t.preview.danger }" />
          </span>

          <span class="theme-option-copy">
            <strong>{{ t.label }}</strong>
            <span>{{ t.description }}</span>
          </span>

          <span v-if="active === t.id" class="theme-option-check" aria-hidden="true">
            <AppIcon name="check" :size="13" />
          </span>
        </button>
      </div>
    </div>

    <p class="tiny faint theme-appearance-hint">
      皮肤只换配色：圆角、间距、动效与字体不随皮肤变；选择记在本机（localStorage），换机器不跟随。
    </p>
  </div>
</template>

<style scoped>
.theme-appearance { display: grid; gap: 18px; }
.theme-appearance-group { display: grid; gap: 10px; }
/* 分区标题：小一号的大写感抬头，不抢下面的卡片 */
.theme-appearance-group-title {
  margin: 0; font-size: 12px; font-weight: 600; letter-spacing: .02em;
  color: var(--text-dim); text-transform: none;
}
.theme-appearance-grid {
  display: grid; gap: 10px;
  grid-template-columns: repeat(auto-fill, minmax(216px, 1fr));
}

/* 一枚皮肤 = 色块 + 名称/说明 + 选中勾 */
.theme-option {
  position: relative; display: flex; align-items: center; gap: 12px;
  padding: 10px 12px; text-align: left; cursor: pointer;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text);
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease),
              box-shadow var(--dur) var(--ease), transform var(--dur-fast) var(--ease);
}
.theme-option:hover {
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
  box-shadow: var(--shadow-1);
}
.theme-option:active { transform: scale(.99); }
.theme-option.is-selected {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 5%, var(--bg-elev));
  box-shadow: var(--ring);
}

/* 色块：外框是 bg，内页是 paper，上面一根强调色条 + 两条墨线 + 一颗语义色点 */
.theme-option-swatch {
  position: relative; flex: none;
  width: 46px; height: 56px; padding: 6px 7px;
  border-radius: 10px; border: 1px solid var(--border);
  display: flex; align-items: flex-start;
}
.theme-option-swatch-paper {
  position: relative; width: 100%; height: 34px;
  border-radius: 6px; padding: 5px 5px 0;
  display: flex; flex-direction: column; gap: 3px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, .10);
}
.theme-option-swatch-bar { display: block; height: 6px; width: 78%; border-radius: 3px; }
.theme-option-swatch-line { display: block; height: 3px; width: 100%; border-radius: 2px; opacity: .55; }
.theme-option-swatch-line.short { width: 62%; opacity: .35; }
.theme-option-swatch-dot {
  position: absolute; right: 6px; bottom: 6px;
  width: 7px; height: 7px; border-radius: 999px;
}

.theme-option-copy { display: grid; gap: 2px; min-width: 0; padding-right: 18px; }
.theme-option-copy strong { font-size: 13px; font-weight: 650; letter-spacing: -.01em; }
.theme-option-copy span {
  font-size: 11.5px; line-height: 1.45; color: var(--text-dim);
}

.theme-option-check {
  position: absolute; right: 9px; top: 9px;
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 999px;
  background: var(--accent); color: var(--text-on-accent);
}

.theme-appearance-hint { margin: 0; max-width: 60ch; }
</style>
