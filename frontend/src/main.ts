/** 应用入口：Vite + Vue 3 + TS。 */

import { createApp } from 'vue';
import App from '@/App.vue';
import { router } from '@/router';
import { reportError } from '@/stores/app';
import { restoreTabs } from '@/stores/tabs';
import { bootTheme } from '@/theme/theme';
import '@/styles/base.css';
import 'katex/dist/katex.min.css';

// 未捕获异常统一提示，避免"点了没反应"
window.addEventListener('unhandledrejection', (ev) => {
  const err = (ev as PromiseRejectionEvent).reason;
  if ((err as any)?.name === 'AbortError') return;
  reportError(err, '未处理的请求错误');
});
window.addEventListener('error', (ev) => {
  console.error('[window.error]', ev.error ?? ev.message);
});

restoreTabs();          // 多标签页：先把上次开着的标签恢复出来，再挂载
bootTheme();            // 换肤要在挂载前落值，否则会先闪一下默认配色
const app = createApp(App);
app.config.errorHandler = (err) => { reportError(err, '界面异常'); };
app.use(router);
app.mount('#app');
