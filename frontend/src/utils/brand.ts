/**
 * 品牌图标：`frontend/public/icon.png` —— 由仓库根的 `icon.png` 裁成 **Apple 应用图标那种
 * 连续圆角（squircle，圆角半径 = 边长的 22.37%、超椭圆指数 ≈5）**，四角透明。
 *
 * 为什么拼 `BASE_URL` 而不是写死 `/icon.png`：构建产物挂在 `/app/` 下
 * （`vite.config.ts` 的 `base`，后端同进程托管），写死根路径会 404。
 */
export const BRAND_ICON = `${import.meta.env.BASE_URL}icon.png`;
