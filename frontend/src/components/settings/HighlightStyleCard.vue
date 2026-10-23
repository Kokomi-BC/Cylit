<script setup lang="ts">
/**
 * 设置 · 显示主题 · **高亮效果**（**不套卡片**：与「主题预设」「字体设置」同处右栏，只用留白与细线分区）。
 *
 * 实现口径不变：这里**不重画一套预览样式**。两张小卡里的示例就是真的 mark.hl，
 * 画法由 base.css 那组声明决定（html[data-hl-style=underline] 管全局、data-hl-force 管这张卡固定演某种），
 * 所以示例与阅读器里看到的必然一致。
 */
import AppIcon from '@/components/AppIcon.vue';
import { HIGHLIGHT_OPTIONS, appearance, setHighlight } from '@/stores/appearance';
</script>

<template>
  <section class="hl" data-hl-card>
    <div class="block-head">
      <h3>高亮效果</h3>
      <p>调整论文阅读与文本标注的显示方式</p>
    </div>

    <div class="hl-opts" role="radiogroup" aria-label="高亮效果">
      <button
        v-for="o in HIGHLIGHT_OPTIONS"
        :key="o.value"
        type="button"
        role="radio"
        class="opt"
        :class="{ on: appearance.highlight === o.value }"
        :aria-checked="appearance.highlight === o.value"
        :data-hl-option="o.value"
        @click="setHighlight(o.value)"
      >
        <span class="opt-head">
          <span class="opt-check" aria-hidden="true">
            <AppIcon v-if="appearance.highlight === o.value" name="check" :size="11" :weight="2.4" />
          </span>
          <span class="opt-name">{{ o.label }}</span>
          <span v-if="appearance.highlight === o.value" class="opt-on">已选中</span>
        </span>
        <span class="opt-desc">{{ o.desc }}</span>

        <!-- 示例：真的 mark.hl，画法由 data-hl-force 钉死 -->
        <span class="opt-demo" :data-hl-force="o.value" aria-hidden="true">
          <mark class="hl yellow">外部知识接进上下文之后，模型的回答会更贴题。</mark>其余文字保持原样。
        </span>
      </button>
    </div>
  </section>
</template>

<style scoped>
.hl { min-width: 0; }
.block-head { display: grid; gap: 1px; margin-bottom: 8px; min-width: 0; }
.block-head h3 {
  margin: 0; font-size: calc(14px * var(--ts, 1)); font-weight: 650;
  letter-spacing: -.01em; line-height: 1.3;
}
.block-head p { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.4; color: var(--text-faint); }

/* 两张选项卡：宽了并排，窄了（并排到右栏一半时约 260）自动竖排 —— 否则示例只剩百来像素 */
.hl-opts { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }

/* 一张可选样式：名字 + 一句说明 + 真示例。选中靠三件事一起说（描边 / 浅底 / 勾） */
.opt {
  display: flex; flex-direction: column; gap: 5px;
  min-width: 0; padding: 8px 9px 9px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text);
  font-family: inherit; text-align: left; cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.opt:hover { background: var(--bg-subtle); }
.opt.on { border-color: var(--accent); background: var(--accent-soft); }
.opt:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.opt-head { display: flex; align-items: center; gap: 6px; min-width: 0; }
.opt-check {
  display: grid; place-items: center; flex: none;
  width: 15px; height: 15px; border-radius: 999px;
  border: 1.5px solid var(--border-strong, var(--border));
  background: transparent; color: var(--text-on-accent);
}
.opt.on .opt-check { border-color: var(--accent); background: var(--accent); }
.opt-name {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: calc(12.5px * var(--ts, 1)); font-weight: 650; line-height: 1.4;
}
.opt.on .opt-name { color: var(--accent-text); }
.opt-on {
  margin-left: auto; flex: none;
  font-size: calc(10.5px * var(--ts, 1)); font-weight: 600; color: var(--accent-text);
}
.opt-desc { font-size: calc(11px * var(--ts, 1)); line-height: 1.45; color: var(--text-faint); }
/* 示例块：顶到最下面（margin-top: auto），两张卡的示例因此底边对齐 */
.opt-demo {
  margin-top: auto; padding: 7px 8px;
  border: 1px solid var(--divider); border-radius: var(--r-xs);
  background: var(--bg-sunken);
  font-size: calc(11.5px * var(--ts, 1)); line-height: 1.7; color: var(--text-dim);
  pointer-events: none; user-select: none;
}
</style>
