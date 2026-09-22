<script setup lang="ts">
/**
 * 阅读器侧栏的内容区（**与目录合并成同一栏**，由 ReaderView 的页签切换）。
 *
 * 页签：批注·笔记（合并）/ 公式 / 插图 / 术语 / 任务（目录由 ReaderToc 负责）。
 * 公式**只读**：来自 Markdown 提取（ReaderFormulaList），不做复核、不分级、不比对裁图。
 */
import { computed } from 'vue';
import { mdToHtml } from '@/composables/useMarkdown';
import { fmtRelative } from '@/utils/format';
import type {
  Annotation, FigureItem, GlossaryTerm, MarkdownFormula, Note, ReaderStats, TaskInfo,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import ProgressBar from '@/components/ProgressBar.vue';
import ReaderFormulaList from './ReaderFormulaList.vue';

/** 简报草稿：`reactive` 对象由父组件持有，这里直接双向使用（不复制，避免两份状态）。 */
export interface BriefDraft {
  domain: string;
  style: string;
  notes: string;
  glossaryText: string;
}

/** 侧栏页签（与 ReaderView 的页签栏一一对应；批注与笔记已合并为一个页签） */
export type SideTab = 'annotations' | 'formulas' | 'figures' | 'terms' | 'tasks';

const props = withDefaults(defineProps<{
  tab: SideTab;
  annotations: Annotation[];
  notes: Note[];
  formulas: MarkdownFormula[];
  formulasLoading?: boolean;
  figures: FigureItem[];
  glossary: GlossaryTerm[];
  briefDraft: BriefDraft;
  stats: ReaderStats;
  progressPercent: number;
  activeTask: TaskInfo | null;
}>(), { formulasLoading: false });

const emit = defineEmits<{
  (e: 'focus', rowId: string, page?: number): void;
  (e: 'copy', text: string): void;
  (e: 'zoom', url: string): void;
  (e: 'save-brief'): void;
  (e: 'regenerate-brief'): void;
  (e: 'delete-annotation', id: string): void;
  (e: 'delete-note', id: string): void;
  (e: 'cancel-task', task: TaskInfo): void;
}>();

function locate(id?: string | null, page?: number | null) {
  if (!id && !page) return;
  emit('focus', id ?? '', page ?? undefined);
}

const annotationHtml = (text: string) => mdToHtml(text);

/** 批注 + 笔记：合并成一个按时间倒序的信息流（各自的类型用徽章区分）。 */
interface MergedItem {
  key: string;
  kind: 'selection' | 'annotation' | 'note';
  label: string;
  badge: string;
  anchorId: string | null;
  page: number | null;
  quote: string;
  contentMd: string;
  at: string | null;
}

const merged = computed<MergedItem[]>(() => {
  const items: MergedItem[] = [];
  for (const a of props.annotations) {
    const selection = a.source === 'selection';
    items.push({
      key: `a:${a.id}`, kind: selection ? 'selection' : 'annotation',
      label: selection ? '划词解释' : (a.type || '批注'),
      badge: selection ? 'ok' : 'accent',
      anchorId: a.block_id, page: a.page ?? null,
      quote: '', contentMd: a.content_md, at: a.created_at,
    });
  }
  for (const n of props.notes) {
    items.push({
      key: `n:${n.id}`, kind: 'note', label: '笔记', badge: 'accent',
      anchorId: n.block_id, page: n.page ?? null,
      quote: n.selected_text ?? '', contentMd: n.content_md,
      at: n.updated_at ?? n.created_at ?? null,
    });
  }
  return items.sort((x, y) => String(y.at ?? '').localeCompare(String(x.at ?? '')));
});
</script>

<template>
  <div class="panel-body">
    <!-- 批注 + 笔记（合并页签：按时间倒序的信息流） -->
    <template v-if="props.tab === 'annotations'">
      <div v-if="!merged.length" class="empty small">还没有批注或笔记（在正文里划词即可新建）</div>
      <div v-for="m in merged" :key="m.key" class="item" :class="m.kind">
        <div class="row tiny">
          <span class="badge" :class="m.badge">{{ m.label }}</span>
          <span v-if="m.page" class="faint">p{{ m.page }}</span>
          <span v-if="m.at" class="faint">{{ fmtRelative(m.at) }}</span>
          <span class="spacer" />
          <button class="btn icon sm" title="定位" @click="locate(m.anchorId, m.page)">
            <AppIcon name="locate" :size="13" />
          </button>
          <button
            class="btn icon sm"
            title="删除"
            @click="m.kind === 'note' ? emit('delete-note', m.key.slice(2)) : emit('delete-annotation', m.key.slice(2))"
          >
            <AppIcon name="trash" :size="13" />
          </button>
        </div>
        <div v-if="m.quote" class="small faint">“{{ m.quote }}”</div>
        <div class="small md" v-html="annotationHtml(m.contentMd)" />
      </div>
    </template>

    <!-- 公式（从 Markdown 提取，只读） -->
    <template v-if="props.tab === 'formulas'">
      <ReaderFormulaList
        :items="props.formulas"
        :loading="props.formulasLoading"
        @focus="(id: string, page?: number) => emit('focus', id, page)"
        @copy="emit('copy', $event)"
      />
    </template>

    <!-- 插图 -->
    <template v-if="props.tab === 'figures'">
      <div v-for="fig in props.figures" :key="fig.block_id" class="item">
        <div class="row tiny">
          <span class="badge">p{{ fig.page }}</span>
          <span v-if="fig.number" class="badge accent">图 {{ fig.number }}</span>
          <span class="spacer" />
          <button class="btn icon sm" title="定位到图注" @click="locate(fig.row_id || fig.block_id, fig.page)">
            <AppIcon name="locate" :size="13" />
          </button>
        </div>
        <img
          v-if="fig.image_url"
          :src="fig.image_url"
          class="figure"
          loading="lazy"
          :title="fig.caption"
          @click="emit('zoom', fig.image_url)"
        >
        <div class="tiny faint">{{ fig.caption }}</div>
      </div>
      <div v-if="!props.figures.length" class="empty small">没有插图</div>
    </template>

    <!-- 术语 / 文档简报 -->
    <template v-if="props.tab === 'terms'">
      <div class="row tiny" style="margin-bottom:6px">
        <span class="muted">文档简报</span>
        <span class="spacer" />
        <button class="btn sm" @click="emit('regenerate-brief')">
          <AppIcon name="refresh" :size="13" />重新生成
        </button>
        <button class="btn sm primary" @click="emit('save-brief')">
          <AppIcon name="check" :size="13" />保存
        </button>
      </div>
      <label class="field">领域<input v-model="props.briefDraft.domain" placeholder="向量量化 / 近似最近邻"></label>
      <label class="field" style="margin-top:6px">风格<input v-model="props.briefDraft.style" placeholder="学术、术语统一"></label>
      <label class="field" style="margin-top:6px">
        要点（每行一条）
        <textarea v-model="props.briefDraft.notes" rows="3" />
      </label>
      <label class="field" style="margin-top:6px">
        术语表（每行 <span class="mono">english=中文</span>）
        <textarea v-model="props.briefDraft.glossaryText" rows="8" />
      </label>
      <div v-for="(t, i) in props.glossary.slice(0, 60)" :key="t.source ?? t.en ?? i" class="term-row">
        <span class="en truncate">{{ t.source ?? t.en }}</span>
        <span class="zh truncate">{{ t.target ?? t.zh }}</span>
      </div>
    </template>

    <!-- 任务 + 进度 -->
    <template v-if="props.tab === 'tasks'">
      <div class="item">
        <div class="row tiny">
          <span class="muted">翻译进度</span>
          <span class="spacer" />
          <span class="mono">{{ props.stats.pages_done ?? 0 }} / {{ props.stats.pages ?? 0 }} 页</span>
        </div>
        <ProgressBar :value="props.progressPercent" />
        <div class="row tiny faint" style="margin-top:6px">
          <span>成对行 {{ props.stats.rows ?? 0 }}</span>
          <span class="spacer" />
          <span v-if="props.stats.model" class="truncate mono" :title="props.stats.model">
            {{ props.stats.model }}
          </span>
        </div>
      </div>
      <div v-if="props.activeTask" class="item">
        <div class="row tiny">
          <span class="badge" :class="props.activeTask.status === 'processing' ? 'accent' : ''">
            {{ props.activeTask.status }}
          </span>
          <span class="faint">{{ props.activeTask.done_items }}/{{ props.activeTask.total_items }}</span>
          <span class="spacer" />
          <button class="btn sm danger" @click="emit('cancel-task', props.activeTask)">
            <AppIcon name="stop" :size="13" />取消
          </button>
        </div>
        <div class="tiny faint">{{ props.activeTask.message }}</div>
        <ProgressBar :value="props.activeTask.progress" />
      </div>
      <div v-else class="empty small">当前没有进行中的任务</div>
    </template>
  </div>
</template>

<style scoped>
.panel-body {
  overflow-y: auto; overflow-x: hidden; flex: 1; min-height: 0; padding: 8px 10px 24px;
  overflow-wrap: anywhere; word-break: break-word;
}
.item {
  border: 1px solid var(--border); border-left: 2px solid var(--border-strong);
  border-radius: var(--radius-sm); background: var(--bg-layer);
  padding: 8px 10px; margin-bottom: 8px;
}
.item.manual { border-left-color: var(--accent); }
.item.auto, .item.selection { border-left-color: var(--ok); }
.item.note { border-left-color: var(--warn); }
.item .figure {
  max-width: 100%; background: #fff; border-radius: var(--radius-sm);
  margin: 6px 0 2px; cursor: zoom-in;
}
.term-row { display: flex; gap: 8px; padding: 5px 6px; border-bottom: 1px solid var(--divider); font-size: 12.5px; }
.term-row .en { flex: 1; font-family: var(--mono); font-size: 12px; }
.term-row .zh { flex: 1; }
</style>
