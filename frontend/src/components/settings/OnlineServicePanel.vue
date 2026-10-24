<script setup lang="ts">
/**
 * 设置页「在线服务」：两个**在线**依赖的服务共用一页 —— 在线解析（MinerU）与网联搜索（DeepSeek）。
 *
 * 为什么并到一页：两者都是"填一把密钥 + 选几个参数 + 验一下通不通"，只是凭据属于不同厂商，
 * 各自开一区只会让左栏多一个同质的入口。所以共用：页头、服务切换、表单样式、右上角动作区；
 * **配置与验证状态各存一份**（`mineru*` 与 WebSearchSettings 内部），互不覆盖。
 *
 * 网联搜索的字段与读写逻辑**不在这里**：那份组件（`WebSearchSettings`）同时被
 * 「Cyli 偏好 → web-search 技能行」使用，两边读写同一份配置与同一个密钥文件。
 * 本页只是把它的三个动作提到右上角（`actions=false` + ref 调用），避免出现第二套实现。
 *
 * 数据面：MinerU 走 `GET/PUT /mineru/config` 与 `POST /mineru/verify`；
 * 搜索走 `GET/PUT /agent/websearch/config` 与 `POST /agent/websearch/verify`。
 * 两个 verify 接口**失败时 HTTP 仍是 200**，成败一律看返回体的 `ok`。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { api } from '@/api/client';
import { toast } from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import type { MineruConfig, MineruVerifyResult, ParseEngine } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import WebSearchSettings from './WebSearchSettings.vue';

type ServiceKey = 'mineru' | 'websearch';

const services: ReadonlyArray<{ id: ServiceKey; label: string; sub: string; icon: string }> = [
  { id: 'mineru', label: '在线解析', sub: 'MinerU · PDF 内容提取', icon: 'document' },
  { id: 'websearch', label: '网联搜索', sub: 'DeepSeek · 联网检索', icon: 'search' },
];

const service = ref<ServiceKey>('mineru');

/* ============================ ① MinerU ============================ */

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

const mineruLoading = ref(false);
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
  mineruLoading.value = true;
  try {
    fillMineru(await api.get<MineruConfig>('/mineru/config'));
  } catch (err) {
    // 后端还没上 `/mineru/*` 时这里必然失败：给一句明确的话，而不是让区块空白
    mineruError.value = `MinerU 配置读取失败：${(err as Error)?.message ?? String(err)}`;
  } finally {
    mineruLoading.value = false;
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

/** 清除 token：不可逆（要重新申请/粘贴），二次确认。 */
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

/* ========================= ② 网联搜索（转发） ========================= */

const ws = ref<InstanceType<typeof WebSearchSettings> | null>(null);

/* ------------------------------ 页头状态 ------------------------------ */

/** 右上角那枚状态徽章：当前这一服的结果要看它自己的那份状态。 */
const statusBadge = computed<{ text: string; cls: string; title: string }>(() => {
  if (service.value === 'mineru') {
    if (mineruLoading.value) return { text: '读取中…', cls: 'dim', title: '' };
    if (mineruChanged.value) return { text: '有未保存的改动', cls: 'warn', title: '改了参数或填了新 token，点「保存设置」生效' };
    if (mineruBase.online_ready) return { text: '配置已就绪', cls: 'ok', title: '有可用 token，且默认引擎指向在线解析' };
    if (mineruBase.has_token) return { text: '已存 token（未验证）', cls: 'dim', title: '保存过 token，但还没验证或最近一次验证没通过' };
    return { text: '未配置 token', cls: 'dim', title: '在线解析需要 mineru.net 的 token' };
  }
  const c = ws.value;
  if (!c) return { text: '读取中…', cls: 'dim', title: '' };
  if (!c.ready) return { text: '接口未就绪', cls: 'warn', title: '后端没有返回联网搜索配置' };
  if (c.dirty) return { text: '有未保存的改动', cls: 'warn', title: '点「保存设置」生效' };
  if (c.test?.ok) return { text: '验证通过', cls: 'ok', title: `${c.test.state} · ${c.test.ms}ms` };
  if (c.hasKey) return { text: '已存密钥（未验证）', cls: 'dim', title: '保存过 API Key，还没点过「测试服务」' };
  return { text: '未配置 API Key', cls: 'dim', title: '网联搜索需要提供方的 API Key' };
});

const busy = computed(() => (service.value === 'mineru'
  ? !!mineruBusy.value
  : !!(ws.value && (ws.value.busy || ws.value.testing))));

async function saveCurrent(): Promise<void> {
  if (service.value === 'mineru') { await saveMineru(); return; }
  await ws.value?.save();
}

async function verifyCurrent(): Promise<void> {
  if (service.value === 'mineru') { await verifyMineru(); return; }
  await ws.value?.verify();
}

async function clearCurrent(): Promise<void> {
  if (service.value === 'mineru') { await clearMineruToken(); return; }
  await ws.value?.clearKey();
}

/** 两个服务各自的「能不能点」：切换服务时动作跟着换，不看另一个的状态。 */
const canVerify = computed(() => (service.value === 'mineru'
  ? !!mineruBase.has_token && !mineruBusy.value
  : !!(ws.value?.ready) && !ws.value?.testing));
const canSave = computed(() => (service.value === 'mineru'
  ? mineruChanged.value && !mineruBusy.value
  : !!(ws.value?.dirty) && !ws.value?.busy));
const canClear = computed(() => (service.value === 'mineru'
  ? !!mineruBase.has_token && !mineruBusy.value
  : !!(ws.value?.hasKey) && !ws.value?.busy));

onMounted(() => {
  void loadMineru();
  // 搜索配置由子组件自己拉；这里只保证切到它时有数据（子组件 onMounted 已经拉过）
});
</script>

<template>
  <div class="ons">
    <!-- 页头：标题 + 一句说明 + 右上角状态与三个动作（两个服务共用同一条操作区） -->
    <!-- 标题由窗格抬头（.pane-head）给，这里只补一句说明 + 右侧状态，避免标题出现两次 -->
    <header class="ons-head">
      <div class="ons-head-text">
        <p class="ons-sub">管理文献解析与联网搜索服务的配置</p>
      </div>
      <!-- 右上角只留状态：按钮统一收在底部那一排气（用户口径：别出现两处同样的按钮） -->
      <div class="ons-actions">
        <span class="badge" :class="statusBadge.cls" :title="statusBadge.title">{{ statusBadge.text }}</span>
      </div>
    </header>

    <!-- 服务切换：两块同宽卡片，选中的那枚描边高亮（不靠颜色也用对勾标出来） -->
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

    <!-- ① 在线解析（MinerU） -->
    <section class="ons-panel" v-if="service === 'mineru'">
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
                ? `已保存 ${mineruTokenText}（留空 = 不修改）`
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
          <p class="load-error tiny" v-if="mineruError">{{ mineruError }}</p>
        </div>
      </div>

      <div class="ons-sec">
        <h3 class="ons-sec-title"><AppIcon name="sliders" :size="14" />解析参数</h3>
        <div class="ons-sec-body">
          <div class="ons-grid">
            <label class="field">
              <span class="field-top">模型版本</span>
              <select v-model="mineruForm.model_version">
                <option v-for="m in MINERU_MODELS" :key="m.value" :value="m.value">{{ m.label }}</option>
              </select>
            </label>
            <label class="field">
              <span class="field-top">语言（影响 OCR）</span>
              <select v-model="mineruForm.language">
                <option v-for="l in MINERU_LANGS" :key="l.value" :value="l.value">{{ l.label }}</option>
              </select>
            </label>
            <label class="field">
              <span class="field-top">默认解析引擎</span>
              <select v-model="mineruForm.engine_default">
                <option value="local">本地解析（离线、快）</option>
                <option value="mineru">在线解析（MinerU 云端）</option>
              </select>
              <span class="field-hint">仅作导入弹窗初值，导入时仍可逐篇改</span>
            </label>
          </div>
        </div>
      </div>
    </section>

    <!-- ② 网联搜索（DeepSeek）：表单与读写都在共用组件里，这里只嵌入 -->
    <section class="ons-panel" v-else>
      <div class="ons-sec">
        <h3 class="ons-sec-title"><AppIcon name="sliders" :size="14" />基础配置</h3>
        <div class="ons-sec-body">
          <WebSearchSettings ref="ws" :actions="false" />
        </div>
      </div>
    </section>

    <!-- 动作只有这一处：右对齐贴在页底，换服务时三个按钮位置不动（用户口径：别出现两排同样的按钮） -->
    <footer class="ons-foot">
      <span class="ons-foot-hint">改动即时写入本机配置，凭据只存在服务端</span>
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

/* 页头：左标题右动作。动作永远贴右缘，换服务时位置不动 */
.ons-head { display: flex; align-items: flex-start; gap: 16px; }
.ons-head-text { min-width: 0; flex: 1 1 auto; }
.ons-title {
  display: flex; align-items: center; gap: 8px;
  margin: 0; font-size: calc(17px * var(--ts, 1)); font-weight: 650; line-height: 1.3; color: var(--text);
}
.ons-title :deep(svg) { color: var(--accent); }
.ons-sub { margin: 4px 0 0; font-size: calc(12px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint); }
.ons-actions { display: flex; align-items: center; gap: 8px; flex: none; flex-wrap: wrap; justify-content: flex-end; }

/* 服务切换：两块等宽，选中 = 一档更亮的底 + 强调色描边 + 对勾（不只靠颜色） */
.ons-switch { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.ons-tile {
  display: flex; align-items: center; gap: 10px; min-width: 0;
  padding: 12px 14px; text-align: left;
  border: 1px solid var(--border); border-radius: var(--r-md); background: var(--bg-elev);
  color: var(--text-dim); cursor: pointer;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.ons-tile:hover { border-color: var(--border-strong); background: var(--bg-hover); }
.ons-tile.on {
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
  background: var(--bg-elev);
  box-shadow: 0 1px 2px rgba(13, 32, 38, .04);
}
.ons-tile-icon {
  display: grid; place-items: center; flex: none;
  width: 30px; height: 30px; border-radius: var(--r-sm);
  background: var(--bg-sunken); color: var(--text-faint);
}
.ons-tile.on .ons-tile-icon { color: var(--accent); }
.ons-tile-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
.ons-tile-label { font-size: calc(13.5px * var(--ts, 1)); font-weight: 650; color: var(--text); }
.ons-tile-sub {
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ons-tile-check { flex: none; color: var(--accent); }

/* 一屏只有一个面板：分组之间用发丝线隔开，不再给每组套一层卡片 */
.ons-panel { display: flex; flex-direction: column; gap: 20px; }
.ons-sec { min-width: 0; display: flex; flex-direction: column; gap: 8px; }
.ons-sec-title {
  display: flex; align-items: center; gap: 6px; margin: 0;
  font-size: calc(13px * var(--ts, 1)); font-weight: 650; line-height: 1.35; color: var(--text);
}
.ons-sec-title :deep(svg) { color: var(--text-faint); }
.ons-sec-body {
  display: flex; flex-direction: column; gap: 12px;
  padding: 14px 16px; border-radius: var(--r-sm); background: var(--bg-sunken);
}
/* 全局 .field > input 有 340px 上限（那是论文库那种窄表单的尺子）：服务凭据这种长文本要整行 */
.ons-sec-body .field > input,
.ons-sec-body .field > select { box-sizing: border-box; width: 100%; max-width: none; }
.ons-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.ons-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
/* 底部动作排：一条发丝线把它与内容分开，按钮贴右缘（与页头状态同一列右缘） */
.ons-foot {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end;
  padding-top: 12px; border-top: 1px solid var(--divider);
}
.ons-foot-hint {
  flex: 1 1 auto; min-width: 0; margin-right: 4px;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
}
.field-hint { font-size: calc(11.5px * var(--ts, 1)); line-height: 1.45; color: var(--text-faint); }
.verify-msg { margin: 0; overflow-wrap: anywhere; }
.verify-msg.ok { color: var(--ok); }
.verify-msg.bad { color: var(--danger); }

/* 窄屏：页头动作换行到标题下方，服务卡片与三列表单逐级降列 */
@media (max-width: 900px) {
  .ons-head { flex-direction: column; gap: 12px; }
  .ons-actions { justify-content: flex-start; }
  .ons-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 560px) {
  .ons-switch, .ons-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
