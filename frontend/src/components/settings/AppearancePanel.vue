<script setup lang="ts">
/**
 * 设置 · 显示主题：**一整页三块**的编排层。
 *
 * 版式（自上而下）：主题预设 + 当前配色（左右并排）→ 显示与排版（一张卡里三段）→
 * 预览效果 + 高亮效果（两块等宽，高度对齐）。三块的内部间距统一 12，行与行之间也是 12 ——
 * 这一区是"看效果"的地方，间距一大视线就断，用户得来回滚才能对上一个改动。
 *
 * 第三行原来是「预览效果 + 高级设置」：高级设置（细腻动画 / 收起左侧栏）低频、而且"收起左侧栏"
 * 在侧栏底部本来就有入口，右栏底部因此常年留白。2026-12 起整卡换成「高亮效果」——
 * 那是每篇论文都会看到的东西，值得占这块地方。`stores/appearance` 与 `stores/shellPrefs`
 * 里的状态一个没删：动画令牌、侧栏收展都照旧走原路径，只是不再有这张卡的开关。
 *
 * 断点用**容器查询**而不是媒体查询：设置页内容区的宽度跟着左侧栏收/展变化
 * （窗口 1600 时 1128，侧栏收起后更宽），媒体查询看的是窗口，量不到这个宽度。
 */
import ThemePresetCard from './ThemePresetCard.vue';
import ThemePaletteCard from './ThemePaletteCard.vue';
import DisplayTuningCard from './DisplayTuningCard.vue';
import DisplayPreviewCard from './DisplayPreviewCard.vue';
import HighlightStyleCard from './HighlightStyleCard.vue';
</script>

<template>
  <div class="appearance" data-appearance>
    <div class="ap-top">
      <ThemePresetCard />
      <ThemePaletteCard />
    </div>
    <DisplayTuningCard />
    <div class="ap-bottom">
      <DisplayPreviewCard />
      <HighlightStyleCard />
    </div>
  </div>
</template>

<style scoped>
/* 容器：子组件里的 @container 都对着它量 */
.appearance {
  container-type: inline-size;
  display: grid;
  gap: 12px;
  min-width: 0;
}

/* 第一行：预设占主空间（1.55 : 1），配色是它的注释，不该一样大 */
.ap-top {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr);
  gap: 12px;
  align-items: stretch;
  min-width: 0;
}

/* 第三行：预览 + 高亮，两块大致等宽（1.1 : 1）并**等高**。
   高亮卡里是两张并排的示例小卡，窄了就只能叠成一列、右栏又回到"下面一大片空白"；
   用 stretch 让两卡同高，示例块再用 margin-top:auto 顶到底 —— 底边因此齐平。 */
.ap-bottom {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  gap: 12px;
  align-items: stretch;
  min-width: 0;
}

/* 网格里的兄弟卡不需要 base.css 那条 `.card + .card` 的 12px 上边距（缝已由 gap 给） */
.appearance :deep(.card + .card) { margin-top: 0; }

/* 第三行的门槛比第一行高：高亮卡里是两张并排的小卡，内容区窄到 820 左右时
   右栏只剩 330 出头 —— 两张示例卡要么挤成一条、要么自己叠起来把卡撑高，两种都不好看。
   这一档起整行落单列（两张卡各占整宽，示例卡反而更宽敞）。 */
@container (max-width: 820px) {
  .ap-bottom { grid-template-columns: minmax(0, 1fr); }
}
/* 700 以下（内容区窄于这个数）：第一行也落单列，配色跟到预设下面 */
@container (max-width: 700px) {
  .ap-top { grid-template-columns: minmax(0, 1fr); }
}
</style>
