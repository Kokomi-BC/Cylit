/**
 * 论文自定义分类（文件夹）的会话内状态 + 请求：库页与侧栏读同一份，免得两边各拉一份互不同步。
 *
 * 请求放在这里而不是 `api/`：那个目录只有通用 `client.ts`，papers 的请求本来就由各视图直接调。
 */

import { reactive } from 'vue';
import { api } from '@/api/client';
import type { Paper, PaperFolder } from '@/api/types';
import { state } from '@/stores/app';

export interface FolderState {
  items: PaperFolder[];
  /** 未分类论文数（与各分类的 `paper_count` 同一口径：都来自 GET /folders） */
  unclassified: number;
  loading: boolean;
  error: string;
}

export const folderState = reactive<FolderState>({
  items: [],
  unclassified: 0,
  loading: false,
  error: '',
});

/** 首次成功拉取后置位：页面与侧栏先后挂载时只打一次后端。 */
let loaded = false;
/** 并发合并：两个入口同时挂载时共用一个在途请求。 */
let inflight: Promise<void> | null = null;

/** 拉分类列表（`force` = 忽略缓存）；失败只写 `error` 不抛，因为它总以 `void` 形式被挂载钩子调用。 */
export function loadFolders(force = false): Promise<void> {
  if (!force && loaded) return Promise.resolve();
  if (inflight) return inflight;
  folderState.loading = true;
  folderState.error = '';
  inflight = (async () => {
    try {
      const data = await api.get<{ items: PaperFolder[]; total: number; unclassified: number }>('/folders');
      folderState.items = data.items ?? [];
      folderState.unclassified = Number(data.unclassified ?? 0);
      loaded = true;
    } catch (err) {
      folderState.error = (err as Error)?.message ?? String(err);
    } finally {
      folderState.loading = false;
      inflight = null;
    }
  })();
  return inflight;
}

export async function createFolder(name: string): Promise<PaperFolder | null> {
  folderState.error = '';
  try {
    const folder = await api.post<PaperFolder>('/folders', { name: name.trim() });
    folderState.items = [...folderState.items, folder];
    return folder;
  } catch (err) {
    folderState.error = (err as Error)?.message ?? String(err);
    return null;
  }
}

export async function renameFolder(id: string, name: string): Promise<boolean> {
  folderState.error = '';
  try {
    const folder = await api.patch<PaperFolder>(`/folders/${id}`, { name: name.trim() });
    folderState.items = folderState.items.map((f) => (f.id === id ? { ...f, name: folder.name } : f));
    return true;
  } catch (err) {
    folderState.error = (err as Error)?.message ?? String(err);
    return false;
  }
}

/** 删分类 = 里面的论文回到未分类（后端只解绑，不删论文）。 */
export async function deleteFolder(id: string): Promise<boolean> {
  folderState.error = '';
  try {
    await api.del<{ moved: number }>(`/folders/${id}`);
    folderState.items = folderState.items.filter((f) => f.id !== id);
    for (const p of state.papers) if (p.folder_id === id) p.folder_id = null;
    return true;
  } catch (err) {
    folderState.error = (err as Error)?.message ?? String(err);
    return false;
  }
}

/** 批量归类（一次 POST，`null` = 移回未分类）→ 实际改到的篇数；失败返回 0，原因在 `error`。 */
export async function assignFolder(paperIds: string[], folderId: string | null): Promise<number> {
  const ids = paperIds.filter(Boolean);
  if (!ids.length) return 0;
  folderState.error = '';
  try {
    const res = await api.post<{ updated: number }>('/papers/folder', {
      paper_ids: ids,
      folder_id: folderId,
    });
    const updated = Number(res?.updated ?? 0);
    // 本地先对齐，再让后端重算一遍计数校准。
    for (const p of state.papers) if (ids.includes(p.id)) p.folder_id = folderId;
    void loadFolders(true);
    return updated;
  } catch (err) {
    folderState.error = (err as Error)?.message ?? String(err);
    return 0;
  }
}

/** 某个分类里的论文（`null` = 未分类）。读全局列表，不额外请求。 */
export function folderPapers(folderId: string | null): Paper[] {
  const id = folderId || null;
  return state.papers.filter((p) => (p.folder_id ?? null) === id);
}

/** 分类名；`null` 或已删的分类都落回「未分类」。 */
export function folderName(id: string | null): string {
  if (!id) return '未分类';
  return folderState.items.find((f) => f.id === id)?.name ?? '未分类';
}
