<script setup lang="ts">
/**
 * 首页：左侧栏 + 顶栏标签条那层外壳归 App.vue / TabBar.vue，这里只画**主区里的一页**。
 *
 * 版面自上而下（设计稿口径）：
 *   刊头（英文小标签 / 大标题 / 一行说明）
 *   → 2×2 等宽等高功能卡（阅读 / 论文解读 / 知识库 / 学习）
 *   → 一行三张小卡（笔记 / 设置 / AI 对话）
 *   → 底部三列（阅读统计 / 最近打开的论文 / 最近创建的笔记）
 *
 * 三条硬规则：
 * 1. **卡片不套全局 `.card`**（一律自有类名 `.fcard` / `.qcard` / `.rcard` / `.stats`）：
 *    base.css 里 `.card + .card { margin-top: 12px }` 是给"竖向堆叠"写的，只在 `.grid` / `.row`
 *    里被清零；自己起的网格容器不在名单里 → 每行第 2 张起会被顶下去 12px，表现就是"同排卡片没对齐"。
 * 2. **没有数据源的区块整块不渲染**（`v-if`），不留空壳、不写"暂无数据"大字：
 *    最近论文看 `state.papers`，最近笔记看 `GET /notes/overview`，统计看 `GET /home/summary`
 *    （在 ReadingStats 里，连请求一起）。
 * 3. 主区宽度由外壳决定且可能很窄：内容限宽 1320px 居中，所有网格列一律 `minmax(0, 1fr)`，
 *    长标题/长作者名不会把卡片撑破（省略号交给组件内部的 nowrap + ellipsis）。
 *
 * 导航（新外壳下导航页不进顶栏标签条）：
 *   导航页 = 论文库 / 知识库 / 学习 / 笔记 / 设置 / AI 对话 → `router.push({ name })`；
 *   「论文解读」是**按论文**的 → `openTab({ name: 'understand', params: { paperId } })`，
 *   一篇论文都没有时退回论文库。
 */
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import FeatureCard from '@/components/home/FeatureCard.vue';
import QuickCard from '@/components/home/QuickCard.vue';
import RecentList from '@/components/home/RecentList.vue';
import ReadingStats from '@/components/home/ReadingStats.vue';
import { api } from '@/api/client';
import { loadPapers, paperTitle, state } from '@/stores/app';
import { openTab } from '@/stores/tabs';
import type { Note, Paper } from '@/api/types';

const router = useRouter();

/** 四张功能卡：图标 / 文案 / 底部 CTA 全部与设计稿一致，首卡点一下主题色 */
const FACES = [
  { key: 'library', label: '阅读', icon: 'library', cta: '进入论文库 →', hint: '导入论文、解析文档、双语对照阅读。' },
  { key: 'understand', label: '论文解读', icon: 'insight', cta: '开始解读 →', hint: '梳理论文背景、动机、贡献、方法与实验结论。' },
  { key: 'knowledge', label: '知识库', icon: 'knowledge', cta: '查看知识库 →', hint: '跨论文搜索与问答，整理概念与公式。' },
  { key: 'study', label: '学习', icon: 'study', cta: '开始学习 →', hint: '整理可复习的概念与学习计划，巩固论文理解。' },
] as const;

const QUICK = [
  { key: 'notes', label: '笔记', icon: 'note', hint: '记录灵感与思考' },
  { key: 'settings', label: '设置', icon: 'settings', hint: '个性化配置与偏好' },
  { key: 'chat', label: 'AI 对话', icon: 'chat', hint: '跨论文问答，读取全部论文' },
] as const;

/* ------------------------------- 论文 ------------------------------- */
const papers = computed<Paper[]>(() => state.papers ?? []);
const recentRead = computed(() => papers.value
  .filter((p) => p.last_read_at)
  .sort((a, b) => String(b.last_read_at).localeCompare(String(a.last_read_at))));
const recentAdded = computed(() => [...papers.value]
  .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? ''))));

/** 与 RecentList 的 props 同形（那边是 scoped 组件内部类型，这里按结构对齐） */
type ListRow = { id: string; title: string; sub: string; time: string };

/* ------------------------------- 笔记 ------------------------------- */
/** `/notes/overview` 的笔记条目（带所属论文标题） */
type NoteRow = Note & { paper_title?: string };
const notes = ref<NoteRow[]>([]);

/* --------------------------- 相对时间 / 纯文本 ---------------------------
 * 两个列表都要"2 分钟前 / 1 小时前 / 昨天"，放在这里算一次、以字符串交给 RecentList：
 * 组件里不再碰时间，列表渲染就是纯静态的了。 */
function relTime(iso?: string | null): string {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '';
  const diff = Date.now() - t;
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  const days = Math.floor(diff / 86_400_000);
  if (days === 1) return '昨天';
  if (days < 7) return `${days} 天前`;
  const d = new Date(t);
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
}

/** 笔记正文是 Markdown：列表里那两行摘要得先去掉记号，否则满屏 ### 和 ** */
function plain(md?: string | null): string {
  return String(md ?? '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[#>*_~`|-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

const paperRows = computed<ListRow[]>(() => recentRead.value.slice(0, 4).map((p) => ({
  id: p.id,
  title: p.title || '（未命名论文）',
  sub: [p.authors, p.venue].filter(Boolean).join(' · '),
  time: relTime(p.last_read_at),
})));

const noteRows = computed<ListRow[]>(() => notes.value.slice(0, 4).map((n) => ({
  id: n.id,
  title: n.paper_title || paperTitle(n.paper_id) || '（未知论文）',
  sub: plain(n.content_md) || plain(n.selected_text) || '',
  time: relTime(n.created_at),
})));

onMounted(() => {
  if (!papers.value.length) void loadPapers({ page_size: 200 }).catch(() => {});
  // 笔记只是首页的一块，取不到就整张卡不渲染，不弹错
  void api.get<{ notes: NoteRow[] }>('/notes/overview')
    .then((d) => {
      notes.value = (d?.notes ?? [])
        .filter((n) => n.created_at)
        .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
    })
    .catch(() => { notes.value = []; });
});

/* ------------------------------- 导航 ------------------------------- */
function go(name: 'library' | 'knowledge' | 'study' | 'notes' | 'settings' | 'chat') {
  void router.push({ name });
}
function onFace(key: (typeof FACES)[number]['key']) {
  if (key === 'understand') { openUnderstand(); return; }
  go(key);
}
/** 「论文解读」按论文走：最近读过的优先，否则最近导入的；一篇都没有就回论文库 */
function openUnderstand() {
  const target = recentRead.value[0] ?? recentAdded.value[0] ?? null;
  if (!target) { go('library'); return; }
  void router.push(openTab({ name: 'understand', params: { paperId: target.id } }) as never);
}
/** 列表行点进去就是那篇论文的阅读标签（阅读器本来就是标签页） */
function openPaper(id: string) {
  void router.push(openTab({ name: 'reader', params: { paperId: id } }) as never);
}
/** 笔记没有单条路由，点行进笔记页 */
function openNote() {
  go('notes');
}
</script>

<template>
  <div class="home">
    <!-- ① 刊头 -->
    <header class="hero">
      <p class="eyebrow">Paper reading / Knowledge workspace</p>
      <h1>从论文阅读，到真正理解。</h1>
      <p class="sub">导入文献、双语阅读、深入解读，再把重要的知识整理下来。</p>
    </header>

    <!-- ② 2×2 功能卡：等宽（minmax(0,1fr)）+ 等高（grid-auto-rows: 1fr） -->
    <div class="features">
      <FeatureCard
        v-for="(f, i) in FACES" :key="f.key"
        :icon="f.icon" :label="f.label" :hint="f.hint" :cta="f.cta" :accent="i === 0"
        @open="onFace(f.key)"
      />
    </div>

    <!-- ③ 一行三张小卡 -->
    <div class="quicks">
      <QuickCard
        v-for="q in QUICK" :key="q.key"
        :icon="q.icon" :label="q.label" :hint="q.hint"
        @open="go(q.key)"
      />
    </div>

    <!-- ④ 底部三列：统计 / 最近论文 / 最近笔记（后两块没数据就整块不渲染） -->
    <div class="bottom">
      <ReadingStats />
      <RecentList
        v-if="paperRows.length"
        icon="document" title="最近打开的论文" more-label="全部"
        :rows="paperRows" row-icon="document"
        @select="openPaper" @more="go('library')"
      />
      <RecentList
        v-if="noteRows.length"
        icon="note" title="最近创建的笔记" more-label="全部"
        :rows="noteRows" row-icon="note" clamp-sub
        @select="openNote" @more="go('notes')"
      />
    </div>
  </div>
</template>

<style scoped>
/* 限宽 + 居中：首页被放进侧栏右侧的主区后，宽屏下也不该把内容拉成一条长线 */
.home { display: flex; flex-direction: column; gap: 16px; max-width: 1320px; margin: 0 auto; }

/* ── ① 刊头 ── */
.hero { padding-bottom: 10px; }
.eyebrow {
  margin: 0 0 12px; font-family: var(--mono);
  font-size: calc(11px * var(--ts, 1)); font-weight: 600;
  letter-spacing: .2em; text-transform: uppercase; color: var(--accent-text);
}
.hero h1 {
  margin: 0; font-size: calc(38px * var(--ts, 1)); font-weight: 600;
  line-height: 1.15; letter-spacing: -.03em; color: var(--text);
}
.hero .sub {
  margin: 12px 0 0; max-width: 34em;
  font-size: calc(14.5px * var(--ts, 1)); line-height: 1.7; color: var(--text-dim);
}

/* ── ② 功能卡：两列 + 等高（grid-auto-rows: 1fr 让两行彼此一样高） ── */
.features {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  grid-auto-rows: 1fr; gap: 16px;
}

/* ── ③ 小卡：一行三张 ── */
.quicks { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }

/* ── ④ 底部三列：统计放宽一档，两张列表等宽；stretch 让三张卡等高 ── */
.bottom {
  display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px; align-items: stretch;
}

/* 响应式：<1080 四张功能卡保持两列（收窄只能靠间距与内边距），<720 一律单列。
   底部三列在窄屏下逐级让位：先 2 列（统计占满一行），再单列。 */
@media (max-width: 1080px) {
  .features { gap: 14px; }
  .quicks { gap: 14px; }
  .bottom { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; }
  .bottom > :first-child { grid-column: 1 / -1; }
}
@media (max-width: 720px) {
  .home { gap: 14px; }
  .hero h1 { font-size: calc(30px * var(--ts, 1)); }
  .hero .sub { font-size: calc(14px * var(--ts, 1)); }
  .features, .quicks, .bottom { grid-template-columns: minmax(0, 1fr); }
  .bottom > :first-child { grid-column: auto; }
}
</style>
