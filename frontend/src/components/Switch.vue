<script setup lang="ts">
/**
 * Switch —— 可访问的「切换开关」。
 *
 * 为什么里面是**一个真实 `<input type="checkbox">`**（而不是 div + click）：
 * 原生 checkbox 一次把三件事都做对了 ——
 * 1. **键盘**：Tab 能聚焦、Space 能切换，不用自己监听按键；
 * 2. **点文字即切换**：外层是 `<label>`，点插槽里的文案与点轨道等价；
 * 3. **AT 语义**：`role="switch"` + 原生 `checked` 就是 `aria-checked` 的事实源，
 *    读屏播报"开/关"是浏览器给的，不需要我们维护一份可能与视觉不同步的状态。
 * 自己用 div 复刻这三样，等于把已经对的东西重写一遍，且极容易在细节上写歪。
 *
 * 视觉与交互都在这里收口（轨道 36×20、滑块 16、140ms 过渡、`disabled` 变灰），
 * 业务方只负责 `v-model` 与插槽文案；组件**不引入新令牌**，颜色全部取自 `tokens.css`。
 */
withDefaults(defineProps<{
  modelValue: boolean;
  disabled?: boolean;
  /** 无障碍名称：插槽里没有可见文字时（例如只有图标）必须给，否则读屏只能念"复选框"。 */
  label?: string;
}>(), { disabled: false, label: '' });

const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>();

/**
 * 取**原生 checkbox 的 `checked`** 作为新值，而不是取反 `modelValue`。
 *
 * ⚠️ 千万别 emit 一个"无参事件"：`v-model` 展开成 `x = $event`，无参时 `$event` 是
 * `undefined` —— 父组件的值变成 `undefined`（既不是 true 也不是 false），`:checked` 于是永远
 * 渲成未勾选。实测就是这样：点一下 DOM 的 `checked` 真的翻了、`--accent` 却没上来，
 * 看起来像"这个开关点不动"。原生控件的状态本来就在事件目标上，读它最准。
 */
function onChange(ev: Event) {
  emit('update:modelValue', (ev.target as HTMLInputElement).checked);
}
</script>

<template>
  <label class="switch-row" :class="{ disabled }">
    <!-- `sr-only`（base.css 全局工具类）只把视觉藏起来，元素仍在布局流外、仍可聚焦、仍被读屏读到 -->
    <input
      class="sr-only"
      type="checkbox"
      role="switch"
      :checked="modelValue"
      :disabled="disabled"
      :aria-label="label || undefined"
      @change="onChange"
    />
    <span class="switch-track"><span class="switch-knob" /></span>
    <span class="switch-text"><slot /></span>
  </label>
</template>

<style scoped>
/* position: relative 是必需的：里面那枚 .sr-only checkbox 是 position: absolute，
   没有定位祖先时它的包含块是**初始包含块**（视口），于是它既不会被父级的 overflow 裁掉，
   又会把自己的位置算进文档的滚动区 —— 页面因此凭空多出一条浏览器滚动条（Cyli 偏好页实测
   文档高度 1683 vs 视口 980）。定位祖先前移到这一行，它就老老实实待在自己那格里。 */
.switch-row {
  position: relative;
  /* 竖直**居中**对齐：索引管理里那种「标题 + 两行说明」的标签，用 flex-start 会把开关顶在
     第一行，比行中线高 10~11px（实测 cy 1320 vs 行中线 1331）—— 一排开关就歪了。 */
  display: flex; align-items: center; gap: 8px;
  /* 整行都是 label：点文案也能切换（原生行为，无需 JS） */
  cursor: pointer; user-select: none;
}
.switch-row.disabled { opacity: .5; cursor: not-allowed; }
/* 连续几个开关时由组件自己留出行距（`margin-top` 只加在后面的那个上）：
 * 业务方只要把 <Switch> 直接堆在模板里，不用为每个开关另写包装与间距。 */
.switch-row + .switch-row { margin-top: 10px; }

.switch-track {
  flex: none; position: relative;
  width: 36px; height: 20px;
  border-radius: var(--radius-ctl);
  background: var(--bg-sunken); border: 1px solid var(--border-strong);
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.switch-knob {
  position: absolute; top: 1px; left: 1px;
  width: 16px; height: 16px; border-radius: 50%;
  background: var(--bg-elev); box-shadow: var(--shadow-1);
  /* 滑块自己也要写全（不继承轨道的 `transition`）：只列 transform，
   * 这样"滑块 140ms 滑过去、轨道同期换色"两件事各自的时长都能被自检量到。 */
  transition: transform 140ms var(--ease);
}

/* 开启态：轨道转强调色、滑块右移。
 * 轨道可移动宽度 = 36 - 2（边框）- 2（左右各 1px 内缩）- 16（滑块）= 16px。 */
.switch-row input:checked + .switch-track { background: var(--accent); border-color: transparent; }
.switch-row input:checked + .switch-track .switch-knob { transform: translateX(16px); }

/* 焦点可见：描边画在**轨道**上，而真正持有焦点的 checkbox 已被 sr-only 藏起
 * （`:focus-visible` 用相邻兄弟选择器接力，键盘 Tab 过来时轨道才有青绿描边）。 */
.switch-row input:focus-visible + .switch-track {
  border-color: var(--accent); box-shadow: var(--ring);
}

.switch-text { min-width: 0; }

/* 真正被藏起来的 checkbox。
 *
 * ⚠️ 必须显式再收一次尺寸：全局 `input[type="checkbox"]` 给了 **15×15 + cursor:pointer**
 * （见 base.css），而 `.sr-only` 也是单类选择器 —— 两者同权重，**后者会赢**，于是这个"看不见的"
 * checkbox 实际是一个 15×15、被 `clip` 裁空的方块。视觉上确实看不见（无伤），但它的
 * `cursor:pointer` 会盖住轨道左侧（少一点"整行可点"的观感），而且靠"读者记得两级级联"来正确
 * 呈现太脆。这里用 `.switch-row > input.sr-only` 明确压住它，1×1 + `pointer-events: none`。 */
.switch-row > input.sr-only {
  width: 1px; height: 1px;
  cursor: inherit;
  pointer-events: none;     /* 点的是整行 label，不是这个 1px 的盒子 */
}
</style>
