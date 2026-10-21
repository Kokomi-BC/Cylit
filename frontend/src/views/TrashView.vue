<script setup lang="ts">
/**
 * 回收站：软删除的论文在这里恢复，或者彻底删掉。
 *
 * 删除时后端已经清掉批注 / 笔记 / 对话历史与解读产物（见 LibraryView 顶部注释），
 * 所以这页只回答"记录和文件还在不在"，顶部那行小字就是讲这件事 —— 别把它写成"恢复如初"。
 */
import { computed, onMounted, ref } from 'vue';
import { api } from '@/api/client';
import AppIcon from '@/components/AppIcon.vue';
import { askConfirm } from '@/stores/confirm';
import { reportError, toast } from '@/stores/app';
import { fmtRelative } from '@/utils/format';

interface TrashItem {
  id: string;
  title: string;
  status: string;
  page_count: number;
  deleted_at: string | null;
  folder_id: string | null;
  /** 删除时 PDF 有没有一起进回收站：false 的恢复出来是个空壳，得自己重导一份 */
  has_files: boolean;
}

const items = ref<TrashItem[]>([]);
const total = ref(0);
const loading = ref(false);
const loadError = ref('');
/** 正在操作的那一篇（`restore:<id>` / `purge:<id>`）：只禁这一篇的两个按钮，别的行照常可点。 */
const busyId = ref('');
const clearing = ref(false);

const count = computed(() => total.value || items.value.length);

async function load() {
  loading.value = true;
  loadError.value = '';
  try {
    const data = await api.get<{ items: TrashItem[]; total: number }>('/papers/trash');
    items.value = data?.items ?? [];
    total.value = Number(data?.total ?? items.value.length);
  } catch (err) {
    loadError.value = (err as Error)?.message ?? String(err);
  } finally {
    loading.value = false;
  }
}

function deletedLabel(item: TrashItem): string {
  return item.deleted_at ? fmtRelative(item.deleted_at) : '时间未知';
}

async function restore(item: TrashItem) {
  if (busyId.value || clearing.value) return;
  busyId.value = `restore:${item.id}`;
  try {
    const res = await api.post<{ files?: boolean }>(`/papers/${item.id}/restore`);
    items.value = items.value.filter((x) => x.id !== item.id);
    total.value = Math.max(0, count.value - 1);
    if (res?.files === false) {
      toast(`已恢复《${item.title}》，但文件已缺失，需重新导入 PDF`, 'warn', 5600);
    } else {
      toast(`已恢复《${item.title}》`, 'ok');
    }
    // 恢复出来的论文回到论文库那份列表里，那边下次进入自己会重拉（不走全局 store，避免这页顺手改了库页筛选）
  } catch (err) {
    reportError(err, '恢复失败');
  } finally {
    busyId.value = '';
  }
}

async function purge(item: TrashItem) {
  if (busyId.value || clearing.value) return;
  if (!(await askConfirm({
    title: `彻底删除《${item.title || item.id}》？`,
    body: '这一步不可恢复：论文记录与文件都会被删掉，回收站里也不会再留下。',
    confirmLabel: '彻底删除',
    danger: true,
  }))) return;
  busyId.value = `purge:${item.id}`;
  try {
    await api.del(`/papers/${item.id}`, { hard: true });
    items.value = items.value.filter((x) => x.id !== item.id);
    total.value = Math.max(0, count.value - 1);
    toast('已彻底删除', 'ok');
  } catch (err) {
    reportError(err, '彻底删除失败');
  } finally {
    busyId.value = '';
  }
}

async function clearAll() {
  if (clearing.value || busyId.value || !items.value.length) return;
  if (!(await askConfirm({
    title: '清空回收站？',
    body: `会彻底删除这里全部 ${items.value.length} 篇论文的记录与文件，不可恢复。`,
    confirmLabel: '清空',
    danger: true,
  }))) return;
  clearing.value = true;
  try {
    // 后端的 failed 是 [{paper_id, error}]：这里只取 id 报给用户，详情留在服务端日志里
    const res = await api.post<{ purged: number; failed: Array<string | { paper_id?: string }> }>('/papers/trash/clear');
    const purged = Number(res?.purged ?? 0);
    const failedIds = (res?.failed ?? [])
      .map((f) => (typeof f === 'string' ? f : String(f?.paper_id ?? '?')));
    await load();
    if (failedIds.length) toast(`已彻底删除 ${purged} 篇，${failedIds.length} 篇失败：${failedIds.join('、')}`, 'warn', 5600);
    else toast(`已彻底删除 ${purged} 篇`, 'ok');
  } catch (err) {
    reportError(err, '清空回收站失败');
  } finally {
    clearing.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="trash-view">
    <!-- 顶部一行：与论文库同一套版式（左 标题 + 计数；右 刷新 / 清空） -->
    <header class="tr-toolbar">
      <div class="tr-heading">
        <h1>回收站</h1>
        <span class="tr-count tnum">{{ count }} 篇</span>
      </div>
      <div class="tr-tools">
        <button type="button" class="tr-tool square" title="刷新列表" :disabled="loading || clearing" @click="load">
          <AppIcon name="refresh" :size="15" />
        </button>
        <button
          type="button"
          class="tr-tool danger"
          :disabled="clearing || !items.length"
          @click="clearAll"
        >
          <AppIcon name="trash" :size="14" />{{ clearing ? '清空中…' : '清空回收站' }}
        </button>
      </div>
    </header>

    <p class="tr-note">
      回收站里的论文可以恢复；但批注、笔记、对话历史与解读产物在删除时就已清掉，恢复不会带回它们。
    </p>

    <p v-if="loadError" class="tr-error">加载失败：{{ loadError }}（可以点右上角刷新重试）</p>

    <p v-if="loading" class="tr-loading">正在加载…</p>

    <div v-else-if="!items.length" class="empty-state">
      <span class="es-icon"><AppIcon name="trash" :size="20" /></span>
      <span class="es-title">回收站是空的</span>
      <span class="es-hint">删除的论文会先放到这里。</span>
    </div>

    <ul v-else class="tr-list">
      <li v-for="item in items" :key="item.id" class="tr-row">
        <div class="tr-main">
          <span class="tr-title">{{ item.title || item.id }}</span>
          <span class="tr-meta">
            <span>{{ deletedLabel(item) }}</span>
            <span class="tr-dot" aria-hidden="true">·</span>
            <span>{{ item.page_count ? `${item.page_count} 页` : '页数未知' }}</span>
          </span>
        </div>
        <div class="tr-acts">
          <span v-if="!item.has_files" class="tr-warn">文件已缺失，恢复后需重新导入 PDF</span>
          <button
            type="button"
            class="tr-act"
            :disabled="!!busyId || clearing"
            @click="restore(item)"
          >
            <AppIcon name="undo" :size="13" />{{ busyId === `restore:${item.id}` ? '恢复中…' : '恢复' }}
          </button>
          <button
            type="button"
            class="tr-act danger"
            :disabled="!!busyId || clearing"
            @click="purge(item)"
          >
            <AppIcon name="trash" :size="13" />{{ busyId === `purge:${item.id}` ? '删除中…' : '彻底删除' }}
          </button>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
/* 顶部一行：与论文库的 .lib-toolbar 同一套语言（这里是 scoped，所以类名各写一份、令牌共用）。 */
.tr-toolbar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin-bottom: 12px; padding-bottom: 12px;
  border-bottom: 1px solid var(--divider);
}
.tr-heading { display: flex; align-items: center; gap: 8px; flex: none; min-width: 0; }
.tr-heading h1 {
  margin: 0; font-size: calc(16px * var(--ts, 1)); font-weight: 600; letter-spacing: -.02em;
}
.tr-count {
  flex: none; display: inline-flex; align-items: center; height: 20px; padding: 0 8px;
  border-radius: 999px; background: var(--bg-subtle); border: 1px solid var(--border);
  font-size: calc(11px * var(--ts, 1)); color: var(--text-dim);
}
.tr-tools { display: flex; align-items: center; gap: 8px; flex: none; margin-left: auto; }
.tr-tool {
  display: inline-flex; align-items: center; gap: 6px;
  height: 30px; padding: 0 12px; border-radius: var(--radius-ctl);
  border: 1px solid var(--border); background: transparent; color: var(--text);
  font-family: inherit; font-size: calc(12px * var(--ts, 1)); font-weight: 500; cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease),
              color var(--dur) var(--ease), transform var(--dur-fast) var(--ease);
}
.tr-tool:hover:not(:disabled) { background: var(--bg-hover); border-color: var(--border-strong); }
.tr-tool:active:not(:disabled) { transform: scale(.97); }
.tr-tool.square { width: 30px; padding: 0; justify-content: center; }
.tr-tool.danger { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 38%, var(--border-strong)); }
.tr-tool.danger:hover:not(:disabled) { background: var(--danger-soft); }
.tr-tool:disabled { opacity: .5; cursor: default; }

/* 顶部说明：小字、一行读得完；窄屏自然换行，不设固定宽度 */
.tr-note {
  margin: 0 0 14px; font-size: calc(12px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim);
}
.tr-error { margin: 0 0 12px; font-size: calc(12.5px * var(--ts, 1)); color: var(--danger); }
.tr-loading { margin: 0; padding: 26px 4px; font-size: calc(13px * var(--ts, 1)); color: var(--text-dim); }

.tr-list { display: flex; flex-direction: column; gap: 4px; margin: 0; padding: 0 0 12px; list-style: none; }

/* 一行一条：左标题 + 元信息（可伸缩、可截断），右操作（不许被文本挤出视口）。
   两侧都 min-width: 0，长标题只会 ellipsis / 换行，永远不产生横向滚动。 */
.tr-row {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 10px 12px; border-radius: var(--radius-ctl);
  border: 1px solid var(--border); background: var(--bg-elev);
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.tr-row:hover { border-color: var(--border-strong); background: var(--bg-hover); }
.tr-main { flex: 1 1 220px; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.tr-title {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: calc(13px * var(--ts, 1)); font-weight: 500; color: var(--text);
}
.tr-meta {
  display: flex; align-items: center; gap: 6px; min-width: 0;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
}
.tr-dot { color: var(--divider); }

.tr-acts { flex: none; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
/* 文件缺失提示：紧挨「恢复」，只在它旁边出现一次；长度可控（一行中文），跟着操作组一起换行 */
.tr-warn {
  min-width: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.4; color: var(--warn);
}
.tr-act {
  display: inline-flex; align-items: center; gap: 5px;
  height: 28px; padding: 0 11px; border-radius: var(--radius-ctl);
  border: 1px solid var(--border); background: transparent; color: var(--text);
  font-family: inherit; font-size: calc(12px * var(--ts, 1)); font-weight: 500; cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease), color var(--dur) var(--ease);
}
.tr-act:hover:not(:disabled) { background: var(--bg-hover); border-color: var(--border-strong); }
.tr-act:focus-visible { outline: none; box-shadow: var(--ring); }
.tr-act.danger { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 38%, var(--border-strong)); }
.tr-act.danger:hover:not(:disabled) { background: var(--danger-soft); }
.tr-act:disabled { opacity: .5; cursor: default; }

/* 窄屏（900px 档）：标题行占满，操作组整体落到下一行并右对齐，不挤成一团 */
@media (max-width: 980px) {
  .tr-acts { width: 100%; justify-content: flex-start; }
  .tr-warn { flex: 1 1 100%; }
}
</style>
