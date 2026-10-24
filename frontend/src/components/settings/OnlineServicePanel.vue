<script setup lang="ts">
/**
 * 设置页「在线服务」：一页管两件事 —— **解析**（本地 / MinerU）与**搜索**（DeepSeek）。
 *
 * 版式口径（用户 2026-12）：分为解析和搜索两个块；解析里本地与在线解析各自可配；
 * 默认引擎在同一个页面里统一设置。于是：
 * · 顶层只有两枚分段：解析 / 搜索（不再"一次只显示一个服务"那样切来切去）；
 * · 解析页**两栏并排**：左本地、右 MinerU，两边的参数同屏可见、同屏可改；
 * · 「默认引擎」只在解析页顶部一处（导入弹窗与重新解析的初值都读它）；
 * · 底部只有一排动作（测试服务 / 清除密钥 / 保存设置），跟着当前分段换目标。
 *
 * 数据面：引擎与 MinerU 凭据在 `/mineru/config`；两个引擎的导入默认值在 `/parse/config`
 * （一个键一套，见 `parse_prefs`）；搜索在 `WebSearchSettings` 里读写同一份配置。
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

type TabKey = 'parse' | 'search';
const tab = ref<TabKey>('parse');

/* ---------------- 解析默认值（两个引擎共用一套键：/parse/config） ---------------- */

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
  const patch = prefsPatch();
  return keys.some((k) => k in patch);
}

const localDirty = computed(() => anyDirty(LOCAL_KEYS));
const mineruPrefsDirty = computed(() => anyDirty(MINERU_PREF_KEYS));

async function savePrefs(): Promise<void> {
  const body = prefsPatch();
  if (!Object.keys(body).length) return;
  fillPrefs(await api.put<ParseConfig>('/parse/config', body));
}

/* ---------------- MinerU 凭据与引擎（/mineru/config） ---------------- */

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

const ENGINE_OPTIONS: ReadonlyArray<{ value: ParseEngine; label: string; note: string }> = [
  { value: 'local', label: '本地解析', note: '离线、快、不花额度；公式与复杂表格偏弱' },
  { value: 'mineru', label: '在线解析', note: 'MinerU 云端；公式 / 表格更强，要 token 且必须联网' },
];

const mineruTokenText = computed(
  () => mineruBase.token_masked || (mineruBase.has_token ? '已保存（掩码不可用）' : ''),
);
/** 模型版本与 OCR 语言都用后端给的列表：官方加语言/模型时前端不用跟着改。 */
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
    body: '清除后在线解析不可用，需要重新填入',
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

/* ---------------- 最近的解析（只读：上次是怎么解析的） ---------------- */

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

/* ---------------- 页头状态与底部动作 ---------------- */

const parseDirty = computed(() => mineruDirty.value || localDirty.value || mineruPrefsDirty.value);

const statusBadge = computed<{ text: string; cls: string; title: string }>(() => {
  if (tab.value === 'parse') {
    if (mineruLoading.value || prefsLoading.value) return { text: '读取中…', cls: 'dim', title: '' };
    if (prefsError.value || mineruError.value) {
      return { text: '读取失败', cls: 'warn', title: prefsError.value || mineruError.value };
    }
    if (parseDirty.value) return { text: '有未保存的改动', cls: 'warn', title: '点底部「保存设置」生效' };
    const engine = mineruBase.engine_default === 'mineru' ? '在线解析' : '本地解析';
    return {
      text: '默认引擎：' + engine,
      cls: mineruBase.engine_default === 'mineru'
        ? (mineruBase.online_ready ? 'ok' : 'warn')
        : 'ok',
      title: mineruBase.has_token ? '已保存 MinerU token' : '还没配置 MinerU token',
    };
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
  if (tab.value === 'parse') return !!mineruBusy.value || prefsLoading.value;
  return !!(ws.value && (ws.value.busy || ws.value.testing));
});

const canSave = computed(() => {
  if (tab.value === 'parse') return parseDirty.value && !busy.value;
  return !!ws.value?.dirty && !ws.value?.busy;
});

const canVerify = computed(() => {
  // 解析页的测试验的是 MinerU token（本地解析没有远程凭据，无事可做）
  if (tab.value === 'parse') return !!mineruBase.has_token && !mineruBusy.value;
  return !!ws.value?.ready && !ws.value?.testing;
});

const canClear = computed(() => {
  if (tab.value === 'parse') return !!mineruBase.has_token && !mineruBusy.value;
  return !!ws.value?.hasKey && !ws.value?.busy;
});

const clearLabel = computed(() => (tab.value === 'parse' ? '清除 token' : '清除密钥'));

async function saveCurrent(): Promise<void> {
  try {
    if (tab.value === 'parse') {
      if (mineruDirty.value) await saveMineru();
      if (mineruPrefsDirty.value || localDirty.value) await savePrefs();
      return;
    }
    await ws.value?.save();
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 5000);
  }
}

async function verifyCurrent(): Promise<void> {
  if (tab.value === 'parse') { await verifyMineru(); return; }
  await ws.value?.verify();
}

async function clearCurrent(): Promise<void> {
  if (tab.value === 'parse') { await clearMineruToken(); return; }
  await ws.value?.clearKey();
}

onMounted(() => {
  void loadPrefs();
  void loadMineru();
  void loadTasks().catch(() => { /* 任务列表拿不到不影响配置本身 */ });
});
</script>

<template>
  <div class="ons">
    <!-- 页头：说明 + 状态（标题由窗格抬头给） -->
    <header class="ons-head">
      <p class="ons-sub">解析（本地 / MinerU）与网联搜索的凭据、参数与默认值</p>
      <span class="badge" :class="statusBadge.cls" :title="statusBadge.title">{{ statusBadge.text }}</span>
    </header>

    <!-- 顶层只分两块：解析 / 搜索 -->
    <div class="seg ons-tabs" role="tablist" aria-label="在线服务">
      <button :class="{ active: tab === 'parse' }" role="tab" :aria-selected="tab === 'parse'"
              data-ons-tab="parse" @click="tab = 'parse'">
        <AppIcon name="document" :size="14" />解析
      </button>
      <button :class="{ active: tab === 'search' }" role="tab" :aria-selected="tab === 'search'"
              data-ons-tab="search" @click="tab = 'search'">
        <AppIcon name="search" :size="14" />搜索
      </button>
    </div>

    <!-- ==================== ① 解析 ==================== -->
    <section class="ons-panel" v-if="tab === 'parse'">
      <p class="ons-error" v-if="prefsError || mineruError">{{ prefsError || mineruError }}</p>

      <!-- 默认引擎：整页唯一一处（导入弹窗 / 重新解析的初值都读它） -->
      <div class="ons-sec">
        <h3 class="ons-sec-title"><AppIcon name="bolt" :size="14" />默认引擎</h3>
        <div class="ons-sec-body">
          <div class="ons-engines" role="radiogroup" aria-label="默认解析引擎">
            <button
              v-for="opt in ENGINE_OPTIONS"
              :key="opt.value"
              type="button"
              class="ons-engine"
              :class="{ on: mineruForm.engine_default === opt.value }"
              role="radio"
              :aria-checked="mineruForm.engine_default === opt.value"
              :data-ons-engine="opt.value"
              @click="mineruForm.engine_default = opt.value"
            >
              <span class="ons-engine-name">{{ opt.label }}</span>
              <span class="ons-engine-note">{{ opt.note }}</span>
            </button>
          </div>
          <p class="field-hint">
            导入弹窗与「重新解析」的初值；单篇仍可临时改用另一个引擎
            <template v-if="!mineruBase.has_token">（在线解析还没配 token）</template>
          </p>
        </div>
      </div>

      <!-- 两个引擎并排：本地 / 在线，同屏可见同屏可改 -->
      <div class="ons-cols">
        <div class="ons-sec">
          <h3 class="ons-sec-title"><AppIcon name="document" :size="14" />本地解析</h3>
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
              <span class="field-hint">取读页渲染分辨率（{{ prefs.vision_dpi_min }}–{{ prefs.vision_dpi_max }}）：越高上下标越准，也越慢</span>
            </label>
            <div class="ons-field">
              <span class="field-top">图片是否包含图注</span>
              <Switch v-model="prefs.parse_figure_caption">裁出的插图带上它的图注</Switch>
            </div>
            <div class="ons-field">
              <span class="field-top">深度图片搜索</span>
              <Switch v-model="prefs.parse_deep_figure_search">整页没有图注时按图块再找一遍</Switch>
            </div>
          </div>
        </div>

        <div class="ons-sec">
          <h3 class="ons-sec-title"><AppIcon name="key" :size="14" />在线解析（MinerU）</h3>
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
            <div class="ons-switch-row">
              <div class="ons-field">
                <span class="field-top">强制 OCR</span>
                <Switch v-model="prefs.mineru_is_ocr">整页走 OCR</Switch>
              </div>
              <div class="ons-field">
                <span class="field-top">识别公式</span>
                <Switch v-model="prefs.mineru_enable_formula">单独解出公式</Switch>
              </div>
              <div class="ons-field">
                <span class="field-top">识别表格</span>
                <Switch v-model="prefs.mineru_enable_table">解成结构化表格</Switch>
              </div>
            </div>
            <label class="field">
              <span class="field-top">页码范围</span>
              <input type="text" v-model="prefs.mineru_page_ranges" placeholder="留空 = 全篇；例如 1-3,5,8-" />
              <span class="field-hint">官方写法：1-3,5,8-；超长论文分段解析更稳</span>
            </label>
          </div>
        </div>
      </div>

      <!-- 最近的解析：改参数时能对上"上次是怎么跑的" -->
      <div class="ons-sec" v-if="recentParses.length">
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
      </div>
    </section>

    <!-- ==================== ② 搜索 ==================== -->
    <section class="ons-panel" v-else>
      <div class="ons-sec">
        <h3 class="ons-sec-title"><AppIcon name="search" :size="14" />网联搜索（DeepSeek）</h3>
        <div class="ons-sec-body">
          <WebSearchSettings ref="ws" :actions="false" />
        </div>
      </div>
      <p class="ons-hint">
        这份配置与「Cyli 偏好 → 联网搜索技能」共用同一份数据与同一个密钥文件，改一处两边都变。
      </p>
    </section>

    <footer class="ons-foot">
      <span class="ons-foot-hint">{{ tab === 'parse' ? '本地解析全离线；MinerU 凭据只存在服务端' : '密钥只存在服务端，接口只回掩码' }}</span>
      <button class="btn sm" :disabled="!canVerify" title="真的打一次服务接口，确认凭据还能用"
              @click="verifyCurrent">
        <AppIcon name="play" :size="15" />{{ busy ? '处理中…' : '测试服务' }}
      </button>
      <button class="btn sm" :disabled="!canClear" @click="clearCurrent">
        <AppIcon name="ban" :size="15" />{{ clearLabel }}
      </button>
      <button class="btn primary sm" :disabled="!canSave" @click="saveCurrent">
        <AppIcon name="check" :size="15" />{{ busy ? '处理中…' : '保存设置' }}
      </button>
    </footer>
  </div>
</template>

<style scoped>
.ons { display: flex; flex-direction: column; gap: 14px; }

.ons-head { display: flex; align-items: flex-start; gap: 16px; }
.ons-sub {
  flex: 1 1 auto; min-width: 0; margin: 0;
  font-size: calc(12px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint);
}

/* 顶层分段：解析 / 搜索（与全站的 .seg 同一套，只是不铺满整行） */
.ons-tabs { align-self: flex-start; height: var(--control-h); }
.ons-tabs button { gap: 6px; padding: 0 14px; }

.ons-panel { display: flex; flex-direction: column; gap: 14px; }
.ons-cols { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; align-items: start; }

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
/* 全局 .field > input 有 340px 上限（窄表单的尺子）：这里要整行 */
.ons-sec-body .field > input,
.ons-sec-body .field > select { box-sizing: border-box; width: 100%; max-width: none; }

/* 默认引擎：两枚卡片式单选（选中 = 强调色描边，不只靠颜色也有位置感） */
.ons-engines { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.ons-engine {
  display: flex; flex-direction: column; gap: 3px; min-width: 0; padding: 10px 12px;
  text-align: left; border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg-elev); cursor: pointer;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.ons-engine:hover { border-color: var(--border-strong); }
.ons-engine.on { border-color: color-mix(in srgb, var(--accent) 55%, transparent); }
.ons-engine-name { font-size: calc(13px * var(--ts, 1)); font-weight: 650; color: var(--text); }
.ons-engine-note { font-size: calc(11px * var(--ts, 1)); line-height: 1.45; color: var(--text-faint); }

.ons-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.ons-switch-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
/* 右栏只有半屏宽：三个开关横排会把文案挤断行，落成一列更好读 */
.ons-cols .ons-switch-row { grid-template-columns: minmax(0, 1fr); gap: 10px; }
.ons-field { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.ons-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.field-hint { font-size: calc(11px * var(--ts, 1)); line-height: 1.45; color: var(--text-faint); }
.verify-msg { margin: 0; overflow-wrap: anywhere; }
.verify-msg.ok { color: var(--ok); }
.verify-msg.bad { color: var(--danger); }
.ons-error { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; color: var(--danger); }
.ons-hint { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.6; color: var(--text-faint); }

/* 最近的解析：一行一条，发丝线分隔 */
.ons-recent-list { margin: 0; padding: 0; list-style: none; }
.ons-recent-row {
  display: flex; align-items: center; gap: 10px; min-height: 30px; min-width: 0;
  font-size: calc(12px * var(--ts, 1)); color: var(--text-dim);
}
.ons-recent-row + .ons-recent-row { border-top: 1px solid var(--divider); }
.ons-recent-name { flex: 1 1 auto; min-width: 0; color: var(--text); }
.ons-recent-engine { flex: none; color: var(--text-faint); }
.ons-recent-time { flex: none; color: var(--text-faint); font-variant-numeric: tabular-nums; }

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
}
@media (max-width: 560px) {
  .ons-engines, .ons-grid, .ons-switch-row { grid-template-columns: minmax(0, 1fr); }
}
</style>
