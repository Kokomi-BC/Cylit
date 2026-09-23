/**
 * 构建前清空 `dist/`（由 `npm run build` 的 prebuild 钩子自动调用）。
 *
 * 为什么不直接用 Vite 的 `build.emptyOutDir: true`：
 * 它按目录逐项 `fs.rmSync(path, { recursive: true, force: true })`，而 Windows 上
 * **任何进程的当前工作目录都不能被删除**。只要有人在 `frontend/dist/assets` 里停过
 * （例如排查时执行过 `Set-Location ...\frontend\dist\assets`），`dist/assets` 就删不掉，
 * 整次 `rmSync` 直接失败；`force: true` 又把错误吞了 → Vite 报告「构建成功」，
 * 旧哈希文件却一个不少地留着（实测 dist 从 87 个涨到 124 个，几乎全是孤儿，
 * 在 git status 里表现为反复「删一批旧的、增一批新的」）。
 *
 * 所以这里改成「先删文件、再收空目录」：
 * 1) 递归**逐个文件**删除（一个文件删不掉不影响其它文件），删不掉的单独记下来重试；
 * 2) 自底向上删空目录（`dist/assets` 作为某个进程的 cwd 时删不掉也没关系：里面已经空了）；
 * 3) 仍有删不掉的文件时给出明确排查提示，但**不中断构建**（dist 里最多留点旧文件）。
 *
 * ⚠️ 删文件必须用 `unlinkSync`，删空目录必须用 `rmdirSync` —— **`rmSync` 在本项目的 Windows 环境下
 * 会"静默失败"**（不抛错、也不删）：实测反复 `rmSync(file, {maxRetries})` 后文件依然在。
 * 后果就是每轮构建的"清 dist"都是空操作，dist/assets 里攒到 **753 个孤儿哈希文件**（每改一行 CSS
 * 就多几个），排查时很迷惑。换成 unlinkSync 后一次就清干净了。
 */

import { existsSync, readdirSync, rmdirSync, statSync, unlinkSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const dist = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'dist');
const ROUNDS = 3;

/** 递归收集 dist 下的文件与目录（目录按「深的在前」排序，便于先清内容再删自己）。 */
function collect(dir) {
  const files = [];
  const dirs = [];
  const walk = (d) => {
    for (const name of readdirSync(d)) {
      const full = join(d, name);
      if (statSync(full).isDirectory()) {
        walk(full);
        dirs.push(full);
      } else {
        files.push(full);
      }
    }
  };
  walk(dir);
  return { files, dirs };
}

let removed = 0;
let stuck = [];

for (let round = 1; round <= ROUNDS; round += 1) {
  if (!existsSync(dist)) break;
  const { files, dirs } = collect(dist);
  if (!files.length && !dirs.length) break;

  removed = 0;
  stuck = [];
  for (const f of files) {
    try {
      unlinkSync(f);
      removed += 1;
    } catch {
      stuck.push(f);
    }
  }
  for (const d of dirs) {
    try {
      rmdirSync(d);
    } catch {
      /* 目录自身删不掉（多半是某个进程的 cwd）——里面已经空了，忽略 */
    }
  }

  if (!stuck.length) break;
  if (round < ROUNDS) {
    console.log(`[clean-dist] 第 ${round} 轮：${stuck.length} 个文件暂时删不掉，稍后重试 ...`);
    sleepSync(300 * round);
  }
}

if (!existsSync(dist)) {
  console.log(`[clean-dist] 已清空 dist/（删除 ${removed} 个文件）`);
} else if (stuck.length) {
  console.warn(
    `[clean-dist] [WARN] ${stuck.length} 个文件删不掉（被其它进程占用）：\n` +
      stuck
        .slice(0, 5)
        .map((f) => `           - ${f.slice(dist.length + 1)}`)
        .join('\n') +
      (stuck.length > 5 ? `\n           ... 其余 ${stuck.length - 5} 个` : '') +
      '\n           常见原因：某个终端 / 资源管理器的当前目录停在 frontend/dist 之下\n' +
      '           （Windows 不允许删除正被当作工作目录的目录），或浏览器、后端正在读这些文件。\n' +
      '           构建会继续，只是 dist 里会残留这些旧哈希文件；清理占用后再构建即可。',
  );
} else {
  console.log(`[clean-dist] dist/ 内容已清空（有目录作为工作目录被占用，已保留空目录）`);
}

/** 同步睡眠：Atomics.wait 不占 CPU（比空转 while 干净）。 */
function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}
