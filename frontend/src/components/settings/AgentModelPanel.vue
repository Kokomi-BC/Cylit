<script setup lang="ts">
/**
 * 设置页「论文问答」面板：**技能**占右列，**对话风格 → 运行限额 → Cyli 解释**竖着叠在左列；
 * <1100px 收成单列，顺序仍是技能排最前（它是这个面板的主内容）。
 *
 * 为什么单独一区：问答用的模型与翻译用的生成模型**是不同的配置项**（`agent` 类型，
 * 见 `services/agent/model_config.py`）—— 翻译要关思考、低温度、便宜；问答要开思考、
 * 会调工具、吃长上下文。混在一张卡里说，用户就只会看到一个"生成模型"，
 * 然后奇怪为什么问答又贵又慢。模型本体（含视觉读页用的那个）在「模型」区编辑：
 * 那儿有密钥、测试连接、预设，搬一份只读表格进来只会让两处状态打架。
 *
 * ① **技能**：`GET /agent/skills` 列表 + 上传（`.zip`，内含 `<name>/SKILL.md`）/ 启用开关 /
 * 下载 / 卸载。**自带技能（builtin）可以禁用但删不掉**（`DELETE` 回 403）—— 下载与卸载只给
 * 外部技能，自带技能那行连灰掉的按钮都不摆：一个点不动的图标既不说明什么，还逼用户去读 title。
 * 禁用的行整体降一档亮度并挂「已禁用」签。行内正文用 `SKILL_BLURB` 的大白话（后端 description
 * 是写给模型判断"该不该调"的，不改它），专业版留在 title 上；「自带」是默认态、不标徽章。
 * ② **对话风格**：提示词里唯一可改的那一段，存 setting 表的 `agent_style`（`PUT /settings`），
 * **留空保存 = 恢复默认**（后端删行，读取点回落到 `agent/prompts.DEFAULT_STYLE`），
 * **只对新对话生效**：风格在建会话那一刻冻结到会话行上（用户口径 2026-10-15）。
 * 版式沿用上一轮：非编辑态只读（点正文进编辑），按钮只在编辑态出现。
 * ③ **运行限额**：六个数字就地可改（`PUT /agent/limits`）。它们是 agent 服务的**启动参数**，
 * 所以保存后只提示"重启 agent 服务后生效"，**绝不自动重启、也不写"立即生效"**
 * （要么骗人，要么悄悄打断正在跑的对话）。
 * ④ **联网搜索**：`web-search` 那一行右侧的齿轮，在该行下方展开内嵌设置（提供方 / 密钥 /
 * 返回条数 / 要不要正文 / 抓取上限 + 测试 + 清除密钥）。密钥落盘在服务端
 * `data/secrets/websearch.key`，接口只回掩码 —— 所以输入框**留空 = 本次不改**，
 * 清除是一次单独动作，还要二次确认。
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
  SettingsPayload, WebsearchConfig, WebsearchVerifyResult,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import Switch from '@/components/Switch.vue';

/** 与后端 `prompts.STYLE_MAX_CHARS` 同一个数：超了后端会截断，这里先挡在输入框上。 */
const STYLE_MAX = 2000;

/* ------------------------------- ① 技能 ------------------------------- */

/** 技能的中文名（走查定稿，别自己译）。
 *
 * 技能名是**工具名**（`answer-paper`、`edit-text`…），它既是模型调用时的标识、也是技能目录名，
 * 但摆在设置页上给用户看的该是"这个技能叫什么"。所以主标题走这张表，工具名降级成名字
 * 后面的小号弱色（对日志、翻技能目录时用得上）。**没收录的外部技能**（用户自己传的）
 * 不瞎译，主标题仍用它自己的名字。 */
const SKILL_NAME_ZH: Record<string, string> = {
  'web-search': '联网搜索',
  'web-fetch': '网页阅读',
  'read-figure': '图表解读',
  'read-formula': '公式解读',
  'annotate-text': '标注与笔记',
  'read-page-image': '原图核对',
  'summarize-paper': '论文概览',
  'answer-paper': '论文问答',
  'explain-term': '术语与译法',
  'edit-text': '文本修订',
  'check-translation': '译文核对',
};

/** 技能说明的**人话版**（走查定稿，措辞别动），键与 SKILL_NAME_ZH 一一对应。
 *
 * 为什么不直接用后端的 description：SKILL.md 里那段是写给**模型**判断"该不该调这个技能"的
 * 规则文本，摊在设置页上读起来像文档。这里按名字给一句"这个技能能做什么"；
 * 取不到条目（外部技能）就回落到它自己的 description，专业版仍挂在行内 title 上、悬停可见。 */
const SKILL_BLURB: Record<string, string> = {
  'web-search': '查论文之外的公开信息（官方文档、开源实现、收录情况），给出候选与摘要。',
  'web-fetch': '按链接读一篇网页正文，摘要不够时读原文。',
  'read-figure': '讲清某张图或表画了什么、曲线怎么走、结论是什么。',
  'read-formula': '取出页面上的公式，逐个符号讲清含义与作用。',
  'annotate-text': '按你的要求标出原文、设成颜色，或在某处写一条笔记。',
  'read-page-image': '直接看页面原图，核对版式、公式排版与表格结构。',
  'summarize-paper': '一句话结论、章节脉络与建议阅读顺序。',
  'answer-paper': '需要跨段落取证的问题：贡献、原理、实验、局限。',
  'explain-term': '术语在本论文中的含义与译法，以及全文译法是否统一。',
  'edit-text': '按你的要求改写解析产物或译文、统一术语。',
  'check-translation': '比对原文与译文是否等价、有无漏译，可用中文复述。',
};

/** 技能名里的那一个"联网搜索"，齿轮与内嵌设置都挂在它身上。 */
const WEB_SEARCH_SKILL = 'web-search';

const skills = ref<AgentSkill[]>([]);
const skillsLoading = ref(false);
/** 成功读到过一次技能表（false = 后端未就绪 → 空态，而不是"确实一个技能都没有"）。 */
const skillsReady = ref(false);
/** 正在进行的技能操作：`upload` / `toggle:<name>` / `download:<name>` / `delete:<name>`。 */
const skillBusy = ref('');
const fileInput = ref<HTMLInputElement | null>(null);

/** 渲染顺序：web-search 置顶，其余保持接口给的顺序（后端已按"启用在前 / 名字"排过）。
 *  **只排副本**：开关的乐观更新、上传后的回填都直接动 `skills`，就地 sort 会打乱那份引用。 */
const skillRows = computed<AgentSkill[]>(() => {
  const i = skills.value.findIndex((s) => s.name === WEB_SEARCH_SKILL);
  if (i <= 0) return skills.value;
  const rest = skills.value.slice();
  const [top] = rest.splice(i, 1);
  return [top, ...rest];
});

async function loadSkills(): Promise<void> {
  skillsLoading.value = true;
  try {
    const data = await api.get<AgentSkillsPayload>('/agent/skills');
    skills.value = Array.isArray(data?.skills) ? data.skills : [];
    skillsReady.value = true;
  } catch {
    skills.value = [];                   // 静默降级成空态（见文件头）
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

/* ---------------------------- ②b Cyli 解释的模型 ----------------------------
   划词解释 / 插图解释是"一锤子买卖"（读一段、讲清楚），与侧栏对话的负载不同：
   对话要长上下文与工具编排，解释可以更便宜更快。所以这里让它单独一份。
   两个值都存 setting 表（空串 = 跟随默认），读取点在 backend/services/agent/explain.py，
   消费方是 POST /agent/ask 的 mode=explain（只在调用方没显式指定模型时才套用）。 */

interface ExplainModelOption {
  id: string;
  name?: string;
  model_name?: string | null;
  provider?: string;
  enabled?: boolean;
  is_default?: boolean;
  parameters?: { reasoning_effort?: string; reasoning_levels?: unknown };
}

/** 内置思考档（与对话页那套同一份）；模型自带 reasoning_levels 时以它为准 */
const BUILTIN_EFFORTS = ['off', 'low', 'medium', 'high', 'max'];

const explainModels = ref<ExplainModelOption[]>([]);
const explainModel = ref('');
const explainEffort = ref('');
const explainBusy = ref(false);

const explainModelChoice = computed(() =>
  explainModels.value.find((m) => m.id === explainModel.value) ?? null);
const explainEffortOptions = computed<string[]>(() => {
  const raw = explainModelChoice.value?.parameters?.reasoning_levels;
  if (Array.isArray(raw) && raw.length) {
    const list = raw
      .map((item) => (typeof item === 'string' ? item : String((item as { value?: unknown })?.value ?? '')))
      .filter(Boolean);
    if (list.length) return list;
  }
  return BUILTIN_EFFORTS;
});
/** 该模型自己配的那一档：选「跟随模型」时它说了算，界面上要写出来 */
const explainModelDefaultEffort = computed(() => {
  const v = explainModelChoice.value?.parameters?.reasoning_effort;
  return typeof v === 'string' ? v : '';
});

async function loadExplain(): Promise<void> {
  const [models, settings] = await Promise.all([
    api.get<{ items?: ExplainModelOption[] }>('/models', { model_type: 'agent' })
      .catch(() => ({ items: [] as ExplainModelOption[] })),
    api.get<SettingsPayload>('/settings').catch(() => null),
  ]);
  explainModels.value = (models?.items ?? []).filter((m) => m.enabled !== false);
  const values = (settings as SettingsPayload | null)?.values ?? {};
  explainModel.value = String(values.explain_model_config_id ?? '');
  explainEffort.value = String(values.explain_reasoning_effort ?? '');
}

async function saveExplain(): Promise<void> {
  explainBusy.value = true;
  try {
    const data = await api.put<SettingsPayload>('/settings', {
      explain_model_config_id: explainModel.value,
      explain_reasoning_effort: explainEffort.value,
    });
    const values = data?.values ?? {};
    explainModel.value = String(values.explain_model_config_id ?? explainModel.value);
    explainEffort.value = String(values.explain_reasoning_effort ?? explainEffort.value);
    toast('已保存：之后的 Cyli 解释按这份配置运行', 'ok', 2600);
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 5000);
  } finally {
    explainBusy.value = false;
  }
}

/* ---------------------------- ④ 联网搜索（web-search 技能） ----------------------------
   这几个值属于**技能自己**（agent 进程调 `/agent/websearch/search` 时才用它们），
   与问答模型无关，所以摆在技能行里，而不是再开一张卡片。
   密钥只存在服务端 `data/secrets/websearch.key`，接口只回掩码 —— 前端拿不到明文，
   于是输入框的语义只能是"这次要换成什么"：留空 = 不动那份，清除要单独一次显式动作。 */

interface WsForm {
  provider: string;
  count: number | '';
  need_content: boolean;
  fetch_chars: number | '';
}

/** 与后端默认值同一套（deepseek / 8 条 / 不取正文 / 抓 6000 字）。 */
const WS_DEFAULT: WsForm = { provider: 'deepseek', count: 8, need_content: false, fetch_chars: 6000 };

const wsOpen = ref(false);
/** 配置读到过（false = 接口未就绪，面板只给一句空态，不给能写坏东西的控件）。 */
const wsReady = ref(false);
const wsLoading = ref(false);
const wsBusy = ref(false);
const wsTesting = ref(false);
const wsProviders = ref<{ id: string; label: string }[]>([]);
const wsHasKey = ref(false);
const wsKeyMasked = ref('');
/** 本次要写进去的新密钥；空串 = 不改（后端把空串当"没提供"）。 */
const wsApiKey = ref('');
/** 服务端那一份：用来算"改了没有"。 */
const wsBase = reactive<WsForm>({ ...WS_DEFAULT });
const wsForm = reactive<WsForm>({ ...WS_DEFAULT });
/** 测试结论：HTTP 一律 200，成败只看这个对象，所以它必须留在页面上，不能只弹一条 toast。 */
const wsTest = ref<WebsearchVerifyResult | null>(null);

function num(v: unknown, fallback: number): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

/** 一次填两处：base 只跟随服务端，form 是用户正在编辑的那份。 */
function fillWs(cfg: WebsearchConfig | null | undefined): void {
  wsProviders.value = Array.isArray(cfg?.providers) ? cfg.providers : [];
  wsHasKey.value = cfg?.has_key === true;
  wsKeyMasked.value = String(cfg?.key_masked ?? '');
  const next: WsForm = {
    provider: String(cfg?.provider || WS_DEFAULT.provider),
    count: num(cfg?.count, 8),
    need_content: cfg?.need_content === true,
    fetch_chars: num(cfg?.fetch_chars, 6000),
  };
  Object.assign(wsBase, next);
  Object.assign(wsForm, next);
  wsApiKey.value = '';
  wsReady.value = true;
}

async function loadWs(): Promise<void> {
  wsLoading.value = true;
  try {
    fillWs(await api.get<WebsearchConfig>('/agent/websearch/config'));
  } catch {
    wsReady.value = false;               // 接口没上线就只读空态（见文件头）
  } finally {
    wsLoading.value = false;
  }
}

/** 齿轮只切展开态；第一次展开才去拉配置 —— 没点开就不该多一次请求。 */
function toggleWs(): void {
  wsOpen.value = !wsOpen.value;
  if (wsOpen.value && !wsReady.value) void loadWs();
}

const wsDirty = computed(() =>
  wsApiKey.value.trim() !== ''
  || wsForm.provider !== wsBase.provider
  || wsForm.count !== wsBase.count
  || wsForm.need_content !== wsBase.need_content
  || wsForm.fetch_chars !== wsBase.fetch_chars);

async function saveWs(): Promise<void> {
  if (wsForm.count === '' || wsForm.fetch_chars === '') {
    toast('「返回条数」与「抓取字数上限」都要一个数字', 'warn');
    return;
  }
  wsBusy.value = true;
  wsTest.value = null;                   // 改了配置，上一次的测试结论就不作数了
  try {
    // 只提交**改动过的**字段：后端每个字段都是可选的，把没动过的条数也跟着写一遍
    // 等于替我重新拍一次板，将来默认值变了也不会被旧页面覆盖回去。
    const body: Record<string, unknown> = {};
    if (wsForm.provider !== wsBase.provider) body.provider = wsForm.provider;
    if (wsForm.count !== wsBase.count) body.count = Number(wsForm.count);
    if (wsForm.need_content !== wsBase.need_content) body.need_content = wsForm.need_content;
    if (wsForm.fetch_chars !== wsBase.fetch_chars) body.fetch_chars = Number(wsForm.fetch_chars);
    const fresh = wsApiKey.value.trim();
    if (fresh) body.api_key = fresh;     // 空串 = 本次不改，所以干脆不放进 body
    fillWs(await api.put<WebsearchConfig>('/agent/websearch/config', body));
    toast('联网搜索设置已保存', 'ok', 2600);
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 5000);
  } finally {
    wsBusy.value = false;
  }
}

/** 清除密钥：不可逆（服务端那份文件真被删掉），二次确认。 */
async function clearWsKey(): Promise<void> {
  if (!(await askConfirm({
    title: '清除联网搜索的 API Key？',
    body: '清掉之后 web-search 技能就搜不了东西，要重新填一把才能用。',
    danger: true,
    confirmLabel: '清除',
  }))) return;
  wsBusy.value = true;
  wsTest.value = null;
  try {
    fillWs(await api.put<WebsearchConfig>('/agent/websearch/config', { clear_key: true }));
    toast('已清除联网搜索密钥', 'ok', 3200);
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 5000);
  } finally {
    wsBusy.value = false;
  }
}

/** 测试：后端真跑一次 count=1 的搜索。失败也得**原地**说清楚是哪一步坏了，所以结论进 `wsTest`。 */
async function verifyWs(): Promise<void> {
  wsTesting.value = true;
  wsTest.value = null;
  try {
    wsTest.value = await api.post<WebsearchVerifyResult>('/agent/websearch/verify');
  } catch (err) {
    // 走到这儿是连"跑测试"都没发出去（接口未上线 / 网络），一样留在原地，不弹完就没了
    wsTest.value = { ok: false, state: 'error', detail: (err as Error)?.message ?? String(err), ms: 0 };
  } finally {
    wsTesting.value = false;
  }
}

/* ------------------------------- 载入 ------------------------------- */

async function reloadAll(): Promise<void> {
  await Promise.all([loadSkills(), loadLimits()]);
}

onMounted(() => {
  void reloadAll();
  void loadStyle();
  void loadExplain();
});
</script>

<template>
  <div class="agent-panel">
    <!-- 版式：右列技能（主内容，DOM 里排第一 —— 窄屏单列时它就是第一屏），
         左列竖着叠「对话风格 → 运行限额 → Cyli 解释」三张卡。
         为什么这么分：十来个技能是一行一行读下来的，有实打实的高度；左列三张各自只有几行，
         跟技能并排才不至于在右下角留一大片空白（限额的六格在半宽里仍是两列，放得下）。 -->
    <div class="panel-grid">
      <!-- ① 技能：每行 = 名字 + 两行大白话说明 + 启用开关（下载 / 卸载只给外部技能） -->
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
            <template v-for="s in skillRows" :key="s.name">
              <div class="skill-row" :class="{ off: !s.enabled }">
                <div class="skill-main">
                  <div class="skill-title">
                    <!-- 主标题是中文名；工具名降级成旁边的小号弱色（未收录的外部技能没有中文名，
                         那它本身就是主标题，不再重复一遍） -->
                    <span class="skill-name" :title="s.name">{{ SKILL_NAME_ZH[s.name] || s.name }}</span>
                    <span v-if="SKILL_NAME_ZH[s.name]" class="skill-id mono">{{ s.name }}</span>
                    <!-- 自带是默认态、不标；只有"外部"值得一眼看出，禁用仍要标 -->
                    <span class="badge dim" v-if="!s.builtin">外部</span>
                    <span class="badge warn" v-if="!s.enabled">已禁用</span>
                  </div>
                  <!-- 正文用大白话（没条目的外部技能回落它自己的 description）；
                       专业版仍挂在 title 上，悬停可见 -->
                  <p class="skill-desc" :title="s.description || '（这个技能没写说明）'">
                    {{ SKILL_BLURB[s.name] || s.description || '（这个技能没写说明）' }}
                  </p>
                </div>
                <!-- 顺序：图标按钮在前、开关永远最后一个 —— 开关贴住右边缘，
                     行里多一个齿轮（或少两个图标）都不会把别行的开关推歪 -->
                <div class="skill-ops">
                  <!-- 下载 / 卸载只属于外部技能：自带技能的 DELETE 回 403，灰按钮只是噪音 -->
                  <template v-if="!s.builtin">
                    <button class="btn icon sm" :title="`下载 ${s.name}.zip`"
                            :disabled="skillBusy === `download:${s.name}`" @click="downloadSkill(s)">
                      <AppIcon name="download" :size="14" />
                    </button>
                    <button class="btn icon sm danger" :title="`卸载 ${s.name}`"
                            :disabled="skillBusy === `delete:${s.name}`" @click="removeSkill(s)">
                      <AppIcon name="trash" :size="14" />
                    </button>
                  </template>
                  <!-- web-search 自带联网搜索配置入口：齿轮就地展开，不另开一页 -->
                  <button v-if="s.name === WEB_SEARCH_SKILL" class="btn icon sm"
                          :class="{ primary: wsOpen }" :title="wsOpen ? '收起联网搜索设置' : '联网搜索设置'"
                          :aria-expanded="wsOpen" @click="toggleWs()">
                    <AppIcon name="sliders" :size="14" />
                  </button>
                  <Switch :model-value="s.enabled" :disabled="skillBusy === `toggle:${s.name}`"
                          :label="`启用技能 ${s.name}`"
                          @update:model-value="toggleSkill(s, $event)" />
                </div>
              </div>
              <!-- ④ 内嵌设置：挂在这一行下方展开，跟着技能一起滚动 -->
              <div v-if="s.name === WEB_SEARCH_SKILL && wsOpen" class="ws-panel">
                <p class="ws-hint" v-if="wsLoading">正在读取联网搜索配置…</p>
                <p class="ws-hint" v-else-if="!wsReady">联网搜索设置暂不可用（接口未就绪）。</p>
                <template v-else>
                  <div class="grid cols-2">
                    <label class="field">
                      <span class="field-top">提供方</span>
                      <select v-model="wsForm.provider">
                        <option v-for="p in wsProviders" :key="p.id" :value="p.id">{{ p.label }}</option>
                      </select>
                    </label>
                    <label class="field">
                      <span class="field-top">返回条数</span>
                      <input type="number" min="1" max="50" step="1" v-model.number="wsForm.count" />
                    </label>
                  </div>
                  <label class="field">
                    <span class="field-top">API Key</span>
                    <input type="password" v-model="wsApiKey" autocomplete="off" spellcheck="false"
                           :placeholder="wsHasKey
                             ? `已保存 ${wsKeyMasked || '密钥'}，留空不改`
                             : '粘贴提供方控制台里的密钥'" />
                  </label>
                  <div class="grid cols-2">
                    <label class="field">
                      <span class="field-top">抓取字数上限</span>
                      <input type="number" min="500" max="20000" step="500"
                             v-model.number="wsForm.fetch_chars" />
                    </label>
                    <div class="field">
                      <span class="field-top">需要返回正文</span>
                      <Switch v-model="wsForm.need_content">抓取网页正文</Switch>
                    </div>
                  </div>
                  <div class="row ws-actions">
                    <button class="btn primary sm" :disabled="wsBusy || !wsDirty" @click="saveWs">
                      <AppIcon name="check" :size="15" />{{ wsBusy ? '保存中…' : '保存' }}
                    </button>
                    <button class="btn sm" :disabled="wsTesting" @click="verifyWs">
                      <AppIcon name="play" :size="15" />{{ wsTesting ? '测试中…' : '测试' }}
                    </button>
                    <button class="btn sm danger" :disabled="wsBusy || !wsHasKey" @click="clearWsKey">
                      <AppIcon name="ban" :size="15" />清除密钥
                    </button>
                  </div>
                  <!-- 测试结论留在原地：成败 + 卡在哪一步（state）+ 耗时 + 后端原话（detail）；
                       只弹一条 toast 的话，用户回头想看成因就没了 -->
                  <p v-if="wsTest" class="ws-test" :class="wsTest.ok ? 'ok' : 'bad'">
                    <AppIcon :name="wsTest.ok ? 'check' : 'ban'" :size="13" />
                    <span>{{ wsTest.ok ? '验证通过' : '验证失败' }}</span>
                    <span class="ws-test-meta">{{ wsTest.state }} · {{ wsTest.ms }}ms</span>
                    <span v-if="wsTest.detail" class="ws-test-detail">{{ wsTest.detail }}</span>
                  </p>
                </template>
              </div>
            </template>
          </div>
          <p class="tiny faint" v-else>
            {{ skillsLoading ? '正在读取技能…'
              : (skillsReady ? '技能目录是空的，上传一个 .zip 就有了。'
                : '还没有读到技能目录（agent 服务未就绪）。') }}
          </p>
        </div>
      </section>

      <!-- 左列：两张"读几行、改一下"的短卡竖着叠，谁都不该独占整宽 -->
      <div class="panel-col">
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

        <!-- ②b Cyli 解释：单独一套模型 + 思考强度。留空 = 跟随默认（对话模型 / 模型自己的档）。 -->
        <section class="card">
          <div class="card-head">
            <AppIcon name="sparkle" :size="14" />Cyli 解释
            <span class="spacer"></span>
            <span class="tiny faint">下一轮解释生效</span>
            <button class="btn primary sm" :disabled="explainBusy" @click="saveExplain">
              <AppIcon name="check" :size="15" />{{ explainBusy ? '保存中…' : '保存' }}
            </button>
          </div>
          <div class="card-body">
            <p class="tiny faint explain-hint">
              划词解释 / 插图解释用的模型与思考强度。解释是"读一段、讲清楚"，
              与侧栏对话的负载不同 —— 这里可以单独挑一个更快或更便宜的。
            </p>
            <div class="grid cols-2">
              <label class="field">
                <span class="field-top">模型</span>
                <select v-model="explainModel" data-explain-model>
                  <option value="">跟随对话默认</option>
                  <option v-for="m in explainModels" :key="m.id" :value="m.id">
                    {{ m.name || m.model_name || m.id }}
                  </option>
                </select>
              </label>
              <label class="field">
                <span class="field-top">思考强度</span>
                <select v-model="explainEffort" data-explain-effort>
                  <option value="">跟随模型{{ explainModelDefaultEffort ? `（${explainModelDefaultEffort}）` : '' }}</option>
                  <option v-for="e in explainEffortOptions" :key="e" :value="e">{{ e }}</option>
                </select>
              </label>
            </div>
          </div>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 竖向卡片间距交给 base.css 的 `.card + .card`（12px），不再叠 gap —— 两处都管会变成 24px。
   面板只剩一块网格，它下面没有兄弟卡，所以网格自己不再留底边距。 */
.agent-panel { display: flex; flex-direction: column; }
.spacer { flex: 1; }
/* 右列技能 + 左列（对话风格 / 运行限额 / Cyli 解释）。`align-items: start` 让每张卡按自己的内容高，
   底边不强行拉齐 —— 拉齐反而会在短卡里空出一大截。
   DOM 里技能排第一（它是这个面板的主内容），宽屏靠 order 把左列提到第一格；
   <1100px 单列时 order 归零，技能自然落回最前，窄屏第一屏就是它。 */
.panel-grid {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px; align-items: start;
}
.panel-col { display: flex; flex-direction: column; gap: 12px; min-width: 0; order: -1; }
/* 左列内部靠 gap 排，别再叠一条 `.card + .card` 的上边距（会变成 24px） */
.panel-col > .card + .card { margin-top: 0; }
@media (max-width: 1100px) {
  .panel-grid { grid-template-columns: minmax(0, 1fr); }
  .panel-col { order: 0; }
}

/* ---------- ① 技能 ---------- */
.skill-list { display: flex; flex-direction: column; }
/* 行与行之间用发丝线分隔（不用卡片套卡片）：第一行不留上边、最后一行不留底 */
.skill-row {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 8px 0; border-top: 1px solid var(--divider);
}
.skill-row:first-child { padding-top: 0; border-top: 0; }
.skill-row:last-child { padding-bottom: 0; }
.skill-main { min-width: 0; flex: 1; }
.skill-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
/* 工具名（英文 id）跟在中文名后面，小号 + 弱色：只在"对日志 / 找目录"时才需要，不抢主标题。 */
.skill-id { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); overflow-wrap: anywhere; }
/* 三档灰阶之一：名字用正文色（禁用的降一档），说明用 fading 的 11.5px 两行。
   说明只给两行：半宽卡片里一长串说明会把行撑高，整张卡跟着变高、右边空一片；
   截掉的全文进 `title`（悬停可见），`anywhere` 兜住长技能名这类不带空格的串。 */
.skill-name { font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; color: var(--text); overflow-wrap: anywhere; }
.skill-desc {
  margin: 1px 0 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.45; color: var(--text-dim);
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  overflow: hidden; overflow-wrap: anywhere;
}
/* 禁用的行整体降一档亮度（开关与按钮仍保持可读，不然会像"整个坏掉了"） */
.skill-row.off .skill-name { color: var(--text-dim); font-weight: 500; }
.skill-row.off .skill-id { opacity: .7; }
.skill-row.off .skill-desc { opacity: .6; }
/* 图标按钮在前、开关收尾，整组贴右边缘：开关永远落在同一个 x 上，
   行里有没有齿轮 / 下载 / 卸载都不会把它推歪。 */
.skill-ops { display: flex; align-items: center; justify-content: flex-end; gap: 6px; flex: none; }

/* ---------- ④ 联网搜索：挂在 web-search 那一行下方，用发丝描边盒与"行"区分开 ---------- */
.ws-panel {
  margin: 2px 0 12px; padding: 12px;
  border: 1px solid var(--divider); border-radius: var(--r-sm);
  background: var(--bg-layer);
}
/* 面板里的每一块（网格 / 字段 / 按钮行）之间统一个间距，省得逐个加 margin */
.ws-panel > * + * { margin-top: 10px; }
.ws-hint { margin: 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim); }
.ws-actions { flex-wrap: wrap; }
/* 测试结论：成功绿、失败红；细节退回弱色 —— 一眼看到成败，细看才知道卡在哪一步 */
.ws-test {
  display: flex; align-items: flex-start; gap: 6px; flex-wrap: wrap;
  margin: 10px 0 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.5;
  overflow-wrap: anywhere;
}
.ws-test.ok { color: var(--ok); }
.ws-test.bad { color: var(--danger); }
.ws-test-meta { color: var(--text-faint); font-variant-numeric: tabular-nums; }
.ws-test-detail { color: var(--text-dim); }

/* ②b Cyli 解释：一行说明 + 两个下拉，别让它看起来像又一段要读的正文 */
.explain-hint { margin: 0 0 10px; line-height: 1.6; max-width: 62ch; }

/* ---------- ③ 运行限额：两列网格；半宽卡里 40px 列间距会显得两列贴边，统一用默认的 24px ---------- */
.limit-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 24px; }
.limit-field { display: grid; gap: 6px; min-width: 0; }
.limit-label { font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); }
.limit-control { display: flex; align-items: center; gap: 8px; min-width: 0; }
.limit-control input {
  width: 100%; max-width: 160px; text-align: right;
  /* 数字等宽：六个数并排时不会左右跳 */
  font-variant-numeric: tabular-nums;
}
/* 单位是输入框**右侧的后缀**，不是占位符也不是塞进框里 */
.limit-unit { flex: none; font-style: normal; font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.limit-hint { margin: 12px 0 0; color: var(--ok); }
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
  font: inherit; font-size: calc(13px * var(--ts, 1)); line-height: 1.75; resize: none;
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
  font-size: calc(13px * var(--ts, 1)); line-height: 1.75; white-space: pre-wrap; overflow-wrap: anywhere;
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
