<script setup lang="ts">
/**
 * 联网搜索（DeepSeek 搜索）配置 —— **唯一的一份**。
 *
 * 同一份配置出现在两个地方：Cyli 偏好里 web-search 技能行的齿轮（就地展开），
 * 以及「在线服务 → 网联搜索」。两边都只是这份组件的宿主，读写的是同一组接口
 * （`GET/PUT /agent/websearch/config`、`POST /agent/websearch/verify`）与同一个
 * 服务端密钥文件（`data/secrets/websearch.key`）—— 不存在第二份配置数据。
 *
 * `actions`：默认 true = 组件自带「保存 / 测试 / 清除密钥」那一行；
 * 在线服务页把这三个动作摆在页面右上角（用户口径），于是传 false，
 * 由父级通过 `ref` 调暴露出来的 save/verify/clearKey —— 状态与校验仍然只在这里写一次。
 *
 * 密钥语义（后端定的）：接口只回掩码，输入框**留空 = 本次不改**，
 * 清除是一枚单独的动作 + 二次确认。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { api } from '@/api/client';
import { toast } from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import type { WebsearchConfig, WebsearchVerifyResult } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import Switch from '@/components/Switch.vue';

/** actions=false 时由父级把「保存 / 测试 / 清除密钥」摆到页面右上角。 */
withDefaults(defineProps<{ actions?: boolean }>(), { actions: true });

interface WsForm {
  provider: string;
  count: number | '';
  need_content: boolean;
  fetch_chars: number | '';
}

/** 与后端默认值同一套（deepseek / 8 条 / 不取正文 / 抓 6000 字）。 */
const WS_DEFAULT: WsForm = { provider: 'deepseek', count: 8, need_content: false, fetch_chars: 6000 };

/** 配置读到过（false = 接口未就绪，只给一句空态，不给能写坏东西的控件）。 */
const ready = ref(false);
const loading = ref(false);
const busy = ref(false);
const testing = ref(false);
const providers = ref<{ id: string; label: string }[]>([]);
const hasKey = ref(false);
const keyMasked = ref('');
/** 本次要写进去的新密钥；空串 = 不改（后端把空串当"没提供"）。 */
const apiKey = ref('');
/** 服务端那一份：用来算"改了没有"。 */
const base = reactive<WsForm>({ ...WS_DEFAULT });
const form = reactive<WsForm>({ ...WS_DEFAULT });
/** 测试结论：HTTP 一律 200，成败只看这个对象，所以它必须留在页面上，不能只弹一条 toast。 */
const test = ref<WebsearchVerifyResult | null>(null);

function num(v: unknown, fallback: number): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

/** 一次填两处：base 只跟随服务端，form 是用户正在编辑的那份。 */
function fill(cfg: WebsearchConfig | null | undefined): void {
  providers.value = Array.isArray(cfg?.providers) ? cfg.providers : [];
  hasKey.value = cfg?.has_key === true;
  keyMasked.value = String(cfg?.key_masked ?? '');
  const next: WsForm = {
    provider: String(cfg?.provider || WS_DEFAULT.provider),
    count: num(cfg?.count, 8),
    need_content: cfg?.need_content === true,
    fetch_chars: num(cfg?.fetch_chars, 6000),
  };
  Object.assign(base, next);
  Object.assign(form, next);
  apiKey.value = '';
  ready.value = true;
}

async function reload(): Promise<void> {
  loading.value = true;
  try {
    fill(await api.get<WebsearchConfig>('/agent/websearch/config'));
  } catch {
    ready.value = false;                 // 接口未上线 / 读失败：只读空态，不假装有配置
  } finally {
    loading.value = false;
  }
}

const dirty = computed(() =>
  apiKey.value.trim() !== ''
  || form.provider !== base.provider
  || form.count !== base.count
  || form.need_content !== base.need_content
  || form.fetch_chars !== base.fetch_chars);

async function save(): Promise<void> {
  if (form.count === '' || form.fetch_chars === '') {
    toast('「返回条数」与「抓取字数上限」都要一个数字', 'warn');
    return;
  }
  busy.value = true;
  test.value = null;                     // 改了配置，上一次的测试结论就不作数了
  try {
    // 只提交**改动过的**字段：后端每个字段都是可选的，把没动过的条数也写一遍
    // 等于替我重新拍一次板，将来默认值变了也不会被旧页面覆盖回去。
    const body: Record<string, unknown> = {};
    if (form.provider !== base.provider) body.provider = form.provider;
    if (form.count !== base.count) body.count = Number(form.count);
    if (form.need_content !== base.need_content) body.need_content = form.need_content;
    if (form.fetch_chars !== base.fetch_chars) body.fetch_chars = Number(form.fetch_chars);
    const fresh = apiKey.value.trim();
    if (fresh) body.api_key = fresh;     // 空串 = 本次不改，所以干脆不放进 body
    fill(await api.put<WebsearchConfig>('/agent/websearch/config', body));
    toast('联网搜索设置已保存', 'ok', 2600);
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 5000);
  } finally {
    busy.value = false;
  }
}

/** 清除密钥：不可逆（服务端那份文件真被删掉），二次确认。 */
async function clearKey(): Promise<void> {
  if (!(await askConfirm({
    title: '清除联网搜索的 API Key？',
    body: '清掉之后 web-search 技能就搜不了东西，要重新填一把才能用。',
    danger: true,
    confirmLabel: '清除',
  }))) return;
  busy.value = true;
  test.value = null;
  try {
    fill(await api.put<WebsearchConfig>('/agent/websearch/config', { clear_key: true }));
    toast('已清除联网搜索密钥', 'ok', 3200);
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 5000);
  } finally {
    busy.value = false;
  }
}

/** 测试：后端真跑一次 count=1 的搜索。失败也得**原地**说清楚是哪一步坏了，所以结论进 test。 */
async function verify(): Promise<void> {
  testing.value = true;
  test.value = null;
  try {
    test.value = await api.post<WebsearchVerifyResult>('/agent/websearch/verify');
  } catch (err) {
    // 走到这儿是连"跑测试"都没发出去（接口未上线 / 网络），一样留在原地，不弹完就没了
    test.value = { ok: false, state: 'error', detail: (err as Error)?.message ?? String(err), ms: 0 };
  } finally {
    testing.value = false;
  }
}

// 组件挂载即拉一次：宿主（技能行 / 在线服务）都是「展开或切到才挂载」，所以这仍是懒加载
onMounted(() => { void reload(); });

defineExpose({ reload, save, verify, clearKey, ready, loading, busy, testing, dirty, test, hasKey });
</script>

<template>
  <div class="ws">
    <p class="ws-hint" v-if="loading && !ready">正在读取联网搜索配置…</p>
    <p class="ws-hint" v-else-if="!ready">联网搜索设置暂不可用（接口未就绪）。</p>
    <template v-else>
      <!-- 基础配置：三个字段一行（窄了自动折成两列 / 一列），不给每个字段套卡片 -->
      <div class="ws-grid">
        <label class="field">
          <span class="field-top">提供方</span>
          <select v-model="form.provider">
            <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.label }}</option>
          </select>
        </label>
        <label class="field">
          <span class="field-top">返回条数</span>
          <input type="number" min="1" max="50" step="1" v-model.number="form.count" />
        </label>
        <label class="field">
          <span class="field-top">抓取字数上限</span>
          <input type="number" min="500" max="200000" step="500" v-model.number="form.fetch_chars" />
        </label>
      </div>

      <div class="ws-key">
        <label class="field">
          <span class="field-top">API Key</span>
          <input
            type="password"
            v-model="apiKey"
            autocomplete="off"
            spellcheck="false"
            :placeholder="hasKey
              ? (keyMasked ? '已保存 ' + keyMasked + '，留空不改' : '已保存密钥，留空不改')
              : '粘贴提供方控制台里的密钥'"
          />
        </label>
      </div>

      <div class="ws-fetch">
        <div class="field">
          <span class="field-top">网页抓取</span>
          <Switch v-model="form.need_content">抓取网页正文</Switch>
          <span class="field-hint">每页最多抓上方设定的字数</span>
        </div>
      </div>

      <p v-if="test" class="ws-test" :class="test.ok ? 'ok' : 'bad'">
        <AppIcon :name="test.ok ? 'check' : 'ban'" :size="13" />
        <span>{{ test.ok ? '验证通过' : '验证失败' }}</span>
        <span class="ws-test-meta">{{ test.state }} · {{ test.ms }}ms</span>
        <span v-if="test.detail" class="ws-test-detail">{{ test.detail }}</span>
      </p>

      <!-- 父级把动作放到了页面右上角时，这里不再重复一排按钮 -->
      <div class="ws-actions" v-if="actions">
        <button class="btn primary sm" :disabled="busy || !dirty" @click="save">
          <AppIcon name="check" :size="15" />{{ busy ? '保存中…' : '保存' }}
        </button>
        <button class="btn sm" :disabled="testing" @click="verify">
          <AppIcon name="play" :size="15" />{{ testing ? '测试中…' : '测试' }}
        </button>
        <button class="btn sm danger" :disabled="busy || !hasKey" @click="clearKey">
          <AppIcon name="ban" :size="15" />清除密钥
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* 表单只有一种节奏：字段之间 12，字段内标签/控件/提示 5 —— 与全页其它表单同档 */
.ws { display: flex; flex-direction: column; gap: 12px; }
.ws-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
/* 全局 .field > input 带 340px 上限（窄表单的尺子）：接口参数与密钥要整行才看得全 */
.ws .field > input, .ws .field > select { box-sizing: border-box; width: 100%; max-width: none; }
.ws-key, .ws-fetch { display: flex; flex-direction: column; gap: 12px; }
.field-hint { font-size: calc(11.5px * var(--ts, 1)); line-height: 1.45; color: var(--text-faint); }
.ws-hint { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.6; color: var(--text-dim); }
.ws-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
/* 测试结论：成功绿、失败红；细节退回弱色 —— 一眼看到成败，细看才知道卡在哪一步 */
.ws-test {
  display: flex; align-items: flex-start; gap: 6px; flex-wrap: wrap;
  margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5;
  overflow-wrap: anywhere;
}
.ws-test.ok { color: var(--ok); }
.ws-test.bad { color: var(--danger); }
.ws-test-meta { color: var(--text-faint); font-variant-numeric: tabular-nums; }
.ws-test-detail { color: var(--text-dim); }

/* 三列在中等宽度就挤了：900 折两列、560 折一列（与全站断点一致） */
@media (max-width: 900px) {
  .ws-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 560px) {
  .ws-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
