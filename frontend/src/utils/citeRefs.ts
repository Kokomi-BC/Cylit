/**
 * 正文里的 `[n]` → **可直接点击的引用锚**（聊天与笔记浮层共用这一份）。
 *
 * 为什么不做成一排单独的引用表：那是把"引用表"和"正文"拆成两处，读的时候要来回对号。
 * 只在**渲染后的 HTML** 上替换，且跳过代码块（`<code>x[0]</code>` 里的不是引用）；
 * 只认这一份数据里**真实存在**的 ref，别把恰好写成 `[1]` 的普通方括号变成按钮。
 * 锚点信息写进 `data-*`：点击时不必再猜"这个编号属于哪一条"，跨消息也不会串号。
 */
export interface RefCite {
  ref: number | string;
  paper_id?: string | null;
  row_id?: string | null;
  block_id?: string | null;
  page?: number | null;
  snippet?: string | null;
  label?: string | null;
}

function escapeAttr(value: string): string {
  return String(value || '')
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export function decorateRefs(html: string, cites?: RefCite[] | null): string {
  if (!cites?.length) return html;
  const byRef = new Map(cites.map((c) => [String(c.ref), c]));
  const parts = html.split(/(<code[\s\S]*?<\/code>|<pre[\s\S]*?<\/pre>)/i);
  return parts.map((part, index) => {
    if (index % 2 === 1) return part;                       // 代码块原样返回
    return part.replace(/\[(\d{1,3})\]/g, (whole, num) => {
      const cite = byRef.get(String(num));
      if (!cite) return whole;
      const title = escapeAttr(cite.snippet || cite.label || `引用 ${num}`);
      /* 用 aria-label 而不是 title：title 会弹浏览器原生灰气泡，与速览卡叠在一起 */
      return `<button type="button" class="cite-inline" aria-label="${title}"`
        + ` data-ref="${num}" data-paper="${escapeAttr(cite.paper_id || '')}"`
        + ` data-row="${escapeAttr(cite.row_id || '')}"`
        + ` data-block="${escapeAttr(cite.block_id || '')}"`
        + ` data-snippet="${escapeAttr(cite.snippet || '')}"`
        + ` data-page="${cite.page ?? ''}">[${num}]</button>`;
    });
  }).join('');
}
