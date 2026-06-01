# MyAgent

MyAgent 是一个本地个人意图感知与资讯推荐服务。它接收用户行为意图，构建多尺度注意力图谱，结合 OpenRouter 模型抓取和生成更贴近当前兴趣的资讯、巡逻推送、服务推荐与马斯洛画像。

## 核心能力

- 意图分析：从行为文本、应用来源、动作类型和停留时长中提取领域、话题、实体与意图目标。
- 注意力图谱：维护短期、中期、长期兴趣权重，支持主动触发资讯更新。
- 资讯生成：基于具体意图或固定领域抓取近期资讯，写入 `feed.json`。
- 主动巡逻：定期扫描高权重话题，发现新动态后追加到资讯流。
- 服务推荐：只在学习分析、比较决策等明确需求下生成服务、课程、工具或商品卡片。
- 需求画像：根据注意力图谱生成马斯洛需求分布。

## 项目结构

```text
server.py      # HTTP API 服务入口
attention.py   # 意图提取与注意力图谱
main.py        # 资讯抓取与 feed 写入
patrol.py      # 高权重话题主动巡逻
services.py    # 服务/商品推荐卡片
maslow.py      # 马斯洛需求画像
settings.py    # 安全配置读取
nba.py         # NBA 当日比赛数据示例脚本
```

## 快速开始

1. 安装依赖：

```bash
python -m pip install -r requirements.txt
```

2. 配置 OpenRouter API Key，推荐使用环境变量：

```bash
export OPENROUTER_API_KEY="your_key_here"
```

也可以复制 `config.example.json` 为本地 `config.json` 后填写密钥。`config.json` 已加入 `.gitignore`，不会进入 GitHub。

3. 启动服务：

```bash
python server.py
```

服务默认监听 `http://localhost:8080`。

## API

- `GET /feed`：读取资讯流。
- `GET /attention`：读取注意力图谱摘要和巡逻状态。
- `GET /profile`：读取推荐画像与马斯洛画像。
- `GET /services`：读取服务推荐卡片。
- `POST /intent`：提交一次用户行为意图。
- `POST /feedback`：提交资讯反馈。
- `POST /services/refresh`：异步刷新服务推荐。

`POST /intent` 示例：

```json
{
  "intent": "我正在研究 NBA 季后赛湖人和火箭的赛后分析",
  "app": "browser",
  "action_type": "active_read",
  "duration_sec": 120,
  "title": "赛后分析文章标题",
  "url": "https://example.com/article"
}
```

## 隐私与安全

本仓库不会提交以下本地敏感或运行态文件：

- `config.json`、`.env*`：本地密钥配置。
- `feed.json`、`attention.json`、`maslow.json`、`patrol_state.json`：个人兴趣、画像和运行缓存。
- `.DS_Store`、临时文件、虚拟环境和 Python 缓存。

发布前请确认没有把真实密钥写入源码。当前代码统一通过 `settings.py` 读取 `OPENROUTER_API_KEY` 或本地 `config.json`。
