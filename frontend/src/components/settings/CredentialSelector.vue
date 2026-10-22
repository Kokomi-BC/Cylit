<script setup lang="ts">
/**
 * CredentialSelector —— 「这条模型用哪把密钥」的选择器。
 *
 * 三种来源（后端 `key_mode`）：跟随提供方默认密钥（`auto`）/ 独立指定某条（`key`）/ 无需密钥（`none`）。
 * 选项里**只出现名称与掩码**（后端给的 `masked`，形如 `••••a1b2`）：任何地方都不回显明文，
 * 也不在前端拼任何"可以还原"的片段。
 *
 * 对外契约（C 阶段的新增模型表单复用，**签名别改**）：
 *   props { providerId, keyMode, providerKeyId, keys, defaultKeyId?, disabled? }
 *   emit  update:keyMode / update:providerKeyId / manage
 * 另有一个**可选**扩展位 `allowNone`：该提供方 `auth === 'none'` 时才显示「无需密钥」——
 * 契约里没有 auth 字段，默认 false（不显示），由调用方按提供方能力传入。
 */
import { computed } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import type { KeyRow } from './modelTypes';

const props = withDefaults(defineProps<{
  providerId: string;
  keyMode: 'auto' | 'key' | 'none';
  providerKeyId: string;
  keys: KeyRow[];
  defaultKeyId?: string;
  disabled?: boolean;
  /** 仅当提供方 `auth === 'none'` 时置 true：多出「无需密钥」一项 */
  allowNone?: boolean;
}>(), { defaultKeyId: '', disabled: false, allowNone: false });

const emit = defineEmits<{
  (e: 'update:keyMode', v: 'auto' | 'key' | 'none'): void;
  (e: 'update:providerKeyId', v: string): void;
  (e: 'manage'): void;
}>();

/** 默认密钥：优先后端指的那条，其次 `is_default` 标记的那条。 */
const defaultKey = computed<KeyRow | null>(() => {
  const byId = props.defaultKeyId
    ? props.keys.find((k) => k.id === props.defaultKeyId)
    : null;
  return byId ?? props.keys.find((k) => k.is_default) ?? null;
});

function statusOf(k: KeyRow): { label: string; cls: string } {
  const s = String(k.status || '').toLowerCase();
  if (s === 'ok' || s === 'ready' || s === 'valid') return { label: '可用', cls: 'ok' };
  if (s === 'invalid' || s === 'error' || s === 'failed') return { label: '失效', cls: 'danger' };
  if (s === 'missing') return { label: '缺值', cls: 'warn' };
  return { label: '未测试', cls: '' };
}

function pickAuto(): void {
  if (props.disabled) return;
  emit('update:keyMode', 'auto');
  emit('update:providerKeyId', '');
}

function pickKey(k: KeyRow): void {
  if (props.disabled) return;
  emit('update:keyMode', 'key');
  emit('update:providerKeyId', k.id);
}

function pickNone(): void {
  if (props.disabled) return;
  emit('update:keyMode', 'none');
  emit('update:providerKeyId', '');
}

function manage(): void {
  emit('manage');
}
</script>

<template>
  <div class="cs-root" :class="{ disabled }" role="radiogroup" aria-label="密钥来源">
    <div class="cs-head">
      <span class="cs-title">密钥来源</span>
      <span class="spacer"></span>
      <button type="button" class="link-btn" data-key-manage @click="manage">
        <AppIcon name="document" :size="12" />管理密钥
      </button>
    </div>

    <button
      type="button" class="cs-opt" :class="{ on: keyMode === 'auto' }"
      role="radio" :aria-checked="keyMode === 'auto'" :disabled="disabled"
      data-key-mode="auto" @click="pickAuto"
    >
      <span class="dot" :class="{ on: keyMode === 'auto' }" />
      <span class="cs-main">
        <span class="cs-name">跟随提供方默认密钥</span>
        <span class="cs-sub" :class="{ warn: !defaultKey }">
          {{ defaultKey ? `${defaultKey.label} · ${defaultKey.masked || '（无掩码）'}` : '这家还没有默认密钥' }}
        </span>
      </span>
    </button>

    <button
      v-for="k in keys" :key="k.id" type="button"
      class="cs-opt" :class="{ on: keyMode === 'key' && providerKeyId === k.id }"
      role="radio" :aria-checked="keyMode === 'key' && providerKeyId === k.id" :disabled="disabled"
      :data-key-id="k.id" @click="pickKey(k)"
    >
      <span class="dot" :class="{ on: keyMode === 'key' && providerKeyId === k.id }" />
      <span class="cs-main">
        <span class="cs-name truncate">
          {{ k.label }}
          <span class="cs-flag" v-if="k.is_default || k.id === defaultKeyId">默认</span>
        </span>
        <span class="cs-sub mono">{{ k.masked || '（无掩码）' }}</span>
      </span>
      <span class="cs-status" :class="statusOf(k).cls">{{ statusOf(k).label }}</span>
    </button>

    <button
      v-if="allowNone" type="button" class="cs-opt" :class="{ on: keyMode === 'none' }"
      role="radio" :aria-checked="keyMode === 'none'" :disabled="disabled"
      data-key-mode="none" @click="pickNone"
    >
      <span class="dot" :class="{ on: keyMode === 'none' }" />
      <span class="cs-main">
        <span class="cs-name">无需密钥</span>
        <span class="cs-sub">这家是本机推理 / 公开端点（auth: none）</span>
      </span>
    </button>

    <p class="cs-hint" v-if="!keys.length">
      这家还没有登记密钥 —— 点「管理密钥」新增一条，之后可以在这里指定。
    </p>
  </div>
</template>

<style scoped>
.cs-root {
  display: flex; flex-direction: column; gap: 4px; min-width: 0;
  padding: 8px; border: 1px solid var(--divider); border-radius: var(--radius-sm);
  background: var(--bg-layer);
}
.cs-root.disabled { opacity: .6; }
.cs-head { display: flex; align-items: center; gap: 8px; padding: 0 2px 2px; }
.cs-title { font-size: calc(12px * var(--ts, 1)); font-weight: 600; color: var(--text-dim); }
.link-btn {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 0; border: 0; background: transparent; color: var(--accent-text);
  font: inherit; font-size: calc(11.5px * var(--ts, 1)); cursor: pointer;
}
.link-btn:hover { text-decoration: underline; }
.cs-opt {
  display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0;
  padding: 6px 8px; border: 1px solid transparent; border-radius: var(--radius-sm);
  background: transparent; color: var(--text); font: inherit; text-align: left; cursor: pointer;
  transition: background var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.cs-opt:hover:not(:disabled) { background: var(--bg-hover); }
.cs-opt.on { background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 35%, transparent); }
.cs-opt:focus-visible { outline: none; box-shadow: var(--ring); }
.cs-opt:disabled { cursor: default; }
.dot {
  flex: none; width: 12px; height: 12px; border-radius: 50%;
  border: 1.5px solid var(--border-strong); background: transparent;
}
.dot.on { border-color: var(--accent); background: radial-gradient(circle at center, var(--accent) 0 45%, transparent 50%); }
.cs-main { display: flex; flex-direction: column; gap: 1px; flex: 1 1 auto; min-width: 0; }
.cs-name {
  display: flex; align-items: center; gap: 5px; min-width: 0;
  font-size: calc(12px * var(--ts, 1)); font-weight: 600;
}
.cs-sub { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cs-sub.warn { color: var(--warn); }
.cs-sub.mono { font-family: var(--mono); }
.cs-flag {
  flex: none; padding: 0 5px; height: 15px; display: inline-flex; align-items: center;
  border-radius: 999px; background: var(--accent-soft); color: var(--accent-text);
  font-size: calc(10px * var(--ts, 1)); font-weight: 600;
}
.cs-status { flex: none; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }
.cs-status.ok { color: var(--ok); }
.cs-status.warn { color: var(--warn); }
.cs-status.danger { color: var(--danger); }
.cs-hint { margin: 2px 2px 0; font-size: calc(11px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint); }
</style>
