<script setup lang="ts">
/**
 * 设置 · 显示主题：**一整页三块**的编排层。
 *
 * 版式（自上而下）：主题预设 + 当前配色（左右并排）→ 显示与排版（一张卡里三段）→
 * 预览效果 + 高级设置（宽窄错落）。三块的内部间距统一 12，行与行之间也是 12 ——
 * 这一区是"看效果"的地方，间距一大视线就断，用户得来回滚才能对上一个改动。
 *
 * 断点用**容器查询**而不是媒体查询：设置页内容区的宽度跟着左侧栏收/展变化
 * （窗口 1600 时 1128，侧栏收起后更宽），媒体查询看的是窗口，量不到这个宽度。
 */
import ThemePresetCard from './ThemePresetCard.vue';
import ThemePaletteCard from './ThemePaletteCard.vue';
import DisplayTuningCard from './DisplayTuningCard.vue';
import DisplayPreviewCard from './DisplayPreviewCard.vue';
import DisplayAdvancedCard from './DisplayAdvancedCard.vue';
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
      <DisplayAdvancedCard />
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

/* 第三行：预览宽、高级窄，高度各随内容（错落，不强行对齐） */
.ap-bottom {
  display: grid;
  grid-template-columns: minmax(0, 1.7fr) minmax(0, .8fr);
  gap: 12px;
  align-items: start;
  min-width: 0;
}

/* 网格里的兄弟卡不需要 base.css 那条 `.card + .card` 的 12px 上边距（缝已由 gap 给） */
.appearance :deep(.card + .card) { margin-top: 0; }

/* 700 以下（内容区窄于这个数）：三行各自单列，配色跟到预设下面 */
@container (max-width: 700px) {
  .ap-top { grid-template-columns: minmax(0, 1fr); }
  .ap-bottom { grid-template-columns: minmax(0, 1fr); }
}
</style>
