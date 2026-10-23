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
  <!-- 外层只当容器（`container-type: inline-size`）：**容器查询不能改容器自己**，
       所以「整页落单列」这条必须查一个外层、改里面的 `.appearance`。 -->
  <div class="appearance-wrap" data-appearance>
  <div class="appearance">
    <div class="ap-left">
      <DisplayPreviewCard />
    </div>

    <div class="ap-right">
      <ThemePresetCard />

      <!-- 宽屏时这两块并排（各占一半）；右栏窄了就自动落成一列 -->
      <div class="ap-pair">
        <CategorySizeBlock />
        <HighlightStyleCard />
      </div>
    </div>
  </div>
  </div>
</template>

<style scoped>
/* 容器放在**外层**：容器查询只能改后代，不能改容器自己（以前那条「整页落单列」写在
   `.appearance` 上，永远不会生效）。 */
.appearance-wrap { container-type: inline-size; min-width: 0; }
.appearance {
  display: grid;
  /* 左卡**收窄**到 520：它只是「看效果」，再宽也只剩留白；右栏因此拿到余下的宽度，
     分类字号与高亮效果才并得开（用户口径 2026-12）。 */
  grid-template-columns: minmax(0, 520px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
  min-width: 0;
}
/* 左卡比右栏矮得多（436 vs 792）：让它在这一层里**吸顶**，往下滚右栏时预览一直看得到，
   画面底下那片空白也不会跟着滚走变成"一块空卡"。 */
.ap-left { min-width: 0; position: sticky; top: 12px; }

/* 右栏：一条竖轴，几块裸内容（不套卡片），块与块之间一条 1px 细线 + 14px 呼吸 */
.ap-right { min-width: 0; display: flex; flex-direction: column; }
.ap-right > * + * { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--divider); }

/* 分类字号 + 高亮效果：宽了并排、窄了落一列（container 量的是这一栏的宽度） */
.ap-pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
@container (max-width: 960px) {
  .ap-pair { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .ap-pair > * + * { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--divider); }
}

/* 网格里的兄弟卡不需要 base.css 那条 `.card + .card` 的 12px 上边距（缝已由 gap 给） */
.appearance :deep(.card + .card) { margin-top: 0; }

/* 内容区窄到 880 以下：左卡 520 + 16 + 右栏至少要 340 才排得下，再窄就只能落单列
   （先预览，再右栏那几块）。 */
@container (max-width: 880px) {
  .appearance { grid-template-columns: minmax(0, 1fr); }
}

</style>
