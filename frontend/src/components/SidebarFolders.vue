<script setup lang="ts">
/**
 * 侧栏的「分类」区 —— 论文自定义分类的就地浏览 / 新建 / 重命名 / 删除。
 *
 * 为什么从 AppSidebar 拆出来：主文件讲的是"导航分组"，这里是数据驱动的三级列表
 * （分类 → 就地展开的论文 → 阅读器）外加就地输入与持久化，塞进同一个文件会让两边都读不下去。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import type { ComponentPublicInstance } from 'vue';
import { useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import Menu from '@/components/Menu.vue';
import { api } from '@/api/client';
import { askConfirm } from '@/stores/confirm';
import {
  createFolder, deleteFolder, folderState, loadFolders, renameFolder,
} from '@/stores/folders';
import { railCollapsed, setRailCollapsed } from '@/stores/shellPrefs';
import { openTab } from '@/stores/tabs';
import type { Paper, PaperFolder } from '@/api/types';

const props = withDefaults(defineProps<{
  /** 侧栏处于手动收起态（图标轨）：整区退化成一枚图标入口 */
  iconOnly?: boolean;
}>(), { iconOnly: false });

const router = useRouter();

/** 「未分类」这一行在展开集合里的键（服务端 id 不会长这样，不会撞）。 */
const UNFILED = '__unfiled__';

/** 一行 = 未分类 / 某个分类；模板因此只需要一份。 */
interface Row {
  id: string;
  name: string;
  count: number;
}

const STORAGE_KEY = 'cylit.sidebar.folders';

interface Prefs {
  collapsed: boolean;
  open: string[];
}

/** 记住"这一区收没收起"和"哪些分类是展开的"：刷新回来保持原位。 */
function readPrefs(): Prefs {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { collapsed: false, open: [] };
    const parsed = JSON.parse(raw) as Partial<Prefs>;
    return {
      collapsed: Boolean(parsed && parsed.collapsed),
      open: Array.isArray(parsed && parsed.open) ? (parsed.open as unknown[]).map(String) : [],
    };
  } catch {
    return { collapsed: false, open: [] };   /* 隐私模式 / 坏数据：当默认展开 */
  }
}

const initial = readPrefs();
const sectionCollapsed = ref(initial.collapsed);
const openIds = ref<string[]>(initial.open);

function persist(): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      collapsed: sectionCollapsed.value,
      open: openIds.value,
    }));
  } catch { /* 隐私模式：界面照常，只是记不住 */ }
}

function toggleSection(): void {
  sectionCollapsed.value = !sectionCollapsed.value;
  persist();
}

function isOpen(id: string): boolean {
  return openIds.value.includes(id);
}

function setOpen(id: string, value: boolean): void {
  const rest = openIds.value.filter((x) => x !== id);
  openIds.value = value ? [...rest, id] : rest;
  persist();
}

/** 展开（并取数）：每次展开都重取，组件内的缓存只在本次展开内有效。 */
function openRow(id: string): void {
  setOpen(id, true);
  void fetchRow(id);
}

function toggleRow(id: string): void {
  if (isOpen(id)) { setOpen(id, false); return; }
  openRow(id);
}

/** <1024px 由 CSS 强制成图标轨（见 AppSidebar 的媒体查询），这里跟它保持同一档。 */
const narrow = ref(false);
const mq = typeof window !== 'undefined' ? window.matchMedia('(max-width: 1024px)') : null;
function onMqChange(ev: MediaQueryListEvent): void { narrow.value = ev.matches; }
const iconOnlyMode = computed(() => props.iconOnly || narrow.value);

/** 图标入口：能展开就展开侧栏，展开不了（窄屏）就去论文库那页看分类。 */
function onIconEntry(): void {
  if (window.matchMedia('(max-width: 1024px)').matches) {
    void router.push({ name: 'library' });
    return;
  }
  setRailCollapsed(false);
  if (sectionCollapsed.value) toggleSection();
}

/** 一行展开后的取数状态。 */
interface RowData {
  items: Paper[];
  loading: boolean;
  error: string;
}

/**
 * 每一行自己取数，**不读全局论文列表**：库页的 loadPapers 会把当前关键词 / 状态筛选
 * 无条件覆盖进那份列表（stores/app.ts:91-95），读它等于让侧栏跟着论文库的搜索框变。
 */
const rowData = reactive<Record<string, RowData>>({});
const EMPTY_ROW: RowData = { items: [], loading: false, error: '' };

function stateOf(rowId: string): RowData {
  return rowData[rowId] ?? EMPTY_ROW;
}

/** 展开时重新取一次（单用户，一次请求很便宜），因此库页刚归类完、回来展开就能看到。 */
async function fetchRow(rowId: string): Promise<void> {
  const entry = rowData[rowId] ?? (rowData[rowId] = { items: [], loading: false, error: '' });
  entry.loading = true;
  entry.error = '';
  try {
    const res = await api.get<{ items: Paper[] }>('/papers', {
      folder: rowId === UNFILED ? 'none' : rowId,
      page_size: 200,      // 后端单页上限；超出部分见行尾那行提示
    });
    entry.items = res?.items ?? [];
  } catch (err) {
    entry.items = [];
    entry.error = (err as Error)?.message ?? String(err);
  } finally {
    entry.loading = false;
  }
}

const rows = computed<Row[]>(() => [
  // 两个数字都只看后端：分类的 paper_count，未分类的 unclassified（GET /folders 一起给）。
  // 未分类为 0 时整行不渲染（连同展开态与缓存篇目）：一个永远空着的行只是噪音。
  ...(folderState.unclassified > 0
    ? [{ id: UNFILED, name: '未分类', count: folderState.unclassified }]
    : []),
  ...folderState.items.map((f) => ({ id: f.id, name: f.name, count: f.paper_count ?? 0 })),
]);

// 分类列表一变（新建 / 改名 / 删除 / 归类后 force 重拉），缓存里的篇目就是旧的：清掉并重取还展开着的行
watch(() => folderState.items, () => {
  for (const key of Object.keys(rowData)) delete rowData[key];
  for (const id of openIds.value) void fetchRow(id);
});

function openPaper(id: string): void {
  void router.push(openTab({ name: 'reader', params: { paperId: id } }) as never);
}

const creating = ref(false);
const editingId = ref<string | null>(null);
const draft = ref('');
const createEl = ref<HTMLInputElement | null>(null);
const renameEl = ref<HTMLInputElement | null>(null);

/** 函数式 ref：重命名输入框在 v-for 里，字符串 ref 会变成数组。 */
function asInput(el: Element | ComponentPublicInstance | null): HTMLInputElement | null {
  return el instanceof HTMLInputElement ? el : null;
}
function bindCreate(el: Element | ComponentPublicInstance | null): void { createEl.value = asInput(el); }
function bindRename(el: Element | ComponentPublicInstance | null): void { renameEl.value = asInput(el); }

function startCreate(): void {
  if (sectionCollapsed.value) toggleSection();
  editingId.value = null;
  draft.value = '';
  creating.value = true;
  void nextTick(() => createEl.value?.focus());
}

function startRename(folder: PaperFolder): void {
  creating.value = false;
  draft.value = folder.name;
  editingId.value = folder.id;
  void nextTick(() => renameEl.value?.select());
}

/**
 * 失焦 = 取消（与 Esc 同义）：分类是会被"顺手点走"的轻操作，
 * 输到一半点别处就默默建出一个半截名字，比丢掉更烦人。
 */
function cancelEdit(): void {
  creating.value = false;
  editingId.value = null;
  draft.value = '';
}

/** Enter 提交后立刻收输入行：Enter 与随之而来的 blur 只会结算一次。 */
async function commitCreate(): Promise<void> {
  if (!creating.value) return;
  const name = draft.value.trim();
  creating.value = false;
  draft.value = '';
  if (!name) return;
  const folder = await createFolder(name);
  if (folder) openRow(folder.id);   // 新建后直接展开，省一次点击
}

async function commitRename(): Promise<void> {
  const id = editingId.value;
  if (!id) return;
  const name = draft.value.trim();
  const before = folderState.items.find((f) => f.id === id)?.name ?? '';
  editingId.value = null;
  draft.value = '';
  if (!name || name === before) return;
  await renameFolder(id, name);
}

const ROW_MENU = [
  { key: 'rename', label: '重命名', icon: 'edit' },
  { key: 'remove', label: '删除分类', icon: 'trash', danger: true },
];

async function onRowAction(row: Row, key: string): Promise<void> {
  const folder = folderState.items.find((f) => f.id === row.id);
  if (!folder) return;
  if (key === 'rename') { startRename(folder); return; }
  if (key !== 'remove') return;
  const ok = await askConfirm({
    title: '删除分类「' + folder.name + '」？',
    body: '里面的论文会回到「未分类」，不会删掉论文。',
    danger: true,
    confirmLabel: '删除分类',
  });
  if (!ok) return;
  if (!(await deleteFolder(folder.id))) return;
  setOpen(folder.id, false);
}

onMounted(() => {
  narrow.value = mq ? mq.matches : false;
  if (mq) mq.addEventListener('change', onMqChange);
  void loadFolders();
  // 上次留在展开态的行，刷新回来得自己去取（否则要先收起再展开才有内容）
  for (const id of openIds.value) void fetchRow(id);
});

onUnmounted(() => {
  if (mq) mq.removeEventListener('change', onMqChange);
});
</script>

<template>
  <!-- 收起态 / <1024px：整区只留一枚图标入口，避免"文字没了、列表还在" -->
  <section v-if="iconOnlyMode" class="sf sf-icon" aria-label="分类">
    <button
      type="button"
      class="sf-icon-btn"
      title="分类"
      aria-label="分类"
      :aria-expanded="!railCollapsed"
      @click="onIconEntry"
    >
      <AppIcon name="tag" :size="18" />
    </button>
  </section>

  <section v-else class="sf" :class="{ 'is-collapsed': sectionCollapsed }" aria-label="分类">
    <div class="sf-head">
      <button
        type="button"
        class="sf-fold"
        :aria-expanded="!sectionCollapsed"
        :title="sectionCollapsed ? '展开分类' : '收起分类'"
        @click="toggleSection"
      >
        <AppIcon name="chevron-down" :size="14" class="sf-caret" />
        <span class="sf-title">分类</span>
      </button>
      <button type="button" class="sf-add" title="新建分类" aria-label="新建分类" @click="startCreate">
        <AppIcon name="add" :size="14" />
      </button>
    </div>

    <div v-if="!sectionCollapsed" class="sf-body">
      <p v-if="folderState.error" class="sf-error">{{ folderState.error }}</p>

      <div v-if="creating" class="sf-input">
        <input
          :ref="bindCreate"
          v-model="draft"
          type="text"
          maxlength="32"
          placeholder="分类名"
          @keydown.enter.prevent="commitCreate"
          @keydown.esc.prevent="cancelEdit"
          @blur="cancelEdit"
        />
      </div>

      <div class="sf-list">
        <template v-for="row in rows" :key="row.id">
          <div class="sf-row" :class="{ open: isOpen(row.id) }">
            <input
              v-if="editingId === row.id"
              :ref="bindRename"
              v-model="draft"
              type="text"
              maxlength="32"
              @keydown.enter.prevent="commitRename"
              @keydown.esc.prevent="cancelEdit"
              @blur="cancelEdit"
            />
            <template v-else>
              <!-- 箭头是独立按钮：只折叠 / 展开这一行，不参与"点名字"那套点击 -->
              <button
                type="button"
                class="sf-caret-btn"
                :aria-expanded="isOpen(row.id)"
                :aria-label="'展开或收起：' + row.name"
                :title="isOpen(row.id) ? '收起' : '展开'"
                @click.stop="toggleRow(row.id)"
              >
                <AppIcon name="chevron-right" :size="12" class="sf-row-caret" />
              </button>
              <button type="button" class="sf-name" :title="row.name" @click="toggleRow(row.id)">
                <span class="sf-text">{{ row.name }}</span>
                <span class="sf-count">{{ row.count }}</span>
              </button>
              <Menu
                v-if="row.id !== UNFILED"
                class="sf-more"
                :items="ROW_MENU"
                :label="'更多操作：' + row.name"
                @select="onRowAction(row, $event)"
              />
            </template>
          </div>

          <template v-if="isOpen(row.id)">
            <p v-if="stateOf(row.id).loading" class="sf-none">正在加载…</p>
            <p v-else-if="stateOf(row.id).error" class="sf-error sf-error-row">{{ stateOf(row.id).error }}</p>
            <template v-else>
              <button
                v-for="p in stateOf(row.id).items"
                :key="p.id"
                type="button"
                class="sf-paper"
                :title="p.title"
                @click="openPaper(p.id)"
              >
                <span class="sf-paper-text">{{ p.title }}</span>
              </button>
              <p v-if="!stateOf(row.id).items.length" class="sf-none">暂无论文</p>
              <p v-else-if="row.count > stateOf(row.id).items.length" class="sf-none">
                仅显示前 200 篇，其余去论文库看
              </p>
            </template>
          </template>
        </template>
        <p v-if="!folderState.items.length" class="sf-empty">
          还没有分类
          <button type="button" class="sf-link" @click="startCreate">新建分类</button>
        </p>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* ⚠️ 这一区是固定高度侧栏里的第二块弹性区：必须自己滚动、且允许被压缩（min-height: 0），
 * 否则高度不够时会把「设置」挤出视口，或者让整条侧栏冒出外层滚动条。 */
.sf {
  flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain;
  display: flex; flex-direction: column; gap: 2px;
  margin: 8px 0; padding-top: 8px;
  border-top: 1px solid var(--divider);
}
/* 收起态只留一行标题：不再占据剩余高度，也不滚动 */
.sf.is-collapsed { flex: 0 0 auto; overflow: hidden; }
/* 图标轨：单个 40px 入口（64px 轨道扣掉内边距还有 44~48px，居中放得下） */
.sf-icon { flex: 0 0 auto; overflow: visible; margin: 8px 0; }

.sf-head {
  /* 列表滚动时标题（和那枚「+」）停在顶部 */
  position: sticky; top: 0; z-index: 1;
  display: flex; align-items: center; gap: 2px;
  padding-bottom: 4px;     /* 标题与第一条分类之间留一口气；用 padding 让 sticky 的背景盖满这段缝隙 */
  background: var(--bg-elev);
}

/* 这一块里最大的一档：14px 标签（不是按钮面 —— 没有底色、没有边框，只有文字和箭头） */
.sf-fold {
  flex: 1; min-width: 0; display: inline-flex; align-items: center; gap: 6px;
  height: 30px; padding: 0 4px 0 8px; border: 0; border-radius: var(--radius-ctl);
  background: transparent; color: var(--text);
  font-size: calc(14px * var(--ts, 1)); font-weight: 650; letter-spacing: .02em;
  cursor: pointer; transition: color var(--dur) var(--ease);
}
.sf-fold:hover { color: var(--accent-text); }
.sf-fold:focus-visible { outline: none; box-shadow: var(--ring); }
.sf-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sf-caret { flex: none; transition: transform var(--dur) var(--ease); }
.sf.is-collapsed .sf-caret { transform: rotate(-90deg); }

.sf-add {
  flex: none; width: 24px; height: 24px; display: inline-flex; align-items: center; justify-content: center;
  padding: 0; border: 0; border-radius: 999px; background: transparent; color: var(--text-faint);
  cursor: pointer; transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.sf-add:hover { background: var(--bg-hover); color: var(--text); }
.sf-add:focus-visible { outline: none; box-shadow: var(--ring); }

.sf-body { display: flex; flex-direction: column; gap: 2px; }
.sf-list { display: flex; flex-direction: column; gap: 1px; }

/* 行高 30px（就地编辑时也不塌）；组间距 6px 由分类行的 margin-top 出，
 * 同一分类的论文之间只隔 .sf-list 的 1px —— 归属一眼可见。 */
.sf-row {
  display: flex; align-items: center; gap: 2px; padding-left: 2px;
  min-height: 28px; border-radius: 8px;
}
.sf-list > .sf-row { margin-top: 6px; }
.sf-list > .sf-row:first-child { margin-top: 0; }
.sf-row:hover { background: var(--bg-hover); }
/* 折叠箭头：独立按钮、定宽 18px（行首那一格），展开 / 收起都不会让名字左右跳；
 * 颜色取最弱一档，悬停与展开时提亮（只旋转，从不隐藏） */
.sf-caret-btn {
  flex: none; width: 18px; height: 28px; display: inline-flex; align-items: center; justify-content: center;
  padding: 0; border: 0; border-radius: 6px; background: transparent; color: var(--text-faint);
  cursor: pointer; transition: color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.sf-row:hover .sf-caret-btn, .sf-row.open .sf-caret-btn { color: var(--accent-text); }
.sf-caret-btn:focus-visible { outline: none; box-shadow: var(--ring); }
.sf-caret-btn:active { background: var(--bg-pressed); }
/* 第一档（最强）：分类名走主题色 + 600，比子项亮也比它大 2px；
 * 「未分类」用的是同一个类，自然同档。悬停 / 展开再深一档（--accent-hover，全站文字加强用的就是它） */
.sf-name {
  flex: 1; min-width: 0; display: inline-flex; align-items: center; gap: 6px;
  height: 28px; padding: 0 6px 0 0; border: 0; border-radius: 8px;
  background: transparent; color: var(--accent-text);
  font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; text-align: left; cursor: pointer;
  transition: color var(--dur) var(--ease);
}
.sf-name:hover, .sf-row.open .sf-name { color: var(--accent-hover); }
.sf-name:focus-visible { outline: none; box-shadow: var(--ring); }
.sf-row-caret { flex: none; transition: transform var(--dur) var(--ease); }
.sf-row.open .sf-row-caret { transform: rotate(90deg); }

.sf-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sf-count {
  flex: none; padding-left: 6px; color: var(--text-faint);
  font-size: calc(11px * var(--ts, 1)); font-variant-numeric: tabular-nums;
}

/* 「⋯」平时藏着，悬停 / 键盘聚焦这一行才出现（行内只留一个主操作） */
.sf-more { flex: none; opacity: 0; transition: opacity var(--dur-fast) var(--ease); }
.sf-row:hover .sf-more, .sf-row:focus-within .sf-more { opacity: 1; }

/* 第二档（明显更弱）：11px / 400 / 弱色。用户口径「论文题目铺满整行、不考虑层级」，
 * 所以不再缩进：左 8px 与侧栏其它行同一条边线，宽度几乎交给标题 */
.sf-paper {
  display: flex; align-items: center; width: 100%; min-width: 0;
  height: 25px; padding: 0 8px; border: 0; border-radius: 6px;
  background: transparent; color: var(--text-dim);
  font-size: calc(11px * var(--ts, 1)); font-weight: 400; text-align: left; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.sf-paper:hover { background: var(--bg-hover); color: var(--text); }
.sf-paper:focus-visible { outline: none; box-shadow: var(--ring); }
.sf-paper-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 就地输入：字号跟分类行走（12.5px），外格 min-height 与所属行一致（28px），编辑时行高不跳 */
.sf-input { display: flex; align-items: center; min-height: 28px; }
.sf-input input, .sf-row input {
  width: 100%; height: 26px; padding: 0 8px; font-size: calc(12.5px * var(--ts, 1));
}

.sf-error {
  margin: 2px 6px 4px; color: var(--danger);
  font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; overflow-wrap: anywhere;
}
.sf-empty {
  display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap;
  margin: 4px 6px 0 22px;   /* 与分类名同一条竖线起排 */
  color: var(--text-faint); font-size: calc(11.5px * var(--ts, 1));
}
.sf-link {
  padding: 0; border: 0; background: transparent; color: var(--accent-text);
  font: inherit; cursor: pointer;
}
.sf-link:hover { text-decoration: underline; }
.sf-none {
  margin: 0 0 2px 8px;      /* 与论文行同一条左线（论文铺满整行后，提示不能还缩进） */
  color: var(--text-faint); font-size: calc(11px * var(--ts, 1));
}
/* 行内取数失败：左线同论文行，字号也一致，仍走 --danger 那一套 */
.sf-error-row { margin: 0 6px 4px 8px; font-size: calc(11px * var(--ts, 1)); }

.sf-icon-btn {
  display: flex; align-items: center; justify-content: center;
  width: 40px; height: 40px; margin: 0 auto; padding: 0;
  border: 0; border-radius: 10px; background: transparent; color: var(--text-dim);
  cursor: pointer; transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.sf-icon-btn:hover { background: var(--bg-hover); color: var(--text); }
.sf-icon-btn:focus-visible { outline: none; box-shadow: var(--ring); }
</style>
