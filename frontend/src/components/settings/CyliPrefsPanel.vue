<script setup lang="ts">
/**
 * 设置页「Cyli 偏好」（原「论文问答」，2026-12 定名并与面板内部一起重做）：
 * **对话偏好**（风格预设 + 自定义指令 + 回复长度）与**技能能力**（已接入技能的列表）两块主内容，
 * 后面跟两张只动一处的小卡：运行上限、Cyli 解释。
 *
 * 版式（2026-12 重做）：一条头部（简介 + 恢复默认 / 保存）→ 上方三组**横向**并排
 * （对话偏好 / 运行上限 / Cyli 解释）→ 下方「技能能力」横跨整幅、单列纵向一行一个技能。
 * 为什么三组横排而技能竖排：三组各自只有 2–5 个短控件，竖着排三屏才轮到技能；
 * 技能是十几行等结构的清单，只有整幅单列才读得下来（多列网格会让每行都窄到放不下操作）。
 * 为什么不再给每组每行套白卡：十来个技能叠出十几层白盒，底色与投影把"分组"这件事
 * 表达成了一堆盒子；分组靠标题 + 留白 + 一条发丝线就够了。
 *
 * ① **对话偏好**：风格存 agent_style_preset（default / professional / concise / plain / custom），
 *    选"自定义"才展开的指令框存 agent_style（<=2000 字，后端 prompts.STYLE_MAX_CHARS）；
 *    回复长度存 agent_reply_length（short / normal / detailed）。三个键都走 PUT /settings。
 *    **只发改过的那几个键**，"没动过就不发请求"是这一页的铁律（沿用原对话风格卡的范式）。
 *    **只对新对话生效**：风格在建会话那一刻冻结到会话行上（用户口径 2026-10-15）。
 * ② **技能能力**：GET /agent/skills 列表 + 上传（.zip，内含 <name>/SKILL.md）/ 启用开关 /
 *    下载 / 卸载 / 查看定义原文。**自带技能（builtin）可以禁用但删不掉**（DELETE 回 403）——
 *    下载与卸载只给外部技能，自带技能那行连灰掉的按钮都不摆。
 *    行内说明**直接来自 SKILL.md 的 description**（客观定义原文，可能是英文，照抄不改写）：
 *    以前这里是手写的大白话，等于前端替后端吹能力，还比真实行为乐观。
 * ③ **运行上限**：五个数字就地可改（PUT /agent/limits）。它们是 agent 服务的**启动参数**，
 *    所以保存后只提示"重启 agent 服务后生效"，**绝不自动重启、也不写"立即生效"**。
 * ④ **Cyli 解释**：划词解释 / 插图解释单独一套模型与思考强度（explain_model_config_id /
 *    explain_reasoning_effort，空串 = 跟随默认）。解释是"读一段、讲清楚"，负载与侧栏对话不同。
 * ⑤ **联网搜索**：web-search 那一行右侧的齿轮，在该行下方展开内嵌设置。密钥落盘在服务端
 *    data/secrets/websearch.key，接口只回掩码 —— 输入框**留空 = 本次不改**，清除是单独动作 + 二次确认。
 *
 * ⚠️ 降级的两条路（都不许弹红字、不许摆假状态）：
 *    - **读**：/agent/skills、/agent/limits、/agent/websearch/config 读取失败一律静默降级成空态/只读；
 *      GET /settings 里没有 agent_style_preset / agent_reply_length（后端还没接上）时，
 *      照常渲染成"默认 / 适中"，见 fillPrefs()。
 *    - **写**：写操作失败必须把后端 message 原样说出来；偏好保存若回 400/404，说明后端还没有这两个键，
 *      明确提示"还没接上"并把界面**退回保存前那一份**（不许假装成功），见 savePrefs()。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { api, downloadBlob } from '@/api/client';
import { reportError, toast } from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import { BUILTIN_EFFORTS } from '@/utils/reasoning';
import type {
  AgentLimits, AgentLimitsPatch, AgentSkill, AgentSkillsPayload, AgentSkillsUploadResult,
  SettingsPayload, WebsearchConfig, WebsearchVerifyResult,
} from '@/api/types';
import {
  PREF_DEFAULT_STATE, PREF_LENGTH_OPTIONS, PREF_STYLE_MAX, PREF_STYLE_OPTIONS,
  isUnsupportedSettingError, normalizeReplyLength, normalizeStylePreset, readSkillMdFromZip,
} from '@/components/settings/cyliPrefs';
import type { CyliPrefsPatch, CyliPrefsState } from '@/components/settings/cyliPrefs';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
import Switch from '@/components/Switch.vue';

/* ------------------------------- ① 对话偏好 ------------------------------- */

/** 表单（用户正在改的那一份）与基线（服务端**当前生效**的那一份）分开，
 *  否则"改了没有"这件事没法判断 —— 而它决定了保存按钮亮不亮、发不发请求。 */
const pref = reactive<CyliPrefsState>({ ...PREF_DEFAULT_STATE });
const prefBase = reactive<CyliPrefsState>({ ...PREF_DEFAULT_STATE });
const prefBusy = ref(false);

const prefDirty = computed(() =>
  pref.preset !== prefBase.preset
  || pref.length !== prefBase.length
  // 自定义指令只在自定义分支下参与比较：切走时那段文本还留在框里，但它已经不生效了
  || (pref.preset === 'custom' && pref.style.trim() !== prefBase.style.trim()));

/** 「恢复默认」可点与否：服务端那一份已经不是出厂态才需要它。 */
const prefCanReset = computed(() =>
  prefBase.preset !== PREF_DEFAULT_STATE.preset
  || prefBase.length !== PREF_DEFAULT_STATE.length
  || prefBase.style !== PREF_DEFAULT_STATE.style);

/** 把 GET /settings 的结果摊进表单与基线。
 *  **后端没接上那两个新键时，这里就是它该有的样子**：缺字段 → 默认 / 适中，
 *  不弹错、不摆「不可用」（用户看到的只是"还没设置过"）。
 *
 *  ⚠️ 判"自定义"看的是 **stored 里有没有 agent_style 这一行**，不看文本空不空：
 *  GET /settings 的 values.agent_style 回的是**当前生效的那一段**，用户没设过时它就是
 *  代码里的 DEFAULT_STYLE（一段非空的口吻描述）。拿"文本非空"当自定义的判据，
 *  会把每个默认用户都显示成"已自定义"。这一条与老对话风格卡的判据同源。 */
function fillPrefs(data: SettingsPayload | null): void {
  const values = data?.values ?? {};
  const defaults = data?.defaults ?? {};
  const stored = data?.stored ?? {};
  const rawStyle = String(values.agent_style ?? '');
  const hasCustom = Object.prototype.hasOwnProperty.call(stored, 'agent_style')
    && rawStyle.trim() !== '';
  const preset = normalizeStylePreset(
    values.agent_style_preset ?? defaults.agent_style_preset,
    hasCustom ? 'custom' : 'default',
  );
  const style = preset === 'custom' ? rawStyle : '';
  const length = normalizeReplyLength(values.agent_reply_length ?? defaults.agent_reply_length);
  Object.assign(pref, { preset, style, length });
  Object.assign(prefBase, { preset, style, length });
}

/** 保存成功后把响应回填成新的基线；响应里没带的键就用"刚发出去的那一份"，
 *  因为后端可能只回 ok（那时把表单当成空串会当场把用户写的东西抹掉）。 */
function commitPrefs(data: SettingsPayload | null): void {
  const values = data?.values ?? {};
  const preset = normalizeStylePreset(values.agent_style_preset, pref.preset);
  const style = preset === 'custom' ? String(values.agent_style ?? pref.style.trim()) : '';
  const length = normalizeReplyLength(values.agent_reply_length, pref.length);
  Object.assign(pref, { preset, style, length });
  Object.assign(prefBase, { preset, style, length });
}

/** 退回保存前那一份（保存失败时用：界面不许停在"看起来存上了"的状态）。 */
function revertPrefs(): void {
  Object.assign(pref, { ...prefBase });
}

async function loadPrefs(): Promise<void> {
  try {
    const data = await api.get<SettingsPayload>('/settings');
    fillPrefs(data);
    // 顺手回填 Cyli 解释那两个下拉 —— 同一份 GET /settings，不必再打一次
    const values = data?.values ?? {};
    explainModel.value = String(values.explain_model_config_id ?? '');
    explainEffort.value = String(values.explain_reasoning_effort ?? '');
    explainBase.value = { model: explainModel.value, effort: explainEffort.value };
  } catch (err) {
    reportError(err, '取 Cyli 偏好失败');
  }
}

async function savePrefs(): Promise<void> {
  if (pref.preset === 'custom' && !pref.style.trim()) {
    toast('选「自定义」需要填一段指令，否则等于没说清要什么样的风格', 'warn', 4800);
    return;
  }
  const body: CyliPrefsPatch = {};
  if (pref.preset !== prefBase.preset) body.agent_style_preset = pref.preset;
  if (pref.preset === 'custom') {
    if (pref.style.trim() !== prefBase.style.trim()) body.agent_style = pref.style.trim();
  } else if (prefBase.style) {
    // 从自定义切到预设：把那段指令清掉，免得"选了严谨专业、旧指令还在下面生效"两头打架
    body.agent_style = '';
  }
  if (pref.length !== prefBase.length) body.agent_reply_length = pref.length;
  if (!Object.keys(body).length) return;          // 没改动就不发请求（这一页的铁律）

  prefBusy.value = true;
  try {
    commitPrefs(await api.put<SettingsPayload>('/settings', body));
    toast('Cyli 偏好已保存，新开的对话生效', 'ok', 3000);
  } catch (err) {
    revertPrefs();
    if (isUnsupportedSettingError(err)) {
      toast('后端还没接上该设置项（agent_style_preset / agent_reply_length），这次没有保存',
        'error', 6400);
    } else {
      reportError(err, '保存 Cyli 偏好失败');
    }
  } finally {
    prefBusy.value = false;
  }
}

/** 恢复默认 = 把表单摆回出厂态并立即保存（沿用原对话风格卡的范式：一个动作干完，不留"再点保存"）。 */
async function resetPrefs(): Promise<void> {
  if (!prefCanReset.value) return;
  Object.assign(pref, { ...PREF_DEFAULT_STATE });
  await savePrefs();
}

/* ------------------------------- ② 技能能力 ------------------------------- */

/** 技能的中文名（走查定稿，别自己译）。
 *
 * 技能名是**工具名**（answer-paper、edit-text…），它既是模型调用时的标识、也是技能目录名，
 * 但摆在设置页上给用户看的该是"这个技能叫什么"。所以主标题走这张表，工具名降级成名字
 * 后面的小号弱色（对日志、翻技能目录时用得上）。**没收录的外部技能**（用户自己传的）
 * 不瞎译，主标题仍用它自己的名字。
 *
 * 警告：这张表**只放名字**：说明一律用后端 description（SKILL.md 的定义原文）。
 * 以前这里还给每个技能手写一句"人话版"，那是前端替后端吹能力 —— 技能改了行为，
 * 这句手写说明不会跟着改，用户看到的就是一句越来越乐观的假话。 */
const SKILL_NAME_ZH: Record<string, string> = {
  'web-search': '联网搜索',
  'web-fetch': '网页阅读',
  'read-text': '正文提取',
  'read-figure': '图表原图',
  'read-formula': '公式提取',
  'annotate-text': '标注与笔记',
  'read-page-image': '整页原图',
  'search-paper': '论文检索',
  'reader-state': '阅读状态',
  'summarize-paper': '全文导读',
  'answer-paper': '论文问答',
  'explain-term': '术语与译法',
  'edit-text': '文本修订',
};

/** 技能名里的那一个"联网搜索"，齿轮与内嵌设置都挂在它身上。 */
const WEB_SEARCH_SKILL = 'web-search';

const skills = ref<AgentSkill[]>([]);
const skillsLoading = ref(false);
/** 成功读到过一次技能表（false = 后端未就绪 → 空态，而不是"确实一个技能都没有"）。 */
const skillsReady = ref(false);
/** 正在进行的技能操作：upload / toggle:<name> / download:<name> / delete:<name>。 */
const skillBusy = ref('');
const fileInput = ref<HTMLInputElement | null>(null);

/** 展示顺序：按**三段**排（用户口径 2026-12-07）——
 *
 *  ① **联网**：搜索 + 阅读挨着，它们是同一件事的两步（先搜到候选、再按链接读正文）；后端按名字
 *     排会把 web-fetch 甩到最后，看着像两件不相干的技能。
 *  ② **方法论**：全文导读 + 论文问答挨着 —— 这两个不"取材料"，它们定的是**怎么读、怎么答**。
 *  ③ **执行**：取材料（正文 / 整页 / 图 / 公式）→ 核对（术语、译文）→ 动手改（标注、修订）。
 *
 *  没收录的外部技能排在最后，并保持接口给的相对顺序。
 *  **只排副本**：开关的乐观更新、上传后的回填都直接动 skills，就地 sort 会打乱那份引用。 */
const SKILL_ORDER = [
  'web-search', 'web-fetch',
  'search-paper', 'reader-state', 'summarize-paper', 'answer-paper',
  'read-text', 'read-page-image', 'read-figure', 'read-formula',
  'explain-term', 'annotate-text', 'edit-text',
];
function skillRank(name: string): number {
  const at = SKILL_ORDER.indexOf(name);
  return at < 0 ? SKILL_ORDER.length : at;
}
const skillRows = computed<AgentSkill[]>(() =>
  skills.value.slice().sort((a, b) => skillRank(a.name) - skillRank(b.name)));

/** 技能搜索：**纯客户端过滤**（中文名 / 英文 ID / SKILL.md 的 description）。
 *  技能表一次就整份在手上，为一段过滤再打一次接口既没必要，也会让"边打字边发请求"
 *  变成后端压力；过滤只作用在展示副本上，skills 本身不动（开关的乐观更新仍改原文）。 */
const skillFilter = ref('');
const filteredSkills = computed<AgentSkill[]>(() => {
  const q = skillFilter.value.trim().toLowerCase();
  if (!q) return skillRows.value;
  return skillRows.value.filter((s) => (
    s.name.toLowerCase().includes(q)
    || (SKILL_NAME_ZH[s.name] || '').toLowerCase().includes(q)
    || (s.description || '').toLowerCase().includes(q)
  ));
});

/** 行内那一行说明：**就是 SKILL.md 的 description**（英文就照英文显示，不翻译、不改写）。
 *  空说明是技能作者没写，不是"没能力" —— 所以摆一句实话，而不是拿名字凑一句。 */
function skillDesc(skill: AgentSkill): string {
  const text = (skill.description || '').trim();
  return text || '这个技能没写说明';
}

/** 技能行的**收起**集合：默认空集合 = 每条技能的说明都摆着（用户口径 2026-12：进页面就要读到）。
 *  为什么反过来记"收起的"而不是"展开的"：默认态是"没有例外"，例外才需要存 —— 记展开集合的话，
 *  每条默认值都得靠"名字不在集合里"去推，新加载出来的技能反而容易漏掉。
 *  纯展示状态：不落后端、不发请求，刷新回到全展开（用户口径：同一页内记住即可）。 */
const collapsedSkills = ref<Set<string>>(new Set());
function isSkillOpen(name: string): boolean {
  return !collapsedSkills.value.has(name);
}
function toggleSkillDetail(skill: AgentSkill): void {
  // 换一个新 Set 再赋值：原地 mutate 不会触发 ref 的依赖
  const next = new Set(collapsedSkills.value);
  if (next.has(skill.name)) next.delete(skill.name);
  else next.add(skill.name);
  collapsedSkills.value = next;
}

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

/** 上传：.zip 里应有 <name>/SKILL.md。成功列出新增的名字，失败给后端 message。 */
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
    toast(added.length ? '已上传技能：' + added.join('、') : '上传成功，但没有读到新增技能',
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
  skillBusy.value = 'toggle:' + skill.name;
  try {
    const data = await api.put<AgentSkillsPayload>(
      '/agent/skills/' + encodeURIComponent(skill.name), { enabled });
    if (Array.isArray(data?.skills)) skills.value = data.skills;
  } catch (err) {
    skill.enabled = before;
    toast((err as Error)?.message || (enabled ? '启用技能失败' : '禁用技能失败'), 'error', 6000);
  } finally {
    skillBusy.value = '';
  }
}

/** 下载：接口回 zip 二进制，走 downloadBlob（request() 会把响应当 JSON 解析，包会坏）。 */
async function downloadSkill(skill: AgentSkill): Promise<void> {
  skillBusy.value = 'download:' + skill.name;
  try {
    const blob = await downloadBlob('/agent/skills/' + encodeURIComponent(skill.name) + '/download');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = skill.name + '.zip';
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
    title: '卸载技能「' + skill.name + '」？',
    body: '会从 agent 的技能目录里删掉它，新开的对话不再能用这个技能。',
    danger: true,
    confirmLabel: '卸载',
  }))) return;
  skillBusy.value = 'delete:' + skill.name;
  try {
    const data = await api.del<AgentSkillsPayload>('/agent/skills/' + encodeURIComponent(skill.name));
    skills.value = Array.isArray(data?.skills)
      ? data.skills
      : skills.value.filter((s) => s.name !== skill.name);
    toast('已卸载技能：' + skill.name, 'ok', 3200);
  } catch (err) {
    toast((err as Error)?.message || '卸载技能失败', 'error', 6000);
  } finally {
    skillBusy.value = '';
  }
}

/* 「查看说明」= SKILL.md 原文弹窗 ---------------------------------------------------
   数据来源是**现有的下载接口**（整个技能包的 zip），前端解出里面的 SKILL.md 正文 ——
   为一段只读文本新开一个后端接口不值得。弹窗里明确标注"这是定义原文"，
   因为这段文字是写给模型判断"该不该调它"的，读起来像规则文档是正常的。 */
const docOpen = ref(false);
const docName = ref('');
const docText = ref('');
const docError = ref('');
const docLoading = ref(false);

async function openSkillDoc(skill: AgentSkill): Promise<void> {
  docName.value = skill.name;
  docText.value = '';
  docError.value = '';
  docOpen.value = true;
  docLoading.value = true;
  try {
    const blob = await downloadBlob('/agent/skills/' + encodeURIComponent(skill.name) + '/download');
    docText.value = await readSkillMdFromZip(blob);
  } catch (err) {
    docError.value = (err as Error)?.message || '读取技能说明失败';
  } finally {
    docLoading.value = false;
  }
}

function closeSkillDoc(): void {
  docOpen.value = false;
}

/* ------------------------------- ③ 运行上限 ------------------------------- */

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
  max_rounds: '', deadline_seconds: '',
  context_budget: '', compact_threshold: '', attachment_inline_limit: '',
});
/** 服务端**当前生效**的那一份：用来算"改了没有"。 */
const limitBase = reactive<Record<LimitKey, number>>({
  max_rounds: 0, deadline_seconds: 0,
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
    max_rounds: 0, deadline_seconds: 0,
    context_budget: 0, compact_threshold: 0, attachment_inline_limit: 0,
  };
  for (const f of LIMIT_FIELDS) {
    const v = limitForm[f.key];
    if (v === '' || !Number.isFinite(Number(v))) {
      toast('「' + f.label + '」需要一个数字', 'warn');
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
    toast((err as Error)?.message || '保存运行上限失败', 'error', 6000);
  } finally {
    limitsBusy.value = false;
  }
}

/* ---------------------------- ④ Cyli 解释的模型 ----------------------------
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

/* 内置思考档与对话页、模型表单共用 utils/reasoning 那一份；模型自带 reasoning_levels 时以它为准。 */

const explainModels = ref<ExplainModelOption[]>([]);
const explainModel = ref('');
const explainEffort = ref('');
const explainBusy = ref(false);
/** 解释配置的基线（读到的那一份）：三组合并成一个「保存」之后，没有基线就只能永远可点 */
const explainBase = ref({ model: '', effort: '' });
const explainDirty = computed(() => (
  explainModel.value !== explainBase.value.model || explainEffort.value !== explainBase.value.effort
));

/* 一个「保存」管三组：每组只在**自己改过**的时候才发请求（未改动不发，是这一页的铁律）。 */
const anyBusy = computed(() => prefBusy.value || limitsBusy.value || explainBusy.value);
const anyDirty = computed(() => prefDirty.value || limitsDirty.value || explainDirty.value);
async function saveAll(): Promise<void> {
  if (prefDirty.value) await savePrefs();
  if (limitsDirty.value) await saveLimits();
  if (explainDirty.value) await saveExplain();
}

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
  return [...BUILTIN_EFFORTS];
});
/** 该模型自己配的那一档：选「跟随模型」时它说了算，界面上要写出来 */
const explainModelDefaultEffort = computed(() => {
  const v = explainModelChoice.value?.parameters?.reasoning_effort;
  return typeof v === 'string' ? v : '';
});
/** "跟随模型（档位）"那一条的文字：括号里是该模型自己的档，没配就只写"跟随模型" */
const explainFollowLabel = computed(() => (explainModelDefaultEffort.value
  ? '跟随模型（' + explainModelDefaultEffort.value + '）'
  : '跟随模型'));

async function loadExplainModels(): Promise<void> {
  try {
    const data = await api.get<{ items?: ExplainModelOption[] }>('/models', { model_type: 'agent' });
    explainModels.value = (data?.items ?? []).filter((m) => m.enabled !== false);
  } catch {
    explainModels.value = [];            // 模型目录读不到就只留"跟随对话默认"，不报错
  }
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
    explainBase.value = { model: explainModel.value, effort: explainEffort.value };
    toast('已保存：之后的 Cyli 解释按这份配置运行', 'ok', 2600);
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 5000);
  } finally {
    explainBusy.value = false;
  }
}

/* ---------------------------- ⑤ 联网搜索（web-search 技能） ----------------------------
   这几个值属于**技能自己**（agent 进程搜网页时才用它们），与问答模型无关，
   所以摆在技能行里，而不是再开一张卡片。
   密钥只存在服务端 data/secrets/websearch.key，接口只回掩码 —— 前端拿不到明文，
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

/** 测试：后端真跑一次 count=1 的搜索。失败也得**原地**说清楚是哪一步坏了，所以结论进 wsTest。 */
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
  void loadPrefs();
  void loadExplainModels();
});
</script>

<template>
  <div class="cyli-prefs">
    <!-- 一条头部：简介在左、两个动作贴右，同一行对齐。
         页面标题不写在这里 —— 设置页的 pane-head 已经给了「Cyli 偏好」（全区共用那一行），
         面板里再写一遍就是同屏两个标题。 -->
    <header class="pref-head">
      <p class="pref-intro">调整 Cyli 的回答方式与已接入的技能；改动只对新开的对话生效。</p>
      <!-- 两枚动作同组：窄屏时整组一起贴右，不会被 flex-wrap 拆成"一个在上一行右侧、
           一个掉到下一行左侧"（那样看着像两个不相干的按钮）。 -->
      <div class="head-actions">
        <!-- 一个头部管三组：每组各摆一枚「保存」等于让人在三处做同一个动作；
             合并后只在**某组真的改过**时才发请求（saveAll 里逐组判断 dirty）。 -->
        <button class="btn ghost sm" :disabled="anyBusy || !prefCanReset" data-pref-reset
                @click="resetPrefs">
          <AppIcon name="refresh" :size="14" />恢复默认
        </button>
        <button class="btn primary sm" :disabled="anyBusy || !anyDirty" data-pref-save
                @click="saveAll">
          <AppIcon name="check" :size="14" />{{ anyBusy ? '保存中…' : '保存' }}
        </button>
      </div>
    </header>

    <!-- 上方设置区：三组横向并排（对话偏好 / 运行上限 / Cyli 解释）。
         够宽才三列，窄了先两列再单列 —— 断点看**容器**宽度，不看窗口宽度（见样式）。 -->
    <section class="pref-top">
      <!-- ① 对话偏好 -->
      <div class="pref-group">
        <header class="group-head">
          <AppIcon name="sliders" :size="14" /><h3>对话偏好</h3>
        </header>
        <div class="group-body pref-fields">
          <label class="ctl">
            <span class="ctl-label">对话风格</span>
            <select id="pref-style" v-model="pref.preset" data-pref-style>
              <option v-for="opt in PREF_STYLE_OPTIONS" :key="opt.id" :value="opt.id">
                {{ opt.label }}
              </option>
            </select>
          </label>
          <!-- 只有"自定义"才展开：选走别的预设时它必须收起，否则用户会以为框里那段还在生效 -->
          <div class="ctl" v-if="pref.preset === 'custom'">
            <label class="ctl-label" for="pref-style-custom">风格指令</label>
            <textarea id="pref-style-custom" v-model="pref.style" rows="3" class="style-input"
                      :maxlength="PREF_STYLE_MAX" data-pref-style-custom
                      placeholder="例如：用中文回答，先给结论再展开理由；不确定时直说不确定。" />
            <span class="pref-count">{{ pref.style.length }} / {{ PREF_STYLE_MAX }}</span>
          </div>
          <div class="ctl">
            <span class="ctl-label">回复长度</span>
            <div class="seg" role="radiogroup" aria-label="回复长度" data-pref-length>
              <button v-for="opt in PREF_LENGTH_OPTIONS" :key="opt.id" type="button"
                      role="radio" :aria-checked="pref.length === opt.id"
                      :class="{ active: pref.length === opt.id }"
                      @click="pref.length = opt.id">{{ opt.label }}</button>
            </div>
          </div>
        </div>
      </div>

      <!-- ② 运行上限 -->
      <div class="pref-group">
        <header class="group-head">
          <AppIcon name="gauge" :size="14" /><h3>运行上限</h3>
        </header>
        <div class="group-body">
        <div class="limit-grid" v-if="limits">
          <label class="ctl" v-for="f in LIMIT_FIELDS" :key="f.key">
            <span class="ctl-label">{{ f.label }}<i class="limit-unit">{{ f.unit }}</i></span>
            <input type="number" :min="f.min" :max="f.max" :step="f.step"
                   v-model.number="limitForm[f.key]" />
          </label>
        </div>
        <p class="pref-hint" v-else>
          {{ limitsLoading ? '正在读取运行上限…' : '运行上限暂不可用（agent 服务未就绪）。' }}
        </p>
        <p class="pref-hint ok" v-if="limitsHint">{{ limitsHint }}</p>
        </div>
      </div>

      <!-- ③ Cyli 解释 -->
      <div class="pref-group">
        <header class="group-head">
          <AppIcon name="sparkle" :size="14" /><h3>Cyli 解释</h3>
        </header>
        <div class="group-body explain-grid">
          <label class="ctl">
            <span class="ctl-label">模型</span>
            <select v-model="explainModel" data-explain-model>
              <option value="">跟随对话默认</option>
              <option v-for="m in explainModels" :key="m.id" :value="m.id">
                {{ m.name || m.model_name || m.id }}
              </option>
            </select>
          </label>
          <label class="ctl">
            <span class="ctl-label">思考强度</span>
            <select v-model="explainEffort" data-explain-effort>
              <option value="">{{ explainFollowLabel }}</option>
              <option v-for="e in explainEffortOptions" :key="e" :value="e">{{ e }}</option>
            </select>
          </label>
        </div>
      </div>
    </section>

    <!-- ② 技能能力：横跨整个主内容区，单列纵向一行一个技能。
         **不给列表建固定高度的内嵌滚动容器**：整页只有 .settings-pane 那一层滚动。 -->
    <section class="skills">
      <header class="skills-head">
        <AppIcon name="grid" :size="14" /><h3 class="skills-title">技能能力</h3>
        <span class="skills-count">{{ skillRows.length }} 个</span>
        <span class="spacer"></span>
        <button class="btn ghost sm" :disabled="skillBusy === 'upload'" @click="pickSkillFile">
          <AppIcon name="upload" :size="14" />{{ skillBusy === 'upload' ? '上传中…' : '上传技能' }}
        </button>
        <button class="btn ghost sm" :disabled="skillsLoading" @click="reloadAll">
          <AppIcon name="refresh" :size="14" />{{ skillsLoading ? '刷新中…' : '刷新' }}
        </button>
        <!-- 真正的文件入口：藏起来，由上面那个按钮代点（原生 file input 长得不像按钮） -->
        <input ref="fileInput" class="sr-only" type="file" accept=".zip,application/zip"
               tabindex="-1" @change="onSkillFile" />
      </header>

        <!-- 搜索 + 列表装进模型页那张卡的同一套外壳（.prov-card 的写法：一整块 .card，
             行间只留发丝线，行本身不描边、不填底、不留行距） -->
        <div class="skill-body">

        <!-- 搜索：纯客户端过滤（中文名 / 英文 ID / 说明），技能表已整份在手，不再打接口 -->
        <label class="skill-search">
          <AppIcon name="search" :size="14" />
          <input v-model="skillFilter" type="text" autocomplete="off" spellcheck="false"
                 data-skill-search placeholder="搜索技能（中文名、英文 ID 或说明）" />
        </label>

        <div class="skill-list" v-if="filteredSkills.length">
          <template v-for="s in filteredSkills" :key="s.name">
            <div class="skill-row" :class="{ off: !s.enabled, open: isSkillOpen(s.name) }"
                 data-skill-row :data-skill-name="s.name">
              <!-- 名字就是开合开关：默认每条都把说明原文（SKILL.md 的 description）摆出来，
                   点名字可以就地收起 —— 十几条长句同屏确实要翻，但"这一页到底有哪些能力"
                   比"列表短"重要，所以默认给全（用户口径 2026-12）。 -->
              <button class="skill-toggle" type="button"
                      :aria-expanded="isSkillOpen(s.name)"
                      :title="(isSkillOpen(s.name) ? '收起' : '展开') + ' ' + s.name + ' 的说明'"
                      @click="toggleSkillDetail(s)">
                <span class="skill-name">{{ SKILL_NAME_ZH[s.name] || s.name }}</span>
                <span v-if="SKILL_NAME_ZH[s.name]" class="skill-id mono">{{ s.name }}</span>
                <!-- 自带是默认态、不标；只有"外部"值得一眼看出。停用状态**只来自 enabled** -->
                <span class="badge dim" v-if="!s.builtin">外部</span>
                <span class="badge warn" v-if="!s.enabled">已停用</span>
              </button>
              <div class="skill-ops">
                <!-- web-search 自带联网搜索配置入口：齿轮就地展开，不另开一页。
                     顺序是「齿轮 → 编辑」，齿轮管这一行的**运行设置**，编辑管**定义原文**。
                     都是幽灵按钮：行里只有图标、没有底色块（用户口径：别给每个按钮套方块）。 -->
                <button v-if="s.name === WEB_SEARCH_SKILL" class="btn ghost icon sm"
                        :class="{ on: wsOpen }"
                        :title="wsOpen ? '收起联网搜索设置' : '联网搜索配置'"
                        :aria-expanded="wsOpen" @click="toggleWs()">
                  <AppIcon name="sliders" :size="14" />
                </button>
                <button class="btn ghost icon sm" data-skill-view
                        :title="'查看 ' + s.name + ' 的 SKILL.md 定义原文'"
                        @click="openSkillDoc(s)">
                  <AppIcon name="edit" :size="14" />
                </button>
                <!-- 下载 / 卸载只属于外部技能：自带技能的 DELETE 回 403，灰按钮只是噪音 -->
                <template v-if="!s.builtin">
                  <button class="btn ghost icon sm" :title="'下载 ' + s.name + '.zip'"
                          :disabled="skillBusy === 'download:' + s.name" @click="downloadSkill(s)">
                    <AppIcon name="download" :size="14" />
                  </button>
                  <button class="btn ghost icon sm danger" :title="'卸载 ' + s.name"
                          :disabled="skillBusy === 'delete:' + s.name" @click="removeSkill(s)">
                    <AppIcon name="trash" :size="14" />
                  </button>
                </template>
                <Switch :model-value="s.enabled" :disabled="skillBusy === 'toggle:' + s.name"
                        :label="'启用技能 ' + s.name"
                        @update:model-value="toggleSkill(s, $event)" />
              </div>
            </div>
            <!-- 说明：SKILL.md 的 description 原文（英文就照英文，不翻译不改写）。
                 整段换行、不截断 —— 它撑开的是行下方的留白，不参与行宽，所以行高不会被它拽歪。
                 摆成行的**兄弟**而不是塞进行里：行高恒为 52，展开后只有内容变长，行本身不变形。 -->
            <div class="skill-detail" v-if="isSkillOpen(s.name)">
              <p class="skill-desc">{{ skillDesc(s) }}</p>
            </div>
            <!-- ⑤ 内嵌设置：挂在这一行下方展开，跟着技能一起滚动 -->
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
                           ? '已保存 ' + (wsKeyMasked || '密钥') + '，留空不改'
                           : '粘贴提供方控制台里的密钥'" />
                </label>
                <div class="grid cols-2">
                  <label class="field">
                    <span class="field-top">抓取字数上限</span>
                    <input type="number" min="500" max="200000" step="500"
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
        <!-- 有技能但被搜索过滤光了：只说这件事，不谎称"目录是空的" -->
        <p class="pref-hint" v-else-if="skills.length">没有匹配「{{ skillFilter.trim() }}」的技能。</p>
        <p class="pref-hint" v-else>
          {{ skillsLoading ? '正在读取技能…'
            : (skillsReady ? '技能目录是空的，上传一个 .zip 就有了。'
              : '还没有读到技能目录（agent 服务未就绪）。') }}
        </p>
        </div>
    </section>

    <!-- 查看说明：SKILL.md 原文（技能包的 zip 里解出来的），等宽字体 + 自己滚 -->
    <Modal :open="docOpen" :title="'技能定义原文：' + docName" wide @close="closeSkillDoc">
      <p class="doc-hint">
        这是 <code>{{ docName }}</code> 的 <code>SKILL.md</code> 原文（技能包里那份文件）。
        模型就是按它判断"什么时候该用这个技能"，所以读起来像规则文档是正常的。
      </p>
      <p class="tiny faint" v-if="docLoading">正在读取技能包…</p>
      <p class="doc-error" v-else-if="docError">{{ docError }}</p>
      <pre class="doc-pre" v-else>{{ docText }}</pre>
    </Modal>
  </div>
</template>
<style scoped>
/* 版式（2026-12 第二轮，口径：**与模型页对齐**，用户原话「每页风格一致」）：
   一条头部 → 上方三组横向并排（对话偏好 / 运行上限 / Cyli 解释）→ 下方「技能能力」横跨整幅。
   这一轮换掉的是**尺子**，不是结构：
   · 字号取模型页那一档（ModelPanel / LimitCard 实量）：分组标题 14.5/650、行标题 13/600、
     标签 12、辅助 11.5；正文控件内文 13。
   · 控件高取模型页主表单的 --control-h（32）—— 实测模型提供方表单里的 input 就是 32，
     前一轮那套 40 是本页自立的，两边差一档，用户一眼就看出来。
   · 技能列表套模型页「提供方」那张卡（.prov-card：一整块 .card + 行间发丝线 + 行不描边），
     标题栏套 LimitCard 的 .lim-status（淡色底 + --r-sm 圆角）。
   · 三组之间仍然不套白卡：标题条 + 留白 + 控件自己的边就是全部层级。 */
.cyli-prefs {
  --ctl-h: var(--control-h);   /* 32：模型页主表单的控件高，本页不再另立一套 */
  --row-pad: 0;                /* 技能行 / 搜索框共用的额外内边距（卡片自己给 14px） */
  display: flex; flex-direction: column; gap: 24px;
  container-type: inline-size;
}
.spacer { flex: 1; }

/* ---------- 头部：一句简介 + 右侧两枚动作 ---------- */
.pref-head { display: flex; align-items: center; gap: 16px; }
/* 简介是说明文字（12 / faint），与模型页 .limits-desc 同一档；
   flex:1 + min-width:0 让它自己换行，动作组因此不会被挤到下一行。 */
.pref-intro {
  flex: 1 1 auto; min-width: 0; margin: 0;
  font-size: calc(12px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint);
}
.head-actions { display: flex; align-items: center; gap: 8px; flex: none; }

/* ---------- 控件：与模型页同一档高度 / 圆角 / 字号 ---------- */
/* 宽度 100% 是对齐的前提：控件卡在某个像素宽度上、旁边的框却是整宽，两者右缘会差出半屏。 */
.cyli-prefs .ctl > select,
.cyli-prefs .ctl > input,
.cyli-prefs .ctl > textarea {
  box-sizing: border-box; width: 100%; height: var(--ctl-h);
  border-radius: var(--r-sm); font-size: calc(13px * var(--ts, 1));
}
/* .seg 跟到同一高度（全局是 --control-h，这里写 var 是为了窄屏改一处即可）；
   三枚选项平分整宽 —— 段落式控件铺满这一列，右缘才与上面的 select 齐。 */
.cyli-prefs .seg { display: flex; width: 100%; height: var(--ctl-h); }
.cyli-prefs .seg button {
  flex: 1 1 0; justify-content: center;
  height: calc(var(--ctl-h) - 6px);
  font-size: calc(12.5px * var(--ts, 1));
}

/* ---------- 上方三组：横向并排 ---------- */
/* 1.1 / 1.1 / 0.9：左边两组要放两个并排的数字框、或一个整宽的指令框，
   Cyli 解释只有两个下拉，窄一点正好。 */
.pref-top {
  display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1.1fr) minmax(0, .9fr);
  gap: 0 24px; align-items: start;
}
.pref-group { min-width: 0; }
/* 标题只是标题：**不铺底**。淡色圆角卡在它下面（.group-body）——用户口径：
   "淡淡的圆角卡片要放在内容下面，文字在卡片之上"。 */
.group-head {
  display: flex; align-items: center; gap: 6px;
  margin-bottom: 8px;
  color: var(--text-faint);
}
/* 内容底：淡色圆角矩形（照 LimitCard 的 .lim-status），只包控件、不包标题 */
.group-body {
  padding: 12px 14px;
  border-radius: var(--r-sm); background: var(--bg-sunken);
}
.group-head h3 {
  margin: 0; font-size: calc(14.5px * var(--ts, 1)); font-weight: 650;
  color: var(--text); line-height: 1.35; white-space: nowrap;
}

/* 一个控件 = 标签在上、控件在下（全篇只这一种，不再有"标签在左/在上"混排） */
.pref-fields { display: flex; flex-direction: column; gap: 12px; }
.ctl { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.ctl-label { font-size: calc(12px * var(--ts, 1)); line-height: 1.35; color: var(--text-dim); }

/* 自定义指令框：笔记编辑框那门语言（整宽 + 淡底 + 发丝描边，聚焦只点亮描边）。
   高度写死 78：rows=3 + line-height 1.6 在 2x 屏上会自己长到 130 出头，整组就被拖长了。 */
.cyli-prefs .ctl > .style-input {
  display: block; height: 78px; padding: 7px 10px;
  border: 1px solid var(--divider); border-radius: var(--r-sm);
  background: var(--bg-layer); color: var(--text);
  font-size: calc(13px * var(--ts, 1)); line-height: 1.6; resize: none;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
/* 覆盖全局的 textarea:hover { background: var(--bg-hover) }（鼠标一过就闪一下灰） */
.cyli-prefs .style-input:hover { background: var(--bg-layer); border-color: var(--border-strong); }
.cyli-prefs .style-input:focus {
  outline: none; box-shadow: none; background: var(--bg-elev);
  border-color: color-mix(in srgb, var(--accent) 60%, transparent);
}
.cyli-prefs .style-input::placeholder { color: var(--text-faint); }
/* 计数落在指令框右下：贴着眼看的那段文字，又不把正文拽开半屏 */
.pref-count {
  align-self: flex-end; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

/* ---------- 运行上限 / Cyli 解释 ---------- */
/* 5 个数字项排两列（2+2+1）：一列要排五行，整组会比旁边两组高出一大截。
   不用 auto-fit：它在中间宽度会排成"3 + 1 + 1"，比整齐的两列更难看。 */
.limit-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
/* 数字右对齐 + 等宽：数值长短变化时不会左右跳 */
.limit-grid input { text-align: right; font-variant-numeric: tabular-nums; }
/* 单位挂在标签后缀里，而不是跟在输入框右边：省下的宽度正好给"1000000"这种长数字 */
.limit-unit { margin-left: 4px; font-style: normal; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
.explain-grid { display: flex; flex-direction: column; gap: 12px; }
.pref-hint {
  margin: 10px 0 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim);
}
.pref-hint.ok { color: var(--ok); }

/* ---------- 下方：技能能力（横跨整幅，一行一个） ---------- */
/* 块本身是**裸块**（与模型页的 .block / .limits 完全一致：无描边、无圆角、无底色）：
   描边和底色交给里面那张 .card，块只负责"标题条 + 卡片"的竖向节奏。 */
.skills {
  min-width: 0; display: flex; flex-direction: column; gap: 12px;
  padding: 0; border: 0; background: transparent; box-shadow: none;
}
/* 标题只是标题（不铺底）；淡色圆角卡挪到下面的 .skill-body */
.skills-head {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 8px;
  color: var(--text-faint);
}
.skills-title {
  margin: 0; font-size: calc(14.5px * var(--ts, 1)); font-weight: 650;
  color: var(--text); line-height: 1.35; white-space: nowrap;
}
/* 数量是一句注脚，不是徽章：不铺底、不加边框，跟在标题后面 */
.skills-count { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }

/* 内容底：淡色圆角矩形（与 .group-body 同一套），只包搜索框与技能行；
   行与行之间只靠 1px 发丝线，行自己不带边框、不带底色、不带行距 */
.skill-body { padding: 10px 14px; border-radius: var(--r-sm); background: var(--bg-sunken); }
/* 卡里的空态/提示行要自己留出上下呼吸（底只给左右内边距，行与行之间的节奏归行自己管） */
.skill-body > .pref-hint { margin: 10px 0 14px; }

/* 搜索框：整宽的输入语义，但只占一个可读的行宽 —— 铺满整张卡的空白框看着像一块空洞。
   左缘与技能行同一条竖线（卡片内边距 14 + --row-pad）。 */
.skill-search {
  display: flex; align-items: center; gap: 8px;
  box-sizing: border-box; width: 100%; max-width: 360px; height: var(--ctl-h);
  margin: 10px var(--row-pad) 8px; padding: 0 10px;
  border: 1px solid var(--border-strong); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text-faint);
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.skill-search:focus-within { border-color: var(--accent); box-shadow: var(--ring); }
.skill-search input {
  width: 100%; height: 100%; min-width: 0; padding: 0;
  border: 0; background: transparent; border-radius: 0;
  font-size: calc(13px * var(--ts, 1)); color: var(--text); box-shadow: none;
}
.skill-search input:hover,
.skill-search input:focus { background: transparent; border: 0; box-shadow: none; }

.skill-list { display: flex; flex-direction: column; }
/* 行与行之间只留一条发丝线（行不描边、不填底、不留行距）：行高 52 落在 48–56 之间，
   名字那侧与操作那侧由同一条 padding 决定，相邻两行的左内边距因此一模一样。 */
.skill-row {
  display: flex; align-items: center; gap: 10px;
  min-height: 52px; padding: 0 var(--row-pad);
  border-top: 1px solid var(--divider);
  /* 圆角必须落在行自己身上，不能只写在 hover 里：底色是一整块矩形，
     而它躺的是一张 --r-sm 的淡色圆角卡 —— 不带圆角鼠标一过就是一块方角底压在圆角卡上。 */
  border-radius: var(--r-sm);
  transition: background var(--dur) var(--ease);
}
.skill-list > .skill-row:first-child { border-top: 0; }
/* 与模型页的 .prov-row 一样：悬停整行浮一层浅底，明确"这一行是连续的"。
   底色只在行的盒内（左右内边距由 --row-pad 决定），不会伸到卡片内边距外面去。 */
.skill-row:hover { background: var(--bg-hover); }
/* 名字那一整块是开关（是一枚按钮，不是文本）：键盘也能开合，aria-expanded 给出状态。
   min-width: 0 + 溢出裁剪是关键 —— 名字再长也只是省略号，绝不会把整页顶出横向滚动条。 */
.skill-toggle {
  display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1;
  padding: 6px 0; border: 0; background: transparent; color: inherit;
  font-family: inherit; text-align: left; cursor: pointer; overflow: hidden;
}
.skill-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--r-xs); }
.skill-toggle:hover .skill-name { color: var(--accent-text); }
.skill-name {
  font-size: calc(13px * var(--ts, 1)); font-weight: 600; color: var(--text);
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  transition: color var(--dur-fast) var(--ease);
}
/* 英文 ID 跟在中文名后面：小一档 + 更淡，只在"对日志 / 找技能目录"时才需要，不抢主信息 */
.skill-id {
  flex: none; max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
}
/* 说明块：SKILL.md 的 description 原文，整段换行、不截断（它在行下方，不参与行宽）。
   左内边距与行一致（--row-pad），说明的第一行字因此与上面的技能名落在同一条竖线上；
   缩进或加符号都会让"这句说明属于哪一条"变得要靠猜。 */
.skill-detail { padding: 0 var(--row-pad) 10px; }
.skill-desc {
  margin: 0; font-size: calc(12.5px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim);
  overflow-wrap: anywhere;
}
/* 停用的行整体降一档亮度（开关与按钮仍保持可读，不然会像"整个坏掉了"） */
.skill-row.off .skill-name { color: var(--text-dim); font-weight: 500; }
.skill-row.off .skill-id { opacity: .7; }
/* 右侧顺序固定：齿轮（配置入口）→ 编辑 → 下载/卸载 → 开关，整组贴右边缘。
   开关永远落在同一个 x 上，行里有没有齿轮 / 下载都不会把它推歪。 */
.skill-ops { display: flex; align-items: center; justify-content: flex-end; gap: 2px; flex: none; }
/* 行里的图标按钮是**幽灵按钮**：不铺底、不描边、不投影、不是圆 —— 悬停才浮出一层浅底。
   尺寸走全局 .btn.icon.sm（26×26，与模型页行内动作同档），这里只把圆角收到 8。 */
.skill-ops .btn.icon.sm { border-radius: 8px; }
/* Switch 的 track 顶上默认有 1px 偏移（它是给"文字 + 轨道"那行准备的），行内按钮区必须拉平 */
.skill-ops :deep(.switch-row) { align-items: center; }
.skill-ops :deep(.switch-track) { margin-top: 0; }

/* ---------- ⑤ 联网搜索：挂在 web-search 那一行下方 ---------- */
/* 一段能改运行参数的表单，用淡底 + 发丝描边与上下的"行"区分开，
   否则与技能行同底色会像下一行的成员。 */
.ws-panel {
  margin: 0 var(--row-pad) 12px; padding: 12px;
  border: 1px solid var(--divider); border-radius: var(--r-sm);
  background: var(--bg-layer);
}
/* 面板里的每一块（网格 / 字段 / 按钮行）之间统一个间距，省得逐个加 margin */
.ws-panel > * + * { margin-top: 10px; }
/* 面板里的控件跟全页同一档高度与字号（全局 .field 是 32 / 12） */
.cyli-prefs .ws-panel input:not([type="checkbox"]),
.cyli-prefs .ws-panel select {
  box-sizing: border-box; width: 100%; max-width: none; height: var(--ctl-h);
  border-radius: var(--r-sm); font-size: calc(13px * var(--ts, 1));
}
.ws-hint { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim); }
.ws-actions { flex-wrap: wrap; }
/* 内嵌面板在窄容器里两列会挤：落成单列，标签与控件才不打架 */
.ws-panel .grid.cols-2 { grid-template-columns: minmax(0, 1fr); }
/* 测试结论：成功绿、失败红；细节退回弱色 —— 一眼看到成败，细看才知道卡在哪一步 */
.ws-test {
  display: flex; align-items: flex-start; gap: 6px; flex-wrap: wrap;
  margin: 10px 0 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5;
  overflow-wrap: anywhere;
}
.ws-test.ok { color: var(--ok); }
.ws-test.bad { color: var(--danger); }
.ws-test-meta { color: var(--text-faint); font-variant-numeric: tabular-nums; }
.ws-test-detail { color: var(--text-dim); }

/* ---------- 窄容器：三组换行 / 减列 ---------- */
@container (max-width: 899px) {
  .pref-top { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px 24px; }
  .ws-panel .grid.cols-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@container (max-width: 559px) {
  .pref-top { grid-template-columns: minmax(0, 1fr); gap: 20px; }
  .limit-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  /* 窄到头时行里的操作按钮先保住名字：名字允许省略号，按钮组不换行、不压字号 */
  .skill-row { gap: 8px; }
  .skill-id { max-width: 34%; }
}

/* ---------- 查看说明弹窗 ---------- */
.doc-hint {
  margin: 0 0 10px; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim);
}
.doc-error { margin: 0; font-size: calc(12px * var(--ts, 1)); color: var(--danger); }
/* 原文用等宽 + 保留换行：SKILL.md 的缩进、列表、"什么时候用"分节都在排版里，
   落进正文样式会揉成一团。自己滚（弹窗最高 86vh，正文比它长是常态）。 */
.doc-pre {
  margin: 0; padding: 12px 14px; max-height: 56vh; overflow: auto;
  background: var(--bg-layer); border: 1px solid var(--divider); border-radius: var(--r-sm);
  font-family: var(--mono); font-size: calc(12px * var(--ts, 1)); line-height: 1.7;
  color: var(--text); white-space: pre-wrap; overflow-wrap: anywhere;
}
</style>
