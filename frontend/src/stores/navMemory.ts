/**
 * 导航页的「上次分区」记忆（设置页的 `#kb` 这类 hash）。
 *
 * 为什么需要单独一层：标签条收窄成"只装论文标签"之后，导航页不再是标签 —— 标签那份
 * `query`/`hash` 记忆（`stores/tabs`）对它就没有载体了。可用户期望"从侧栏点回设置，
 * 还停在刚才那一区"。于是这里只存一件事：**路由名 → hash**。
 *
 * 论文页不用这一层（它们的 hash 由标签自己记，见 `stores/tabs` 的 `activateRoute`）。
 * 键名带 `shell.` 前缀，和 `pm.shell.tabs` 同一族，便于整体清理。
 */
const STORAGE_KEY = 'pm.shell.navhash';

function load(): Record<string, string> {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Record<string, string> : {};
  } catch {
    return {};    /* 隐私模式 / 脏数据：当成没有记忆，别让导航跟着崩 */
  }
}

/**
 * 记住某个导航页停在哪个分区。
 *
 * ⚠️ `hash` 为空时**不覆盖**已有记忆：从别处点一下「设置」（不带 hash、落默认分区）不该把
 * 用户上次记住的 `#kb` 抹掉 —— 抹掉之后"切回来还是那一区"就再也回不去了。
 */
export function rememberNavHash(name: string, hash: string): void {
  const key = String(name || '');
  const clean = String(hash || '');
  if (!key || !clean) return;
  try {
    const map = load();
    if (map[key] === clean) return;
    map[key] = clean;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch { /* 隐私模式忽略 */ }
}

/** 这个导航页上次停在哪一区（'' = 没记忆过 → 落默认分区）。 */
export function lastNavHash(name: string): string {
  const key = String(name || '');
  return key ? (load()[key] ?? '') : '';
}
