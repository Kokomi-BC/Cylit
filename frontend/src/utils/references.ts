/**
 * 参考文献的两件小事：**展示标题**与**发表网站地址**。
 *
 * 为什么要从原文里抠：`paper_reference` 那几列结构化字段（title/authors/venue/doi/url）
 * 是留给模型或后续解析补的，**离线抽取只写 `raw`**（见 `metadata_fill.save_references`）——
 * 也就是说现在库里几乎每条都只有原文。而用户要的"点条跳到发表网站"必须能用，
 * 所以地址三级回退：结构化字段 → 原文里抠出来的 DOI/arXiv/URL → 学术搜索。
 */
import type { PaperReference } from '@/api/types';

/** 原文里的 DOI（`10.xxxx/yyy`，参考文献里最常见的两种写法都吃） */
const DOI_IN_TEXT = /\b(10\.\d{4,9}\/[^\s"'<>)\]]+)/i;
/** 原文里的完整链接 */
const URL_IN_TEXT = /(https?:\/\/[^\s"'<>)\]]+)/i;
/** 原文里的 arXiv 号（`arXiv:2401.12345` / `arXiv: 2401.12345v2`） */
const ARXIV_IN_TEXT = /arxiv[:\s]*(\d{4}\.\d{4,5})(v\d+)?/i;

/** 去掉结尾的标点：抄进参考文献的 URL 后面常跟着句号/逗号/括号 */
function tidy(url: string): string {
  return url.trim().replace(/[.,;:)\]]+$/, '');
}

/** 条目开头的编号（`[12]` / `(12)` / `12.`）—— **搜索时要去掉**：它只会把结果带偏 */
const REF_NO_RE = /^\s*(?:\[\d{1,4}\]|\(\d{1,4}\)|\d{1,4}[.)])\s*/;

/** 给搜索用的文本：剥掉编号（用户口径 2026-10-07：跳学术搜索时别把标号带进去） */
export function stripRefNumber(text: string): string {
  return String(text ?? '').replace(REF_NO_RE, '').trim();
}

/**
 * 这条文献的"发表网站"：
 * `url` → `doi` → 原文里的 arXiv → 原文里的 URL → 原文里的 DOI → 学术搜索。
 *
 * 学术搜索放最后是**兜底而不是首选**：它一定能给出点什么（用户点下去不会觉得按钮坏了），
 * 但只有前面几条都抠不出来时才用它。
 */
export function referenceUrl(ref: PaperReference): string {
  const direct = tidy(String(ref.url ?? ''));
  if (/^https?:\/\//i.test(direct)) return direct;

  const doiField = String(ref.doi ?? '').trim().replace(/^https?:\/\/(dx\.)?doi\.org\//i, '');
  if (doiField) return `https://doi.org/${tidy(doiField)}`;

  const raw = String(ref.raw ?? '');
  const arxiv = ARXIV_IN_TEXT.exec(raw);
  if (arxiv) return `https://arxiv.org/abs/${arxiv[1]}`;

  const href = URL_IN_TEXT.exec(raw);
  if (href) return tidy(href[1]);

  const doi = DOI_IN_TEXT.exec(raw);
  if (doi) return `https://doi.org/${tidy(doi[1])}`;

  const q = [stripRefNumber(String(ref.title_en ?? ref.title ?? '')), ref.authors, ref.year]
    .filter(Boolean).join(' ') || stripRefNumber(raw);
  return `https://scholar.google.com/scholar?q=${encodeURIComponent(q.slice(0, 300))}`;
}

/** 原始条目里的**所有**完整链接（`URL_IN_TEXT` 是非全局的，这里单独开一个用 `matchAll`） */
const URL_IN_TEXT_G = /(https?:\/\/[^\s"'<>)\]]+)/gi;
/** GitHub 仓库地址（`github.com/owner/repo`，带不带协议都认） */
const GITHUB_IN_TEXT = /(?:https?:\/\/)?(?:www\.)?github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+/i;

function isGithub(url: string): boolean {
  return /github\.com\//i.test(url);
}

function normalizeGithub(url: string): string {
  const hit = tidy(url);
  const withScheme = /^https?:\/\//i.test(hit) ? hit : `https://${hit}`;
  // 只留 `github.com/owner/repo` 这一段（后面的 `/tree/main`、`?tab=readme` 都截掉）
  const m = /^https?:\/\/(?:www\.)?github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+/i.exec(withScheme);
  return m ? m[0] : withScheme;
}

/**
 * 这条文献里的 **GitHub 仓库地址**（没有就空串）—— 侧栏那枚代码图标用它。
 *
 * 结构化字段 `code_url` 优先（后端 `metadata_fill.ref_code_url` 抠好落库），
 * 老数据没有那一列时再从原文里现抠一次。
 */
export function codeUrl(ref: PaperReference): string {
  const direct = String(ref.code_url ?? '').trim();
  if (direct) return normalizeGithub(direct);
  const m = GITHUB_IN_TEXT.exec(String(ref.raw ?? ''));
  return m ? normalizeGithub(m[0]) : '';
}

/**
 * **论文页面**的地址 —— 与 `referenceUrl` 的区别只有一处：**跳过 GitHub**。
 *
 * 为什么要分开（用户口径 2026-10-07）：参考文献里同时有论文链接和代码链接时，侧栏并排给两枚图标，
 * 论文图标必须跳论文页（arXiv/DOI），不能因为原文里 GitHub 链接排在前面就跳到仓库去。
 * 全都抠不出来时才退回 `referenceUrl` 的学术搜索兜底。
 */
export function paperUrl(ref: PaperReference): string {
  return paperHref(ref) || referenceUrl(ref);
}

/**
 * 真正的**论文地址**（arXiv / DOI / 结构化 `url` 字段）。抠不出来就空串。
 *
 * ⚠️ 刻意**不**拿原文里随便一个网址来顶：`Elastic search., 2025. https://www.elastic.co/…`
 * 那种是"网址链接"而不是论文页，用户口径里它们要用**链接图标**（`refLink` 按这个区分）。
 */
function paperHref(ref: PaperReference): string {
  const direct = tidy(String(ref.url ?? ''));
  if (/^https?:\/\//i.test(direct) && !isGithub(direct)) return direct;

  const doiField = String(ref.doi ?? '').trim().replace(/^https?:\/\/(dx\.)?doi\.org\//i, '');
  if (doiField) return `https://doi.org/${tidy(doiField)}`;

  const raw = String(ref.raw ?? '');
  const arxiv = ARXIV_IN_TEXT.exec(raw);
  if (arxiv) return `https://arxiv.org/abs/${arxiv[1]}`;

  const doi = DOI_IN_TEXT.exec(raw);
  if (doi) return `https://doi.org/${tidy(doi[1])}`;
  return '';
}

/** 原文里第一个**非 GitHub** 的链接（"网址链接"那种：官网、项目页…） */
function firstWebUrl(raw: string): string {
  for (const hit of String(raw ?? '').matchAll(URL_IN_TEXT_G)) {
    const url = tidy(hit[1]);
    if (!isGithub(url)) return url;
  }
  return '';
}

/** 条目左边那枚图标的种类：论文（arXiv/DOI）/ 代码仓库 / 普通网址 */
export type RefLinkKind = 'paper' | 'github' | 'link';

export interface RefLink {
  kind: RefLinkKind;
  url: string;
}

/**
 * 一条文献的**链接图标**怎么画（用户口径 2026-10-07 第六轮）。
 *
 * - 左边那枚：论文（`document` 图标）/ GitHub（`github`）/ 普通网址（`link`）。
 *   有论文链接就用论文图标；**只有**代码链接时左边直接是 GitHub 图标；只有普通网址时是链接图标；
 *   什么都没有（只能去学术搜索）时仍然给论文图标 —— "所有条目左边都有论文图标"这条口径不能破。
 * - `code`：**论文与 GitHub 同时存在**时，GitHub 图标放到**标题下面**（单独的图标行），
 *   因为左边那枚已经被论文占用了。
 */
export function refLink(ref: PaperReference): { primary: RefLink; code: RefLink | null } {
  const code = codeUrl(ref);
  const repo: RefLink | null = code ? { kind: 'github', url: code } : null;

  // ① 有**论文**链接（结构化 url / DOI / 原文里的 arXiv 或 DOI）→ 左边论文图标；
  //    同时有仓库就把它交给"标题下面那一行"。
  const paper = paperHref(ref);
  if (paper) return { primary: { kind: 'paper', url: paper }, code: repo };

  // ② 只有代码链接 → 左边直接是 GitHub 图标（不再在标题下面重复一枚）。
  if (repo) return { primary: repo, code: null };

  // ③ 只有普通网址（官网 / 项目页…）→ 链接图标。
  const web = firstWebUrl(String(ref.raw ?? ''));
  if (web) return { primary: { kind: 'link', url: web }, code: null };

  // ④ 什么都抠不出来：仍是论文图标（点它走学术搜索兜底）—— "每条左边都有论文图标"这条不能破。
  return { primary: { kind: 'paper', url: referenceUrl(ref) }, code: null };
}
