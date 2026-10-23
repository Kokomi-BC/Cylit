<script setup lang="ts">
/**
 * 设置 · 显示主题：**一整页三块**的编排层。
 *
 * 版式（2026-12 用户口径，最终）：**左一卡、右一栏**
 *   左：**预览效果**（样例 + 当前配色，唯一套卡片的东西 —— 它才是"看效果"的地方）
 *   右：一条窄而长的竖轴，三块**都不套卡片**，只用留白与 1px 细线分区：
 *       主题预设（竖卡网格 + 竖向滚动）→ 分类字号（窄步进器）→ 高亮效果。
 * 为什么右栏不套卡：三张卡摞起来就是三条卡缝 + 三个卡头，而它们本来就是同一件事
 * （"这一页怎么显示"）的三个旋钮；裸块 + 细线更像设置项列表，也更省高度。
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

import DisplayPreviewCard from './DisplayPreviewCard.vue';
import CategorySizeBlock from './CategorySizeBlock.vue';
import HighlightStyleCard from './HighlightStyleCard.vue';
</script>

<template>
  <div class="appearance" data-appearance>
    <div class="ap-left">
      <DisplayPreviewCard />
    </div>

    <div class="ap-right">
      <ThemePresetCard />
      <CategorySizeBlock />
      <HighlightStyleCard />
    </div>
  </div>
</template>

<style scoped>
/* 容器：子组件里的 @container 都对着它量 */
.appearance {
  container-type: inline-size;
  display: grid;
  /* 左宽右窄：左卡是"看效果"的主力（样例 + 配色），右栏是三个旋钮，420 够用又不抢视线 */
  grid-template-columns: minmax(0, 1fr) minmax(0, 420px);
  gap: 16px;
  align-items: start;
  min-width: 0;
}
/* 左卡比右栏矮得多（436 vs 792）：让它在这一层里**吸顶**，往下滚右栏时预览一直看得到，
   画面底下那片空白也不会跟着滚走变成"一块空卡"。 */
.ap-left { min-width: 0; position: sticky; top: 12px; }

/* 右栏：一条竖轴，三块裸内容（不套卡片），块与块之间一条 1px 细线 + 14px 呼吸 */
.ap-right { min-width: 0; display: flex; flex-direction: column; }
.ap-right > * + * { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--divider); }

/* 网格里的兄弟卡不需要 base.css 那条 `.card + .card` 的 12px 上边距（缝已由 gap 给） */
.appearance :deep(.card + .card) { margin-top: 0; }

/* 内容区窄到 940 以下：右栏 420 + 16 的缝之后，左卡只剩 500 以下 —— 样例里的标题会被截成
   半行（实测 1280 宽时左卡只剩 366）。这一档起落单列：先预览，再三个旋钮。 */
@container (max-width: 940px) {
  .appearance { grid-template-columns: minmax(0, 1fr); }
}

</style>
