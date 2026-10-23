<script setup lang="ts">
/**
 * 设置：**分区**的参数编辑 + 只读系统信息与健康检查。
 *
 * 版式（模仿 Edge 设置页）：**贴左的导航栏 + 自己滚的内容窗格**，一次只渲染一区；
 * 整页不滚窗口 —— 分区高度不等，交给窗口滚的话滚动条会随切区出现/消失、整页宽度跟着变，
 * 左栏就横向抖一下。深链（`/app/settings#kb`）只负责切窗格。
 *
 * 分区按职责分成五组（左边栏顺序，组内即"改行为 → 改模型 → 改数据 → 改服务"的动线）：
 * · **通用**：1. 显示主题（皮肤注册表驱动，写 localStorage 立即生效）。
 * · **智能与模型**：2. 模型（任务分类配置 + 提供方目录与密钥，密钥只在各提供方详情里管理，
 *   页面上原来那个独立的「密钥」折叠区已删除）、
 *   3. Agent 设置（原「论文问答」：技能、对话风格、运行限额；模型本体在「模型」区）。
 * · **内容与索引**：4. 索引管理（检索规模 / 概览 / 论文索引表 / 对账 / chunk 元数据）、
 *   5. 任务队列（后台任务进度；2026-12 从「服务与系统」挪过来 —— 它改的是索引数据本身）、
 *   6. 搜索记录（本地搜索历史）。
 * · **服务与系统**：7. 在线解析（MinerU token 与默认参数，token **只写不回显**，可就地验证）、
 *   8. 系统信息（`SystemInfoPanel`：运行环境 / 存储空间 / 服务健康 / 索引存储状态）。
 * · **独立入口**：9. 回收站（软删论文的恢复 / 彻底删除；原来的独立页 /trash 已并到这里）——
 *   它是"对论文数据做不可逆操作"的兜底，与索引管理的日常入口不是一路，所以单列一组、不与
 *   内容与索引同组（用户口径 2026-12）。
 *
 * 分组标题本身是**折叠开关**：只收放左栏列表这一列，路由、当前分区、窗格内容一概不动；
 * 换到被收起的那一组时自动展开（见 `watch(active)`），所以深链 / 老标签永远不会落到
 * "入口看不见"的状态。折叠偏好只记本机（`cylit:settings-groups`），不进 hash、不进设置表。
 *
 * 原来的「常规参数」整区**已拆掉**（用户口径）：真生效的检索参数搬进「索引管理」的检索规模小节，
 * 任务工作线程数 / 页面渲染 dpi / 公式裁图 dpi / 默认生成模型四项直接删除 —— 前两个的读取点
 * 读的是环境变量、设置表里的值没人读；末一个已被模型系统的「默认模型」取代。
 * 老深链 `/settings#general` 由路由守卫改写到 `#kb`（见 `router/index.ts`）。
 *
 * 数据面：`GET /settings`（values + stored + editable 白名单）、`PUT /settings`、
 * MinerU 另走 `/mineru/config`（GET/PUT）与 `/mineru/verify`（POST）。
 * 健康与索引状态由 `SystemInfoPanel` 自己取（`/settings/health`、`/knowledge/index/status`）。
 * 设置写入 setting 表，重启后仍生效。
 */
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api } from '@/api/client';
import type { MineruConfig, MineruVerifyResult, ParseEngine } from '@/api/types';
import {
  loadSettings, reportError, state, toast, withLoading,
} from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import AppIcon from '@/components/AppIcon.vue';
import KbIndexPanel from '@/components/settings/KbIndexPanel.vue';
import AgentModelPanel from '@/components/settings/AgentModelPanel.vue';
import ModelPanel from '@/components/settings/ModelPanel.vue';
import SearchHistoryPanel from '@/components/settings/SearchHistoryPanel.vue';
import AppearancePanel from '@/components/settings/AppearancePanel.vue';
import TaskQueuePanel from '@/components/settings/TaskQueuePanel.vue';
import SystemInfoPanel from '@/components/settings/SystemInfoPanel.vue';
import TrashPanel from '@/components/trash/TrashPanel.vue';

/* ---------------------------- 分区导航 ---------------------------- */

/** 分区：`id` 同时是锚点（`/app/settings#kb` 直接落在「索引管理」）。
 *
 * 版式是**贴左导航 + 自滚内容窗格**：一次只渲染一区，于是不需要"滚到第几屏"的锚点对齐
 * —— 深链只负责切区，滚动由窗格自己在切区时归零。
 */
const SECTIONS = [
  { id: 'appearance', label: '显示主题', icon: 'sparkle', group: '通用', desc: '换深浅配色、字体与字号，点一下立即生效' },
  { id: 'models', label: '模型', icon: 'model', group: '智能与模型', desc: '按任务分配模型，密钥收在各提供方里' },
  // 「论文问答」→「Agent 设置」：这一区管的是**对话 Agent 本身**（技能 / 风格 / 运行上限），
  // 与「模型」（提供方、密钥、任务分类映射）职责不同，两者各自独立、不合并。
  // 图标换掉 `chat`：那枚是侧栏「AI 对话」的图标，两个入口长一样。改用专用的机器人头（见 AppIcon 的 agent）。
  { id: 'agent', label: 'Agent 设置', icon: 'agent', group: '智能与模型', desc: '调对话的技能、说话风格与运行上限' },
  // 图标同理：`knowledge`（知识图谱）是侧栏「知识库」的图标，索引管理改用数据库圆柱（AppIcon 的 index）。
  { id: 'kb', label: '索引管理', icon: 'index', group: '内容与索引', desc: '管理论文索引、向量检索及索引数据状态' },
  // 任务队列归「内容与索引」：队列里跑的是导入 / 解析 / 建索引，改的是索引数据本身，
  // 归到「服务与系统」会让人以为它只管服务健康。只挪分类，页内排版与筛选一概不动。
  { id: 'tasks', label: '任务队列', icon: 'task', group: '内容与索引', desc: '看导入、解析等后台任务的进度' },
  { id: 'history', label: '搜索记录', icon: 'search', group: '内容与索引', desc: '看本机保存的搜索记录，可清空' },
  { id: 'mineru', label: '在线解析', icon: 'bolt', group: '服务与系统', desc: '接在线解析服务，填密钥并验证' },
  // 系统信息保持独立：四张卡（运行环境 / 存储空间 / 服务健康 / 索引存储状态）是同一份只读
  // 健康视角，且与「在线解析」的凭证、参数毫无交集 —— 塞进同一页只会让两边都得滚。
  { id: 'about', label: '系统信息', icon: 'server', group: '服务与系统', desc: '看运行环境、存储占用与各服务健康' },
  // 回收站单列一组「独立入口」：低频、不可逆，不该排在日常入口中间，也不归「内容与索引」。
  { id: 'trash', label: '回收站', icon: 'trash', group: '独立入口', desc: '把删掉的论文恢复回来，或彻底删除' },
] as const;

type Section = typeof SECTIONS[number];
/** 分组名（'通用' | '智能与模型' | '内容与索引' | '服务与系统' | '独立入口'）。 */
type GroupName = Section['group'];

/** 搜索用的同义词表：用户按"想改什么"找，不会按分区名找（要深色不会去敲"显示主题"）。 */
const SECTION_KEYWORDS: Record<string, string[]> = {
  appearance: ['主题', '皮肤', '字号', '字体', '深色', '浅色', '换肤'],
  models: ['模型', '密钥', '提供方', '厂商', '连接', '思考'],
  agent: ['技能', '对话风格', '联网搜索', '限额', '问答'],
  kb: ['索引', '向量', '分块', '检索', '论文'],
  history: ['记录', '历史', '搜索'],
  trash: ['回收站', '恢复', '删除'],
  mineru: ['token', '解析', '在线'],
  tasks: ['任务', '重试', '进度', '队列'],
  about: ['版本', '健康', '系统', '只读', '环境', '存储', '磁盘'],
};

/** 搜索词只活在组件里，**不进 hash** —— hash 只表示分区，深链才不会被搜索状态污染。 */
const query = ref('');
const needle = computed(() => query.value.trim().toLowerCase());

/**
 * 按标签 / 一句话说明 / 同义词表过滤。命中口径就是"用户说出口的词"，
 * 分组标题跟着剩下的项走，所以没命中的分组自然整段消失，不用另写一套过滤。
 */
const visibleSections = computed(() => {
  const k = needle.value;
  if (!k) return SECTIONS;
  return SECTIONS.filter((s) => s.label.toLowerCase().includes(k)
    || s.desc.toLowerCase().includes(k)
    || (SECTION_KEYWORDS[s.id] ?? []).some((w) => w.toLowerCase().includes(k)));
});

/** 左栏分组：(已过滤的) 分区按 group 归纳，顺序就是 SECTIONS 里的先后。
 *  SECTIONS 里同一组的分区必须连着写 —— 这里只做"相邻同名归并"，不做重排。 */
interface NavGroup { name: GroupName; items: Section[] }
const navGroups = computed<NavGroup[]>(() => {
  const out: NavGroup[] = [];
  for (const s of visibleSections.value) {
    const last = out[out.length - 1];
    if (last && last.name === s.group) last.items.push(s);
    else out.push({ name: s.group, items: [s] });
  }
  return out;
});

/* ------------------------- 分组折叠（纯本机偏好） -------------------------
 * 只收放左栏列表这一列：路由不动、当前分区不动、窗格内容不动。
 * 三条硬约束：
 *   ① 搜索时一律展开 —— 否则"搜到了却看不见"；
 *   ② 切到被收起的那一组时自动展开（ensureGroupOpen），深链 / 老标签 / 前进后退都不会
 *      落到"当前页的入口藏起来了"；
 *   ③ 收起的是当前页所在的那一组时，标题提一档色（.has-active），左栏仍看得出"我在哪"。
 */
const GROUP_KEY = 'cylit:settings-groups';
/** 只认已知的分组名：改版删掉的分组名留在 localStorage 里也不该把新分组一起收起来。 */
const GROUP_NAMES: readonly GroupName[] = [...new Set(SECTIONS.map((s) => s.group))];

function readCollapsed(): Set<GroupName> {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(GROUP_KEY) || '[]');
    if (!Array.isArray(raw)) return new Set();
    return new Set(raw.filter((n): n is GroupName => GROUP_NAMES.includes(n as GroupName)));
  } catch {
    return new Set();                 // 隐私模式 / 内容被改坏：当作全展开
  }
}

const collapsed = ref<Set<GroupName>>(readCollapsed());

/** 是否收起：搜索命中时不收（约束 ①）。 */
function isCollapsed(name: GroupName): boolean {
  return !needle.value && collapsed.value.has(name);
}

function writeCollapsed(next: Set<GroupName>) {
  try {
    localStorage.setItem(GROUP_KEY, JSON.stringify([...next]));
  } catch {
    // 写不进去就只是"下次不记得"，不影响本次交互
  }
}

function toggleGroup(name: GroupName) {
  const next = new Set(collapsed.value);
  if (next.has(name)) next.delete(name); else next.add(name);
  collapsed.value = next;
  writeCollapsed(next);
}

/** 当前分区所在的组必须是展开的（约束 ②）。已是展开状态时不做任何事。 */
function ensureGroupOpen(id: string) {
  const g = SECTIONS.find((s) => s.id === id)?.group;
  if (g && collapsed.value.has(g)) toggleGroup(g);
}

const route = useRoute();
const router = useRouter();
const active = ref<string>(SECTIONS[0].id);

/** 当前分区：窗格抬头用它，section 里不再重复写标题。 */
const activeSection = computed(() => SECTIONS.find((s) => s.id === active.value) ?? SECTIONS[0]);

/** 内容窗格：窗口不滚了，它自己就是滚动容器，切区后要把它归零。 */
const paneRef = ref<HTMLElement | null>(null);
/** 等 DOM 换完再归零 —— 新分区多高要到渲染后才定，早一步设会被旧内容的溢出量顶回去。 */
function resetPaneScroll() {
  void nextTick(() => {
    if (paneRef.value) paneRef.value.scrollTop = 0;
  });
}

/** 点分区：换窗格 + 把 hash 写进地址栏（用 replace，不堆历史，也不触发任何滚动）。 */
function go(id: string) {
  active.value = id;
  if (route.hash !== `#${id}`) void router.replace({ hash: `#${id}` });
  resetPaneScroll();
}

/** 回车跳到第一个命中：列表已被搜索词收窄，"第一个"就是最接近想要的那个。 */
function jumpToFirst() {
  const first = visibleSections.value[0];
  if (first) go(first.id);
}

/** Esc 清空搜索、恢复全部分区；当前分区不动，选中态因此照样在。 */
function clearQuery() {
  query.value = '';
}

/** 深链与浏览器前进/后退：只切区（窗格归零由 resetPaneScroll 负责）。 */
watch(() => route.hash, (hash) => {
  const id = String(hash || '').replace('#', '');
  if (SECTIONS.some((s) => s.id === id)) {
    active.value = id;
    resetPaneScroll();
  }
});

/** 导航会超出可视区（窄屏横排、或视口矮）：切区后把当前项滚进可视区；没溢出就直接跳过。 */
const navRef = ref<HTMLElement | null>(null);
function revealActive() {
  void nextTick(() => {
    const nav = navRef.value;
    if (!nav || nav.scrollWidth <= nav.clientWidth + 1) return;
    nav.querySelector<HTMLElement>('.settings-nav-item.active')
      ?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });
}
// 换区先把那一组展开（折叠只影响列表，但当前页的入口必须在场），再做横排的滚动兜底
watch(active, () => {
  ensureGroupOpen(active.value);
  revealActive();
});

const loadError = ref('');

/** 首次进入用骨架提示，加载失败给明确错误而不是空白页。 */
const loading = computed(() => !!state.loading.settings && !state.settings && !loadError.value);

/* ---------------------------- 载入 ---------------------------- */

async function load() {
  loadError.value = '';
  try {
    await withLoading('settings', () => loadSettings(), { silent: true });
  } catch (err) {
    loadError.value = `设置加载失败：${(err as Error)?.message ?? String(err)}`;
    reportError(err, '设置加载失败');
  }
}


/* ============================ 在线解析（MinerU） ============================
 * MinerU 是**联网的第三方解析服务**：导入时把 PDF 交给它，公式/表格比本地解析更强。
 * 它要一个 token（官方每 90 天过期），所以这一区的东西分两类：
 * - **凭证**：token 输入框 + 保存 / 验证 / 清除（token 只写不回显：后端只给掩码）；
 * - **默认参数**：模型版本、语言、默认解析引擎（导入弹窗的初值）。
 *
 * 与「索引管理」里那些检索参数的区别：这些**不进 `PUT /settings`**，而是各自的 `/mineru/config`。
 * 原因是 token 属于敏感值，不该混在通用设置白名单里（也方便后端单独做权限与掩码）。
 * ========================================================================== */

interface MineruForm {
  /** 新 token：**空 = 本次不改**（后端拿不到 token 就不动已存的那个） */
  token: string;
  engine_default: ParseEngine;
  model_version: 'pipeline' | 'vlm';
  language: string;
}

const MINERU_MODELS: ReadonlyArray<{ value: 'pipeline' | 'vlm'; label: string }> = [
  { value: 'pipeline', label: 'pipeline（默认；版面 + 公式更稳）' },
  { value: 'vlm', label: 'vlm（VLM 大模型；复杂版面更强）' },
];

const MINERU_LANGS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'en', label: 'en（英文论文，默认）' },
  { value: 'ch', label: 'ch（中文文献）' },
];

const mineruForm = reactive<MineruForm>({
  token: '', engine_default: 'local', model_version: 'pipeline', language: 'en',
});
/** 服务端**当前生效**的那一份：用来算"改了没有"，以及撤销 / 保存成功后回填。 */
const mineruBase = reactive<MineruConfig>({
  has_token: false, token_masked: null, engine_default: 'local',
  model_version: 'pipeline', language: 'en',
});
const mineruBusy = ref('');
const mineruError = ref('');
const mineruVerify = ref<MineruVerifyResult | null>(null);

const mineruTokenText = computed(
  () => mineruBase.token_masked || (mineruBase.has_token ? '已保存（掩码不可用）' : ''),
);
/** 只在这些东西真的变了（或填了新 token）时才允许保存，避免无意义请求。 */
const mineruChanged = computed(() => !!mineruForm.token
  || mineruForm.engine_default !== mineruBase.engine_default
  || mineruForm.model_version !== mineruBase.model_version
  || mineruForm.language !== mineruBase.language);

/** 把后端返回的配置同时灌进"表单值"与"服务端权威值"（保存成功后也走它）。 */
function fillMineru(cfg: MineruConfig) {
  mineruBase.has_token = cfg?.has_token === true;
  mineruBase.token_masked = cfg?.token_masked ?? null;
  mineruBase.token_source = cfg?.token_source ?? null;
  mineruBase.engine_default = cfg?.engine_default === 'mineru' ? 'mineru' : 'local';
  mineruBase.model_version = cfg?.model_version === 'vlm' ? 'vlm' : 'pipeline';
  mineruBase.language = cfg?.language ? String(cfg.language) : 'en';
  mineruBase.base_url = cfg?.base_url ?? '';
  mineruBase.token_file = cfg?.token_file ?? null;
  mineruBase.online_ready = cfg?.online_ready === true;
  mineruForm.token = '';
  mineruForm.engine_default = mineruBase.engine_default;
  mineruForm.model_version = mineruBase.model_version;
  mineruForm.language = mineruBase.language;
}

async function loadMineru() {
  mineruError.value = '';
  try {
    fillMineru(await api.get<MineruConfig>('/mineru/config'));
  } catch (err) {
    // 后端还没上 `/mineru/*` 时这里必然失败：给一句明确的话，而不是让区块空白
    mineruError.value = `MinerU 配置读取失败：${(err as Error)?.message ?? String(err)}`;
  }
}

/** 后端 `envelope(message=…)` 的文案（client 挂在 `__meta` 上，与论文库同一写法）。 */
function metaMessage(res: any): string {
  return String(res?.__meta?.message ?? '');
}

/** 保存 token 与默认参数。**只提交变更项**（后端按字段可选处理）。 */
async function saveMineru() {
  if (!mineruChanged.value) { toast('没有需要保存的改动', 'warn'); return; }
  const patch: Record<string, unknown> = {};
  if (mineruForm.token) patch.token = mineruForm.token;
  if (mineruForm.engine_default !== mineruBase.engine_default) {
    patch.engine_default = mineruForm.engine_default;
  }
  if (mineruForm.model_version !== mineruBase.model_version) {
    patch.model_version = mineruForm.model_version;
  }
  if (mineruForm.language !== mineruBase.language) patch.language = mineruForm.language;
  mineruBusy.value = 'save';
  try {
    const res = await api.put<MineruConfig>('/mineru/config', patch);
    fillMineru(res);
    mineruVerify.value = null;         // 换了 token / 参数：上次的验证结论已经不作数了
    toast(metaMessage(res) || 'MinerU 配置已保存', 'ok', 3600);
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 6000);
  } finally {
    mineruBusy.value = '';
  }
}

/**
 * 验证 token：后端会真的打一次 MinerU 接口。
 *
 * ⚠️ **失败时 HTTP 仍然是 200**（`ok:false` + `state`），所以这里按 `ok`/`state` 判成败，
 * 绝不按 HTTP 状态码；`message` 是后端给的一句可执行中文提示，原样展示（加前缀会毁掉指引）。
 */
async function verifyMineru() {
  mineruBusy.value = 'verify';
  mineruVerify.value = null;
  try {
    const res = await api.post<MineruVerifyResult>('/mineru/verify', {});
    mineruVerify.value = res;
    toast(res?.message || (res?.ok ? 'Token 有效' : 'Token 验证未通过'),
      res?.ok ? 'ok' : 'error', res?.ok ? 3600 : 7000);
    await loadMineru();                // 验证顺手刷新掩码与 online_ready
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 6000);
    mineruVerify.value = { ok: false, state: 'network', message: (err as Error)?.message };
  } finally {
    mineruBusy.value = '';
  }
}

/** 清除 token：不可逆（要重新申请/粘贴），用原生 confirm 二次确认。 */
async function clearMineruToken() {
  if (!(await askConfirm({
    title: '清除 MinerU token',
    body: '清除后「在线解析」不可用，需要重新填入',
    danger: true,
    confirmLabel: '清除',
  }))) return;
  mineruBusy.value = 'clear';
  try {
    const res = await api.put<MineruConfig>('/mineru/config', { clear_token: true });
    fillMineru(res);
    mineruVerify.value = null;
    toast(metaMessage(res) || '已清除 MinerU token', 'ok', 3200);
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 6000);
  } finally {
    mineruBusy.value = '';
  }
}

onMounted(async () => {
  await load();
  await loadMineru();
  // 深链（/app/settings#kb）：只切到对应窗格 —— 一次只渲染一区，窗格一律从顶部开始。
  const id = String(route.hash || '').replace('#', '');
  if (id && SECTIONS.some((s) => s.id === id)) active.value = id;
  // 首次进来 active 可能压根没变过（默认区），watch 就不会触发 —— 这里补一次展开
  ensureGroupOpen(active.value);
  resetPaneScroll();
});
</script>

<template>
  <!-- 版式：贴左的导航栏 + 自己滚的内容窗格（模仿 Edge 设置页）。
       一次只渲染一区：切区把窗格滚回顶部，深链只负责选窗格。 -->
  <div class="settings-layout">
    <aside class="settings-rail">
      <h1 class="settings-rail-title">设置</h1>
      <!-- 搜索只筛左栏列表：窗格仍显示当前分区，回车才换区 -->
      <label class="settings-search">
        <AppIcon name="search" :size="13" />
        <input
          v-model="query"
          type="search"
          placeholder="搜索设置"
          aria-label="搜索设置"
          @keydown.enter.prevent="jumpToFirst"
          @keydown.esc.prevent="clearQuery"
        />
      </label>
      <nav class="settings-nav" ref="navRef" aria-label="设置分类">
        <div v-for="g in navGroups" :key="g.name" class="nav-group">
          <!-- 分组标题就是折叠开关：aria-expanded 跟着实际状态走；
               收起时若当前页就在这一组里，标题提一档色（has-active），仍看得出"我在哪"。 -->
          <button
            type="button"
            class="settings-nav-group"
            :class="{ 'has-active': g.items.some((s) => s.id === active) }"
            :aria-expanded="!isCollapsed(g.name)"
            :title="(isCollapsed(g.name) ? '展开「' : '收起「') + g.name + '」'"
            @click="toggleGroup(g.name)"
          >
            <span class="settings-nav-group-label">{{ g.name }}</span>
            <span class="nav-caret" :class="{ open: !isCollapsed(g.name) }" aria-hidden="true"></span>
          </button>
          <div class="nav-group-items" :class="{ collapsed: isCollapsed(g.name) }">
            <button
              v-for="s in g.items"
              :key="s.id"
              type="button"
              class="settings-nav-item"
              :class="{ active: active === s.id }"
              :aria-current="active === s.id ? 'page' : undefined"
              :title="s.label"
              @click="go(s.id)"
            >
              <AppIcon class="settings-nav-icon" :name="s.icon" :size="15" /><span class="settings-nav-label">{{ s.label }}</span>
            </button>
          </div>
        </div>
      </nav>
      <p class="settings-empty" v-if="!visibleSections.length">没有匹配的设置</p>
    </aside>
    <div class="settings-pane" ref="paneRef">
      <!-- 页头：图标 + 一个大标题（Windows 设置那种「一页一个大标题、不挂二级说明」）。
           副标题整句去掉（用户口径 2026-12：移除二级说明）；desc 只留给左栏搜索的同义词匹配，
           想知道这一区管什么，悬停标题即可（title 上挂着它）。 -->
      <header class="pane-head">
        <span class="pane-head-icon"><AppIcon :name="activeSection.icon" :size="17" /></span>
        <h2 :title="activeSection.desc">{{ activeSection.label }}</h2>
      </header>

    <!-- 首次加载 -->
    <div class="empty card" v-if="loading">正在加载设置…</div>

    <!-- 加载失败（可重试） -->
    <div class="empty card" v-else-if="loadError">
      <p class="load-error">{{ loadError }}</p>
      <button class="btn sm" @click="load">
        <AppIcon name="refresh" :size="14" />重试
      </button>
    </div>

    <template v-else>
      <!-- ① 模型：原先独立成页，"模型管理"本来也是设置，于是并进来当一区 -->
      <section id="models" class="sec" v-if="active === 'models'">
        <ModelPanel />
      </section>

      <!-- ② 索引管理（管理/诊断，即时生效，没有"保存"） -->
      <section id="kb" class="sec" v-if="active === 'kb'">
        <KbIndexPanel />
      </section>

      <!-- ③ Agent 设置（原「论文问答」）：技能、对话风格与运行限额（模型本体在「模型」区） -->
      <section id="agent" class="sec" v-if="active === 'agent'">
        <AgentModelPanel />
      </section>

      <!-- ④ 显示主题 + 显示大小：换肤与字号都即时生效，不放"保存" -->
      <section id="appearance" class="sec" v-if="active === 'appearance'">
        <AppearancePanel />
      </section>

      <!-- ⑤ 在线解析（MinerU）：token 与默认参数，即时保存（没有"保存设置"那一步） -->
      <section id="mineru" class="sec" v-if="active === 'mineru'">
        <div class="sec-actions">
          <span class="badge accent" v-if="mineruBase.online_ready">已就绪</span>
          <span class="badge warn" v-else-if="mineruBase.has_token">已存 token（未验证）</span>
          <span class="badge" v-else>未配置 token</span>
          <span class="spacer"></span>
          <button class="btn sm" :disabled="!!mineruBusy || !mineruChanged" @click="saveMineru">
            <AppIcon name="check" :size="15" />{{ mineruBusy === 'save' ? '保存中…' : '保存' }}
          </button>
          <button
            class="btn sm"
            :disabled="!!mineruBusy || !mineruBase.has_token"
            :title="mineruBase.has_token ? '真的打一次 MinerU 接口，确认 token 还能用' : '还没有 token 可验证'"
            @click="verifyMineru"
          >
            <AppIcon name="bolt" :size="15" />{{ mineruBusy === 'verify' ? '验证中…' : '验证' }}
          </button>
        </div>

        <div class="cards-2">
          <div class="card">
            <div class="card-head">凭证</div>
            <div class="card-body">
              <label class="field">
                <span class="field-top">API token</span>
                <input
                  type="password"
                  autocomplete="off"
                  spellcheck="false"
                  v-model="mineruForm.token"
                  :placeholder="mineruBase.has_token
                    ? `已保存 ${mineruTokenText}（留空 = 不修改）`
                    : '粘贴 mineru.net 申请到的 token'"
                />
              </label>
              <div class="row row-gap">
                <button
                  class="btn sm danger"
                  :disabled="!!mineruBusy || !mineruBase.has_token"
                  @click="clearMineruToken"
                >
                  <AppIcon name="trash" :size="14" />{{ mineruBusy === 'clear' ? '清除中…' : '清除 token' }}
                </button>
                <span class="spacer"></span>
              </div>
              <!-- 验证结论：有效 / 无效 / 过期 / 连不上，各自给不同的语义色与可执行提示 -->
              <div class="row row-gap" v-if="mineruVerify">
                <span class="badge" :class="mineruVerify.ok ? 'ok' : 'danger'">
                  <AppIcon :name="mineruVerify.ok ? 'check_circle' : 'error'" :size="13" />
                  {{ mineruVerify.ok ? 'Token 有效' : 'Token 不可用' }}
                </span>
                <span class="tiny faint" v-if="mineruVerify.latency_ms !== null
                  && mineruVerify.latency_ms !== undefined">
                  延迟 {{ mineruVerify.latency_ms }} ms
                </span>
                <span class="tiny faint" v-if="mineruVerify.http_status">
                  HTTP {{ mineruVerify.http_status }}<template v-if="mineruVerify.code !== null
                    && mineruVerify.code !== undefined"> / code {{ mineruVerify.code }}</template>
                </span>
              </div>
              <p
                class="tiny verify-msg"
                :class="mineruVerify?.ok ? 'ok' : 'bad'"
                v-if="mineruVerify?.message"
              >{{ mineruVerify.message }}</p>
              <p class="tiny faint meta-line">
                <span>token 文件 <span class="mono">{{ mineruBase.token_file || '—' }}</span></span>
                <span v-if="mineruBase.base_url">服务地址 <span class="mono">{{ mineruBase.base_url }}</span></span>
              </p>
              <p class="load-error tiny" v-if="mineruError">{{ mineruError }}</p>
            </div>
          </div>

          <div class="card">
            <div class="card-head">默认参数</div>
            <div class="card-body">
              <div class="grid cols-2">
                <label class="field">
                  模型版本
                  <select v-model="mineruForm.model_version">
                    <option v-for="m in MINERU_MODELS" :key="m.value" :value="m.value">{{ m.label }}</option>
                  </select>
                </label>
                <label class="field">
                  语言（影响 OCR）
                  <select v-model="mineruForm.language">
                    <option v-for="l in MINERU_LANGS" :key="l.value" :value="l.value">{{ l.label }}</option>
                  </select>
                </label>
              </div>
              <label class="field field-block">
                默认解析引擎
                <select v-model="mineruForm.engine_default">
                  <option value="local">本地解析（离线、快）</option>
                  <option value="mineru">在线解析（MinerU 云端）</option>
                </select>
              </label>
              <p class="tiny faint hint">仅作导入弹窗初值，导入时仍可逐篇改。</p>
            </div>
          </div>
        </div>
      </section>

      <!-- ⑥ 任务队列：原「任务」页的精简版（进度 / 取消 · 重试 · 删除 + 实时指示灯） -->
      <section id="tasks" class="sec" v-if="active === 'tasks'">
        <TaskQueuePanel />
      </section>

      <!-- ⑦ 搜索记录（本机 localStorage） -->
      <section id="history" class="sec" v-if="active === 'history'">
        <SearchHistoryPanel />
      </section>

      <!-- ⑧ 回收站：原独立页 /trash，老链接与老标签由路由重定向落到这一区 -->
      <section id="trash" class="sec" v-if="active === 'trash'">
        <TrashPanel />
      </section>

      <!-- ⑨ 系统信息（只读）：运行环境 / 存储空间 / 服务健康 / 索引存储状态，四张卡纵向排 -->
      <section id="about" class="sec" v-if="active === 'about'">
        <SystemInfoPanel />
      </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* 设置页私有细节（其余样式复用 base.css 的通用类） */
.load-error { color: var(--danger); }
/* 字段下面那行提示：一行、小字、离字段 8px（不留段落式留白） */
.hint { margin: 8px 0 0; }
/* token 文件 / 服务地址：并成一行，中间 16px 分隔 */
.meta-line { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 8px 0 0; }

/**
 * 版式：**钉死的左栏 + 自己滚的内容列**（模仿 Edge 设置页，一次只渲染一区）。
 *
 * 这里是整页而不是弹窗：导航栏从内容区最左边起、208px 固定宽，**不画边框也不画投影** ——
 * 圆角、边框、投影叠上去就是一张浮在页面里的卡片，与"整页设置"的定位不符。
 *
 * 整页**不滚窗口**：高度钉成"视口 − 顶栏 − .app-main 上下内边距"，滚动只发生在两栏内部。
 * 为什么必须钉：各分区高度不等，交给窗口滚就会随切区出现/消失一条滚动条，整页宽度跟着变，
 * 左栏于是横向抖一下 —— 用户报的正是这个。算式与 base.css:269 的内边距（18 / 64）绑死，
 * 那边改了这里必须跟着改；861–1024 段的内边距是 14 / 56，见下面单独的媒体查询。
 *
 * 两栏之间**没有分隔线**：分层交给留白（左栏右留白 24、内容列左留白 24）与分组标题，
 * 一根竖线反而把整页切成两个盒子。
 */
.settings-layout {
  --rail-col: 208px;
  display: grid;
  grid-template-columns: var(--rail-col) minmax(0, 1fr);
  /* 显式写行高：两栏都靠 height:100% 撑满，行若是 auto 就退回按内容算，窗格的滚动区会变矮 */
  grid-template-rows: minmax(0, 1fr);
  /* 只扣「顶栏」：设置页的上下内边距都是 0（见 base.css 的 .app-main.settings），
     再扣 64 就会在窗口底部长出一条空白带、并把列表提前裁断（用户口径 2026-12-02）。
     顶部那 18px 也不再扣 —— 那正是"只有设置页被裁"的来源：它留在滚动区外面，
     窗格上沿于是比别页高出一截、还露一条浅色带（用户口径 2026-10）。 */
  height: calc(100dvh - var(--header-h));
  overflow: hidden;
}
.settings-rail {
  /* 不吸顶：容器已经钉住了，整栏跟着容器走即可（吸顶 + 定高反而会互相打架） */
  position: static;
  /* 不撑满整列：height:100% 把整列高度都算成侧栏，最后一项下面那片空白于是也成了侧栏的。
     改成只占内容高度、超过一屏才在栏内滚 —— 空白还给页面，末尾几项照样点得到。 */
  align-self: start;
  height: auto; max-height: 100%; overflow-y: auto;
  /* 栏内各块的间距由下面各自的 margin 给，不叠 flex gap（两处都管会变成加倍的缝） */
  display: flex; flex-direction: column; gap: 0;
  /* 上留白 18：与窗格的 padding-top 同一个数（那 18px 从 .app-main 挪到了两栏自己身上，
     见 base.css 的 .app-main.settings），所以标题的顶线位置一点没动；
     左 6 给焦点环留余量，右 24 顶替被删掉的那条竖发丝线（栏宽 208 因此留给可点项 178） */
  padding: 18px 24px 12px 6px;
}
/* 左栏抬头：整页唯一的 h1，比右侧页头标题（22）小一档，不抢内容。
 *
 * 顶线要和右侧页头标题对齐 —— 两边都有半行距，字号不同就差出来了：
 *   右：框顶 70（.pane-head 无上留白）+ 2.25（图标 32 撑起行高，27.5 的 h2 垂直居中）
 *       + (22 × 1.25 − 22) / 2 = 72.25 + 2.75 ≈ 75
 *   左：框顶 70 + 2（下面这行 margin-top）+ (18 × 1.3 − 18) / 2 = 72 + 2.7 ≈ 74.7
 * 差 0.3px，肉眼即齐（实测 em 顶 75 / 74.7）。line-height 不能沿用继承的 1.55：
 * 那是 4.5px 半行距，比 2.7 深，只有负留白才对得齐。 */
.settings-rail-title {
  margin: 2px 0 12px; padding: 0 10px;
  font-size: calc(18px * var(--ts, 1)); font-weight: 650; letter-spacing: -.01em;
  line-height: 1.3;
}
/* 搜索框：与论文库那条同构（图标 + 无边框输入），整行铺满。
   高 30 比导航项（32）矮一档，圆角收到 --r-sm，读起来是"栏内工具"而不是一枚按钮。 */
.settings-search {
  display: flex; align-items: center; gap: 8px;
  height: 30px; padding: 0 10px; margin-bottom: 12px;
  border-radius: var(--r-sm);
  background: var(--bg-sunken); border: 1px solid var(--border); color: var(--text-faint);
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.settings-search:focus-within {
  border-color: var(--accent); box-shadow: var(--ring); background: var(--bg-elev);
}
.settings-search input {
  width: 100%; min-width: 0; height: 100%; padding: 0;
  border: 0; border-radius: 0; background: transparent; box-shadow: none;
  font-size: calc(12px * var(--ts, 1));
}
.settings-search input:hover,
.settings-search input:focus { background: transparent; border: 0; box-shadow: none; }
.settings-empty { margin: 0; font-size: calc(12.5px * var(--ts, 1)); color: var(--text-faint); }
.settings-nav { display: flex; flex-direction: column; gap: 0; }
/* 分组 = 标题（折叠开关）+ 可点项。标题把左栏切成「通用 / 智能与模型 / 内容与索引 /
   服务与系统 / 独立入口」五段，只当路标，所以降一档色、不抢可点项；**不做大写转换** ——
   中文没有大小写，uppercase 只会在混排的英文（API、MinerU）上突然变形。
   收起只把 .nav-group-items 藏掉：组标题留在原处，随时点得开。 */
.nav-group { display: block; }
.nav-group-items.collapsed { display: none; }
.settings-nav-group {
  display: flex; align-items: center; gap: 6px; width: 100%; height: 20px;
  margin: 14px 0 4px; padding: 0 10px;
  background: transparent; border: 0; border-radius: var(--r-sm); cursor: pointer;
  font-family: inherit; font-size: calc(11.5px * var(--ts, 1)); font-weight: 650; letter-spacing: .02em;
  color: var(--text-faint); text-align: left;
  transition: color var(--dur-fast) var(--ease);
}
/* 首组不吃 14px：顶线交给搜索框的 margin-bottom，与整页其余留白对齐 */
.nav-group:first-child .settings-nav-group { margin-top: 0; }
.settings-nav-group:hover { color: var(--text-dim); }
/* 收起时当前页就在这一组里：标题提一档色 —— 不然左栏看不出"我现在在哪" */
.settings-nav-group.has-active { color: var(--text-dim); }
.settings-nav-group:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.settings-nav-group-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 折叠箭头：两条描边拼一个 V（展开朝下、收起朝右），不引新图标。
   rotate(45deg) 时"┘"的尖朝下、rotate(-45deg) 朝右 —— 中间只差 90°，转起来干净。 */
.nav-caret {
  flex: none; width: 5px; height: 5px; margin-left: auto;
  border-right: 1.4px solid currentColor; border-bottom: 1.4px solid currentColor;
  transform: rotate(-45deg); transition: transform var(--dur-fast) var(--ease);
}
.nav-caret.open { transform: rotate(45deg); }
/* 可点项：32 高（--control-h）、14 圆角（--r-sm）、左右 10 内边距 —— 比按钮松、比卡片紧。
   当前项 = --bg-hover 底 + 正文色 + 图标转 --accent，不做整行彩色胶囊；
   hover 走更浅一档的 --bg-subtle：两档必须分得开，否则"鼠标停着的"和"正看着的"长得一样。 */
.settings-nav-item {
  display: flex; align-items: center; gap: 8px; width: 100%;
  height: var(--control-h); padding: 0 10px; border-radius: var(--r-sm);
  background: transparent; color: var(--text-dim);
  font-family: inherit; font-size: calc(13px * var(--ts, 1)); font-weight: 500; text-align: left; cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
/* 相邻项之间 2px：圆角 14 的行贴起来会连成一片。缝交给这条外边距而不是 nav 的 gap ——
   gap 会把分组标题上下那两条 16 / 6 也一起加宽。 */
.settings-nav-item + .settings-nav-item { margin-top: 2px; }
.settings-nav-item:hover { background: var(--bg-subtle); color: var(--text); }
.settings-nav-item.active { background: var(--bg-hover); color: var(--text); font-size: calc(13.5px * var(--ts, 1)); font-weight: 650; }
/* 图标比文字暗一档：不选时是路标，选中时才升到强调色，视线跟着当前分区走 */
.settings-nav-icon { flex: none; color: var(--text-faint); transition: color var(--dur-fast) var(--ease); }
.settings-nav-item:hover .settings-nav-icon { color: var(--text-dim); }
.settings-nav-item.active .settings-nav-icon { color: var(--accent); }
/* 描边必须画在元素内：左栏与横排的 nav 都是滚动容器，画在外面的那圈会被裁掉（用户截图里的黑边） */
.settings-nav-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.settings-nav-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.settings-pane {
  min-width: 0; display: flex; flex-direction: column;
  /* 左栏钉死之后，滚动全部落在这里（全页只有这一层滚动） */
  height: 100%; overflow-y: auto; overscroll-behavior: contain;
  /* 滚动条槽常驻：分区一高一矮，滚动条一会儿有、一会儿没有，不留槽内容宽度就会跟着跳 */
  scrollbar-gutter: stable;
  /* 只补左边：右边那 24 由 base.css 的 `.app-main.settings` 给（左内边距那边是 0），
     这里再补一份就变成 48，内容会明显偏左 —— 两侧留白不等宽，比宽度本身更刺眼。 */
  padding-left: var(--sp-5); padding-right: 0;
  /* 顶 18 / 底 24：上下呼吸空间都跟着内容一起滚（主区那边已经是 0，见 base.css）。
     顶部这 18px 必须在这里给、不能留在 .app-main 上 —— 那样它就在滚动区外面，
     内容滚上去会被窗格上沿切平；放进来之后是滚走，内容直接钻到白色顶栏底下。 */
  padding-top: 18px; padding-bottom: var(--sp-5);
}

/* 内容列封顶 1200 并居中：再宽两列卡片之间就空得发慌，正文行也散。
   居中只能交给子项的 auto 外边 —— padding 的百分比算的是栅格区宽度，把常驻的滚动条槽
   也算进去了，那样内容会整体左偏半个滚动条宽。 */
.settings-pane > * { width: 100%; max-width: 1200px; margin-inline: auto; }
/* 纵向节奏：抬头到第一块 16，块与块 24。写外边距而不是 flex gap —— 两种间距只有一处能覆盖，
   gap 办不到，负外边距那套算下来更难读。 */
.settings-pane > * + * { margin-top: var(--sp-5); }
.settings-pane > .pane-head + * { margin-top: var(--sp-4); }
/* 页头：图标底 + 大标题。**不画分隔线** —— 抬头只是标签，一根线会把它变成"表头"。
   字号 22（原 15 + 一句副标题）：照 Windows 设置的做法，一页就一个大标题，下面直接进内容；
   副标题那行删除后，首屏反而多出 18px 给内容。图标底也抬到 32，与大标题同一档重量。 */
.pane-head {
  display: flex; align-items: center; gap: 10px;
  padding: 0;
}
.pane-head-icon {
  display: grid; place-items: center; flex: none;
  width: 32px; height: 32px; border-radius: var(--r-sm);
  background: var(--accent-soft); color: var(--accent-text);
}
.pane-head h2 {
  margin: 0; min-width: 0;
  font-size: calc(22px * var(--ts, 1)); font-weight: 650; line-height: 1.25; letter-spacing: -.02em;
}

/* 861–1024：.app-main 的左右内边距收成 12 / 56（base.css），高度算式不用再换档 ——
   顶部在设置页已经被归零（base.css 的 .app-main.settings），只有「视口 − 顶栏」。
   右侧是"补差"：这一档 .app-main.settings 给的是 12，补 4 才够 16。 */
@media (max-width: 1024px) {
  .settings-pane { padding-left: var(--sp-4); padding-right: 4px; padding-top: 14px; }
  .settings-rail { padding-top: 14px; }
  .cards-2 { grid-template-columns: minmax(0, 1fr); }
}

/* 窄屏：导航折到内容上方、横排可滚（不挤成两列，也不把内容压到 200px 宽）。
   横向滚动交给 nav 自己 —— "设置"那一格始终留在左边；当前项由 revealActive 滚进可视区。 */
@media (max-width: 860px) {
  /* 手机回到窗口滚动：一屏内的滚动区很难用 —— 手势总被内层吃掉，滚到底也换不了区 */
  .settings-layout {
    grid-template-columns: minmax(0, 1fr); row-gap: 14px;
    height: auto; overflow: visible;
    /* 主区左内边距在设置页是 0：那是给桌面「左栏贴主区左缘」用的，窄屏单列会贴到视口边缘，
       这里补回 12px，与主区右侧的 12 对称 */
    padding-left: var(--sp-3);
  }
  /* 这一档起留白由 .settings-layout 统一给：窗格自己再补就成了双层 */
  .settings-pane {
    padding-left: 0; padding-right: 0;
    height: auto; overflow: visible;
  }
  /* 折成两行："设置" + 搜索框一行，导航自己占满下一行（横排要整条宽度才滚得顺） */
  .settings-rail {
    position: static; height: auto; max-height: none; flex-direction: row; flex-wrap: wrap; align-items: center;
    gap: 10px; padding: 0 0 10px; overflow: visible;
    border-bottom: 1px solid var(--divider);
  }
  .settings-rail-title { flex: none; margin: 0; padding: 0; }
  .settings-search { flex: 1 1 140px; min-width: 0; margin-bottom: 0; }
  .settings-empty { flex: 1 0 100%; }
  .settings-nav {
    flex: 1 0 100%; min-width: 0; flex-direction: row; gap: 4px;
    overflow-x: auto; scrollbar-width: none;
  }
  .settings-nav::-webkit-scrollbar { display: none; }
  /* flex: none 是关键：不然项会被挤扁，横排就变成"每项都省略号" */
  .settings-nav-item { width: auto; flex: none; white-space: nowrap; }
  /* 横排时相邻项之间的缝交给 nav 的 gap 4，竖排那条 2px 要撤掉 */
  .settings-nav-item + .settings-nav-item { margin-top: 0; }
  /* 横排时分组标题只剩噪声（一段一段断开反而更难扫），直接不显示；
     组容器与组内容都摊平成 display:contents，项才铺得进这一条横排。
     ⚠️ 收起态在这里必须让位：标题已经藏了，再藏项就等于凭空少几个入口。 */
  .settings-nav-group { display: none; }
  .settings-nav > .nav-group,
  .settings-nav .nav-group-items,
  .settings-nav .nav-group-items.collapsed { display: contents; }
  .pane-head { padding-top: 0; }
}

/* ≤560：内边距收到 12（主区这一档本来就是 12，窗格不再补），
   搜索框独占一行 —— 与「设置」挤在同一行时只剩 80px 可用，等于写了看不见。 */
@media (max-width: 560px) {
  .settings-rail { gap: 8px; padding-bottom: 8px; }
  .settings-search { flex: 1 0 100%; }
}

/* 分区：只放内容（标题在窗格抬头）。区内再叠一层块间距 24 —— 「显示主题」区里就是两个面板，
   间距得和页面其它块同一个数，否则同一页出现两种节奏。 */
.sec { min-width: 0; display: flex; flex-direction: column; gap: var(--sp-5); scroll-margin-top: calc(var(--header-h) + var(--sp-5)); }
.sec-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

/* 卡片两列（短卡并排；长表要整宽就自己加 grid-column: 1 / -1）。
   间距只由 gap 给 —— base.css 的 .card + .card 在这里会把第二张顶下去 12px，要抹掉。 */
.cards-2 {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px; align-items: start;
}
.cards-2 > .card + .card { margin-top: 0; }

/* 表单项：标签一行 */
.field-top { display: flex; align-items: baseline; gap: 8px; width: 100%; }

/* 在线解析（MinerU）：卡片内的行间距与验证结论的语义色 */
.row-gap { margin-top: 8px; }
/* token 输入框不跟着 `.field > input` 的 340px 上限走：token 很长，宽一点更好核对 */
.field input[type="password"] { max-width: 100%; width: 100%; font-family: var(--mono); }
.field-block { margin-top: 8px; }
/* 验证结论：成功走 --ok、失败走 --danger（与徽章同一套语义色，不用新色） */
.verify-msg { margin: 8px 0 0; line-height: 1.6; }
.verify-msg.ok { color: var(--ok); }
.verify-msg.bad { color: var(--danger); }
</style>
