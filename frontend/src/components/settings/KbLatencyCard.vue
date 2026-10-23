<script setup lang="ts">
/**
 * 「检索耗时趋势」—— 目前是一张**说明性占位卡**。
 *
 * 为什么先摆一张空卡而不是画一条曲线：检索耗时根本没有落库。后端现在没有任何
 * "这次检索花了多久"的记录，前端能拿到的只有答案本身。这时候画出来的曲线只可能是
 * 编的，而一条编的曲线比一块空白更糟——用户会拿它判断系统快不快、要不要调参数。
 *
 * 所以这张卡只干一件事：用**两行**把"缺什么"说清楚——一句结论，一句要什么数据
 * （一张检索日志表 ts / mode / total_ms / stage_ms，以及"平均耗时 + P95"这个聚合口径）。
 * 之前是三段话，空状态卡比旁边画着曲线的卡还高，一排三张里它占掉半屏又什么都没画；
 * 现在压到 200 以内，与趋势卡、类型分布卡同排时高度由另外两张决定。
 *
 * 等接口与表就位，把这里换成 KbTrendCard 那套 SVG 画法即可，卡片位置与高度都不用再动。
 */
import AppIcon from '@/components/AppIcon.vue';
</script>

<template>
  <section class="card latency">
    <div class="card-head">
      <AppIcon name="gauge" :size="15" /><span class="ttl">检索耗时趋势</span>
      <span class="spacer"></span>
      <span class="badge dim">暂无数据</span>
    </div>

    <div class="card-body tight">
      <div class="empty-state lat-empty">
        <!-- 这里原来挂的是 gauge（一段圆弧 + 指针），空状态里就一个孤零零的圆弧，
             看着像没转完的 spinner。这是"没有数据"，不是"正在加载"：换成 ban（⊘）。
             图标旁不加任何动画 —— 卡头已经写了「暂无数据」，图标只需要表意。 -->
        <span class="es-icon"><AppIcon name="ban" :size="18" /></span>
        <span class="es-title">后端还没有记录检索耗时</span>
        <span class="es-hint tiny faint lat-hint">
          要画出这条趋势，需要一张检索日志表（ts / mode / total_ms / stage_ms），
          并给出平均耗时与 P95 的聚合口径
        </span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ttl { font-size: calc(13.5px * var(--ts, 1)); }
.latency { min-width: 0; }
.latency .card-head { flex-wrap: wrap; row-gap: 4px; }

/* 空状态只占"图标 + 一句结论 + 一句要什么"：padding 与间距都收一档，
   卡整体压到 200 以内；一排三张时它不该是最高那张。 */
.lat-empty { min-height: 0; justify-content: center; padding: 6px 8px; gap: 4px; }
/* 第二行让位给 .tiny.faint 的档次：全局 `.empty-state .es-hint` 是 12.5px / --text-dim，
   作用域内把选择器提一档（0,3,0）才盖得住它 */
.lat-empty .es-hint.lat-hint {
  max-width: 46ch;
  font-size: calc(11px * var(--ts, 1));
  line-height: 1.5;
  color: var(--text-faint);
  /* 窄卡里这句话会折成三行，最后一行只剩"口径"两个字 —— pretty 让断行均匀些
     （不支持的浏览器只是照常折行，不影响高度与字号） */
  text-wrap: pretty;
}
</style>
