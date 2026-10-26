<script setup lang="ts">
/**
 * 设置页「在线服务」：一页管两件事 —— **解析**（本地 / MinerU）与**搜索**（DeepSeek）。
 *
 * 版式口径（用户 2026-12 第三次）：**一页到底**，页内不再有"解析 / 搜索"切换胶囊 ——
 * 两件事本来就是同一页的两半，切来切去反而看不到全貌。
 *   · 自上而下：默认引擎（一枚轻量分段）→ 两栏（左本地 / 右 MinerU）→ 网联搜索 → 最近的解析；
 *   · 每个分组都与设置页其它面板同一套：**标题在块外 + 淡色圆角内容底**；
 *   · MinerU 的低频项（模型版本 / OCR 语言 / 强制 OCR / 页码范围）折进「高级选项」，默认值不变；
 *   · 动作只有页脚这一条：保存状态 + 清除 token / 清除密钥（低强调、危险色）+ 测试服务 + 保存设置。
 *
 * 数据面：引擎与 MinerU 凭据在 `/mineru/config`；两个引擎的导入默认值在 `/parse/config`
 * （一个键一套，见 `parse_prefs`）；搜索在 `WebSearchSettings` 里读写同一份配置。
 * 本页只改版式与文案，接口、键名、默认值一个都没动。
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

/** 默认引擎：一枚轻量分段（用户口径：状态标签式，不另外造两枚说明卡）。 */
const ENGINE_OPTIONS: ReadonlyArray<{ value: ParseEngine; label: string }> = [
  { value: 'local', label: '本地解析' },
  { value: 'mineru', label: '在线解析' },
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

/* ---------------- 保存状态与底部动作（解析 + 搜索共用一条） ---------------- */

const parseDirty = computed(() => mineruDirty.value || localDirty.value || mineruPrefsDirty.value);

/** 整页的保存状态（页脚左侧那一枚）：已保存 / 有未保存的改动 / 读取失败。
 *  两个服务的**验证**结论各自挂在自己的卡头上（MinerU token、网联搜索密钥），页脚不重复。 */
const saveState = computed<{ text: string; cls: string; title: string }>(() => {
  if (mineruLoading.value || prefsLoading.value) return { text: '读取中…', cls: 'dim', title: '' };
  if (prefsError.value || mineruError.value) {
    return { text: '读取失败', cls: 'warn', title: prefsError.value || mineruError.value };
  }
  if (parseDirty.value || ws.value?.dirty) {
    return { text: '有未保存的改动', cls: 'warn', title: '点右下「保存设置」生效' };
  }
  return { text: '已保存', cls: 'ok', title: '与服务端一致' };
});

/** 网联搜索的凭据状态（挂在它自己那张卡的标题右端） */
const wsBadge = computed<{ text: string; cls: string; title: string }>(() => {
  const c = ws.value;
  if (!c) return { text: '读取中…', cls: 'dim', title: '' };
  if (!c.ready) return { text: '接口未就绪', cls: 'warn', title: '后端没有返回联网搜索配置' };
  if (c.test) {
    return c.test.ok
      ? { text: '验证通过', cls: 'ok', title: c.test.state + ' · ' + c.test.ms + 'ms' }
      : { text: '验证失败', cls: 'danger', title: c.test.detail || c.test.state };
  }
  return c.hasKey
    ? { text: '已存 API Key', cls: 'dim', title: '还没点过「测试服务」' }
    : { text: '未配置 API Key', cls: 'dim', title: '' };
});

const busy = computed(() => !!mineruBusy.value || prefsLoading.value
  || !!(ws.value && (ws.value.busy || ws.value.testing)));

const canSave = computed(() => (parseDirty.value || !!ws.value?.dirty) && !busy.value);

/** 测试服务：把**已经配了凭据**的服务都打一次（没配的跳过，不拿"缺 key"当失败报红） */
const canVerify = computed(() => (!!mineruBase.has_token || !!ws.value?.hasKey) && !busy.value);

const canClearToken = computed(() => !!mineruBase.has_token && !mineruBusy.value);
const canClearKey = computed(() => !!ws.value?.hasKey && !ws.value?.busy);

async function clearKey(): Promise<void> {
  await ws.value?.clearKey();
}

/** 保存：三组各自只在**改过**的时候发请求（未改动不发，是这一页的铁律）。 */
async function saveAll(): Promise<void> {
  try {
    if (mineruDirty.value) await saveMineru();
    if (mineruPrefsDirty.value || localDirty.value) await savePrefs();
    if (ws.value?.dirty) await ws.value.save();
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 5000);
  }
}

async function verifyAll(): Promise<void> {
  if (!mineruBase.has_token && !ws.value?.hasKey) {
    toast('先填一个 MinerU token 或联网搜索密钥，再来测试', 'warn');
    return;
  }
  if (mineruBase.has_token) await verifyMineru();
  if (ws.value?.hasKey) await ws.value.verify();
}

onMounted(() => {
  void loadPrefs();
  void loadMineru();
  void loadTasks().catch(() => { /* 任务列表拿不到不影响配置本身 */ });
});
</script>

<template>
  <div class="ons">
    <!-- 页内**没有服务切换**（用户口径 2026-12）：解析（本地 / MinerU）与网联搜索在同一页，
         从上往下读一遍就配完了；保存状态与动作只有页脚那一条。 -->
    <section class="ons-panel">
      <p class="ons-error" v-if="prefsError || mineruError">{{ prefsError || mineruError }}</p>

      <!-- 默认引擎：一枚轻量分段，导入弹窗与「重新解析」的初值都读它 -->
      <div class="ons-line">
        <span class="ons-line-label">默认引擎</span>
        <div class="seg ons-engine-seg" role="radiogroup" aria-label="默认解析引擎">
          <button
            v-for="opt in ENGINE_OPTIONS"
            :key="opt.value"
            type="button"
            :class="{ active: mineruForm.engine_default === opt.value }"
            role="radio"
            :aria-checked="mineruForm.engine_default === opt.value"
            :data-ons-engine="opt.value"
            @click="mineruForm.engine_default = opt.value"
          >{{ opt.label }}</button>
        </div>
      </div>

      <!-- 两栏：左本地、右在线（窄了自然落成一列） -->
      <div class="ons-cols">
        <div class="ons-group">
          <h3 class="ons-h"><AppIcon name="document" :size="14" />本地解析</h3>
          <div class="ons-fields">
            <Switch v-model="prefs.parse_vision_enabled" label="视觉读页">视觉读页</Switch>
            <label class="field">
              <span class="field-top">
                读页 dpi
                <span class="ons-unit">{{ prefs.vision_dpi_min }}–{{ prefs.vision_dpi_max }}</span>
              </span>
              <input
                type="number"
                :min="prefs.vision_dpi_min"
                :max="prefs.vision_dpi_max"
                step="10"
                v-model.number="prefs.parse_vision_dpi"
              />
            </label>
            <Switch v-model="prefs.parse_figure_caption" label="图片包含图注">图片包含图注</Switch>
            <Switch v-model="prefs.parse_deep_figure_search" label="深度图片搜索">深度图片搜索</Switch>
          </div>
        </div>

        <div class="ons-group">
          <h3 class="ons-h">
            <AppIcon name="key" :size="14" />在线解析（MinerU）
            <!-- 验证状态贴在标题右端（与系统信息页的块头同一手法）：只在点过「测试服务」时才有延迟 -->
            <span class="ons-h-right">
              <span class="ons-unit" v-if="mineruVerify?.latency_ms !== null && mineruVerify?.latency_ms !== undefined">
                {{ mineruVerify.latency_ms }} ms
              </span>
              <span class="badge" :class="mineruVerify ? (mineruVerify.ok ? 'ok' : 'danger') : 'dim'"
                    data-ons-verify>
                {{ mineruVerify ? (mineruVerify.ok ? 'Token 有效' : 'Token 不可用')
                  : (mineruBase.has_token ? '尚未验证' : '未配置') }}
              </span>
            </span>
          </h3>
          <div class="ons-fields">
            <label class="field">
              <span class="field-top">API Token</span>
              <input
                type="password"
                autocomplete="off"
                spellcheck="false"
                v-model="mineruForm.token"
                :placeholder="mineruBase.has_token
                  ? '已保存 ' + mineruTokenText + '（留空不改）'
                  : '粘贴 mineru.net 申请到的 token'"
              />
            </label>
            <!-- 验证失败要说清是哪一步坏的：真实错误原样显示，不用静态文案顶替 -->
            <p class="ons-verify-msg" :class="mineruVerify?.ok ? 'ok' : 'bad'" v-if="mineruVerify?.message">
              {{ mineruVerify.message }}
            </p>

            <Switch v-model="prefs.mineru_enable_formula" label="识别公式">识别公式</Switch>
            <Switch v-model="prefs.mineru_enable_table" label="识别表格">识别表格</Switch>

            <!-- 低频项折叠：默认值与开关状态都不变，只是默认不占版面 -->
            <details class="ons-adv">
              <summary>高级选项<AppIcon class="ons-adv-chev" name="chevron-down" :size="14" /></summary>
              <div class="ons-adv-body">
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
                <Switch v-model="prefs.mineru_is_ocr" label="强制 OCR">强制 OCR</Switch>
                <label class="field">
                  <span class="field-top">页码范围</span>
                  <input type="text" v-model="prefs.mineru_page_ranges" placeholder="留空 = 全篇，例如 1-3,5,8-" />
                </label>
              </div>
            </details>
          </div>
        </div>
      </div>

      <!-- ==================== ② 网联搜索（原「搜索」子页） ==================== -->
      <div class="ons-group">
        <h3 class="ons-h">
          <AppIcon name="search" :size="14" />网联搜索
          <span class="ons-h-right">
            <span class="badge" :class="wsBadge.cls" :title="wsBadge.title" data-ons-ws-state>
              {{ wsBadge.text }}
            </span>
          </span>
        </h3>
        <div class="ons-fields">
          <WebSearchSettings ref="ws" :actions="false" />
        </div>
      </div>

      <!-- 最近的解析：改参数时能对上"上次是怎么跑"的 -->
      <div class="ons-group" v-if="recentParses.length">
        <h3 class="ons-h"><AppIcon name="clock" :size="14" />最近的解析</h3>
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

    <!-- 全页唯一的动作区：保存状态 + 两枚「清除」（低强调、危险色）贴左，测试 / 保存贴右 -->
    <footer class="ons-foot">
      <span class="badge" :class="saveState.cls" :title="saveState.title" data-ons-state>
        {{ saveState.text }}
      </span>
      <button class="btn ghost sm danger" :disabled="!canClearToken" @click="clearMineruToken">
        <AppIcon name="ban" :size="15" />清除 token
      </button>
      <button class="btn ghost sm danger" :disabled="!canClearKey" @click="clearKey">
        <AppIcon name="ban" :size="15" />清除密钥
      </button>
      <span class="ons-foot-gap" />
      <button class="btn sm" :disabled="!canVerify" title="真的打一次服务接口，确认凭据还能用"
              @click="verifyAll">
        <AppIcon name="play" :size="15" />{{ busy ? '处理中…' : '测试服务' }}
      </button>
      <button class="btn primary sm" :disabled="!canSave" @click="saveAll">
        <AppIcon name="check" :size="15" />{{ busy ? '处理中…' : '保存设置' }}
      </button>
    </footer>
  </div>
</template>

<style scoped>
/* 一页只有一条竖排：分组 → 动作区（页内没有服务切换，解析与搜索都在这一页）。
   分组的样式与设置页其它面板完全一致：**标题在块外、内容装淡色圆角底**（--bg-sunken + --r-sm）。
   卡片内边距 / 卡片间距与「Cyli 偏好」同一套数，两页读起来是一个尺子。 */
.ons {
  --card-pad: 20px;
  --card-gap: 24px;
  display: flex; flex-direction: column; gap: var(--card-gap);
  container-type: inline-size;
}

.ons-panel { display: flex; flex-direction: column; gap: var(--card-gap); }
.ons-cols {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  /* stretch：两栏的内容底等高（起跳点本来就一样，下边缘也拉齐 —— 差个几像素最扎眼） */
  gap: var(--card-gap); align-items: stretch;
}
.ons-group { min-width: 0; display: flex; flex-direction: column; gap: 8px; }
/* 分组标题只是标题（不铺底）：字号与系统信息 / 索引管理的块头同一档 */
.ons-h {
  display: flex; align-items: center; gap: 7px; margin: 0;
  /* 标题行给一个下限：右栏标题里带徽章（22px），左栏只有文字 —— 没有这条下限，
     两栏的内容底会差 2px 起跳，一行两块就"不齐"了（系统信息页踩过同一个坑）。 */
  min-height: 24px;
  font-size: calc(14.5px * var(--ts, 1)); font-weight: 650; line-height: 1.35; color: var(--text);
}
.ons-h :deep(svg) { flex: none; color: var(--text-faint); }
.ons-h-right { margin-left: auto; display: inline-flex; align-items: center; gap: 8px; }
/* 内容底：淡色圆角块，只包字段、不包标题 */
.ons-fields {
  display: flex; flex-direction: column; gap: 10px; min-width: 0;
  flex: 1;                     /* 两栏等高：短的那张把高度撑到自己那栏的底 */
  padding: var(--card-pad); border-radius: var(--r-sm); background: var(--bg-sunken);
}
/* 全局 label.field > input 有 340px 上限（窄表单的尺子）：这里要整列宽 */
.ons-fields .field > input,
.ons-fields .field > select { box-sizing: border-box; width: 100%; max-width: none; }

/* 开关行：文字在左、轨道在右。整行仍是 <label>，点文字也能切 */
.ons-fields :deep(.switch-row) { align-items: center; }
.ons-fields :deep(.switch-row + .switch-row) { margin-top: 0; }
.ons-fields :deep(.switch-text) {
  order: -1; flex: 1 1 auto; min-width: 0;
  font-size: calc(12.5px * var(--ts, 1)); color: var(--text-dim);
}

/* 默认引擎：一枚轻量分段（不另设说明卡） */
.ons-line { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ons-line-label { font-size: calc(12.5px * var(--ts, 1)); color: var(--text-dim); }
.ons-engine-seg { flex: none; height: 30px; }
.ons-engine-seg button { height: 24px; padding: 0 12px; font-size: calc(12.5px * var(--ts, 1)); }
.ons-unit { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); font-variant-numeric: tabular-nums; }

/* 高级选项：一枚可点的细行，展开后仍是同一套字段节奏 */
.ons-adv { border-top: 1px solid var(--divider); padding-top: 8px; }
.ons-adv > summary {
  display: flex; align-items: center; gap: 6px;
  cursor: pointer; list-style: none;
  font-size: calc(12px * var(--ts, 1)); color: var(--text-dim);
}
.ons-adv > summary::-webkit-details-marker { display: none; }
/* 箭头走统一图标入口（原先 ::after 里的 ▾/▴ 是字符，字号一放大就和文字对不齐）。
   旋转挂在 [open] 上：details 的开合只有 CSS 看得见，不为此再引一个 ref。 */
.ons-adv > summary .ons-adv-chev { flex: none; color: var(--text-faint); transition: transform .16s ease; }
.ons-adv[open] > summary .ons-adv-chev { transform: rotate(180deg); }
.ons-adv > summary:hover { color: var(--text); }
.ons-adv-body { display: flex; flex-direction: column; gap: 10px; padding-top: 10px; }
/* 右栏只有半屏宽：模型版本的选项文字很长（pipeline（版面+公式更稳，默认）），
   两列会把 select 里的文字截成省略号 —— 高级选项里的字段一律单列 */
.ons-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; }

.ons-verify-msg { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; overflow-wrap: anywhere; }
.ons-verify-msg.ok { color: var(--ok); }
.ons-verify-msg.bad { color: var(--danger); }
.ons-error { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; color: var(--danger); }

/* 最近的解析：同样装进淡色圆角块，一行一条、发丝线分隔 */
.ons-recent-list {
  margin: 0; padding: 6px var(--card-pad); list-style: none;
  border-radius: var(--r-sm); background: var(--bg-sunken);
}
.ons-recent-row {
  display: flex; align-items: center; gap: 10px; min-height: 30px; min-width: 0;
  font-size: calc(12px * var(--ts, 1)); color: var(--text-dim);
}
.ons-recent-row + .ons-recent-row { border-top: 1px solid var(--divider); }
.ons-recent-name { flex: 1 1 auto; min-width: 0; color: var(--text); }
.ons-recent-engine { flex: none; color: var(--text-faint); }
.ons-recent-time { flex: none; color: var(--text-faint); font-variant-numeric: tabular-nums; }

/* 动作区：一条发丝线 + 按钮贴右；清除密钥是低强调的危险动作（幽灵底 + 危险色） */
.ons-foot {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding-top: 12px; border-top: 1px solid var(--divider);
}
.ons-foot-gap { flex: 1 1 auto; }
.ons-foot .btn.ghost.danger { color: var(--danger); }
.ons-foot .btn.ghost.danger:hover:not(:disabled) { background: var(--danger-soft); color: var(--danger); }

/* 两栏在中等宽度就挤了：容器（不是视口）说了算，窗格收窄时同样落成一列 */
@container (max-width: 720px) {
  .ons-cols { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 720px) {
  .ons-cols { grid-template-columns: minmax(0, 1fr); }
  .ons-grid { grid-template-columns: minmax(0, 1fr); }
}
@container (max-width: 560px) {
  .ons { --card-pad: 16px; --card-gap: 20px; }
}
</style>
