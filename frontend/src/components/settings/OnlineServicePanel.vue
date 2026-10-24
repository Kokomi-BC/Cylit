<script setup lang="ts">
/**
 * 设置页「在线服务」：三个解析 / 检索服务共用一页。
 *
 * · **本地解析**（PyMuPDF，离线）：读页开关 / 读页 dpi / 插图两个开关；
 * · **在线解析**（MinerU）：token + 模型版本 + OCR 语言 + 三项开关 + 页码范围；
 * · **网联搜索**（DeepSeek）：表单与读写都在 WebSearchSettings（与 Cyli 偏好的技能行同一份）。
 *
 * 两个原则：
 * ① 页头只有状态，**动作只有底部一排**（测试服务 / 清除密钥 / 保存设置）——不出现两处同样的按钮；
 * ② 每个服务的配置与验证状态**各存一份**，互不覆盖；保存时只提交真的改过的字段。
 *
 * 数据面：本地与 MinerU 的"导入默认值"都在 /parse/config（一个键一套，见 parse_prefs），
 * MinerU 的 token / 引擎 / 模型 / 语言在 /mineru/config；验证走 POST /mineru/verify。
 * 两个 verify 接口失败时 HTTP 仍是 200，成败只看返回体的 ok。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { api } from '@/api/client';
import { loadTasks, paperTitle, state, toast } from '@/stores/app';
import { fmtRelative, taskStatus } from '@/utils/format';
import { askConfirm } from '@/stores/confirm';
import type { MineruConfig, MineruVerifyResult, ParseConfig, ParseEngine } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import Switch from '@/components/Switch.vue';
import WebSearchSettings from './WebSearchSettings.vue';

type ServiceKey = 'local' | 'mineru' | 'websearch';

const services: ReadonlyArray<{ id: ServiceKey; label: string; sub: string; icon: string }> = [
  { id: 'local', label: '本地解析', sub: 'PyMuPDF · 离线提取', icon: 'document' },
  { id: 'mineru', label: '在线解析', sub: 'MinerU · 云端提取', icon: 'bolt' },
  { id: 'websearch', label: '网联搜索', sub: 'DeepSeek · 联网检索', icon: 'search' },
];

const service = ref<ServiceKey>('local');
/** 只有在线服务有远程凭据要验 / 要清。 */
const isOnline = computed(() => service.value !== 'local');

/* ---------------- 解析默认值（本地 + MinerU 共用一套键：/parse/config） ---------------- */

const prefs = reactive<ParseConfig>({
  mineru_is_ocr: false,
  mineru_enable_formula: true,
  mineru_enable_table: true,
  mineru_page_ranges: '',
  parse_vision_enabled: true,
  parse_vision_dpi: 200,
  parse_figure_caption: false,
  parse_deep_figure_search: false,
  vision_dpi_min: 72,
  vision_dpi_max: 600,
});
const prefsBase = ref<ParseConfig>({ ...prefs });
const prefsLoading = ref(false);
const prefsError = ref('');

function fillPrefs(cfg: ParseConfig | null | undefined): void {
  const next: ParseConfig = {
    mineru_is_ocr: cfg?.mineru_is_ocr === true,
    mineru_enable_formula: cfg?.mineru_enable_formula !== false,
    mineru_enable_table: cfg?.mineru_enable_table !== false,
    mineru_page_ranges: String(cfg?.mineru_page_ranges ?? ''),
    parse_vision_enabled: cfg?.parse_vision_enabled !== false,
    parse_vision_dpi: Number(cfg?.parse_vision_dpi ?? 200),
    parse_figure_caption: cfg?.parse_figure_caption === true,
    parse_deep_figure_search: cfg?.parse_deep_figure_search === true,
    vision_dpi_min: Number(cfg?.vision_dpi_min ?? 72),
    vision_dpi_max: Number(cfg?.vision_dpi_max ?? 600),
  };
  Object.assign(prefs, next);
  prefsBase.value = { ...next };
}

async function loadPrefs(): Promise<void> {
  prefsLoading.value = true;
  prefsError.value = '';
  try {
    fillPrefs(await api.get<ParseConfig>('/parse/config'));
  } catch (err) {
    prefsError.value = '解析默认值读取失败：' + ((err as Error)?.message ?? String(err));
  } finally {
    prefsLoading.value = false;
  }
}

const PREF_KEYS = [
  'mineru_is_ocr', 'mineru_enable_formula', 'mineru_enable_table', 'mineru_page_ranges',
  'parse_vision_enabled', 'parse_vision_dpi', 'parse_figure_caption', 'parse_deep_figure_search',
] as const;

/** 只把改过的键交上去（后端每个键都是可选的，多写等于替用户重新拍板）。 */
function prefsPatch(): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const key of PREF_KEYS) {
    if (prefs[key] !== prefsBase.value[key]) body[key] = prefs[key];
  }
  return body;
}

const LOCAL_KEYS = [
  'parse_vision_enabled', 'parse_vision_dpi', 'parse_figure_caption', 'parse_deep_figure_search',
] as const;
const MINERU_PREF_KEYS = [
  'mineru_is_ocr', 'mineru_enable_formula', 'mineru_enable_table', 'mineru_page_ranges',
] as const;

function anyDirty(keys: readonly string[]): boolean {
  const body = prefsPatch();
  return keys.some((k) => k in body);
}

const localDirty = computed(() => anyDirty(LOCAL_KEYS));
const mineruPrefsDirty = computed(() => anyDirty(MINERU_PREF_KEYS));

async function savePrefs(): Promise<void> {
  const body = prefsPatch();
  if (!Object.keys(body).length) return;
  fillPrefs(await api.put<ParseConfig>('/parse/config', body));
}

/* ---------------- MinerU 凭证与引擎（/mineru/config） ---------------- */

interface MineruForm {
  token: string;
  engine_default: ParseEngine;
  model_version: string;
  language: string;
}

const mineruForm = reactive<MineruForm>({
  token: '', engine_default: 'local', model_version: 'pipeline', language: 'en',
});
const mineruBase = reactive<MineruConfig>({
  has_token: false, token_masked: null, engine_default: 'local',
  model_version: 'pipeline', language: 'en',
});
const mineruBusy = ref('');
const mineruError = ref('');
const mineruVerify = ref<MineruVerifyResult | null>(null);
const mineruLoading = ref(false);

const ENGINE_OPTIONS = [
  { value: 'local', label: '本地解析（离线、快、不花额度）' },
  { value: 'mineru', label: '在线解析（MinerU 云端，公式/表格更强）' },
];

const mineruTokenText = computed(
  () => mineruBase.token_masked || (mineruBase.has_token ? '已保存（掩码不可用）' : ''),
);
/** 模型版本与 OCR 语言都用**后端给的列表**：官方加语言/模型时前端不用跟着改。 */
const modelOptions = computed(() => (mineruBase.model_version_options?.length
  ? mineruBase.model_version_options
  : [{ value: 'pipeline', label: 'pipeline（默认）' }, { value: 'vlm', label: 'vlm' }]));
const langOptions = computed(() => (mineruBase.languages?.length
  ? mineruBase.languages
  : [{ value: 'en', label: 'en（英文）' }, { value: 'ch', label: 'ch（中文）' }]));

const mineruDirty = computed(() => !!mineruForm.token
  || mineruForm.engine_default !== mineruBase.engine_default
  || mineruForm.model_version !== mineruBase.model_version
  || mineruForm.language !== mineruBase.language);

function fillMineru(cfg: MineruConfig): void {
  mineruBase.has_token = cfg?.has_token === true;
  mineruBase.token_masked = cfg?.token_masked ?? null;
  mineruBase.token_source = cfg?.token_source ?? null;
  mineruBase.engine_default = cfg?.engine_default === 'mineru' ? 'mineru' : 'local';
  mineruBase.model_version = (cfg?.model_version ?? 'pipeline') as MineruConfig['model_version'];
  mineruBase.language = cfg?.language ? String(cfg.language) : 'en';
  mineruBase.base_url = cfg?.base_url ?? '';
  mineruBase.token_file = cfg?.token_file ?? null;
  mineruBase.online_ready = cfg?.online_ready === true;
  mineruBase.model_version_options = cfg?.model_version_options ?? mineruBase.model_version_options;
  mineruBase.languages = cfg?.languages ?? mineruBase.languages;
  mineruForm.token = '';
  mineruForm.engine_default = mineruBase.engine_default;
  mineruForm.model_version = String(mineruBase.model_version);
  mineruForm.language = mineruBase.language;
}

async function loadMineru(): Promise<void> {
  mineruError.value = '';
  mineruLoading.value = true;
  try {
    fillMineru(await api.get<MineruConfig>('/mineru/config'));
  } catch (err) {
    mineruError.value = 'MinerU 配置读取失败：' + ((err as Error)?.message ?? String(err));
  } finally {
    mineruLoading.value = false;
  }
}

function metaMessage(res: any): string {
  return String(res?.__meta?.message ?? '');
}

async function saveMineru(): Promise<void> {
  if (!mineruDirty.value) { toast('MinerU 没有需要保存的改动', 'warn'); return; }
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
    mineruVerify.value = null;
    toast(metaMessage(res) || 'MinerU 配置已保存', 'ok', 3600);
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 6000);
  } finally {
    mineruBusy.value = '';
  }
}

async function verifyMineru(): Promise<void> {
  mineruBusy.value = 'verify';
  mineruVerify.value = null;
  try {
    const res = await api.post<MineruVerifyResult>('/mineru/verify', {});
    mineruVerify.value = res;
    toast(res?.message || (res?.ok ? 'Token 有效' : 'Token 验证未通过'),
      res?.ok ? 'ok' : 'error', res?.ok ? 3600 : 7000);
    await loadMineru();
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 6000);
    mineruVerify.value = { ok: false, state: 'network', message: (err as Error)?.message };
  } finally {
    mineruBusy.value = '';
  }
}

async function clearMineruToken(): Promise<void> {
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

/* ---------------- 网联搜索（转发给共用组件） ---------------- */

const ws = ref<InstanceType<typeof WebSearchSettings> | null>(null);

/* ---------------- 页头状态与底部动作 ---------------- */

const statusBadge = computed<{ text: string; cls: string; title: string }>(() => {
  if (service.value === 'local') {
    if (prefsLoading.value) return { text: '读取中…', cls: 'dim', title: '' };
    if (prefsError.value) return { text: '读取失败', cls: 'warn', title: prefsError.value };
    if (localDirty.value) return { text: '有未保存的改动', cls: 'warn', title: '点底部「保存设置」生效' };
    return {
      text: prefs.parse_vision_enabled ? '离线解析 + 视觉读页' : '离线解析（不读扫描页）',
      cls: 'ok',
      title: '本地 PyMuPDF 解析，全程不走网络',
    };
  }
  if (service.value === 'mineru') {
    if (mineruLoading.value) return { text: '读取中…', cls: 'dim', title: '' };
    if (mineruDirty.value || mineruPrefsDirty.value) {
      return { text: '有未保存的改动', cls: 'warn', title: '改了参数或填了新 token，点底部「保存设置」生效' };
    }
    if (mineruBase.online_ready) return { text: '配置已就绪', cls: 'ok', title: '有可用 token' };
    if (mineruBase.has_token) return { text: '已存 token（未验证）', cls: 'dim', title: '还没验证或最近一次没通过' };
    return { text: '未配置 token', cls: 'dim', title: '在线解析需要 mineru.net 的 token' };
  }
  const c = ws.value;
  if (!c) return { text: '读取中…', cls: 'dim', title: '' };
  if (!c.ready) return { text: '接口未就绪', cls: 'warn', title: '后端没有返回联网搜索配置' };
  if (c.dirty) return { text: '有未保存的改动', cls: 'warn', title: '点底部「保存设置」生效' };
  if (c.test?.ok) return { text: '验证通过', cls: 'ok', title: c.test.state + ' · ' + c.test.ms + 'ms' };
  if (c.hasKey) return { text: '已存 API Key（未验证）', cls: 'dim', title: '还没点过「测试服务」' };
  return { text: '未配置 API Key', cls: 'dim', title: '网联搜索需要提供方的 API Key' };
});

const busy = computed(() => {
  if (service.value === 'local') return prefsLoading.value;
  if (service.value === 'mineru') return !!mineruBusy.value;
  return !!(ws.value && (ws.value.busy || ws.value.testing));
});

const canSave = computed(() => {
  if (service.value === 'local') return localDirty.value && !prefsLoading.value;
  if (service.value === 'mineru') return (mineruDirty.value || mineruPrefsDirty.value) && !mineruBusy.value;
  return !!ws.value?.dirty && !ws.value?.busy;
});

const canVerify = computed(() => {
  if (service.value === 'local') return false;          // 本地解析没有远程凭据可验
  if (service.value === 'mineru') return !!mineruBase.has_token && !mineruBusy.value;
  return !!ws.value?.ready && !ws.value?.testing;
});

const canClear = computed(() => {
  if (service.value === 'local') return false;
  if (service.value === 'mineru') return !!mineruBase.has_token && !mineruBusy.value;
  return !!ws.value?.hasKey && !ws.value?.busy;
});

async function saveCurrent(): Promise<void> {
  try {
    if (service.value === 'local') {
      await savePrefs();
      toast('本地解析设置已保存', 'ok', 2600);
      return;
    }
    if (service.value === 'mineru') {
      // 一个按钮管两处：凭据/引擎走 /mineru/config，四项导入默认值走 /parse/config
      if (mineruDirty.value) await saveMineru();
      if (mineruPrefsDirty.value) await savePrefs();
      return;
    }
    await ws.value?.save();
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 5000);
  }
}

async function verifyCurrent(): Promise<void> {
  if (service.value === 'mineru') { await verifyMineru(); return; }
  if (service.value === 'websearch') await ws.value?.verify();
}

async function clearCurrent(): Promise<void> {
  if (service.value === 'mineru') { await clearMineruToken(); return; }
  if (service.value === 'websearch') await ws.value?.clearKey();
}

/** 底部那条「导入时会发生什么」的取值：改参数时不用来回切服务核对。 */
const engineLabel = computed(
  () => (mineruForm.engine_default === 'mineru' ? '在线解析（MinerU）' : '本地解析'),
);
const engineIsMineru = computed(() => mineruForm.engine_default === 'mineru');

/* ---------------- 最近的解析任务（只读：上次到底是怎么解析的） ---------------- */

const recentParses = computed(() => state.tasks
  .filter((t) => String(t.type ?? '') === 'PARSE')
  .slice(0, 5));

/** 任务参数里的引擎：没写 = 当时用的是默认引擎。 */
function taskEngine(t: { params?: Record<string, any> }): string {
  const e = String(t.params?.engine ?? '');
  if (e === 'mineru') return '在线解析';
  if (e === 'local') return '本地解析';
  return '默认引擎';
}

onMounted(() => {
  void loadPrefs();
  void loadMineru();
  void loadTasks().catch(() => { /* 任务列表拿不到不影响配置本身 */ });
});
</script>

<template>
  <div class="ons">
    <!-- 页头：说明 + 状态（标题由窗格抬头给，不重复） -->
    <header class="ons-head">
      <p class="ons-sub">本地解析、MinerU 在线解析与网联搜索的凭据与参数</p>
      <div class="ons-actions">
        <span class="badge" :class="statusBadge.cls" :title="statusBadge.title">{{ statusBadge.text }}</span>
      </div>
    </header>

    <!-- 服务切换：三块等宽，选中 = 强调色描边 + 对勾（不只用颜色表达） -->
    <div class="ons-switch" role="tablist" aria-label="在线服务">
      <button
        v-for="s in services"
        :key="s.id"
        type="button"
        role="tab"
        class="ons-tile"
        :class="{ on: service === s.id }"
        :aria-selected="service === s.id"
        @click="service = s.id"
      >
        <span class="ons-tile-icon"><AppIcon :name="s.icon" :size="17" /></span>
        <span class="ons-tile-text">
          <span class="ons-tile-label">{{ s.label }}</span>
          <span class="ons-tile-sub">{{ s.sub }}</span>
        </span>
        <AppIcon v-if="service === s.id" class="ons-tile-check" name="check_circle" :size="16" />
      </button>
    </div>

    <!-- ① 本地解析：全离线，参数只影响"怎么读 PDF" -->
    <section class="ons-panel" v-if="service === 'local'">
      <p class="ons-error" v-if="prefsError">{{ prefsError }}</p>
      <div class="ons-cols">
        <div class="ons-sec">
          <h3 class="ons-sec-title"><AppIcon name="document" :size="14" />扫描页与读页</h3>
          <div class="ons-sec-body">
            <div class="ons-field">
              <span class="field-top">视觉读页</span>
              <Switch v-model="prefs.parse_vision_enabled">没有文本层的页交给视觉模型</Switch>
              <span class="field-hint">关掉后扫描页只留页图，正文为空</span>
            </div>
            <label class="field">
              <span class="field-top">读页 dpi</span>
              <input
                type="number"
                :min="prefs.vision_dpi_min"
                :max="prefs.vision_dpi_max"
                step="10"
                v-model.number="prefs.parse_vision_dpi"
              />
              <span class="field-hint">
                取读页渲染分辨率（{{ prefs.vision_dpi_min }}–{{ prefs.vision_dpi_max }}）：越高上下标越准，也越慢
              </span>
            </label>
          </div>
        </div>

        <div class="ons-sec">
          <h3 class="ons-sec-title"><AppIcon name="layers" :size="14" />插图</h3>
          <div class="ons-sec-body">
            <div class="ons-field">
              <span class="field-top">插图带图注</span>
              <Switch v-model="prefs.parse_figure_caption">裁出的插图带上它的图注</Switch>
              <span class="field-hint">导入时仍可逐篇改；开 = 现成的「图 + 图注」卡片</span>
            </div>
            <div class="ons-field">
              <span class="field-top">深度找图</span>
              <Switch v-model="prefs.parse_deep_figure_search">整页没有图注时按图块再找一遍</Switch>
              <span class="field-hint">更全，但会多花一些时间</span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ② 在线解析（MinerU） -->
    <section class="ons-panel" v-else-if="service === 'mineru'">
      <div class="ons-cols">
        <div class="ons-sec">
          <h3 class="ons-sec-title"><AppIcon name="key" :size="14" />凭证</h3>
          <div class="ons-sec-body">
            <label class="field">
              <span class="field-top">API token</span>
              <input
                type="password"
                autocomplete="off"
                spellcheck="false"
                v-model="mineruForm.token"
                :placeholder="mineruBase.has_token
                  ? '已保存 ' + mineruTokenText + '（留空 = 不修改）'
                  : '粘贴 mineru.net 申请到的 token'"
              />
              <span class="field-hint" v-if="mineruBase.token_file">
                token 文件 <span class="mono">{{ mineruBase.token_file }}</span>
              </span>
            </label>
            <div class="ons-row">
              <span class="badge" :class="mineruVerify ? (mineruVerify.ok ? 'ok' : 'danger') : 'dim'">
                <AppIcon :name="mineruVerify ? (mineruVerify.ok ? 'check_circle' : 'error') : 'info'" :size="13" />
                {{ mineruVerify ? (mineruVerify.ok ? 'Token 有效' : 'Token 不可用') : (mineruBase.has_token ? '尚未验证' : '未配置') }}
              </span>
              <span class="tiny faint" v-if="mineruVerify?.latency_ms !== null && mineruVerify?.latency_ms !== undefined">
                延迟 {{ mineruVerify.latency_ms }} ms
              </span>
            </div>
            <p class="tiny verify-msg" :class="mineruVerify?.ok ? 'ok' : 'bad'" v-if="mineruVerify?.message">
              {{ mineruVerify.message }}
            </p>
            <p class="ons-error" v-if="mineruError">{{ mineruError }}</p>
          </div>
        </div>

        <div class="ons-sec">
          <h3 class="ons-sec-title"><AppIcon name="sliders" :size="14" />解析参数</h3>
          <div class="ons-sec-body">
            <div class="ons-grid">
              <label class="field">
                <span class="field-top">模型版本</span>
                <select v-model="mineruForm.model_version">
                  <option v-for="m in modelOptions" :key="m.value" :value="m.value">{{ m.label }}</option>
                </select>
              </label>
              <label class="field">
                <span class="field-top">OCR 语言</span>
                <select v-model="mineruForm.language">
                  <option v-for="l in langOptions" :key="l.value" :value="l.value">{{ l.label }}</option>
                </select>
              </label>
            </div>
            <label class="field">
              <span class="field-top">默认解析引擎</span>
              <select v-model="mineruForm.engine_default">
                <option v-for="e in ENGINE_OPTIONS" :key="e.value" :value="e.value">{{ e.label }}</option>
              </select>
              <span class="field-hint">导入弹窗的初值，导入时仍可逐篇改</span>
            </label>
          </div>
        </div>
      </div>

      <!-- 与本地解析同属"导入默认值"：一个按钮保存时会同时写 /parse/config -->
      <div class="ons-sec">
        <h3 class="ons-sec-title"><AppIcon name="document" :size="14" />在线解析的导入默认值</h3>
        <div class="ons-sec-body">
          <div class="ons-switch-row">
            <div class="ons-field">
              <span class="field-top">强制 OCR</span>
              <Switch v-model="prefs.mineru_is_ocr">忽略文本层，整页走 OCR</Switch>
            </div>
            <div class="ons-field">
              <span class="field-top">识别公式</span>
              <Switch v-model="prefs.mineru_enable_formula">把公式单独解出来</Switch>
            </div>
            <div class="ons-field">
              <span class="field-top">识别表格</span>
              <Switch v-model="prefs.mineru_enable_table">把表格解成结构</Switch>
            </div>
          </div>
          <label class="field">
            <span class="field-top">页码范围</span>
            <input type="text" v-model="prefs.mineru_page_ranges" placeholder="留空 = 全篇；例如 1-3,5,8-" />
            <span class="field-hint">官方写法：1-3,5,8-；超长论文分段解析更稳</span>
          </label>
        </div>
      </div>
    </section>

    <!-- ③ 网联搜索（DeepSeek）：表单与读写都在共用组件里 -->
    <section class="ons-panel" v-else>
      <div class="ons-sec">
        <h3 class="ons-sec-title"><AppIcon name="search" :size="14" />搜索服务配置</h3>
        <div class="ons-sec-body">
          <WebSearchSettings ref="ws" :actions="false" />
        </div>
      </div>
    </section>

    <!-- 最近的解析任务：只说"上次怎么解析的"，改参数时能对上号 -->
    <section class="ons-recent" v-if="recentParses.length">
      <h3 class="ons-sec-title"><AppIcon name="clock" :size="14" />最近的解析</h3>
      <ul class="ons-recent-list">
        <li class="ons-recent-row" v-for="t in recentParses" :key="t.id">
          <span class="ons-recent-name truncate" :title="t.paper_id || t.id">
            {{ t.paper_id ? paperTitle(t.paper_id) : t.id }}
          </span>
          <span class="ons-recent-engine">{{ taskEngine(t) }}</span>
          <span class="badge" :class="taskStatus(t.status).cls">{{ taskStatus(t.status).label }}</span>
          <span class="ons-recent-time">{{ fmtRelative(t.updated_at || t.created_at) }}</span>
        </li>
      </ul>
    </section>

    <!-- 导入时会按这套取值（只读摘要）：改参数时不用来回切服务核对 -->
    <section class="ons-summary-bar" aria-label="导入时的默认取值">
      <span class="ons-sum"><b>默认引擎</b>{{ engineLabel }}</span>
      <span class="ons-sum"><b>读页</b>{{ prefs.parse_vision_enabled ? "视觉模型" : "不读正文" }} · {{ prefs.parse_vision_dpi }} dpi</span>
      <span class="ons-sum"><b>插图</b>{{ prefs.parse_figure_caption ? "带图注" : "不带图注" }} · {{ prefs.parse_deep_figure_search ? "深度找图" : "常规找图" }}</span>
      <span class="ons-sum" v-if="engineIsMineru"><b>MinerU</b>{{ mineruForm.model_version }} · {{ mineruForm.language }} · {{ prefs.mineru_is_ocr ? "强制 OCR" : "按文本层" }} · 公式{{ prefs.mineru_enable_formula ? "开" : "关" }} · 表格{{ prefs.mineru_enable_table ? "开" : "关" }} · {{ prefs.mineru_page_ranges || "全篇" }}</span>
    </section>

    <!-- 动作只有这一处：右对齐贴在页底，换服务时三个按钮位置不动 -->
    <footer class="ons-foot">
      <span class="ons-foot-hint">{{ isOnline ? '凭据只存在服务端，接口只回掩码' : '本地解析全离线，不产生任何请求' }}</span>
      <button class="btn sm" :disabled="!canVerify" title="真的打一次服务接口，确认凭据还能用"
              @click="verifyCurrent">
        <AppIcon name="play" :size="15" />{{ busy ? '处理中…' : '测试服务' }}
      </button>
      <button class="btn sm" :disabled="!canClear" @click="clearCurrent">
        <AppIcon name="ban" :size="15" />清除密钥
      </button>
      <button class="btn primary sm" :disabled="!canSave" @click="saveCurrent">
        <AppIcon name="check" :size="15" />{{ busy ? '处理中…' : '保存设置' }}
      </button>
    </footer>
  </div>
</template>

<style scoped>
.ons { display: flex; flex-direction: column; gap: 16px; }

.ons-head { display: flex; align-items: flex-start; gap: 16px; }
.ons-sub {
  flex: 1 1 auto; min-width: 0; margin: 0;
  font-size: calc(12px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint);
}
.ons-actions { display: flex; align-items: center; gap: 8px; flex: none; }

/* 服务切换：三块等宽（窄屏折成两列 / 一列），选中 = 一档更亮的底 + 强调色描边 + 对勾 */
.ons-switch { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.ons-tile {
  display: flex; align-items: center; gap: 10px; min-width: 0;
  padding: 10px 12px; text-align: left;
  border: 1px solid var(--border); border-radius: var(--r-md); background: var(--bg-elev);
  color: var(--text-dim); cursor: pointer;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.ons-tile:hover { border-color: var(--border-strong); background: var(--bg-hover); }
.ons-tile.on {
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
  background: var(--bg-elev);
}
.ons-tile-icon {
  display: grid; place-items: center; flex: none;
  width: 28px; height: 28px; border-radius: var(--r-sm);
  background: var(--bg-sunken); color: var(--text-faint);
}
.ons-tile.on .ons-tile-icon { color: var(--accent); }
.ons-tile-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
.ons-tile-label { font-size: calc(13px * var(--ts, 1)); font-weight: 650; color: var(--text); }
.ons-tile-sub {
  font-size: calc(11px * var(--ts, 1)); color: var(--text-faint);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ons-tile-check { flex: none; color: var(--accent); }

/* 面板：宽屏两栏并排（凭证 | 参数），窄屏一栏 —— 空间利用率靠这个，不靠多套卡片 */
.ons-panel { display: flex; flex-direction: column; gap: 16px; }
.ons-cols { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: start; }
.ons-sec { min-width: 0; display: flex; flex-direction: column; gap: 8px; }
.ons-sec-title {
  display: flex; align-items: center; gap: 6px; margin: 0;
  font-size: calc(12.5px * var(--ts, 1)); font-weight: 650; line-height: 1.35; color: var(--text);
}
.ons-sec-title :deep(svg) { color: var(--text-faint); }
.ons-sec-body {
  display: flex; flex-direction: column; gap: 12px;
  padding: 12px 14px; border-radius: var(--r-sm); background: var(--bg-sunken);
}
/* 全局 .field > input 带 340px 上限（窄表单的尺子）：这里要整行 */
.ons-sec-body .field > input,
.ons-sec-body .field > select { box-sizing: border-box; width: 100%; max-width: none; }
.ons-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
/* 三个开关并排：每个都是「标题 + 开关」，不给它们各自套卡片 */
.ons-switch-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.ons-field { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.ons-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.field-hint { font-size: calc(11px * var(--ts, 1)); line-height: 1.45; color: var(--text-faint); }
.verify-msg { margin: 0; overflow-wrap: anywhere; }
.verify-msg.ok { color: var(--ok); }
.verify-msg.bad { color: var(--danger); }
.ons-error { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; color: var(--danger); }

/* 最近的解析：一行一条，发丝线分隔（与系统信息里的服务列表同一套密度） */
.ons-recent { display: flex; flex-direction: column; gap: 8px; }
.ons-recent-list { margin: 0; padding: 0; list-style: none; }
.ons-recent-row {
  display: flex; align-items: center; gap: 10px; min-height: 30px; min-width: 0;
  font-size: calc(12px * var(--ts, 1)); color: var(--text-dim);
}
.ons-recent-row + .ons-recent-row { border-top: 1px solid var(--divider); }
.ons-recent-name { flex: 1 1 auto; min-width: 0; color: var(--text); }
.ons-recent-engine { flex: none; color: var(--text-faint); }
.ons-recent-time { flex: none; color: var(--text-faint); font-variant-numeric: tabular-nums; }

/* 只读摘要条：一行 key→value，窄屏自动换行；它不是表单，所以不铺字段底 */
.ons-summary-bar {
  display: flex; align-items: baseline; gap: 6px 18px; flex-wrap: wrap;
  padding: 10px 14px; border-radius: var(--r-sm); background: var(--bg-sunken);
}
.ons-sum {
  min-width: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim);
  overflow-wrap: anywhere;
}
.ons-sum b {
  margin-right: 6px; font-weight: 600; color: var(--text-faint);
}

/* 底部动作排：一条发丝线与内容分开，按钮贴右缘 */
.ons-foot {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end;
  padding-top: 12px; border-top: 1px solid var(--divider);
}
.ons-foot-hint {
  flex: 1 1 auto; min-width: 0; margin-right: 4px;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
}

@media (max-width: 1000px) {
  .ons-cols { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 900px) {
  .ons-head { flex-direction: column; gap: 8px; }
  .ons-actions { justify-content: flex-start; }
  .ons-switch { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 560px) {
  .ons-switch, .ons-grid, .ons-switch-row { grid-template-columns: minmax(0, 1fr); }
}
</style>
