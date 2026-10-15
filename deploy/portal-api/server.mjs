#!/usr/bin/env node
/**
 * 门户管理 API（零依赖，node:http）。
 *
 * 只有三件事：
 *   GET  /Portal/api/status   服务器与 PM2 现状（只读）
 *   GET  /Portal/api/links    首页入口清单
 *   PUT  /Portal/api/links    改写首页入口清单（要求管理员）
 *
 * 管理员判定不自己验 JWT，而是拿请求里的 Cookie 去问 Detect 的
 * /auth/nginx/admin（204 = 管理员，403 = 普通用户，401 = 未登录）。
 * 这样只有一处密钥来源，门户不碰 JWT_SECRET。
 */
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { readFile, writeFile, rename } from 'node:fs/promises';
import { promisify } from 'node:util';
import path from 'node:path';

const run = promisify(execFile);

const PORT = Number(process.env.PORT || 3090);
const HOST = process.env.HOST || '127.0.0.1';
const ROOT = process.env.PORTAL_ROOT || '/var/www/portal';
const LINKS = path.join(ROOT, 'links.json');
const DETECT = process.env.DETECT_ORIGIN || 'http://127.0.0.1:3000';
const PM2_BIN = process.env.PM2_BIN || '/root/.nvm/versions/node/v25.6.1/bin/pm2';

const MAX_BODY = 256 * 1024;

function send(res, code, body, type = 'application/json; charset=utf-8') {
  const payload = typeof body === 'string' ? body : JSON.stringify(body);
  res.writeHead(code, {
    'content-type': type,
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
  });
  res.end(payload);
}

async function isAdmin(req) {
  const cookie = req.headers.cookie;
  if (!cookie) return { ok: false, code: 401, reason: '未登录' };
  try {
    const r = await fetch(`${DETECT}/auth/nginx/admin`, { headers: { cookie } });
    if (r.status === 204) return { ok: true, code: 204 };
    if (r.status === 403) return { ok: false, code: 403, reason: '当前账号不是管理员' };
    return { ok: false, code: r.status, reason: '登录已过期' };
  } catch {
    return { ok: false, code: 503, reason: '鉴权服务不可用' };
  }
}

async function pm2List() {
  try {
    const { stdout } = await run(PM2_BIN, ['jlist'], { maxBuffer: 8 * 1024 * 1024, timeout: 15000 });
    const apps = JSON.parse(stdout);
    return apps.map((a) => ({
      name: a.name,
      status: a.pm2_env?.status ?? 'unknown',
      cpu: a.monit?.cpu ?? 0,
      mem: a.monit?.memory ?? 0,
      restarts: a.pm2_env?.restart_time ?? 0,
      uptime: a.pm2_env?.pm_uptime ?? null,
      mode: a.pm2_env?.exec_mode ?? 'fork',
      script: a.pm2_env?.pm_exec_path ?? '',
    }));
  } catch (e) {
    return { error: String(e.message || e) };
  }
}

function systemInfo() {
  const os = globalThis.process;
  const mem = os.memoryUsage();
  return {
    node: os.version,
    pid: os.pid,
    uptimeSec: Math.round(os.uptime()),
    rss: mem.rss,
    now: new Date().toISOString(),
  };
}

async function hostInfo() {
  try {
    const [load, memInfo, disk] = await Promise.all([
      readFile('/proc/loadavg', 'utf8'),
      readFile('/proc/meminfo', 'utf8'),
      run('df', ['-B1', '--output=size,used,avail,pcent', '/'], { timeout: 8000 }),
    ]);
    const kv = Object.fromEntries(
      memInfo.split('\n').filter(Boolean).map((l) => {
        const [k, v] = l.split(':');
        return [k.trim(), parseInt(v, 10) * 1024];
      }),
    );
    const line = disk.stdout.trim().split('\n').pop().trim().split(/\s+/);
    return {
      load: load.trim().split(' ').slice(0, 3).map(Number),
      loadText: load.trim().split(' ').slice(0, 3).join(' '),
      mem: { total: kv.MemTotal, available: kv.MemAvailable },
      swap: { total: kv.SwapTotal, free: kv.SwapFree },
      disk: { total: Number(line[0]), used: Number(line[1]), avail: Number(line[2]), pcent: line[3] },
    };
  } catch (e) {
    return { error: String(e.message || e) };
  }
}

async function readLinks() {
  return JSON.parse(await readFile(LINKS, 'utf8'));
}

function validate(doc) {
  if (!doc || typeof doc !== 'object') throw new Error('payload 必须是对象');
  if (!Array.isArray(doc.links)) throw new Error('links 必须是数组');
  if (doc.links.length > 24) throw new Error('入口数量上限 24');
  const seen = new Set();
  doc.links = doc.links.map((l, i) => {
    const id = String(l.id || '').trim() || `svc-${i + 1}`;
    if (seen.has(id)) throw new Error(`重复 id：${id}`);
    seen.add(id);
    const name = String(l.name || '').trim();
    if (!name) throw new Error(`第 ${i + 1} 条缺少名称`);
    const href = String(l.href || '').trim();
    if (!/^(\/|https?:\/\/)/.test(href)) throw new Error(`「${name}」的路径必须以 / 或 http(s):// 开头`);
    const accent = /^#[0-9a-fA-F]{6}$/.test(String(l.accent || '')) ? l.accent : '#a78bfa';
    return {
      id,
      name: name.slice(0, 40),
      desc: String(l.desc || '').slice(0, 200),
      href,
      tag: String(l.tag || '').slice(0, 24),
      accent,
      probe: String(l.probe || href).slice(0, 200),
    };
  });
  const brand = doc.brand && typeof doc.brand === 'object' ? doc.brand : {};
  doc.brand = {
    name: String(brand.name || '服务门户').slice(0, 60),
    tagline: String(brand.tagline || '').slice(0, 120),
    eyebrow: String(brand.eyebrow || 'Server Portal').slice(0, 40),
  };
  return doc;
}

async function body(req) {
  const chunks = [];
  let n = 0;
  for await (const c of req) {
    n += c.length;
    if (n > MAX_BODY) throw new Error('请求体过大');
    chunks.push(c);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const p = url.pathname.replace(/\/+$/, '') || '/';

  try {
    if (p === '/health') return send(res, 200, { ok: true });

    if (p === '/api/links' && req.method === 'GET') return send(res, 200, await readLinks());

    if (p === '/api/links' && (req.method === 'PUT' || req.method === 'POST')) {
      const who = await isAdmin(req);
      if (!who.ok) return send(res, who.code === 204 ? 403 : who.code, { error: who.reason });
      const doc = validate(await body(req));
      const tmp = `${LINKS}.tmp`;
      await writeFile(tmp, `${JSON.stringify(doc, null, 2)}\n`, 'utf8');
      await rename(tmp, LINKS);
      return send(res, 200, { ok: true, links: doc.links.length });
    }

    if (p === '/api/status' && req.method === 'GET') {
      const who = await isAdmin(req);
      if (!who.ok) return send(res, who.code === 204 ? 403 : who.code, { error: who.reason });
      const [apps, host_] = await Promise.all([pm2List(), hostInfo()]);
      return send(res, 200, { apps, host: host_, portal: systemInfo() });
    }

    return send(res, 404, { error: 'not found' });
  } catch (e) {
    return send(res, 400, { error: String(e.message || e) });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[portal-api] http://${HOST}:${PORT}  root=${ROOT}  detect=${DETECT}`);
});
