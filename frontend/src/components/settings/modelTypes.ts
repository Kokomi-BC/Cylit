/**
 * 模型提供方 / 密钥的共享类型。
 *
 * 形状**只照后端已冻结的契约**写（`GET /models/providers`、`GET /models/providers/{id}/keys`）：
 * 迁移期 / 老后端的字段可能缺席，所以能缺的一律可选，读的地方统一给空态兜底，
 * 绝不因为某个字段没返回就炸掉整块界面。
 *
 * 三个新组件（ProviderSelector / CredentialSelector / ProviderDrawer）共用这一份，
 * C 阶段的新增模型表单也直接 import 这里的类型。
 */

/** 一家提供方（官方的身份由后端目录判定，自定义来自 provider 表）。 */
export interface ProviderRow {
  id: string;
  kind: 'official' | 'custom';
  label: string;
  /** 品牌图标键（后端算的）；空串 = 前端用统一占位方块 */
  icon?: string;
  note?: string;
  /** `bearer` = 要密钥；`none` = 本机推理这类不需要密钥 */
  auth?: 'bearer' | 'none';
  base_url?: string | null;
  has_base_url?: boolean;
  /** 这家支持的任务类型 */
  types?: string[];
  /** 已配置的模型条数 */
  configured?: number;
  /** 已存密钥条数 */
  key_count?: number;
  /** 默认密钥（空 = 这家没有默认密钥） */
  default_key_id?: string | null;
  default_key_label?: string | null;
  /** 连接状态点：ok / error / unknown */
  status?: 'ok' | 'error' | 'unknown' | string;
}

/** 一条已存密钥。**永远没有明文**：`masked` 是 `••••a1b2` 这种掩码，可能为空串。 */
export interface KeyRow {
  id: string;
  provider_id: string;
  label: string;
  masked: string;
  is_default: boolean;
  /** ok / missing / invalid（后端枚举，缺省按"未测试"画） */
  status?: string | null;
  last_tested_at?: string | null;
  /** 迁移出来、还没确认归属的密钥（`GET /models/keys?unassigned=1`） */
  unassigned?: boolean;
}

/** `POST /models/{id}/test`、`POST /models/providers/{id}/test`、`POST /models/keys/{id}/test` 的公共返回。 */
export interface ProviderTestResult {
  ok?: boolean;
  status?: string;
  message?: string;
  count?: number;
}

/** `GET /models` 每行新增的密钥来源（后端算的，前端只负责画）。 */
export interface KeySource {
  mode: 'provider' | 'key' | 'none' | 'legacy';
  key_id?: string | null;
  key_label?: string | null;
  status?: 'ok' | 'missing' | 'invalid' | string;
}
