/** 推理强度的**内置档位建议**（由低到高）。
 *
 * 为什么单列一份：模型表单的「填入内置档位」、阅读器聊天的等级菜单、Agent 设置里的解释档
 * 原来是三份各自写死的 `['off','low','medium','high','max']` 拷贝，加一档要改三处 ——
 * 已经漂过一次（用户口径 2026-10：「每个模型支持自定义的推理强度，比如有些是 xhigh」）。
 *
 * 厂商档位名并不统一：OpenAI 系是 minimal / low / medium / high，DeepSeek 用 off/thinking，
 * 有的家（含部分第三方网关）还认 **xhigh**。所以这里只是**建议值** ——
 * 模型自己配了 `parameters.reasoning_levels` 就以那份为准（前后端都这样取），
 * 下面这份只在"没配"时兜底，并给模型表单的「填入内置档位」当模板。
 */
export const BUILTIN_EFFORTS = [
  'off',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
] as const;

/** 模型表单里那一列输入框的占位符：顺手把 xhigh 这类不常见档位示范出来 */
export const EFFORT_VALUE_PLACEHOLDER = '值，如 xhigh';
