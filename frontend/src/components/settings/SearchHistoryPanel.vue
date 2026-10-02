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
  <section class="card">
    <div class="card-head">
      <AppIcon name="search" :size="15" />搜索记录
      <span class="badge" v-if="entries.length">{{ entries.length }} 条</span>
      <span class="spacer"></span>
      <label class="tiny faint row nowrap">
        最多保留
        <select v-model.number="limit" @change="onLimitChange">
          <option v-for="n in LIMIT_CHOICES" :key="n" :value="n">{{ n }} 条</option>
        </select>
      </label>
      <button class="btn sm" :disabled="!entries.length" @click="onClear">
        <AppIcon name="trash" :size="14" />清空
      </button>
    </div>
    <div class="card-body tight">
      <table class="data fixed" v-if="entries.length">
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
      <div class="empty" v-else>
        还没有搜索记录 —— 在「知识库」页检索一次就会记在这里。
      </div>
    </div>
    <div class="card-body note">
      <span class="tiny faint">
        记录只存在<strong>本机浏览器</strong>（localStorage），不进数据库：换浏览器、换设备或清理浏览器数据都会一起消失。
      </span>
    </div>
  </section>
</template>

<style scoped>
.w-mode { width: 92px; }
.w-scope { width: 220px; }
.w-hits { width: 64px; }
.w-time { width: 96px; }
.w-ops { width: 56px; }
/* 分隔线用卡片自己的 divider，上面那块 body 已经带了 8px 内边距 */
.note { padding: 8px 16px 12px; }
.card-head select { max-width: 120px; }
</style>
