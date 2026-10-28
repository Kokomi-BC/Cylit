<script setup lang="ts">
/**
 * 设置页「Cyli 偏好」（原「论文问答」，2026-12 定名并与面板内部一起重做）：
 * **对话偏好**（风格预设 + 回复长度）与**技能能力**（已接入技能的列表）两块主内容，
 * 中间跟一张只动一处的小卡：运行上限。
 *
 * 版式（2026-12 第三轮）：**标题与两枚动作在窗格抬头那一行**（Teleport 到 #pane-head-actions）→
 * 上方两列等宽淡色圆角卡（对话偏好 / 运行上限，stretch 拉平）→ 一张满宽的「自定义指令」
 * （风格选「自定义」才出现）→ 下方「技能能力」横跨整幅，一行一个技能（左内容 / 右五格操作区）。
 * 为什么横排而技能竖排：两三个短控件横着排一排就够，竖着排三屏才轮到技能；
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
 * ④ **解释已并入对话**：划词解释由对话本身承担，设置里不再单独配模型与思考强度 ——
 *    原来那组 UI（两个下拉）已撤。后端 explain_model_config_id / explain_reasoning_effort
 *    两个键、backend/services/agent/explain.py、POST /agent/ask 的 mode=explain 都保留。
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
import type {
  AgentLimits, AgentLimitsPatch, AgentSkill, AgentSkillsPayload, AgentSkillsUploadResult,
  SettingsPayload,
} from '@/api/types';
import {
  PREF_DEFAULT_STATE, PREF_LENGTH_OPTIONS, PREF_STYLE_MAX, PREF_STYLE_OPTIONS,
  isUnsupportedSettingError, normalizeReplyLength, normalizeStylePreset, readSkillMdFromZip,
} from '@/components/settings/cyliPrefs';
import type { CyliPrefsPatch, CyliPrefsState } from '@/components/settings/cyliPrefs';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
import Switch from '@/components/Switch.vue';
import WebSearchSettings from '@/components/settings/WebSearchSettings.vue';

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
  'deep-read-paper': '论文精读',
  'answer-paper': '论文问答',
  'explain-term': '术语与译法',
  'edit-text': '文本修订',
  // 2026-12 新增（第二阶段改造）：目录浏览。名字只写"它是什么"，
  // 能力一律来自后端 description（见上面那条警告）。
  'browse-paper-library': '论文目录',
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
 *  ② **方法论**：论文精读 + 论文问答挨着 —— 这两个不"取材料"，它们定的是**怎么读、怎么答**。
 *  ③ **执行**：取材料（正文 / 整页 / 图 / 公式）→ 核对（术语、译文）→ 动手改（标注、修订）。
 *
 *  没收录的外部技能排在最后，并保持接口给的相对顺序。
 *  **只排副本**：开关的乐观更新、上传后的回填都直接动 skills，就地 sort 会打乱那份引用。 */
/** 排序用的名字表：三段顺序与上面那句注释一一对应。
 *
 * 2026-12 起**只剩顺序**：分组标题与那句「这一段在干什么」都按用户口径删掉了
 * （先「移除技能分类的标题」，再「一起删掉，列表就是纯技能行」）；按组合并的那个
 * computed（skillGroups）一并删除，列表直接渲染排序后的 filteredSkills。 */
const SKILL_ORDER: string[] = [
  'web-search', 'web-fetch',
  // 目录浏览排在"找"这一段的**最前**：先看库里有什么，再决定读哪一篇 / 在哪一篇里搜。
  // 没收录进这张表的名字会排到最后（skillRank 返回长度），所以新技能必须记得加进来。
  'browse-paper-library',
  'search-paper', 'reader-state', 'deep-read-paper', 'answer-paper',
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

/* 技能行的说明**不再可收起**（用户口径 2026-12：去掉点名字收起详情的逻辑）。
   每条技能的 description 就是「这一页到底有哪些能力」的正文，藏起来只让人多点一下；
   真觉得长就用上面那个搜索框过滤。原来那套 collapsedSkills / toggleSkillDetail 已删。 */

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

/* ---------------------------- 一个「保存」管两组 ----------------------------
   每组只在**自己改过**的时候才发请求（未改动不发，是这一页的铁律）。
   explain 那一组已随「解释并入对话」撤掉，这一页只剩对话偏好与运行上限。 */

const anyBusy = computed(() => prefBusy.value || limitsBusy.value);
const anyDirty = computed(() => prefDirty.value || limitsDirty.value);
async function saveAll(): Promise<void> {
  if (prefDirty.value) await savePrefs();
  if (limitsDirty.value) await saveLimits();
}

/* ---------------------------- ⑤ 联网搜索（web-search 技能） ----------------------------
   这一行只负责「展开 / 收起」：字段与读写都在共用组件 `WebSearchSettings` 里 ——
   它同时被「在线服务 → 网联搜索」使用，两边读写的**是同一份配置与同一个服务端密钥文件**
   （`data/secrets/websearch.key`），所以这里绝不能再写一套。
   密钥语义：接口只回掩码，输入框留空 = 本次不改，清除是单独一次带二次确认的动作。 */
/** 齿轮只切展开态；配置由 WebSearchSettings 自己拉（没展开就不该多一次请求）。 */
const wsOpen = ref(false);
function toggleWs(): void {
  wsOpen.value = !wsOpen.value;
}

/* ------------------------------- 载入 ------------------------------- */

async function reloadAll(): Promise<void> {
  await Promise.all([loadSkills(), loadLimits()]);
}

onMounted(() => {
  void reloadAll();
  void loadPrefs();
});
</script>

<template>
  <div class="cyli-prefs">
    <!-- 两枚动作挂到窗格抬头那一行的右端（「Cyli 偏好」标题 + 恢复默认 + 保存 同一条水平线）。
         面板里不再自己造一行标题或简介：标题归 pane-head，简介归左栏 desc。
         逻辑没动 —— 仍然一个头部管两组，只在**某组真的改过**时才发请求（saveAll 逐组判断 dirty）。 -->
    <Teleport to="#pane-head-actions" defer>
      <div class="head-actions">
        <button class="btn ghost sm" :disabled="anyBusy || !prefCanReset" data-pref-reset
                @click="resetPrefs">
          <AppIcon name="refresh" :size="14" />恢复默认
        </button>
        <button class="btn primary sm" :disabled="anyBusy || !anyDirty" data-pref-save
                @click="saveAll">
          <AppIcon name="check" :size="14" />{{ anyBusy ? '保存中…' : '保存' }}
        </button>
      </div>
    </Teleport>

    <!-- 两张卡并排（用户口径 2026-12）：**左 = 对话偏好（含运行上限），右 = 自定义指令**。
         左卡本身就是"两半合一"（对话的两个控件 + 一条带小标题的发丝线 + 五个运行上限数字），
         自定义指令单独占右列，文本框跟着右卡拉到同高；不选「自定义」时右列不存在，
         左卡自动独占整行（.pref-cols--solo）。 -->
    <div class="pref-cols" :class="{ 'pref-cols--solo': pref.preset !== 'custom' }">
    <section class="pref-group">
      <header class="group-head">
        <AppIcon name="sliders" :size="14" /><h3>对话偏好</h3>
      </header>
      <div class="group-body pref-stack">
        <div class="pref-pair">
          <label class="ctl">
            <span class="ctl-label">对话风格</span>
            <select id="pref-style" v-model="pref.preset" data-pref-style>
              <option v-for="opt in PREF_STYLE_OPTIONS" :key="opt.id" :value="opt.id">
                {{ opt.label }}
              </option>
            </select>
          </label>
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

        <!-- 运行上限：3 列数值网格（窄了自动 2 列 / 1 列）；标签在上、输入框在下，宽度与高度全一致 -->
        <div class="pref-sep"><span class="pref-sep-label">运行上限</span></div>
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
    </section>

    <!-- ③ 自定义指令（右列）：只在风格选「自定义」时出现 —— 选走别的预设必须收起，
         否则会以为框里那段还在生效。标题在卡外、文本框随卡长高、字数贴框的右下。 -->
    <section class="pref-group pref-custom" v-if="pref.preset === 'custom'">
      <header class="group-head">
        <AppIcon name="edit" :size="14" /><h3>自定义指令</h3>
      </header>
      <div class="group-body">
        <textarea id="pref-style-custom" v-model="pref.style" class="style-input"
                  :maxlength="PREF_STYLE_MAX" data-pref-style-custom aria-label="自定义指令"
                  placeholder="例如：用中文回答，先给结论再展开理由；不确定时直说不确定。" />
        <span class="pref-count">{{ pref.style.length }} / {{ PREF_STYLE_MAX }}</span>
      </div>
    </section>
    </div>

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
             条与条之间只留发丝线，条本身不描边、不填底；悬停整条浮起（底色 + 阴影）） -->
        <div class="skill-body">

        <!-- 搜索：纯客户端过滤（中文名 / 英文 ID / 说明），技能表已整份在手，不再打接口 -->
        <label class="skill-search">
          <AppIcon name="search" :size="14" />
          <input v-model="skillFilter" type="text" autocomplete="off" spellcheck="false"
                 data-skill-search placeholder="搜索技能（中文名、英文 ID 或说明）" />
        </label>

        <div class="skill-list" v-if="filteredSkills.length">
          <!-- 纯技能行：**没有分类标题，也没有分组说明**（用户口径 2026-12：先「移除技能分类的标题」，
               再「一起删掉，列表就是纯技能行」）。三段顺序还在（SKILL_ORDER），只是不再可视分组。 -->
          <template v-for="s in filteredSkills" :key="s.name">
            <!-- 一条技能 = **横向两块**（用户口径 2026-12）：左边名字 / 英文 ID / 说明竖着排，
                 右边是操作区。说明因此不会把名字挤歪，操作区也不会被长说明顶走。 -->
            <div class="skill-item" :class="{ off: !s.enabled }"
                 data-skill-row :data-skill-name="s.name">
              <div class="skill-main">
                <!-- 名字是**纯文本**（2026-12 起不再是一枚开合按钮）：说明一直摆着，不用点。 -->
                <div class="skill-title">
                  <span class="skill-name">{{ SKILL_NAME_ZH[s.name] || s.name }}</span>
                  <span v-if="SKILL_NAME_ZH[s.name]" class="skill-id mono">{{ s.name }}</span>
                  <!-- 自带是默认态、不标；只有"外部"值得一眼看出。停用状态**只来自 enabled** -->
                  <span class="badge dim" v-if="!s.builtin">外部</span>
                  <span class="badge warn" v-if="!s.enabled">已停用</span>
                </div>
                <p class="skill-desc">{{ skillDesc(s) }}</p>
              </div>
              <!-- 操作区是**五格固定网格**：齿轮（配置）→ 编辑 → 下载 → 卸载 → 开关。
                   缺哪一格就留一个空占位，于是每一条里的「编辑」和开关都落在同一个 x 上
                   （用户口径 2026-12：技能行内操作控件要有统一对齐基准）。
                   都是幽灵按钮：行里只有图标、没有底色块。 -->
              <div class="skill-ops">
                <button v-if="s.name === WEB_SEARCH_SKILL" class="btn ghost icon sm"
                        :class="{ on: wsOpen }"
                        :title="wsOpen ? '收起联网搜索设置' : '联网搜索配置'"
                        :aria-expanded="wsOpen" @click="toggleWs()">
                  <AppIcon name="sliders" :size="14" />
                </button>
                <span v-else class="ops-slot" aria-hidden="true"></span>
                <button class="btn ghost icon sm" data-skill-view
                        :title="'查看 ' + s.name + ' 的 SKILL.md 定义原文'"
                        @click="openSkillDoc(s)">
                  <AppIcon name="edit" :size="14" />
                </button>
                <!-- 下载 / 卸载只属于外部技能：自带技能的 DELETE 回 403，灰按钮只是噪音 -->
                <button v-if="!s.builtin" class="btn ghost icon sm" :title="'下载 ' + s.name + '.zip'"
                        :disabled="skillBusy === 'download:' + s.name" @click="downloadSkill(s)">
                  <AppIcon name="download" :size="14" />
                </button>
                <span v-else class="ops-slot" aria-hidden="true"></span>
                <button v-if="!s.builtin" class="btn ghost icon sm danger" :title="'卸载 ' + s.name"
                        :disabled="skillBusy === 'delete:' + s.name" @click="removeSkill(s)">
                  <AppIcon name="trash" :size="14" />
                </button>
                <span v-else class="ops-slot" aria-hidden="true"></span>
                <Switch :model-value="s.enabled" :disabled="skillBusy === 'toggle:' + s.name"
                        :label="'启用技能 ' + s.name"
                        @update:model-value="toggleSkill(s, $event)" />
              </div>
            </div>
            <!-- ⑤ 内嵌设置：挂在 web-search 这一行下方展开，跟着技能一起滚动。
                 字段与读写都在共用组件里（与「在线服务 → 网联搜索」是同一份配置） -->
            <div v-if="s.name === WEB_SEARCH_SKILL && wsOpen" class="ws-panel">
              <WebSearchSettings />
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
/* 版式（2026-12 第三轮，口径：**统一对齐基准 + 两卡等高**）：
   抬头一行（标题 + 恢复默认 + 保存）→ 上方两列等宽卡（对话偏好 / 运行上限）→
   满宽「自定义指令」→「技能能力」横跨整幅。这一轮换掉的是**网格与基准**，不是功能：
   · 三个间距数收在变量里（--card-pad 20 / --card-gap 24 / --field-gap 14），
     所有卡片、字段、标题都从同一个左缘起步；
   · 两张卡 stretch 拉平：同起点、同内边距、同标题高度、同下边缘；
   · 技能行改成"左内容 / 右操作"两列，右列是五格固定网格（缺格留占位），
     于是每一条的图标与开关都落在同一个 x 上。
   尺子沿用第二轮（与模型页对齐）：
   · 字号取模型页那一档（ModelPanel / LimitCard 实量）：分组标题 14.5/650、行标题 13/600、
     标签 12、辅助 11.5；正文控件内文 13。
   · 控件高取模型页主表单的 --control-h（32）—— 实测模型提供方表单里的 input 就是 32，
     前一轮那套 40 是本页自立的，两边差一档，用户一眼就看出来。
   · 技能列表套模型页「提供方」那张卡（.prov-card：一整块 .card + 行间发丝线 + 行不描边），
     标题栏套 LimitCard 的 .lim-status（淡色底 + --r-sm 圆角）。
   · 三组之间仍然不套白卡：标题条 + 留白 + 控件自己的边就是全部层级。 */
.cyli-prefs {
  --ctl-h: var(--control-h);   /* 32：模型页主表单的控件高，本页不再另立一套 */
  /* 全页只有这三个间距数：卡片内边距 / 卡片之间 / 卡内字段之间。
     卡片内边距同时也是"文字左缘"的唯一基准 —— 标题、字段、搜索框、技能名都从这一条线起。 */
  --card-pad: 20px;
  --card-gap: 24px;
  --field-gap: 14px;
  display: flex; flex-direction: column; gap: var(--card-gap);
  container-type: inline-size;
}
.spacer { flex: 1; }

/* 动作组：由 Teleport 挂进窗格抬头那一行（标题 + 恢复默认 + 保存 同一条水平线），
   这里只管两条并排的间距 */
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

/* ---------- 卡片（一区一张，卡内再分小段用发丝线） ---------- */
/* 两张卡并排：左（对话偏好 + 运行上限）拿宽一点，右（自定义指令）跟着拉平 */
.pref-cols {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr);
  gap: var(--card-gap);
  align-items: stretch;
}
/* 没选「自定义」时右列不渲染：左卡独占整行，不留一个空轨道 */
.pref-cols--solo { grid-template-columns: minmax(0, 1fr); }
/* 左卡只占半幅时，五个数字回到两列（三列会窄到看不清 1000000） */
.pref-cols .limit-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.pref-group { min-width: 0; display: flex; flex-direction: column; }
/* 标题只是标题：**不铺底**，淡色圆角卡在它下面（与系统信息 / 索引管理的块同一套）。
   min-height 让带图标与不带图标的标题一样高，两张卡的正文起点因此对齐。 */
.group-head {
  display: flex; align-items: center; gap: 6px;
  min-height: 20px; margin-bottom: 8px;
  color: var(--text-faint);
}
/* 内容底：淡色圆角矩形，只包控件、不包标题；flex:1 才撑得出"两张卡等高" */
.group-body {
  flex: 1;
  padding: var(--card-pad);
  border-radius: var(--r-sm); background: var(--bg-sunken);
}
.group-head h3 {
  margin: 0; font-size: calc(14.5px * var(--ts, 1)); font-weight: 650;
  color: var(--text); line-height: 1.35; white-space: nowrap;
}

/* 卡内竖向节奏：小段之间 14，小段内部也 14 */
.pref-stack { display: flex; flex-direction: column; gap: var(--field-gap); }
/* 一张卡里的两列（对话风格 / 回复长度）：等宽，同一行 */
.pref-pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--field-gap); }
/* 小段分隔：一枚小标题 + 一条发丝线，用来在卡内分出「运行上限」这半 */
.pref-sep { display: flex; align-items: center; gap: 10px; }
.pref-sep-label { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); white-space: nowrap; }
.pref-sep::after { content: ''; flex: 1 1 auto; height: 1px; background: var(--divider); }

/* 一个控件 = 标签在上、控件在下（全篇只这一种，不再有"标签在左/在上"混排） */
.ctl { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.ctl-label { font-size: calc(12px * var(--ts, 1)); line-height: 1.35; color: var(--text-dim); }

/* 自定义指令卡（右列）：标题在卡外，框占满内容宽并与左卡同高，字数贴在框的右下 */
.pref-custom .group-body { display: flex; flex-direction: column; gap: 6px; }
.pref-custom .style-input { flex: 1 1 auto; height: auto; min-height: clamp(140px, 20vh, 220px); }
/* 自定义指令框：**独占一行卡片**，宽度就是整幅内容宽。
   clamp 让它在高一点的视口里再长一档（140–220），不会顶掉半屏；
   字号 13 / 行高 1.6 是正文的读数，行数由高度说了算（不再跟 rows 走）。 */
.cyli-prefs .style-input {
  display: block; width: 100%;
  height: clamp(140px, 20vh, 220px); padding: 10px 12px;
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

/* ---------- 运行上限 ---------- */
/* 5 个数字项排两列（2+2+1）：一列要排五行，整组会比旁边两组高出一大截。
   不用 auto-fit：它在中间宽度会排成"3 + 1 + 1"，比整齐的两列更难看。 */
.limit-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px var(--field-gap); }
/* 数字右对齐 + 等宽：数值长短变化时不会左右跳 */
.limit-grid input { text-align: right; font-variant-numeric: tabular-nums; }
/* 单位挂在标签后缀里，而不是跟在输入框右边：省下的宽度正好给"1000000"这种长数字 */
.limit-unit { margin-left: 4px; font-style: normal; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
.pref-hint {
  margin: 10px 0 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim);
}
.pref-hint.ok { color: var(--ok); }

/* ---------- 下方：技能能力（横跨整幅） ---------- */
/* 块本身是**裸块**（与模型页的 .block / .limits 一致：无描边、无圆角、无底色）：
   描边和底色交给里面那张淡色圆角卡，块只负责"标题条 + 卡片"的竖向节奏。 */
.skills {
  min-width: 0; display: flex; flex-direction: column; gap: 8px;
  padding: 0; border: 0; background: transparent; box-shadow: none;
}
/* 标题 / 数量 / 上传 / 刷新**同一条水平线**：数量跟在标题后，两枚动作被 .spacer 推到右端 */
.skills-head {
  position: relative;          /* 同理：藏起来的那枚 file input 是绝对定位的，得有个定位祖先把它的溢出圈住 */
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  min-height: 20px;
  color: var(--text-faint);
}
.skills-title {
  margin: 0; font-size: calc(14.5px * var(--ts, 1)); font-weight: 650;
  color: var(--text); line-height: 1.35; white-space: nowrap;
}
/* 数量是一句注脚，不是徽章：不铺底、不加边框，跟在标题后面 */
.skills-count { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }

/* 内容底：与 .group-body 同一套淡色圆角卡、同一个内边距 */
.skill-body { padding: var(--card-pad); border-radius: var(--r-sm); background: var(--bg-sunken); }
/* 卡里的空态/提示行跟着卡片内边距走 */
.skill-body > .pref-hint { margin: 12px 0 0; }

/* 搜索框：独立一行，左缘与技能名 / 说明的左缘同一条竖线（都是卡片内边距 20）。
   只占一个可读的行宽 —— 铺满整张卡的空白框看着像一块空洞。 */
.skill-search {
  display: flex; align-items: center; gap: 8px;
  box-sizing: border-box; width: 100%; max-width: 360px; height: var(--ctl-h);
  margin: 0 0 4px; padding: 0 10px;
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
/* 分组标题与分组说明都删了（2026-12）：列表就是一行行技能，条与条之间只留发丝线。 */
/* 一条技能 = **横向两块**（用户口径 2026-12）：
   左 = 名字 / 英文 ID / 说明（竖着排、可换行），右 = 固定五格操作区。
   条本身不描边、不填底、不响应悬停；条与条之间只留一条发丝线。 */
.skill-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  /* center：操作区在**整条**里垂直居中（说明折成两行时，图标与开关也不会吊在第一行上） */
  align-items: center;
  column-gap: 16px;
  padding: 14px 0;
}
.skill-item + .skill-item { border-top: 1px solid var(--divider); }
.skill-main { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
/* 名字那一整块**只是标题**（2026-12 起不再是开合按钮：说明一直摆着）。
   min-width: 0 + 溢出裁剪是关键 —— 名字再长也只是省略号，绝不会把操作区顶走。 */
.skill-title {
  display: flex; align-items: center; gap: 8px; min-width: 0;
  overflow: hidden;
}
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
/* 说明：SKILL.md 的 description 原文（英文就照英文，不翻译不改写），**左列整宽、自然换行**。
   左列宽度已经由右列操作区让出来了，不需要再收一个行宽。 */
.skill-desc {
  margin: 0;
  font-size: calc(12.5px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim);
  overflow-wrap: anywhere;
}
/* 停用的那条连说明一起降一档：整条读起来是"关掉的"，但仍能看清写的是什么 */
.skill-item.off .skill-desc { color: var(--text-faint); }
.skill-item.off .skill-name { color: var(--text-dim); font-weight: 500; }
.skill-item.off .skill-id { opacity: .7; }
/* 操作区 = **五格固定网格**（齿轮 / 编辑 / 下载 / 卸载 / 开关）：缺哪一格就留一个
   .ops-slot 占位，于是「编辑」图标与开关在每一条里都落在同一个 x 上、垂直也对齐
   （用户口径 2026-12：技能行内操作控件要有统一对齐基准）。 */
.skill-ops {
  display: grid;
  grid-template-columns: repeat(4, 26px) auto;
  align-items: center; justify-items: center;
  gap: 2px; flex: none;
}
.ops-slot { display: block; width: 26px; height: 26px; }
/* 开关在这里没有文字：把 Switch 内部的空文案槽收掉 ——
   否则它会占掉 8px 的列间距，轨道右缘就比卡片内容右缘缩进 8px（看着"没贴齐"）。 */
.skill-ops :deep(.switch-text) { display: none; }
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
  margin: 0 0 16px; padding: 14px;
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

/* ---------- 窄容器：两列 → 单列（卡片自己的内边距也跟着收一档） ---------- */
@container (max-width: 899px) {
  .cyli-prefs { --card-pad: 18px; }
  /* 并排 → 上下叠；卡内的两列（对话风格 / 回复长度）也收成一列 */
  .pref-cols, .pref-cols--solo { grid-template-columns: minmax(0, 1fr); }
  .pref-pair { grid-template-columns: minmax(0, 1fr); }
  .limit-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@container (max-width: 559px) {
  .cyli-prefs { --card-pad: 16px; --card-gap: 20px; }
  .limit-grid { grid-template-columns: minmax(0, 1fr); }
  /* 窄到头时先保住名字：名字允许省略号，操作区不换行、不压字号 */
  .skill-item { column-gap: 10px; }
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
