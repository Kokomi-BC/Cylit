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
 * 两类的读法不一样：前者是"风格"，一屏看全就好；后者是"颜色"，多到铺开会把这张卡撑到
 * 五行以上，主题、配色、缩放、预览就一屏看不全 —— 所以走**一条横滚的色带**：
 * 样式照旧（还是同一枚方卡），颜色左右滚着挑。
 */
const skins = computed(() => themes.value.filter((t) => !t.custom && t.order < 100));

/** 色带：自己的排在前面（藏到后面等于找不到），其余按注册顺序 */
const colorThemes = computed(() => [
  ...themes.value.filter((t) => t.custom),
  ...themes.value.filter((t) => !t.custom && t.order >= 100),
]);

const stripRef = ref<HTMLElement | null>(null);

/** 左右各滚一屏多一点：按"看得见的宽度"的 80% 走，别让人一格一格点 */
function scrollStrip(dir: number) {
  const el = stripRef.value;
  if (!el) return;
  el.scrollBy({ left: dir * Math.round(el.clientWidth * 0.8), behavior: 'smooth' });
}

/** 竖向滚轮在这个横条上转成横向 —— 否则鼠标停在色带上怎么滚都不动。
 *  但滚到头就把滚轮还给页面：不然鼠标停在色带上，整页就再也滚不下去了。 */
function onStripWheel(ev: WheelEvent) {
  const el = stripRef.value;
  if (!el) return;
  if (Math.abs(ev.deltaY) <= Math.abs(ev.deltaX)) return;
  if (el.scrollWidth <= el.clientWidth + 1) return;
  const atStart = el.scrollLeft <= 1;
  const atEnd = el.scrollLeft + el.clientWidth >= el.scrollWidth - 1;
  if ((ev.deltaY < 0 && atStart) || (ev.deltaY > 0 && atEnd)) return;
  el.scrollLeft += ev.deltaY;
  ev.preventDefault();
}

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
  <section class="card preset">
    <div class="card-head">
      <div class="hd">
        <h3>主题预设</h3>
        <p>点一套就换，写进这台机器</p>
      </div>
      <span class="spacer" />
      <button type="button" class="add-btn" title="用取色器做一套自己的主题" data-theme-new @click="openEditor()">
        <AppIcon name="add" :size="13" /><span>自定义</span>
      </button>
    </div>

    <div class="card-body preset-body">
      <div class="wall" role="radiogroup" aria-label="主题预设">
        <div
          v-for="t in skins"
          :key="t.id"
          class="tile"
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
              <span class="sw-line" :style="{ background: t.preview.ink }" />
              <span class="sw-line short" :style="{ background: t.preview.ink }" />
            </span>
          </span>
          <span class="tile-label">{{ t.label }}</span>

          <span v-if="t.custom" class="tile-tools">
            <button type="button" class="tile-tool" title="编辑这套主题"
                    @click.stop="openEditor(t)">
              <AppIcon name="edit" :size="12" />
            </button>
            <button type="button" class="tile-tool danger" title="删除这套主题"
                    @click.stop="removeCustom(t.id, t.label)">
              <AppIcon name="trash" :size="12" />
            </button>
          </span>

          <span v-if="active === t.id" class="tile-check" aria-hidden="true">
            <AppIcon name="check" :size="11" />
          </span>
        </div>

      </div>

      <!-- 色带：注册表里那些配色主题，一条横滚的带子 -->
      <div class="strip-row">
        <button type="button" class="strip-btn" aria-label="向前翻" title="更多配色" @click="scrollStrip(-1)">
          <AppIcon name="back" :size="12" />
        </button>
        <div class="strip" ref="stripRef" role="radiogroup" aria-label="更多配色" @wheel="onStripWheel">
        <div
          v-for="t in colorThemes"
          :key="t.id"
          class="tile strip-tile"
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
            </span>
          </span>
          <span class="tile-label">{{ t.label }}</span>

          <span v-if="t.custom" class="tile-tools">
            <button type="button" class="tile-tool" title="编辑这套主题" @click.stop="openEditor(t)">
              <AppIcon name="edit" :size="12" />
            </button>
            <button type="button" class="tile-tool danger" title="删除这套主题"
                    @click.stop="removeCustom(t.id, t.label)">
              <AppIcon name="trash" :size="12" />
            </button>
          </span>

          <span v-if="active === t.id" class="tile-check" aria-hidden="true">
            <AppIcon name="check" :size="11" />
          </span>
        </div>
        </div>
        <button type="button" class="strip-btn" aria-label="向后翻" title="更多配色" @click="scrollStrip(1)">
          <AppIcon name="forward" :size="12" />
        </button>
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
/* ---- 卡头：标题 14 / 说明 11.5，右侧"当前"，三样都在一行里 ---- */
.card-head { padding: 11px 14px 9px; }
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

.preset { display: flex; flex-direction: column; min-width: 0; }
.preset-body { flex: 1; padding: 12px 14px 14px; }

/* 墙：固定三列（每格 ~200 宽，色块与名字都放得下）。auto-fit 在 670 宽时会挤成四列，
   格子变窄、名字开始省略 —— 三列才是这一格该有的密度。窄了退到两列。 */
.wall { display: grid; gap: 8px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
@container (max-width: 640px) {
  .wall { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

/* 一枚主题 = 色块 + 名称（+ 自定义的两枚小按钮）。名称是唯一文字：
   配色特征交给色块，不写"XX 底 · YY 强调"那种说明。 */
.tile {
  position: relative; display: flex; align-items: center; gap: 9px;
  min-height: 44px; padding: 6px 8px; text-align: left; cursor: pointer;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text);
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease),
              box-shadow var(--dur) var(--ease);
}
.tile:hover {
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
  background: var(--bg-hover);
}
.tile:focus-visible { outline: none; box-shadow: var(--ring); }
.tile.is-on {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 6%, var(--bg-elev));
  box-shadow: var(--ring);
}

/* 色块：外框是 bg、内页是 paper，上面一根强调条 + 两条墨线（就是一套皮肤的样子） */
.sw {
  flex: none; width: 32px; height: 32px; padding: 4px 5px;
  border-radius: 9px; border: 1px solid var(--border);
  display: flex; align-items: flex-start;
}
.sw-paper {
  width: 100%; height: 100%; border-radius: 5px; padding: 4px 4px 0;
  display: flex; flex-direction: column; gap: 2px;
}
.sw-bar { display: block; height: 4px; width: 80%; border-radius: 2px; }
.sw-line { display: block; height: 2px; width: 100%; border-radius: 2px; opacity: .5; }
.sw-line.short { width: 58%; opacity: .3; }

.tile-label {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; letter-spacing: -.01em;
}

.tile-check {
  position: absolute; right: 6px; top: 6px;
  display: inline-flex; align-items: center; justify-content: center;
  width: 15px; height: 15px; border-radius: 999px;
  background: var(--accent); color: var(--text-on-accent);
}

/* 自定义的两枚按钮：默认淡出，鼠标进卡 / 键盘聚焦才现身（紧凑但可达） */
.tile-tools {
  position: absolute; right: 4px; bottom: 3px; display: inline-flex; gap: 1px;
  opacity: 0; transition: opacity var(--dur) var(--ease);
}
.tile:hover .tile-tools,
.tile:focus-within .tile-tools { opacity: 1; }
.tile-tool {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border: 0; border-radius: 6px;
  background: transparent; color: var(--text-faint); cursor: pointer;
}
.tile-tool:hover { background: var(--bg-hover); color: var(--text); }
.tile-tool.danger:hover { background: var(--danger-soft); color: var(--danger); }

/* ---- 色带：多出来的配色横着排，左右两颗箭头就是"还能滚"的说明 ---- */
.strip-row { display: flex; align-items: center; gap: 6px; margin-top: 9px; }
.strip-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; cursor: pointer;
  border: 1px solid var(--border); border-radius: 7px;
  background: var(--bg-elev); color: var(--text-dim);
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.strip-btn:hover { background: var(--bg-hover); color: var(--text); }
.strip-btn:focus-visible { outline: none; box-shadow: var(--ring); }

.strip {
  flex: 1; min-width: 0;
  display: flex; gap: 8px; overflow-x: auto; overflow-y: hidden;
  padding-bottom: 2px; scroll-snap-type: x proximity; overscroll-behavior-x: contain;
}
/* 滚动条不画：它和下面那排卡片挨太近，看着像一条多余的分隔线。
   "还能往右滚"由右边露出的半格卡片 + 卡头那颗箭头说清楚。 */
.strip { scrollbar-width: none; }
.strip::-webkit-scrollbar { height: 0; }
/* 色带里的一格比墙里窄一档：一行要多放几套才值得滚 */
.strip-tile { flex: 0 0 auto; width: 142px; min-height: 38px; padding: 3px 8px; scroll-snap-align: start; }
.strip-tile .sw { width: 28px; height: 28px; border-radius: 8px; padding: 3px 4px; }

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
