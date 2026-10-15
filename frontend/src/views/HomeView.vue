<script setup lang="ts">
/**
 * 首页：应用外壳的落地页（顶栏改成标签条后，**导航就住在这里**）。
 *
 * 版面（用户口径 2026-10-15 第五轮：对齐 + 密度 + 设计完成度）：
 *   两栏刊头（标签一行，大标题在左、说明在右、底边对齐）→ 核心功能（7:5 / 5:7 不对称网格）
 *   → 研究工具（紧凑一行）→ 阅读统计（底部唯一的数据可视化）。
 *
 * 五条规矩：
 * 1. **不对称只来自网格尺寸**：四张卡的内部结构完全一样（左上图标容器、右上箭头、
 *    下方标题 + 描述），间距靠 CSS 的 `gap` 自然拉开 —— 不用 `margin-top:auto` 把文字
 *    顶到卡片底部（那会在卡片中间留一块没用的空白）；
 * 2. ⚠️ 首页的卡片**不复用全局 `.card`**（叫 `.bcard`）：base.css 里那条
 *    `.card + .card { margin-top: 12px }` 是给"竖向堆叠"写的，它的清零只覆盖
 *    `.grid` / `.row` 两种容器；`.bento` 不在名单里 → 每行第 2..4 张会被顶下去 12px，
 *    表现就是"标题/描述跟同排的卡不齐"（正是用户报的"阅读卡片没有对齐"）。
 *    自己起一个类名，跟那套全局规则彻底解耦；
 * 3. 一律白底 + 浅灰描边 + 极轻投影，层级靠**尺寸 / 间距 / 字号**表达，不靠彩色底；
 * 4. 图标只走 `<AppIcon>`（视图里不内联 svg），19px 线性图标装在 38px 浅灰容器里；
 * 5. 首页是**功能区跳转**：不放"最近读过的论文"这类列表（那是论文库的活儿）。
 *
 * 点一下都是**在新标签页**里打开（走 `openTab`），首页本身不动。
 */
import { computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import ReadingStats from '@/components/home/ReadingStats.vue';
import { loadPapers, state } from '@/stores/app';
import { openTab } from '@/stores/tabs';
import type { Paper } from '@/api/types';

const router = useRouter();

/** 四个面（改造后的主结构）—— 顺序就是流水线顺序：读 → 懂 → 查 → 练 */
const FACES = [
  { name: 'library', label: '阅读', icon: 'library', span: 7, key: true, hint: '导入论文、解析文档、双语对照阅读。' },
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
  .sort((a, b) => String(b.last_read_at).localeCompare(String(a.last_read_at))));
const recentAdded = computed(() => [...papers.value]
  .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? ''))));
/** 「论文解读」是**按论文**的：首页不列清单，但要挑一篇最该看的（最近读过的 → 否则最近导入的） */
const firstToUnderstand = computed(() => (recentRead.value[0] ?? recentAdded.value[0] ?? null));

onMounted(() => {
  if (!papers.value.length && !state.loading.papers) void loadPapers({ limit: 200 }).catch(() => {});
});

async function openRoute(name: string) {
  // 理解页是 `/understand/:paperId`：没有论文可挑时直接回论文库
  if (name === 'understand') {
    const target = firstToUnderstand.value;
    if (!target) return openRoute('library');
    return openUnderstand(target.id);
  }
  await router.push(openTab({ name }) as never);
}
async function openUnderstand(id: string) {
  await router.push(openTab({ name: 'understand', params: { paperId: id } }) as never);
}
</script>

<template>
  <div class="home">
    <!-- ① 刊头：英文小标签一行，大标题在左、说明在右（底边对齐） -->
    <header class="hero">
      <p class="eyebrow">Paper reading / Knowledge workspace</p>
      <h1>从论文阅读，到真正理解。</h1>
      <p class="sub">导入文献、双语阅读、深入解读，再把重要的知识整理下来。</p>
    </header>

    <!-- ② 四个核心功能：不对称来自网格，卡片内部一律同一套结构 -->
    <section class="block" aria-labelledby="core-title">
      <h2 id="core-title" class="sec">核心功能</h2>
      <div class="bento">
        <button
          v-for="n in FACES" :key="n.name" type="button"
          class="bcard" :class="`sp${n.span}`"
          @click="openRoute(n.name)"
        >
          <span class="b-top">
            <span class="ico-box" :class="{ key: n.key }"><AppIcon :name="n.icon" :size="19" /></span>
            <AppIcon name="arrow-ne" :size="15" class="go" />
          </span>
          <span class="b-text">
            <span class="b-title">{{ n.label }}</span>
            <span class="b-desc">{{ n.hint }}</span>
          </span>
        </button>
      </div>
    </section>

    <!-- ③ 研究工具：比核心功能轻一档（小一号的图标容器、没有投影） -->
    <section class="block" aria-labelledby="tools-title">
      <h2 id="tools-title" class="sec">研究工具</h2>
      <div class="tools">
        <button v-for="t in TOOLS" :key="t.name" type="button" class="tool" @click="openRoute(t.name)">
          <span class="ico-box sm"><AppIcon :name="t.icon" :size="17" /></span>
          <span class="tool-text">
            <span class="tool-name">{{ t.label }}</span>
            <span class="tool-hint">{{ t.hint }}</span>
          </span>
        </button>
      </div>
    </section>

    <!-- ④ 阅读统计：本页唯一的数据可视化（真实数据见 ReadingStats） -->
    <ReadingStats />
  </div>
</template>

<style scoped>
/* ── 间距体系（全页只用这几档）─────────────────────────────────────────
   页面左右由 `.app-main` 给；这里只定内容宽度与纵向节奏：
   模块间距 30 · 卡片间距 14 · 卡片内边距 20 · 图标与文字 18 · 标题与描述 6。
   ⚠️ 圆角用 `--radius-sm`(14px)：Linear 式的紧凑卡片语言，与全站 22px 的区别是有意的。 */
.home { max-width: 1320px; margin: 0 auto; }

/* ── ① 刊头 ──
   两栏**编辑式**排布：标签占满一行，大标题在左、说明在右，两者**底边对齐**。
   一栏放不下时说明会掉到标题下面 —— 既不会在右上角留一块空地，也不用造新组件去填。 */
.hero {
  display: grid; grid-template-columns: minmax(0, 1.02fr) minmax(300px, 1fr);
  align-items: end; column-gap: 44px; row-gap: 10px; padding: 14px 0 26px;
}
.eyebrow {
  grid-column: 1 / -1; margin: 0; font-size: 10.5px; font-weight: 700; letter-spacing: .18em;
  text-transform: uppercase; color: var(--text-faint);
}
.hero h1 {
  margin: 0; font-size: clamp(26px, 2.4vw, 32px); font-weight: 600;
  letter-spacing: -.03em; line-height: 1.3; color: var(--text);
}
.hero .sub { margin: 0 0 3px; font-size: 14px; line-height: 1.75; color: var(--text-dim); }

/* ── 模块抬头：核心功能 / 研究工具 用同一种（分区标签，不是装饰性编号）── */
.block { margin-top: 30px; }
.sec { margin: 0 0 12px; font-size: 12.5px; font-weight: 650; letter-spacing: .01em; color: var(--text-dim); }

/* ── ② 便当格 ──
   ⚠️ 容器名不是 `.grid`：这一块**不吃**全局 `.card` / `.card + .card` 那套规则（见文件头注释）。 */
.bento { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 14px; align-items: stretch; }
.sp7 { grid-column: span 7; }
.sp5 { grid-column: span 5; }

.bcard {
  display: flex; flex-direction: column; gap: 18px; width: 100%; height: 100%;
  min-height: 152px; padding: 20px; margin: 0;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elev);
  box-shadow: var(--shadow-1); color: var(--text); font: inherit; text-align: left;
  cursor: pointer; appearance: none;
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease),
              transform var(--dur) var(--ease);
}
/* `height: 100%` 是必须的：<button> 在网格里**不吃**默认的 `align-items: stretch`
   （表单控件的高度由内容定），不写这一条，同一行里"描述长的那张"会比邻座高一截。 */
.bcard:hover { border-color: var(--border-strong); box-shadow: var(--shadow-2); transform: translateY(-2px); }
.bcard:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }

.b-top { display: flex; align-items: center; justify-content: space-between; }
/* 图标容器：38px 浅灰方片，图标在其中**正中**（grid + place-items）——
   比"裸图标贴在角上"更稳，也不至于像一块彩色色块抢戏。 */
.ico-box {
  display: grid; place-items: center; flex: none; width: 38px; height: 38px;
  border-radius: 11px; background: var(--bg-sunken); color: var(--text-dim);
  transition: color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.ico-box.key { background: var(--accent-soft); color: var(--accent-text); }
.bcard:hover .ico-box { color: var(--accent); }
.bcard:hover .ico-box.key { color: var(--accent-text); }
/* 箭头只留**右上角**这一处（左下角不再重复一枚跳转符号） */
.go {
  flex: none; color: var(--text-faint);
  transition: color var(--dur) var(--ease), transform var(--dur) var(--ease);
}
.bcard:hover .go { color: var(--accent); transform: translate(2px, -2px); }

.b-text { display: flex; flex-direction: column; gap: 6px; }
.b-title { font-size: 19px; font-weight: 620; letter-spacing: -.025em; }
.b-desc { max-width: 34em; font-size: 13.5px; line-height: 1.7; color: var(--text-dim); }

/* ── ③ 研究工具 ── */
.tools { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.tool {
  display: flex; align-items: center; gap: 11px; min-height: 62px; padding: 12px 13px;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elev);
  color: var(--text); font: inherit; text-align: left; cursor: pointer; appearance: none;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.tool:hover { border-color: var(--border-strong); background: var(--bg-hover); }
.tool:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.ico-box.sm { width: 32px; height: 32px; border-radius: 9px; }
.tool:hover .ico-box { color: var(--accent); }
.tool-text { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.tool-name { font-size: 12.5px; font-weight: 600; letter-spacing: -.01em; }
.tool-hint { font-size: 11px; color: var(--text-faint); }

/* ── 响应式：先收成两列，再收成一列（内边距同步变小） ── */
@media (max-width: 1080px) {
  .hero { grid-template-columns: minmax(0, 1fr); row-gap: 8px; padding: 12px 0 22px; }
  .eyebrow { margin-bottom: 4px; }
  .hero .sub { margin-top: 2px; }
  .sp7, .sp5 { grid-column: span 6; }
  .bcard { min-height: 142px; padding: 18px; gap: 16px; }
  .tools { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 640px) {
  .block { margin-top: 24px; }
  .sp7, .sp5 { grid-column: span 12; }
  .bcard { min-height: 132px; padding: 16px; gap: 14px; }
  .b-title { font-size: 18px; }
  .b-desc { font-size: 13px; }
}
@media (max-width: 460px) {
  .tools { grid-template-columns: minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  .bcard, .bcard:hover { transform: none; }
  .go, .bcard:hover .go { transform: none; }
}
</style>
