# Cylit 服务器部署（43.136.18.54）

腾讯云 Ubuntu 24.04，4 vCPU / 3.6 GB / 40 GB（已用 57%），
上面已经有三个 PM2 项目（Detect / Firefly / Dust），本次新增 **Cylit** 和 **门户** 两套。

---

## 1. 这台机器上现在跑着什么

| PM2 name | 端口 | 目录 | 对外入口 |
|---|---|---|---|
| `fake-news-detector` | 3000 | `/root/Detect` | `https://fakenewsdetect.art/` |
| `firefly-blog` | 3001 | `/root/Firefly` | `https://fakenewsdetect.art/Blog` |
| `dust-analyzer` | 8080 | `/root/Dust/…/frontend` | `https://fakenewsdetect.art/dust/` |
| **`cylit-api`** | 8710 | `/root/Cylit` | `https://fakenewsdetect.art/cylit/` |
| **`cylit-agent`** | 8711 | `/root/Cylit` | 内部，由 8710 转发 |
| **`portal-api`** | 3090 | `/var/www/portal-api` | `https://fakenewsdetect.art/Portal/api/` |

### 从「进门」到「找到东西」的路径

门户是这一站的**默认落点**：登录完、或者已登录时直接敲域名，都落到 `/Portal/`。
Detect 自己的首页退到 `/Welcome`（门户上那张 Detect 卡片就指向它）。

| 请求 | 未登录 | 已登录 |
|---|---|---|
| `/` | 302 → `/Login` | **302 → `/Portal/`** |
| `/Login` | 200（登录表单） | 302 → `/Portal/` |
| `/Welcome` | 302 → `/Login` | 200（Detect 首页） |
| `/Portal/` | 200（门户，隐藏「管理入口」） | 200（管理员多一个「管理入口」） |

> ⚠️ 门户上 Detect 那张卡片**不能**再指 `/` —— 已登录时 `/` 会跳回门户，变成一个死循环。
> 它在 `links.json` 里指向 `/Welcome`。

这些落点全在 Detect 里（`Login.html` + `src/app.js`），由 `deploy/patch-detect.py`
幂等改写（每处锚点必须在文件里**恰好出现一次**，否则整体跳过，绝不做半吊子替换）。

nginx 站点配置只有一个：`/etc/nginx/sites-available/detect.conf`
（`sites-enabled/` 里只有它）。门户和 Cylit 的 location 通过
`/etc/nginx/snippets/portal.conf`、`/etc/nginx/snippets/cylit.conf` include 进去，
由 `deploy/patch-nginx.py` 幂等注入。


**开放端口**：安全组只放了 80 / 443 / 26010 / 26020。
所以门户挂在**域名的子路径** `/Portal/`，没有新开端口、也没换证书。

---

## 2. 目录布局

```
/root/Cylit                    代码（git clone，origin = Kokomi-BC/Cylit）
  .venv                        Python 3.12 虚拟环境
  .env                         运行配置（600）
  data/secrets/                模型密钥（从本机搬过来的）
  data/papers/                 论文产物（服务器上重新解析出来的）
  frontend/dist                前端构建产物（从本机上传，服务器上没跑 npm build）
/var/www/portal                门户首页 + admin.html + links.json + 字体
/var/www/portal-api/server.mjs 门户管理 API（零依赖 node:http）
/root/deploy/ecosystem.config.cjs  PM2 清单
/root/deploy/patch-nginx.py        nginx 注入脚本
/etc/nginx/snippets/{portal,cylit}.conf
```

---

## 3. 部署时踩到的三个坑（都已解决，改环境时别再踩）

### 3.1 MySQL 的 root 是 auth_socket

`mysql -uroot -proot` 能进，是因为它走 unix socket + OS root 身份，
**密码根本没被校验**。换成 PyMySQL 走 TCP 立刻 `Access denied`。

所以单独建了业务账号：

```sql
CREATE USER 'cylit_user'@'localhost' IDENTIFIED BY '<pw>';
CREATE USER 'cylit_user'@'127.0.0.1' IDENTIFIED BY '<pw>';
GRANT ALL PRIVILEGES ON papermind.* TO 'cylit_user'@'localhost';
GRANT ALL PRIVILEGES ON papermind.* TO 'cylit_user'@'127.0.0.1';
```

口令写在 `/root/Cylit/.env` 的 `PR_DB_USER` / `PR_DB_PASSWORD`。
**PM2 的 env 里故意不再重复写库连接**——PM2 env 优先级高于 .env，写两处迟早对不上。

### 3.2 口令里不能有 `@`

后端把连接拼成 `user:password@host`，口令里的 `@` 会被 SQLAlchemy 当成分隔符：

```
Cylit@PM2026-8710  ->  Can't connect to MySQL server on 'PM2026-8710@127.0.0.1'
```

现在服务器上用的口令里只有字母数字和 `-`（**具体值见服务器上的 `/root/Cylit/.env`，
不进仓库**）。改口令时也请遵守这条：只用字母数字和 `-` `_`。

### 3.3 前端挂在 `/cylit/`，三处必须逐字一致

这个前缀散在三个地方，**改一处漏一处就是白屏 + 一堆 404**（浏览器会去根路径拿 assets）：

| 位置 | 值 |
|---|---|
| `frontend/vite.config.ts` | `base: '/cylit/'` |
| `frontend/index.html` | `href="/cylit/icon.png"` |
| `backend/main.py` | `UI_BASE = "/cylit"` |
| `frontend/src/router/index.ts` | `createWebHistory(import.meta.env.BASE_URL)` —— 跟着 base 走，不用改 |

改完必须 `npm run build` 重新出 `frontend/dist` —— `base` 是**构建期**常量，
只改 nginx 是没用的。

> 最早挂的是 `/app/`（零代码改动移植）。2026-10-15 换成项目名 `/cylit/`，
> nginx 里给旧路径留了 301（`location /app/ { rewrite ^/app/(.*)$ /cylit/$1 permanent; }`），
> 书签和深链不会断。

前缀冲突已核对 `/root/Detect/src/app.js`：

| 前缀 | 归属 | 为什么安全 |
|---|---|---|
| `/cylit/` | Cylit | Detect 没有这个路由 |
| `/api/v1/` | Cylit | Detect 的 Express 只挂 `/api/`，nginx 取最长前缀，`/api/v1/` 优先生效 |
| `/papers/` | Cylit | Detect 没有这个路由 |

### 3.4 证书到期了怎么换

证书是 TrustAsia 的 3 个月 DV 证书（当前那张 2026-10-15 → 2027-01-13），
腾讯云控制台下载 `fakenewsdetect.art_nginx.zip` 后：

```bash
# 先备份旧证书（.key 是私钥，别丢）
ssh 43.136.18.54 'cd /root/Detect/fakenewsdetect.art_nginx && \
  tar -czf ../cert-backup-$(date +%F-%H%M).tgz *.crt *.pem *.key *.csr'
# 传新的四个文件到同一目录，文件名一致（bundle.crt / bundle.pem / .key / .csr）
scp fakenewsdetect.art_bundle.crt fakenewsdetect.art_bundle.pem \
    fakenewsdetect.art.key fakenewsdetect.art.csr \
    43.136.18.54:/root/Detect/fakenewsdetect.art_nginx/
ssh 43.136.18.54 'nginx -t && systemctl reload nginx'
# 验收：nginx 实际发出去的那张
echo | openssl s_client -connect fakenewsdetect.art:443 -servername fakenewsdetect.art 2>/dev/null \
  | openssl x509 -noout -dates -issuer
```

**证书包不要留在仓库里**（含私钥）——`deploy/.gitignore` 已经挡了 `cert-new/`。

---

## 4. 门户（`/Portal/`）

### 4.1 鉴权：复用 Detect 的 JWT，不碰密钥

Detect 的登录态是 httpOnly cookie `token`（JWT，带 `role`）。
门户不自己验 JWT，而是分两层：

1. **nginx**：`if ($cookie_token = "") { return 302 /Login; }` —— 没登录直接去登录页；
2. **portal-api**：拿请求里的 Cookie 去问 `http://127.0.0.1:3000/auth/nginx/admin`，
   204 = 管理员、403 = 普通用户、401 = 过期。

好处是 `JWT_SECRET` 只有 Detect 一处，门户完全不知道它。

- 首页 `index.html` 调 `/auth/me`，`role === 'admin'` 才显示「管理入口」；
- `admin.html` 对普通用户显示 403 面板，不显示任何管理数据。

已存在的管理员账号（`detect_db.users`）：`Nahida`、`abc`、`15771364642@163.com`。

### 4.2 管理台能做什么

- **进程与负载**：PM2 各进程状态/CPU/内存/重启次数；内存、swap、根分区、load、portal-api 运行时长。
- **首页入口**：增删改首页的四个服务卡片（名称/路径/标签/主色/描述）+ 站点名/眉标/副标题，
  存回 `/var/www/portal/links.json`（先写 `.tmp` 再 `rename`，不会读到半个文件）。

### 4.3 首页设计

按 `Cylit/.agents/skills/high-end-visual-design`（Leonxlnx/taste-skill）：

- **Vibe**：Ethereal Glass —— OLED 黑 `#050505` + 三团径向 mesh + 胶片颗粒（`opacity .035`，固定层）
- **Layout**：Asymmetrical Bento —— 12 列栅格，卡片按 `7/5/5/7` 交替，刻意不对称
- **Double-Bezel**：每张卡是「外壳 `padding:8px` + 内芯」，两层同心圆角（`2rem` / `calc(2rem-.375rem)`）
- **Button-in-Button**：`进入 ↗` 的箭头包在独立圆形里，hover 时对角位移 + 缩放
- **Motion**：`cubic-bezier(.32,.72,0,1)`，IntersectionObserver 入场（位移 + 模糊 + 淡入），
  只动 `transform`/`opacity`
- **字体**：HarmonyOS Sans（直接复用 `/var/www/detect/font` 那套 ttf，不依赖 CDN）
- 768px 以下塌成单列，全部 `col-span` 归 1

---

## 5. 常用运维命令

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"

pm2 list                              # 六个进程
pm2 logs cylit-api --lines 50         # 后端日志
pm2 restart cylit-api                 # 改完 .env 要重启才生效
pm2 save                              # 固化开机自启列表

nginx -t && systemctl reload nginx    # 改完 snippet

# Cylit CLI（后端在跑的时候也能用，任务系统会排队）
cd /root/Cylit && .venv/bin/python cli/pr.py stats
cd /root/Cylit && .venv/bin/python cli/pr.py qa <paper_id>
```

改 nginx 之前先备份；`patch-nginx.py` 自己会备份到
`detect.conf.bak-<时间戳>`，重复执行是幂等的。

---

## 6. 从本机同步代码

代码走 git（仓库是公开的）：

```bash
ssh 43.136.18.54 'cd /root/Cylit && git fetch && git reset --hard origin/main'
```

**不在 git 里**、需要单独传的东西只有两类：

```bash
# 1) 前端产物（服务器上没跑 npm build）
cd <本机>/Cylit && tar -czf - frontend/dist | ssh 43.136.18.54 'tar -xzf - -C /root/Cylit'

# 2) 密钥
cd <本机>/Cylit && tar -czf - data/secrets | ssh 43.136.18.54 'tar -xzf - -C /root/Cylit'
```

如果哪天要在服务器上重新构建前端：
`cd /root/Cylit/frontend && npm install && npm run build`（node v25.6.1 已就绪）。

---

## 7. 模型配置怎么来的

服务器上 `pr.py init` 只装了 4 个内置 mock/启发式模型，
真正在用的 8 条是**从本机 `papermind.model_config` 导出来再灌进去的**：

```bash
# 本机
mysqldump -uroot -proot --no-create-info --complete-insert --skip-extended-insert \
  -r deploy/model-setting.sql papermind model_config setting
# 服务器
mysql -uroot papermind -e "DELETE FROM model_config; DELETE FROM setting;"
mysql -uroot papermind < /root/deploy/model-setting.sql
```

默认模型（沿用本机）：

| 用途 | provider | model | 密钥 |
|---|---|---|---|
| generation | deepseek | `deepseek-flash`（思考模式 high） | `deepseek.key` |
| vision | deepseek | `deepseek-v4-flash-vision-exp` | `deepseek.key` |
| embedding | dashscope_multimodal | `qwen3-vl-embedding`（dim 2560） | `dashscope.key` |
| reranker | dashscope | `qwen3-vl-rerank` | `dashscope.key` |

> 探测发现：`api.deepseek.com` 现在只认 `deepseek-flash` 和 `deepseek-v4-pro`，
> 其它别名（`deepseek-chat` / `deepseek-reasoner` / `deepseek-v4-flash`）都被路由到 `deepseek-flash`。

---

## 8. 部署当天发现并修掉的两个问题

### 8.1 「取消」之后任务永远卡在 pending（已修）

**症状**：任务面板一直显示「翻译 排队中 0/22」，永远不动。

**根因**：`acquire_next()` 会跳过 `cancel_requested=1` 的行，而唯一写 `CANCELLED`
的地方是 `_execute()` 里的 `ctx.should_stop()` —— 那条路径只在任务**已经跑起来**时才有。
于是「还没被领取就请求取消」的任务没人替它收尾。

**改法**（`backend/services/task/task_service.py`）：

- `_set_flags()`：取消一个 `pending` 任务时直接落终态（`已取消（尚未开始）`）；
- `recycle_stale()`：顺手清扫历史遗留的 `pending + cancel_requested` 行
  —— 这也负责治好改代码之前就已经卡住的那一条。

### 8.2 卡片不转圈，害得用户以为没在跑（已修）

`PaperCard` 的 `running`（转圈遮罩）只看任务表 `activeTaskOf(p)`。
但 CLI（`pr.py translate`）**故意不走任务队列**，任务表里空空如也 ——
卡片不转圈、按钮又写着「继续翻译」，看起来像"没在跑"；再点一下就是
**同一篇两套翻译并发**。

**改法**（`frontend/src/views/LibraryView.vue`）：`running` 再加上
`inFlight(p)`（论文行自己是 `parsing` / `translating`）。`running` 只驱动遮罩
（`pointer-events: none`），不影响按钮语义。

> 这两处改动**只在本机源码里**改好，服务器上已同步部署（源码 + 重建后的
> `frontend/dist`）。要固化请在本机 `git commit && git push`，再到服务器 `git pull`。

### 8.3 索引不会自己开始 —— 不是 bug，是链条的闸门

「翻译完成 → 自动建索引」挂在 `handlers._chain_index()` 上，它的前置条件是
`_pages_fully_translated()`：**A 路（英文转录）与 B 路（中文翻译）都要满页**。

| 你怎么触发的 | 会发生什么 |
|---|---|
| 网页上点「翻译」并让它跑完 | 两路都满 → 自动 INDEX → 自动「解读」 |
| `pr.py translate`（CLI） | 写的是**融合**产物 `md/pages.json`，不产生任务 → 链条不触发 |

CLI 跑完想建索引，直接走接口（会生成正经的任务记录，任务面板可见）：

```bash
curl -X POST http://127.0.0.1:8710/api/v1/knowledge/index \
  -H 'content-type: application/json' \
  -d '{"paper_id":"<paper_id>","rebuild":true}'
```

接口这一条**不会**连带生成「解读」（`then_understand` 只有自动链条才会带）。

索引本身只读 `pages.json`，**并不要求 A 路满页**；卡住的只是那道闸门。
所以「B 路已满、A 路只有半截」时，与其把 A 路补完（又是一笔 token），
不如直接调一次建索引接口。

### 8.4 一个与本部署无关的测试失败

`tests/test_kb_formula_source.py::test_real_corpus_counts_and_subset_invariant`
断言的是**本机那份语料**的公式数（329）。服务器上重新解析出来是 345，
于是它在服务器上必然失败 —— 这是一条绑死本地数据的金标测试，不是回归。
其余 72 项任务相关测试全绿。
