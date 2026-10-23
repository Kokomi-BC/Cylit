<script setup lang="ts">
/**
 * 设置 · 显示主题 · **分类字号**（裸块，放在「预览效果」卡下面）：
 *  - 每一类一行：名字 + **该类字号的真实样例** + 一个下拉（小 / 中 / 大）；
 *  - 块底两行：「一键替换」把五类一起设成同一档；「字体」一个下拉（三套预设 + 本机字体）。
 *
 * 样例不是装饰：字号取自该类的 CSS 变量（`--ts-<类>` = 整体 × 该类），点一下当场变 ——
 * 与阅读器里那类文字用的是同一个令牌，看到多少就是多少。
 *
 * 三档而不是 ±5% 步进（用户口径 2026-12）：调字号是「选一个大概」，不是微调刻度；
 * 小 90% / 中 100% / 大 115% 已经覆盖常用区间。存量里的 0.85 / 1.25 仍生效，
 * 下拉取**最近**的那一档，不会一个都不选中。
 *
 * 本机字体：Chromium / Edge 的 `queryLocalFonts()` 要一次用户手势 + 浏览器授权，
 * 返回本机安装的字体族名（https 页面才有）；Firefox / Safari 没有这个 API，
 * 那里下拉里就只留三套预设，不假装能读。
 */
import { ref } from 'vue';
import {
  DISPLAY_CATEGORIES,
  displayScale,
  setCategory,
  type DisplayCategory,
} from '@/stores/display';
import {
  TYPOGRAPHY_OPTIONS,
  appearance,
  setFontFamily,
  setTypography,
} from '@/stores/appearance';

/** 样例文字：每类一句自己的话，读起来就知道调的是哪一块 */
const CATEGORY_META: Record<DisplayCategory, { label: string; sample: string }> = {
  body: { label: '正文', sample: '论文正文与笔记的阅读字' },
  chat: { label: '对话', sample: '问答气泡里的这句话' },
  note: { label: '笔记', sample: '批注与高亮上的说明' },
  side: { label: '侧栏', sample: '目录、插图与面板' },
  ui: { label: '界面', sample: '按钮、表格与表单' },
};

const categories = DISPLAY_CATEGORIES.map((key) => ({ key, ...CATEGORY_META[key] }));

/** 三档 */
const STEPS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 0.9, label: '小' },
  { value: 1, label: '中' },
  { value: 1.15, label: '大' },
];

/** 当前值落在哪一档：取最近的（存量里的 0.85 / 1.25 也有选中项，不会空着） */
function nearest(scale: number): number {
  return STEPS.reduce(
    (best, s) => (Math.abs(s.value - scale) < Math.abs(best - scale) ? s.value : best),
    STEPS[1].value,
  );
}

/** 样例的实时字号：读该类的令牌（与阅读器同一份），换个档当场就变 */
function sampleSize(key: DisplayCategory): string {
  return `calc(13px * var(--ts-${key}))`;
}

function onSizeChange(key: DisplayCategory, ev: Event): void {
  const next = Number((ev.target as HTMLSelectElement).value);
  if (Number.isFinite(next)) setCategory(key, next);
}

/* ------------------------- 一键替换 ------------------------- */
const bulk = ref(1);

/** 五类一起设成同一档（用户口径 2026-12：底部一键替换） */
function applyBulk(): void {
  for (const key of DISPLAY_CATEGORIES) setCategory(key, bulk.value);
}

/* ------------------------- 本机字体 ------------------------- */
type LocalFontData = { family: string };
type QueryLocalFonts = () => Promise<LocalFontData[]>;

const localFonts = ref<string[]>([]);
const fontsBusy = ref(false);
const fontsError = ref('');

/** 浏览器有没有那个 API（没有就把「读取本机字体」那项藏起来） */
const fontsApiAvailable = typeof (window as unknown as { queryLocalFonts?: unknown }).queryLocalFonts === 'function';

async function loadLocalFonts(): Promise<void> {
  const fn = (window as unknown as { queryLocalFonts?: QueryLocalFonts }).queryLocalFonts;
  if (typeof fn !== 'function') {
    fontsError.value = '这个浏览器不允许网页读取本机字体（Chrome / Edge 可以）';
    return;
  }
  fontsBusy.value = true;
  fontsError.value = '';
  try {
    const list = await fn.call(window);
    const names = Array.from(new Set((list ?? []).map((f) => String(f?.family ?? '').trim()).filter(Boolean)))
      .sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'));
    localFonts.value = names;
    if (!names.length) fontsError.value = '这台机器上没有读到可用字体';
  } catch {
    /* 用户点了「拒绝」或浏览器策略不允许：给一句能看懂的话 */
    fontsError.value = '没有拿到读取本机字体的权限';
  } finally {
    fontsBusy.value = false;
  }
}

/** 下拉里「读取本机字体…」这一项（点了先读，读完再选具体字体） */
const LOAD_ACTION = '__load__';

/** 字体下拉的当前值：本机字体优先，否则是预设 */
const fontValue = ref('');
function syncFontValue(): void {
  fontValue.value = appearance.fontFamily ? `local:${appearance.fontFamily}` : `preset:${appearance.typography}`;
}
syncFontValue();

async function onFontChange(ev: Event): Promise<void> {
  const value = (ev.target as HTMLSelectElement).value;
  if (value === LOAD_ACTION) {
    syncFontValue();            // 把下拉拨回当前值：它是个动作，不是一个选项
    await loadLocalFonts();
    return;
  }
  if (value.startsWith('local:')) setFontFamily(value.slice(6));
  else if (value.startsWith('preset:')) setTypography(value.slice(7) as typeof appearance.typography);
  syncFontValue();
}
</script>

<template>
  <section class="cats-block" data-category-sizes>
    <div class="block-head">
      <h3>分类字号</h3>
      <p>每类各挑一档，右边就是它真实的样子</p>
    </div>

    <div class="cats">
      <div v-for="c in categories" :key="c.key" class="cat">
        <span class="cat-name">{{ c.label }}</span>
        <span class="cat-sample" :style="{ fontSize: sampleSize(c.key) }">{{ c.sample }}</span>
        <select
          class="size-select"
          :value="String(nearest(displayScale[c.key]))"
          :aria-label="c.label + '字号'"
          :data-size-select="c.key"
          @change="onSizeChange(c.key, $event)"
        >
          <option v-for="s in STEPS" :key="s.value" :value="String(s.value)">{{ s.label }}</option>
        </select>
      </div>
    </div>

    <div class="foot-row">
      <span class="cat-name">批量</span>
      <div class="foot-ctl">
        <span class="foot-label">全部设为</span>
        <select class="size-select" v-model.number="bulk" aria-label="全部字号" data-bulk-select>
          <option v-for="s in STEPS" :key="s.value" :value="s.value">{{ s.label }}</option>
        </select>
        <button type="button" class="apply-btn" data-bulk-apply @click="applyBulk">应用到全部</button>
      </div>
    </div>

    <div class="foot-row">
      <span class="cat-name">字体</span>
      <div class="foot-ctl">
        <select
          class="font-select"
          :value="fontValue"
          aria-label="字体"
          data-font-select
          @change="onFontChange"
        >
          <option v-for="o in TYPOGRAPHY_OPTIONS" :key="o.value" :value="`preset:${o.value}`">{{ o.label }}</option>
          <option v-if="fontsApiAvailable" :value="LOAD_ACTION">{{ fontsBusy ? '读取中…' : '读取本机字体…' }}</option>
          <option v-for="name in localFonts" :key="name" :value="`local:${name}`">{{ name }}</option>
        </select>
        <span v-if="appearance.fontFamily" class="font-hint">本机字体</span>
        <span v-if="fontsBusy" class="font-hint">正在读取…</span>
      </div>
    </div>
    <p v-if="fontsError" class="font-msg">{{ fontsError }}</p>
  <p v-else-if="appearance.fontFamily" class="font-msg">正在使用本机字体：{{ appearance.fontFamily }}</p>
  </section>
</template>

<style scoped>
.block-head { display: grid; gap: 1px; margin-bottom: 8px; min-width: 0; }
.block-head h3 {
  margin: 0; font-size: calc(14px * var(--ts, 1)); font-weight: 650;
  letter-spacing: -.01em; line-height: 1.3;
}
.block-head p { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.4; color: var(--text-faint); }

.cats-block { min-width: 0; }
.cats { display: flex; flex-direction: column; gap: 2px; }
/* 一行：名字（定宽）+ 样例（吃满）+ 下拉（定宽） */
.cat {
  display: grid; grid-template-columns: 40px minmax(0, 1fr) 68px;
  align-items: center; gap: 8px; min-height: 30px;
}
.cat-name { font-size: calc(12px * var(--ts, 1)); font-weight: 600; }
/* 样例：**吃该类的字号令牌**，行高按字号走，不会被裁 */
.cat-sample {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  line-height: 1.35; color: var(--text-dim);
}

/* 下拉：一行一个，宽度固定 —— 五行的下拉框在同一条竖线上 */
.size-select, .font-select {
  height: 26px; padding: 0 6px; min-width: 0; width: 100%;
  border: 1px solid var(--border); border-radius: var(--radius-ctl);
  background: var(--bg-elev); color: var(--text);
  font-family: inherit; font-size: calc(11.5px * var(--ts, 1));
}
.size-select:focus, .font-select:focus { outline: none; border-color: var(--accent); box-shadow: var(--ring); }

/* 底部两行（一键替换 / 字体）：左边名字与上面五行对齐，右边控件靠左排 */
.foot-row {
  display: grid; grid-template-columns: 40px minmax(0, 1fr); align-items: center; gap: 8px;
  margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--divider);
}
.foot-ctl { display: flex; align-items: center; gap: 8px; min-width: 0; }
.foot-label { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
.foot-ctl .size-select { width: 64px; flex: none; }
.foot-ctl .font-select { width: auto; max-width: 220px; flex: 0 1 auto; }
.apply-btn {
  flex: none; height: 26px; padding: 0 10px;
  border: 1px solid var(--border); border-radius: var(--radius-ctl);
  background: var(--bg-elev); color: var(--text-dim);
  font-family: inherit; font-size: calc(11.5px * var(--ts, 1)); cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.apply-btn:hover { background: var(--bg-hover); color: var(--text); border-color: var(--border-strong); }
.apply-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.font-hint { font-size: calc(11px * var(--ts, 1)); color: var(--accent-text); }
.font-msg { margin: 6px 0 0 48px; font-size: calc(11px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint); }
</style>
