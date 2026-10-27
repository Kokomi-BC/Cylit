<script setup lang="ts">
/**
 * 解读正文的**共享渲染器**：阅读器左栏页签（「论文解读」）与整页（`/understand/:paperId`）都用它。
 *
 * 为什么要共享：同一份 markdown 有两个出口。两处各写一遍渲染，排版一定会各自漂移
 * （行宽、段间距、标题档位、表格样式、公式溢出…）。这里只负责"把 markdown 按
 * **阅读器的正文排版**画出来"，数据从 `useUnderstanding` 来、标题由各自的宿主渲染。
 *
 * 排版口径（对齐阅读器 `ReaderRow` 的 `.rr-body`）：行高 1.78、段间距 .55em、标题 em 档位、
 * 数字右对齐 + 不折行的表格、公式横向可滚。字号取 `--reader-font-size`（宿主设好）。
 * ⚠️ 那些数值是**抄** ReaderRow 的：改一处要同时看另一处（跨组件拿不到它的 scoped 样式）。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { mdToHtml } from '@/composables/useMarkdown';
import ImageViewer from '@/components/reader/ImageViewer.vue';
import type { UnderstandingRefItem, ZoomTarget } from '@/api/types';
import { buildInsightOutline } from '@/utils/insightOutline';

const props = withDefaults(defineProps<{
  markdown?: string | null;
  /** 整篇都没有解读（与"还没生成"不同）：显示空状态 + 「开始解读」 */
  empty?: boolean;
  /** 首次加载中（还没有任何内容可显示） */
  loading?: boolean;
  starting?: boolean;
  /** 解读任务在跑（宿主按任务列表 / 本地"刚发起"算）：空状态里给一行"会自动刷新"的提示 */
  running?: boolean;
  /** 后端 `error.message`，原样显示 */
  error?: string;
  /** 轮询到上限：提示用户稍后再看 */
  timedOut?: boolean;
  /**
   * 整页阅读：正文列按阅读器的行宽收窄（没有侧栏的宿主用）。
   * 整页是自己给右栏定宽（`InsightPage` 的列宽 + 页边距），所以那里不再传它；
   * 这个 prop 留给"把正文直接放进一个很宽的容器"的场景。
   */
  narrow?: boolean;
  /**
   * 标题 anchor 前缀：宿主拿**同一个前缀**调 `buildInsightOutline()` 生成目录，
   * 这里按序把 id 贴到渲染出来的 `h1..h6` 上。两边的 id 必须同源，
   * 否则点目录会滚到别处（所以前缀是必填语义，默认值只作兜底）。
   */
  anchorPrefix?: string;
  /**
   * 论文 id：与 `mdToHtml` 的第二个参数同义 —— 后端产物里的图片是相对路径
   * （`../figures/x.png`），不给 paperId 就不会改写成 `/papers/<id>/figures/...`，
   * 浏览器按当前地址解析成 `/app/figures/...` → 404。解读正文是模型写的，通常没有图，
   * 但产物里一旦出现就整块裂图，所以照传。
   */
  paperId?: string;
  /**
   * 解读里的引用位置（后端 `refs_resolved` + `stale_refs`，按编号索引）。
   * 没有它，正文里的 `[A023]` 也能渲染成标号，只是点了跳不动 —— 所以是可选 prop。
   */
  refs?: UnderstandingRefItem[];
  /**
   * 编号 → 图地址（后端材料清单里 `image` 那一栏，形如 `{A012: '/api/v1/papers/…/figures/region/6/2?v=…'}`）。
   *
   * 解读正文里模型写的是 `![图注](img:A012)` —— 短码只有后端认得，这里换成真实裁图地址；
   * 查不到地址的（该条材料没有图）整块不渲染，不留裂图标。
   */
  images?: Record<string, string>;
}>(), {
  markdown: '', empty: false, loading: false, starting: false, running: false,
  error: '', timedOut: false, narrow: false, anchorPrefix: 'ins', paperId: '', images: () => ({}),
});

const emit = defineEmits<{
  (e: 'start'): void;
  /** 标题 anchor 已贴好（宿主据此算一次"当前小节"，否则首屏一个高亮都没有） */
  (e: 'anchors-ready'): void;
  /**
   * 点了正文里的引用标号：宿主负责跳到原文（阅读器滚到那一段，整页则开阅读器）。
   *
   * `rows` 是这一枚标号覆盖的**全部**正文行（逗号分隔）：一组连续材料（`[A012,A013]`）
   * 落在一个方括号里时，宿主按它闪整组（组内每个短码的 row_ids 并集）。
   */
  (e: 'cite', p: { code: string; block: string; page: number; latex?: string; rows?: string }): void;
  /** 悬浮标号：宿主在旁边弹一张速览卡（点标号仍然走 cite，不受影响） */
  (e: 'peek', p: {
    anchor: { x: number; y: number; w: number; h: number };
    page?: number | null;
    rowId?: string | null;
    blockId?: string | null;
    label?: string | null;
    /** 公式类引用带 LaTeX：卡片里直接画那条公式（同行两条引用才分得开） */
    latex?: string | null;
    /** 这条引用覆盖的整段行 id（逗号分隔）：卡片据此把标题和它下面的正文一起显示 */
    rows?: string | null;
  }): void;
  (e: 'peek-hide'): void;
}>();

const root = ref<HTMLElement | null>(null);

/** 材料类型 → 悬停提示里的短名（后端 chunk_type 的值域；不认识的就不写） */
const TYPE_LABEL: Record<string, string> = {
  formula: '公式', table: '表格', figure: '插图', unit: '段落', text: '段落',
};

/**
 * 旧产物的引用写法：「（来源 Chunk: A023, A031）」。
 *
 * 2026-11-28 起提示词只让模型写 「[A023][A031]」，但**库里已有的解读全是旧写法** ——
 * 解读不该因为换了提示词就继续显示一坨「（来源 Chunk: …）」原文，所以渲染这侧两种写法
 * 都归一成方括号，顺带让这个词从界面上彻底消失。
 */
const LEGACY_GROUP = /[（(]\s*来源\s*Chunk\s*[:：]\s*([A-Za-z]\d{2,4}(?:\s*[,，、;；]\s*[A-Za-z]\d{2,4})*)\s*[）)]/g;
const LEGACY_BARE = /来源\s*Chunk\s*[:：]\s*([A-Za-z]\d{2,4})/g;

function normalizeMarks(md: string): string {
  if (!/Chunk/i.test(md)) return md;
  return md
    .replace(LEGACY_GROUP, (_m, codes: string) =>
      String(codes).split(/[,，、;；]/).map((c) => '[' + c.trim().toUpperCase() + ']').join(''))
    .replace(LEGACY_BARE, (_m, code: string) => '[' + String(code).toUpperCase() + ']');
}

/** 编号 → 引用位置（宿主把 stale_refs 排在前面、refs_resolved 排在后面，有效的覆盖过期的） */
const refMap = computed(() => {
  const map = new Map<string, UnderstandingRefItem>();
  for (const item of props.refs ?? []) {
    const code = String(item?.code ?? '').toUpperCase();
    if (code) map.set(code, item);
  }
  return map;
});

function escapeAttr(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/**
 * 正文里的引用标号：`[A023]`（单块）或 `[A012,A013]`（同一处连续材料，**一个方括号 + 半角逗号**）。
 *
 * 「并排的多个方括号」`[A012][A013]` **不是**一组 —— 那是两枚标号，各编各的号。
 */
const CITE_GROUP = /\[([A-Z]\d{2,4}(?:\s*[,，]\s*[A-Z]\d{2,4})*)\]/g;

/**
 * 正文里的一处引用 → 可点的**跳转标号**「[n]」。
 *
 * 为什么在渲染后的 HTML 上换、而不是给 marked 加一条行内扩展（正文里的参考文献标号
 * 就是那么做的）：序号要按**这一篇里第一次出现**的顺序编（1、2、3…），还得把编号查成
 * 页码/块 id —— 那条链路是纯函数、拿不到 refs。这里只认方括号里带字母的编号，
 * 与「[2, 4]」那套参考文献标号（数字）天然不冲突；代码块里的方括号照样跳过。
 *
 * 一组连续材料（`[A012,A013]`）只画**一枚**按钮、只占**一个**号：组不是新的短码，
 * 只是一个方括号里并列了几个短码（`refs_json` 的 `codes` 里那几个照样都在）。
 * 要闪的那几行 = 组内每个短码的 `row_ids`（或 `pids`）**并集**，挂在 `data-rows` 上 ——
 * 宿主（阅读器）按它闪整组，所以点击/悬浮的上报一个字都不用改。
 *
 * 查不到位置的编号也照样编上号（只是点了不跳）：它们本来就是模型编出来的/
 * 已被重建索引冲掉的，直接从正文里抹掉反而会让用户以为漏了内容。
 * 组里**只要有一个**短码查不到位置，就整组不合并、逐码各画一枚 —— 绝不吞号。
 */
function decorateCites(html: string): string {
  const parts = html.split(/(<code[\s\S]*?<\/code>|<pre[\s\S]*?<\/pre>)/i);
  const order = new Map<string, number>();
  /** 编号键 → 号。单块用短码本身，组用**排序后的**短码串（同一组出现两次仍是同一个号）。 */
  const numberOf = (key: string): number => {
    let n = order.get(key);
    if (!n) {
      n = order.size + 1;
      order.set(key, n);
    }
    return n;
  };
  return parts.map((part, index) => {
    if (index % 2 === 1) return part;                      // 代码块原样返回
    return part.replace(CITE_GROUP, (whole, group: string) => {
      const codes = String(group).split(/[,，]/).map((c) => c.trim().toUpperCase()).filter(Boolean);
      if (!codes.length) return whole;
      /* 组里有一个短码查不到位置 → 退回"逐码一枚"（今天的行为：不合并、不吞号） */
      const allKnown = codes.length > 1 && codes.every((c) => !!refMap.value.get(c));
      if (!allKnown) {
        return codes.map((code) => citeMark(code, [code], numberOf(code))).join('');
      }
      return citeMark(codes[0], codes, numberOf([...codes].sort().join(',')));
    });
  }).join('');
}

/**
 * 一枚标号按钮。`group` 是这一枚代表的短码（单块只有一个，组是括号里那几个）。
 *
 * `data-block` 取**第一个**短码的落点（跳转只去一处，镜头别来回拽）；
 * `data-rows` 是组内所有短码的行 id 并集（去重，按文档顺序）—— 宿主据此闪整组。
 */
function citeMark(primary: string, group: string[], n: number): string {
  const refs = group
    .map((code) => refMap.value.get(code))
    .filter((r): r is UnderstandingRefItem => !!r);
  const ref = refMap.value.get(primary);
  /* 页码去重：同一页上相邻的几块写进一个括号时，"第 3 页、第 3 页"是废话 */
  const pages: number[] = [];
  for (const r of refs) {
    const page = Number(r.page) || 0;
    if (page > 0 && !pages.includes(page)) pages.push(page);
  }
  const where = pages.map((p) => '第 ' + p + ' 页').join('、');
  const kind = TYPE_LABEL[String(ref?.chunk_type ?? '')] || '';
  const tip = [where, kind].filter(Boolean).join(' · ');
  const title = ref ? (tip ? tip + ' · 跳到原文' : '跳到原文') : '这一条引用已不在清单里';
  /* 落点优先用**正文行 id**（后端从 chunk 元数据补的），块 id 只当兜底：
     公式 chunk 的块 id 是 row-xxx 或 …-x01 这类伪块 id，阅读器翻不成行 ——
     那正是「点标号却整页闪」的原因（用户 2026-12-03）。 */
  const anchor = String(ref?.row_id || ref?.block_id || '');
  /* 组内每一块的落点：**正文行 id 优先**（`row_ids` 是后端从 chunk 元数据补的复数形式，
     `row_id` 是单数形式 —— 实测清单里只有后者，前者常常缺），pid 只当最后兜底。
     为什么必须优先行 id：pid（`r-1a2b…`）在阅读器里**不是行 id**，`resolveRefId` 认不出，
     会退到"那一页的首行" —— 用户 2026-12-27 实测：标号 45 的浮窗内容是对的、闪的却是一张**插图**
     （第 8 页首行正好是图）。 */
  const rows: string[] = [];
  for (const r of refs) {
    const own = (r.row_ids?.length ? r.row_ids : undefined)
      ?? (r.row_id ? [r.row_id] : undefined)
      ?? r.pids
      ?? [];
    for (const id of own) {
      const rid = String(id || '');
      if (rid && !rows.includes(rid)) rows.push(rid);
    }
  }
  /* 公式的 LaTeX 只在**单独一个短码**时带：多码组里带上第一条公式，点了会闪到别的块上去 */
  const latex = group.length === 1 ? String(ref?.latex ?? '') : '';
  return '<button type="button" class="cite-inline ins-cite"'
    /* aria-label 而不是 title：title 会弹浏览器原生那一条灰气泡，而悬浮标号本来就会弹速览卡 */
    + ' aria-label="' + escapeAttr(title) + '"'
    + ' data-code="' + primary + '"'
    + ' data-block="' + escapeAttr(anchor) + '"'
    + ' data-page="' + (pages.length ? pages[0] : '') + '"'
    + ' data-latex="' + escapeAttr(latex) + '"'
    + ' data-rows="' + escapeAttr(rows.join(',')) + '">[' + n + ']</button>';
}

/** 点标号：只上报，不自己滚 —— 整页那侧根本没有阅读器，滚不动的活儿得宿主决定 */
function onCitePeek(ev: PointerEvent) {
  const btn = (ev.target as HTMLElement | null)?.closest?.('.ins-cite') as HTMLElement | null;
  if (!btn) return;
  const r = btn.getBoundingClientRect();
  const page = Number(btn.dataset.page || 0);
  emit('peek', {
    anchor: { x: r.left, y: r.top, w: r.width, h: r.height },
    page: page > 0 ? page : null,
    rowId: btn.dataset.block || null,
    blockId: btn.dataset.code || null,
    label: btn.getAttribute('aria-label') || '',
    latex: btn.dataset.latex || '',
    rows: btn.dataset.rows || '',
  });
}

function onCitePeekOut() { emit('peek-hide'); }

function onCiteClick(ev: MouseEvent) {
  const btn = (ev.target as HTMLElement | null)?.closest?.('.ins-cite') as HTMLElement | null;
  if (!btn) return;
  const page = Number(btn.dataset.page || 0);
  emit('cite', {
    code: btn.dataset.code || '',
    block: btn.dataset.block || '',
    page: page > 0 ? page : 0,
    latex: btn.dataset.latex || '',
    rows: btn.dataset.rows || '',
  });
}

/**
 * markdown → HTML。
 *
 * 表格外面那层横向滚动容器与「三线表」样式**不再由这里负责**：mdToHtml 现在统一把
 * <table> 包进 .tbl-wrap（见 useMarkdown.decorateTables），样式在 base.css 的 .md table。
 *
 * 引用标记在这之后换成一枚枚标号（decorateCites）—— 顺序不能反：先换会把 HTML 弄脏，
 * marked 再把按钮当文本转义一次就全废了。
 */
/** 编号 → 图地址（键统一大写，模型写成小写也能对上） */
const imageMap = computed(() => {
  const map = new Map<string, string>();
  for (const [code, url] of Object.entries(props.images ?? {})) {
    const key = String(code || '').toUpperCase();
    const href = String(url || '');
    if (key && href) map.set(key, href);
  }
  return map;
});

/** `<img src="img:A012" alt="图注">`：marked 对 `![alt](img:A012)` 的产物 */
const FIG_IMG = /<img\b([^>]*?)\bsrc="img:([A-Za-z]\d{2,4})"([^>]*?)>/g;

function attrOf(attrs: string, name: string): string {
  const m = new RegExp(name + '="([^"]*)"').exec(attrs);
  return m ? m[1] : '';
}

/** `![图注](img:A012)` 的图注**原文**（含公式的 `$…$`）。
 *
 * 为什么从 markdown 原文取、而不是读渲染后的 `alt`：marked 的 katex 扩展会在**图注里**
 * 也接管 `$…$`，于是 `alt` 属性里躺着的是一整段转义过的 KaTeX HTML ——
 * 实测「图 2：… KV cache 中 `$QK^\top$` 计算…」的 alt 里就是 `<span class="katex">…<annotation`，
 * 原样塞进 `<figcaption>` 后浏览器把它当**文字**显示出来（用户 2026-12 的截图）。
 * 图注只认原文，公式再单独走一遍 `mdToHtml` 渲染成真正的公式。 */
function collectCaptions(md: string): Map<string, string> {
  const out = new Map<string, string>();
  const re = /!\[([^\]\n]*)\]\(img:([A-Za-z]\d{2,4})\)/g;
  let m = re.exec(md);
  while (m) {
    out.set(m[2].toUpperCase(), m[1]);
    m = re.exec(md);
  }
  return out;
}

/** 图注 → `<figcaption>` 里的 HTML：公式渲染成 KaTeX，文字仍是文字（`<p>` 壳去掉）。 */
function captionHtml(raw: string): string {
  const html = mdToHtml(raw, props.paperId);
  const one = /^<p>([\s\S]*)<\/p>\s*$/.exec(html);
  return one ? one[1] : html;
}

/** 图注的纯文本版（`alt` 属性 / 大图标题用）：KaTeX 的 HTML 一律不要，只留公式源码。 */
function captionText(raw: string): string {
  return raw.replace(/\$([^$]+)\$/g, '$1').replace(/\s+/g, ' ').trim();
}

/**
 * 正文里的「`![图注](img:A012)`」→ **真正的裁图 + 图注**（用户口径 2026-11：解读要能引用并显示图片，
 * 像对话页那样）。
 *
 * 为什么在渲染后的 HTML 上换、而不是给 marked 加一条图片扩展：短码 → 地址的映射只有后端有
 * （区域裁图要图注锚定 + 版本号），marked 那条链路拿不到；而且代码块里的 `img:` 不该被换。
 * 所以跟 `decorateCites` 同一层处理。
 *
 * 换不到的（清单里那一条没有图）**整块删掉**：宁可少一张图，也不要一个裂图标 ——
 * 后端 `clean_image_refs` 已经先删过一遍，这里是兜底（老解读、旧清单）。
 */
function decorateFigures(html: string, captions: Map<string, string>): string {
  if (!html || !html.includes('img:')) return html;
  const parts = html.split(/(<code[\s\S]*?<\/code>|<pre[\s\S]*?<\/pre>)/i);
  return parts.map((part, index) => {
    if (index % 2 === 1) return part;              // 代码块原样返回
    return part.replace(FIG_IMG, (whole: string, before: string, rawCode: string, after: string) => {
      const code = rawCode.toUpperCase();
      const url = imageMap.value.get(code);
      if (!url) return '';
      const attrs = before + ' ' + after;
      const raw = captions.get(code);
      /* 图注有两份用处：`<figcaption>` 要**渲染过的**（公式才是公式），
         `alt` / 大图标题要**纯文本**（读屏与浮层标题里不能有 HTML）。 */
      const cap = raw ? '<figcaption>' + captionHtml(raw) + '</figcaption>' : '';
      const alt = raw ? captionText(raw) : decodeAttr(attrOf(attrs, 'alt'));
      return '<figure class="ins-fig"><img class="ins-fig-img" src="' + escapeAttr(url)
        + '" alt="' + escapeAttr(alt) + '" data-code="' + code + '" loading="lazy">' + cap + '</figure>';
    });
  }).join('');
}

function render(md?: string | null): string {
  const src = String(md ?? '');
  return decorateCites(
    decorateFigures(mdToHtml(normalizeMarks(src), props.paperId), collectCaptions(src)),
  );
}

const html = computed(() => (String(props.markdown ?? '').trim() ? render(props.markdown) : ''));

/* ------------------------------ 点图放大 ------------------------------
 * 复用阅读器那套大图查看器（缩放 / 双指 / 拖动 / 左右切图都在里面，见 `reader/ImageViewer`）：
 * 解读里的插图在整页视图下可能比正文窄一半，不放大看不清数字与坐标轴。
 * 图集 = 正文里**实际渲染出来**的那几张（按出现顺序），左右切的就是"这份解读里的图"。
 */
const zoom = ref<ZoomTarget | null>(null);
const zoomOrigin = ref<{ left: number; top: number; width: number; height: number } | null>(null);

function decodeAttr(text: string): string {
  return text.replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

const docFigures = computed<ZoomTarget[]>(() => {
  const out: ZoomTarget[] = [];
  const re = /<img class="ins-fig-img" src="([^"]*)" alt="([^"]*)"/g;
  let m = re.exec(html.value);
  while (m) {
    out.push({ url: decodeAttr(m[1]), caption: decodeAttr(m[2]) });
    m = re.exec(html.value);
  }
  return out;
});

function onFigureClick(ev: MouseEvent): void {
  const img = (ev.target as HTMLElement | null)?.closest?.('.ins-fig-img') as HTMLImageElement | null;
  if (!img) return;
  const r = img.getBoundingClientRect();
  zoomOrigin.value = { left: r.left, top: r.top, width: r.width, height: r.height };
  zoom.value = { url: img.getAttribute('src') || '', caption: img.getAttribute('alt') || '' };
}

/** 正文点击：标号跳原文 + 点图放大（一个 @click 里分派，两者互不干扰） */
function onDocClick(ev: MouseEvent): void {
  onCiteClick(ev);
  onFigureClick(ev);
}

/**
 * 给渲染出来的标题**按序贴 id**（id 由 `buildInsightOutline` 生成）。
 *
 * 为什么在 DOM 上贴而不是改 `useMarkdown` 的渲染产物：`mdToHtml` 那条链路是多年的不变量
 * （阅读器正文、Markdown 视图、图注都吃它），为一个 anchor 去动它有连带风险；
 * 而这里 DOM 顺序与大纲顺序天然一致（同一份 markdown、同一个纯函数），贴一遍就够了。
 */
function assignAnchors() {
  const box = root.value;
  if (!box) return;
  const heads = props.markdown
    ? buildInsightOutline(props.markdown, { prefix: props.anchorPrefix })
    : [];
  Array.from(box.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6')).forEach((el, i) => {
    const id = heads[i]?.id;
    if (id) el.id = id;
  });
  // 贴完就通知宿主：目录那边要立刻按"阅读线"点亮一条，否则首屏一个高亮都没有（要等用户滚一下）
  emit('anchors-ready');
}
watch(html, () => { void nextTick(assignAnchors); });
onMounted(() => { void nextTick(assignAnchors); });
</script>

<template>
  <div ref="root" class="doc" :class="{ narrow }" @click="onDocClick"
     @pointerover="onCitePeek" @pointerout="onCitePeekOut">
    <div v-if="error" class="item err">
      <div class="tiny">{{ error }}</div>
      <!-- 只在"确实卡在前置条件"时补这句：网络抖动也挂一句"先建索引"会把人引到错的方向 -->
      <div v-if="/索引|index|段落/i.test(error)" class="tiny faint">
        前置条件：这篇论文要先建过知识库索引（解读读的是索引里的段落，不是页图）。
      </div>
    </div>
    <div v-else-if="timedOut" class="hint">
      已等待 4 分钟仍没拿到结果，任务可能还在跑 —— 稍后再看。
    </div>

    <!-- 还没有解读：空状态也要能往下走（口径：这里保留「开始解读」，它不是"重新生成"） -->
    <div v-if="loading && empty" class="empty small">正在读取解读…</div>
    <div v-else-if="empty" class="item">
      <div class="tiny faint">还没有解读</div>
      <div class="tiny faint note">
        解读读的是知识库索引里的段落（不是页图），所以需要先对这篇论文建索引。
      </div>
      <button class="btn sm primary" :disabled="starting" @click="emit('start')">
        {{ starting ? '已发起…' : '开始解读' }}
      </button>
      <!-- 发起后要一次全篇调用（实测 1~2 分钟）：说清楚"已经开始了、会自动刷"，
           否则界面还是那四个字，看起来像没点动。这不是动作按钮，只是一行提示。 -->
      <div v-if="running || starting" class="tiny faint note">
        解读任务在跑（实测 1~2 分钟），完成后会自动刷新。
      </div>
    </div>

    <template v-else>
      <div v-if="!html" class="hint">还没有生成「论文解读」。</div>
      <div v-if="html" class="body md" v-html="html" />
    </template>

    <!-- 点插图放大：与阅读器同一套查看器（胶囊里有放大 / 百分比 / 缩小 / 重置 / 关闭） -->
    <ImageViewer
      v-if="zoom"
      :target="zoom"
      :gallery="docFigures"
      :origin="zoomOrigin"
      icon="image"
      :title="zoom.caption || '插图'"
      @close="zoom = null"
    />
  </div>
</template>

<style scoped>
/* 字号跟着阅读器的 `--reader-font-size`（宿主设；取不到时 15px 与阅读器中档接近）。
   下面所有排版都用 em 相对它缩放 —— 与阅读器"标题/图注/公式一起缩放"同一口径。 */
.doc { font-size: calc(var(--reader-font-size, 15px) * var(--ts, 1)); color: var(--text); min-width: 0; }
/* 整页阅读：正文列按阅读器的文本列行宽收窄（`.pg-rows:not(.two-col)` 用的是 880px） */
.doc.narrow { max-width: 880px; margin: 0 auto; }

.item {
  border: 1px solid var(--border); border-left: 2px solid var(--border-strong);
  border-radius: var(--radius-sm); background: var(--bg-layer);
  padding: 8px 10px; margin-bottom: 8px;
}
.item.err { border-left-color: var(--danger); }
.hint { font-size: .82em; color: var(--text-faint); line-height: 1.5; margin: 0 0 8px; }
.note { margin: 4px 0 6px; line-height: 1.5; }

/* 正文排版：数值与阅读器 ReaderRow 的 `.rr-body` 一一对应（见文件头注释）。 */
.body { line-height: 1.78; overflow-wrap: anywhere; line-break: strict; text-autospace: normal; }
/* 解读里的插图（正文 `![图注](img:A012)`）：与正文同宽、圆角描边，图注在下面居中。
   图来自后端「按图注锚定的区域裁图」，尺寸不定，所以只限宽不限高（宽图自己缩、窄图别被拉大）。 */
.body :deep(.ins-fig) { display: grid; justify-items: center; gap: 6px; margin: 14px 0 16px; }
.body :deep(.ins-fig-img) {
  display: block; max-width: 100%; width: auto; height: auto;
  border: 1px solid var(--border); border-radius: 10px; background: var(--bg-elev);
  cursor: zoom-in;
}
.body :deep(.ins-fig-img:hover) { border-color: var(--border-strong); }
.body :deep(.ins-fig figcaption) {
  font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint);
  text-align: center; max-width: 46em;
}
.body :deep(p) { margin: 0 0 .55em; }
.body :deep(h1) { font-size: 1.4em; }
.body :deep(h2) { font-size: 1.24em; }
.body :deep(h3) { font-size: 1.1em; }
.body :deep(h4), .body :deep(h5), .body :deep(h6) { font-size: 1em; }
.body :deep(h1), .body :deep(h2), .body :deep(h3), .body :deep(h4),
.body :deep(h5), .body :deep(h6) { margin: 0 0 .35em; font-weight: 600; line-height: 1.35; }
.body :deep(ul), .body :deep(ol) { margin: .3em 0; padding-left: 1.35em; }
.body :deep(.katex-display) { margin: .5em 0; overflow-x: auto; overflow-y: hidden; }
/* 行内公式比一行还高时（分式、大括号、\left( … \right)），浏览器不会把行盒撑高 ——
   上一行末尾的括号、逗号就压在公式身上（用户口径 2026-12-05 的"括号与公式重叠"，老毛病）。
   让 .katex 成为 inline-block：行盒按它的外接框让位，上下不再互相压。
   实测：4 条 h≈41–44px 的公式（行高 23px）改动前 4 条全压住上一行，改动后 0 条。 */
.body :deep(.katex) { display: inline-block; }
/* 分隔线（`---`）由 base.css 的 `.md hr` 统一画（以前这里把它藏成 12px 空白）。 */
/* 表格：与阅读器 / Markdown 视图**共用** base.css 的那套三线表（见 `.md table`），不在这里另写一套 */
</style>
