<script setup lang="ts">
/**
 * 设置页「论文问答」面板（**问答模型卡片**）。
 *
 * 为什么单独一区：问答用的模型与翻译用的生成模型**是不同的配置项**（`agent` 类型，
 * 见 `services/agent/model_config.py`）—— 翻译要关思考、低温度、便宜；问答要开思考、
 * 会调工具、吃长上下文。混在一张卡里说，用户就只会看到一个"生成模型"，
 * 然后奇怪为什么问答又贵又慢。
 *
 * 这里**只读展示 + 指路**：真正的编辑入口在「模型」页（那儿有密钥、测试连接、预设），
 * 复制一份表单到这里只会让两处状态打架。
 */
import { computed, onMounted, ref } from 'vue';
import { api } from '@/api/client';
import { reportError, toast } from '@/stores/app';
import { openTab } from '@/stores/tabs';
import type { AgentStatus } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';

const status = ref<AgentStatus | null>(null);
const loading = ref(false);
const skills = ref<string[]>([]);
const skillsOpen = ref(false);

const model = computed(() => status.value?.model ?? null);
const vision = computed(() => model.value?.vision ?? null);
const limits = computed(() => status.value?.limits ?? null);

/** 参数摘要：只列"会明显改变行为"的那几个（全列出来是一屏 JSON，没人看）。 */
const modelParams = computed(() => {
  const p = (model.value?.parameters ?? {}) as Record<string, any>;
  const out: Array<[string, string]> = [];
  if (p.thinking) out.push(['思考', p.thinking === 'disabled' ? '关' : '开']);
  if (p.reasoning_effort) out.push(['推理强度', String(p.reasoning_effort)]);
  if (p.max_tokens) out.push(['最大输出', `${Math.round(Number(p.max_tokens) / 1024)}K tokens`]);
  if (p.top_p) out.push(['top_p', String(p.top_p)]);
  if (p.temperature !== undefined) out.push(['温度', String(p.temperature)]);
  return out;
});

async function load() {
  loading.value = true;
  try {
    status.value = await api.get<AgentStatus>('/agent/status');
  } catch (err) {
    status.value = null;
    reportError(err, '取问答状态失败');
  } finally {
    loading.value = false;
  }
}

async function toggleSkills() {
  skillsOpen.value = !skillsOpen.value;
  if (!skillsOpen.value || skills.value.length) return;
  try {
    const data = await api.get<{ ok?: boolean; skills?: Array<{ name: string }>; error?: string }>('/agent/skills');
    skills.value = (data.skills ?? []).map(s => s.name);
    if (!skills.value.length) toast(data.error || '还没有加载到技能', 'warn');
  } catch (err) {
    reportError(err, '取技能列表失败');
  }
}

function gotoModels() {
  void openTab({ name: 'models' });
}

onMounted(load);
</script>

<template>
  <div class="agent-panel">
    <div class="sec-actions">
      <span class="badge ok" v-if="status?.running">服务运行中</span>
      <span class="badge warn" v-else-if="status?.enabled">服务未启动</span>
      <span class="badge" v-else>问答已关闭</span>
      <span class="spacer"></span>
      <button class="btn sm" :disabled="loading" @click="load">
        <AppIcon name="refresh" :size="15" />{{ loading ? '读取中…' : '刷新' }}
      </button>
      <button class="btn sm" @click="toggleSkills">
        <AppIcon name="sparkle" :size="15" />已加载技能
      </button>
      <button class="btn primary sm" @click="gotoModels">
        <AppIcon name="model" :size="15" />去模型页配置
      </button>
    </div>

    <div v-if="skillsOpen" class="card">
      <div class="card-head"><AppIcon name="sparkle" :size="14" />已加载技能</div>
      <div class="card-body">
        <p v-if="!skills.length" class="tiny faint">还没有读到技能目录。</p>
        <div v-else class="chips">
          <span v-for="s in skills" :key="s" class="badge mono">{{ s }}</span>
        </div>
      </div>
    </div>

    <!-- ① 问答模型 -->
    <section class="card">
      <div class="card-head">
        <AppIcon name="chat" :size="14" />问答模型
        <span class="spacer"></span>
        <span v-if="model?.usable" class="badge ok">可用</span>
        <span v-else class="badge danger">不可用</span>
      </div>
      <div class="card-body">
        <div class="grid2">
          <div class="kv"><span class="k">模型</span><span class="v mono">{{ model?.model_name || '—' }}</span></div>
          <div class="kv"><span class="k">提供方</span><span class="v">{{ model?.provider || '—' }}</span></div>
          <div class="kv"><span class="k">密钥</span>
            <span class="v">{{ model?.has_key ? '已配置' : '缺失' }}</span>
          </div>
          <div class="kv"><span class="k">配置项</span><span class="v">{{ model?.config_id || '—' }}</span></div>
          <div class="kv wide"><span class="k">接口</span><span class="v mono">{{ model?.base_url || '—' }}</span></div>
        </div>

        <div v-if="modelParams.length" class="chips">
          <span v-for="([k, v]) in modelParams" :key="k" class="badge">{{ k }} {{ v }}</span>
        </div>

        <p v-if="model?.is_mock" class="tiny warn-line">
          <AppIcon name="warning" :size="13" />当前是离线占位模型：问答会明确报错而不是硬答，请在模型页换成真实模型。
        </p>
        <p v-else-if="model?.fallback_to_generation" class="tiny faint">
          未单独配置，当前回落到「生成」的默认模型；建议到模型页单独配一个。
        </p>
        <p v-else-if="!model?.usable" class="tiny warn-line">
          <AppIcon name="warning" :size="13" />没有可用的密钥，问答不会启动。
        </p>
      </div>
    </section>

    <!-- ② 视觉模型（与图片问答不是一回事） -->
    <section class="card">
      <div class="card-head"><AppIcon name="image" :size="14" />视觉模型</div>
      <div class="card-body">
        <div class="grid2">
          <div class="kv"><span class="k">模型</span><span class="v mono">{{ vision?.model_name || '未配置' }}</span></div>
          <div class="kv"><span class="k">密钥</span><span class="v">{{ vision?.has_key ? '已配置' : '缺失' }}</span></div>
        </div>
        <p class="tiny faint">扫描件读页（OCR）用；对话里上传的图片不经过它。</p>
      </div>
    </section>

    <!-- ③ 运行限额 -->
    <section class="card">
      <div class="card-head"><AppIcon name="sliders" :size="14" />运行限额</div>
      <div class="card-body">
        <div class="grid2" v-if="limits">
          <div class="kv"><span class="k">单轮轮数上限</span><span class="v mono">{{ limits.max_rounds }}</span></div>
          <div class="kv"><span class="k">检索预算</span><span class="v mono">{{ limits.search_budget }}</span></div>
          <div class="kv"><span class="k">单轮时限</span><span class="v mono">{{ limits.deadline_seconds }} 秒</span></div>
          <div class="kv"><span class="k">上下文预算</span><span class="v mono">{{ Math.round(limits.context_budget / 1000) }}K tokens</span></div>
          <div class="kv"><span class="k">自动压缩触发</span><span class="v mono">{{ limits.auto_compress_percent }}%</span></div>
          <div class="kv"><span class="k">单轮送图上限</span><span class="v mono">{{ limits.attachment_inline_limit }} 张</span></div>
        </div>
        <p class="tiny faint">
          上下文用到 {{ limits?.auto_compress_percent ?? 90 }}% 时，下一轮提问会先把已谈过的内容压成要点
          （原文仍可回看）；对话本身不限轮数。
        </p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.agent-panel { display: flex; flex-direction: column; }
.sec-actions { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.spacer { flex: 1; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 8px 18px; }
.kv { display: flex; align-items: baseline; gap: 8px; font-size: 13px; min-width: 0; }
.kv .k { flex: none; color: var(--text-faint); font-size: 12px; min-width: 76px; }
.kv .v { color: var(--text); min-width: 0; overflow-wrap: anywhere; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.warn-line { display: flex; align-items: center; gap: 5px; color: var(--warn); margin: 10px 0 0; }
</style>
