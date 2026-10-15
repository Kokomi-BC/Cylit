<script setup lang="ts">
/**
 * 设置页「论文问答」面板（**问答模型卡片**）。
 *
 * 为什么单独一区：问答用的模型与翻译用的生成模型**是不同的配置项**（`agent` 类型，
 * 见 `services/agent/model_config.py`）—— 翻译要关思考、低温度、便宜；问答要开思考、
 * 会调工具、吃长上下文。混在一张卡里说，用户就只会看到一个"生成模型"，
 * 然后奇怪为什么问答又贵又慢。
 *
 * 模型部分**只读展示 + 指路**：真正的编辑入口在「模型」页（那儿有密钥、测试连接、预设），
 * 复制一份表单到这里只会让两处状态打架。
 *
 * 但**对话风格**（提示词里唯一可改的那一段）就地可编辑：它不属于任何模型，
 * 存的是 setting 表的 `agent_style`（`PUT /settings`），**留空保存 = 恢复默认**
 * （后端删掉那一行，读取点回落到 `agent/prompts.DEFAULT_STYLE`）。
 */
import { computed, onMounted, ref } from 'vue';
import { api } from '@/api/client';
import { reportError, toast } from '@/stores/app';
import { openTab } from '@/stores/tabs';
import type { AgentStatus, SettingsPayload } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';

/** 与后端 `prompts.STYLE_MAX_CHARS` 同一个数：超了后端会截断，这里先挡在输入框上。 */
const STYLE_MAX = 2000;

const status = ref<AgentStatus | null>(null);
const loading = ref(false);
const skills = ref<string[]>([]);
const skillsOpen = ref(false);

/* ---------------------------- 对话风格（可改的提示词） ---------------------------- */
const styleText = ref('');
/** 服务器上**生效**的那一份：用来判断编辑框里有没有改动。 */
const styleSaved = ref('');
const styleCustom = ref(false);
const styleDefault = ref('');
const styleBusy = ref(false);
const styleShowDefault = ref(false);
const styleDirty = computed(() => styleText.value.trim() !== styleSaved.value.trim());

async function loadStyle() {
  try {
    const data = await api.get<SettingsPayload>('/settings');
    const fallback = String(data.defaults?.agent_style ?? '');
    styleDefault.value = fallback;
    styleSaved.value = String(data.values?.agent_style ?? fallback);
    // "改过没有"看的是 setting 表里有没有那一行 —— 用户完全可能把默认那段原样抄进去，
    // 比较文本是判不出"自定义过"的。
    styleCustom.value = Object.prototype.hasOwnProperty.call(data.stored ?? {}, 'agent_style');
    styleText.value = styleSaved.value;
  } catch (err) {
    reportError(err, '取对话风格失败');
  }
}

/** 保存；``text`` 为空串就是**恢复默认**（后端删行，不是存一段空风格）。 */
async function saveStyle(text: string) {
  styleBusy.value = true;
  try {
    const data = await api.put<SettingsPayload>('/settings', { agent_style: text });
    styleSaved.value = String(data.values?.agent_style ?? styleDefault.value);
    styleText.value = styleSaved.value;
    styleCustom.value = !!text.trim();
    toast(text.trim() ? '对话风格已保存，下一句就生效' : '已恢复默认风格');
  } catch (err) {
    reportError(err, '保存对话风格失败');
  } finally {
    styleBusy.value = false;
  }
}

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

onMounted(() => {
  void load();
  void loadStyle();
});
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

    <!-- ① 对话风格：提示词里唯一可改的一段 -->
    <section class="card">
      <div class="card-head">
        <AppIcon name="sparkle" :size="14" />对话风格
        <span class="spacer"></span>
        <span v-if="styleCustom" class="badge">已自定义</span>
        <span v-else class="badge ok">默认</span>
      </div>
      <div class="card-body">
        <textarea v-model="styleText" rows="4" :maxlength="STYLE_MAX" class="style-input"
                  placeholder="例如：你是猫娘助手，说话结尾带「喵」。" />
        <p class="tiny faint">
          改的是"你是谁、什么语气"。<b>规则改不动</b>：只管这一篇论文、跑题直接拒绝、
          引用要带 [ref]、没证据就说没有、改产物要先读后写 —— 这些写死在代码里。
          改完<b>下一句问答就生效</b>；留空保存 = 用回默认。
        </p>
        <div class="style-actions">
          <button class="btn primary sm" :disabled="styleBusy || !styleDirty"
                  @click="saveStyle(styleText)">
            <AppIcon name="check" :size="15" />保存
          </button>
          <button class="btn sm" :disabled="styleBusy || !styleCustom" @click="saveStyle('')">
            <AppIcon name="refresh" :size="15" />恢复默认
          </button>
          <button class="btn sm" @click="styleShowDefault = !styleShowDefault">
            {{ styleShowDefault ? '收起默认' : '看默认风格' }}
          </button>
          <span class="spacer"></span>
          <span class="tiny faint mono">{{ styleText.length }} / {{ STYLE_MAX }}</span>
        </div>
        <pre v-if="styleShowDefault" class="style-default tiny">{{ styleDefault }}</pre>
      </div>
    </section>

    <!-- ② 问答模型 -->
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

    <!-- ③ 视觉模型（与图片问答不是一回事） -->
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

    <!-- ④ 运行限额 -->
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
/* 对话风格编辑框（用户口径 2026-10-15："太丑了，不符合风格"）：
   全局那条 `input, select, textarea` 是给**表单控件**写的（白底 + 深色描边 + 聚焦时一整圈 accent 光环），
   而且**没给宽度** —— `<textarea>` 就按 `cols=20` 撑成一条 300px 的小白框，摆在整宽卡片里格外扎眼。
   这里换成笔记编辑框（`ReaderSidePanel`）同一门语言：整宽 + 淡底 + 发丝描边，
   聚焦只把描边点亮一档（不要 `--ring` 那圈光晕）；高度给足（风格本来就该写好几行）。
   `resize: none` 同笔记编辑框：浏览器自带的右下角斜纹角标是另一个"控件感"的来源，
   要更多空间就往上滚，别为了可拖拽留一个丑角标。 */
.style-input {
  display: block; width: 100%; box-sizing: border-box;
  min-height: 128px; padding: 11px 13px;
  border: 1px solid var(--divider); border-radius: var(--r-sm);
  background: var(--bg-layer); color: var(--text);
  font: inherit; font-size: 13px; line-height: 1.75; resize: none;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
/* 覆盖全局的 `textarea:hover { background: var(--bg-hover) }`（鼠标一过就闪一下灰） */
.style-input:hover { background: var(--bg-layer); border-color: var(--border-strong); }
.style-input:focus {
  outline: none; box-shadow: none; background: var(--bg-elev);
  border-color: color-mix(in srgb, var(--accent) 60%, transparent);
}
.style-input::placeholder { color: var(--text-faint); }
.style-actions { display: flex; align-items: center; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.style-default {
  margin: 10px 0 0; padding: 10px 12px; white-space: pre-wrap; overflow-wrap: anywhere;
  background: var(--bg-sunken); border: 1px solid var(--border); border-radius: 8px;
  color: var(--text-dim); line-height: 1.7;
}
</style>
