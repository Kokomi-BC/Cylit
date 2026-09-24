/**
 * 阅读器深链位置：**跳回原文**只有这一处口径。
 *
 * 为什么单独一个模块：知识库 / 笔记 / 高亮 / 批注 / RAG 来源都要"跳到某篇论文的某一段"，
 * 而锚点有**三种**，前端必须一次说清（阅读器据此做三级解析，见 `ReaderView.applyDeepLink`）：
 *
 * - `row`：正文行 id（`p001-06`）—— 精确到行；
 * - `page`：页码 —— 行 id 失效（重译后行号变了）或检索给的是解析块 id
 *   而该块没有对应正文行（公式 / 插图 / 表格）时的兜底；
 * - 检索侧给的是**解析块 id**（`attentionisa-p01-b06`），也塞进 `row`：阅读器会先按行 id
 *   直命、再用后端下发的 `block_rows` 映射把它翻成行 id，都不行才退到 `page`。
 *
 * ⚠️ 只能在**事件回调**里调（`openTab` 会改响应式状态：放进 `:to` 就是渲染死循环，
 * 知识库页已经这么炸过一次）。
 */
export function readerTarget(paperId: string, anchor?: string | null, page?: number | null): {
  name: string;
  params: Record<string, string>;
  query: Record<string, string>;
} {
  const query: Record<string, string> = {};
  const row = String(anchor ?? '').trim();
  if (row) query.row = row;
  if (page && Number(page) > 0) query.page = String(Number(page));
  return { name: 'reader', params: { paperId }, query };
}
