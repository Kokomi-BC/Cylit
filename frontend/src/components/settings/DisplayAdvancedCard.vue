<script setup lang="ts">
/**
 * 设置 · 显示主题 · **高级设置**：动效与外壳偏好 —— 都不常动，但一动就是全局的，
 * 所以收在一张窄卡里、可以折起来，不跟"主题 / 字号"抢视线。
 *
 * 「细腻动画」走 `stores/appearance`：开着时保留全站的过渡与位移；
 * 关掉只是把三个动效时长令牌归零（低配机器 / 晕动症的退路）。开关**默认开**，
 * 措辞也是正向的 —— 设置页不该一上来就劝人关掉动画。
 * 「收起左侧栏」复用 `stores/shellPrefs`：侧栏本来就能收，这里只是给设置页一个入口。
 *
 * **没有「恢复默认」按钮**：两个开关自己就能拨回默认，"缩放"那边也已经有「恢复 100%」，
 * 再放一个总复位按钮只是把同一件事说两遍。
 */
import { ref } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { railCollapsed, toggleRail } from '@/stores/shellPrefs';
import { appearance, setMotion } from '@/stores/appearance';

const open = ref(true);
</script>

<template>
  <section class="card adv">
    <button
      type="button"
      class="adv-head"
      :aria-expanded="open"
      aria-controls="appearance-advanced"
      @click="open = !open"
    >
      <span class="hd">
        <span class="hd-title">高级设置</span>
        <span class="hd-desc">动效与外壳偏好</span>
      </span>
      <span class="spacer" />
      <AppIcon class="chev" :class="{ 'is-open': open }" name="forward" :size="13" />
    </button>

    <div v-show="open" id="appearance-advanced" class="adv-body">
      <div class="row">
        <span class="row-text">
          <span class="row-label">细腻动画</span>
          <span class="row-desc">按钮、卡片与浮层的过渡效果</span>
        </span>
        <button
          type="button"
          class="sw"
          :class="{ on: appearance.motion }"
          role="switch"
          :aria-checked="appearance.motion"
          aria-label="细腻动画"
          data-motion-toggle
          @click="setMotion(!appearance.motion)"
        ><span class="sw-knob" /></button>
      </div>

      <div class="row">
        <span class="row-text">
          <span class="row-label">收起左侧栏</span>
          <span class="row-desc">只留图标轨，主区更宽</span>
        </span>
        <button
          type="button"
          class="sw"
          :class="{ on: railCollapsed }"
          role="switch"
          :aria-checked="railCollapsed"
          aria-label="收起左侧栏"
          data-rail-toggle
          @click="toggleRail()"
        ><span class="sw-knob" /></button>
      </div>

    </div>
  </section>
</template>

<style scoped>
.adv { min-width: 0; overflow: hidden; }

/* 卡头就是一个按钮：整条可点，别让用户去找那个箭头 */
.adv-head {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 11px 14px 9px; border: 0; background: transparent;
  color: var(--text); text-align: left; font-family: inherit; cursor: pointer;
}
.adv-head:hover { background: var(--bg-subtle); }
.adv-head:focus-visible { outline: none; box-shadow: var(--ring); }
.hd { display: grid; gap: 2px; min-width: 0; }
.hd-title { font-size: calc(14px * var(--ts, 1)); font-weight: 650; letter-spacing: -.01em; line-height: 1.3; }
.hd-desc { font-size: calc(11.5px * var(--ts, 1)); line-height: 1.4; color: var(--text-faint); }
.chev { color: var(--text-faint); transition: transform var(--dur) var(--ease); }
.chev.is-open { transform: rotate(90deg); }

.adv-body { padding: 0 14px 10px; border-top: 1px solid var(--divider); }

.row {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 0; min-height: 38px;
}
.row + .row { border-top: 1px solid var(--divider); }
.row-text { display: grid; gap: 1px; min-width: 0; }
.row-label { font-size: calc(12px * var(--ts, 1)); font-weight: 600; }
.row-desc { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }

/* 开关：轨道 34×20、滑钮 14，全用令牌 —— 开=强调色，关=凹槽 + 描边 */
.sw {
  position: relative; flex: none; width: 34px; height: 20px; padding: 0;
  border-radius: 999px; border: 1px solid var(--border-strong);
  background: var(--bg-sunken); cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.sw.on { background: var(--accent); border-color: transparent; }
.sw:focus-visible { outline: none; box-shadow: var(--ring); }
.sw-knob {
  position: absolute; top: 2px; left: 2px; width: 14px; height: 14px;
  border-radius: 999px; background: var(--text-faint);
  transition: transform var(--dur) var(--ease), background var(--dur) var(--ease);
}
.sw.on .sw-knob { transform: translateX(14px); background: var(--text-on-accent); }

/* 行与行之间的线由 .row + .row 给；最后一行就是卡片的下沿，不再补一条"页脚"分隔线 */
</style>
