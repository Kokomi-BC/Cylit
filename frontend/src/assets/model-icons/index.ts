/**
 * 厂商 / 模型分类的图标（**备用素材**，用户口径 2026-12-07：先攒着，后面要用直接取）。
 *
 * 来源与注意点见同目录的 README.md。这里只做一件事：把「配置里的 provider / model_type」
 * 翻译成一个可以塞进 <img src> 或 CSS mask 的资源地址。
 *
 * ⚠️ 这些标记是**单色**的（页面上的 SVG 用的是 `currentColor`）：
 *   · 想让它跟着主题色走，用 CSS mask（见 README 里那段），不要直接 <img>；
 *   · 直接 <img> 只会得到黑色（SVG 被当成独立文档渲染，currentColor 取不到外部颜色）。
 */
import deepseek from './vendor/deepseek.svg';
import qwen from './vendor/qwen.svg';
import alibaba from './vendor/alibaba.svg';
import zhipu from './vendor/zhipu.svg';
import moonshot from './vendor/moonshot.svg';
import minimax from './vendor/minimax.svg';
import siliconcloud from './vendor/siliconcloud.svg';
import openai from './vendor/openai.svg';
import claude from './vendor/claude.svg';
import gemini from './vendor/gemini.svg';
import catText from './category/text.svg';
import catImage from './category/image.svg';
import catVideo from './category/video.svg';
import catAudio from './category/audio.svg';
import catMultimodal from './category/multimodal.svg';
import catVector from './category/vector.svg';
import catRerank from './category/rerank.svg';

/** 厂商 / 平台的标记 */
export const VENDOR_ICON: Record<string, string> = {
  deepseek, qwen, alibaba, zhipu, moonshot, minimax, siliconcloud, openai, claude, gemini,
};

/** 模型**能力分类**的图标（文本 / 图像 / 视频 / 语音 / 多模态 / 向量 / 重排） */
export const CATEGORY_ICON: Record<string, string> = {
  text: catText,
  image: catImage,
  video: catVideo,
  audio: catAudio,
  multimodal: catMultimodal,
  vector: catVector,
  rerank: catRerank,
};

/** 别名：后端 provider 字段的实际取值 → 图标键（`dashscope` 就是阿里云百炼，跑的是 Qwen） */
const VENDOR_ALIAS: Record<string, string> = {
  dashscope: 'qwen',
  dashscope_rag: 'qwen',
  dashscope_multimodal: 'qwen',
  aliyun: 'qwen',
  aliyun_dashscope: 'qwen',
  tongyi: 'qwen',
  bailian: 'qwen',
  glm: 'zhipu',
  'z-ai': 'zhipu',
  zhipuai: 'zhipu',
  kimi: 'moonshot',
  'gpt': 'openai',
  'openai-compatible': 'openai',
  anthropic: 'claude',
  google: 'gemini',
  googleai: 'gemini',
};

/** provider → 图标地址；认不出来就返回空串（调用方自己决定退回什么图标） */
export function vendorIcon(provider?: string | null): string {
  const raw = String(provider || '').trim().toLowerCase();
  if (!raw) return '';
  if (VENDOR_ICON[raw]) return VENDOR_ICON[raw];
  const alias = VENDOR_ALIAS[raw];
  if (alias && VENDOR_ICON[alias]) return VENDOR_ICON[alias];
  /* 前缀兜底：`deepseek-fast` / `openai_compatible` 这类带后缀的写法 */
  for (const key of Object.keys(VENDOR_ICON)) {
    if (raw.startsWith(key)) return VENDOR_ICON[key];
  }
  return '';
}

/** 后端 `model_type` → 分类图标（`vision` 有画面、`ocr` 是文档、`agent` 也是文本） */
const TYPE_TO_CATEGORY: Record<string, keyof typeof CATEGORY_ICON> = {
  generation: 'text',
  agent: 'text',
  vision: 'image',
  ocr: 'text',
  embedding: 'vector',
  reranker: 'rerank',
};

export function categoryIcon(modelType?: string | null): string {
  const key = TYPE_TO_CATEGORY[String(modelType || '')];
  return key ? CATEGORY_ICON[key] : '';
}
/* ---------------------------- 彩色标记 ----------------------------
 * 用户口径 2026-12-07 三轮：要**厂商的彩色图标**（不是模型分类图标）。
 * 站点上那些 `data-icon` 字形是单色的，所以彩色这一套另找官方来源（见 README）：
 * 深色底、SaaS 的官方 logo、以及 svgl / simple-icons 两个开源图标库。 */
import cDeepseek from './vendor-color/deepseek.svg';
import cQwen from './vendor-color/qwen.svg';
import cQwenWordmark from './vendor-color/qwen-wordmark.svg';
import cZhipu from './vendor-color/zhipu.svg';
import cMoonshot from './vendor-color/moonshot.svg';
import cOpenai from './vendor-color/openai.svg';
import cClaude from './vendor-color/claude.svg';
import cGemini from './vendor-color/gemini.svg';
import cAlibaba from './vendor-color/alibaba.png';
import cMinimax from './vendor-color/minimax.ico';
import cDoubao from './vendor-color/doubao.png';

/** 厂商**彩色**标记（原色，不跟主题走；适合做徽章/头像那种圆形小图标） */
export const VENDOR_COLOR_ICON: Record<string, string> = {
  deepseek: cDeepseek,
  qwen: cQwen,
  zhipu: cZhipu,
  moonshot: cMoonshot,
  openai: cOpenai,
  claude: cClaude,
  gemini: cGemini,
  alibaba: cAlibaba,
  minimax: cMinimax,
  doubao: cDoubao,
};

/** Qwen 的完整字标（75×20，横向）：做页头 / 介绍块时用，做 16px 图标请用 `vendorColorIcon('qwen')` */
export const QWEN_WORDMARK = cQwenWordmark;

/** provider → 彩色标记地址；同样认别名（dashscope → qwen、kimi → moonshot…），认不出返回空串 */
export function vendorColorIcon(provider?: string | null): string {
  const raw = String(provider || '').trim().toLowerCase();
  if (!raw) return '';
  if (VENDOR_COLOR_ICON[raw]) return VENDOR_COLOR_ICON[raw];
  const alias = VENDOR_COLOR_ALIAS[raw];
  if (alias && VENDOR_COLOR_ICON[alias]) return VENDOR_COLOR_ICON[alias];
  for (const key of Object.keys(VENDOR_COLOR_ICON)) {
    if (raw.startsWith(key)) return VENDOR_COLOR_ICON[key];
  }
  return '';
}

const VENDOR_COLOR_ALIAS: Record<string, string> = {
  dashscope: 'qwen',
  dashscope_rag: 'qwen',
  dashscope_multimodal: 'qwen',
  aliyun: 'qwen',
  tongyi: 'qwen',
  bailian: 'qwen',
  kimi: 'moonshot',
  moonshotai: 'moonshot',
  glm: 'zhipu',
  'z-ai': 'zhipu',
  zhipuai: 'zhipu',
  anthropic: 'claude',
  google: 'gemini',
  googlegemini: 'gemini',
  'gpt': 'openai',
  /* 豆包 = 字节的模型，走的是火山方舟（Ark）的 OpenAI 兼容端点，两种写法都认到同一个标记 */
  volcengine: 'doubao',
  ark: 'doubao',
  bytedance: 'doubao',
  volc: 'doubao',
};
