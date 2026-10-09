# 部署方案 · Cloudflare 可行性评估（目标版本 v1.9.40）

> 结论先行：**CF Pages 托管 `live.html` 完全可行且是已验证范式；但「每 300s 刷新管线」无法在 CF Pages 上跑，必须配一个外部算力（免费云服务）跑 Python 管线再推 Pages。** 纯 CF 单栈不成立，推荐「CF Pages 静态 + 外部免费算力跑管线」混合架构。

---

## 1. 本地现状（部署前要搬上云的）

| 组件 | 职责 | 云上对应 |
|---|---|---|
| `run_live.sh`（每 300s 接力） | `collect_live.py → markets.py → narrate.py → build_live.py` 重建 `live.html` | 常驻算力 + 出网 |
| `start_server.py`（:8800） | 服务 `live.html` + `/__session__.json`（导播台 4s 轮询实时时段） | 静态托管 + 会话端点 |
| 数据源 | 腾讯/新浪/东财 push2his、DeanFi（美股广度）、`yangxiaa.cc/rrg/`（collect_rrg） | 出网白名单 |
| `narrate.py --engine api` | 调 LLM 生成解说 | LLM API 可达 |

---

## 2. CF 可行性分三块评估

### 2.1 静态产物 `live.html` → CF Pages：✅ 完全可行
- **`live.html` 已验证自包含**：163KB 单文件、1 个内联 `<style>` + 2 个内联 `<script>`、**零外部/相对资源引用、无 nav.css**。→ 走 CF Pages 无 base-path / slash / 外部资源 404 等坑。
- **范式已就绪**：`wolong-cross-space-deploy` + `cloudflare-pages-direct-deploy` 给出完整凭据与 9 步防坑（白名单暂存目录、`404.html`、cache-bust 验证）。
  - Account `a2dac81f66ddd96fc47dcc5243b46177`，部署用账户级 `cfat_…`（base64 注入，命令零字面量）。
  - 挂 `yangxiaa.cc/直播`（或子域）走既有 `site-router` Worker（已用于 `/dp`、`/ub`）。
- **无 nav.css 依赖** → 可不接入站间导航（或后续按需接），挂路径零风险。

### 2.2 刷新管线 `run_live.sh`（Python + 出网 + LLM）→ CF Pages：❌ 不可行
- CF Pages = 纯静态文件托管，**无长驻进程、无定时 Python 执行**。
- 管线要：① 出网抓 腾讯/新浪/东财/DeanFi/`yangxiaa.cc/rrg/`；② 调 LLM API（narrate api 引擎）。Pages 一概不支持。
- 唯一在 CF 体系内的替代 = **Cloudflare Worker + Cron Triggers 重写管线**：Worker 能 fetch 外网 + 调 LLM，但要把 `collect_live/markets/narrate/build` 整套 Python **移植成 JS/Wasm**，且受 CPU-time 限制（免费 Cron 10k 次/日，多源抓取+LLM 易超时）。**工程成本高、风险大，不推荐首版。**

### 2.3 `/__session__.json` 端点（导播台 4s 轮询）→ 需 Pages Functions / Worker
- 本地由 `start_server.py` 提供；上云后要么**烘焙进 live.html**（但这会让 4s 实时状态退化成 300s 才更新），要么由 **Pages Functions 或 Worker** 每次请求时返回当前会话 JSON（从 KV/R2 读管线写入的状态，或按请求现算）。

---

## 3. 推荐架构（CF Pages 静态 + 外部免费算力跑管线）

```
[免费算力/定时任务] 每 300s:
   run_live.sh（collect→markets→narrate→build）
        │  产出 live.html + 写入 session 状态
        ▼
   wrangler pages deploy  →  CF Pages（yangxiaa.cc/直播）
   + 写 session 到 Pages Functions/KV（供导播台轮询）
        │
   访客浏览器 ──► CF Pages 静态 live.html + site-router 路由
```

- **静态层**：CF Pages（已验证可行）。
- **算力层**：选一个免费云服务跑 `run_live.sh` 并推 Pages（见 §4）。
- **会话层**：Pages Functions（或 Worker）serve `/__session__.json`。

---

## 4. 算力层备选（「不行再找其他免费云服务」）

| 方案 | 可行性 | 注意 |
|---|---|---|
| **GitHub Actions 定时** | 中 | `schedule:` 最小 5min；runner 临时，状态文件（`commentary_history.json`/`rrg_state.json`）需回写仓库或 KV；公有库免费额度够个人低流量 |
| **Oracle Cloud Free Tier**（永久免费 2×ARM） | 高 | 真·常驻，最接近本地 `run_live.sh` 体验；需自建进程守护（nohup/disown，见 BTC 交易台铁律） |
| **Fly.io / Render 免费档** | 中 | 有休眠（Render free 15min 休眠），不适合 300s 连续刷新；Fly 小实例可常驻但免费额度紧 |
| **Cloudflare Worker+ Cron（重写管线）** | 低（首版） | 需 Python→JS 移植 + CPU-time 风险 |

**首版推荐**：Oracle Free Tier 常驻跑 `run_live.sh`（行为完全一致、零移植成本）+ wrangler 推 Pages；GitHub Actions 作备选（无服务器时）。

---

## 5. 云端验收清单（单独验收，本地通 ≠ 上云通）

- [ ] 算力层 **server→server 出网可达 `yangxiaa.cc/rrg/`**（collect_rrg 最高风险，本地通≠云端通）
- [ ] 算力层出网可达 腾讯/新浪/东财/DeanFi
- [ ] 算力层调 LLM API 可达（narrate api 引擎）
- [ ] 管线状态文件（`commentary_history.json`/`rrg_state.json`）跨轮持久化（临时 runner 必做）
- [ ] `/__session__.json` 端点上线且导播台 4s 轮询正常
- [ ] live.html 经白名单暂存 + `404.html` + cache-bust 验证（防内部文件泄漏/旧缓存）

---

## 6. 结论
- **CF Pages 托管直播页 = 立即可做**（自包含单文件 + 已验证范式 + 既有 site-router）。
- **整站上云 ≠ 纯 CF**：刷新管线必须外挂免费算力。先定算力方案，再开搭。
- 估算工作量：静态部署 ~0.5 天（复用范式）；算力层搭建 ~0.5–1 天（取决于选 Oracle/Actions）；会话端点 ~0.5 天。

---

## 7. B 方案落地（GitHub Actions，师傅 2026-10-09 拍板先试）

### 7.1 架构
```
GitHub Actions cron（每 5min）
   └─ ci_run_once.sh（= 本地 run_live.sh 一轮，无死循环/无 macOS 锁）
         collect_live(按需) → markets → [eulerpool跳过] → collect_rrg → narrate(api) → build_live
         └─ 产物拷到 _site/（仅 live.html + 404.html）
   └─ actions/cache 持久化 data/（snapshot/commentary/rrg_state 跨轮续传）
   └─ cloudflare/wrangler-action → pages deploy _site --project-name wolong-live
        └─ 访客浏览器 ← CF Pages（yangxiaa.cc/直播，经既有 site-router Worker）
```
- **5min 粒度 = 本地 calm 档（300s）**，正好对齐；active(120s) 在 Actions 上不可达（平台最小 5min），但 active 默认已停用，无影响。
- 源码**零外部依赖**（纯 stdlib），CI 只 `setup-python` + 跑脚本，无需 `pip install`。

### 7.2 新增文件
| 文件 | 作用 |
|---|---|
| `.github/workflows/deploy.yml` | 调度 + 缓存 + 部署 |
| `ci_run_once.sh` | 单次管线（Linux/CI 友好，用环境 `python3`） |
| `.gitignore` | 排除 data/ logs/ live.html config.json 等运行态，避免泄漏源码与密钥 |
| `config.json.example` | 脱敏配置模板（agnes.key → `${AGNES_API_KEY}`），仓库提交此文件 |

### 7.3 手动前置（一次性，需师傅操作）
1. **建 GitHub 仓库并推送**（项目当前非 git 仓）：`git init` → 加远程 → `git add .` → `git commit` → `git push`。`.gitignore` 已确保 config.json / data / live.html 不入库。
2. **仓库 Secrets**（Settings → Secrets）：
   - `AGNES_API_KEY`：解说 api 引擎必需（值同本地 config.json 里的 key）。
   - `CLOUDFLARE_API_TOKEN`：具有 `Pages:edit` 权限的 CF API Token（账户级 `cfat_…`）。
   - `CLOUDFLARE_ACCOUNT_ID`：账户 ID（`a2dac81f66ddd96fc47dcc5243b46177`）。
3. **一次性建 Pages 项目**：`npx wrangler pages project create wolong-live --production-branch=main`。
4. **site-router 挂 `yangxiaa.cc/直播`**（既有 Worker，参照 /dp、/ub 接法）。

### 7.4 已知取舍 / 风险
- **状态持久化**：`actions/cache` 用 `restore-keys: live-state-` + `key: live-state-<run_id>`，每轮新增一个缓存条目；长期运行会增长（LRU 自动淘汰，单条目 ~200KB，可接受），如需可定期 prune。
- **`collect_rrg` 出网是最高风险**：`yangxiaa.cc/rrg/` 须云端 server→server 可达（本地通≠云端通），上云后必须单独验收；失败仅 RRG 瓦片不更新，不拖垮主流程。
- **eulerpool 云端跳过**：云端无 7897 出口代理且被 Cloudflare 拦，未设 `EULERPOOL_API_KEY` 即跳过，快讯池照旧用 RSS。
- **解说配额**：api 引擎每轮 1 次 LLM 调用（≈ 免费档 10 RPM，余量充足）。
- **会话端点 `/__session__.json`**：B 方案首版未含 Pages Functions；导播台 4s 轮询在 CF 上会退化成 5min 才更新（与页面刷新同频）。如需实时，后续加 Pages Functions/KV（待定，非首版阻塞项）。

### 7.5 验收清单（沿用 §5，重点标 collect_rrg 出网）
- [ ] 仓库推送 + 3 个 Secrets 就位
- [ ] 首次手动 `workflow_dispatch` 跑通：pipeline 无报错、`_site/live.html` 生成、Pages 上线
- [ ] **collect_rrg 出网可达 `yangxiaa.cc/rrg/`**（最高优先级，单独验）
- [ ] 出网可达 腾讯/新浪/东财/DeanFi；LLM API（agnes）可达
- [ ] data/ 跨轮持久化生效（次轮 snapshot 接续，不回到冷启动）
- [ ] live.html 经白名单 + 404.html + cache-bust 验证（防内部文件泄漏/旧缓存）
- [ ] yangxiaa.cc/直播 经 site-router 可访问

---

### 7.6 实测记录（2026-10-09 · v1.9.38 → v1.9.39）

**首次 CI 部署：✅ 成功**。手动 `workflow_dispatch` → Success（1m16s）→ `https://wolong-live.pages.dev/live.html` 线上可访问，内容真实：A股 盘前解说 + 3 条带「原文↗」的国内快讯（鸿富瀚业绩预告 / 贝莱德减持恒瑞 H 股 / 上交所终止企查查 IPO）。**V1.9.36 的「A股 场快讯配比 + 资讯可见性」修复在云端同样生效**。

**实测抓出的三个云端专属坑（本地全都不暴露）**：

| # | 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|---|
| 1 | **冷启动无快照** | 整轮 `FileNotFoundError` 崩 | `markets.py main()` 硬 `open(data/snapshot.json)`；本地该文件因历史运行一直存在 | `ci_run_once.sh` 缺失时写空壳 `{}` 自举（V1.9.38） |
| 2 | **CI 时区 = UTC** | 北京 17:12 被判成 A股「盘前」，页面/解说时间戳全显示 09:12（差 8 小时） | `market_state()` 的 `now = now or datetime.now()` 及各处 `time.strftime` 走**进程本地时区**；runner 是 UTC | `ci_run_once.sh` `export TZ=Asia/Shanghai` + `deploy.yml` job `env.TZ`（V1.9.39） |
| 3 | **无 config.json** | `narrate --preset` 直接 exit 1 | `config.json` 含密钥被 gitignore，CI 无此文件 | 由 `config.json.example` + Secret `AGNES_API_KEY` 现场生成（V1.9.38） |

**新增运维观测**：`ci_run_once.sh` 每轮把自检摘要写入 `$GITHUB_STEP_SUMMARY`（运行时间 / 当前场次 / 快讯池 / traded_today / 解说段数 / 产物大小），在 Actions 运行页直接可见，不必翻全量日志。

**⚠️ 成本硬约束（B 方案的根本问题）**：
- 私有仓 Free 计划仅 **2,000 分钟/月**（**public 仓无限免费**）；计费按 **job 向上取整到整分钟**（本轮 1m16s → 计 2 分钟）。
- cron `*/5` 全天 = 288 轮/天 × 2 分钟 = **576 分钟/天 ≈ 17,280 分钟/月 → 约 3.5 天耗尽**。
- 默认 spending limit = $0 → **耗尽后不是扣费，而是静默停摆**（页面停更，直到下月 1 日额度重置）。降到 45 分钟/轮才能压进额度，直播体验不可接受。
- 结论：**B 方案适合验证链路，不适合长期承载**。长期二选一：**① 仓库转 public**（无限免费，代价＝源码公开）或 **② 换常驻算力**（§4 的 Oracle Free Tier，真免费无限、行为与本地一致）。Actions 可保留作兜底通道。

**入口问题**：`*.pages.dev` 在中国大陆直连不稳（实测连接超时）。对外最终入口应经既有 `site-router`（`纽扣岛/site-router/`）挂 `yangxiaa.cc/live*` → `wolong-live.pages.dev`（ROUTES 表加一行 + zone 级路由，需 CF 凭证，尚未执行）。

### 7.7 时区问题根治 + 转 public（2026-10-09 · v1.9.40）

**为什么 §7.6 的 V1.9.39 修法不够**：它只在 `ci_run_once.sh` 里 `export TZ=Asia/Shanghai`，属**外挂补丁** —— 依赖每个调用方自觉设置，换入口（本地跑 / 别的 CI / 新算力机）就再次踩坑。第一次上线后线上仍显示 A股 盘前，就是因为那轮 run 跑的是补丁前的代码。

**根治（V1.9.40）**：新增 `cn_tz.py`，`import` 即执行 `os.environ["TZ"]="Asia/Shanghai"` + `time.tzset()`，把**进程时区强制锚定**，与操作系统彻底解耦。5 个含裸时间的脚本各加一行 `import cn_tz`：

| 脚本 | 裸时间点 |
|---|---|
| `markets.py` | `market_state()` 的 `now = now or dt.datetime.now()` → 当前场次判定 |
| `narrate.py` | `dt.datetime.now()` 时间戳、`cn_fresh` 日期比较 |
| `collect_live.py` | `generated_at` 时间戳、`--date` 默认交易日 |
| `build_live.py` | `today`、`time.localtime()` 时间戳 |
| `collect_rrg.py` | `fetched_at` 时间戳 |

CI 侧 `export TZ` 保留作双保险（两道防线）。

**验证证据（决定性）**：`git archive HEAD` 导出与 CI 检出完全一致的干净副本，**故意强制 `TZ=UTC`**（模拟 runner 默认）跑一轮：

| 组 | 结果 |
|---|---|
| 对照（旧代码，裸时间） | `09:35:35 UTC` ← 即误判 A股「盘前」的来源 |
| 实验（新代码，`import cn_tz`） | `17:35:35 CST` ✓ |
| 端到端（强制 UTC 跑全链路） | 自检摘要「**当前场次 = 美股** / 场次检查于 **17:39**」、exit 0、产物 93 KB ✓ |

**转 public（解 Actions 成本限制，需师傅网页操作，约 20 秒）**：
1. 打开 `https://github.com/yangxia028/wolong-live/settings`
2. 滚到最底 **Danger Zone** → **Change repository visibility** → **Make public**
3. 按提示输入仓库名确认

转 public 后：Actions **无限免费**（私有仓 2,000 分钟/月 的限制解除）→ 5 分钟一轮可持续承载。密钥安全性不变（`config.json` 从未入库，走 Secrets + `.gitignore`；`ci_run_once.sh` 经检查不回显任何密钥）。

> 运维铁律（本次踩到）：**代码修复推送后必须云端重跑一次才生效** —— 线上页面是上一轮的产物，看旧页面不等于修复失败。核验线上时优先看 `$GITHUB_STEP_SUMMARY` 里的自检摘要（含运行时间与当前场次），比读页面文字更快。
