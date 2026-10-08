<script setup lang="ts">
/**
 * 参考文献列表 —— **阅读器侧栏与论文详情弹窗共用同一份**（用户口径 2026-10-08：
 * "参考文献使用侧栏的参考文献同款风格，并且支持跳转"）。
 *
 * 一条两行：第一行「标号 + 中文标题」，第二行英文标题（作者段收进 tooltip）；
 * 左边一枚跳转图标**占一整列**（论文 / GitHub / 普通网址三种造型，见 `utils/references`），
 * 整张卡片也是跳转，点它去发表网站（DOI / arXiv / 原文里的链接，兜底学术搜索）。
 *
 * ⚠️ 样式是**自带**的：调用方的 `scoped` 样式只能命中本组件的根元素，
 * 所以卡片外观必须写在这里，两处才会长得一模一样。
 */
import AppIcon from '@/components/AppIcon.vue';
import { paperUrl, refLink, type RefLinkKind } from '@/utils/references';
import type { PaperReference } from '@/api/types';

const props = defineProps<{ items: PaperReference[] }>();

/** 点一条文献 → 去它的**发表网站**（`paperUrl` 会跳过 GitHub：代码图标才是仓库）。 */
function openRef(ref: PaperReference) {
  window.open(paperUrl(ref), '_blank', 'noopener');
}

/** 打开一个已经确定好的地址（跳转图标用它，避免再走一遍"论文地址"的回退链）。 */
function openUrl(url: string) {
  if (url) window.open(url, '_blank', 'noopener');
}

/** 最左边那枚跳转图标的造型与提示（按"这条文献的主链接是什么"选）。 */
const LEAD_ICON: Record<RefLinkKind, string> = {
  paper: 'document', github: 'github', link: 'link',
};
const LEAD_TITLE: Record<RefLinkKind, string> = {
  paper: '打开论文页面', github: '打开代码仓库（GitHub）', link: '打开原网页',
};

/** 那枚图标的悬停提示：论文图标但其实是兜底搜索时，别谎称"论文页面"。 */
function leadTitle(ref: PaperReference): string {
  const link = refLink(ref).primary;
  if (link.kind === 'paper' && /scholar\.google\.com/i.test(link.url)) return '去学术搜索找这篇';
  return LEAD_TITLE[link.kind];
}
</script>

<template>
  <div class="ref-list">
    <!-- ⚠️ 是 `<div>` 而不是 `<button>`：卡片**里面**还要放跳转图标（左边那枚 + 可能的 GitHub 图标），
         按钮套按钮是非法 HTML（浏览器会把内层按钮提出去）。`role=button` + 回车补齐键盘可达。 -->
    <div
      v-for="r in props.items"
      :key="r.id"
      class="item ref-item"
      role="button"
      tabindex="0"
      :data-ref-no="r.order_no"
      :title="r.raw"
      @click="openRef(r)"
      @keydown.enter.prevent="openRef(r)"
    >
      <!-- 最左边那枚**跳转图标占一整列**：论文链接 → 论文图标、只有代码链接 → GitHub 图标、
           只有普通网址 → 链接图标；什么都抠不出来时仍是论文图标（点它是学术搜索兜底）。 -->
      <button class="ref-lead" :title="leadTitle(r)"
              :data-ref-lead="refLink(r).primary.kind"
              @click.stop="openUrl(refLink(r).primary.url)">
        <AppIcon :name="LEAD_ICON[refLink(r).primary.kind]" :size="18" />
      </button>
      <div class="ref-main">
        <!-- 标号**内联**在标题前面；整张卡片就是那个跳转 -->
        <span class="ref-head" :class="{ 'ref-clamp': !r.title }">
          <span class="ref-no">[{{ r.order_no }}]</span>
          <span v-if="r.title" class="ref-zh">{{ r.title }}</span>
          <!-- 没有中文标题、但**规则切出了英文标题** → 显示题名本身，不把作者段摊在脸上；
               两样都没有才回退到整条原文。 -->
          <span v-else>{{ r.title_en || r.raw }}</span>
        </span>
        <!-- 英文那一行：**只有标题**，整条原文（含作者、会议、年份、链接）在 tooltip 里 -->
        <span v-if="r.title" class="ref-en ref-clamp">{{ r.title_en || r.raw }}</span>
        <!-- **论文与 GitHub 同时存在**：GitHub 图标放标题下面（左边那枚被论文占用了） -->
        <div v-if="refLink(r).code" class="ref-foot">
          <button class="btn icon sm" title="打开代码仓库（GitHub）" data-ref-code
                  @click.stop="openUrl(refLink(r).code!.url)">
            <AppIcon name="github" :size="13" />
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ---------------- 卡片本体（与侧栏 `.item` 同一套观感） ----------------
 * 这一层刻意写全：调用方的 scoped 样式进不到组件内部，样式必须自带。 */
.item {
  border: 1px solid var(--border); border-left: 2px solid var(--border-strong);
  border-radius: var(--radius-sm); background: var(--bg-layer);
  padding: 8px 10px; margin-bottom: 8px;
}
/* 引用文献：一条两行 —— 第一行「标号 + 中文标题」（标号内联，不占一整列），
   第二行英文（只放标题：作者段在 tooltip 里）。整张卡片都是跳转按钮。 */
.ref-item {
  display: flex; flex-direction: row; align-items: flex-start; gap: 5px;
  width: 100%; text-align: left;
  /* 左边距收窄（10 → 6）：图标那一列本来就贴着左边，留 10 会让"图标两侧的空白"一边大一边小 */
  padding: 8px 10px 8px 6px;
  font-family: inherit; font-size: 12px; color: var(--text); cursor: pointer;
  border-left-color: var(--border-strong);
}
.ref-item:hover { border-color: var(--accent); background: var(--bg-hover); }
.ref-item:focus-visible { outline: none; box-shadow: var(--ring); }
/* 最左边那枚跳转图标**占一整列**：图标 18px，外面只留 2px 点区（22×22），与卡片首行对齐 ——
   一列图标从上到下齐平，扫一眼就知道每条文献是什么类型的链接。 */
.ref-lead {
  flex: none; display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; margin: -2px 0 0; padding: 0;
  border: 0; border-radius: var(--radius-sm); background: transparent;
  color: var(--text-faint); cursor: pointer;
}
.ref-lead:hover { color: var(--accent); background: var(--bg-elev); }
/* 图标右边那一栏：标号 + 标题 + 英文行 +（论文与 GitHub 都有时）GitHub 行 */
.ref-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
/* 「论文 + GitHub 都有」时，GitHub 图标贴在标题下面（右对齐） */
.ref-foot { display: flex; justify-content: flex-end; gap: 2px; margin-top: 3px; }
.ref-head { display: block; }
.ref-no { font-family: var(--mono); font-size: 11px; color: var(--text-faint); margin-right: 4px; }
.ref-zh { font-weight: 600; }
.ref-en { color: var(--text-dim); font-size: 11.5px; line-height: 1.5; }
/* 两行封顶（原文常常很长，整条看 hover 的 tooltip）。
   ⚠️ 必须是**块级**钳制，而且标号要**在它里面**：块级会独占一行，把标号放在它外面
   就会被挤到上面单独成行；反过来用行内级（`-webkit-inline-box`）又会被长文本挤到下一行。 */
.ref-clamp {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; line-clamp: 2;
}
</style>
