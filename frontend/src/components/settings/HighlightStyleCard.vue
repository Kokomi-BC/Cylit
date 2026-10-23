<script setup lang="ts">
/**
 * 设置 · 显示主题 · **高亮效果**：已保存文本标注的两种画法，二选一，全局生效。
 *
 * 为什么在这儿：原来这张位置是「高级设置」（动效 / 收起左侧栏）。那两件事低频，
 * 而且「收起左侧栏」在侧栏底部本来就有入口 —— 更值得占这块地方的是**每篇论文都会看到**
 * 的东西：高亮长什么样。所以整张卡换成高亮画法（用户口径 2026-12）。
 *
 * 实现口径（关键）：这里**不重画一套预览样式**。两张小卡里的示例就是真的 mark.hl，
 * 画法由 base.css 里那组声明决定（html[data-hl-style=underline] 管全局、
 * data-hl-force 管"这张卡固定演某种"）—— 所以示例与阅读器里看到的必然一致。
 *
 * 偏好落 stores/appearance（localStorage，键 'cylit:appearance' 的 highlight 字段），
 * 点一下即时生效并立刻落盘：没有"保存"这一步，也就没有未保存态。
 */
import AppIcon from '@/components/AppIcon.vue';
import { HIGHLIGHT_OPTIONS, appearance, setHighlight } from '@/stores/appearance';
</script>

<template>
  <section class="card hl" data-hl-card>
    <div class="card-head">
      <div class="hd">
        <h3>高亮效果</h3>
        <p>调整论文阅读与文本标注的显示方式</p>
      </div>
    </div>

    <div class="card-body hl-body">
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

          <!-- 示例：真的 mark.hl，画法由 data-hl-force 钉死（与阅读器共用 base.css 那份声明）。
               跨行才看得出差别 —— 荧光笔要贴着行盒铺开，下划线要跟着每一行各自画一条。 -->
          <span class="opt-demo" :data-hl-force="o.value" aria-hidden="true">
            <mark class="hl yellow">外部知识接进上下文之后，模型的回答会更贴题。</mark>其余文字保持原样。
          </span>
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.card-head { padding: 11px 14px 9px; }
.hd { display: grid; gap: 2px; min-width: 0; }
.hd h3 {
  margin: 0; font-size: calc(14px * var(--ts, 1)); font-weight: 650;
  letter-spacing: -.01em; line-height: 1.3;
}
.hd p { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.4; color: var(--text-faint); }

.hl { min-width: 0; display: flex; flex-direction: column; }
/* 卡身自己当容器：两张小卡什么时候该叠成一列，看的是**卡身**多宽，不是视口 */
.hl-body {
  padding: 10px 14px 12px;
  container-type: inline-size;
  display: flex; flex-direction: column;
  flex: 1;
}
.hl-opts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; flex: 1; }

/* 一张可选样式：名字 + 一句说明 + 真示例。选中靠三件事一起说（描边 / 浅底 / 勾），
   不只靠颜色 —— 深色主题或色觉差异下也得看得出选的是哪个。 */
.opt {
  display: flex; flex-direction: column; gap: 5px;
  min-width: 0; padding: 9px 10px 10px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text);
  font-family: inherit; text-align: left; cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.opt:hover { background: var(--bg-subtle); }
.opt.on { border-color: var(--accent); background: var(--accent-soft); }
.opt:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.opt-head { display: flex; align-items: center; gap: 6px; min-width: 0; }
/* 选中圆点：未选是空心圈，选中填强调色 + 白勾（同一个位置，切换时不动） */
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
.opt-desc {
  font-size: calc(11px * var(--ts, 1)); line-height: 1.45; color: var(--text-faint);
}
/* 示例块：顶到最下面（margin-top: auto），两张卡的示例因此**底边对齐** */
.opt-demo {
  margin-top: auto; padding: 7px 9px;
  border: 1px solid var(--divider); border-radius: var(--r-xs);
  background: var(--bg-sunken);
  font-size: calc(12px * var(--ts, 1)); line-height: 1.75; color: var(--text-dim);
  /* 示例是"看效果"，不是可点的标注 */
  pointer-events: none; user-select: none;
}

/* 卡身窄到放不下两栏：叠起来（否则每个示例只剩 100px 出头，跨行看不出来） */
@container (max-width: 340px) {
  .hl-opts { grid-template-columns: minmax(0, 1fr); }
}
</style>
