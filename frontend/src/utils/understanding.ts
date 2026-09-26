/**
 * 「解读」这一个动作的**共享口径**：三处都有「重新生成解读」（论文库卡片「⋯」、
 * 阅读器工具栏「⋯」、理解页顶部），三处各写一份确认文案必然会走音。
 *
 * 为什么必须二次确认：重跑 = 两路各一次**全篇**模型调用（实测 1~2 分钟），
 * 而它就在一个「⋯」菜单里 —— 误点一下就是白花钱白等。
 *
 * 为什么先 GET 一次再决定弹不弹：论文列表行里**没有**"解读生成过没有"这个字段，
 * 猜不出来；而接口很小（库里就是两行 markdown）。查询失败一律当"没有解读"
 * （宁可少弹一次确认，也不要"点了没反应"）。
 */
import { api } from '@/api/client';

/** 这篇论文有没有生成过解读（`a` / `b` 任一有 markdown 就算）。查询失败按"没有"处理。 */
export async function hasInsight(paperId: string): Promise<boolean> {
  try {
    const d = await api.get<{ a?: { markdown?: string } | null; b?: { markdown?: string } | null }>(
      `/papers/${paperId}/understanding`);
    return Boolean(d?.a?.markdown || d?.b?.markdown);
  } catch {
    return false;
  }
}

/**
 * 已经有解读时用 **HTML 原生 confirm** 问一次（与「重新翻译」同一风格：只是重跑一遍，
 * 不是不可逆删除，不值得为它维护一个站内弹窗的状态）。返回 `true` = 继续重跑。
 */
export async function confirmRegenerateInsight(paperId: string, title?: string): Promise<boolean> {
  if (!(await hasInsight(paperId))) return true;
  return window.confirm(
    `重新生成《${title || paperId}》的解读？\n\n`
    + '会忽略已有的解读结果，两路都重跑一遍（解读读的是知识库的 chunk）。');
}
