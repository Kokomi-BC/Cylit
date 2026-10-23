<script setup lang="ts">
/**
 * 设置 · 显示主题 · **当前配色**：四枚语义色令牌 + 一张迷你界面缩略图。
 *
 * 色值不是写死的：挂载后读 getComputedStyle(document.documentElement) 上的令牌，
 * 换主题时再读一遍（自定义主题编辑过程中也会跟着变）。所以小字里的十六进制**就是**
 * 现在的值，不是示意。令牌被写成 color-mix() 一类表达式时读不到十六进制，那一行就
 * 只留色块，不编一个数字出来。
 *
 * 2026-12 用户口径：这块与「主题预设」**并成一张卡**（预设竖向列表在左、配色在右），
 * 所以给一个 `bare` 开关 —— 开着只出内容，卡片外壳与卡头由 ThemePresetCard 统一提供。
 */
import { onMounted, onUnmounted, ref } from 'vue';
import { THEME_CHANGE_EVENT } from '@/theme/theme';

const props = defineProps<{ bare?: boolean }>();

const TOKENS = [
  { key: 'accent', label: '主色', cls: 'is-accent' },
  { key: 'ok', label: '成功', cls: 'is-ok' },
  { key: 'warn', label: '警告', cls: 'is-warn' },
  { key: 'danger', label: '危险', cls: 'is-danger' },
] as const;

const hexes = ref<Record<string, string>>({});

function readHex(name: string): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return /^#[0-9a-f]{6}$/i.test(raw) ? raw.toUpperCase() : '';
}

function refresh() {
  hexes.value = {
    accent: readHex('--accent'),
    ok: readHex('--ok'),
    warn: readHex('--warn'),
    danger: readHex('--danger'),
  };
}

onMounted(() => {
  refresh();
  window.addEventListener(THEME_CHANGE_EVENT, refresh);
});
onUnmounted(() => window.removeEventListener(THEME_CHANGE_EVENT, refresh));
</script>

<template>
  <component :is="props.bare ? 'div' : 'section'" class="palette" :class="props.bare ? 'palette-bare' : 'card'">
    <!-- 不带外壳时只留一行小字当列名：并进「主题预设」那张卡之后，右栏没有名字会读不出是什么 -->
    <div v-if="props.bare" class="bare-label">当前配色</div>
    <div v-else class="card-head">
      <div class="hd">
        <h3>当前配色</h3>
        <p>四枚语义色令牌的实时取值</p>
      </div>
    </div>

    <div class="palette-body">
      <div class="chips">
        <span v-for="t in TOKENS" :key="t.key" class="chip" :data-token="t.key">
          <i class="dot" :class="t.cls" aria-hidden="true" />
          <span class="chip-label">{{ t.label }}</span>
          <span class="chip-hex mono">{{ hexes[t.key] || '—' }}</span>
        </span>
      </div>

      <!-- 迷你界面：侧栏 + 内容页，元素全部吃当前令牌，换肤时整张图跟着变 -->
      <div class="mini" aria-hidden="true">
        <div class="mini-side">
          <span class="mini-avatar" />
          <span class="mini-line" />
          <span class="mini-line" />
          <span class="mini-line short" />
        </div>
        <div class="mini-main">
          <span class="mini-pill" />
          <span class="mini-line wide" />
          <span class="mini-line mid" />
          <div class="mini-tags">
            <span class="mini-tag is-accent" />
            <span class="mini-tag is-ok" />
            <span class="mini-tag is-warn" />
            <span class="mini-tag is-danger" />
          </div>
        </div>
      </div>
    </div>
  </component>
</template>

<style scoped>
.card-head { padding: 11px 14px 9px; }
.hd { display: grid; gap: 2px; min-width: 0; }
.hd h3 {
  margin: 0; font-size: calc(14px * var(--ts, 1)); font-weight: 650;
  letter-spacing: -.01em; line-height: 1.3;
}
.hd p { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.4; color: var(--text-faint); }

/* 卡片拉满整行高度（与左侧预设列表等高），缩略图吃掉剩下的空间 */
.palette { display: flex; flex-direction: column; min-width: 0; }
.palette-body { flex: 1; display: flex; flex-direction: column; gap: 10px; padding: 10px 14px 12px; }
/* bare：并进「预览效果」卡之后，上面一条细线与样例分开（不画第二个卡壳） */
.palette-bare { padding: 0; }
.palette-bare .palette-body { padding: 12px 0 0; border-top: 1px solid var(--divider); }
/* 卡宽 660 左右：四枚色块铺一行；窄了自动落回两列 */
.palette-bare .chips { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
/* 缩略图跟着长满剩余高度，别在下面留一片白 */
.palette-bare .mini { max-height: none; min-height: 104px; }
.bare-label {
  margin: 0 0 8px; font-size: calc(12.5px * var(--ts, 1)); font-weight: 650;
}

/* 令牌：2×2 —— 一行四个的话"主色 + 十六进制"会挤到必须省略 */
.chips { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.chip {
  display: flex; align-items: center; gap: 6px; min-width: 0;
  height: 26px; padding: 0 8px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev);
}
.dot { flex: none; width: 12px; height: 12px; border-radius: 4px; border: 1px solid var(--border-strong); }
.dot.is-accent { background: var(--accent); }
.dot.is-ok { background: var(--ok); }
.dot.is-warn { background: var(--warn); }
.dot.is-danger { background: var(--danger); }
.chip-label { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim); white-space: nowrap; }
.chip-hex {
  margin-left: auto; font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint);
  font-variant-numeric: tabular-nums; letter-spacing: .01em;
}

/* 缩略图：一块"界面"，不是装饰画 —— 侧栏、主区、强调条、四颗语义点各就各位 */
.mini {
  flex: 1; min-height: 88px; max-height: 100px; display: grid;
  grid-template-columns: 46px minmax(0, 1fr); gap: 7px;
  padding: 8px; border-radius: var(--r-sm);
  border: 1px solid var(--border);
  background: var(--bg-sunken);
}
.mini-side {
  display: grid; align-content: start; gap: 5px;
  padding: 7px 6px; border-radius: 7px; background: var(--bg-elev);
  border: 1px solid var(--border);
}
.mini-avatar { width: 12px; height: 12px; border-radius: 4px; background: var(--accent); opacity: .85; }
.mini-main {
  display: grid; align-content: start; gap: 5px;
  padding: 8px; border-radius: 7px; background: var(--bg-elev);
  border: 1px solid var(--border);
}
.mini-pill { height: 9px; width: 54%; border-radius: 5px; background: var(--accent); }
.mini-line { height: 3px; border-radius: 2px; background: var(--text); opacity: .16; }
.mini-line.wide { width: 94%; }
.mini-line.mid { width: 68%; }
.mini-line.short { width: 52%; }
.mini-tags { display: flex; gap: 4px; margin-top: 1px; }
.mini-tag { height: 9px; width: 22px; border-radius: 4px; }
.mini-tag.is-accent { background: var(--accent-soft); }
.mini-tag.is-ok { background: var(--ok-soft); }
.mini-tag.is-warn { background: var(--warn-soft); }
.mini-tag.is-danger { background: var(--danger-soft); }
</style>
