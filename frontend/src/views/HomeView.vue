<script setup lang="ts">
/**
 * 首页：应用外壳的落地页（顶栏改成标签条后，**导航就住在这里**）。
 *
 * 版面（用户口径 2026-10-15 第六轮：重构，走「极简柔光」方向 + 首页专属的"封面语言"）：
 *   刊头（英文小标签 / display 级大标题 / 说明 → 右侧一条真实数据 rail，发丝线分隔）
 *   → 核心功能（12 栏网格：5×2 的主角卡「阅读」+ 7 / 4 / 3 三张错落小卡）
 *   → 研究工具（一行四枚 pill，权重明显轻一档）
 *   → 阅读统计（底部唯一的数据可视化，见 ReadingStats）
 *
 * 六条规矩：
 * 1. **不对称只来自网格尺寸**：四张卡的内部结构完全一样（左上图标容器 / 右上箭头 / 下方
 *    标题 + 描述），间距靠 CSS 的 `gap` 自然拉开 —— 不用 `margin-top:auto` 把文字顶到卡片
 *    底部（那会在卡片中间留一块没用的空白）；
 * 2. ⚠️ 首页的卡片**不复用全局 `.card`**（叫 `.bcard`）：base.css 里那条
 *    `.card + .card { margin-top: 12px }` 是给"竖向堆叠"写的，它的清零只覆盖
 *    `.grid` / `.row` 两种容器；`.bento` 不在名单里 → 每行第 2..4 张会被顶下去 12px，
 *    表现就是"标题/描述跟同排的卡不齐"（用户报的"阅读卡片没有对齐"）。
 *    自己起一个类名，跟那套全局规则彻底解耦；
 * 3. 一律白底 + 浅灰描边 + 极轻投影，层级靠**尺寸 / 间距 / 字号**表达，不靠彩色底；
 * 4. 图标只走 `<AppIcon>`（视图里不内联 svg），19px 线性图标装在 38px 浅灰容器里；
 * 5. 首页是**功能区跳转**：不放"最近读过的论文"这类列表（那是论文库的活儿）；
 * 6. "封面语言"只在本页生效：大字号刊头、发丝分隔线、主角卡里那片极淡的点阵 ——
 *    颜色 / 圆角 / 投影 / 强调色**一律仍走全站 token**，所以换皮肤与深色模式都跟着走。
 *
 * 数据 rail 的数字全部来自真实接口（`/home/summary` + 已加载的论文数），拿不到就**整条 rail
 * 消失**、刊头收成单栏 —— 不摆占位符、不编数字。
 *
 * 点一下都是**在新标签页**里打开（走 `openTab`），首页本身不动。
 */
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import ReadingStats from '@/components/home/ReadingStats.vue';
import { api } from '@/api/client';
import { loadPapers, state } from '@/stores/app';
import { openTab } from '@/stores/tabs';
import type { HomeSummary, Paper } from '@/api/types';

const router = useRouter();

/** 四个面（流水线顺序：读 → 懂 → 查 → 练）。
 *  `span` 是 12 栏网格里的宽度：主角卡 5 栏占两行，其余三张 7 / 4 / 3 拼成右边那一列。 */
const FACES = [
  { name: 'library', label: '阅读', icon: 'library', span: 5, lead: true, hint: '导入论文、解析文档、双语对照阅读。' },
  { name: 'understand', label: '论文解读', icon: 'insight', span: 7, lead: false, hint: '梳理论文背景、动机、贡献、方法与实验结论。' },
  { name: 'knowledge', label: '知识库', icon: 'knowledge', span: 4, lead: false, hint: '跨论文搜索与问答，整理概念与公式。' },
  { name: 'study', label: '学习', icon: 'study', span: 3, lead: false, hint: '整理可复习的概念与学习计划。' },
];

/** 研究工具：信息密度降一档 —— 名称已经能说明问题，提示语进 `title`，不再占一行字 */
const TOOLS = [
  { name: 'notes', label: '笔记', icon: 'note', hint: '批注、笔记与高亮' },
  { name: 'models', label: '模型', icon: 'model', hint: '生成 / 向量 / 视觉模型' },
  { name: 'tasks', label: '任务', icon: 'task', hint: '队列、进度与重试' },
  { name: 'settings', label: '设置', icon: 'settings', hint: '参数、索引与搜索记录' },
];

const papers = computed<Paper[]>(() => state.papers ?? []);
/** 论文总数：列表接口给的 `total` 才算数（`papers` 只是加载出来的那一页） */
const paperCount = computed(() => state.papersTotal || papers.value.length);

const recentRead = computed(() => papers.value
  .filter((p) => p.last_read_at)
  .sort((a, b) => String(b.last_read_at).localeCompare(String(a.last_read_at))));
const recentAdded = computed(() => [...papers.value]
  .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? ''))));
/** 「论文解读」是**按论文**的：首页不列清单，但要挑一篇最该看的（最近读过的 → 否则最近导入的） */
const firstToUnderstand = computed(() => (recentRead.value[0] ?? recentAdded.value[0] ?? null));

/** 刊头右侧的数据 rail：拿不到就空着（模板里 `v-if` 掉，刊头收单栏） */
const summary = ref<HomeSummary | null>(null);
const rail = computed<{ k: string; v: string }[]>(() => {
  const out: { k: string; v: string }[] = [];
  if (paperCount.value > 0) out.push({ k: '文献', v: String(paperCount.value) });
  if (summary.value) {
    out.push({ k: '已解读', v: String(summary.value.understandings) });
    out.push({ k: '笔记', v: String(summary.value.notes) });
  }
  return out;
});

onMounted(() => {
  if (!papers.value.length && !state.loading.papers) void loadPapers({ limit: 200 }).catch(() => {});
  // 只读概览：失败就当没有（首页不该因为一个统计数字弹错）
  void api.get<HomeSummary>('/home/summary')
    .then((d) => { summary.value = d ?? null; })
    .catch(() => { summary.value = null; });
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
    <!-- ① 刊头：英文小标签一行，大标题在左、真实数据在右（右侧拿不到数据就整条消失） -->
    <header class="hero" :class="{ solo: !rail.length }">
      <div class="hero-l">
        <p class="eyebrow">Paper reading / Knowledge workspace</p>
        <h1>从论文阅读，到真正理解。</h1>
        <p class="sub">导入文献、双语阅读、深入解读，再把重要的知识整理下来。</p>
      </div>
      <dl v-if="rail.length" class="rail">
        <div v-for="r in rail" :key="r.k" class="rail-row">
          <dt>{{ r.k }}</dt>
          <dd>{{ r.v }}</dd>
        </div>
      </dl>
    </header>

    <!-- ② 四个核心功能：不对称来自网格，卡片内部一律同一套结构 -->
    <section class="block" aria-labelledby="core-title">
      <h2 id="core-title" class="sec">核心功能</h2>
      <div class="bento">
        <button
          v-for="n in FACES" :key="n.name" type="button"
          class="bcard" :class="[`sp${n.span}`, { lead: n.lead }]"
          @click="openRoute(n.name)"
        >
          <span class="b-top">
            <span class="ico-box" :class="{ key: n.lead }"><AppIcon :name="n.icon" :size="19" /></span>
            <AppIcon name="arrow-ne" :size="15" class="go" />
          </span>
          <!-- 主角卡给一枚**真实**的论文数（拿不到就整块不出现，卡片自然收成普通卡的高度） -->
          <span v-if="n.lead && paperCount > 0" class="b-stat">
            {{ paperCount }}<em>篇文献</em>
          </span>
          <span class="b-text">
            <span class="b-title">{{ n.label }}</span>
            <span class="b-desc">{{ n.hint }}</span>
          </span>
          <span v-if="n.lead" class="dots" aria-hidden="true" />
        </button>
      </div>
    </section>

    <!-- ③ 研究工具：比核心功能轻一档（一行 pill、没有投影） -->
    <section class="block" aria-labelledby="tools-title">
      <h2 id="tools-title" class="sec">研究工具</h2>
      <div class="tools">
        <button
          v-for="t in TOOLS" :key="t.name" type="button"
          class="tool" :title="t.hint" @click="openRoute(t.name)"
        >
          <span class="ico-box sm"><AppIcon :name="t.icon" :size="17" /></span>
          <span class="tool-name">{{ t.label }}</span>
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
   模块间距 34 · 卡片间距 14 · 卡片内边距 20 · 图标与文字 16 · 标题与描述 6。
   ⚠️ 圆角用 `--radius-sm`(14px)：Linear 式的紧凑卡片语言，与全站 22px 的区别是有意的。 */
.home { max-width: 1320px; margin: 0 auto; }

/* ── ① 刊头 ──
   两栏**编辑式**排布：大标题在左、数据 rail 在右，两者**底边对齐**；下面一条发丝线收口。
   rail 拿不到数据（或窄屏）时收成单栏 —— 不会在右上角留一块空地。 */
.hero {
  display: grid; grid-template-columns: minmax(0, 1fr) 288px;
  align-items: end; column-gap: 56px; row-gap: 18px;
  padding: 8px 0 30px; border-bottom: 1px solid var(--divider);
}
.hero.solo { grid-template-columns: minmax(0, 1fr); }
.eyebrow {
  margin: 0 0 14px; font-family: var(--mono);
  font-size: 10.5px; font-weight: 600; letter-spacing: .2em;
  text-transform: uppercase; color: var(--accent-text);
}
.hero h1 {
  margin: 0; font-size: clamp(30px, 3.2vw, 46px); font-weight: 600;
  line-height: 1.1; letter-spacing: -.038em; color: var(--text);
}
.hero .sub { margin: 16px 0 0; max-width: 30em; font-size: 14.5px; line-height: 1.7; color: var(--text-dim); }

.rail { margin: 0; }
.rail-row {
  display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
  padding: 10px 0; border-bottom: 1px solid var(--divider);
}
.rail-row:first-child { border-top: 1px solid var(--divider); }
.rail dt { font-size: 12.5px; color: var(--text-faint); }
.rail dd { margin: 0; font-size: 18px; font-weight: 620; letter-spacing: -.02em; color: var(--text); }

/* ── 模块抬头：一条发丝线 + 一档小字，全页只有这一种 ── */
.block { margin-top: 34px; }
.sec {
  margin: 0 0 14px; padding-top: 12px;
  border-top: 1px solid var(--divider);
  font-size: 12.5px; font-weight: 650; letter-spacing: .01em; color: var(--text-dim);
}

/* ── ② 便当格 ──
   ⚠️ 容器名不是 `.grid`：这一块**不吃**全局 `.card` / `.card + .card` 那套规则（见文件头注释）。 */
.bento { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); grid-auto-rows: minmax(130px, auto); gap: 14px; align-items: stretch; }
.sp3 { grid-column: span 3; }
.sp4 { grid-column: span 4; }
.sp5 { grid-column: span 5; }
.sp7 { grid-column: span 7; }

.bcard {
  position: relative; display: flex; flex-direction: column; justify-content: space-between;
  gap: 16px; width: 100%; height: 100%; padding: 20px; margin: 0; overflow: hidden;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elev);
  box-shadow: var(--shadow-1); color: var(--text); font: inherit; text-align: left;
  cursor: pointer; appearance: none;
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease),
              transform var(--dur) var(--ease);
}
/* 主角卡：占两行、得到一枚真实数字与一片点阵（"向量/语料"的极简暗示，不做插画） */
.bcard.lead { grid-row: span 2; background: linear-gradient(160deg, var(--bg-elev), var(--bg-sunken)); }
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

.b-stat { font-size: 40px; font-weight: 600; line-height: 1; letter-spacing: -.045em; color: var(--text); }
.b-stat em { margin-left: 7px; font-size: 12.5px; font-weight: 400; font-style: normal; letter-spacing: 0; color: var(--text-faint); }
/* 点阵：右下角一小片极淡的圆点，给主角卡一点"数据"的质感（纯装饰，不参与命中） */
.dots {
  position: absolute; right: 20px; bottom: 18px; width: 120px; height: 60px; pointer-events: none;
  background-image: radial-gradient(color-mix(in srgb, var(--accent) 32%, transparent) 1.2px, transparent 1.2px);
  background-size: 13px 13px;
}

.b-text { display: flex; flex-direction: column; gap: 6px; }
.b-title { font-size: 19px; font-weight: 620; letter-spacing: -.025em; }
.b-desc { max-width: 34em; font-size: 13.5px; line-height: 1.7; color: var(--text-dim); }

/* ── ③ 研究工具：一行 pill ── */
.tools { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.tool {
  display: flex; align-items: center; gap: 10px; padding: 10px 14px;
  border: 1px solid var(--border); border-radius: var(--radius-ctl); background: var(--bg-elev);
  color: var(--text); font: inherit; text-align: left; cursor: pointer; appearance: none;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.tool:hover { border-color: var(--border-strong); background: var(--bg-hover); }
.tool:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.ico-box.sm { width: 26px; height: 26px; border-radius: 999px; }
.tool:hover .ico-box { color: var(--accent); }
.tool-name { font-size: 12.5px; font-weight: 600; letter-spacing: -.01em; }

/* ── 响应式：先收成两列，再收成一列（主角卡跨行只在宽屏有意义） ── */
@media (max-width: 1080px) {
  .hero { grid-template-columns: minmax(0, 1fr); row-gap: 14px; padding-bottom: 24px; }
  .bento { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .sp3, .sp4, .sp5, .sp7 { grid-column: auto; }
  .tools { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .dots { display: none; }
}
@media (max-width: 640px) {
  .block { margin-top: 26px; }
  .bento { grid-template-columns: minmax(0, 1fr); }
  .bcard.lead { grid-row: auto; }
  .bcard { padding: 18px; gap: 14px; }
  .b-stat { font-size: 34px; }
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
