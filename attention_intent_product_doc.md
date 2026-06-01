# 注意力图谱与意图提取产品文档

## 1. 背景

当前项目已经具备一个个人资讯代理的雏形：系统接收用户行为意图，更新注意力图谱，在兴趣权重达到阈值时触发资讯抓取，并通过后台巡逻持续监控高兴趣话题。

现有机制的核心问题是：系统已经能知道“用户看过什么”，但还不够稳定地知道“用户为什么看、现在还关不关心、是否值得推送”。因此，本阶段优化重点不是增加更多资讯源，而是重构注意力图谱和意图判断机制，让系统能区分短期注意力、长期偏好、真实需求和路过噪音。

## 2. 产品目标

### 2.1 核心目标

构建一个证据驱动的注意力与意图系统，使代理能够：

- 从用户行为中识别真实意图，而不是只记录关键词。
- 区分瞬时兴趣、会话主题、中期关注、长期偏好和身份型偏好。
- 根据不同意图类型设置不同衰减周期。
- 在合适时机主动推送资讯，同时避免重复、误推和过度打扰。
- 为后续服务推荐、每日简报、长期画像提供稳定基础。

### 2.2 非目标

本阶段不优先解决：

- 大规模多用户系统。
- 完整前端交互设计。
- 复杂商业推荐闭环。
- 多模型自动路由。
- 付费、账号、权限体系。

## 3. 当下场景问题

当前项目的典型场景是：

1. 用户在手机或浏览器中搜索、阅读、刷到某些内容。
2. 外部系统把行为总结为一段 `intent` 文本发送到本地服务。
3. 服务端分析这段文本，更新兴趣图谱。
4. 当兴趣权重达到阈值时，系统生成一条个性化资讯 feed。
5. 后台巡逻线程定期监控高权重话题的新动态。

当前痛点包括：

- 只靠 `level1 / level2 / level3` 难以表达真实意图。
- 单次 LLM 分类容易污染图谱。
- 主动搜索、认真阅读、刷到路过的权重区分不够稳定。
- 所有兴趣衰减逻辑过于统一，无法适配赛事、突发新闻、长期爱好等不同周期。
- 触发推送主要看权重，缺少新鲜度、疲劳度、置信度判断。
- 用户画像和推送逻辑之间的关系还不够清楚。

## 4. 核心概念

### 4.1 行为证据 Evidence

系统不应直接把一次行为写成确定兴趣，而应先记录为证据。

示例：

```json
{
  "id": "evt-xxx",
  "raw_intent": "用户查看湖人vs火箭G5赛后分析",
  "app": "com.android.chrome",
  "action_type": "active_read",
  "timestamp": 1779769053,
  "engagement": 0.82,
  "duration_sec": 90,
  "source": "browser"
}
```

`action_type` 建议分为：

- `active_search`：主动搜索，强信号。
- `active_open`：主动打开目标内容，中强信号。
- `active_read`：停留阅读，中强信号。
- `passive_exposure`：刷到或曝光，弱信号。
- `system_noise`：系统、工具、通知类噪音。
- `negative_signal`：跳过、关闭、不感兴趣、屏蔽。

### 4.2 意图候选 Intent Candidate

一次证据可以生成多个候选意图，每个意图带置信度。

```json
{
  "intent_goal": "follow_event_update",
  "domain": "体育",
  "topic": "NBA季后赛",
  "entity": "湖人vs火箭",
  "confidence": 0.84,
  "time_sensitive": true,
  "freshness_hours": 12,
  "reason": "用户主动阅读赛后分析，内容具备强时效性"
}
```

建议支持的 `intent_goal`：

- `follow_news`：关注新闻动态。
- `follow_event_update`：追踪事件进展。
- `learn_analysis`：学习分析、背景、策略。
- `compare_decision`：比较、决策、购买前研究。
- `entertainment_browse`：娱乐浏览。
- `task_completion`：完成任务，不应推送。
- `system_operation`：系统操作，不应推送。
- `accidental_exposure`：路过曝光，低权重或不入图。

### 4.3 注意力节点 Attention Node

图谱节点不只保存标签，还保存它代表的意图结构。

```json
{
  "key": "intent:NBA季后赛:follow_event_update",
  "domain": "体育",
  "topic": "NBA季后赛",
  "entity": "湖人vs火箭",
  "intent_goal": "follow_event_update",
  "time_horizon": "short",
  "state": "active",
  "score": 0.78,
  "confidence": 0.81,
  "evidence_count": 5,
  "last_seen": 1779769053,
  "last_pushed": 1779769053,
  "decay_profile": "real_time_event"
}
```

### 4.4 多尺度注意力

建议把注意力分成五层：

| 层级 | 含义 | 典型周期 | 例子 | 用途 |
| --- | --- | --- | --- | --- |
| 瞬时意图 | 用户此刻想知道什么 | 分钟到数小时 | 今天 NBA 比分 | 即时推送 |
| 会话主题 | 一段时间内连续关注什么 | 数小时到 1 天 | 连续看湖人、火箭、赛后分析 | 判断上下文 |
| 中期关注 | 最近一段时间反复出现 | 数天到数周 | NBA季后赛、OpenAI诉讼 | 巡逻监控 |
| 长期偏好 | 稳定兴趣 | 数月 | AI、篮球、德州扑克 | 个性化基础 |
| 身份偏好 | 更抽象的取向 | 长期 | 技术探索、竞技策略、审美偏好 | 画像和推荐 |

## 5. 意图提取流程

### 5.1 输入

`POST /intent` 输入建议扩展为：

```json
{
  "intent": "用户查看湖人vs火箭G5赛后分析",
  "app": "com.android.chrome",
  "action_type": "active_read",
  "duration_sec": 90,
  "url": "https://...",
  "title": "湖人vs火箭G5赛后分析",
  "timestamp": 1779769053
}
```

如果外部系统暂时只能提供 `intent` 和 `app`，服务端需要做降级推断。

### 5.2 LLM 输出

LLM 不直接输出单一结论，而输出候选意图数组：

```json
{
  "valuable": true,
  "primary_action": "active_read",
  "engagement": 0.82,
  "candidates": [
    {
      "domain": "体育",
      "topic": "NBA季后赛",
      "entity": "湖人vs火箭",
      "intent_goal": "follow_event_update",
      "confidence": 0.84,
      "time_sensitive": true,
      "freshness_hours": 12,
      "decay_profile": "real_time_event"
    },
    {
      "domain": "体育",
      "topic": "湖人队",
      "entity": "湖人",
      "intent_goal": "follow_news",
      "confidence": 0.55,
      "time_sensitive": true,
      "freshness_hours": 24,
      "decay_profile": "hot_topic"
    }
  ]
}
```

### 5.3 图谱更新原则

- 高置信候选直接更新对应节点。
- 中置信候选进入观察区，不立即触发推送。
- 低置信候选只保留 evidence，不进入稳定图谱。
- 工具、系统、任务完成类意图默认不触发推送。
- 路过曝光必须经过多次累积才可能转为兴趣。
- 负反馈应立即降低相关节点分数，并延长冷却时间。

## 6. 衰减周期设计

### 6.1 衰减类型

| decay_profile | 场景 | 半衰期 | 最大有效期 |
| --- | --- | --- | --- |
| `real_time_event` | 比分、股价、突发新闻 | 1-6 小时 | 24 小时 |
| `hot_topic` | 热点新闻、诉讼、车企动态 | 12-48 小时 | 7 天 |
| `short_task` | 订酒店、买票、路线查询 | 1-3 天 | 14 天 |
| `seasonal_interest` | NBA季后赛、某剧、某产品周期 | 3-14 天 | 60 天 |
| `stable_interest` | AI、篮球、机器人、德州扑克 | 30-180 天 | 长期 |
| `identity_preference` | 技术探索、竞技策略、审美偏好 | 慢衰减 | 长期 |

### 6.2 推荐评分公式

```text
attention_score =
  short_term_heat * 0.45 +
  mid_term_interest * 0.30 +
  long_term_preference * 0.15 +
  confidence * 0.10 +
  active_bonus -
  passing_penalty -
  fatigue_penalty
```

说明：

- `short_term_heat` 决定此刻是否值得推。
- `mid_term_interest` 决定近期是否值得巡逻。
- `long_term_preference` 决定是否符合用户稳定偏好。
- `confidence` 降低 LLM 误判带来的污染。
- `active_bonus` 来自主动搜索、打开、停留。
- `passing_penalty` 来自被动曝光、快速滑过。
- `fatigue_penalty` 来自近期重复推送或用户无响应。

### 6.3 不同层级的使用方式

- 即时推送主要看 `short_term_heat` 和 `urgency`。
- 后台巡逻主要看 `mid_term_interest` 和 `long_term_preference`。
- 每日简报主要看 `stable_interest` 和过去 24 小时新鲜度。
- 服务推荐主要看 `stable_interest` 加明确需求信号。

## 7. 节点状态机

每个意图节点维护状态：

| 状态 | 含义 | 进入条件 | 退出条件 |
| --- | --- | --- | --- |
| `observed` | 刚观察到，还不确定 | 新证据产生 | 多次证据或置信度提升 |
| `warming` | 正在升温 | 短时间多次出现 | 达到触发分或衰减 |
| `active` | 可触发推送 | 分数、置信度、新鲜度达标 | 推送后或热度下降 |
| `saturated` | 刚推过，进入疲劳期 | 已推送 | 冷却结束 |
| `cooling` | 关注下降 | 长时间无新证据 | 再次激活或休眠 |
| `dormant` | 长期偏好沉睡 | 稳定兴趣但近期不活跃 | 强相关新闻唤醒 |
| `rejected` | 用户明确不感兴趣 | 负反馈 | 手动恢复或长期自然恢复 |

## 8. 推送触发策略

### 8.1 触发公式

```text
push_score =
  relevance * 0.35 +
  novelty * 0.25 +
  urgency * 0.20 +
  confidence * 0.15 -
  fatigue * 0.25
```

触发条件：

```text
push_score >= threshold
AND node.state in ["warming", "active", "dormant"]
AND novelty >= minimum_novelty
AND cooldown_passed = true
```

### 8.2 推送类型

- `instant_push`：高时效、高相关，例如比赛结果、重大新闻。
- `feed_insert`：普通资讯流更新，不强打扰。
- `daily_digest`：每日摘要，适合长期兴趣。
- `weekly_review`：长期偏好总结。
- `service_card`：明确需求场景下的服务推荐。

### 8.3 推送解释

每条推送建议带解释字段：

```json
{
  "why": "因为你最近多次主动阅读 NBA季后赛 相关内容，且该话题有新赛况。"
}
```

这能提升可理解性，也方便调试误推。

## 9. 数据结构调整

### 9.1 attention.json 建议结构

```json
{
  "version": 2,
  "evidence_log": [],
  "intent_nodes": {},
  "sessions": [],
  "identity_profile": {},
  "updated_at": "2026-05-26T12:00:00"
}
```

### 9.2 intent_nodes 示例

```json
{
  "intent:NBA季后赛:follow_event_update": {
    "domain": "体育",
    "topic": "NBA季后赛",
    "entity": "湖人vs火箭",
    "intent_goal": "follow_event_update",
    "decay_profile": "real_time_event",
    "state": "active",
    "score": 0.78,
    "confidence": 0.81,
    "evidence_count": 5,
    "active_count": 4,
    "passive_count": 1,
    "negative_count": 0,
    "last_seen": 1779769053,
    "last_pushed": 1779769053,
    "cooldown_until": 1779772653,
    "sources": ["com.android.chrome"]
  }
}
```

### 9.3 feed.json 建议补充字段

```json
{
  "id": "1779769052963-patrol",
  "category": "杨瀚森下放发展联盟",
  "topic": "NBA篮球",
  "intent_goal": "follow_news",
  "trigger_node": "intent:NBA篮球:follow_news",
  "push_type": "feed_insert",
  "confidence": 0.76,
  "novelty": 0.82,
  "urgency": 0.68,
  "why": "因为你最近持续关注 NBA篮球，且该话题有新动态。"
}
```

## 10. API 调整建议

### 10.1 POST /intent

职责：接收行为证据，更新图谱，返回是否触发。

返回示例：

```json
{
  "ok": true,
  "triggered": true,
  "trigger": {
    "topic": "NBA季后赛",
    "intent_goal": "follow_event_update",
    "push_type": "feed_insert",
    "reason": "score_passed_threshold"
  },
  "debug": {
    "engagement": 0.82,
    "confidence": 0.84,
    "state": "active"
  }
}
```

### 10.2 GET /attention

返回图谱摘要，区分：

- 当前热意图。
- 近期关注。
- 长期偏好。
- 被拒绝或降权节点。
- 巡逻候选。

### 10.3 POST /feedback

新增负反馈入口：

```json
{
  "feed_id": "1779769052963-patrol",
  "action": "dismiss",
  "reason": "not_interested"
}
```

支持的 action：

- `click`
- `read`
- `save`
- `share`
- `dismiss`
- `not_interested`
- `block_topic`

## 11. 实施路线

### Phase 1：修复与重构基础

- 修复 `attention.py` 中 `_calc_weight` 重复定义问题。
- 合并 `main.py/main_new.py` 和 `patrol.py/patrol_new.py`。
- 为 attention 数据增加 `version`。
- 引入 evidence log 和 intent candidate。
- 保持现有接口兼容。

### Phase 2：多尺度注意力

- 实现短期、中期、长期三套分数。
- 引入 `decay_profile`。
- 实现节点状态机。
- `/attention` 返回更清晰的摘要。

### Phase 3：推送质量优化

- 推送触发改为 `relevance + novelty + urgency + confidence - fatigue`。
- feed 增加推送解释。
- 巡逻模块使用新状态机和 decay profile。
- 增加重复内容识别和来源质量评分。

### Phase 4：反馈闭环

- 新增 `/feedback`。
- 用户点击、关闭、不感兴趣会反向影响图谱。
- 支持手动屏蔽、提升、降低话题。

### Phase 5：画像和服务推荐

- 将长期偏好和身份偏好输出给 `maslow.py`。
- 把 `services.py` 接入明确需求场景，而不是泛化强推。
- 支持每日简报、长期趋势总结。

## 12. 成功指标

### 12.1 体验指标

- 推送点击率提升。
- 用户关闭/不感兴趣比例下降。
- 重复推送比例下降。
- 系统推送理由可解释。

### 12.2 算法指标

- 主动行为与被动曝光区分准确率提升。
- 同义话题合并率提升。
- 过期资讯进入 feed 的比例下降。
- 高置信节点触发率高于低置信节点。

### 12.3 工程指标

- attention 数据结构可迁移。
- 核心评分逻辑有单元测试。
- 每次 `/intent` 可追踪决策链。
- 外部 API 失败不破坏本地状态。

## 13. 风险与对策

| 风险 | 表现 | 对策 |
| --- | --- | --- |
| LLM 误判 | 错误话题进入图谱 | 候选意图加置信度，低置信只观察 |
| 过度推送 | 用户觉得烦 | fatigue、cooldown、负反馈 |
| 长期兴趣污染 | 一次路过变成稳定兴趣 | evidence 累积、被动曝光低权重 |
| 资讯过期 | feed 出现旧闻 | decay profile、publishDate 校验 |
| 数据结构复杂 | 迭代困难 | version 字段、分阶段迁移 |
| API 不稳定 | 请求失败或返回脏 JSON | schema 校验、失败兜底 |

## 14. 文档自审

### 14.1 完整性检查

- 已覆盖产品背景和目标。
- 已定义当下项目面对的核心问题。
- 已给出注意力分层方案。
- 已给出意图提取结构。
- 已给出衰减周期设计。
- 已给出节点状态机。
- 已给出推送触发策略。
- 已给出数据结构和 API 调整建议。
- 已给出实施路线和成功指标。
- 已识别主要风险并给出对策。

### 14.2 是否能解决当下场景

这份方案可以解决当下项目的核心场景问题：

- 对“用户真正意图”的识别，从单次标签抽取升级为证据累积和候选意图确认。
- 对“注意力分层”的定义，从简单领域树升级为短期、中期、长期、身份偏好的多尺度结构。
- 对“衰减周期”的定义，从统一权重衰减升级为按意图类型选择半衰期。
- 对“是否推送”的判断，从只看兴趣权重升级为相关性、新鲜度、紧急度、置信度和疲劳度综合判断。
- 对“误推和重复推”的治理，引入负反馈、冷却、状态机和 novelty 判断。

### 14.3 仍需补充的问题

后续进入实现前，还需要补充三类细节：

- 外部行为采集端能提供哪些字段，例如停留时长、URL、标题、点击/关闭反馈。
- 第一版 decay profile 的具体参数，需要结合真实使用日志调参。
- 是否继续使用 JSON 文件作为存储，还是在 Phase 2 前迁移到 SQLite。

### 14.4 结论

文档已经足够指导第一阶段工程落地。建议先按 Phase 1 实施，修复现有注意力图谱的稳定性问题，并在不破坏当前 `/intent` 和 `/feed` 的前提下引入 evidence log、候选意图和新的 decay profile。
