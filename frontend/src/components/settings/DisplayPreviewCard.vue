<script setup lang="ts">
/**
 * 设置 · 显示主题 · **预览效果**：一块"界面里的样子"，标题 / 正文 / 标签 / 按钮各来一个。
 *
 * 这里的字号全部走 `calc(Npx * var(--ts, 1))`、字面走 `--display` / `--sans`，
 * 所以调上面的缩放、分类字号或字体族，这块当场跟着变 —— 不需要再写一套预览逻辑。
 * 正文里那句 **真的 `mark.hl`** 同理：它就是阅读器里那枚标注本体（同一份 base.css 声明），
 * 「高亮效果」一切换，这块立刻跟着换画法 —— 这是"预览"，不是画了一张像高亮的图。
 *
 * ⚠️ 整块 `aria-hidden` + `pointer-events: none`：它是**示例**，按钮点了不该真的发生什么。
 * 做一个点了有反应的假按钮，比不做预览更糟。
 *
 * 2026-12 用户口径（最终版式）：这一栏只有它一张卡 —— 样例 + 「当前配色」都并进来，
 * 分类字号与高亮效果搬到右栏（裸块），所以这里只留「看」的部分。
 * 含义没变：字号、主题色、标注画法一改，这块当场跟着变。
 */
import ThemePaletteCard from './ThemePaletteCard.vue';
</script>

<template>
  <section class="card preview">
    <div class="card-head">
      <div class="hd">
        <h3>预览效果</h3>
        <p>字号、配色与标注画法改完，这里当场跟着变</p>
      </div>
    </div>

    <div class="card-body preview-body">
      <div class="sample" aria-hidden="true" data-preview>
          <div class="sample-top">
            <span class="sample-title">量子纠缠与局域性：一个可检验的视角</span>
            <span class="pv-tag is-accent">论文</span>
          </div>
          <p class="sample-text">
            这一段用来看字号：正文按「分类字号 · 正文」渲染，
            <mark class="hl yellow" data-preview-hl>标了黄的句子</mark>与
            <mark class="hl" style="--hl-bg:#7ec8ff66;--hl-ink:#2f7fd4" data-preview-hl-custom>自定义色</mark>
            就是阅读器里那枚标注本体 —— 换高亮效果会当场跟着变。
          </p>
          <div class="sample-tags">
            <span class="pv-tag is-accent">摘要</span>
            <span class="pv-tag is-ok">已索引</span>
            <span class="pv-tag">方法</span>
            <span class="pv-tag">实验</span>
          </div>
          <div class="sample-actions">
            <span class="pv-btn is-primary">开始阅读</span>
            <span class="pv-btn">加入收藏</span>
            <span class="pv-link">查看详情 ›</span>
          </div>
      </div>

      <ThemePaletteCard bare />
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

.preview { min-width: 0; }
/* 样例在上、「当前配色」在下：两块之间那条细线由 ThemePaletteCard 自己画 */
.preview-body { display: flex; flex-direction: column; gap: 12px; padding: 12px 14px 14px; }

.sample {
  display: grid; gap: 7px; padding: 11px 14px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-sunken);
  pointer-events: none; user-select: none;
}
.sample-top { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.sample-title {
  min-width: 0; font-family: var(--display);
  font-size: calc(16px * var(--ts, 1)); font-weight: 650;
  letter-spacing: -.015em; line-height: 1.35;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sample-text {
  margin: 0; font-size: calc(12.5px * var(--ts, 1)); line-height: 1.65; color: var(--text-dim);
}
.sample-tags { display: flex; flex-wrap: wrap; gap: 5px; }
.pv-tag {
  padding: 2px 7px; border-radius: 5px; white-space: nowrap;
  background: var(--bg-elev); color: var(--text-dim);
  border: 1px solid var(--border);
  font-size: calc(10.5px * var(--ts, 1)); line-height: 1.5;
}
.pv-tag.is-accent { background: var(--accent-soft); color: var(--accent-text); border-color: transparent; }
.pv-tag.is-ok { background: var(--ok-soft); color: var(--ok); border-color: transparent; }

.sample-actions { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.pv-btn {
  display: inline-flex; align-items: center; height: 26px; padding: 0 10px;
  border-radius: var(--radius-ctl); border: 1px solid var(--border);
  background: var(--bg-elev); color: var(--text);
  font-size: calc(11.5px * var(--ts, 1)); font-weight: 500;
}
.pv-btn.is-primary { background: var(--accent); color: var(--text-on-accent); border-color: transparent; }
.pv-link { color: var(--accent-text); font-size: calc(11.5px * var(--ts, 1)); }
</style>
