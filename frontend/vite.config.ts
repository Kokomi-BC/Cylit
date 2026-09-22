import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

/**
 * 前端构建配置。
 *
 * 关键约定：
 * - `base: '/app/'`：构建产物由 FastAPI 同进程托管在 `/app/`（`backend/main.py::_mount_static`）；
 * - 生产构建输出 `frontend/dist/`，后端对该目录做「文件存在则返回文件，否则回退 index.html」（SPA 路由）；
 * - 开发模式：`npm run dev` 起 Vite（默认 5173），把 `/api` 与 `/papers` 代理到后端 8710，
 *   于是开发期也能拿到真实数据与论文资源，不必改前端代码。
 */
const BACKEND = process.env.PR_BACKEND || 'http://127.0.0.1:8710';

export default defineConfig({
  base: '/app/',
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
