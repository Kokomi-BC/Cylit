/**
 * 跨论文引用：**这条引用属于哪一篇 / 该请求什么地址 / 缓存键怎么拼**。
 *
 * 为什么单独抽一个纯函数模块：这三件事原来各判一遍 —— 判定「不是本文的」只在
 * `ReaderChat.gotoCitation` 里生效（一句 toast + return），速览卡与跳转都不知道该去哪篇，
 * 于是别的论文的引用进来只能显示一张空卡。抽出来之后三个调用点问同一枚函数，
 * 也才好在没有前端测试框架的情况下用一个 node 脚本把口径钉住
 * （同篇 / 别篇 / 缺 paper_id 的老数据 / 缓存键必须带 paper_id）。
 *
 * 全部是纯函数：不碰网络、不碰 Vue、不读全局状态。
 */

/** 判定"属于哪一篇、落在哪个段"只看这三个字段（别的字段不参与）。 */
export interface CitePaperRef {
  paper_id?: string | null;
  block_id?: string | null;
  /** 页级**转录行**的身份（形如 p002-00）。块式引用这里为空、行式引用 block_id 为空 ——
   *  两侧合起来才是"这一段在哪"。后端 block_detail 两种 id 都能解析。 */
  row_id?: string | null;
}

/** 这一处引用指向的段 id：**块 id 优先，没有就退行 id**。
 *
 *  为什么必须退：agent 读页级转录行时引用里带的是 row_id、block_id 为空 ——
 *  只看 block_id 会让这一类跨篇引用**一个请求都不发**，浮窗永远空白（实测的缺口之一）。
 *  行 id 每篇都长一样（p002-00），所以缓存键里必须带 paper_id（见 citeBlockKey）。 */
export function citeSegmentId(cite: CitePaperRef | null | undefined): string {
  const blockId = String(cite?.block_id ?? '').trim();
  if (blockId) return blockId;
  return String(cite?.row_id ?? '').trim();
}

/**
 * 这条引用属于哪一篇。
 *
 * **缺 `paper_id` 的老会话退回当前论文**：这个字段是后加的，跨库检索之前的存量消息
 * 一律没有它 —— 因为缺一个字段就"什么都不显示"是最糟的退路（那些引用的落点其实就在本文里）。
 */
export function citePaperId(
  cite: CitePaperRef | null | undefined,
  currentPaperId: string | null | undefined,
): string {
  const own = String(cite?.paper_id ?? '').trim();
  if (own) return own;
  return String(currentPaperId ?? '').trim();
}

/** 跨篇取块要请求的地址（后端 `GET /papers/{paper_id}/blocks/{block_id}`）。
 *  缺 paper_id / block_id 时返回空串 —— 这一处没有可现取的内容。 */
export function citeBlockUrl(
  paperId: string | null | undefined,
  blockId: string | null | undefined,
): string {
  const pid = String(paperId ?? '').trim();
  const bid = String(blockId ?? '').trim();
  if (!pid || !bid) return '';
  return `/papers/${encodeURIComponent(pid)}/blocks/${encodeURIComponent(bid)}`;
}

/**
 * 跨篇块内容的缓存键。
 *
 * **paper_id 必须进键**：`p003-00` 这种行 id 每篇论文都长得一模一样，只按 block_id 缓存
 * 会把 A 篇的内容显示给 B 篇（这正是以前踩过的坑）。任一侧为空时返回空串 = 这一处不缓存。
 */
export function citeBlockKey(
  paperId: string | null | undefined,
  blockId: string | null | undefined,
): string {
  const pid = String(paperId ?? '').trim();
  const bid = String(blockId ?? '').trim();
  if (!pid || !bid) return '';
  return `${pid}::${bid}`;
}

/**
 * **论文级引用**（agent 直接引一篇论文本身：只有 `paper_id`，没有块 / 行 id）→
 * 取这一篇的档案做预览（后端 `GET /papers/{paper_id}/brief`）。
 *
 * 为什么需要它：这类引用在库里是**独立的一类**（实测一条会话里 2 条 `kind=paper`），
 * 从前它既没有块 id 也没有行 id，速览卡只能落到「这条引用没给可取原文的块 id」那一句上 ——
 * 用户点开只看到一句论文题名加一条灰字说明，等于没法看（用户口径 2026-12-28）。
 * 论文级引用**不该**去要块：它引的就是"这篇文章"。
 */
export function citeBriefUrl(paperId: string | null | undefined): string {
  const pid = String(paperId ?? '').trim();
  if (!pid) return '';
  return `/papers/${encodeURIComponent(pid)}/brief`;
}

/** 论文档案的缓存键（键里必须带 paper_id：同一张卡换一篇就是另一份档案）。 */
export function citeBriefKey(paperId: string | null | undefined): string {
  const pid = String(paperId ?? '').trim();
  return pid ? `${pid}::brief` : '';
}

export interface CiteBlockTarget {
  /** 这条引用属于哪篇（缺 paper_id 时就是当前论文） */
  paperId: string;
  /** true = 别篇：本文的 reader 数据里没有它，内容得按 paper_id 现取 */
  foreign: boolean;
  /** 要请求的块地址（'' = 不请求：同篇照旧从 reader 数据里找，一个字都不改） */
  url: string;
  /** 缓存键（'' = 不缓存） */
  key: string;
}

/** 一处引用在速览卡里的**取数决策**：同篇 / 别篇 / 缺字段三种情形都在这里落定。 */
export function citeBlockTarget(
  cite: CitePaperRef | null | undefined,
  currentPaperId: string | null | undefined,
): CiteBlockTarget {
  const paperId = citePaperId(cite, currentPaperId);
  const current = String(currentPaperId ?? '').trim();
  const foreign = Boolean(paperId) && paperId !== current;
  if (!foreign) return { paperId, foreign: false, url: '', key: '' };
  const seg = citeSegmentId(cite);
  return {
    paperId,
    foreign: true,
    url: citeBlockUrl(paperId, seg),
    key: citeBlockKey(paperId, seg),
  };
}

/** 后端 `block_detail` 里速览卡关心的那几个字段（其余一概不用）。 */
export interface BlockDetailLike {
  page?: number | null;
  type?: string | null;
  text?: string | null;
  translation?: string | null;
  formula?: {
    latex_final?: string | null;
    latex_repaired?: string | null;
    latex_candidate?: unknown;
  } | null;
  /** 插图块的那张图（后端新加的键；别的块是 null 或干脆没这个键）。
   *  `image_url` 是**相对路径**，模板照旧走 assetUrl；图注中英各一份。 */
  figure?: {
    index?: number | null;
    image_url?: string | null;
    caption?: string | null;
    caption_zh?: string | null;
  } | null;
}

export interface PeekBlockFields {
  /** 块类型（heading / para / formula / figure / table …）：卡片标题用 */
  type: string;
  /** 译文侧（`translation`） */
  zh: string;
  /** 原文侧（`text`） */
  en: string;
  /** 公式（只有公式块有）：`latex_final` → `latex_repaired` → `latex_candidate[0]` */
  latex: string;
  /** 图注（后端 figure.caption_zh → figure.caption；两者都没有时退回 Markdown 图片的 alt） */
  caption: string;
  /** 插图地址（后端 figure.image_url；没有时退回 Markdown 图片的 src。模板照旧走 `assetUrl`） */
  imageUrl: string;
  page: number;
}

/** 公式卡里的 LaTeX：三档按可信度取第一个非空的。 */
function formulaLatexOf(f: BlockDetailLike['formula']): string {
  if (!f) return '';
  const final = String(f.latex_final ?? '').trim();
  if (final) return final;
  const repaired = String(f.latex_repaired ?? '').trim();
  if (repaired) return repaired;
  const list = Array.isArray(f.latex_candidate) ? f.latex_candidate : [];
  for (const one of list) {
    const s = String(one ?? '').trim();
    if (s) return s;
  }
  return '';
}

/** figure 里的图注：**中文优先**，没有就用原文图注（两边都空 = 没图注）。 */
function figureCaptionOf(fig: BlockDetailLike['figure']): string {
  return String(fig?.caption_zh ?? '').trim() || String(fig?.caption ?? '').trim();
}

/** Markdown 图片 → { alt, src }；没有就返回 null。 */
const MD_IMG = /!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/;

/**
 * `block_detail` 的响应 → 速览卡要填的那几个字段。
 *
 * 插图 / 表格在解析产物里是**一整块文字**（图注与图片都在 Markdown 里），所以这里顺手把
 * 图片那一段拆出来：地址交给模板（`assetUrl`），alt 当图注，剩下的文字仍当正文 ——
 * 否则同一句图注会在"图注"与"正文"两处各来一遍。
 */
export function blockPeekFields(detail: BlockDetailLike | null | undefined): PeekBlockFields {
  const d = detail ?? {};
  const text = String(d.text ?? '').trim();
  let zh = String(d.translation ?? '').trim();
  /* 图那一份**优先**：后端 figure 给的就是"这一段是哪张图、地址与中英图注"，
     比从 Markdown 文字里抠更准；抠不出来（老后端 / 图块只剩文字）时才由它兜底。
     figure 缺失或为 null 时两个值都是空串 → 下面的行为与从前逐字一致。 */
  let imageUrl = String(d.figure?.image_url ?? '').trim();
  let caption = figureCaptionOf(d.figure);
  let en = text;
  /* 图注块（`type=caption` 的行）的正文**就是**那句图注：语言不同、粗体标记不同，
     直接比字符串对不上。归一之后相等就把它从正文里去掉 —— 否则卡片里同一句话
     在「图注」与「正文」各出现一遍（实测跨篇插图卡就是这样重复的）。 */
  const flat = (s: string): string => String(s || '').replace(/[*_`]/g, '').replace(/\s+/g, '').trim();
  const dedupeCaption = (): void => {
    const c = flat(caption);
    if (!c) return;
    if (flat(zh) === c) zh = '';
    if (flat(en) === c) en = '';
  };
  const hit = MD_IMG.exec(text);
  if (hit) {
    const mdCaption = String(hit[1] ?? '').trim();
    if (!caption) caption = mdCaption;
    if (!imageUrl) imageUrl = String(hit[2] ?? '').trim();
    /* 只剩图片那一句时正文就该空着：图注已经在上面的 caption 里画了一次
       （caption 已被 figure 覆盖时按新值比，同一句同样不再进正文） */
    const rest = text.replace(hit[0], '').trim();
    en = rest && rest !== mdCaption && rest !== caption ? rest : '';
  }
  dedupeCaption();
  return {
    type: String(d.type ?? '').trim(),
    zh,
    en,
    latex: formulaLatexOf(d.formula),
    caption,
    imageUrl,
    page: Number(d.page ?? 0) || 0,
  };
}
