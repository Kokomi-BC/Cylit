<script setup lang="ts">
/**
 * 「重新解析」对话框：把导入弹窗里那几个"每篇不同"的参数再摆一次。
 *
 * 为什么要有它：导入时的引擎/模型/OCR/公式/表格/页码范围只落在**那一次**的 PARSE 任务参数上，
 * 后面对同一篇重跑时（换引擎、上次开了强制 OCR 这次想关掉、超长论文要分段）没有入口，
 * 只能删掉重导。这个对话框补的就是这条路 —— 它不改导入流程，只是同一个接口
 * （`POST /papers/{id}/parse`）的另一处调用点。
 *
 * 初值一律来自设置页：`GET /parse/config`（导入默认值）+ `GET /mineru/config`（默认引擎 /
 * 模型 / 语言 / 有没有 token）。两个接口都失败时退回内置默认值，不让重解析被一个可选接口卡住。
 */
import { computed, reactive, ref, watch } from 'vue';
import { api } from '@/api/client';
import { reportError, toast } from '@/stores/app';
import type { MineruConfig, MineruModelVersion, ParseConfig, ParseEngine } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
import Switch from '@/components/Switch.vue';

const props = defineProps<{ open: boolean; paperId: string; title?: string }>();
const emit = defineEmits<{ (e: 'close'): void; (e: 'done'): void }>();

interface Form {
  engine: ParseEngine;
  model: string;
  language: string;
  is_ocr: boolean;
  formula: boolean;
  table: boolean;
  ranges: string;
  figure_caption: boolean;
  deep_figure_search: boolean;
  then_translate: boolean;
}

const DEFAULT_FORM: Form = {
  engine: 'local',
  model: 'pipeline',
  language: 'en',
  is_ocr: false,
  formula: true,
  table: true,
  ranges: '',
  figure_caption: false,
  deep_figure_search: false,
  then_translate: false,
};

const form = reactive<Form>({ ...DEFAULT_FORM });
const loading = ref(false);
const busy = ref(false);
const hasToken = ref(false);
const modelOptions = ref<Array<{ value: string; label: string }>>([
  { value: 'pipeline', label: 'pipeline（版面 + 公式更稳，默认）' },
  { value: 'vlm', label: 'vlm（视觉大模型，复杂版面更强）' },
]);
const langOptions = ref<Array<{ value: string; label: string }>>([
  { value: 'en', label: '英语' },
  { value: 'ch', label: '中文' },
]);

const needsToken = computed(() => form.engine === 'mineru' && !hasToken.value);

/** 打开时重新取一次默认值：用户很可能刚在设置页改完就回来重解析。 */
async function loadDefaults(): Promise<void> {
  loading.value = true;
  try {
    const [prefs, cfg] = await Promise.all([
      api.get<ParseConfig>('/parse/config').catch(() => null),
      api.get<MineruConfig>('/mineru/config').catch(() => null),
    ]);
    const next: Form = { ...DEFAULT_FORM };
    if (cfg) {
      hasToken.value = cfg.has_token === true;
      next.engine = cfg.engine_default === 'mineru' ? 'mineru' : 'local';
      if (cfg.model_version) next.model = String(cfg.model_version);
      if (cfg.language) next.language = String(cfg.language);
      if (cfg.model_version_options?.length) modelOptions.value = cfg.model_version_options;
      if (cfg.languages?.length) langOptions.value = cfg.languages;
    }
    if (prefs) {
      next.is_ocr = prefs.mineru_is_ocr === true;
      next.formula = prefs.mineru_enable_formula !== false;
      next.table = prefs.mineru_enable_table !== false;
      next.ranges = String(prefs.mineru_page_ranges ?? '');
      next.figure_caption = prefs.parse_figure_caption === true;
      next.deep_figure_search = prefs.parse_deep_figure_search === true;
    }
    Object.assign(form, next);
  } finally {
    loading.value = false;
  }
}

watch(() => props.open, (open) => { if (open) void loadDefaults(); });

async function submit(): Promise<void> {
  if (form.engine === 'mineru' && needsToken.value) {
    toast('还没配置 MinerU token：先到「设置 → 在线服务 → 在线解析」填一个', 'warn', 4200);
    return;
  }
  const ranges = form.ranges.trim();
  if (form.engine === 'mineru' && ranges && !/^[0-9,\-\s]+$/.test(ranges)) {
    toast('页码范围只能填数字、逗号和连字符，例如 1-10 或 2,4-6', 'warn', 4200);
    return;
  }
  busy.value = true;
  try {
    const task = await api.post<{ __meta?: { message?: string } }>(
      `/papers/${props.paperId}/parse`, {},
      {
        force: true,
        then_translate: form.then_translate,
        engine: form.engine,
        ...(form.engine === 'mineru'
          ? {
            mineru_model_version: form.model,
            mineru_language: form.language,
            mineru_is_ocr: form.is_ocr,
            mineru_enable_formula: form.formula,
            mineru_enable_table: form.table,
            ...(ranges ? { mineru_page_ranges: ranges } : {}),
          }
          : {
            figure_caption: form.figure_caption,
            deep_figure_search: form.deep_figure_search,
          }),
      },
    );
    toast(String(task?.__meta?.message ?? '') || '已创建解析任务', 'ok', 4200);
    emit('done');
    emit('close');
  } catch (err) {
    reportError(err, '重新解析失败');
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <Modal :open="open" :title="title ? '重新解析：' + title : '重新解析'" @close="emit('close')">
    <p class="rp-note">初值来自「设置 → 在线服务」，这里可以只对这一篇改。重跑会覆盖这篇的解析产物（译文与笔记不受影响）。</p>

    <p class="rp-loading" v-if="loading">正在读取默认值…</p>
    <template v-else>
      <div class="rp-engines" role="radiogroup" aria-label="解析引擎">
        <button
          v-for="opt in [
            { value: 'local', name: '本地解析', note: '离线、快；公式与复杂表格偏弱' },
            { value: 'mineru', name: '在线解析（MinerU）', note: '公式 / 表格更强；需要 token 且必须联网' },
          ]"
          :key="opt.value"
          type="button"
          class="rp-engine"
          :class="{ on: form.engine === opt.value }"
          role="radio"
          :aria-checked="form.engine === opt.value"
          :data-reparse-engine="opt.value"
          @click="form.engine = opt.value as ParseEngine"
        >
          <span class="rp-engine-name">{{ opt.name }}</span>
          <span class="rp-engine-note">{{ opt.note }}</span>
        </button>
      </div>

      <p class="rp-warn" v-if="needsToken">
        <AppIcon name="warning" :size="13" />还没配置 MinerU token：请到「设置 → 在线服务 → 在线解析」填入并验证
      </p>

      <div class="rp-grid" v-if="form.engine === 'mineru'">
        <label class="field">
          <span class="field-top">模型版本</span>
          <select v-model="form.model" data-reparse-model>
            <option v-for="m in modelOptions" :key="m.value" :value="m.value">{{ m.label }}</option>
          </select>
        </label>
        <label class="field">
          <span class="field-top">OCR 语言</span>
          <select v-model="form.language" data-reparse-language>
            <option v-for="l in langOptions" :key="l.value" :value="l.value">{{ l.label }}</option>
          </select>
        </label>
      </div>

      <template v-if="form.engine === 'mineru'">
        <Switch v-model="form.is_ocr">
          <span class="rp-opt"><span class="rp-opt-text">强制 OCR</span>
            <span class="rp-opt-note">扫描件 / 字体乱码时打开；有文本层的 PDF 一般不需要（更慢）</span></span>
        </Switch>
        <Switch v-model="form.formula">
          <span class="rp-opt"><span class="rp-opt-text">识别公式</span>
            <span class="rp-opt-note">关掉可少一次模型调用；公式会退化成图片</span></span>
        </Switch>
        <Switch v-model="form.table">
          <span class="rp-opt"><span class="rp-opt-text">识别表格</span>
            <span class="rp-opt-note">关掉可快一些；有表格的论文要开</span></span>
        </Switch>
        <label class="field">
          <span class="field-top">页码范围</span>
          <input v-model="form.ranges" data-reparse-ranges placeholder="留空 = 全部页；例：1-10、2,4-6" />
        </label>
      </template>

      <template v-else>
        <Switch v-model="form.figure_caption">
          <span class="rp-opt"><span class="rp-opt-text">图片是否包含图注</span>
            <span class="rp-opt-note">开 = 图体与图注拼成一张；关 = 只裁图体本身</span></span>
        </Switch>
        <Switch v-model="form.deep_figure_search">
          <span class="rp-opt"><span class="rp-opt-text">深度图片搜索</span>
            <span class="rp-opt-note">开 = 整页没有图注时也按墨迹带兜底裁图</span></span>
        </Switch>
      </template>

      <Switch v-model="form.then_translate">
        <span class="rp-opt"><span class="rp-opt-text">解析完接着翻译</span>
          <span class="rp-opt-note">解析完成后自动排一个逐页翻译任务</span></span>
      </Switch>
    </template>

    <template #footer>
      <button class="btn" @click="emit('close')">取消</button>
      <button class="btn primary" :disabled="busy || loading" @click="submit">
        {{ busy ? '提交中…' : '开始重新解析' }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.rp-note { margin: 0 0 12px; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.6; color: var(--text-faint); }
.rp-loading { margin: 0 0 12px; font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); }
.rp-engines { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }
.rp-engine {
  display: flex; flex-direction: column; gap: 3px; min-width: 0; padding: 10px 12px;
  text-align: left; border: 1px solid var(--border); border-radius: var(--r-md);
  background: var(--bg-elev); cursor: pointer;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.rp-engine:hover { border-color: var(--border-strong); }
.rp-engine.on { border-color: color-mix(in srgb, var(--accent) 55%, transparent); background: var(--bg-elev); }
.rp-engine-name { font-size: calc(13px * var(--ts, 1)); font-weight: 650; color: var(--text); }
.rp-engine-note { font-size: calc(11.5px * var(--ts, 1)); line-height: 1.45; color: var(--text-faint); }
.rp-warn {
  display: flex; align-items: center; gap: 6px; margin: 0 0 12px;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--warn);
}
.rp-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 12px; }
.rp-opt { display: flex; flex-direction: column; gap: 2px; }
.rp-opt-text { font-size: calc(12.5px * var(--ts, 1)); color: var(--text); }
.rp-opt-note { font-size: calc(11px * var(--ts, 1)); line-height: 1.45; color: var(--text-faint); }
@media (max-width: 560px) {
  .rp-engines, .rp-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
