<script setup lang="ts">
/**
 * 设置页「论文问答」面板：**技能 / 对话风格 / 运行限额**，自上而下三块。
 *
 * 为什么单独一区：问答用的模型与翻译用的生成模型**是不同的配置项**（`agent` 类型，
 * 见 `services/agent/model_config.py`）—— 翻译要关思考、低温度、便宜；问答要开思考、
 * 会调工具、吃长上下文。混在一张卡里说，用户就只会看到一个"生成模型"，
 * 然后奇怪为什么问答又贵又慢。模型本体（含视觉读页用的那个）在「模型」区编辑：
 * 那儿有密钥、测试连接、预设，搬一份只读表格进来只会让两处状态打架。
 *
 * ① **技能**：`GET /agent/skills` 列表 + 上传（`.zip`，内含 `<name>/SKILL.md`）/ 启用开关 /
 * 下载 / 卸载。**自带技能（builtin）可以禁用但删不掉**（`DELETE` 回 403，前端干脆不给可用入口，
 * 只留一个灰掉的垃圾桶 + title 说明原因）；禁用的行整体降一档亮度并挂「已禁用」签。
 * ② **对话风格**：提示词里唯一可改的那一段，存 setting 表的 `agent_style`（`PUT /settings`），
 * **留空保存 = 恢复默认**（后端删行，读取点回落到 `agent/prompts.DEFAULT_STYLE`），
 * **只对新对话生效**：风格在建会话那一刻冻结到会话行上（用户口径 2026-10-15）。
 * 版式沿用上一轮：非编辑态只读（点正文进编辑），按钮只在编辑态出现。
 * ③ **运行限额**：六个数字就地可改（`PUT /agent/limits`）。它们是 agent 服务的**启动参数**，
 * 所以保存后只提示"重启 agent 服务后生效"，**绝不自动重启、也不写"立即生效"**
 * （要么骗人，要么悄悄打断正在跑的对话）。
 *
 * ⚠️ 这一组接口后端可能还没上线：`/agent/skills` 与 `/agent/limits` 的**读取失败一律静默
 * 降级成空态 / 只读**，不抛错、不弹红字 —— 设置页不该因为一个可选功能没部署就满屏报错。
 * 但**写操作**（上传 / 切换 / 卸载 / 保存限额）失败必须把后端 message 原样说出来。
 */
import { computed, nextTick, onMounted, reactive, ref } from 'vue';
import { api, downloadBlob } from '@/api/client';
import { reportError, toast } from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import type {
  AgentLimits, AgentLimitsPatch, AgentSkill, AgentSkillsPayload, AgentSkillsUploadResult,
  AgentStatus, SettingsPayload,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import Switch from '@/components/Switch.vue';

/** 与后端 `prompts.STYLE_MAX_CHARS` 同一个数：超了后端会截断，这里先挡在输入框上。 */
const STYLE_MAX = 2000;

const status = ref<AgentStatus | null>(null);

async function loadStatus(): Promise<void> {
  try {
    status.value = await api.get<AgentStatus>('/agent/status');
  } catch {
    status.value = null;                 // 服务没起来也只是一句"问答已关闭"，不报错
  }
}

/* ------------------------------- ① 技能 ------------------------------- */

const skills = ref<AgentSkill[]>([]);
const skillsDir = ref('');
const skillsLoading = ref(false);
/** 成功读到过一次技能表（false = 后端未就绪 → 空态，而不是"确实一个技能都没有"）。 */
const skillsReady = ref(false);
/** 正在进行的技能操作：`upload` / `toggle:<name>` / `download:<name>` / `delete:<name>`。 */
const skillBusy = ref('');
const fileInput = ref<HTMLInputElement | null>(null);

async function loadSkills(): Promise<void> {
  skillsLoading.value = true;
  try {
    const data = await api.get<AgentSkillsPayload>('/agent/skills');
    skills.value = Array.isArray(data?.skills) ? data.skills : [];
    skillsDir.value = String(data?.dir ?? '');
    skillsReady.value = true;
  } catch {
    skills.value = [];                   // 静默降级成空态（见文件头）
    skillsDir.value = '';
    skillsReady.value = false;
  } finally {
    skillsLoading.value = false;
  }
}

function pickSkillFile(): void {
  fileInput.value?.click();
}

/** 上传：`.zip` 里应有 `<name>/SKILL.md`。成功列出新增的名字，失败给后端 message。 */
async function onSkillFile(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';                      // 清掉，同一个文件再选一次也能触发 change
  if (!file) return;
  if (!/\.zip$/i.test(file.name)) {
    toast('只接受 .zip 包（里面放 <技能名>/SKILL.md）', 'warn', 4600);
    return;
  }
  skillBusy.value = 'upload';
  const form = new FormData();
  form.append('file', file);
  try {
    const data = await api.upload<AgentSkillsUploadResult>('/agent/skills/upload', form);
    if (Array.isArray(data?.skills)) {
      skills.value = data.skills;
      skillsReady.value = true;
    } else {
      await loadSkills();
    }
    const added = data?.added ?? [];
    toast(added.length ? `已上传技能：${added.join('、')}` : '上传成功，但没有读到新增技能',
      added.length ? 'ok' : 'warn', 4800);
  } catch (err) {
    toast((err as Error)?.message || '上传技能失败', 'error', 6000);
  } finally {
    skillBusy.value = '';
  }
}

/** 启用 / 禁用：先动本地（开关要立刻跟手），失败回滚。自带技能也允许禁用。 */
async function toggleSkill(skill: AgentSkill, enabled: boolean): Promise<void> {
  const before = skill.enabled;
  skill.enabled = enabled;
  skillBusy.value = `toggle:${skill.name}`;
  try {
    const data = await api.put<AgentSkillsPayload>(
      `/agent/skills/${encodeURIComponent(skill.name)}`, { enabled });
    if (Array.isArray(data?.skills)) skills.value = data.skills;
  } catch (err) {
    skill.enabled = before;
    toast((err as Error)?.message || `${enabled ? '启用' : '禁用'}技能失败`, 'error', 6000);
  } finally {
    skillBusy.value = '';
  }
}

/** 下载：接口回 zip 二进制，走 `downloadBlob`（`request()` 会把响应当 JSON 解析，包会坏）。 */
async function downloadSkill(skill: AgentSkill): Promise<void> {
  skillBusy.value = `download:${skill.name}`;
  try {
    const blob = await downloadBlob(`/agent/skills/${encodeURIComponent(skill.name)}/download`);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${skill.name}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (err) {
    toast((err as Error)?.message || '下载技能失败', 'error', 6000);
  } finally {
    skillBusy.value = '';
  }
}

/** 卸载：不可逆（技能文件真被删掉），二次确认。自带技能没有这个入口。 */
async function removeSkill(skill: AgentSkill): Promise<void> {
  if (!(await askConfirm({
    title: `卸载技能「${skill.name}」？`,
    body: '会从 agent 的技能目录里删掉它，新开的对话不再能用这个技能。',
    danger: true,
    confirmLabel: '卸载',
  }))) return;
  skillBusy.value = `delete:${skill.name}`;
  try {
    const data = await api.del<AgentSkillsPayload>(`/agent/skills/${encodeURIComponent(skill.name)}`);
    skills.value = Array.isArray(data?.skills) ? data.skills : skills.value.filter((s) => s.name !== skill.name);
    toast(`已卸载技能：${skill.name}`, 'ok', 3200);
  } catch (err) {
    toast((err as Error)?.message || '卸载技能失败', 'error', 6000);
  } finally {
    skillBusy.value = '';
  }
}

/* ---------------------------- ② 对话风格（可改的提示词） ---------------------------- */

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

async function loadStyle(): Promise<void> {
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
async function startEditStyle(): Promise<void> {
  if (styleEditing.value) return;
  styleText.value = styleSaved.value;
  styleEditing.value = true;
  await nextTick();
  const el = styleEl.value;
  el?.focus();
  el?.setSelectionRange(el.value.length, el.value.length);
}

/** 退出编辑态：丢掉没保存的改动。 */
function cancelStyle(): void {
  styleText.value = styleSaved.value;
  styleEditing.value = false;
}

/** 保存；`text` 为空串就是**恢复默认**（后端删行，不是存一段空风格）。
 *  恢复默认顺便把默认那段填回编辑框 —— 这就是原来那个「看默认风格」要做的事，
 *  一个按钮干完（用户口径 2026-10-15："看默认风格改成恢复默认"）。 */
async function saveStyle(text: string): Promise<void> {
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

/* ------------------------------- ③ 运行限额 ------------------------------- */

interface LimitFieldDef {
  key: keyof AgentLimitsPatch;
  label: string;
  /** 单位：跟在输入框**右侧**做后缀（不塞进输入框） */
  unit: string;
  min: number;
  max?: number;
  step: number;
}

const LIMIT_FIELDS: LimitFieldDef[] = [
  { key: 'max_rounds', label: '单轮轮数上限', unit: '轮', min: 1, step: 1 },
  { key: 'search_budget', label: '检索预算', unit: '次', min: 0, step: 1 },
  { key: 'deadline_seconds', label: '单轮时限', unit: '秒', min: 1, step: 5 },
  { key: 'context_budget', label: '上下文预算', unit: 'tokens', min: 1000, step: 1000 },
  { key: 'compact_threshold', label: '自动压缩触发', unit: '%', min: 1, max: 100, step: 5 },
  { key: 'attachment_inline_limit', label: '单轮送图上限', unit: '张', min: 1, step: 1 },
];

type LimitKey = keyof AgentLimitsPatch;

const limits = ref<AgentLimits | null>(null);
const limitsLoading = ref(false);
const limitsBusy = ref(false);
/** 保存后那句原地提示（值为空 = 没保存过）。**不提"立即生效"**：这些是启动参数。 */
const limitsHint = ref('');

const limitForm = reactive<Record<LimitKey, number | ''>>({
  max_rounds: '', search_budget: '', deadline_seconds: '',
  context_budget: '', compact_threshold: '', attachment_inline_limit: '',
});
/** 服务端**当前生效**的那一份：用来算"改了没有"。 */
const limitBase = reactive<Record<LimitKey, number>>({
  max_rounds: 0, search_budget: 0, deadline_seconds: 0,
  context_budget: 0, compact_threshold: 0, attachment_inline_limit: 0,
});

function fillLimits(data: AgentLimits): void {
  for (const f of LIMIT_FIELDS) {
    const v = Number(data[f.key]);
    const ok = Number.isFinite(v);
    limitForm[f.key] = ok ? v : '';
    limitBase[f.key] = ok ? v : 0;
  }
}

const limitsDirty = computed(() => LIMIT_FIELDS.some((f) => limitForm[f.key] !== limitBase[f.key]));

async function loadLimits(): Promise<void> {
  limitsLoading.value = true;
  try {
    const data = await api.get<AgentLimits>('/agent/limits');
    if (!data || typeof data.max_rounds !== 'number') throw new Error('接口未就绪');
    limits.value = data;
    fillLimits(data);
  } catch {
    limits.value = null;                 // 静默降级成只读空态（见文件头）
  } finally {
    limitsLoading.value = false;
  }
}

async function saveLimits(): Promise<void> {
  if (!limits.value) return;
  const body: AgentLimitsPatch = {
    max_rounds: 0, search_budget: 0, deadline_seconds: 0,
    context_budget: 0, compact_threshold: 0, attachment_inline_limit: 0,
  };
  for (const f of LIMIT_FIELDS) {
    const v = limitForm[f.key];
    if (v === '' || !Number.isFinite(Number(v))) {
      toast(`「${f.label}」需要一个数字`, 'warn');
      return;
    }
    body[f.key] = Number(v);
  }
  limitsBusy.value = true;
  limitsHint.value = '';
  try {
    const res = await api.put<{ ok?: boolean; restart_required?: boolean }>('/agent/limits', body);
    for (const f of LIMIT_FIELDS) limitBase[f.key] = Number(limitForm[f.key] || 0);
    limitsHint.value = res?.restart_required === false ? '已保存' : '已保存，重启 agent 服务后生效';
  } catch (err) {
    toast((err as Error)?.message || '保存运行限额失败', 'error', 6000);
  } finally {
    limitsBusy.value = false;
  }
}

/* ------------------------------- 载入 ------------------------------- */

async function reloadAll(): Promise<void> {
  await Promise.all([loadStatus(), loadSkills(), loadLimits()]);
}

onMounted(() => {
  void reloadAll();
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
      <span class="tiny faint truncate" v-if="skillsDir" :title="skillsDir">{{ skillsDir }}</span>
    </div>

    <!-- ① 技能：每行 = 名字 + 一行说明 + 自带/外部签 + 启用开关 + 下载 + 卸载 -->
    <section class="card">
      <div class="card-head">
        <AppIcon name="sparkle" :size="14" />技能
        <span class="spacer"></span>
        <button class="btn sm" :disabled="skillBusy === 'upload'" @click="pickSkillFile">
          <AppIcon name="upload" :size="14" />{{ skillBusy === 'upload' ? '上传中…' : '上传技能' }}
        </button>
        <button class="btn sm" :disabled="skillsLoading" @click="reloadAll">
          <AppIcon name="refresh" :size="14" />{{ skillsLoading ? '刷新中…' : '刷新' }}
        </button>
        <!-- 真正的文件入口：藏起来，由上面那个按钮代点（原生 file input 长得不像按钮） -->
        <input ref="fileInput" class="sr-only" type="file" accept=".zip,application/zip"
               tabindex="-1" @change="onSkillFile" />
      </div>
      <div class="card-body">
        <div class="skill-list" v-if="skills.length">
          <div class="skill-row" :class="{ off: !s.enabled }" v-for="s in skills" :key="s.name">
            <div class="skill-main">
              <div class="skill-title">
                <span class="skill-name mono">{{ s.name }}</span>
                <span class="badge dim">{{ s.builtin ? '自带' : '外部' }}</span>
                <span class="badge warn" v-if="!s.enabled">已禁用</span>
              </div>
              <p class="skill-desc">{{ s.description || '（这个技能没写说明）' }}</p>
            </div>
            <div class="skill-ops">
              <Switch :model-value="s.enabled" :disabled="skillBusy === `toggle:${s.name}`"
                      :label="`启用技能 ${s.name}`"
                      @update:model-value="toggleSkill(s, $event)" />
              <button class="btn icon sm" :title="`下载 ${s.name}.zip`"
                      :disabled="skillBusy === `download:${s.name}`" @click="downloadSkill(s)">
                <AppIcon name="download" :size="14" />
              </button>
              <button v-if="!s.builtin" class="btn icon sm danger" :title="`卸载 ${s.name}`"
                      :disabled="skillBusy === `delete:${s.name}`" @click="removeSkill(s)">
                <AppIcon name="trash" :size="14" />
              </button>
              <!-- 自带技能：卸载入口灰掉并说明原因（DELETE 对 builtin 回 403，不给可用入口） -->
              <button v-else class="btn icon sm" disabled title="自带技能不可删除">
                <AppIcon name="trash" :size="14" />
              </button>
            </div>
          </div>
        </div>
        <p class="tiny faint" v-else>
          {{ skillsLoading ? '正在读取技能…'
            : (skillsReady ? '技能目录是空的，上传一个 .zip 就有了。'
              : '还没有读到技能目录（agent 服务未就绪）。') }}
        </p>
      </div>
    </section>

    <!-- ② 对话风格：提示词里唯一可改的一段。非编辑态只读（点正文进编辑），按钮只在编辑态出现。 -->
    <section class="card">
      <div class="card-head">
        <AppIcon name="chat" :size="14" />对话风格
        <span class="spacer"></span>
        <span class="tiny faint">只对新开的对话生效</span>
        <span v-if="styleCustom" class="badge">已自定义</span>
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

    <!-- ③ 运行限额：六个数字就地可改。它们是 agent 服务的启动参数 → 只提示"重启后生效" -->
    <section class="card">
      <div class="card-head">
        <AppIcon name="sliders" :size="14" />运行限额
        <span class="spacer"></span>
        <button class="btn primary sm" :disabled="limitsBusy || !limits || !limitsDirty"
                @click="saveLimits">
          <AppIcon name="check" :size="14" />{{ limitsBusy ? '保存中…' : '保存' }}
        </button>
      </div>
      <div class="card-body">
        <div class="limit-grid" v-if="limits">
          <label class="limit-field" v-for="f in LIMIT_FIELDS" :key="f.key">
            <span class="limit-label">{{ f.label }}</span>
            <span class="limit-control">
              <input type="number" :min="f.min" :max="f.max" :step="f.step"
                     v-model.number="limitForm[f.key]" />
              <i class="limit-unit">{{ f.unit }}</i>
            </span>
          </label>
        </div>
        <p class="tiny faint" v-else>
          {{ limitsLoading ? '正在读取运行限额…' : '运行限额暂不可用（agent 服务未就绪）。' }}
        </p>
        <p class="limit-hint tiny" v-if="limitsHint">{{ limitsHint }}</p>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 三块卡片：间距交给 base.css 的 `.card + .card`（12px），不再叠 gap —— 两处都管会变成 24px。 */
.agent-panel { display: flex; flex-direction: column; }
.sec-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.spacer { flex: 1; }

/* ---------- ① 技能 ---------- */
.skill-list { display: flex; flex-direction: column; }
/* 行与行之间用发丝线分隔（不用卡片套卡片）：第一行不留上边、最后一行不留底 */
.skill-row {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 10px 0; border-top: 1px solid var(--divider);
}
.skill-row:first-child { padding-top: 0; border-top: 0; }
.skill-row:last-child { padding-bottom: 0; }
.skill-main { min-width: 0; flex: 1; }
.skill-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
/* 三档灰阶之一：名字用正文色（禁用的降一档），说明用 fading 的 12px 单行 */
.skill-name { font-size: 13px; font-weight: 600; color: var(--text); overflow-wrap: anywhere; }
.skill-desc {
  margin: 2px 0 0; font-size: 12px; line-height: 1.5; color: var(--text-dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 禁用的行整体降一档亮度（开关与按钮仍保持可读，不然会像"整个坏掉了"） */
.skill-row.off .skill-name { color: var(--text-dim); font-weight: 500; }
.skill-row.off .skill-desc { opacity: .6; }
.skill-ops { display: flex; align-items: center; gap: 6px; flex: none; padding-top: 1px; }

/* ---------- ③ 运行限额：两列网格，≥1200px 留白更松 ---------- */
.limit-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 24px; }
.limit-field { display: grid; gap: 6px; min-width: 0; }
.limit-label { font-size: 12px; color: var(--text-dim); }
.limit-control { display: flex; align-items: center; gap: 8px; min-width: 0; }
.limit-control input {
  width: 100%; max-width: 160px; text-align: right;
  /* 数字等宽：六个数并排时不会左右跳 */
  font-variant-numeric: tabular-nums;
}
/* 单位是输入框**右侧的后缀**，不是占位符也不是塞进框里 */
.limit-unit { flex: none; font-style: normal; font-size: 12px; color: var(--text-faint); }
.limit-hint { margin: 12px 0 0; color: var(--ok); }
@media (min-width: 1200px) {
  .limit-grid { gap: 24px 40px; }
}
@media (max-width: 720px) {
  .limit-grid { grid-template-columns: minmax(0, 1fr); }
}

/* ---------- ② 对话风格 ---------- */
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
