# 模型 / 厂商图标（备用素材）

用户口径 2026-12-07：先把图标攒下来备用，**暂未接进界面**。

## 来源

| 文件 | 来源 |
| --- | --- |
| `vendor/deepseek.svg` `qwen.svg` `alibaba.svg` `zhipu.svg` `moonshot.svg` `minimax.svg` `siliconcloud.svg` | 千问 AI 平台模型列表页 <https://www.qianwenai.com/models> 的图标字体（`data-icon`） |
| `vendor/openai.svg` `claude.svg` `gemini.svg` | [simple-icons](https://simple-icons.org/)（CC0）15.x |
| `category/*.svg` | 同上「千问 AI 平台」页的分类筛选图标（文本 / 图像 / 视频 / 语音 / 多模态 / 向量 / 重排） |

> 该页只把**分类**图标和**国产厂商**标记做成图标字体；GPT / Claude / Gemini 不在其中，故用 simple-icons 补齐。
> 厂商标记是各家商标，只在本项目界面内做「这是哪家的模型」用，不要外发或用于宣传。

## 用法

```ts
import { vendorIcon, categoryIcon } from '@/assets/model-icons';

vendorIcon('deepseek');        // => /assets/deepseek-xxxx.svg
vendorIcon('dashscope');       // => Qwen 的标记（百炼跑的就是 Qwen）
categoryIcon('embedding');     // => 向量分类图标
```

## ⚠️ 都是**单色**标记

原 SVG 用 `currentColor` 上色。**直接 `<img>` 只会得到黑色**（SVG 被当成独立文档渲染，取不到外部颜色）。
要跟着主题色走，用 mask：

```css
.vendor-ico {
  width: 16px; height: 16px; background: currentColor;
  -webkit-mask: var(--ico) center / contain no-repeat;
  mask: var(--ico) center / contain no-repeat;
}
```

```html
<span class="vendor-ico" :style="{ '--ico': `url(${vendorIcon(p.provider)})` }" />
```
---

## 彩色标记（`vendor-color/`，用户口径 2026-12-07 三轮）

千问 AI 平台那张页上的厂商标记是**图标字体（单色）**，没有彩色版本 —— 所以彩色这套另找**官方**来源：

| 文件 | 来源 | 说明 |
| --- | --- | --- |
| `deepseek.svg` | [svgl](https://svgl.app) `library/deepseek.svg` | 官方蓝 `#4D6BFE` |
| `qwen.svg` | Qwen 官网前端资源 `…/static/images/qwen-logo.svg` | 官方 `#082DFF`；原文件是 **75×20 的横排字标**，这里 `viewBox` 裁到 `0 0 18 20` 只留图形 |
| `qwen-wordmark.svg` | 同上（未裁） | 需要横排字标时用它 |
| `zhipu.svg` | Z.ai 官方 `z-cdn.chatglm.cn/z-ai/static/logo.svg` | 蓝渐变（`#126EF4` 系） |
| `moonshot.svg` | [svgl](https://svgl.app) `library/kimi-icon.svg` | 其实是 **Kimi** 的 App 图标（月之暗面的产品），蓝 `#027aff` |
| `openai.svg` | [svgl](https://svgl.app) `library/openai.svg` | OpenAI 的品牌本来就是**黑色**，不是缺颜色 |
| `claude.svg` | [svgl](https://svgl.app) `library/claude-ai-icon.svg` | Anthropic 的 `#D97757` |
| `gemini.svg` | [svgl](https://svgl.app) `library/gemini.svg` | 官方四角星渐变 |
| `alibaba.png` | 阿里云官网 `rel=icon`（32×32） | 只有这个尺寸的官方彩色图；再大就得自己放大 |
| `minimax.ico` | MiniMax 官网 `favicon.ico` | 彩色应用图标（`.ico` 在 `<img>` 里正常显示） |

**没拿到的**：`siliconcloud`（硅基流动）在官方站点上没有可直接下载的彩色标记 —— 只用 `vendor/` 里的单色那份。

```ts
import { vendorColorIcon, QWEN_WORDMARK } from '@/assets/model-icons';

vendorColorIcon('deepseek');   // 彩色
vendorColorIcon('dashscope');  // → Qwen 的彩色标记
```

⚠️ 彩色这套**不要**做 mask（mask 只取形状、会丢掉颜色），直接 `<img>`。
⚠️ 都是各家商标，仅在本项目里标识「这是哪家的模型」，别外发或用于宣传。
