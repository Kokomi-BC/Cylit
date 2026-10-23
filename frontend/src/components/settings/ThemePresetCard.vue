<script setup lang="ts">
/**
 * 设置 · 显示主题 · **主题预设**：一墙皮肤 + 自定义主题编辑器。
 *
 * 数据全部来自注册表（`@/theme/theme` → `registry.ts`）：内置皮肤、跟随系统、用户自建
 * 走同一套 CSS 令牌，这里只负责"用户点了哪个 id"。
 *
 * 没做筛选栏：预设一共六套，一屏看完；筛选反而多一行、还得分组说明它为什么存在。
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
  type ThemeDefinition,
  type ThemeSpec,
} from '@/theme/theme';

const themes = ref<ThemeDefinition[]>(listThemes());
const active = ref(getTheme());

/**
 * 注册表里有二十多套：**六套手写皮肤**（跟随系统 / 经典 / 素纸 / 墨家 / 雾青 / 黛瓦夜色）
 * + 一批由调色板推出来的**配色**，还有用户自建的。
 *
 * 2026-12 用户口径：改成**竖向矩形列表 + 竖向滚动**，并与「当前配色」并成一张卡
 * （列表在左、配色在右）—— 横滚色带在宽屏上要来回拨箭头，竖排一眼能扫完、滚一格就是一行。
 * 两类仍分两组（「预设」是风格、「配色」是颜色），组间只隔一行 11px 小字，不再分两张卡。
 */
const skins = computed(() => themes.value.filter((t) => !t.custom && t.order < 100));

/** 色带：自己的排在前面（藏到后面等于找不到），其余按注册顺序 */
const colorThemes = computed(() => [
  ...themes.value.filter((t) => t.custom),
  ...themes.value.filter((t) => !t.custom && t.order >= 100),
]);


function refresh() { themes.value = listThemes(); }

function choose(id: string) { active.value = setTheme(id); }

/** 另一个标签页改了皮肤时跟一下。预览中的临时主题不参与"选中"，否则正在编辑的那套会闪走。 */
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
    title: '删除主题「' + label + '」？',
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

function isAuto(key: ColorKey): boolean { return key === 'accentSoft' && !draft.accentSoft; }

onMounted(() => window.addEventListener(THEME_CHANGE_EVENT, onThemeEvent));
onUnmounted(() => window.removeEventListener(THEME_CHANGE_EVENT, onThemeEvent));
</script>

<template>
  <section class="preset">
    <div class="block-head">
      <div class="hd">
        <h3>主题预设</h3>
        <p>点一套就换，写进这台机器</p>
      </div>
      <span class="spacer" />
      <button type="button" class="add-btn" title="用取色器做一套自己的主题" data-theme-new @click="openEditor()">
        <AppIcon name="add" :size="13" /><span>自定义</span>
      </button>
    </div>

    <!-- 一排多个主体小卡（名字在色块下面），竖向滚动：25 套铺开会把这一栏撑到十行以上，
         滚动条本身就是「还有更多」的说明。 -->
    <div class="preset-list" data-preset-list>
      <div class="pgroup" role="radiogroup" aria-label="外观预设">
        <div class="group-label">预设</div>
        <div
          v-for="t in skins"
          :key="t.id"
          class="row"
          :class="{ 'is-on': active === t.id }"
          role="radio"
          :aria-checked="active === t.id"
          tabindex="0"
          :title="t.label"
          :data-theme-option="t.id"
          @click="choose(t.id)"
          @keydown.enter.prevent="choose(t.id)"
          @keydown.space.prevent="choose(t.id)"
        >
          <span class="sw" :style="{ background: t.preview.bg }" aria-hidden="true">
            <span class="sw-paper" :style="{ background: t.preview.paper }">
              <span class="sw-bar" :style="{ background: t.preview.accent }" />
              <span class="sw-line mid" :style="{ background: t.preview.ink }" />
              <span class="sw-line" :style="{ background: t.preview.ink }" />
              <span class="sw-line short" :style="{ background: t.preview.ink }" />
              <span class="sw-line short" :style="{ background: t.preview.ink }" />
            </span>
          </span>
          <span class="row-label">{{ t.label }}</span>

          <span v-if="t.custom" class="row-tools">
            <button type="button" class="row-tool" title="编辑这套主题" @click.stop="openEditor(t)">
              <AppIcon name="edit" :size="11" />
            </button>
            <button type="button" class="row-tool danger" title="删除这套主题"
                    @click.stop="removeCustom(t.id, t.label)">
              <AppIcon name="trash" :size="11" />
            </button>
          </span>

          <span v-if="active === t.id" class="row-check" aria-hidden="true">
            <AppIcon name="check" :size="10" />
          </span>
        </div>
      </div>

      <div class="pgroup" role="radiogroup" aria-label="配色">
        <div class="group-label">配色</div>
        <div
          v-for="t in colorThemes"
          :key="t.id"
          class="row"
          :class="{ 'is-on': active === t.id }"
          role="radio"
          :aria-checked="active === t.id"
          tabindex="0"
          :title="t.label"
          :data-theme-option="t.id"
          @click="choose(t.id)"
          @keydown.enter.prevent="choose(t.id)"
          @keydown.space.prevent="choose(t.id)"
        >
          <span class="sw" :style="{ background: t.preview.bg }" aria-hidden="true">
            <span class="sw-paper" :style="{ background: t.preview.paper }">
              <span class="sw-bar" :style="{ background: t.preview.accent }" />
              <span class="sw-line mid" :style="{ background: t.preview.ink }" />
              <span class="sw-line" :style="{ background: t.preview.ink }" />
              <span class="sw-line short" :style="{ background: t.preview.ink }" />
              <span class="sw-line short" :style="{ background: t.preview.ink }" />
            </span>
          </span>
          <span class="row-label">{{ t.label }}</span>

          <span v-if="t.custom" class="row-tools">
            <button type="button" class="row-tool" title="编辑这套主题" @click.stop="openEditor(t)">
              <AppIcon name="edit" :size="11" />
            </button>
            <button type="button" class="row-tool danger" title="删除这套主题"
                    @click.stop="removeCustom(t.id, t.label)">
              <AppIcon name="trash" :size="11" />
            </button>
          </span>

          <span v-if="active === t.id" class="row-check" aria-hidden="true">
            <AppIcon name="check" :size="10" />
          </span>
        </div>
      </div>
    </div>
  </section>

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
/* ---- 块头：标题 14 / 说明 11.5 / 右侧「＋ 自定义」，三样一行。
   这一栏（主题预设 / 字体设置 / 高亮效果）**不套卡片**，只用留白与细线分区（用户口径 2026-12）。 ---- */
.block-head { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 10px; }
.hd { display: grid; gap: 2px; min-width: 0; }
.hd h3 {
  margin: 0; font-size: calc(14px * var(--ts, 1)); font-weight: 650;
  letter-spacing: -.01em; line-height: 1.3;
}
.hd p { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.4; color: var(--text-faint); }
/* 「＋ 自定义」：卡头里的小按钮。做主题是**动作**，不是第七套皮肤 ——
   摆进墙里会占掉半行格子，摆进色带又得滚到最后才看得见。 */
.add-btn {
  flex: none; display: inline-flex; align-items: center; gap: 3px;
  height: 24px; padding: 0 8px; cursor: pointer;
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--bg-elev); color: var(--text-dim);
  font-family: inherit; font-size: calc(11px * var(--ts, 1));
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease),
              border-color var(--dur) var(--ease);
}
.add-btn:hover { background: var(--bg-hover); color: var(--text); border-color: var(--border-strong); }
.add-btn:focus-visible { outline: none; box-shadow: var(--ring); }

.preset { min-width: 0; }
/* 列表：一排多个主体小卡（名字在色块下面），**竖向滚动**。25 套铺开会把这一栏撑到
   十行以上；竖排的滚动条本身就是「还有更多」的说明。 */
.preset-list {
  /* 高度 460：每排几张随宽度变（1600 六张、1480 四张、1440 九张），任何定值都会切到某一排，
     所以要保证**至少一整排配色 + 下一条排头**完整可见，剩下的交给 28px 淡出 ——
     露个四五十像素像渲染坏了，露一整排才像「下面还有」（用户口径 2026-12：中等宽度像错位）。 */
  min-width: 0; max-height: 460px; overflow-y: auto; overscroll-behavior: contain;
  display: flex; flex-direction: column; gap: 10px;
  padding-right: 4px;
  scrollbar-width: thin; scrollbar-gutter: stable;
  /* 底边 28px 淡出：被裁的那半排是「下面还有」，不是「渲染坏了」 */
  mask-image: linear-gradient(to bottom, #000 calc(100% - 28px), transparent);
}
/* 竖卡：**色块在上、名字在下（左对齐）**，一排多个（用户口径 2026-12 第三次确认）。
   轨道固定 88px —— 卡宽 ≈ 名字那么宽，不会拖成一条长条；auto-fill 自己决定一排几个
   （760 宽排 8 个、440 排 4 个、328 排 3 个）。 */
.pgroup {
  display: grid; grid-template-columns: repeat(auto-fill, 88px);
  gap: 8px; align-content: start; justify-content: start; min-width: 0;
}
.group-label {
  grid-column: 1 / -1;
  padding: 0 2px; font-size: calc(11px * var(--ts, 1)); font-weight: 650;
  letter-spacing: .02em; color: var(--text-faint);
}

/* 一枚主题 = 色块 + 名字（+ 自定义的两枚小按钮，悬停才现身）。
   名字是唯一文字，**左对齐贴在色块下面**：配色特征交给色块，不写说明。 */
.row {
  position: relative;
  display: flex; flex-direction: column; align-items: stretch; gap: 4px;
  min-width: 0; padding: 5px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text); cursor: pointer;
  font-family: inherit; text-align: left;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease),
              box-shadow var(--dur) var(--ease);
}
.row:hover {
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
  background: var(--bg-hover);
}
.row:focus-visible { outline: none; box-shadow: var(--ring); }
.row.is-on {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 6%, var(--bg-elev));
  box-shadow: var(--ring);
}

/* 色块：外框是 bg、内页是 paper，上面一根强调条（就是一套皮肤的样子） */
.sw {
  width: 100%; height: 96px; padding: 5px 5px;
  border-radius: 7px; border: 1px solid var(--border);
  display: flex; align-items: flex-start;
}
/* 内页那圈描边不能省：纸色与行的白底几乎同色，少了它色块读起来只是一个点 */
.sw-paper {
  width: 100%; height: 100%; border-radius: 5px; padding: 5px 4px 0;
  display: flex; flex-direction: column; gap: 2px;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--text) 10%, transparent);
}
.sw-bar { display: block; height: 5px; width: 70%; border-radius: 2px; }
.sw-line { display: block; height: 3px; width: 100%; border-radius: 2px; opacity: .45; }
.sw-line.mid { width: 82%; opacity: .4; }
.sw-line.short { width: 58%; opacity: .28; }

.row-label {
  min-width: 0; text-align: left;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: calc(11px * var(--ts, 1)); font-weight: 600; line-height: 1.3;
}
.row.is-on .row-label { color: var(--accent-text); }

/* 选中勾：压在色块右上角（位置固定，切换时不跳） */
.row-check {
  position: absolute; top: 9px; right: 9px;
  display: inline-flex; align-items: center; justify-content: center;
  width: 16px; height: 16px; border-radius: 999px;
  background: var(--accent); color: var(--text-on-accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--bg-elev) 70%, transparent);
}

/* 自定义的两枚按钮：默认淡出，鼠标进这一格 / 键盘聚焦才现身（紧凑但可达） */
.row-tools {
  position: absolute; top: 9px; left: 9px;
  display: inline-flex; gap: 2px;
  opacity: 0; transition: opacity var(--dur) var(--ease);
}
.row:hover .row-tools,
.row:focus-within .row-tools { opacity: 1; }
.row-tool {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border: 0; border-radius: 5px;
  background: color-mix(in srgb, var(--bg-elev) 92%, transparent);
  color: var(--text-faint); cursor: pointer;
}
.row-tool:hover { background: var(--bg-hover); color: var(--text); }
.row-tool.danger:hover { background: var(--danger-soft); color: var(--danger); }

/* 窄了：配色落到列表下面（右栏 316 一旦保不住，两列会一起挤成两条窄缝） */
@container (max-width: 460px) {
  .preset-list { max-height: 340px; }
}

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

@media (max-width: 620px) {
  .ce-fields { grid-template-columns: minmax(0, 1fr); }
}
</style>
