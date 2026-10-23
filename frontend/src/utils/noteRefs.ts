/**
 * 笔记里存**标号跳转信息**：回答正文里的 `[1]` / `A045` 这类标号，落成笔记后要还能点。
 *
 * 为什么不加数据库列：`note` 表只有 `content_md` 一个自由文本列，加列要走迁移；而跳转信息天然
 * 是"这条笔记的附属品"—— 用一行 HTML 注释挂在正文**末尾**，Markdown 渲染时会被丢弃（用户看不到），
 * 前端读原文时再解析出来。零迁移、可回退（删掉注释就是普通笔记）。
 *
 * 放在**末尾**而不是开头：笔记列表对正文有 `line-clamp` 截断，开头塞一坨注释会把真正的第一句挤下去，
 * 而截断只影响"显示"，解析读的是完整原文。
 */
export interface NoteRef {
  ref: string;
  block_id?: string;
  row_id?: string;
  page?: number;
}

const NOTE_REFS_RE = /<!--\s*refs:([\s\S]*?)-->\s*$/;

/** 正文 + 标号跳转表 → 落库用的 `content_md`（没有标号就原样返回）。 */
export function withNoteRefs(
  md: string,
  // 直接吃 `AgentCitation` 的形状（`ref` 在那边是 number），省掉调用处再转一层
  refs?: Array<{ ref: string | number; block_id?: string | null; row_id?: string | null; page?: number | null }> | null,
): string {
  const text = String(md ?? '').trimEnd();
  const list = (refs ?? []).filter((r) => r && r.ref);
  if (!text || !list.length) return text;
  const slim = list.map((r) => ({
    ref: String(r.ref),
    ...(r.block_id ? { block_id: String(r.block_id) } : {}),
    ...(r.row_id ? { row_id: String(r.row_id) } : {}),
    ...(r.page ? { page: Number(r.page) } : {}),
  }));
  try {
    return `${text}\n<!-- refs:${JSON.stringify(slim)} -->`;
  } catch {
    return text;
  }
}

/**
 * **显示 / 编辑**用的正文：摘掉末尾那行 `<!-- refs:[…] -->`。
 *
 * 为什么到处都得走它：`mdToHtml` 把 HTML 当**文本**转义（`html: escapeHtml`），那行注释
 * 于是被原样印出来（用户 2026-10-23 报的「笔记内显示 ref 之类的内容」就是它）；
 * 编辑框 / 首页摘要更是直接吃原文。显示归显示，跳转表仍由 `splitNoteRefs` 单独拿走。
 */
export function noteBodyText(md?: string | null): string {
  return splitNoteRefs(md).text;
}

/**
 * 存回去：用户改完的正文 + **这条笔记原来那份标号表** → 落库用的 `content_md`。
 * 只改正文、不动跳转，编辑一次不会把 `[1]` 的可点能力弄丢。
 */
export function withKeptNoteRefs(text: string, originalMd?: string | null): string {
  return withNoteRefs(text, splitNoteRefs(originalMd).refs);
}

/** 反过来：把 `content_md` 拆成「可渲染的正文」+「标号跳转表」。 */
export function splitNoteRefs(md?: string | null): { text: string; refs: NoteRef[] } {
  const raw = String(md ?? '');
  const match = NOTE_REFS_RE.exec(raw);
  if (!match) return { text: raw, refs: [] };
  let refs: NoteRef[] = [];
  try {
    const parsed = JSON.parse(match[1]);
    if (Array.isArray(parsed)) refs = parsed.filter((r) => r && r.ref);
  } catch { refs = []; }
  return { text: raw.slice(0, match.index).trimEnd(), refs };
}
