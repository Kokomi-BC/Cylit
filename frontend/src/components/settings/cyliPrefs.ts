/**
 * 「Cyli 偏好」这一页专用的类型与常量（**不进 `api/types.ts`**：那几个设置键还在后端并行落地，
 * 契约没冻结前不往公共类型表里加半成品字段）。
 *
 * 三个键的口径（前端只发、后端消费）：
 *   `agent_style_preset` 风格预设：default / rigorous / concise / plain / custom（取值与后端
 *                         `agent/prompts.STYLE_PRESETS` 逐一对应，拼错一个字母就是 400）
 *   `agent_style`        **只在自定义时才有意义**的那段指令（≤2000 字）
 *   `agent_reply_length` 回复长度：short / normal / detailed
 *
 * 为什么 preset 与 style 分开存：预设是"后端认得的枚举"，后端才能自己决定往系统提示词后面追加哪一段；
 * 自定义是"用户原话"。合成一个键的话，前端就得把每个预设的提示词抄一份进来 —— 后端改口径，
 * 前端不跟着改就会静默失效。
 */

/** 风格预设。`custom` 是唯一会展开输入框的那一支。 */
export type CyliStylePreset = 'default' | 'rigorous' | 'concise' | 'plain' | 'custom';

/** 回复长度三档。`normal` 是默认。 */
export type CyliReplyLength = 'short' | 'normal' | 'detailed';

export interface CyliPrefsState {
  preset: CyliStylePreset;
  /** 自定义风格的指令原文（非自定义时恒为空串）。 */
  style: string;
  length: CyliReplyLength;
}

/** `PUT /settings` 的**窄**请求体：只带真正改过的那几个键。 */
export interface CyliPrefsPatch {
  agent_style_preset?: CyliStylePreset;
  agent_style?: string;
  agent_reply_length?: CyliReplyLength;
}

/** 与后端 `agent/prompts.STYLE_MAX_CHARS` 同一个数（超了后端会截断，这里先挡在输入框上）。 */
export const PREF_STYLE_MAX = 2000;

export const PREF_STYLE_OPTIONS: { id: CyliStylePreset; label: string }[] = [
  { id: 'default', label: '默认（自然、清晰）' },
  { id: 'rigorous', label: '严谨专业' },
  { id: 'concise', label: '简洁直接' },
  { id: 'plain', label: '通俗易懂' },
  { id: 'custom', label: '自定义' },
];

export const PREF_LENGTH_OPTIONS: { id: CyliReplyLength; label: string }[] = [
  { id: 'short', label: '简短' },
  { id: 'normal', label: '适中' },
  { id: 'detailed', label: '详细' },
];

/** 出厂态：默认风格 + 适中长度 + 没有自定义指令。 */
export const PREF_DEFAULT_STATE: CyliPrefsState = { preset: 'default', style: '', length: 'normal' };

const PRESET_IDS: CyliStylePreset[] = ['default', 'rigorous', 'concise', 'plain', 'custom'];
const LENGTH_IDS: CyliReplyLength[] = ['short', 'normal', 'detailed'];

/** 认不出的值一律回落到 `fallback`。
 *  这条是**降级路径的一半**：后端还没接上这两个键时 `GET /settings` 里没有它们，
 *  页面必须照常渲染成"默认 / 适中"，而不是弹错或摆一个「不可用」的假状态。 */
export function normalizeStylePreset(raw: unknown, fallback: CyliStylePreset = 'default'): CyliStylePreset {
  const v = String(raw ?? '').trim() as CyliStylePreset;
  return PRESET_IDS.includes(v) ? v : fallback;
}

export function normalizeReplyLength(raw: unknown, fallback: CyliReplyLength = 'normal'): CyliReplyLength {
  const v = String(raw ?? '').trim() as CyliReplyLength;
  return LENGTH_IDS.includes(v) ? v : fallback;
}

/** 写设置时后端"不认识这个键"的那一类失败（400 校验 / 404 路由或白名单里没有它）。
 *  降级路径的另一半：调用方据此说"后端还没接上该设置项"并把界面退回保存前那一份。 */
export function isUnsupportedSettingError(err: unknown): boolean {
  const status = Number((err as { status?: unknown } | null)?.status ?? 0);
  return status === 400 || status === 404 || status === 405;
}

/**
 * 从一个技能 zip 里取出 `SKILL.md` 的正文。
 *
 * 为什么要在前端解压：`GET /agent/skills/{name}/download` 回的是**整个技能包的 zip**
 * （见 `services/agent/skills_store.export_zip`，ZIP_DEFLATED），拿到的不是 markdown 文本。
 * 而"查看说明"要展示的就是 SKILL.md 原文 —— 为了它新增一个后端接口不值得，
 * 用浏览器自带的 `DecompressionStream('deflate-raw')` 解一层就够，不引依赖。
 *
 * 只读 zip 的中央目录（可信的那一份），本地头里只重读名字/扩展长度 ——
 * 那些长度与中央目录**允许不相等**，直接拿中央目录的去算偏移会读错字节。
 */
export async function readSkillMdFromZip(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const eocd = findEndOfCentralDirectory(view);
  if (eocd < 0) throw new Error('技能包不是有效的 zip');
  const count = view.getUint16(eocd + 10, true);
  let at = view.getUint32(eocd + 16, true);
  const decoder = new TextDecoder('utf-8');
  for (let i = 0; i < count; i += 1) {
    if (view.getUint32(at, true) !== 0x02014b50) break;
    const method = view.getUint16(at + 10, true);
    const compSize = view.getUint32(at + 20, true);
    const nameLen = view.getUint16(at + 28, true);
    const extraLen = view.getUint16(at + 30, true);
    const commentLen = view.getUint16(at + 32, true);
    const localAt = view.getUint32(at + 42, true);
    const name = decoder.decode(bytes.subarray(at + 46, at + 46 + nameLen));
    at += 46 + nameLen + extraLen + commentLen;
    if (!/(^|\/)SKILL\.md$/.test(name)) continue;
    const localNameLen = view.getUint16(localAt + 26, true);
    const localExtraLen = view.getUint16(localAt + 28, true);
    const start = localAt + 30 + localNameLen + localExtraLen;
    const raw = bytes.subarray(start, start + compSize);
    if (method === 0) return decoder.decode(raw);
    if (method !== 8) throw new Error(`技能包用了不认识的压缩方式（${method}）`);
    return inflateRaw(raw);
  }
  throw new Error('技能包里没有 SKILL.md');
}

/** 中央目录结尾记录（EOCD）的长度是 22 + 注释，注释上限 65535 —— 从尾巴往回找它的签名。 */
function findEndOfCentralDirectory(view: DataView): number {
  const least = Math.max(0, view.byteLength - 22 - 0xffff);
  for (let at = view.byteLength - 22; at >= least; at -= 1) {
    if (view.getUint32(at, true) === 0x06054b50) return at;
  }
  return -1;
}

type RawInflateCtor = new (format: 'deflate-raw') => ReadableWritablePair<Uint8Array, BufferSource>;

async function inflateRaw(raw: Uint8Array): Promise<string> {
  const Ctor = (globalThis as unknown as { DecompressionStream?: RawInflateCtor }).DecompressionStream;
  if (!Ctor) throw new Error('这个浏览器不支持解压技能包，请改用下载查看');
  // 这段字节就是普通内存（不是 SharedArrayBuffer），但 DataView 切出来的子数组类型标的是 ArrayBufferLike，
  // 而 Blob 只收 ArrayBuffer 支撑的那一份 —— 类型上转一次即可，不做拷贝。
  const stream = new Blob([raw as unknown as BlobPart]).stream()
    .pipeThrough(new Ctor('deflate-raw'));
  return new Response(stream).text();
}
