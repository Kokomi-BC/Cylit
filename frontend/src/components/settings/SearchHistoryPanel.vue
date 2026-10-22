<script setup lang="ts">
/**
 * 设置页「搜索记录」面板：知识库搜索历史的**管理入口**（列表 / 删单条 / 清空 / 保留条数）。
 *
 * 记录本身由知识库页在每次检索后写入 `utils/searchHistory`（**只存本机 localStorage**，
 * 不进数据库、不上传）；这里只做管理，不改任何后端数据。
 *
 * "像浏览器那样"的取舍：新的在前、同词去重置顶、上限可调、可逐条删。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';
import {
  DEFAULT_LIMIT, LIMIT_CHOICES, clearHistory, historyLimit, loadHistory,
  removeSearch, setHistoryLimit, subscribeHistory, type SearchEntry,
} from '@/utils/searchHistory';
import { paperTitle, toast } from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import { fmtDate, fmtRelative, truncate } from '@/utils/format';
import AppIcon from '@/components/AppIcon.vue';

const entries = ref<SearchEntry[]>([]);
const limit = ref(DEFAULT_LIMIT);
let unsubscribe: (() => void) | null = null;

/** 时间戳 → 展示用：相对时间 + 精确时间 tooltip（fmtDate/fmtRelative 都吃 ISO 字符串）。 */
function iso(ts: number): string {
  return new Date(ts).toISOString();
}

/**
 * 范围文案：`全部` / `本论文《标题》` / `跨文献（排除《标题》）`。
 * 论文标题优先用缓存里的（服务端权威），拿不到就退回 id —— 不假装知道。
 */
function scopeText(e: SearchEntry): string {
  if (e.scope === 'all' || !e.paper_id) return '全部';
  const title = truncate(paperTitle(e.paper_id), 28);
  return e.scope === 'cross' ? `跨文献（排除《${title}》）` : `本论文《${title}》`;
}

/** 只有真的出现过别的模式才显示「模式」列（现在只有 search，不占版面）。 */
const hasOtherModes = computed(() => entries.value.some((e) => e.mode !== 'search'));

function modeText(mode: string): string {
  return { search: '相似检索', qa: '问答', browse: '浏览' }[mode] ?? mode;
}

function reload() {
  entries.value = loadHistory();
  limit.value = historyLimit();
}

function onRemove(e: SearchEntry) {
  removeSearch(e);
  reload();
  toast('已删除这条搜索记录', 'ok', 2000);
}

async function onClear() {
  if (!entries.value.length) return;
  if (!(await askConfirm({
    title: `清空全部 ${entries.value.length} 条搜索记录？`,
    body: '只影响本机浏览器里的这份记录，论文、索引、笔记都不受影响。',
    confirmLabel: '清空',
  }))) return;
  clearHistory();
  reload();
  toast('已清空搜索记录', 'ok', 2400);
}

function onLimitChange() {
  setHistoryLimit(limit.value);
  reload();
  toast(`搜索记录最多保留 ${limit.value} 条`, 'ok', 2400);
}

onMounted(() => {
  reload();
  // 论文标题只用来把 paper_id 显示成人看得懂的名字：`state.papers` 是全局响应式的
  // （同页的「知识库与索引」面板会加载它），这里**不再单独拉一次**，加载完标题自动补上。
  unsubscribe = subscribeHistory(reload);   // 其它标签页改了记录，这里跟着变
});

onUnmounted(() => {
  unsubscribe?.();
  unsubscribe = null;
});
</script>

<template>
  <div class="sh">
    <p class="panel-note">记着你搜过什么，想删就删。</p>

    <div class="grid sh-grid">
      <!-- 保留条数：一张短卡 -->
      <div class="card">
        <div class="card-head">保留条数</div>
        <div class="card-body">
          <label class="field">
            <span class="field-top">最多保留</span>
            <select v-model.number="limit" @change="onLimitChange">
              <option v-for="n in LIMIT_CHOICES" :key="n" :value="n">{{ n }} 条</option>
            </select>
          </label>
          <p class="sh-hint">放满了就从最旧的一条开始丢。</p>
        </div>
      </div>

      <!-- 清空：一张短卡 -->
      <div class="card">
        <div class="card-head">清空</div>
        <div class="card-body sh-actions">
          <p class="sh-hint">清掉的是本机这份记录，论文和笔记都不动。</p>
          <button class="btn sm" :disabled="!entries.length" @click="onClear">
            <AppIcon name="trash" :size="14" />清空全部
          </button>
        </div>
      </div>

      <!-- 记录表：长内容，整宽 -->
      <div class="card sh-card-wide" v-if="entries.length">
        <div class="card-head">
          <span>搜索记录</span>
          <span class="sh-count tnum">{{ entries.length }} 条</span>
        </div>
        <table class="data fixed">
          <thead>
            <tr>
              <th>搜索词</th>
              <th v-if="hasOtherModes" class="w-mode">模式</th>
              <th class="w-scope">范围</th>
              <th class="num w-hits">命中</th>
              <th class="w-time">时间</th>
              <th class="w-ops">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="e in entries" :key="`${e.ts}-${e.q}`">
              <td class="truncate" :title="e.q">{{ e.q }}</td>
              <td v-if="hasOtherModes"><span class="badge">{{ modeText(e.mode) }}</span></td>
              <td class="tiny truncate" :title="scopeText(e)">{{ scopeText(e) }}</td>
              <td class="num">{{ e.hits === null ? '—' : e.hits }}</td>
              <td class="tiny" :title="fmtDate(iso(e.ts))">{{ fmtRelative(iso(e.ts)) }}</td>
              <td class="ops">
                <button class="btn sm ghost" title="删除这条记录" @click="onRemove(e)">
                  <AppIcon name="trash" :size="14" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="card sh-card-wide" v-else>
        <p class="empty">还没有搜索记录。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 顶部一句话：一行说完这一块能干什么，不解释做法 */
.sh { display: flex; flex-direction: column; gap: 12px; }
.panel-note {
  margin: 0; font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint);
}

/* 卡片网格：短卡两列并排，长内容整宽。间距只由 `.grid` 的 gap 负责，卡片不再加 margin。 */
.sh-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; }
.sh-card-wide { grid-column: 1 / -1; }

/* 短卡里的说明与按钮：一句一行，按钮跟在后面 */
.sh-hint { margin: 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim); }
.field + .sh-hint { margin-top: 10px; }
.sh-actions { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; }

/* 记录数与标题同一行，靠右 */
.sh-count { margin-left: auto; font-weight: 500; color: var(--text-faint); font-size: calc(11px * var(--ts, 1)); }

.w-mode { width: 92px; }
.w-scope { width: 220px; }
.w-hits { width: 64px; }
.w-time { width: 96px; }
.w-ops { width: 56px; }
select { max-width: 120px; }

/* 窄一档：卡片回落单列，短卡也竖着排 */
@media (max-width: 1100px) {
  .sh-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
