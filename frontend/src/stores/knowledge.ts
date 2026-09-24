/**
 * 知识库页的**会话内状态**：离开这个标签页再回来，内容还在。
 *
 * 为什么需要它：外壳（`App.vue`）用 `<RouterView :key="routeKey">` 给视图按"标签键"重建实例 ——
 * 切到别的页面时 `KnowledgeView` 会被**卸载**，写在组件里的 `ref`（搜索词、结果、浏览筛选、
 * 展开的全文）全部随之丢掉；切回来看到的就是一个空页面（用户报的"返回后内容被清空"）。
 *
 * 所以"这个页面在看什么"放在模块级 `reactive` 里（和 `stores/app.ts` 同一套做法）：
 * - 组件卸载不影响它，切回来直接渲染上次的结果，**不重复请求**；
 * - 只活在**当前这次页面会话**里：刷新（F5）或关掉标签页就没了 —— 那是"重新开始"，
 *   而搜索词本身已经落在 `utils/searchHistory` 的 localStorage 里，输入框一点就能重跑。
 * - 不缓存 `overview`（库规模/论文列表）：它要反映真实的索引状态，每次进页面重新取。
 */

import { reactive } from 'vue';
import type {
  KnowledgeChunkDetail, KnowledgeChunkRow, SimilarResult,
} from '@/api/types';

export type KbMode = 'search' | 'browse';
export type KbScope = 'all' | 'paper';

export interface KnowledgeState {
  /** 当前模式（搜索 / 浏览） */
  mode: KbMode;
  /** 搜索：输入词、范围、选中的论文、"排除所选论文"开关 */
  query: string;
  scope: KbScope;
  paperId: string;
  excludeSelf: boolean;
  /** 上一次检索结果（null = 还没检索过；`error` 是失败原因） */
  result: SimilarResult | null;
  error: string;
  /** 浏览：筛选条件与已取回的条目 */
  bPaperId: string;
  bType: string;
  bLang: string;
  chunks: KnowledgeChunkRow[];
  /** 展开全文：当前篇 + 已取回的正文 */
  detailId: string;
  detail: KnowledgeChunkDetail | null;
}

export const kbState = reactive<KnowledgeState>({
  mode: 'search',
  query: '',
  scope: 'all',
  paperId: '',
  excludeSelf: false,
  result: null,
  error: '',
  bPaperId: '',
  bType: '',
  bLang: '',
  chunks: [],
  detailId: '',
  detail: null,
});
