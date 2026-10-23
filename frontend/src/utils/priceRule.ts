/**
 * 分时段（峰谷）单价的**草稿行** + 本地预校验。
 *
 * 口径与后端 `services/model/price_rules.py` 一致，只在这里抄一份，别处不再实现：
 *   · 模型上的四个单价 = **时段之外**的价（DeepSeek 口径里就是空闲价）；
 *   · 时段规则 = **例外覆盖**，且**只对工作日（周一–周五）生效**，周末一律落到默认价；
 *   · 同一模型的时段之间**不许重叠**（跨午夜的段先拆成两段再比）。
 *
 * 这里的校验只负责"省一次往返"：真正判定以后端 400 的 message 为准（原样展示），
 * 所以文案写得直白，不追求与后端逐字相同。
 */

/** 编辑器里的一行（全是字符串：输入框绑的就是字符串，提交时才转数字） */
export interface PriceRuleDraft {
  label: string;
  start: string;
  end: string;
  price_in: string;
  price_out: string;
  price_cache_read: string;
  price_cache_write: string;
}

/** 后端认的 HH:MM（两位小时 + 两位分钟，24 小时制）—— 与 billing.parse_hhmm 同一个口径 */
export const HHMM_RE = /^([01]\d|2[0-3]):([0-5]\d)$/;

const MINUTES_PER_DAY = 24 * 60;

export function emptyRule(): PriceRuleDraft {
  return { label: '', start: '', end: '', price_in: '', price_out: '', price_cache_read: '', price_cache_write: '' };
}

function textOf(value: unknown): string {
  if (value === null || value === undefined || value === '') return '';
  const n = Number(value);
  return Number.isFinite(n) ? String(n) : String(value);
}

/** 接口里的 items[] → 草稿行（GET /models/{id}/price-rules 与 GET /models/{id} 的 price_rules 同形） */
export function draftOf(row: Record<string, any> | null | undefined): PriceRuleDraft {
  const r = row || {};
  return {
    label: String(r.label ?? ''),
    start: String(r.start ?? ''),
    end: String(r.end ?? ''),
    price_in: textOf(r.price_in),
    price_out: textOf(r.price_out),
    price_cache_read: textOf(r.price_cache_read),
    price_cache_write: textOf(r.price_cache_write),
  };
}

/** 单价字符串 → 数字：空 = 未配置（null），非法（非有限数 / 负数）= ok:false */
export function priceValue(raw: unknown): { ok: boolean; value: number | null } {
  const s = String(raw ?? '').trim();
  if (!s) return { ok: true, value: null };
  const n = Number(s);
  if (!Number.isFinite(n) || n < 0) return { ok: false, value: null };
  return { ok: true, value: n };
}

export function minutesOf(text: unknown): number | null {
  const matched = HHMM_RE.exec(String(text ?? '').trim());
  if (!matched) return null;
  return Number(matched[1]) * 60 + Number(matched[2]);
}

/** 一段时段 → 一天之内的若干左闭右开区间（跨午夜拆两段，与后端 rule_intervals 同算法） */
export function intervalsOf(startMin: number, endMin: number): [number, number][] {
  if (startMin < endMin) return [[startMin, endMin]];
  return [[startMin, MINUTES_PER_DAY], [0, endMin]];
}

export function rulesOverlap(aStart: number, aEnd: number, bStart: number, bEnd: number): boolean {
  const left = intervalsOf(aStart, aEnd);
  const right = intervalsOf(bStart, bEnd);
  return left.some(([a0, a1]) => right.some(([b0, b1]) => a0 < b1 && b0 < a1));
}

export interface RuleIssue {
  /** 第几行（0 基）；-1 = 不属于某一行的整体性错误 */
  index: number;
  message: string;
}

/** 逐条 + 两两判重叠，返回全部问题（编辑器按行取第一条显示，提交时取第一条拦下） */
export function ruleIssues(rows: PriceRuleDraft[]): RuleIssue[] {
  const issues: RuleIssue[] = [];
  const spans: ({ start: number; end: number } | null)[] = [];

  rows.forEach((row, i) => {
    const n = i + 1;
    const start = minutesOf(row.start);
    const end = minutesOf(row.end);
    if (start === null) issues.push({ index: i, message: '第 ' + n + ' 行：开始时间要是 HH:MM 的 24 小时时刻（如 09:00）' });
    if (end === null) issues.push({ index: i, message: '第 ' + n + ' 行：结束时间要是 HH:MM 的 24 小时时刻（如 18:00）' });
    if (start !== null && end !== null && start === end) {
      issues.push({ index: i, message: '第 ' + n + ' 行：开始与结束相同（零长度时段永远命中不了）' });
    }
    spans.push(start !== null && end !== null && start !== end ? { start, end } : null);

    const pin = priceValue(row.price_in);
    const pout = priceValue(row.price_out);
    if (!pin.ok) issues.push({ index: i, message: '第 ' + n + ' 行：输入价必须是非负数字（可以填小数，如 0.02）' });
    else if (pin.value === null) issues.push({ index: i, message: '第 ' + n + ' 行：缺输入价（时段是整段覆盖，只配一半和没配一样）' });
    if (!pout.ok) issues.push({ index: i, message: '第 ' + n + ' 行：输出价必须是非负数字（可以填小数，如 4.5）' });
    else if (pout.value === null) issues.push({ index: i, message: '第 ' + n + ' 行：缺输出价（时段是整段覆盖，只配一半和没配一样）' });
    if (!priceValue(row.price_cache_read).ok) issues.push({ index: i, message: '第 ' + n + ' 行：缓存读价格必须是非负数字（留空 = 这一段不单独计价）' });
    if (!priceValue(row.price_cache_write).ok) issues.push({ index: i, message: '第 ' + n + ' 行：缓存写价格必须是非负数字（留空 = 这一段不单独计价）' });
  });

  for (let i = 0; i < spans.length; i += 1) {
    const left = spans[i];
    if (!left) continue;
    for (let j = i + 1; j < spans.length; j += 1) {
      const right = spans[j];
      if (!right) continue;
      if (rulesOverlap(left.start, left.end, right.start, right.end)) {
        issues.push({
          index: j,
          message: '第 ' + (j + 1) + ' 行与第 ' + (i + 1) + ' 行重叠（同一模型的时段之间不允许重叠）',
        });
      }
    }
  }
  return issues;
}

/** 提交时用的一句话（没有 → 空串） */
export function ruleDraftError(rows: PriceRuleDraft[]): string {
  return ruleIssues(rows)[0]?.message || '';
}

/** 某一行要显示的那条红字（没有 → 空串） */
export function ruleIssueAt(rows: PriceRuleDraft[], index: number): string {
  return ruleIssues(rows).find((item) => item.index === index)?.message || '';
}

/** 草稿行 → PUT body 里的 item（label 去空白、时间原样、价格空 = null） */
export function toRuleItem(row: PriceRuleDraft): Record<string, unknown> {
  return {
    label: String(row.label || '').trim(),
    start: String(row.start || '').trim(),
    end: String(row.end || '').trim(),
    price_in: priceValue(row.price_in).value,
    price_out: priceValue(row.price_out).value,
    price_cache_read: priceValue(row.price_cache_read).value,
    price_cache_write: priceValue(row.price_cache_write).value,
  };
}
