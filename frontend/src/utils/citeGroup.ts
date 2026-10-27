/**
 * 引用分组 / 排序：**纯函数**（吃数据、吐数据，不碰 DOM、不碰 Vue 状态）。
 *
 * 为什么单独一个模块：点击（`focusMany`）与速览卡（`withCiteChain`）必须用**同一套**
 * 成员判定，否则会出现「正文闪一处、卡片画另一处」；单测也直接 import 这份源码，
 * 不会再出现"测试跑的是另一份拷贝"。
 */

/** 引用池里的一项（与 ReaderChat 的 `CitePoolItem` 结构一致）。 */
export type CiteGroupItem = {
  ref: number; row: string; block: string; page: number; latex: string;
  /** 这一条证据覆盖的行身份（`PageRow.pid`）：同一块跨页时有多行，点一枚标号要把整块一起闪。 */
  pids?: string[];
};

/** 行 id 拆「页前缀 + 行号」：`p006-01` → (`p006-`, 1)。排序只靠这两段。
 *  `row-` 前缀先归一（与 `rowAnchorId` 同一条规则）：公式 / 表格类引用给的是 `row-p006-03`。 */
export function citeRowKeyOf(id?: string | null): { prefix: string; num: number } | null {
  const m = /^(.*?)(\d+)$/.exec(String(id || '').replace(/^row-/, ''));
  return m ? { prefix: m[1], num: Number(m[2]) } : null;
}

/**
 * 点一个方括号 → 该**一起闪 / 一起显示**的那几处。
 *
 * 口径（用户口径「服务器的 2 和 10 为什么是同一段内容」后定稿）：
 * - **只看方括号里有几个号**：`[2]` 只闪它自己那一行、`[10]` 只闪它自己那一行；
 *   `[3,4]` / `[15,16,17]` 才把这几处一起闪。
 * - **不再按"落点相邻"自己扩链**：生产库那条会话里 ref 10 = `p005-02`、ref 2 = `p005-03`，
 *   是同一小节的**两段不同正文**（前一段讲随机旋转 + QR 分解，后一段讲旋转后 `Πx` 均匀分布），
 *   行号只差 1。旧规则把它们连成一条，于是点 2 和点 10 闪的、滚的完全一样 ——
 *   用户看到的就是"2 和 10 是同一段内容"，两枚标号再也分不出来。
 * - 合并写法的**唯一**来源是模型自己写在方括号里的号（提示词已要求同一处的材料写成一个
 *   方括号，如 `[15,16,17]`），前端不再猜相邻。
 */
export function pickCiteGroup(refs: number[], pool: CiteGroupItem[]): CiteGroupItem[] {
  const byRef = new Map<number, CiteGroupItem>();
  for (const item of pool) if (!byRef.has(item.ref)) byRef.set(item.ref, item);
  const out: CiteGroupItem[] = [];
  const seen = new Set<number>();
  for (const raw of Array.isArray(refs) ? refs : []) {
    const ref = Number(raw);
    if (!Number.isFinite(ref) || seen.has(ref)) continue;
    const item = byRef.get(ref);
    if (!item) continue;
    seen.add(ref);
    out.push(item);
  }
  return out;
}

/** 一组引用按论文顺序（页前缀 → 行号）排：速览卡是当一整段读的（标题在上、正文在下），
 *  顺序错读起来就是跳的；滚动落点取这一组里**文档顺序最上面**的那一行。
 *  认不出行号的（`fig:p…` 合成锚点、只有块 id 的）一律排到最后 —— `mapSpanId` 认不出会丢掉，
 *  全丢光时它自己退回锚点那一行。 */
export function sortCiteRows(items: CiteGroupItem[]): CiteGroupItem[] {
  /* `fig:p<页>:n<图号>` 是我们自己拼的合成锚点（见 ReaderChat.citationAnchor），不是正文行 id：
     给它排序键只会让它按"图号数字"挤到段落行前面去，所以一律当没有序。 */
  const keyed = items.map((item, i) => ({
    item, i, key: /^fig:/.test(item.row) ? null : citeRowKeyOf(item.row),
  }));
  keyed.sort((a, b) => {
    if (a.key && b.key) {
      // 页前缀直接比字符串：`p006-` / `p007-` 是补零的，字典序与页序一致
      if (a.key.prefix !== b.key.prefix) return a.key.prefix < b.key.prefix ? -1 : 1;
      if (a.key.num !== b.key.num) return a.key.num - b.key.num;
      return a.i - b.i;
    }
    if (a.key) return -1;
    if (b.key) return 1;
    return a.i - b.i;
  });
  return keyed.map((e) => e.item);
}
