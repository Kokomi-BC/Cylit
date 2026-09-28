# Cylit 前端（Vite + Vue 3 + TypeScript）

> 规划依据：`ParperMind/智能论文阅读与知识管理系统_总体规划_v3_合并版.md` §22（前端七个页面）、§3.1 A2（三视图）
> 后端契约：`docs/API.md` ｜ 进度：`docs/进度.md` ｜ 踩坑：`docs/dev-log.md`

## 1. 构建与运行

```powershell
cd frontend
npm install          # 首次（本机 npm 已配置 npmmirror 镜像）
npm run build        # 产物 → frontend/dist

cd ..                # 回到项目根
python cli/pr.py serve --port 8710
# 浏览器打开 http://127.0.0.1:8710/app/     （http://127.0.0.1:8710/ 会 307 跳过去）
```

开发模式（热更新，不用每次 build）：

```powershell
cd frontend
npm run dev          # http://127.0.0.1:5173/app/
```

Vite 已把 `/api`、`/papers`、`/health` 代理到 `127.0.0.1:8710`，
所以开发期拿的是**真实后端数据**；后端地址不同就设 `PR_BACKEND=http://host:port`。

自检（静态资源 + 接口 + **真实浏览器渲染每个路由**）：

```powershell
python scripts/frontend_check.py --block-markers
# → data/logs/frontend_check.json，退出码 0 = 全通过
# 36 项 = 静态资源 17 + 打包产物 3 + 12 个路由真实渲染 + 4 个阅读器形态/整屏布局断言
# 当前为 41 项（路由用例已增至 14 个，含「原 PDF」真 PDF / 扫描件位图两个用例）
```

`--block-markers` 另外加一轮"本轮新增可视元素"的检查（不带这个参数时项数与语义与之前完全一致）：

| 分区 | 断言 |
|---|---|
| ⑫ 导入弹窗 | 三个「切换开关」都在、里面是**真实 `checkbox` + `role=switch` + `sr-only`**、轨道 36×20 / 滑块 16 / 140ms、点文字（`<label>`）能开能关、开态换成 `--accent`、隐藏 checkbox **可聚焦**（键盘可达）、解析引擎是互斥二选一、**选 MinerU 后那三个本地开关整块不再渲染**（不是置灰）、选 MinerU 才有「在线解析参数」区（模型可选 `pipeline`/`vlm`、OCR 语言、强制 OCR / 识别公式 / 识别表格三个开关且默认 `关·开·开`、页码范围带可照抄的示例）、没配 token 时给"去哪配"的提示（**已配 token 时该提示必须消失**） |
| ⑬ 设置页 MinerU 分区 | `#mineru` 分区与药丸导航项、token 是 `password` 且不回显、保存 / 验证 / 清除三个按钮、模型版本 / 语言 / 默认引擎三个下拉、token 文件路径与 90 天提示、**忙碌态**（保存中 / 验证中变文案且禁用） |
| ⑭ `/mineru/*` 契约 | `GET /mineru/config` 的字段形状、`PUT` 回包、`POST /mineru/verify` **HTTP 恒 200 + `ok`/`state`** |
| ⑮ 公式面板 | 挑一篇"公式来自原文兜底"的论文（`/formulas` 的 `source=parse/mixed`）真开阅读器：**面板里有条目**、没有空态文案、兜底条目带「解析」徽章、页签角标 = 条目数、筛选条"全部 N"自洽 —— 对应"未翻译的论文公式面板不再为空"这条口径 |

当前为 **129/129 全绿**（后端已上线 + 已配真 token 的环境）。

三条**自检自身的约定**（下次别再踩）：

- **改了机器状态要还原**：⑬ 要用真实点击「保存」验忙碌态，因此会写设置表 —— 跑完自动把
  `engine_default` / `model_version` / `language` 快照 PUT 回去（只回写设置项，**不碰 token**），
  并打印 `[INFO] 已把自检改动的设置还原：…`。
- **"含大图的论文"要挑横向的**：`find_big_figure_paper` 除面积（≥25%）外还要求宽高比 ≥1.2 ——
  竖长图受高度上限约束、卡片宽度天然上不去，拿它测"允许显示更大"必然假红。
- **切换引擎后要重新查 DOM**：旧数组里还留着已卸载的节点，会把"已隐藏"误判成"还在"；
  统计 `.switch-row` 时还要排除在线参数区里的开关（那也是 `.switch-row`）。

跑这个脚本时**不要**在 GBK 控制台上直接 `python`：章节标题里的 `⑪` 不在 GBK 里，
`print` 会抛 `UnicodeEncodeError`、自检停在第十一节（脚本已把 stdout/stderr 重配成 UTF-8 兜底）。

## 2. 技术选型

| 项 | 选择 | 说明 |
|---|---|---|
| 构建 | **Vite 6** | `base=/app/`，产物 `dist/`，由后端同进程托管 |
| 框架 | **Vue 3.5 + `<script setup>` + TypeScript** | 规划原写 React + TS；经确认改为 Vue（**框架不受限**），因为已有实现可整体迁移、风险最低 |
| 路由 | vue-router 4（history 模式） | 后端对 `/app/*` 做 SPA 回退，刷新任意路由不 404 |
| Markdown | marked + marked-katex-extension + katex | 公式在 tokenizer 阶段接管（见 §4） |
| 原 PDF | **pdfjs-dist**（本地依赖，按路由懒加载） | 阅读器左栏的「原 PDF」用 pdf.js 渲染矢量页 + 文字层（可选中/复制）；扫描页回退后端位图页图（见 §4.9） |
| 状态 | `reactive` 全局 store + composables | 规模不大，不引 Pinia |
| 样式 | 设计令牌 + `base.css` + 组件 `<style scoped>` | 组件私有样式随组件走，不再堆在一个大 CSS 里 |

> 上一轮锚定的"零构建（runtime 模板字符串）"方案已被本轮取代：
> 无构建时样式只能全局、类型缺失、依赖只能手工 vendor、也没有 HMR。
> 本轮的取舍见 `docs/dev-log.md`。

## 3. 目录

```
frontend/
├─ index.html            入口（开发期引 /src/main.ts，构建后引 /app/assets/*）
├─ vite.config.ts        base=/app/ · 手动分包(vue/markdown) · dev 代理到 8710
├─ tsconfig.json         strict + paths(@/*)，vue-tsc 做类型检查
├─ dist/                 构建产物（**不入版本库**：文件名带哈希，按需 `npm run build` 生成）
└─ src/
   ├─ main.ts            建应用、挂路由、全局错误提示、引入 KaTeX 样式
   ├─ App.vue            头部 + <RouterView> + Toast；阅读器用通栏布局
   ├─ api/
   │  ├─ client.ts       REST 客户端（拆 {data,error,trace_id} 信封、ApiError、assetUrl）
   │  └─ types.ts        后端数据模型（Paper / PageRow / PageTranslation / FormulaCard…）
   ├─ stores/app.ts      跨页缓存 + toast + 共享 loader
   ├─ router/index.ts    7 条路由（library/reader/knowledge/notes/models/tasks/settings）
   ├─ composables/
   │  ├─ useMarkdown.ts  marked+KaTeX（带缓存）+ 高亮注入
   │  ├─ usePageRows.ts  行模型：页级翻译产物 + 解析块 → 逐页行列表（渲染/搜索/目录共用）
   │  ├─ useEventStream.ts  SSE 封装：断线降级轮询、`?nosse=1` 强制降级、卸载自动清理
   │  └─ useHotkeys.ts   阅读器快捷键
   ├─ utils/format.ts    日期/百分比/状态文案/防抖节流/剪贴板/目录树
   ├─ styles/            tokens.css（WinUI 3 设计令牌）+ base.css（Fluent 控件）
   ├─ components/        AppHeader · AppIcon(全站唯一 SVG 图标) · ToastHost · Modal
   │  └─ reader/         ReaderPageRows（正文：单一滚动容器的逐行对照）· ReaderRow
   │                     · ReaderToc（中英双语目录）· ReaderSidePanel（侧栏内容区）
   │                     · ReaderFormulaList（从 Markdown 提取的公式，只读）· SelectionMenu
   └─ views/             LibraryView · ReaderView · KnowledgeView
                         · NotesView · ModelsView · TasksView · SettingsView
```

## 4. 三个必须知道的坑（都已在 `docs/dev-log.md` 记录）

### 4.1 公式必须先于 Markdown 解析被接管

事实源里有两类文本：原生解析块（纯文本）与扫描页块（模型返回的 Markdown，含 `$…$` / `$$…$$`）。
若先交给 marked 再补渲染公式，`\{0,1\}^{b \cdot d}` 的反斜杠会被 marked 的转义规则吃掉、`_` 会被当强调。
所以用 **marked-katex-extension** 在 tokenizer 阶段接管公式（`nonStandard: true` 支持单 `$`），
并把 marked 的 `html` renderer 覆写成转义（论文正文里的 `<` 不能变成 HTML 注入）。

**但扩展的 block 规则只认「`$$` 单独占一行」**：
`/^(\${1,2})\n((?:\\[^]|[^\\])+?)\n\1(?:\n|$)/`。模型写的是 `$$\begin{aligned}` 这种
"定界符与内容同行"的多行公式，行内规则又排除换行（`[^\\\n]`），于是整块公式会掉成普通段落 ——
前端看到原始 LaTeX，`_` 还会被渲染成 `<em>`（实测 TurboQuant p019-08 的三行 aligned）。
`useMarkdown.ts` 因此在交给 marked **之前**自己把 `$$…$$`（可跨行、定界符可与内容同行）
抽成 `%%BLKMATH<i>%%` 占位符（跳过代码围栏），解析完再用 `katex.renderToString(displayMode)`
替换回去；行内 `$…$` 仍交给扩展。`scripts/frontend_check.py` 有回归用例
（`reader-多行公式`：断言 `katex-display` 出现、DOM 里不再有 `$$`）。

### 4.2 「零构建」时模板属性里的 `&&` 会被当字符实体（本轮已不复现，留档）

runtime 模板编译器按 HTML 解析模板字符串，属性值里的裸 `&&` 会被实体解析破坏 →
**条件恒为假、元素不渲染、不报错**。实测：`v-if="a && b"` ❌ / `a &amp;&amp; b` ✅ / `a || b` ✅。
SFC 由 `@vue/compiler-sfc` 编译，**不再有这个问题**；但"编译不报错却白屏/布局被撑破"这类问题仍需
`scripts/frontend_check.py` 的真实渲染 + 整屏布局断言来兜（它就是为此写的）。

### 4.3 `data-*` 与 `strictTemplates`

阅读器用 `data-row-id` / `data-page` / `data-side` 做滚动跟踪与划词归属标记。
开启 `vueCompilerOptions.strictTemplates` 时 vue-tsc 会报这些 `data-*` 属性不存在
（原生元素属性类型没有索引签名），而给 `@vue/runtime-dom` 打补丁会连带打坏 `vue` 的类型导出。
因此 `npm run typecheck` 用默认档；模板正确性交给浏览器渲染自检。

### 4.4 目录不能按字号猜，也不能只用一种语言

上一版目录是按块的字号/编号在前端猜层级，结果把算法伪代码行（`3: Generate a random
projection matrix …`、`6: r ← x − DEQUANT(idx)`）和表格数值行（`1645.2945.16`）都当成了标题，
38 条里有一半是噪声。现在目录**直接来自页级翻译产物里的标题行**（`pages.json` 的
`type=heading` 行）：模型在同一次调用里产出英文原文标题与中文译文标题，因此目录天然
**中英双语**（英文在上、中文在下），层级就是模型给出的 `#/##/###`。
没有页级产物的论文退回解析阶段的标题索引（只有英文，`text_zh` 为空）。
后端 `build_outline` 仍然处理编号断链（不会把 `0.1` 挂到 `ABSTRACT` 下）。

### 4.5 对照为什么改成"一个滚动容器"，以及怎么做到真的对齐

**旧做法的问题**：左右两栏各自滚动，再用 `useScrollSync` 以 `[data-page]` 为锚点联动。
页内长短差异让"原文第 3 段"与"译文第 3 段"错位，用户看到的就是"根本没对齐"；
根因是两栏内容来自**两次不同的产出**（PDF 抽取 vs 模型整页译文），行数本来就不一致。

**现在的做法**：翻译就是页级一次调用，同一行里的原文与译文**出自同一次产出**，
块序号一一对应（`align=exact`）。前端因此只需要一个滚动容器：

| 左栏呈现 | 布局 | 对齐粒度 |
|---|---|---|
| `原文文本`（默认） | 一行两列：左原文 / 右译文（`.pg-rows.two-col` 网格） | **逐段**（天然对齐，无联动脚本） |
| `原 PDF` | 每页一行的两列：左页图（页内 `position: sticky`）/ 右该页译文行 | **逐页**（页图跟着该页译文走，不会跑到别的页） |

两侧只有**字体族**不同（英文衬线 / 中文无衬线），字号与行距完全一致，所以对照时两侧观感一致。
正文字号由工具栏的 **小 / 中 / 大**（默认**小** = 13px）统一驱动：`ReaderPageRows` 把
`--reader-font-size` 传给 `.rows-wrap`，行内正文/标题/图注/公式全部用 `em` 相对缩放；
选择记在 `localStorage`（纯展示偏好，不进服务端进度）。

已删除：`useScrollSync.ts`、`ReaderBlock.vue`、`ReaderCompareRows.vue`、
`ReaderOriginalPane.vue`、`ReaderTranslationPane.vue`。
滚动跟踪（当前段高亮 / 进度持久化）改为**偏移缓存 + 二分查找**：内容变化时测一次
`[data-row-id]` 的绝对偏移，滚动时零布局读取，`scrollHeight` 变了自动重测。

### 4.6 插图按论文的排法显示（通栏、图在上图注在下、跟随图注锚定）

正文里的插图**不是**"塞在某一侧的缩略图"，而是按论文自己的排法渲染：

| 项 | 做法 |
|---|---|
| 位置 | 挂在该图**图注行的上方**（论文里图在上、图注在下），对照模式下 `grid-column: 1 / -1` **跨两列通栏居中** |
| 裁剪 | 后端 `figure_regions.page_regions` **按图注/标题锚定**收紧 bbox（`GET /api/v1/papers/{id}/figures/region/{page}/{i}`，600dpi 按需渲染 + 落盘缓存，文件名带 bbox 哈希） |
| 视图 | 原文 / 译文 / **对照**三种文本视图都显示；**「左 PDF / 右译文」模式也显示**（挂在译文列的图注上方——页图缩放后往往看不清图内数字），只有"纯原 PDF"单栏不显示（那一种整页就是页图，没有文本行可挂） |
| 交互 | 点击打开大图（`zoom-in` 光标 + `Modal`）；窗口过窄时单列堆叠 |

> 页图模式（`left=page`）里的**文字列永远是译文**：另一列就是原 PDF 页图（即原文），
> 早期实现让文字列跟着 `pairOrder` 走，导致「译文在左」时文字列也放原文 —— 原文出现两次、
> 译文消失（已修）。

> 为什么不用解析阶段切的 `figures/*.png`：那些裁图会把相邻的另一张图甚至中间的小标题一起裁进来
> ——TurboQuant p8 的 Figure 3 裁图里同时含 Figure 2 与 `2.2 NEEDLE-IN-A-HAYSTACK` 标题，
> 前端于是出现"同一张图显示两次、图里夹着正文"。现在按「图注上方最近的标题/图注」收紧边界，
> 实测 p8 Figure 3 的 bbox 从 `[95.8, 535.0]` 收到 `[304.6, 541.6]`（排除 Figure 2 与标题）。
> 回归测试见 `tests/test_figure_regions.py`。
>
> 表格不贴图：页级翻译已按提示词把表格**逐格转录成 Markdown 表格**（数字照抄、表头翻译），
> 贴图会与文字表格重复；表注行照常显示。
> **表格按"通栏"渲染**：半栏放不下 9 列数字（`45.29` 会被折成两行），所以 `type=table` 的行
> 用 `grid-column: 1 / -1` 跨两列、两侧上下排（原文表在上、译文表在下），单元格 `white-space: nowrap`
> ＋数字右对齐，放不下时横向滚动——与论文里通栏表格的读法一致。

### 4.7 Markdown 本体里也要有图（"Markdown 视图"）
模型产出的两份 Markdown **没有任何 `![]()` 图片语法**（它只写图注），所以任何 Markdown 渲染器
都看不到插图。后端 `reader_service.bilingual_markdown()` 在**读取时**按图号把插图行插到对应
图注上方（`figure_regions.inject_images`，模型自己给了 `![` 就不动），于是：

| 通道 | 图片引用形式 |
|---|---|
| `GET /api/v1/papers/{id}/markdown/translated`（视图/应用内） | `/api/v1/papers/<id>/figures/region/<page>/<i>`（浏览器可直接取） |
| 落盘 `data/papers/<id>/md/translated.md` | `../figures/region-…png`（在 md/ 目录下用 Typora/VS Code 打开即可见） |
| 阅读器工具栏「Markdown」按钮 → 下载 | 引用补成 `http://127.0.0.1:8710/api/v1/…`，任何编辑器都能显示 |

阅读器工具栏最右的文档图标打开 **Markdown 视图**：`上下对照 / 原文 / 译文` 三态 + 逐页切换 +
一键下载整篇（深链 `?md=1&mdpage=8`）。「上下对照」= 两份**完整** Markdown（各自都带图，
读译文那一半也能看到插图）。

### 4.8 左右栏合并成一栏；公式只读（从 Markdown 提取）

- **侧栏合并**：原来左栏=目录、右栏=批注/笔记/公式/插图/术语/任务，现在**只有一栏**
  （`.reader { grid-template-columns: 300px minmax(0, 1fr) }`），顶部一排页签
  **图标 + 文字**（只用图标不直观）切换
  `目录 / 批注·笔记 / 公式 / 插图 / 术语 / 任务`（带计数，窄栏自动换行）。
  **批注与笔记已合并成一个页签**：按时间倒序的信息流，每条用徽章区分
  `划词解释 / 批注 / 笔记`，都可定位、可删除（`DELETE /notes/{id}` 与 `DELETE /annotations/{id}`）。
  正文因此宽了 340px —— 对照时两侧都更好读、插图与通栏表格也不再挤。
  窄屏（<1100px）用工具栏的侧栏按钮弹出同一栏。深链 `?side=formulas`
  （`?side=notes` 兼容映射到 `annotations`）。
- **公式只读**：公式列表**直接从页级产物的 Markdown 里提取**
  （`GET /papers/{id}/formulas` → `source=markdown`，行间 31 / 行内 313：单字符变量这类噪声会过滤掉），
  展示 KaTeX、可复制 LaTeX、可定位到正文行。**不再有复核**：不分级、不比对裁图、没有"采纳/修正/定稿"，
  解析阶段的公式卡只在 `?source=parse` 时返回（解析质检参考）。
- **复核模式已整体移除**（前后端）：`ReviewView.vue`、`FormulaCard.vue`、路由 `/review/:paperId`、
  `GET /papers/{id}/review`、`PUT /review/{reviewId}`、`reader.reviews` / `reviews_open`、
  `paper.review_open`（含 MySQL 列与 `review` 表）全部删除。解析阶段的一致性检查与插图体检仍做，
  只落进质量报告，不再进人力队列。

### 4.9 左栏「原 PDF」是真 PDF（pdf.js），不是位图

之前左栏挂的是 `/api/v1/papers/{id}/pages/{n}` 返回的 **150dpi PNG**：**选不中、搜不到、复制不了**，
放大还发虚。现在交给 `PdfPage.vue`（**pdfjs-dist**）按页渲染：

- `canvas` 出矢量图（按 `宽度 × dpr`，`dpr ≤ 2`）+ `TextLayer` 出**真文字**（可选中/复制/Ctrl+F）；
- **同一篇只加载一次 PDF**（模块级 Promise 缓存），22 页共用一份 document；
- **进入视口附近才渲染**（`IntersectionObserver`，`rootMargin: 900px`）：实测 22 个 canvas 里只有 2 个真出图；
- 渲染前用 `PageInfo.width/height`（PDF 点）撑 `aspect-ratio`，避免"先扁后弹"；
- 渲染失败 → 自动回退后端位图页图（宁可退回位图也不开天窗）。

**分流**：`PageInfo.has_text_layer === true` → pdf.js；扫描页（`false`）继续用位图 ——
扫描件的 PDF 里就是一整页图，走 pdf.js 白搭内存；未解析/拿不到页信息也回退位图。

**划词**：选区落在 `[data-pdf-page]` 里时进 `anchored: false`，
浮层只留「AI 解释 / 复制」（高亮与笔记靠 `block_id + 字符偏移`，PDF 文字映射不回去），
解释弹窗也不再提供"存为批注"。后端 `explain_selection` 对 `block_id` 本来就是可选的，**后端零改动**。
**页上不挂任何点击行为**（点一下不该弹东西；要开原生阅读器用工具栏的「打开原 PDF」）。
**文字层的 CSS 照官方抄，别手写最小集**：pdf.js v6 用
`font-size = --total-scale-factor × --min-font-size × --font-height`，
span 上再 `scale(1/--min-font-size)` 抵消。`--font-height` 是**页面单位**（实测 10pt 正文→ 9.96），
**不随视口缩放**。只写"透明 + 绝对定位"、漏掉 `font-size`，隐形文字就会用继承字号 →
**选中的框与看到的文字对不上**（用户报过）。`--min-font-size` / `--min-font-size-inv` 缺一个也会整体缩放。

**出图与显示解耦（性能关键）**：页面内容放在固定尺寸的 `.pdf-surface` 里，
外层只用 `transform: scale(k)` 跟随容器宽度 —— 开合侧栏/拖页宽滑杆**不重新光栅化**。
重出只在"实时几何还在视口附近 + 倍率偏离 >15%"时防抖 450ms 补一次。
★ 不要用"曾经可见"的粘性标志做重出判据：滚过全篇后它会让 22 页全算可见，
一次侧栏开合就把全篇重画（实测卡顿就是这么来的）。宿主元素上的 `data-renders` 是出图计数器，便于排查。

**四个已经踩过的坑**（都实测过）：

1. **渲染宽度只能来自容器 `clientWidth`**：入参是"期望宽度"，外框（`.media-box` 带 `max-width:100%`）
   可能把它夹窄 —— 按入参出图会比外框宽，被 `overflow:hidden` 裁掉，看起来就是"页面偏右、右边被切"。
2. **首次进入视口时容器可能还没排版**（`clientWidth = 0`）。**不要用 `requestAnimationFrame` 重试**：
   隐藏/未聚焦标签页里 rAF 被节流到几乎不跑。改成依赖 `ResizeObserver` + 定时器 + `visibilitychange` 三道。
3. **隐藏标签页里 IO / RO / rAF 全都不回调**（`document.visibilityState === 'hidden'`）：
   pdf.js 的**分片渲染靠 rAF 推进**，于是会停在"画了一半、promise 永不 resolve"。
   已加一个仅隐藏期生效的 rAF 垫片（隐藏时 = `setTimeout`，可见即还原）。
4. **文字层要显式 `z-index`**：canvas 会被浏览器提升为独立合成层，光靠 DOM 顺序不保证文字层在上，
   否则**选中底色根本看不到**。
5. **往下拖进空白区要把焦点拉回真文字**：文字层只管"有字的地方"，页面下部空白不属于任何文本节点。
   实测（真实拖拽 + 逐事件记录）Chrome 一进空白就把 `focus` 给**文字层容器**（offset = 子节点序号）：
   offset 在 0 时选区**倒长到页首**，递增时**一路覆盖到层末**（实测 131 字 → 3176 字）。
   所以：每次 `selectionchange` 只把"与本层同一个 `.pdf-text-layer` 里的文本节点"视为有效焦点并记为 `lastGood`，
   否则（容器／别的层／别的栏）用 `setBaseAndExtent` 把焦点拉回 `lastGood` —— 选区就"只铺到已选中的地方"。
   占位元素 `.endOfContent` 保留，但**盒子只对齐当前选区的外接矩形**（JS 写 `top`/`height`），
   `::selection` 置透明；它永远待在层末，别插到锤点旁边（曾因此被塞进 `.pdf-surface`，变成静态整页大块）。
   ★ 不要用"铺满整页的安全港"，也不要在 `pointerup` 里复位 DOM —— 后者会让浏览器重新归一化，
   实测把刚选好的文字直接收掉（选区变空、浮层不出）。

**划词选中色全站唯一来源**：`tokens.css` 的 `--selection-bg` + `base.css` 的全局 `::selection`；
`PdfPage.vue` 引用同一令牌（并保持 `color: transparent`，字形仍由画布提供）。

**为什么不用浏览器原生阅读器**：每页一个 `<iframe>` ＝ 每页一个 PDF 插件实例（内存吃不消）；
单实例整篇 `<iframe>` 与"每页贴在该页译文旁"的对照布局冲突，换页靠 `#page=` 重载还会闪。
想要原生体验时用工具栏「打开原 PDF」（`Content-Disposition: inline` 直接开原生阅读器）。

### 4.6 CSS Grid 只写 columns 不写 rows → 侧栏不滚、整页滚

实测数据（iframe 内 1600×1000 视口）：`.reader` 容器 948px，但 `.reader-content` 长到 **1372px**
（`.reader-panel` 也是 1372）——因为 `grid-template-rows` 没写，内容行按 max-content 撑开，
侧栏自身永远不滚，滚动条落在 `<html>` 上。修法三件套：

```css
.reader { grid-template-rows: auto minmax(0, 1fr); overflow: hidden; }
.reader-aside, .reader-panel, .reader-content { min-width: 0; min-height: 0; }
.app-main.wide { height: calc(100vh - var(--header-h)); overflow: hidden; }
```

修复后自检 §③ 断言三种阅读模式都满足：`docScroll == viewport`（整页不滚）、
侧栏底边不出视口、无越界元素。

## 5. 已实现能力（阶段 09 验收映射）

| 规划验收 | 实现位置 | 说明 |
|---|---|---|
| A2 三视图 | `views/ReaderView.vue` + `components/reader/*` | 原文（**原文文本** / **原 PDF 页图**）/ 译文 / **对照** |
| 对照·原文文本（默认） | `ReaderPageRows.vue` + `ReaderRow.vue` | 一行一对「原文 / 译文」（页级翻译同一次产出，天然逐段对齐）；**单一滚动容器**，没有联动脚本；窄屏自动上下堆叠 |
| 对照·原 PDF | `ReaderPageRows.vue`（`originalMode=page`） | 每页页图贴在该页译文左侧（页内 `position: sticky`），可缩放；按**页**对齐 |
| 目录（中英双语） | `ReaderToc.vue` + 后端 `GET /papers/{id}/outline` | 条目来自页级产物的标题行：**英文在上、中文在下**，可折叠、带编号与页码；编号断链按顶级处理 |
| A2-1 切模式位置不跳变 | `setViewMode` / `setPairOrder` / `setOriginalMode` → `focusRow` | 记住 `currentRowId`，切换后回到同一段 |
| A2-2 原文译文同屏 | 逐行网格 / 页图 + 该页译文 | 不做文本相似度匹配；点目录项或任一侧都会定位 |
| A2-3 顺序切换配对不变 | `pairOrder` | 原文在左 / 译文在左只是两侧位置 |
| A2-4 模式与顺序被记住 | `PUT /progress` + 载入恢复 | 服务端权威：`view_mode`/`pair_order`/`last_block_id`/`scroll_ratio`；`?view=&order=&left=` 可覆盖（分享/自检用） |
| A2-5 窄屏退化 | `ReaderView` scoped `@media (max-width:1100px)` | <1100px 对照退化为单栏（行内上下堆叠）；页图宽度跟随容器，无横向滚动（自检用 900×900 断言） |
| **整屏布局（不整页滚动）** | `.reader { grid-template-rows: auto minmax(0,1fr) }` + 各栏 `min-*: 0` + `.app-main.wide { height; overflow:hidden }` | 滚动只发生在正文栏内；自检 §③ 对三种阅读模式断言"整页不滚动、侧栏不出视口" |
| A1 逐页流式 | `useEventStream` + `applyPage` | SSE `page_translated` → **只替换那一页的行**；断线降级轮询（`/translations?since_page=`） |
| 页级进度 | 右栏进度卡 + 工具栏徽章 | `pages_done / pages` + 成对行数 + 当前模型（唯一粒度是页） |
| A11 划词解释 | `SelectionMenu` → `/annotations/from-selection` | 可"存为批注"；高亮按 `row_id + 偏移` 锚定并可点击删除 |
| A12 笔记/高亮/批注 | 阅读器右栏 + `/notes` | 增删改查 + 跳回原文 |
| A13 模型管理 | `/models` | 列表 / 测试 / 设默认 / 预设导入 / 密钥写入（只列文件名、不回显） |
| 任务可控 | `/tasks` | 进度 / 暂停 / 恢复 / 取消 / 重试 / 日志（SSE 实时，降级轮询） |
| **WinUI 3 外观** | `styles/tokens.css` + `base.css` + `AppIcon.vue` | Fluent 令牌（单支强调色 #0078D4 / 深色 #60CDFF，无紫色、无渐变）、控件高 32/圆角 4、**图标一律内联 SVG**（无 emoji、无字符图标） |

**深链**：`/app/reader/<paperId>?row=<rowId>&view=bilingual&left=page&order=en_left`
（`row` 定位到某一段，兼容旧参数 `block`；`view` 视图、`left` 左栏呈现、`order` 左右顺序、`nosse=1` 强制轮询降级）。

**快捷键**：`1/2/3` 切视图 · `o` 换左右 · `p` 切原文文本/原 PDF · `j/k` 上下段 · `/` 搜索 · `Esc` 关浮层。

**明确的未实现提示**（不静默失败）：`POST /rag/query` 返回 `implemented:false` 时，`/knowledge`
页面会显式写明"RAG 问答尚未实现（阶段 14–17）"并给出后端原文，不伪造答案与来源。

## 6. 代码量现状（诚实记录）

| 项 | 行数 |
|---|---|
| `src/**/*.{ts,vue}` | 约 6,600（本轮删掉 5 个旧组件 + `useScrollSync`，新增 `usePageRows` / `ReaderPageRows` / `ReaderRow` / `AppIcon`） |
| `src/styles/*.css` | 约 320（WinUI 3 令牌 + Fluent 控件） |

迁移前的"零构建"版本视图层是 8 个 `.js` 共 211 KB（≈5.5 千行）+ `app.css` 20 KB。
**结论：行数基本持平，没有明显下降**——省下的是全局 CSS 与手工 vendor，增加的是类型定义、
更完整的加载/失败/空状态呈现、以及组件拆分。真正的收益是工程能力：类型检查、HMR、
路由级代码分割（每个视图独立 chunk）、可以随时引第三方库、样式随组件走。

若下一步想**真的减少行数**，建议按这个顺序抽公共件（当前各视图重复度最高处）：

1. `PageShell.vue`（页头 + 工具栏 + 加载/失败/空三态）——8 个视图都在重复这段；
2. `useAsyncList.ts`（列表加载 + 错误 + 空态 + 刷新）；
3. `ConfirmButton.vue`（`window.confirm` + busy 态）——目前在 6 个视图里各写一遍。
