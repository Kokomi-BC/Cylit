import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

/**
 * 前端构建配置。
 *
 * 关键约定：
 * - `base: '/cylit/'`：构建产物由 FastAPI 同进程托管在 `/cylit/`（`backend/main.py::UI_BASE`）。
 *   **两处必须逐字一致** —— 只改一边，浏览器会去根路径拿 assets，表现为白屏 + 一堆 404；
 * - 生产构建输出 `frontend/dist/`，后端对该目录做「文件存在则返回文件，否则回退 index.html」（SPA 路由）；
 * - 开发模式：`npm run dev` 起 Vite（默认 5173），把 `/api` 与 `/papers` 代理到后端 8710，
 *   于是开发期也能拿到真实数据与论文资源，不必改前端代码。
 */
const BACKEND = process.env.PR_BACKEND || 'http://127.0.0.1:8710';

export default defineConfig({
  base: '/cylit/',
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        // 把体积大且变化少的依赖单独切块，便于本地缓存
        manualChunks: {
          vue: ['vue', 'vue-router'],
          markdown: ['marked', 'marked-katex-extension', 'katex'],
        },
      },
    },
  },
  server: {
    // 固定绑到 127.0.0.1：默认只监听 localhost（可能解析成 ::1），
    // 那样 `http://127.0.0.1:5173/app/` 会连接被拒，文档里的地址就对不上了。
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/papers': { target: BACKEND, changeOrigin: true },
      '/health': { target: BACKEND, changeOrigin: true },
    },
  },
});
