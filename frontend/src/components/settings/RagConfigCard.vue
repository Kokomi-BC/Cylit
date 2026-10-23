<script setup lang="ts">
/**
 * 「检索配置」卡片：设置页「索引管理」重做里唯一写检索参数的一块。
 *
 * **它是什么**：三组参数 —— ①三项召回 top_k ②重排（开关 / 候选数 / 当前绑定的重排模型）
 * ③允许通用知识。四项数字 + 一项布尔，全部落在 setting 表，走 PUT /settings。
 *
 * **数据从哪来**（组件自身不保存任何一份"默认值"或"当前值"的副本）：
 * - GET /settings → values（回填表单与"改了没有"的基线）/ defaults（「恢复默认」唯一来源）。
 *   defaults 缺失时按钮**禁用并说明原因**，绝不在前端另写一份 20/20/12/6 —— 那会变成
 *   第二处默认值，改了后端这里会静默漂掉（backend/api/settings.py 顶部那段注释记的
 *   就是这类"两处键名 / 两处默认值"的坑）。
 * - GET /models/types → defaults.reranker，只读显示「重排模型」。重排模型不在这里改：
 *   它属于模型页（reranker 类型）的事，这里放一个下拉等于把"绑定"这件事复制两份。
 * - 校验不查后端：retrieval.rag_k() 只认整数，越界 / 小数在服务端不会被拦，只会写进一个
 *   "看起来生效、其实跑的不是这个数"的值，所以在提交前拦下（该字段标红 + 保存禁用）。
 *
 * **为什么这么排**：顺序照**一次提问的数据流**走 —— 先召回（两路 top_k）、再管融合后留多少、
 * 再重排、最后才是"检索不够时能不能用模型自己的知识"。用户从上往下读，就是在读自己的问题
 * 是怎么被检索出来的；「启用重排」紧贴重排候选数，因为它是同一个键的两半（见下方 rerankOn）。
 * dirty 比对、fillRag、保存后 rejected 逐项提示，全部沿用 KbIndexPanel.vue 里那套写法：
 * 同一份数据面两处必须同构，否则同一次改动在两个界面里的"未保存"判定会不一致。
 */
import { computed, onMounted, reactive, ref } from 'vue';
import { api } from '@/api/client';
import type { ModelTypeDefault, SettingsPayload } from '@/api/types';
import { loadModelTypes, reportError, state, toast } from '@/stores/app';
import AppIcon from '@/components/AppIcon.vue';
import Switch from '@/components/Switch.vue';

/* ------------------------------- 字段表 -------------------------------
 * 四个数字键各自的 min 与说明就写在这一张表里：模板按表渲染，校验按表判断 ——
 * 加一个参数是加一行，而不是"模板、校验、MIN 常量"三处各改一遍。
 * --------------------------------------------------------------------- */

interface RagNumField {
  key: 'rag_top_k_vector' | 'rag_top_k_lexical' | 'rag_top_k_fused' | 'rag_top_n_rerank';
  label: string;
  hint: string;
  min: number;
}

type RagKey = RagNumField['key'] | 'allow_general_knowledge';

const RAG_NUM: RagNumField[] = [
  { key: 'rag_top_k_vector', label: '向量召回 top_k', hint: '语义检索这一路取回多少条候选', min: 1 },
  { key: 'rag_top_k_lexical', label: '关键词召回 top_k', hint: 'BM25 这一路取回多少条候选（专有名词、公式符号靠它）', min: 1 },
  { key: 'rag_top_k_fused', label: '融合后 top_k', hint: '两路按名次融合（RRF）后，留给重排的条数', min: 1 },
  { key: 'rag_top_n_rerank', label: '重排候选数量', hint: '重排后交给模型引用的条数', min: 0 },
];

/** 前三项是召回（同一组），第 4 项单独落在「重排参数」组里。 */
const RAG_TOP = RAG_NUM.slice(0, 3);
const RAG_RERANK = RAG_NUM[3];
const RAG_KEYS: RagKey[] = [...RAG_NUM.map((f) => f.key), 'allow_general_knowledge'];

/** 提示文案里的中文名：保存接口只回键名，直接把 rag_top_k_fused 甩给用户没人看得懂。 */
const LABELS: Record<RagKey, string> = {
  rag_top_k_vector: '向量召回 top_k',
  rag_top_k_lexical: '关键词召回 top_k',
  rag_top_k_fused: '融合后 top_k',
  rag_top_n_rerank: '重排候选数量',
  allow_general_knowledge: '允许通用知识',
};

/* ------------------------------- 表单状态 ------------------------------- */

const ragForm = reactive<Record<RagKey, number | '' | boolean>>({
  rag_top_k_vector: '', rag_top_k_lexical: '', rag_top_k_fused: '',
  rag_top_n_rerank: '', allow_general_knowledge: false,
});
/** 服务端**当前生效**的那一份：用来算"改了没有"，也是「撤销改动」回填的目标。 */
const ragBase = reactive<Record<RagKey, number | '' | boolean>>({ ...ragForm });

/** GET /settings 的 defaults：只有它给出的键才能「恢复默认」。 */
const defaults = ref<Record<string, any> | null>(null);
/** 只在 PUT /settings 期间为真。 */
const busy = ref(false);
/** 成功读到过一次服务端值；没读到时那句话（默认值不可用）不提前吓人。 */
const loaded = ref(false);
/** 正在读（首帧与重试）：表单只读，且绝不显示校验错误。 */
const loading = ref(false);
/** 读失败：只给一句「读取失败」+ 重试，不给四个空 input 配红字。 */
const loadFailed = ref(false);
/** 失败原因原样显示（不翻译、不改写），失败态里总得留下点可排查的信息。 */
const loadError = ref('');

/** 后端 values 是动态 JSON：在这里收敛成"数字 or 空串 / 布尔"，与 KbIndexPanel 完全同构。 */
function fillRag(values: Record<string, any> | null | undefined): void {
  const v = values || {};
  for (const f of RAG_NUM) {
    const raw = v[f.key];
    ragForm[f.key] = raw === null || raw === undefined || raw === '' ? '' : Number(raw);
    ragBase[f.key] = ragForm[f.key];
  }
  ragForm.allow_general_knowledge = v.allow_general_knowledge === true
    || v.allow_general_knowledge === 'true' || v.allow_general_knowledge === 1;
  ragBase.allow_general_knowledge = ragForm.allow_general_knowledge;
}

/**
 * 读当前检索配置。失败不弹 toast，而是把卡片切成失败态（一句「读取失败」+ 重试）：
 * 首帧到 /settings 返回之间本来就有 1–2 秒，这两秒里冒出一条错误提示、四个空输入框
 * 再加四行红字，等于把"还没读到"报成"用户填错了"。
 */
async function loadRag(): Promise<void> {
  if (loading.value) return;
  loading.value = true;
  loadFailed.value = false;
  try {
    const data = await api.get<SettingsPayload>('/settings');
    defaults.value = data?.defaults ?? null;
    fillRag(data?.values);
    loaded.value = true;
    loadError.value = '';
  } catch (err) {
    loadFailed.value = true;
    loadError.value = err instanceof Error ? err.message : '';
  } finally {
    loading.value = false;
  }
}

/* ------------------------------- 默认值 ------------------------------- */

/**
 * 五个键的默认值必须由后端给齐才允许恢复 —— 只恢复一半比不给更糟：
 * 用户会以为"已经回到默认了"，其实某一项还是旧值。所以全有或全无。
 */
const defaultsAvailable = computed<boolean>(() => {
  const d = defaults.value;
  if (!d) return false;
  return RAG_KEYS.every((k) => d[k] !== undefined && d[k] !== null);
});

/** 把表单填成 defaults，**不保存**：默认值只是一次未提交的编辑，写不写回由用户决定。 */
function applyDefaults(): void {
  const d = defaults.value;
  if (!d) return;
  for (const f of RAG_NUM) {
    const raw = d[f.key];
    ragForm[f.key] = raw === undefined || raw === null || raw === '' ? '' : Number(raw);
  }
  ragForm.allow_general_knowledge = d.allow_general_knowledge === true;
}

/** 撤回到服务端当前生效值（保存成功后若只想放弃本地改动，也走这里）。 */
function resetForm(): void {
  for (const k of RAG_KEYS) ragForm[k] = ragBase[k];
}

/* ------------------------------- 校验 ------------------------------- */

function validateNum(f: RagNumField): string {
  const raw = ragForm[f.key];
  if (raw === '' || raw === null || raw === undefined) return '请填入数字';
  const n = Number(raw);
  if (!Number.isFinite(n)) return '请填入数字';
  if (!Number.isInteger(n)) return '必须填整数';
  if (n < f.min) return f.min === 0 ? '不能小于 0' : `不能小于 ${f.min}`;
  return '';
}

/**
 * 只对数字项算错：布尔项没有"非法值"这回事。
 *
 * 还没读到服务端值时，空串的意思是"没有值"，不是"填错了" —— 这里必须返回空，否则
 * 首帧就会在四格下面各挂一行「请填入数字」，整张卡看着像坏了。
 */
const errors = computed<Partial<Record<RagKey, string>>>(() => {
  const out: Partial<Record<RagKey, string>> = {};
  if (!loaded.value) return out;
  for (const f of RAG_NUM) {
    const msg = validateNum(f);
    if (msg) out[f.key] = msg;
  }
  return out;
});

const hasErrors = computed<boolean>(() => Object.keys(errors.value).length > 0);

/* ------------------------------- 改动与保存 ------------------------------- */

const changed = computed<RagKey[]>(() => RAG_KEYS.filter((k) => ragForm[k] !== ragBase[k]));

/**
 * 表单能不能编辑：读到服务端值之前一律禁用（含重试中与保存中）。
 * 空着却仍可编辑的数字框会让人以为"本来就该我填"，禁用才说得清"还没读到"。
 */
const formDisabled = computed<boolean>(() => !loaded.value || loading.value || busy.value);

/** 保存的前置条件里必须有 loaded：没读到当前值就谈不上"改动"，更不能把空串写进 setting 表。 */
const canSave = computed<boolean>(() => !formDisabled.value && changed.value.length > 0 && !hasErrors.value);

async function save(): Promise<void> {
  if (!canSave.value) return;
  const patch: Record<string, unknown> = {};
  for (const k of changed.value) patch[k] = ragForm[k];
  busy.value = true;
  try {
    const res = await api.put<{
      applied?: Record<string, unknown>;
      rejected?: Record<string, string>;
      values?: Record<string, unknown>;
    }>('/settings', patch);
    // 以服务端生效值为准：被拒的项在这里回滚成库里那一份，不靠前端猜
    if (res?.values) fillRag(res.values);
    const applied = Object.keys(res?.applied || {});
    const rejected = res?.rejected || {};
    if (applied.length) {
      toast(`已保存（重启后仍生效）：${applied.map((k) => LABELS[k as RagKey] || k).join('、')}`, 'ok', 4200);
    }
    for (const [k, reason] of Object.entries(rejected)) {
      toast(`「${LABELS[k as RagKey] || k}」未生效：${reason}`, 'warn', 5200);
    }
    if (!applied.length && !Object.keys(rejected).length) {
      toast('服务端未返回任何生效项，请检查请求内容', 'warn');
    }
  } catch (err) {
    reportError(err, '保存检索配置失败');
  } finally {
    busy.value = false;
  }
}

/* ------------------------------- 两个开关 ------------------------------- */

const allowGeneral = computed<boolean>({
  get: () => ragForm.allow_general_knowledge === true,
  set: (v) => { ragForm.allow_general_knowledge = v; },
});

/**
 * 「启用重排」是 rag_top_n_rerank > 0 的**视图**，不是第二个设置键。
 *
 * 关掉写 0：后端只有"重排多少条"这一个键，再造一个开关键就会出现"开关开、条数是 0"
 * 这种自相矛盾的状态，而两个键谁说了算没有答案。打开时若原值就是 0，用 defaults 里的
 * 重排数把它填回去；默认值取不到就**不猜**（留 0 让用户自己填，并明确说一声），
 * 否则用户点了开关、开关看起来没动。
 */
const rerankOn = computed<boolean>({
  get: () => Number(ragForm.rag_top_n_rerank) > 0,
  set: (on) => {
    if (!on) { ragForm.rag_top_n_rerank = 0; return; }
    const cur = Number(ragForm.rag_top_n_rerank);
    if (Number.isFinite(cur) && cur > 0) return;
    const raw = defaults.value?.rag_top_n_rerank;
    const fallback = raw === undefined || raw === null || raw === '' ? NaN : Number(raw);
    if (Number.isFinite(fallback) && fallback > 0) {
      ragForm.rag_top_n_rerank = fallback;
      return;
    }
    toast('取不到默认重排条数，请手动填写一个大于 0 的整数', 'warn');
  },
});

/* ---------------------------- 重排模型（只读） ---------------------------- */

const reranker = computed<ModelTypeDefault | null>(() => state.modelTypes?.defaults?.reranker ?? null);

/**
 * is_mock 的默认值（provider 为 mock / hash / heuristic）不是"绑定了一个模型"：
 * retrieval.rerank_hits 遇到它只会走词法启发式并标 degraded。把它的名字显示成已绑定的
 * 重排模型，等于让降级结果看起来像模型打分，所以按未绑定显示，并单独说明它是占位。
 */
const rerankerMock = computed<boolean>(() => reranker.value?.is_mock === true);
/**
 * 值里只留**配置名**：后端把 name 拼成「<配置名> · <类型>」，括号里那串又是各类型的
 * model_name —— 原样塞进值里就是一行四十多个字，会把外面那个字段网格直接撑破。
 * 后缀与括号都是能从 name 上剥掉的装饰，完整的 name / 类型 / model_name 改放 title。
 */
const rerankerName = computed<string>(() => {
  const r = reranker.value;
  if (!r || r.is_mock) return '';
  const raw = (r.name || r.model_name || '').trim();
  const short = raw.replace(/\s*·\s*reranker\s*$/, '').replace(/（[^）]*）\s*$/, '').trim();
  return short || raw;
});

/** 值上放不下的部分（完整 name、类型、model_name、provider）全部进 title，悬停可看全。 */
const rerankerTitle = computed<string>(() => {
  const r = reranker.value;
  if (!r) return '未绑定重排模型：在「模型」页的 reranker 类型里绑定';
  const bits = ['类型：reranker', `model_name：${r.model_name || '—'}`, `配置：${r.name || '—'}`];
  if (r.provider) bits.push(`provider：${r.provider}`);
  if (r.is_mock) bits.push('当前是启发式占位，不是真模型');
  return bits.join(' · ');
});

onMounted(() => {
  void loadRag();
  if (!state.modelTypes) {
    loadModelTypes().catch((err) => reportError(err, '取重排模型失败'));
  }
});
</script>

<template>
  <section class="card">
    <div class="card-head">
      <AppIcon name="sliders" :size="15" />检索配置
      <span class="spacer"></span>
      <span v-if="changed.length" class="badge warn">未保存</span>
      <button class="btn sm ghost" :disabled="!defaultsAvailable || busy"
              :title="defaultsAvailable ? '把表单填成默认值（不保存）' : '后端未返回这几项的默认值'"
              @click="applyDefaults">恢复默认</button>
    </div>

    <div class="card-body cfg-body">
      <!-- 读失败：不给四个空 input 配红字（那看起来像"用户填错了"），只给一句原因 + 重试 -->
      <div v-if="loadFailed" class="load-fail">
        <p class="tiny error-text">读取失败</p>
        <p v-if="loadError" class="tiny faint note">{{ loadError }}</p>
        <button type="button" class="btn sm" :disabled="loading" @click="loadRag()">
          {{ loading ? '重试中…' : '重试' }}
        </button>
      </div>

      <template v-else>
        <!-- 首帧 → GET /settings 返回之间：只说"正在读"，不说"填错了"（errors 此时恒为空） -->
        <p v-if="!loaded" class="tiny faint note">正在读取当前检索配置…</p>
        <p v-if="loaded && !defaultsAvailable" class="tiny faint note">
          后端没有返回这几项在代码里的默认值，所以「恢复默认」暂不可用；仍可手动改任一项再保存。
        </p>

        <!-- ① 检索参数：两路召回各多少条，以及融合之后留多少。
             字段摆成网格（断点见 .field-grid 的容器查询）：宽卡三列并排，窄卡一列，中间不留空。 -->
        <section class="group">
          <h3 class="group-title">检索参数</h3>
          <div class="fwrap">
            <div class="field-grid">
              <div v-for="f in RAG_TOP" :key="f.key" class="field">
                <label class="field-label" :for="`rag-${f.key}`">{{ f.label }}</label>
                <div class="ctl">
                  <input :id="`rag-${f.key}`" class="num-input" :class="{ invalid: !!errors[f.key] }"
                         type="number" step="1" inputmode="numeric" :min="f.min"
                         :aria-invalid="!!errors[f.key]" :disabled="formDisabled"
                         v-model.number="ragForm[f.key]" />
                  <i class="unit">条</i>
                </div>
                <p v-if="errors[f.key]" class="tiny error-text">{{ errors[f.key] }}</p>
                <p v-else class="tiny faint hint">{{ f.hint }}</p>
              </div>
            </div>
          </div>
        </section>

        <!-- ② 重排参数：开关是这一组的第一行（它和条数本来就是同一个键的两半），
             下面「条数 + 当前绑定的模型」走同一个字段网格，于是和①的字段落在同一条竖线上。 -->
        <section class="group">
          <h3 class="group-title">重排参数</h3>

          <div class="field">
            <Switch v-model="rerankOn" :disabled="formDisabled">启用重排</Switch>
            <p class="tiny faint hint">关闭 = 不调用重排模型，直接用 RRF 融合后的顺序。</p>
          </div>

          <div class="fwrap">
            <div class="field-grid">
              <div class="field">
                <label class="field-label" :class="{ dimmed: !rerankOn }" for="rag-rerank">重排候选数量</label>
                <div class="ctl">
                  <input id="rag-rerank" class="num-input" :class="{ invalid: !!errors.rag_top_n_rerank }"
                         type="number" step="1" inputmode="numeric" :min="RAG_RERANK.min"
                         :aria-invalid="!!errors.rag_top_n_rerank" :disabled="formDisabled || !rerankOn"
                         v-model.number="ragForm.rag_top_n_rerank" />
                  <i class="unit">条</i>
                </div>
                <p v-if="errors.rag_top_n_rerank" class="tiny error-text">{{ errors.rag_top_n_rerank }}</p>
                <p v-else-if="!rerankOn" class="tiny faint hint">
                  关闭后不生效：检索直接用 RRF 融合后的顺序，最多「融合后 top_k」条。
                </p>
                <p v-else class="tiny faint hint">{{ RAG_RERANK.hint }}</p>
              </div>

              <!-- 重排模型：只读的一行字，不是输入框 —— 绑定的地方在「模型」页，这里再给个下拉就是两份绑定 -->
              <div class="field">
                <span class="field-label">重排模型</span>
                <p class="model-name" :class="{ faint: !rerankerName }" :title="rerankerTitle">
                  {{ rerankerName || '未绑定' }}
                </p>
                <p class="tiny faint hint">在「模型」页的 reranker 类型里绑定</p>
                <p v-if="rerankerMock && reranker?.name" class="tiny faint hint">
                  当前是启发式占位（{{ reranker.name }}），不是真模型：只按词面重合度打分，检索结果会标注降级。
                </p>
              </div>
            </div>
          </div>
        </section>

        <!-- ③ 回答策略：检索不够时允不允许用模型自己的知识 -->
        <section class="group">
          <h3 class="group-title">回答策略</h3>
          <div class="field">
            <Switch v-model="allowGeneral" :disabled="formDisabled">允许通用知识</Switch>
            <p class="tiny faint hint">检索结果不够时，允许模型用自己的知识回答；关掉则只依据检索到的内容作答。</p>
          </div>
        </section>

        <div class="footer">
          <p class="tiny faint note">改动需要点「保存」才生效；只影响新发起的检索，不改已有 chunk，不需要重建索引。</p>
          <div class="row actions">
            <button class="btn sm ghost" :disabled="!changed.length || formDisabled" @click="resetForm">撤销改动</button>
            <button class="btn primary sm" :disabled="!canSave" @click="save">
              <AppIcon name="check" :size="14" />{{ busy ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
/* 只用 tokens.css 的变量：这里写死任何一个色值，深浅两套皮肤就会当场露馅。 */
/* 卡体就是这张卡的宽度锚点（卡底那行容器查询量的是它）：量"这块地方有多宽"，不是视口有多宽。 */
.cfg-body { display: flex; flex-direction: column; gap: 14px; container-type: inline-size; }

.group { display: flex; flex-direction: column; gap: 10px; }
/* 组之间一条 divider + 14px 呼吸：分组靠线与间距，不靠卡片套卡片（嵌套容器会脏） */
.group + .group { border-top: 1px solid var(--divider); padding-top: 14px; }
.group-title {
  margin: 0;
  font-size: calc(13px * var(--ts, 1));
  font-weight: 600;
  color: var(--text);
}

/* 字段网格的父级：容器查询的锚点，断点只按"这一格有多宽"决策（三列 / 两列 / 一列），
   与视口无关 —— 同一段 CSS 在宽单列的 956px 卡和三栏里的窄卡都成立。 */
.fwrap { container-type: inline-size; }
.field-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px 16px; min-width: 0; }
@container (min-width: 480px) {
  .field-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@container (min-width: 720px) {
  .field-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

/* 一个字段自上而下只有三件事：label → 控件 → 说明。字段自己决定内部宽度，不再靠 spacer 顶。 */
.field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.field-label {
  font-size: calc(13px * var(--ts, 1));
  color: var(--text-dim);
  min-width: 0;
  overflow-wrap: anywhere;
}
/* 关掉重排时这一行"存在但不生效"：降到弱色。不隐藏 —— 隐藏会让下面的行往上跳。 */
.field-label.dimmed { color: var(--text-faint); }

.ctl { display: flex; align-items: center; gap: 6px; min-width: 0; }

/* 数字输入：撑满字段但不超过 120px，右对齐 + 等宽数字 → 三列并排时个位也对得上 */
.num-input { flex: 0 1 auto; width: 100%; max-width: 120px; text-align: right; font-variant-numeric: tabular-nums; }
.num-input.invalid { border-color: var(--danger); }
.num-input:disabled { background: var(--bg-sunken); color: var(--text-faint); cursor: not-allowed; }

.unit { flex: none; font-style: normal; font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }

/* 只读的模型名：占住和 input 一样的高度（同一行的说明才对得齐），长了省略，全量信息在 title */
.model-name {
  margin: 0;
  min-width: 0;
  max-width: 100%;
  line-height: var(--control-h);
  font-size: calc(13px * var(--ts, 1));
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.hint { margin: 0; line-height: 1.5; }
.error-text { margin: 0; color: var(--danger); line-height: 1.5; }
.note { margin: 0; line-height: 1.55; }

/* 读取失败态：一块说明 + 一个重试按钮，替掉半张空表单 */
.load-fail {
  display: flex; flex-direction: column; align-items: flex-start; gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  background: var(--bg-sunken);
}
.load-fail .error-text { font-weight: 600; }
/* 失败原因里可能带一长串 URL / 英文 message：长词也必须断行，不许把卡片撑出横向滚动 */
.load-fail .note { min-width: 0; overflow-wrap: anywhere; }

/* 卡底：说明靠左、按钮靠右，同一行两端对齐。之前说明是贴着按钮的右对齐块，
   读起来像按钮的注解，而不是给整张卡的说明。 */
.footer {
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px;
  border-top: 1px solid var(--divider);
  padding-top: 12px;
}
.footer .note { flex: 1 1 auto; min-width: 0; text-align: left; }
.actions { margin-left: auto; justify-content: flex-end; flex-wrap: wrap; }

/* 窄了：说明独占一行，按钮换到下一行仍靠右（margin-left: auto 在换行后同样生效）。 */
@container (max-width: 559px) {
  .footer .note { flex: 1 1 100%; }
}
</style>
