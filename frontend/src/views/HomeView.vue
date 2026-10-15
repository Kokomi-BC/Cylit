<script setup lang="ts">
/**
 * 首页：应用外壳的落地页（顶栏改成标签条后，**导航就住在这里**）。
 *
 * 版面（用户口径 2026-10-15 第二轮："要更有设计感"，参照便当盒方向）：
 *   hero（刊头 + 文案 + 一块反色装饰板）→ 4 个功能面（**不等宽**的便当格）
 *   → 接着上次（真实数据）→ 研究工具（一行四枚）→ 落款
 *
 * 三条不变的老规矩：
 * 1. 点一下**在新标签页**里打开（走 `openTab`），首页本身不动；
 * 2. 强调色只有一支（`--accent`）——便当格之间的区分靠**深浅**（底色 / 描边 / 圆角），
 *    不靠第二支颜色；深色皮肤下同一份规则自然变暗，不需要两套；
 * 3. 图标一律走 `<AppIcon>`（视图里不内联 svg），装饰画用 CSS 画（不用图片）。
 */
import { computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import { loadPapers, state } from '@/stores/app';
import { openTab } from '@/stores/tabs';
import { fmtRelative } from '@/utils/format';
import type { Paper } from '@/api/types';

const router = useRouter();

/** 四个面（改造后的主结构）—— 顺序就是流水线顺序：读 → 懂 → 查 → 练 */
const FACES = [
  {
    name: 'library', label: '阅读', icon: 'library', art: 'book', span: 7,
    note: 'PAPER READER / 01', cta: '进入论文库',
    hint: '导入论文、解析文档、两路并发翻译。在阅读器里对照原文与译文逐段精读。',
  },
  {
    name: 'understand', label: '论文解读', icon: 'sparkle', art: 'layers', span: 5,
    note: 'INSIGHT / 02', cta: '开始解读', tint: 1,
    hint: '梳理背景、动机、贡献、方法与实验结论，先建立整体理解，再回到原文。',
  },
  {
    name: 'knowledge', label: '知识库', icon: 'knowledge', art: 'kb', span: 5,
    note: 'KNOWLEDGE BASE / 03', cta: '查找知识',
    hint: '跨论文检索与问答，覆盖插图、公式与表格，回答都带可追溯的证据。',
  },
  {
    name: 'study', label: '学习', icon: 'task', art: 'ring', span: 7,
    note: 'REVIEW / 04', cta: '查看计划', tint: 1, badge: '在建',
    hint: '把理解结果整理成可复习的概念与学习计划，让读过的东西留下来。',
  },
];

const TOOLS = [
  { name: 'notes', label: '笔记', icon: 'note', hint: '批注、笔记与高亮' },
  { name: 'models', label: '模型', icon: 'model', hint: '生成 / 向量 / 视觉' },
  { name: 'tasks', label: '任务', icon: 'task', hint: '队列、进度与重试' },
  { name: 'settings', label: '设置', icon: 'settings', hint: '参数、索引与记录' },
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
    <!-- ① 刊头：左边一句话说清这是什么，右边一块**反色板**当视觉锚点 -->
    <section class="hero">
      <div class="hero-main">
        <div class="eyebrow">Paper reading · Knowledge workspace</div>
        <h1>从论文阅读，<span class="hl">到真正理解。</span></h1>
        <p class="hero-copy">
          导入论文 → 解析 → 两路并发翻译，在阅读器里对照精读；再把结论整理进知识库。
          下面每个入口都在<strong>新的标签页</strong>里打开，随时切回来。
        </p>
      </div>
      <aside class="hero-art" aria-hidden="true">
        <div class="art-top">
          <span class="art-label">Read · Understand · Organize</span>
          <span class="art-symbol"><AppIcon name="sparkle" :size="15" /></span>
        </div>
        <p class="art-title">让每篇论文<br>都更容易读懂。</p>
        <div class="art-bottom"><span>Cylit / Paper workspace</span><span>01 — 04</span></div>
      </aside>
    </section>

    <!-- ② 四个功能面：不等宽便当格 -->
    <div class="sec-head">
      <div>
        <h2>从这里开始</h2>
        <p>选择一个功能，继续你的研究流程。</p>
      </div>
      <span class="sec-idx">01 — Workspace</span>
    </div>
    <div class="bento">
      <button
        v-for="n in FACES"
        :key="n.name"
        class="bcard click"
        :class="[`sp-${n.span}`, n.tint ? 'tint' : '']"
        @click="openRoute(n.name)"
      >
        <span class="card-top">
          <span class="icon-wrap"><AppIcon :name="n.icon" :size="19" /></span>
          <span class="go"><AppIcon name="forward" :size="15" /></span>
        </span>

        <!-- 装饰画：纯 CSS（便当格右上一块"物证"，让四张卡一眼能分开） -->
        <span v-if="n.art === 'book'" class="art art-book">
          <i class="sheet s1" /><i class="sheet s2" /><i class="sheet s3" />
        </span>
        <span v-else-if="n.art === 'layers'" class="art art-layers">
          <i /><i /><i />
        </span>
        <span v-else-if="n.art === 'kb'" class="art art-kb" />
        <span v-else class="art art-ring"><AppIcon name="sparkle" :size="40" /></span>

        <span class="art-note">{{ n.note }}</span>
        <span class="card-body">
          <span class="card-title">
            {{ n.label }}
            <em v-if="n.badge" class="badge-soft">{{ n.badge }}</em>
          </span>
          <span class="card-hint">{{ n.hint }}</span>
          <span class="card-link">{{ n.cta }}<AppIcon name="arrow-ne" :size="13" /></span>
        </span>
      </button>
    </div>

    <!-- ③ 接着上次：真实数据（最近读过的 / 能去理解的） -->
    <div class="sec-head">
      <div>
        <h2>接着上次</h2>
        <p>回到刚读过的那几篇，或者直接进解读页。</p>
      </div>
      <span class="sec-idx">02 — Recent</span>
    </div>
    <div class="bento">
      <section class="bcard sp-7">
        <div class="card-top">
          <span class="icon-wrap"><AppIcon name="play" :size="17" /></span>
          <span class="card-tag">继续阅读</span>
          <span class="spacer" />
          <button class="btn sm ghost" @click="openRoute('library')">全部论文</button>
        </div>
        <div class="list">
          <p v-if="!recentRead.length" class="list-empty">还没有阅读记录 —— 先去论文库导入一篇。</p>
          <button v-for="p in recentRead" :key="p.id" class="paper-row" @click="openPaper(p.id)">
            <span class="truncate">{{ p.title || p.id }}</span>
            <span class="spacer" />
            <span class="tiny faint nowrap">{{ fmtRelative(p.last_read_at) }}</span>
          </button>
        </div>
      </section>

      <section class="bcard sp-5">
        <div class="card-top">
          <span class="icon-wrap"><AppIcon name="layers" :size="17" /></span>
          <span class="card-tag">去理解</span>
          <span class="spacer" />
          <button class="btn sm ghost" @click="openRoute('study')">学习页</button>
        </div>
        <div class="list">
          <p v-if="!forUnderstand.length" class="list-empty">还没有可理解的论文。</p>
          <button
            v-for="p in forUnderstand"
            :key="`u-${p.id}`"
            class="paper-row"
            @click="openUnderstand(p.id)"
          >
            <span class="truncate">{{ p.title || p.id }}</span>
            <span class="spacer" />
            <AppIcon name="chevron-right" :size="14" />
          </button>
        </div>
      </section>
    </div>

    <!-- ④ 研究工具：一行四枚，只放"随时可用"的辅助面 -->
    <div class="sec-head">
      <div>
        <h2>研究工具</h2>
        <p>常用辅助功能，随时可用。</p>
      </div>
      <span class="sec-idx">03 — Tools</span>
    </div>
    <div class="tool-grid">
      <button v-for="t in TOOLS" :key="t.name" class="tool" @click="openRoute(t.name)">
        <span class="icon-wrap sm"><AppIcon :name="t.icon" :size="17" /></span>
        <span class="tool-text">
          <span class="tool-label">{{ t.label }}</span>
          <span class="tool-hint">{{ t.hint }}</span>
        </span>
        <span class="tool-go"><AppIcon name="arrow-ne" :size="15" /></span>
      </button>
    </div>

    <footer class="foot">
      <span>为阅读、理解与长期积累而设计。</span>
      <span>Cylit · Research workspace</span>
    </footer>
  </div>
</template>

<style scoped>
/* ── 版面骨架 ─────────────────────────────────────────────────────────
   `.app-main` 已经给了最大宽度与内边距，这里只收窄到"读起来舒服"的一列。 */
.home { max-width: 1180px; margin: 0 auto; }

/* ── ① 刊头 ── */
.hero {
  display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(268px, .85fr);
  align-items: end; gap: 30px; margin-bottom: 34px;
}
.eyebrow {
  display: flex; align-items: center; gap: 10px; margin-bottom: 15px;
  font-size: 10px; font-weight: 750; letter-spacing: .16em; text-transform: uppercase;
  color: var(--text-faint);
}
.eyebrow::before { content: ""; width: 22px; height: 1px; background: currentColor; opacity: .55; }
.hero h1 {
  margin: 0; font-size: clamp(28px, 3.5vw, 44px); line-height: 1.08;
  letter-spacing: -.055em; font-weight: 720; color: var(--text);
}
/* 高亮 = **一支强调色的半透明笔刷**（不是第二支颜色，也不是背景块）：
   深色皮肤下同一条规则混出来就是暗色底，不用另写一套。 */
.hl { position: relative; z-index: 0; white-space: nowrap; }
.hl::after {
  content: ""; position: absolute; z-index: -1; left: -3px; right: -5px; bottom: .14em;
  height: .4em; border-radius: 3px; transform: rotate(-1.4deg);
  background: color-mix(in srgb, var(--accent) 34%, transparent);
}
.hero-copy {
  margin: 18px 0 0; max-width: 46em;
  font-size: 12.5px; line-height: 1.85; color: var(--text-dim);
}
.hero-copy strong { color: var(--text); font-weight: 600; }

/* 反色装饰板：底色直接取 `--text`（浅色皮肤=深板，深色皮肤=浅板），文字用 `--bg` —— 
   两个皮肤里都是"一张压在场上的实心板"，不需要为深色再画一份。 */
.hero-art {
  position: relative; overflow: hidden; isolation: isolate;
  display: flex; flex-direction: column; justify-content: space-between;
  min-height: 194px; padding: 20px 22px; border-radius: var(--radius);
  background: color-mix(in srgb, var(--text) 94%, var(--accent)); color: var(--bg);
  box-shadow: var(--shadow-2);
}
.hero-art::before {
  content: ""; position: absolute; right: 16px; top: -10px; width: 186px; height: 186px;
  border-radius: 50%; border: 1px solid color-mix(in srgb, var(--bg) 18%, transparent);
  box-shadow:
    0 0 0 22px color-mix(in srgb, var(--bg) 6%, transparent),
    0 0 0 45px color-mix(in srgb, var(--bg) 4%, transparent),
    0 0 0 70px color-mix(in srgb, var(--bg) 2.5%, transparent);
}
.hero-art::after {
  content: ""; position: absolute; right: 62px; top: 34px; width: 94px; height: 130px;
  border-radius: 55% 45% 50% 45%; transform: rotate(24deg);
  background: var(--accent); opacity: .88;
}
.art-top, .art-bottom, .art-title { position: relative; z-index: 2; }
.art-top, .art-bottom { display: flex; align-items: center; justify-content: space-between; }
.art-label, .art-bottom { font-size: 9.5px; letter-spacing: .14em; text-transform: uppercase; }
.art-label { color: color-mix(in srgb, var(--bg) 72%, transparent); }
.art-symbol {
  display: grid; place-items: center; width: 28px; height: 28px; border-radius: 50%;
  border: 1px solid color-mix(in srgb, var(--bg) 32%, transparent); color: var(--accent);
}
.art-title {
  margin: 16px 0; max-width: 15ch;
  font-size: 21px; font-weight: 720; line-height: 1.24; letter-spacing: -.045em;
}
.art-bottom { color: color-mix(in srgb, var(--bg) 62%, transparent); }

/* ── 小节抬头（编号像刊物的页眉，一排里把"这是什么"和"第几块"分开） ── */
.sec-head {
  display: flex; align-items: flex-end; justify-content: space-between; gap: 18px;
  margin: 30px 0 13px;
}
.sec-head h2 { margin: 0; font-size: 15px; font-weight: 720; letter-spacing: -.035em; }
.sec-head p { margin: 5px 0 0; font-size: 11.5px; color: var(--text-faint); }
.sec-idx {
  flex: none; font-size: 10px; letter-spacing: .15em; text-transform: uppercase;
  color: var(--text-faint);
}

/* ── 便当格 ──
   12 列网格 + 固定 gap：跨度只有 7/5 两种，"不等宽"来自这里，不来自卡片自身高度。 */
.bento { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 14px; }
.sp-7 { grid-column: span 7; }
.sp-5 { grid-column: span 5; }

.bcard {
  position: relative; display: flex; flex-direction: column; overflow: hidden; isolation: isolate;
  min-height: 202px; padding: 20px; border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--bg-elev); color: var(--text); box-shadow: var(--shadow-1);
  transition: transform .22s var(--ease), box-shadow .22s var(--ease), border-color .22s var(--ease);
}
/* 可点的那种（四个功能面）：整块是按钮，文字左对齐、字体继承 */
.bcard.click {
  width: 100%; text-align: left; font: inherit; cursor: pointer; appearance: none;
}
.bcard.click:hover { transform: translateY(-3px); box-shadow: var(--shadow-2); border-color: var(--border-strong); }
.bcard.click:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
/* 底色分工：① 白/浅底 = 主线功能；② tint = 第二档（多混一点强调色）。
   深浅只差一点点 —— 便当格要看出"不同"，但整页仍然只有一支颜色。 */
.bcard.tint { background: color-mix(in srgb, var(--accent) 9%, var(--bg-elev)); }

.card-top { position: relative; z-index: 3; display: flex; align-items: center; gap: 10px; }
.icon-wrap {
  display: grid; place-items: center; flex: none; width: 38px; height: 38px;
  border-radius: 13px; background: var(--bg-sunken); color: var(--text);
}
.bcard.tint .icon-wrap { background: color-mix(in srgb, var(--accent) 12%, var(--bg-elev)); }
.icon-wrap.sm { width: 34px; height: 34px; border-radius: 11px; }
/* 箭头圆钮：hover 时转 -35°，从"下一格"变成"斜着出去"——与"新标签页打开"同义 */
.go {
  margin-left: auto; display: grid; place-items: center; flex: none;
  width: 30px; height: 30px; border-radius: 50%;
  border: 1px solid var(--border); background: var(--bg-elev); color: var(--text-dim);
  transition: transform .22s var(--ease), color .22s var(--ease);
}
.bcard.click:hover .go { transform: rotate(-35deg); color: var(--text); }
.card-tag { font-size: 12px; font-weight: 650; letter-spacing: -.02em; }
.card-body { position: relative; z-index: 3; margin-top: auto; padding-top: 26px; max-width: 34em; }
/* 窄格（5 列）里的装饰画就在右手边：正文收窄到 64%，长句也不会顶到画上 */
.sp-5 .card-body { max-width: 64%; }
.card-title { display: flex; align-items: center; gap: 8px; font-size: 21px; font-weight: 720; letter-spacing: -.045em; }
.badge-soft {
  font-style: normal; font-size: 10px; font-weight: 600; letter-spacing: .02em; padding: 2px 7px;
  border-radius: var(--radius-ctl); background: var(--accent-soft); color: var(--accent-text);
}
.card-hint { display: block; margin-top: 8px; max-width: 30em; font-size: 12px; line-height: 1.78; color: var(--text-dim); }
.card-link {
  display: inline-flex; align-items: center; gap: 6px; margin-top: 15px;
  font-size: 11.5px; font-weight: 650; color: var(--accent-text);
}
.card-link :deep(svg) { transition: transform .2s var(--ease); }
.bcard.click:hover .card-link :deep(svg) { transform: translate(2px, -2px); }
.art-note {
  position: absolute; right: 20px; bottom: 18px; z-index: 2;
  font-size: 9px; letter-spacing: .12em; text-transform: uppercase; color: var(--text-faint);
}

/* ── 便当格里的装饰画（纯 CSS，不用图片） ── */
.art { position: absolute; z-index: 1; pointer-events: none; }
.art-book { right: 26px; top: 26px; width: 172px; height: 142px; transform: rotate(3deg); }
.sheet {
  position: absolute; width: 100px; height: 132px; border: 1px solid var(--border);
  border-radius: 7px; background: var(--bg-layer); box-shadow: var(--shadow-1);
}
.sheet::before {
  content: ""; display: block; height: 3px; width: 32px; margin: 13px 10px 0;
  border-radius: 3px; background: var(--border-strong);
}
.sheet::after {
  content: ""; position: absolute; left: 10px; right: 12px; top: 32px; height: 64px;
  background: repeating-linear-gradient(to bottom, var(--divider) 0 1px, transparent 1px 6px);
}
.sheet.s1 { right: 50px; top: 6px; transform: rotate(-14deg); background: var(--bg-sunken); }
.sheet.s2 { right: 21px; top: 1px; transform: rotate(8deg); }
.sheet.s3 {
  right: 0; top: 14px; transform: rotate(19deg);
  background: color-mix(in srgb, var(--accent) 22%, var(--bg-elev));
  border-color: color-mix(in srgb, var(--accent) 34%, var(--border));
}
.sheet.s3::before { background: color-mix(in srgb, var(--accent) 55%, var(--border-strong)); }
.sheet.s3::after {
  background: repeating-linear-gradient(to bottom,
    color-mix(in srgb, var(--accent) 50%, var(--border)) 0 1px, transparent 1px 6px);
}
/* 摆在右下角**编号之上**（bottom 44）：编号在 18px 那一行，两者错开就不打架 */
.art-layers { right: 20px; bottom: 44px; width: 104px; height: 88px; }
.art-layers i {
  position: absolute; display: block; width: 64px; height: 50px; border-radius: 16px;
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
  background: color-mix(in srgb, var(--accent) 8%, var(--bg-elev));
}
.art-layers i:nth-child(1) { right: 24px; top: 0; transform: rotate(14deg); }
.art-layers i:nth-child(2) { right: 9px; top: 15px; transform: rotate(-8deg); background: color-mix(in srgb, var(--accent) 14%, var(--bg-elev)); }
.art-layers i:nth-child(3) { right: 0; top: 30px; transform: rotate(5deg); background: color-mix(in srgb, var(--accent) 22%, var(--bg-elev)); }
.art-kb {
  right: 24px; top: 34px; width: 82px; height: 84px; border-radius: 20px; transform: rotate(7deg);
  background: color-mix(in srgb, var(--accent) 18%, var(--bg-elev));
  border: 1px solid color-mix(in srgb, var(--accent) 26%, var(--border));
}
.art-kb::before {
  content: ""; position: absolute; left: 17px; right: 17px; top: 28px; height: 1px;
  background: color-mix(in srgb, var(--accent) 45%, var(--border-strong));
  box-shadow: 0 12px color-mix(in srgb, var(--accent) 45%, var(--border-strong)),
              0 24px color-mix(in srgb, var(--accent) 45%, var(--border-strong));
}
.art-kb::after {
  content: ""; position: absolute; left: 11px; top: 24px; width: 4px; height: 4px; border-radius: 50%;
  background: color-mix(in srgb, var(--accent) 70%, var(--border-strong));
  box-shadow: 0 12px color-mix(in srgb, var(--accent) 70%, var(--border-strong)),
              0 24px color-mix(in srgb, var(--accent) 70%, var(--border-strong));
}
.art-ring {
  right: 30px; top: 30px; display: grid; place-items: center; width: 108px; height: 108px;
  border-radius: 50%; border: 1px solid color-mix(in srgb, var(--accent) 34%, var(--border));
  color: color-mix(in srgb, var(--accent) 82%, var(--text));
}
.art-ring::before {
  content: ""; position: absolute; inset: 12px; border-radius: 50%;
  border: 1px dashed color-mix(in srgb, var(--accent) 40%, var(--border));
}

/* ── 列表卡里的行 ── */
.list { position: relative; z-index: 3; margin-top: 12px; }
.list-empty { margin: 6px 0 2px; font-size: 12px; color: var(--text-faint); }
.paper-row {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 7px 8px; border: 0; border-radius: var(--radius-sm);
  background: transparent; color: var(--text); font: inherit; font-size: 12.5px;
  text-align: left; cursor: pointer;
}
.paper-row:hover { background: var(--bg-hover); }

/* ── ④ 工具行 ── */
.tool-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.tool {
  display: flex; align-items: center; gap: 12px; min-height: 84px; padding: 14px 15px;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elev);
  color: var(--text); font: inherit; text-align: left; cursor: pointer; appearance: none;
  transition: transform .2s var(--ease), box-shadow .2s var(--ease), border-color .2s var(--ease);
}
.tool:hover { transform: translateY(-2px); box-shadow: var(--shadow-2); border-color: var(--border-strong); }
.tool:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.tool-text { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; }
.tool-label { font-size: 13px; font-weight: 700; letter-spacing: -.02em; }
.tool-hint { font-size: 10.5px; color: var(--text-faint); }
.tool-go { flex: none; display: flex; color: var(--text-faint); transition: transform .2s var(--ease), color .2s var(--ease); }
.tool:hover .tool-go { transform: translate(2px, -2px); color: var(--text); }

/* ── 落款 ── */
.foot {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-top: 28px; padding-top: 16px; border-top: 1px solid var(--divider);
  font-size: 10.5px; color: var(--text-faint);
}
.foot span:last-child { letter-spacing: .1em; text-transform: uppercase; }

/* ── 响应式：先收跨度，再收装饰画，最后收列数 ── */
@media (max-width: 1000px) {
  .hero { grid-template-columns: minmax(0, 1fr); gap: 20px; align-items: stretch; }
  .hero-art { min-height: 168px; }
  .sp-7, .sp-5 { grid-column: span 6; }
  .tool-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .art-book { right: 12px; top: 22px; transform: scale(.84) rotate(3deg); transform-origin: top right; }
}
@media (max-width: 680px) {
  .sp-7, .sp-5 { grid-column: span 12; }
  .bcard { min-height: 180px; padding: 18px; }
  .card-body { padding-top: 20px; }
  .card-title { font-size: 19px; }
  .sec-head { margin-top: 24px; }
  /* 窄屏上装饰画会跟正文抢位置 → 整块收掉，卡片只留字（信息优先） */
  .art { display: none; }
  .art-note { display: none; }
  .tool-grid { grid-template-columns: minmax(0, 1fr); }
  .tool { min-height: 68px; }
  .foot { flex-direction: column; align-items: flex-start; gap: 6px; }
}
/* 系统里关掉动效：hover 的位移/旋转全部不动（颜色仍会变，仍知道鼠标在哪） */
@media (prefers-reduced-motion: reduce) {
  .bcard, .bcard.click:hover, .tool, .tool:hover, .go, .card-link :deep(svg) { transform: none; }
}
</style>
