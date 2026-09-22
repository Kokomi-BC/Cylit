# PaperMind · 智能论文阅读与知识管理系统

依据《智能论文阅读与知识管理系统 —— 总体规划 v3.0（合并版）》实现的本地优先系统。
**本轮交付：解析与阅读主干 + 前端（Vite + Vue 3 + TS：阅读器与六个管理页面）。**

> **版本库约定**：仓库根就是这个目录（`paper-reader/`）。
> `data/`（论文、页图、IR、译文产物、密钥、日志）已被忽略，目录由代码自动创建；
> `frontend/dist/` 是**故意纳入**的构建产物（离线演示时 `pr.py serve` 直接可用）。
> 首次克隆后：`npm install`（前端依赖）→ 需要真实模型时把 key 放进 `data/secrets/`。

| 文档 | 内容 |
|---|---|
| [docs/README_后端.md](docs/README_后端.md) | 交付范围、目录结构、数据流、关键设计决策、验收状态 |
| [docs/API.md](docs/API.md) | 全部 API 说明与调用示例（含前端托管与 SSE 事件格式） |
| [frontend/README.md](frontend/README.md) | **前端**：构建与运行、技术选型、目录、三个必须知道的坑、验收映射 |
| [docs/进度.md](docs/进度.md) | 当前进度总览（已完成 / 未完成 / 实测数据 / 下一轮优先级） |
| [docs/dev-log.md](docs/dev-log.md) | 开发日志：阶段对照、真实踩坑与修复、实测数据 |

## 30 秒上手

```powershell
conda run -n scenemind python -m pip install -r requirements.txt
cd frontend; npm install; npm run build; cd ..   # 前端构建（产物 dist/，后端托管在 /app/）
.\scripts\start.ps1                                   # 起后端 → 前端 /app/ ｜ 接口文档 /docs
python cli/pr.py build "论文.pdf" --id my-paper        # 导入 + 解析
python cli/pr.py translate my-paper                   # 逐页翻译（未配模型时用 Mock）
python scripts/e2e_check.py                           # 35 项后端验收自检
python scripts/frontend_check.py --block-markers      # 40 项前端验收自检（真实浏览器渲染）
python -m pytest tests -q                             # 单元 + 集成测试
```

浏览器打开 **http://127.0.0.1:8710/app/** 使用三视图阅读器；
前端开发模式（HMR）：`cd frontend; npm run dev` → http://127.0.0.1:5173/app/ 。

## 一轮实测（TurboQuant, ICLR 2026, 22 页）

| 指标 | 值 |
|---|---|
| 解析耗时 | 3.5 s（含 22 页 150dpi 渲染与全部公式高倍裁图） |
| 结构化 | 480 块（段落 138 / 标题 38 / 公式 73 / 图注 8 / 插图 5 / 表格 1 / 参考 217） |
| 公式分级 | A 50 / B 17 / C 1 → **C 占比 1.5%**（目标 ≤15%） |
| **逐页翻译（真实 DeepSeek 视觉模型）** | 22 页 / **22 次调用**、186.8 s、成功 22 失败 0、**277 行全部 `exact` 对齐**；prompt 60.7k + completion 143.6k（其中推理 97.6k） |
| 扫描件（影印 PDF） | 2 页纯图像 PDF：视觉模型识别 13s（≈9.8k 字符）→ 翻译 36s，全页成功 |
| 索引 | 128 chunk（text/formula/figure/table），LanceDB 128 条向量 |
| 删除一致性对账 | issues = 0 |
| 验收脚本 | `scripts/e2e_check.py` + `scripts/frontend_check.py` |

## 最重要的五个设计点

1. **不信任 PyMuPDF 的文本块边界**：只把 span 当证据，自研「基线聚行 → 行距基线切段」，
   否则段落会被切碎、展示公式会被拆成多个块。
2. **Markdown 是内容事实源，MySQL 只存定位**（≤200 字预览）：
   阅读器读 `md/blocks.json`（解析产物）与 `md/pages.json`（翻译产物），既快又与事实源同源。
3. **块 ID 带论文前缀**（`turboquantic-p07-b12`）：MySQL `block.id` 是全局主键，
   无前缀会导致第二篇论文导入时主键冲突。
4. **翻译只有一种粒度：一页一次调用，原文与译文同一次产出**。把页图 + 解析器抽出的页内文本
   一起交给模型，它返回 `original_md`（忠实转录）+ `translation_md`（逐块对应的中文译文）。
   于是"对齐"不再需要算法：两份 Markdown 的块序号天然一一对应（`pair_rows` 只在块数不等时
   做一次 difflib 序列对齐），**没有切块、没有回退补译、没有校验拦截**。
5. **公式不做特殊处理，模型可信任**：公式在原位跟整页一起交给模型，它返回的 LaTeX 直接采用
   ——PDF 抽取出的 B 级公式本身可能是坏的（括号未闭合、命令拼错），锁死等于忠实保留错误。
6. **扫描件（影印 PDF）走同一条链路**：文本层为空时页内文本只是空参考，模型完全依据页图转录
   + 翻译；读页只转录不翻译，看不清写 `[?]` 不许猜。

## 未实现（下一轮）

RAG 检索与问答（阶段 14–17，接口占位返回 `implemented: false`）、
版面保留 PDF 产线 B（阶段 18–20，任务类型已注册并明确返回未实现）、
AI 辅助阅读 / 论文结构分析（P1）、前端三视图界面。

