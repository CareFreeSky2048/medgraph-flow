# MedGraph Flow · 复旦基础医学碎片化图谱学习系统

将繁重的医学课件编译为**「概念去重 + 显式逻辑桥接 + 考纲权重路由」**的四层知识图谱，并在手机端以 3~5 分钟碎片化逻辑流的方式进行低门槛推演、断点续接、双模式岔路探索与邻域连坐复习。

## 托管到 GitHub 并部署到手机（推荐）

本目录已配置独立 Git 仓库与 GitHub Actions 自动部署流水线（[.github/workflows/deploy-pages.yml](.github/workflows/deploy-pages.yml)）：

```powershell
cd c:\Users\32461\Desktop\AI_Cowork\med_graph_app
git remote add origin https://github.com/<你的用户名>/<你的仓库名>.git
git push -u origin main
```

推送后，在 GitHub 仓库页面打开 **Settings → Pages → Source 选择 `GitHub Actions`**，数十秒后即可获得 HTTPS 手机访问地址（`https://<你的用户名>.github.io/<你的仓库名>/`），手机 Safari / Chrome 打开后点击 **「添加到主屏幕」** 即可像原生 App 一样离线/在线使用。

> 提示：若在手机端点击右上角 `☁️ 同步` 绑定一个仅含 `gist` 权限的 GitHub Token，每次打卡都会自动同步到你的私有 Gist；以后在电脑上合并新课件并 `git push`，手机端刷新或重启 PWA 即可无缝拿到最新图谱且保留全部旧掌握度。

## 在 Antigravity 中直接用 Gemini 增量添加课件（Skill 工作流）

工作区已安装专用 Skill：[`.agents/skills/medgraph-slide-ingest/SKILL.md`](../.agents/skills/medgraph-slide-ingest/SKILL.md)。以后每天下课后只需：
1. 在 Antigravity 对话里直接把桌面课件（`.pptx` / `.ppt` / `.pdf` / `.docx` 或截图）发给 Gemini，说一句：**“用 medgraph-slide-ingest 把这份课件增量入库”**。
2. Gemini 会自动提取文本、匹配三份教学大纲权重、复用已有概念并拉出跨学科边、无损合入 `data/graph.json` 并重新构建 `dist/`。
3. 执行 `git add -A && git commit -m "ingest: xxx" && git push`，手机端自动升级。

（也保留了命令行 `--mode api` 与 `--mode prompt-pack` 兼容接口。）

## 交互与算法设计要点

- **逻辑线双模式岔路（手动探索 + 算法主动规划）**：
  - **手动进入**：每张微卡片下方直接嵌入**完整中文标题、无截断的「🧭 邻域星系图」**（上方=先修/结构基础、两侧=跨学科/易混鉴别、下方=顺流机制/临床后果）。点击任一邻点先预览 `bridge_text` 衔接逻辑，再点击进入岔路。
  - **算法规划进入**：① 开荒时自动织入高价值跨学科/薄弱顺道岔路；② 当你在某个知识点点击 `🔴 陌路 / 重来` 且存在未掌握的先修/结构基础父节点时，算法**自动先带你绕道 1 站补齐基础**，完成后点击顶部琥珀色横幅即可弹回原断点继续攻克。
- **自适应微卡片**：机制类（递进推演流）、结构解剖类（胶囊遮罩 + 临床陷阱）、鉴别对比类（微对比表 + 秒测题）。

## 本地预览与测试命令

- 离线单文件试玩：[dist/medgraph_offline.html](dist/medgraph_offline.html)
- 运行全部验证：
```powershell
node med_graph_app/tests/test_router.cjs
python med_graph_app/tests/test_ingest.py
node med_graph_app/tests/test_mobile_ui.cjs
```
