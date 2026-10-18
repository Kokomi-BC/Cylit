<script setup lang="ts">
/**
 * 设置页「论文问答」面板（对话风格 / 运行限额 / 已加载技能）。
 *
 * 为什么单独一区：问答用的模型与翻译用的生成模型**是不同的配置项**（`agent` 类型，
 * 见 `services/agent/model_config.py`）—— 翻译要关思考、低温度、便宜；问答要开思考、
 * 会调工具、吃长上下文。混在一张卡里说，用户就只会看到一个"生成模型"，
 * 然后奇怪为什么问答又贵又慢。
 *
 * 模型本体（含视觉读页用的那个）在「模型」区编辑：那儿有密钥、测试连接、预设，
 * 搬一份只读表格进来只会让两处状态打架。
 *
 * **对话风格**（提示词里唯一可改的那一段）就地可编辑：它不属于任何模型，
 * 存的是 setting 表的 `agent_style`（`PUT /settings`），**留空保存 = 恢复默认**
 * （后端删掉那一行，读取点回落到 `agent/prompts.DEFAULT_STYLE`）。
 * **只对新对话生效**（用户口径 2026-10-15）：风格在建会话那一刻冻结到会话行上，
 * 之后的改动追不到已有会话 —— 否则一次改动会把所有历史会话的 system 前缀缓存同时打掉。
 * 版式：**非编辑态只读**（点正文进编辑），保存 / 恢复默认 / 取消只在编辑态出现；
 * 恢复默认顺便把默认那段填回编辑框（原来那个「看默认风格」的活它一起干了）。
 */
import { computed, nextTick, onMounted, ref } from 'vue';
import { api } from '@/api/client';
import { reportError, toast } from '@/stores/app';
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
/** 编辑态：只有进了编辑态才显示按钮（用户口径 2026-10-15："按钮在未处于编辑状态时隐藏"）。
 *  入口是**点正文**（不是按钮）—— 非编辑态一个按钮都不摆。 */
const styleEditing = ref(false);
const styleEl = ref<HTMLTextAreaElement | null>(null);
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

/** 进编辑态：先让编辑框等于"服务器上那一份"，光标停在末尾。 */
async function startEditStyle() {
  if (styleEditing.value) return;
  styleText.value = styleSaved.value;
  styleEditing.value = true;
  await nextTick();
  const el = styleEl.value;
  el?.focus();
  el?.setSelectionRange(el.value.length, el.value.length);
}

/** 退出编辑态：丢掉没保存的改动。 */
function cancelStyle() {
  styleText.value = styleSaved.value;
  styleEditing.value = false;
}

/** 保存；``text`` 为空串就是**恢复默认**（后端删行，不是存一段空风格）。
 *  恢复默认顺便把默认那段填回编辑框 —— 这就是原来那个「看默认风格」要做的事，
 *  一个按钮干完（用户口径 2026-10-15："看默认风格改成恢复默认"）。 */
async function saveStyle(text: string) {
  styleBusy.value = true;
  try {
    const data = await api.put<SettingsPayload>('/settings', { agent_style: text });
    styleSaved.value = String(data.values?.agent_style ?? styleDefault.value);
    styleText.value = styleSaved.value;
    styleCustom.value = !!text.trim();
    styleEditing.value = false;
    toast(text.trim() ? '对话风格已保存，新开的对话生效' : '已恢复默认风格，新开的对话生效');
  } catch (err) {
    reportError(err, '保存对话风格失败');
  } finally {
    styleBusy.value = false;
  }
}

const limits = computed(() => status.value?.limits ?? null);

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

    <!-- ① 对话风格：提示词里唯一可改的一段。
         非编辑态只读（点正文进编辑），按钮只在编辑态出现。 -->
    <section class="card">
      <div class="card-head">
        <AppIcon name="sparkle" :size="14" />对话风格
        <span class="spacer"></span>
        <span class="tiny faint">只对新对话生效</span>
        <span v-if="styleCustom" class="badge">已自定义</span>
        <span v-else class="badge ok">默认</span>
      </div>
      <div class="card-body">
        <p v-if="!styleEditing" class="style-view" title="点击编辑"
           role="button" tabindex="0"
           @click="startEditStyle"
           @keydown.enter.prevent="startEditStyle"
           @keydown.space.prevent="startEditStyle">{{ styleSaved }}</p>
        <template v-else>
          <textarea ref="styleEl" v-model="styleText" rows="4" :maxlength="STYLE_MAX"
                    class="style-input" @keydown.esc="cancelStyle"
                    placeholder="例如：你是猫娘助手，说话结尾带「喵」。" />
          <div class="style-actions">
            <button class="btn primary sm" :disabled="styleBusy || !styleDirty"
                    @click="saveStyle(styleText)">
              <AppIcon name="check" :size="15" />保存
            </button>
            <button class="btn sm" :disabled="styleBusy || (!styleCustom && !styleDirty)"
                    @click="saveStyle('')">
              <AppIcon name="refresh" :size="15" />恢复默认
            </button>
            <button class="btn sm" :disabled="styleBusy" @click="cancelStyle">取消</button>
            <span class="spacer"></span>
            <span class="tiny faint mono">{{ styleText.length }} / {{ STYLE_MAX }}</span>
          </div>
        </template>
      </div>
    </section>

    <!-- ② 运行限额 -->
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
/* 非编辑态：同一只盒子里放**读**的正文，点一下进编辑（用户口径 2026-10-15：
   "按钮在未处于编辑状态时隐藏" —— 所以入口是正文本身，不是一个"编辑"按钮）。
   `cursor: text` 而不是 pointer：它本来就是"点进输入框"的手感；
   `min-height` 与输入框取同一个数，进出编辑态时卡片不会跳一下。 */
.style-view {
  margin: 0; padding: 11px 13px; box-sizing: border-box; min-height: 128px;
  border: 1px solid var(--divider); border-radius: var(--r-sm);
  background: var(--bg-layer); color: var(--text);
  font-size: 13px; line-height: 1.75; white-space: pre-wrap; overflow-wrap: anywhere;
  cursor: text;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.style-view:hover { border-color: var(--border-strong); background: var(--bg-elev); }
.style-view:focus-visible {
  outline: none;
  border-color: color-mix(in srgb, var(--accent) 60%, transparent);
}
.style-actions { display: flex; align-items: center; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
</style>
