/**
 * 「笔记」信息流的**唯一一份归并规则**（阅读器侧栏与笔记整页共用这一份）。
 *
 * 用户口径（2026-10-07）：**批注 / 高亮 / 笔记统一成「笔记」** ——
 * 一条 = **一处高亮（哪一段文字）+ 它的正文**。于是库里那三张表在这里被摊平成一串条目：
 *
 * - 高亮按 `group_id` 归并（一次跨行划词 = 几条记录 = 界面上**一条**笔记）；
 * - 挂在这处高亮上的笔记就是它的正文（没有就标「待写」，提示用户点铅笔补上）；
 * - 没挂在任何高亮上的笔记（第四步之前建的 / 高亮先被删了）照样成一条；
 * - 批注（划词解释 / 人工批注）按**同一种卡片**列出来，只是没有"引用原文"那一行。
 *
 * 排序：按时间倒序（没有时间的用空串，稳定地排在最后）。
 *
 * ⚠️ 两个调用方只共用**归并**，不共用展示：侧栏是窄条、整页是宽卡片，
 * 而且整页要带"哪篇论文"的徽章 —— 所以这里给的是中性行对象，标题/正文由各自渲染。
 */
import type { Annotation, Highlight, Note } from '@/api/types';

/**
 * 高亮原文的清洗：PDF 里选中**公式 / 图片块**时，浏览器给的不是文字，而是一个
 * U+FFFC（对象替换符）。它原样渲染出来就是一枚「OBJ」占位字形（用户 2026-11-26 报的
 * "公式在笔记里显示成 obj" 就是它，服务端库里 8 条笔记的 selected_text 是 EFBFBC）。
 *
 * 这类字符不是内容：换成空格、合并空白、去首尾；清完为空就让调用方整行不渲染
 * —— 宁可只显示"这条笔记写的是什么"，也不显示一个看不懂的占位方块。
 */
export function cleanQuote(text: string): string {
  return String(text ?? '')
    .replace(/[\uFFFC\uFFFD\u200B]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export interface NotesFeedRow<H = Highlight, N = Note, A = Annotation> {
  /** 稳定键（`h:<分组>` / `n:<笔记id>` / `a:<批注id>`）：列表 `key`、滚动定位、编辑态都用它 */
  key: string;
  kind: 'highlight' | 'note' | 'annotation';
  /** 定位用的锚点（正文行 id / `md:p<页>` / `pdf:p<页>`；批注是它的 `block_id`） */
  anchorId: string | null;
  /** 删除用的一串高亮 id（跨行划词切出来的一组共用一条笔记） */
  anchorIds: string[];
  page: number | null;
  /** 高亮原文（批注没有） */
  quote: string;
  /** 高亮的颜色（整页的色点要用） */
  color: string;
  /** 挂在哪一面：`row` / `markdown` / `pdf`（三套锚点不同，界面要说清） */
  scope?: 'row' | 'markdown' | 'pdf';
  /** 已有笔记的 id（空 = 还没写正文） */
  noteId: string;
  /** 挂在哪条高亮上（批注为空） */
  highlightId: string;
  contentMd: string;
  /** 徽章文字：笔记 / 待写 / 划词解释 / 批注（类型） */
  label: string;
  /** 徽章配色（与 `.badge.*` 对应） */
  badge: string;
  at: string | null;
  /** 原始记录（编辑 / 删除要按类型走不同接口） */
  highlight: H | null;
  note: N | null;
  annotation: A | null;
}

export function buildNotesFeed<H extends Highlight, N extends Note, A extends Annotation>(input: {
  highlights: H[];
  notes: N[];
  annotations: A[];
}): NotesFeedRow<H, N, A>[] {
  const rows: NotesFeedRow<H, N, A>[] = [];
  const noteOf = (hlId: string) => input.notes.find((n) => n.highlight_id === hlId) ?? null;
  const usedNotes = new Set<string>();

  const groups = new Map<string, H[]>();
  for (const h of input.highlights) {
    const g = h.group_id || h.id;
    groups.set(g, [...(groups.get(g) ?? []), h]);
  }
  for (const [g, list] of groups) {
    const ids = list.map((h) => h.id);
    const note = list.map((h) => noteOf(h.id)).find(Boolean) ?? null;
    if (note) usedNotes.add(note.id);
    const first = list[0];
    rows.push({
      key: `h:${g}`, kind: note ? 'note' : 'highlight',
      anchorId: first.block_id ?? null, anchorIds: ids,
      page: first.page ?? null,
      // 原文是占位符（在 PDF 里选中公式 / 图片块）时退回那一行的正文 markdown：
      // 用户要看到的是"这条笔记标的是哪个式子"，不是一个占位字形。
      quote: list.map((h) => cleanQuote(h.text ?? '') || cleanQuote(h.quote_md ?? ''))
        .filter(Boolean).join(' '),
      color: first.color || 'yellow',
      scope: first.scope ?? 'row',
      noteId: note?.id ?? '', highlightId: ids[0] ?? '',
      contentMd: note?.content_md ?? '',
      label: note ? '笔记' : '待写',
      badge: note ? 'accent' : 'dim',
      at: note?.updated_at ?? note?.created_at ?? first.created_at ?? null,
      highlight: first, note, annotation: null,
    });
  }
  for (const n of input.notes) {
    if (usedNotes.has(n.id)) continue;
    rows.push({
      key: `n:${n.id}`, kind: 'note', anchorId: n.block_id, anchorIds: [],
      page: n.page ?? null,
      quote: cleanQuote(n.selected_text ?? '') || cleanQuote(n.quote_md ?? ''),
      color: '',
      scope: n.scope, noteId: n.id, highlightId: n.highlight_id ?? '',
      contentMd: n.content_md, label: '笔记', badge: 'accent',
      at: n.updated_at ?? n.created_at ?? null,
      highlight: null, note: n, annotation: null,
    });
  }
  for (const a of input.annotations) {
    const selection = a.source === 'selection';
    rows.push({
      key: `a:${a.id}`, kind: 'annotation', anchorId: a.block_id, anchorIds: [],
      page: a.page ?? null, quote: '', color: '', noteId: '', highlightId: '',
      contentMd: a.content_md,
      label: selection ? '划词解释' : (a.type || '批注'),
      badge: selection ? 'ok' : 'accent', at: a.created_at,
      highlight: null, note: null, annotation: a,
    });
  }
  return rows.sort((x, y) => String(y.at ?? '').localeCompare(String(x.at ?? '')));
}
