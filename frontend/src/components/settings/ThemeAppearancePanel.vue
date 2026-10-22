<script setup lang="ts">
/** 设置 · 显示主题：**一个主题库** + 轻量筛选 + 自定义主题编辑器。
 *
 * 信息架构只有两层：筛选栏（单选，默认「全部」）+ 统一网格。不再按开发来源 / 命名系列分区
 * —— 主题之间没有层级差别，用户只需要浏览、筛选、挑一个、或者自己做一个。
 *
 * 数据源全部来自注册表（`@/theme/theme` → `registry.ts`）：预设、用户自定义、老皮肤
 * 走**同一套 CSS 令牌**，面板只负责"用户点了哪个 id"。
 * 颜色编辑用的是原生 `<input type="color">`：不需要用户理解任何 CSS。
 */

import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
import { askConfirm } from '@/stores/confirm';
import { toast } from '@/stores/app';
import {
  DEFAULT_THEME_ID,
  PREVIEW_THEME_ID,
  THEME_CHANGE_EVENT,
  THEME_PALETTES,
  deleteCustomTheme,
  endPreview,
  getTheme,
  listThemes,
  mix,
  previewTheme,
  refreshCustomThemes,
  saveCustomTheme,
  setTheme,
  themeFacets,
  type ThemeDefinition,
  type ThemeFacet,
  type ThemeSpec,
} from '@/theme/theme';

/* ------------------------------ 列表与筛选 ------------------------------ */

/** 筛选栏：「全部」在最前，其余按"看得见的外观"分：浅色 / 深色 / 彩色 / 自定义。 */
const FILTERS: ReadonlyArray<{ key: 'all' | ThemeFacet; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'light', label: '浅色' },
  { key: 'dark', label: '深色' },
  { key: 'color', label: '彩色' },
  { key: 'custom', label: '自定义' },
];

const filter = ref<'all' | ThemeFacet>('all');
const themes = ref<ThemeDefinition[]>(listThemes());
const active = ref(getTheme());

/** 当前筛选下要显示的主题（自定义主题也在同一个网格里，只是多两个按钮）。 */
const shown = computed(() => (filter.value === 'all'
  ? themes.value
  : themes.value.filter((t) => themeFacets(t).includes(filter.value as ThemeFacet))));

/** 当前这套皮肤的色板：只读，回答"现在用的是什么配色"。 */
const activeSwatches = computed(() => {
  const preview = themes.value.find((t) => t.id === active.value)?.preview;
  if (!preview) return [];
  return [
    { label: '页面背景', color: preview.bg },
    { label: '卡片', color: preview.paper },
    { label: '主色', color: preview.accent },
    { label: '文字', color: preview.ink },
    { label: '提示', color: preview.danger },
  ];
});

/** 每个筛选下有几套（筛选栏自己把话说清楚，不用另写说明）。 */
function countOf(key: 'all' | ThemeFacet): number {
  return key === 'all' ? themes.value.length : themes.value.filter((t) => themeFacets(t).includes(key)).length;
}

function refresh() { themes.value = listThemes(); }

function choose(id: string) {
  active.value = setTheme(id);
}

/** 另一个标签页改了皮肤时跟一下（storage 事件不保证有，自家事件总会有）。
 *  预览中的临时主题不参与"当前选中"，否则正在编辑的那套会从列表里"消失"。 */
function onThemeEvent(ev: Event) {
  const detail = (ev as CustomEvent<{ theme?: string }>).detail;
  const id = detail?.theme || getTheme();
  if (id && id !== PREVIEW_THEME_ID) active.value = id;
}

/* ------------------------------ 编辑器 ------------------------------ */

/** 7 个用户可调颜色（顺序 = 编辑器里的行序）。 */
const COLOR_FIELDS = [
  { key: 'accent', label: '主色' },
  { key: 'accentSoft', label: '强调色' },
  { key: 'bg', label: '页面背景' },
  { key: 'surface', label: '卡片背景' },
  { key: 'border', label: '边框' },
  { key: 'ink', label: '主要文字' },
  { key: 'dim', label: '次要文字' },
] as const;

type ColorKey = (typeof COLOR_FIELDS)[number]['key'];

const editorOpen = ref(false);
/** 正在编辑的自定义主题 id；空 = 新建。 */
const editingId = ref('');
/** 进编辑器之前的主题：取消 / 关弹窗时还原。 */
const restoreId = ref(DEFAULT_THEME_ID);

const draft = reactive({
  label: '',
  dark: false,
  accent: '#0f7b8a',
  accentSoft: '',
  bg: '#f2f4f5',
  surface: '#ffffff',
  border: '#0d202614',
  ink: '#171b1d',
  dim: '#5b6469',
});

const palettes = computed(() => ({
  academic: THEME_PALETTES.filter((p) => p.group === 'academic'),
  dopamine: THEME_PALETTES.filter((p) => p.group === 'dopamine'),
}));

function fillDraft(spec: ThemeSpec, label: string, dark: boolean) {
  draft.label = label;
  draft.dark = dark;
  for (const f of COLOR_FIELDS) draft[f.key] = spec[f.key] || '';
}

function specFromDraft(): ThemeSpec {
  return {
    bg: draft.bg, surface: draft.surface, ink: draft.ink, dim: draft.dim,
    accent: draft.accent, accentSoft: draft.accentSoft || undefined,
    border: draft.border, dark: draft.dark,
  };
}

function applyPalette(p: (typeof THEME_PALETTES)[number]) {
  const keep = draft.label;
  fillDraft(p.spec, keep, !!p.spec.dark);
}

function openEditor(def?: ThemeDefinition) {
  const current = getTheme();
  restoreId.value = current === PREVIEW_THEME_ID ? DEFAULT_THEME_ID : current;
  editingId.value = def?.id || '';
  fillDraft(def?.spec ?? THEME_PALETTES[0].spec, def?.label || '', !!def?.spec?.dark);
  editorOpen.value = true;
  previewTheme(draft.label, specFromDraft());
}

/** 关编辑器：默认还原到进来之前那套（保存时传 `false`）。 */
function closeEditor(revert = true) {
  if (revert) active.value = endPreview(restoreId.value);
  editorOpen.value = false;
  editingId.value = '';
}

/** 边调边看：草稿一变就重挂预览皮肤。 */
watch(draft, () => {
  if (editorOpen.value) previewTheme(draft.label, specFromDraft());
}, { deep: true });

function save() {
  if (!draft.label.trim()) {
    toast('给主题起个名字', 'warn', 2600);
    return;
  }
  const wasEdit = !!editingId.value;
  const id = saveCustomTheme({ id: editingId.value || undefined, label: draft.label, spec: specFromDraft() });
  endPreview();                 // 丢掉临时皮肤；新主题已在注册表里，CSS 会一起重建
  refresh();
  active.value = setTheme(id);
  closeEditor(false);
  toast(wasEdit ? '已更新主题' : '已保存自定义主题', 'ok', 2600);
}

/** 删除一个自定义主题（只认自定义：内置的删不掉，注册表里根本没有它）。 */
async function removeCustom(id: string, label: string) {
  if (!id.startsWith('custom-')) return;
  const ok = await askConfirm({
    title: `删除主题「${label}」？`,
    body: '删除后不能恢复。',
    danger: true,
    confirmLabel: '删除',
  });
  if (!ok) return;
  if (editingId.value === id) closeEditor(true);
  deleteCustomTheme(id);
  refreshCustomThemes();        // 重读 localStorage + 重建生成的 CSS
  refresh();
  active.value = getTheme();
  toast('已删除自定义主题', 'ok', 2400);
}

function onColor(key: ColorKey, value: string) { draft[key] = value; }
function onHex(key: ColorKey, ev: Event) {
  const v = (ev.target as HTMLInputElement).value.trim();
  if (/^#[0-9a-f]{6}$/i.test(v)) draft[key] = v.toLowerCase();
  else (ev.target as HTMLInputElement).value = draft[key];
}

/** 色块显示的颜色：留空的「强调色」显示**它实际会用的那一档**（由主色推），
 *  否则那一行会是一块空白，看起来像坏了。与 `specToVars()` 里同一条规则。 */
function swatchOf(key: ColorKey): string {
  if (draft[key]) return draft[key];
  if (key === 'accentSoft') {
    return draft.dark ? mix(draft.bg, draft.accent, 0.22) : mix(draft.surface, draft.accent, 0.11);
  }
  return 'transparent';
}

/** 颜色行里"留空"的说明（只有强调色可留空）。 */
function isAuto(key: ColorKey): boolean { return key === 'accentSoft' && !draft.accentSoft; }

onMounted(() => window.addEventListener(THEME_CHANGE_EVENT, onThemeEvent));
onUnmounted(() => window.removeEventListener(THEME_CHANGE_EVENT, onThemeEvent));
</script>

<template>
  <div id="theme-appearance-panel" class="theme-appearance">
    <p class="panel-note">点一套皮肤就能换，也能自己做一套。</p>

    <div class="grid ta-grid">
      <!-- 皮肤：筛选栏 + 图块墙，内容长，整宽 -->
      <div class="card ta-card-wide">
        <div class="card-head">皮肤</div>
        <div class="card-body ta-wall">
          <!-- 筛选栏：单选，只负责"快速缩小范围"，不制造新的分组层级 -->
          <div class="seg theme-filter" role="group" aria-label="主题筛选">
            <button
              v-for="f in FILTERS"
              :key="f.key"
              type="button"
              :class="{ active: filter === f.key }"
              :aria-pressed="filter === f.key"
              :data-theme-filter="f.key"
              @click="filter = f.key"
            >{{ f.label }}<span class="theme-filter-n">{{ countOf(f.key) }}</span></button>
          </div>

          <div class="theme-appearance-grid" role="radiogroup" aria-label="主题">
            <div
              v-for="t in shown"
              :key="t.id"
              :id="`theme-option-${t.id}`"
              role="radio"
              :aria-checked="active === t.id"
              tabindex="0"
              class="theme-option"
              :class="{ 'is-selected': active === t.id }"
              :data-theme-option="t.id"
              :data-theme-group="t.group"
              :data-theme-custom="t.custom ? '1' : '0'"
              @click="choose(t.id)"
              @keydown.enter.prevent="choose(t.id)"
              @keydown.space.prevent="choose(t.id)"
            >
              <span class="theme-option-swatch" :style="{ background: t.preview.bg }" aria-hidden="true">
                <span class="theme-option-swatch-paper" :style="{ background: t.preview.paper }">
                  <span class="theme-option-swatch-bar" :style="{ background: t.preview.accent }" />
                  <span class="theme-option-swatch-line" :style="{ background: t.preview.ink }" />
                  <span class="theme-option-swatch-line short" :style="{ background: t.preview.ink }" />
                </span>
                <span class="theme-option-swatch-dot" :style="{ background: t.preview.danger }" />
              </span>
      
              <span class="theme-option-copy"><strong>{{ t.label }}</strong></span>
      
              <span v-if="t.custom" class="theme-option-tools">
                <button type="button" class="theme-option-tool" title="编辑"
                        @click.stop="openEditor(t)">
                  <AppIcon name="edit" :size="14" />
                </button>
                <button type="button" class="theme-option-tool danger" title="删除"
                        @click.stop="removeCustom(t.id, t.label)">
                  <AppIcon name="trash" :size="14" />
                </button>
              </span>
              <span v-if="active === t.id" class="theme-option-check" aria-hidden="true">
                <AppIcon name="check" :size="13" />
              </span>
            </div>

            <!-- 创建入口：永远排在网格最后（不是"另一个区域"，就是列表的下一格） -->
            <button
              type="button"
              class="theme-option is-new"
              data-theme-new
              @click="openEditor()"
            >
              <span class="theme-option-new-icon"><AppIcon name="add" :size="20" /></span>
              <span class="theme-option-copy"><strong>创建自定义主题</strong></span>
            </button>
          </div>
        </div>
      </div>

      <!-- 当前配色：只读物色板，短卡并排 -->
      <div class="card">
        <div class="card-head">当前配色</div>
        <div class="card-body">
          <div class="ta-swatches" v-if="activeSwatches.length">
            <span v-for="s in activeSwatches" :key="s.label" class="ta-swatch">
              <i class="ta-swatch-dot" :style="{ background: s.color }" aria-hidden="true" />
              <span class="ta-swatch-label">{{ s.label }}</span>
            </span>
          </div>
          <p class="tiny faint" v-else>这套皮肤没留下色板。</p>
        </div>
      </div>

      <!-- 自定义：入口放进一张短卡，和上面的色板并排 -->
      <div class="card">
        <div class="card-head">自定义</div>
        <div class="card-body ta-custom">
          <p class="ta-hint">自己做的皮肤也排在墙里，随时能改。</p>
          <button type="button" class="btn sm primary" @click="openEditor()">
            <AppIcon name="add" :size="15" />新建一套
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- ---------------------------- 主题编辑器 ---------------------------- -->
  <Modal
    :open="editorOpen"
    :title="editingId ? '编辑主题' : '自定义主题'"
    @close="closeEditor(true)"
  >
    <div class="ce">
      <div class="ce-row">
        <label class="ce-name">
          <span>名称</span>
          <input type="text" maxlength="24" v-model="draft.label" placeholder="我的主题" />
        </label>
        <div class="ce-mode" role="group" aria-label="底色明暗">
          <button type="button" :class="{ active: !draft.dark }" @click="draft.dark = false">浅色</button>
          <button type="button" :class="{ active: draft.dark }" @click="draft.dark = true">深色</button>
        </div>
      </div>

      <!-- 调色板：一排色块，点一下铺满整套颜色（之后仍可单独改） -->
      <div class="ce-block" v-for="(list, kind) in palettes" :key="kind">
        <h4>{{ kind === 'academic' ? '调色板' : '多巴胺色' }}</h4>
        <div class="ce-palettes">
          <button
            v-for="p in list"
            :key="p.id"
            type="button"
            class="ce-palette"
            :title="p.label"
            :data-palette="p.id"
            @click="applyPalette(p)"
          >
            <span class="ce-palette-chip" :style="{ background: p.spec.bg }">
              <span class="ce-palette-chip-in" :style="{ background: p.spec.surface }">
                <span class="ce-palette-bar" :style="{ background: p.spec.accent }" />
              </span>
            </span>
            <span>{{ p.label }}</span>
          </button>
        </div>
      </div>

      <!-- 颜色：点色块开系统取色器，右边显示 HEX（也可以直接改） -->
      <div class="ce-block">
        <h4>颜色</h4>
        <div class="ce-fields">
          <label class="ce-field" v-for="f in COLOR_FIELDS" :key="f.key">
            <span class="ce-field-label">{{ f.label }}</span>
            <span class="ce-color" :class="{ 'is-auto': isAuto(f.key) }">
              <input
                type="color"
                :value="swatchOf(f.key)"
                :data-color="f.key"
                @input="onColor(f.key, ($event.target as HTMLInputElement).value)"
              />
              <span class="ce-swatch" :style="{ background: swatchOf(f.key) }" />
            </span>
            <input
              type="text"
              class="ce-hex mono"
              spellcheck="false"
              :value="draft[f.key]"
              :placeholder="isAuto(f.key) ? '自动（跟随主色）' : ''"
              @change="onHex(f.key, $event)"
            />
          </label>
        </div>
      </div>
    </div>

    <template #footer>
      <button
        v-if="editingId"
        type="button"
        class="btn sm danger"
        @click="removeCustom(editingId, draft.label)"
      >
        <AppIcon name="trash" :size="14" />删除
      </button>
      <span class="spacer" />
      <button type="button" class="btn sm" @click="closeEditor(true)">取消</button>
      <button type="button" class="btn sm primary" @click="save">
        <AppIcon name="check" :size="15" />保存
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.theme-appearance { display: flex; flex-direction: column; gap: 12px; }

/* 顶部一句话：一行说完这一块能干什么，不解释做法 */
.panel-note {
  margin: 0; font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint);
}

/* 卡片网格：短卡两列并排，长内容整宽。间距只由 .grid 的 gap 负责（base.css 已把网格内的 .card + .card 清零），这里不再给卡片加 margin。 */
.ta-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; }
.ta-card-wide { grid-column: 1 / -1; }
.ta-wall { display: grid; gap: 12px; }

/* 当前配色：一枚小色块 + 名称，只读 */
.ta-swatches { display: grid; gap: 8px 12px; grid-template-columns: repeat(auto-fit, minmax(88px, 1fr)); }
.ta-swatch { display: flex; align-items: center; gap: 7px; min-width: 0; }
.ta-swatch-dot {
  flex: none; width: 16px; height: 16px; border-radius: 5px;
  border: 1px solid var(--border-strong);
}
.ta-swatch-label {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim);
}

/* 自定义入口：一句说明 + 一枚按钮 */
.ta-custom { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; }
.ta-hint { margin: 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim); }

/* 筛选栏：全站分段控件语言（`.seg` 托盘 + 白色 pill），只加一个"有几套"的小计数 ——
   它比任何说明文字都更能说清这一档里有什么。 */
.theme-filter { width: fit-content; max-width: 100%; flex-wrap: wrap; height: auto; }
.theme-filter :deep(button) { gap: 6px; }
.theme-filter-n {
  font-size: calc(11px * var(--ts, 1)); font-weight: 500; color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
.theme-filter :deep(button.active) .theme-filter-n { color: inherit; opacity: .7; }

.theme-appearance-grid {
  display: grid; gap: 10px;
  grid-template-columns: repeat(auto-fill, minmax(216px, 1fr));
}

/* 一枚主题 = 色块 + 名称 + 选中勾（自定义的另有两枚小按钮）。
   名称是唯一的文字：配色特征交给上面的色块，不再写"XX底 · YY强调"。 */
.theme-option {
  position: relative; display: flex; align-items: center; gap: 12px;
  min-height: 76px; padding: 10px 12px; text-align: left; cursor: pointer;
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
.theme-option:focus-visible { outline: none; box-shadow: var(--ring); }
.theme-option.is-selected {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 5%, var(--bg-elev));
  box-shadow: var(--ring);
}

/* 「＋ 自定义」：虚线卡片，与皮肤卡同高但一眼看出是"新增" */
.theme-option.is-new {
  justify-content: flex-start; border-style: dashed; background: transparent;
  color: var(--text-dim); font-family: inherit;
}
.theme-option-new-icon {
  flex: none; display: inline-flex; align-items: center; justify-content: center;
  width: 46px; height: 56px; border-radius: 10px;
  border: 1px dashed var(--border-strong); color: var(--text-faint);
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
.theme-option-copy strong {
  font-size: calc(13px * var(--ts, 1)); font-weight: 600; letter-spacing: -.01em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

.theme-option-check {
  position: absolute; right: 9px; top: 9px;
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 999px;
  background: var(--accent); color: var(--text-on-accent);
}
.theme-option-tools { position: absolute; right: 6px; bottom: 6px; display: inline-flex; gap: 2px; }
.theme-option-tool {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; border: 0; border-radius: 8px;
  background: transparent; color: var(--text-faint); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.theme-option-tool:hover { background: var(--bg-hover); color: var(--text); }
.theme-option-tool.danger:hover { background: var(--danger-soft); color: var(--danger); }

/* ---------------------------- 编辑器 ---------------------------- */
.ce { display: grid; gap: 16px; }
.ce-row { display: flex; align-items: flex-end; gap: 12px; }
.ce-name { flex: 1; display: grid; gap: 5px; font-size: calc(12.5px * var(--ts, 1)); color: var(--text-dim); }
.ce-name input {
  height: var(--control-h); padding: 0 11px; border-radius: var(--radius-sm);
  border: 1px solid var(--border-strong); background: var(--bg-elev); color: var(--text);
  font-family: inherit; font-size: calc(13px * var(--ts, 1));
}
.ce-name input:focus { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.ce-mode { display: inline-flex; gap: 2px; padding: 3px; height: var(--control-h); box-sizing: border-box; border-radius: var(--radius-ctl); background: var(--bg-sunken); }
.ce-mode button {
  border: 0; border-radius: var(--radius-ctl); padding: 0 12px; cursor: pointer;
  background: transparent; color: var(--text-dim); font-family: inherit; font-size: calc(12.5px * var(--ts, 1));
}
.ce-mode button.active { background: var(--bg-elev); color: var(--accent-text); font-weight: 600; box-shadow: var(--shadow-1); }

.ce-block { display: grid; gap: 8px; }
.ce-block h4 { margin: 0; font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; color: var(--text-dim); }
.ce-palettes { display: flex; flex-wrap: wrap; gap: 8px; }
.ce-palette {
  display: inline-flex; align-items: center; gap: 8px; padding: 5px 10px 5px 5px;
  border: 1px solid var(--border); border-radius: var(--radius-ctl);
  background: var(--bg-elev); color: var(--text); font-family: inherit; font-size: calc(12.5px * var(--ts, 1));
  cursor: pointer; transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.ce-palette:hover { border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); background: var(--bg-hover); }
.ce-palette-chip {
  flex: none; width: 30px; height: 20px; border-radius: 999px; padding: 3px;
  border: 1px solid var(--border); display: flex; align-items: center;
}
.ce-palette-chip-in { position: relative; width: 100%; height: 100%; border-radius: 999px; }
.ce-palette-bar { position: absolute; left: 2px; right: 2px; bottom: 1px; height: 3px; border-radius: 2px; }

.ce-fields { display: grid; gap: 7px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.ce-field { display: flex; align-items: center; gap: 9px; font-size: calc(12.5px * var(--ts, 1)); }
.ce-field-label { flex: none; width: 68px; color: var(--text-dim); }
.ce-color {
  position: relative; flex: none; width: 30px; height: 30px;
  border-radius: 9px; overflow: hidden; border: 1px solid var(--border-strong);
}
/* 原生取色器**铺满整块色块且完全透明**：点哪儿都开系统调色板，
   同时保留原生控件的键盘可达性；色块本身只是它下面的一层皮。 */
.ce-color input[type="color"] {
  position: absolute; inset: 0; z-index: 1; width: 100%; height: 100%;
  padding: 0; border: 0; opacity: 0; cursor: pointer;
}
.ce-swatch { position: absolute; inset: 0; }
/* 留空 = 跟随主色：色块描一圈虚线，表示这一档是推出来的、不是用户选的 */
.ce-color.is-auto { border-style: dashed; }
.ce-hex {
  flex: 1; min-width: 0; height: 30px; padding: 0 9px;
  border-radius: 9px; border: 1px solid var(--border);
  background: var(--bg-sunken); color: var(--text-dim); font-size: calc(12px * var(--ts, 1));
}
.ce-hex:focus { outline: none; border-color: var(--accent); color: var(--text); }

/* 窄一档：卡片回落单列，短卡也竖着排 */
@media (max-width: 1100px) {
  .ta-grid { grid-template-columns: minmax(0, 1fr); }
}

@media (max-width: 620px) {
  .ce-fields { grid-template-columns: minmax(0, 1fr); }
}
</style>
