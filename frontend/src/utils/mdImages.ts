/**
 * 回答（对话里的 markdown）**渲染之后的图片加固**。
 *
 * 背景：后端清洗放开了"任意 https 图片"——站外图（别的论文库、模型随手写的地址）会原样留在
 * markdown 里。这带来两件事，都在前端兜：
 * ① **用户浏览器直连第三方**：请求带 `Referer`（本机地址 / 论文 id 全在里面）+ 真实 IP。
 *    一律 `referrerpolicy="no-referrer"`，跳转链一律 `rel="noopener noreferrer"`。
 * ② **裂图**：模型写出的地址常常根本不存在，浏览器只会渲染一个破图标 + alt。
 *    加载失败就把这张图换成一条**普通链接**（alt / "查看原图"）+ 一句极短提示，读者还能点开看。
 *
 * 为什么在渲染后的 DOM 上做、而不是给 marked 加图片扩展：
 * - 图片地址只有后端知道（站内裁图是 `img:A012` 短码 → 后端翻成地址，见 InsightDoc 那边），
 *   marked 那条链路拿不到最终 src；
 * - `error` 事件**只有 DOM 知道**，源里没有任何信息能预测这张图会不会 404。
 *
 * 幂等：加固过的 `<img>` 记 `data-mdimg`，换过的记 `data-mdimg-broken`。
 * 重复调用（每次内容更新都会调）不会重复挂监听、也不会把已经换好的链接再包一层。
 */

/** 已加固（属性 + 监听器都挂过了） */
const HARD = 'data-mdimg';
/** 已经换成了链接（同一张图只换一次） */
const BROKEN = 'data-mdimg-broken';
/** 备注：裂图后面那句提示的文案（很短，一行内） */
const HINT = '（图没加载出来，点开看原图）';

/** 把 `root` 里所有图片加固一遍。**可重复调用**：已经处理过的直接跳过。 */
export function hardenAnswerImages(root: HTMLElement | null | undefined): void {
  if (!root) return;
  for (const img of root.querySelectorAll<HTMLImageElement>('img')) hardenOne(img);
}

function hardenOne(img: HTMLImageElement): void {
  if (img.hasAttribute(BROKEN)) return;
  if (!img.hasAttribute(HARD)) {
    img.setAttribute(HARD, '1');
    img.setAttribute('referrerpolicy', 'no-referrer');
    img.setAttribute('loading', 'lazy');
    img.setAttribute('decoding', 'async');
    img.addEventListener('error', () => replaceWithLink(img), { once: true });
  }
  /* 监听器挂上之前就已经失败过的图（缓存里的 404 / 屏幕上已有的历史消息）：
     `complete` 为真而 `naturalWidth` 还是 0，就是"已经结束了但没画出像素"。
     没写 src 的图不算（那样它会一直是 0，换成链接就是误伤）。 */
  if (img.complete && img.naturalWidth === 0 && (img.getAttribute('src') || '').trim()) {
    replaceWithLink(img);
  }
}

/** 裂图 → 「链接 + 一句提示」。原地替换（同一个父节点、同一个位置），不留空位。 */
function replaceWithLink(img: HTMLImageElement): void {
  if (img.hasAttribute(BROKEN)) return;
  img.setAttribute(BROKEN, '1');
  const doc = img.ownerDocument;
  const src = (img.getAttribute('src') || '').trim();
  const label = (img.getAttribute('alt') || '').trim() || '查看原图';
  const frag = doc.createDocumentFragment();
  /* 外面已经套了 `<a>`（markdown 的 `[![alt](img)](link)`）就不能再套一层：
     内嵌 `<a>` 是非法结构，浏览器解析时会把 DOM 拆开。那种情况只留 alt 文字，
     跳转交给外面那层现成的链接。 */
  if (src && !img.closest('a')) {
    const a = doc.createElement('a');
    a.className = 'md-img-link';
    a.href = src;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = label;
    frag.append(a);
  } else {
    const text = doc.createElement('span');
    text.className = 'md-img-fallback';
    text.textContent = label;
    frag.append(text);
  }
  const hint = doc.createElement('span');
  hint.className = 'md-img-hint';
  hint.textContent = HINT;
  frag.append(hint);
  img.replaceWith(frag);
}

/**
 * 在**回答容器**上长期加固：返回一个停止函数。
 *
 * 两个入口缺一不可：
 * - **MutationObserver（childList）**：流式回答是"每落一批增量就重设最后一块的 innerHTML"，
 *   新插入的 `<img>` 只有 DOM 自己知道；靠组件里 watch + nextTick 会漏掉已封口块之后插进来的图。
 * - **捕获阶段的 `error` 监听**：`error` 不冒泡，但捕获阶段在祖先上收得到；这样"插进来 → 立刻失败"
 *   这种一帧内发生的事也不会漏（观察器回调要到下一帧才跑）。
 *
 * `selector` 只认回答正文（默认 `.md.body`，ReaderChat 的答案容器就是 `class="md body"`）：
 * 阅读器正文、PDF 页图、裁图卡片都不在这棵树下，一个都不动。
 */
export function watchAnswerImages(root: HTMLElement | null | undefined, selector = '.md.body'): () => void {
  if (!root) return () => {};
  const hardenAll = (t?: HTMLElement) => { if (t) hardenAnswerImages(t); };
  root.querySelectorAll<HTMLElement>(selector).forEach(hardenAll);

  const pending = new Set<HTMLElement>();
  let queued = false;
  const flush = () => {
    queued = false;
    const batch = [...pending];
    pending.clear();
    for (const el of batch) if (el.isConnected) hardenAnswerImages(el);
  };
  const mo = new MutationObserver((records) => {
    for (const rec of records) {
      rec.addedNodes.forEach((node) => {
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        const el = node as HTMLElement;
        /* 两种插法都接住：插进正文里的（增量 / KaTeX 后渲染）和整块正文被换掉的（历史消息、切会话） */
        const host = (el.closest?.(selector) ?? el.querySelector?.(selector)) as HTMLElement | null;
        if (host) pending.add(host);
      });
    }
    if (!pending.size || queued) return;
    queued = true;
    requestAnimationFrame(flush);           // 一帧内的合成一次
  });
  mo.observe(root, { childList: true, subtree: true });

  const onError = (e: Event) => {
    const t = e.target;
    if (!(t instanceof HTMLImageElement) || !root.contains(t)) return;
    const host = t.closest<HTMLElement>(selector);
    if (host) hardenAnswerImages(host);
  };
  root.addEventListener('error', onError, true);

  return () => {
    mo.disconnect();
    root.removeEventListener('error', onError, true);
    pending.clear();
  };
}
