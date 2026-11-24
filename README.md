# 📰 每日德语精读 · Daily German Reader

每天自动抓取一篇简易德语新闻（附音频），挑出一句 20 词以上的长句，生成**逐词词表 + 参考译文 + 语法拆解**。
学习流程：**先自己精读翻译 → 再核对答案 → 有疑问一键复制提示词问 AI**。

- 💰 **完全免费**：GitHub Actions（定时任务）+ GitHub Pages（网站托管）+ Gemini API 免费额度
- 🤖 **全自动**：每天北京时间 15:00 左右自动更新，无需开电脑
- 📱 **手机/电脑通用**：笔记保存在你自己的浏览器里

```
german-daily-reader/
├── .github/workflows/daily-fetch.yml   # 每日定时：抓取 → 提交数据 → 部署网站
├── data/news.json                      # 所有文章与精读数据
├── scripts/fetch_news.py               # 抓取 + 长句提取 + 词表/解析生成
├── scripts/requirements.txt
├── index.html · style.css · app.js     # 纯静态前端
└── README.md
```

---

## 🚀 小白部署教程（约 15 分钟）

### 第 0 步：准备

- 一个 GitHub 账号（免费注册：<https://github.com/signup>）
- 一个 Google 账号（用来申请免费的 Gemini API Key，**可选**，没有也能用）

### 第 1 步：创建仓库并上传代码

1. 登录 GitHub，点击右上角 **`+` → New repository**。
2. **Repository name** 填 `german-daily-reader`（随意），选择 **Public**（免费账号的 Pages 需要公开仓库），点 **Create repository**。
3. 上传代码，二选一：

**方式 A：命令行（推荐，最省事）** —— 在项目文件夹里打开终端：

```bash
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<你的用户名>/german-daily-reader.git
git push -u origin main
```

> 首次 push 会要求登录。密码处需要填 **Personal Access Token**，或者安装 [GitHub Desktop](https://desktop.github.com/) 图形化上传。

**方式 B：网页上传**

1. 在新仓库页面点 **uploading an existing file**，把 `index.html`、`style.css`、`app.js`、`README.md`、`.gitignore`、`data`、`scripts` 文件夹拖进去 → **Commit changes**。
2. ⚠️ macOS 默认隐藏以 `.` 开头的文件夹，`.github` 很可能没被拖进去。请单独创建：
   **Add file → Create new file**，文件名输入 `.github/workflows/daily-fetch.yml`（输入 `/` 会自动变成文件夹），把本地该文件内容粘贴进去 → **Commit changes**。
   （Finder 里按 `Cmd + Shift + .` 可以显示隐藏文件。）

### 第 2 步：申请免费 Gemini API Key（可选，强烈推荐）

1. 打开 **Google AI Studio**：<https://aistudio.google.com/apikey>，用 Google 账号登录。
2. 点击 **Create API key**（如提示选择项目，选默认或新建一个即可），复制生成的一长串 Key（形如 `AIza...`）。
3. 无需绑定信用卡，使用免费层级即可；本项目**每天只调用 1 次**，远低于免费额度。

> [!NOTE]
> - Google AI Studio 在部分国家/地区（包括中国大陆）不提供服务，申请 Key 时需要所在网络环境受支持。**拿到 Key 之后**，实际调用是在 GitHub 的美国服务器上完成的，不受你本地网络影响。
> - 免费层级的请求内容可能被 Google 用于改进产品，本项目只发送公开新闻句子，没有隐私问题。
> - 不配置 Key 也能正常运行：脚本会自动切换到“免费翻译 + 规则解析”的降级模式（词表释义质量会差一些）。

### 第 3 步：把 Key 存进 GitHub Secrets（安全，不会公开）

1. 进入你的仓库 → **Settings**（仓库顶部最右的齿轮）。
2. 左侧 **Secrets and variables → Actions** → **New repository secret**。
3. **Name** 填 `GEMINI_API_KEY`（必须完全一致），**Secret** 粘贴刚才的 Key → **Add secret**。

> ⚠️ 千万不要把 Key 直接写进代码或提交到仓库里。

### 第 4 步：开启 GitHub Pages（网站托管）

1. 仓库 **Settings → Pages**。
2. **Build and deployment → Source** 选择 **GitHub Actions**（注意不是 “Deploy from a branch”）。
3. 不需要再点别的，保存即生效。

### 第 5 步：确认 Actions 权限

1. 仓库 **Settings → Actions → General**。
2. **Actions permissions** 选 **Allow all actions and reusable workflows**。
3. 页面底部 **Workflow permissions** 选 **Read and write permissions** → **Save**。

### 第 6 步：手动运行第一次 🎉

1. 点击仓库顶部 **Actions** 标签（如果出现 “I understand my workflows, go ahead and enable them”，点击它）。
2. 左侧选择 **Daily German News Fetch** → 右侧 **Run workflow** → 保持默认 → 绿色 **Run workflow**。
3. 等 1–3 分钟，两个任务 `fetch` 和 `deploy` 都变成绿色 ✅。
   - 点开 `fetch` 可以看到运行日志和当日摘要（选了哪篇、哪句、多少词、是否用了 Gemini）。
   - 点开 `deploy` 能看到网站地址。
4. 打开网站：**`https://<你的用户名>.github.io/german-daily-reader/`** 🎊

之后**每天会自动运行**，你什么都不用做。建议把网址添加到手机主屏幕。

---

## ⚙️ 可选配置（Settings → Secrets and variables → Actions → **Variables** 标签）

| 变量名 | 作用 | 示例 |
|---|---|---|
| `GEMINI_MODEL` | 更换 Gemini 模型（Google 推出新免费模型时修改） | `gemini-2.5-flash` |
| `SOURCE_STRATEGY` | `priority` = 按优先级；`rotate` = 每天轮换首选来源，内容更多样 | `rotate` |

修改抓取时间：编辑 `.github/workflows/daily-fetch.yml` 里的 `cron`（使用 **UTC 时间**，北京时间减 8 小时）。

手动运行时还可以：
- 勾选 **force**：重新抓取并覆盖今天的文章（换一篇）
- 选择 **source**：指定优先来源

---

## 🧑‍💻 本地预览与调试

```bash
# 预览网页（不能直接双击 index.html，浏览器会禁止读取 JSON）
python3 -m http.server 8000
# 打开 http://localhost:8000

# 本地试跑抓取脚本
pip install -r scripts/requirements.txt
python scripts/fetch_news.py --dry-run                      # 只打印结果，不写文件
GEMINI_API_KEY=你的key python scripts/fetch_news.py --force   # 真实写入
```

---

## ❓ 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| Actions 里 `fetch` 红色 ❌，日志显示 `所有来源均失败` | 新闻网站临时故障或改版。过几小时手动重跑；若持续失败，修改 `scripts/fetch_news.py` 顶部 `SOURCES` 中的网址 |
| `git push` 报 403 / `Permission denied` | 第 5 步没有设置 **Read and write permissions** |
| `deploy` 报错 `Pages not enabled` / `Branch not allowed` | 第 4 步 Source 没选 **GitHub Actions**；或默认分支不叫 `main`（把 workflow 里的 `main` 改成你的分支名） |
| 网站显示 “数据加载失败” | 第一次运行还没完成，或 `data/news.json` 格式被手动改坏 |
| 日志里出现 `Gemini 返回 HTTP 400/403` | Key 填错或模型名已下线：检查 Secret，或在 Variables 中设置新的 `GEMINI_MODEL` |
| `Gemini 返回 HTTP 429` | 免费额度暂时用尽，当天自动降级，第二天恢复 |
| 词表标着 “自动解析（降级）” | 当天没有使用 Gemini（未配置 Key 或调用失败） |
| 音频播放失败 | 原媒体音频已下线或禁止外链，点击提示里的链接去原文收听 |
| 换了浏览器/手机后笔记不见了 | 笔记保存在各自浏览器的 localStorage 里，不跨设备同步 |
| 60 天后定时任务停了 | GitHub 会暂停长期无提交仓库的定时任务；本项目每天自动提交，一般不会触发。若被暂停，在 Actions 页面重新启用即可 |

---

## 📜 版权说明

新闻正文与音频版权归 NDR、MDR、Deutschlandfunk（nachrichtenleicht）、ARD tagesschau、Deutsche Welle 等原媒体所有。
本项目仅供个人语言学习，请勿用于商业用途；音频直接引用原站地址，不做转存。
