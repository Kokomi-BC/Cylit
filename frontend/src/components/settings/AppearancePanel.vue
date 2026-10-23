<script setup lang="ts">
/**
 * 设置 · 显示主题：**一整页三块**的编排层。
 *
 * 版式（2026-12 用户口径，最新）：**两列，各两块**
 *   左：**预览效果**（唯一的卡片：样例 + 当前配色）→ 分类字号（裸块：每类带样例 + 小/中/大 / 字体）
 *   右：**主题预设**（裸块：竖卡网格 + 竖向滚动）→ 高亮效果（裸块：荧光笔 / 下划线）
 * 为什么右两列都裸放：三张卡摞起来就是三条卡缝 + 三个卡头，而它们本来就是同一件事
 * （"这一页怎么显示"）的旋钮；裸块 + 1px 细线更像设置项列表，也更省高度。
 * 左卡**不再吸顶**：分类字号搬到它下面之后，左列本身就是一整段内容，吸顶只会挡住右列。
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
      <CategorySizeBlock />
    </div>

    <div class="ap-right">
      <ThemePresetCard />
      <HighlightStyleCard />
    </div>
  </div>
  </div>
</template>

<style scoped>
/* 容器放在**外层**：容器查询只能改后代，不能改容器自己（以前那条「整页落单列」写在
   `.appearance` 上，永远不会生效）。 */
.appearance-wrap { container-type: inline-size; min-width: 0; }
/* 两列等宽：左边「看效果」（预览卡 + 分类字号），右边「挑东西」（主题 + 高亮）。
   16 / 20 的缝：竖向 16、横向 20，左边是卡、右边是裸块，横向多一点才不显得贴在一起。 */
.appearance {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 20px;
  align-items: start;
  min-width: 0;
}
/* 两列都只做竖排容器；不吸顶（分类字号搬到左列之后，吸顶只会挡住右列） */
.ap-left, .ap-right { min-width: 0; display: flex; flex-direction: column; }
.ap-left > * + *,
.ap-right > * + * {
  margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--divider);
}

/* 网格里的兄弟卡不需要 base.css 那条 `.card + .card` 的 12px 上边距（缝已由 gap 给） */
.appearance :deep(.card + .card) { margin-top: 0; }

/* 内容区窄到 940 以下：两列各剩 460 不到 —— 左边样例、右边主题列表都会挤，落单列
   （先「看效果」：预览 + 分类字号，再「挑东西」：主题 + 高亮）。 */
@container (max-width: 940px) {
  .appearance { grid-template-columns: minmax(0, 1fr); }
}

</style>
