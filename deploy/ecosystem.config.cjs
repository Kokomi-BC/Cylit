/**
 * PM2 应用清单（服务器 43.136.18.54）
 *
 *   pm2 start /root/deploy/ecosystem.config.cjs
 *   pm2 save
 *
 * 与既有的 fake-news-detector / firefly-blog / dust-analyzer 并存，只新增三个。
 */
const NODE = '/root/.nvm/versions/node/v25.6.1/bin/node';

module.exports = {
  apps: [
    {
      name: 'cylit-api',
      cwd: '/root/Cylit',
      script: '.venv/bin/python',
      args: 'cli/pr.py serve --host 127.0.0.1 --port 8710',
      interpreter: 'none',
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      watch: false,
      max_memory_restart: '1200M',
      kill_timeout: 8000,
      env: {
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED: '1',
        PR_DEBUG: 'false',
        PR_HOST: '127.0.0.1',
        PR_PORT: '8710',
        PR_LOG_LEVEL: 'INFO',
        // 数据库连接不在这里写死：/root/Cylit/.env 是唯一来源
        // （PM2 env 的优先级高于 .env，写两处迟早对不上）
        PR_USER_ID: '1',
      },
    },
    {
      // 问答 agent 故意独立成进程：它挂了，解析/翻译照样跑（见 scripts/start.bat 的注释）
      name: 'cylit-agent',
      cwd: '/root/Cylit',
      script: '.venv/bin/python',
      args: 'cli/pr.py agent',
      interpreter: 'none',
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      watch: false,
      max_memory_restart: '1200M',
      kill_timeout: 8000,
      env: {
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED: '1',
        PR_DEBUG: 'false',
        PR_LOG_LEVEL: 'INFO',
        PR_AGENT_HOST: '127.0.0.1',
        PR_AGENT_PORT: '8711',
        PR_USER_ID: '1',
      },
    },
    {
      name: 'portal-api',
      cwd: '/var/www/portal-api',
      script: 'server.mjs',
      interpreter: NODE,
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      watch: false,
      max_memory_restart: '200M',
      env: {
        PORT: '3090',
        HOST: '127.0.0.1',
        PORTAL_ROOT: '/var/www/portal',
        DETECT_ORIGIN: 'http://127.0.0.1:3000',
      },
    },
  ],
};
