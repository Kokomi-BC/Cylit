<script setup lang="ts">
/**
 * 首页：应用外壳的落地页（顶栏改成标签条后，**导航就住在这里**）。
 *
 * 版面（用户口径 2026-10-15 第三轮：macOS / Linear 式的工具界面 + 不对称便当格）：
 *   纯文字刊头 → 四个核心功能（7:5 / 5:7 的不对称网格）→ 接着上次 → 研究工具 → 阅读统计。
 *
 * 三条规矩：
 * 1. **不对称只来自网格尺寸**：四张卡的内部结构完全一样（左上图标、右上箭头、下方标题 + 描述、
 *    统一内边距与字号），不做"元素各飞各的"那种不规则；
 * 2. 强调色仍然只有一支：卡片一律白底 + 极浅描边，图标/箭头在 hover 时才转成 `--accent`；
 * 3. 图标一律走 `<AppIcon>`（视图里不内联 svg），20px 线性图标 —— 图标负责识别，不当装饰。
 *
 * 点一下都是**在新标签页**里打开（走 `openTab`），首页本身不动。
 */
import { computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import ReadingStats from '@/components/home/ReadingStats.vue';
import { loadPapers, state } from '@/stores/app';
import { openTab } from '@/stores/tabs';
import { fmtRelative } from '@/utils/format';
import type { Paper } from '@/api/types';

const router = useRouter();

/** 四个面（改造后的主结构）—— 顺序就是流水线顺序：读 → 懂 → 查 → 练 */
const FACES = [
  { name: 'library', label: '阅读', icon: 'library', span: 7, hint: '导入论文、解析文档、双语对照阅读。' },
  { name: 'understand', label: '论文解读', icon: 'sparkle', span: 5, hint: '梳理论文背景、动机、贡献、方法与实验结论。' },
  { name: 'knowledge', label: '知识库', icon: 'knowledge', span: 5, hint: '跨论文搜索与问答，整理概念、公式和知识依据。' },
  { name: 'study', label: '学习', icon: 'task', span: 7, hint: '整理可复习的概念与学习计划，巩固论文理解。' },
];

const TOOLS = [
  { name: 'notes', label: '笔记', icon: 'note', hint: '批注、笔记与高亮' },
  { name: 'models', label: '模型', icon: 'model', hint: '生成 / 向量 / 视觉模型' },
  { name: 'tasks', label: '任务', icon: 'task', hint: '队列、进度与重试' },
  { name: 'settings', label: '设置', icon: 'settings', hint: '参数、索引与搜索记录' },
];

const papers = computed<Paper[]>(() => state.papers ?? []);
const recentRead = computed(() => papers.value
  .filter((p) => p.last_read_at)
  .sort((a, b) => String(b.last_read_at).localeCompare(String(a.last_read_at)))
  .slice(0, 4));
const recentAdded = computed(() => [...papers.value]
  .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')))
  .slice(0, 4));
/** 理解/学习是**按论文**的：首页给出的入口要挑一篇，所以列最近读过的几篇 */
const forUnderstand = computed(() => (recentRead.value.length ? recentRead.value : recentAdded.value));

onMounted(() => {
  if (!papers.value.length && !state.loading.papers) void loadPapers({ limit: 200 }).catch(() => {});
});

async function openRoute(name: string) {
  // 理解页是 `/understand/:paperId`：没有论文可挑时直接回论文库
  if (name === 'understand') {
    const target = forUnderstand.value[0];
    if (!target) return openRoute('library');
    return openUnderstand(target.id);
  }
  await router.push(openTab({ name }) as never);
}
async function openPaper(id: string) {
  await router.push(openTab({ name: 'reader', params: { paperId: id } }) as never);
}
async function openUnderstand(id: string) {
  await router.push(openTab({ name: 'understand', params: { paperId: id } }) as never);
}
</script>

<template>
  <div class="home">
    <!-- ① 刊头：只有两行字（不放装饰图、不放个人标签） -->
    <header class="hero">
      <h1>从论文阅读，到真正理解。</h1>
      <p>导入文献、双语阅读、深入解读，再把重要的知识整理下来。</p>
    </header>

    <!-- ② 四个核心功能：不对称来自网格，卡片内部一律同一套结构 -->
    <div class="bento">
      <button
        v-for="n in FACES" :key="n.name" type="button"
        class="card" :class="`sp${n.span}`"
        @click="openRoute(n.name)"
      >
        <span class="card-top">
          <AppIcon :name="n.icon" :size="20" class="card-ico" />
          <AppIcon name="arrow-ne" :size="16" class="card-go" />
        </span>
        <span class="card-text">
          <span class="card-title">{{ n.label }}</span>
          <span class="card-desc">{{ n.hint }}</span>
        </span>
      </button>
    </div>

    <!-- ③ 接着上次：真实数据（最近读过的 / 能去理解的），做成一张安静的宽卡 -->
    <section class="card wide">
      <div class="wide-head">
        <h2>接着上次</h2>
        <span class="spacer" />
        <button type="button" class="btn sm ghost" @click="openRoute('library')">全部论文</button>
        <button type="button" class="btn sm ghost" @click="openRoute('study')">学习页</button>
      </div>
      <div class="cols">
        <div class="col">
          <p class="col-title">最近读过</p>
          <p v-if="!recentRead.length" class="col-empty">还没有阅读记录 —— 先去论文库导入一篇。</p>
          <button
            v-for="p in recentRead" :key="p.id" type="button"
            class="row" @click="openPaper(p.id)"
          >
            <span class="truncate">{{ p.title || p.id }}</span>
            <span class="spacer" />
            <span class="row-meta">{{ fmtRelative(p.last_read_at) }}</span>
          </button>
        </div>
        <div class="col">
          <p class="col-title">可去理解</p>
          <p v-if="!forUnderstand.length" class="col-empty">还没有可理解的论文。</p>
          <button
            v-for="p in forUnderstand" :key="`u-${p.id}`" type="button"
            class="row" @click="openUnderstand(p.id)"
          >
            <span class="truncate">{{ p.title || p.id }}</span>
            <span class="spacer" />
            <span class="row-meta">解读</span>
            <AppIcon name="chevron-right" :size="13" class="row-go" />
          </button>
        </div>
      </div>
    </section>

    <!-- ④ 研究工具：视觉权重低于核心功能（更小的图标与字号、更紧凑） -->
    <h2 class="sec">研究工具</h2>
    <div class="tools">
      <button v-for="t in TOOLS" :key="t.name" type="button" class="tool" @click="openRoute(t.name)">
        <AppIcon :name="t.icon" :size="18" class="tool-ico" />
        <span class="tool-text">
          <span class="tool-name">{{ t.label }}</span>
          <span class="tool-hint">{{ t.hint }}</span>
        </span>
      </button>
    </div>

    <!-- ⑤ 阅读统计：真实数据（本机按天的阅读时长），图表见 ReadingStats 组件 -->
    <ReadingStats />
  </div>
</template>

<style scoped>
/* `.app-main` 已给最大宽度与内边距，这里只收窄到"读起来舒服"的一列。
   ⚠️ 圆角用 `--radius-sm`（14px）：首页这套是 Linear 式的紧凑卡片语言，
   与全站 `.card`（22px）的区别是有意的，改回来只需换这一个变量。 */
.home { max-width: 1100px; margin: 0 auto; }

/* ── ① 刊头 ── */
.hero { padding: 22px 0 30px; }
.hero h1 {
  margin: 0; font-size: clamp(25px, 2.9vw, 33px); font-weight: 600;
  letter-spacing: -.035em; line-height: 1.3; color: var(--text);
}
.hero p { margin: 12px 0 0; max-width: 46em; font-size: 13.5px; line-height: 1.8; color: var(--text-dim); }

/* ── ② 便当格 ── */
.bento { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 14px; }
.sp7 { grid-column: span 7; }
.sp5 { grid-column: span 5; }

.card {
  display: flex; flex-direction: column; min-height: 168px; padding: 20px;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elev);
  box-shadow: var(--shadow-1); color: var(--text); font: inherit; text-align: left;
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease),
              transform var(--dur) var(--ease);
}
/* ⚠️ height: 100% 是必须的：<button> 在网格里**不吃**默认的 align-items: stretch
   （表单控件的高度由内容定），不写这一条，同一行里"描述长的那张"会比邻座高一截。 */
button.card { cursor: pointer; appearance: none; width: 100%; height: 100%; }
.card:hover { border-color: var(--border-strong); box-shadow: var(--shadow-2); transform: translateY(-2px); }
.card:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }

/* 卡片内部只有两行结构：上面"图标 + 箭头"，下面"标题 + 描述"（四张卡一模一样） */
.card-top { display: flex; align-items: center; justify-content: space-between; }
.card-ico { color: var(--text-dim); transition: color var(--dur) var(--ease); }
.card:hover .card-ico { color: var(--accent); }
/* 箭头只用**一处**（右上角），左下角不再重复一枚跳转符号 */
.card-go {
  color: var(--text-faint); flex: none;
  transition: color var(--dur) var(--ease), transform var(--dur) var(--ease);
}
.card:hover .card-go { color: var(--accent); transform: translate(2px, -2px); }
.card-text { display: flex; flex-direction: column; gap: 7px; margin-top: auto; padding-top: 26px; }
.card-title { font-size: 15.5px; font-weight: 650; letter-spacing: -.025em; }
.card-desc { max-width: 32em; font-size: 12.5px; line-height: 1.72; color: var(--text-dim); }

/* ── ③ 接着上次（宽卡，非按钮：里面每一行才是可点的） ── */
.card.wide { min-height: 0; margin-top: 14px; cursor: default; }
.card.wide:hover { transform: none; border-color: var(--border); box-shadow: var(--shadow-1); }
.wide-head { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.wide-head h2 { margin: 0; font-size: 13px; font-weight: 650; letter-spacing: -.02em; }
.cols { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 26px; }
.col { min-width: 0; }
.col-title { margin: 0 0 4px; font-size: 11px; color: var(--text-faint); }
.col-empty { margin: 6px 0; font-size: 12px; color: var(--text-faint); }
.row {
  display: flex; align-items: center; gap: 8px; width: 100%; padding: 6px 8px;
  border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text);
  font: inherit; font-size: 12.5px; text-align: left; cursor: pointer;
}
.row:hover { background: var(--bg-hover); }
.row-meta { flex: none; font-size: 11px; color: var(--text-faint); }
.row-go { flex: none; color: var(--text-faint); }

/* ── ④ 研究工具 ── */
.sec { margin: 26px 0 11px; font-size: 12.5px; font-weight: 650; letter-spacing: -.02em; color: var(--text-dim); }
.tools { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.tool {
  display: flex; align-items: center; gap: 11px; padding: 13px 14px;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elev);
  color: var(--text); font: inherit; text-align: left; cursor: pointer; appearance: none;
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.tool:hover { border-color: var(--border-strong); box-shadow: var(--shadow-1); }
.tool:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.tool-ico { flex: none; color: var(--text-dim); transition: color var(--dur) var(--ease); }
.tool:hover .tool-ico { color: var(--accent); }
.tool-text { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.tool-name { font-size: 13px; font-weight: 600; letter-spacing: -.015em; }
.tool-hint { font-size: 11px; color: var(--text-faint); }

/* ── 响应式：中等屏收成两列，窄屏收成一列（内边距同步变小） ── */
@media (max-width: 980px) {
  .hero { padding: 16px 0 24px; }
  .sp7, .sp5 { grid-column: span 6; }
  .card { min-height: 156px; padding: 18px; }
  .card-text { padding-top: 20px; }
  .tools { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 620px) {
  .sp7, .sp5 { grid-column: span 12; }
  .card { min-height: 132px; padding: 16px; }
  .card-text { padding-top: 16px; }
  .cols { grid-template-columns: minmax(0, 1fr); gap: 4px; }
  .wide-head { flex-wrap: wrap; }
  .sec { margin-top: 22px; }
}
@media (max-width: 460px) {
  .tools { grid-template-columns: minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  .card, .card:hover { transform: none; }
  .card-go, .card:hover .card-go { transform: none; }
}
</style>
