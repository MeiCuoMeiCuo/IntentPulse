"""
主动巡逻模块
定期扫描注意力图谱中的高权重话题，主动发现新内容推送给用户
不依赖用户行为触发，纯主动式
"""
import json
import os
import time
import threading
from datetime import datetime
from typing import Dict, List, Optional
from urllib.request import Request, urlopen

from settings import get_openrouter_api_key

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
PATROL_STATE_PATH = os.path.join(ROOT_DIR, "patrol_state.json")

# ── 巡逻参数 ──────────────────────────────────────────────────────

# 话题权重达到多少才值得巡逻
PATROL_WEIGHT_THRESHOLD = 0.25

# 各权重区间的巡逻间隔（秒）
# 权重越高，巡逻越频繁
PATROL_INTERVALS = [
    (0.70, 30 * 60),    # 权重 >= 0.70：30分钟巡逻一次（核心兴趣）
    (0.50, 60 * 60),    # 权重 >= 0.50：1小时
    (0.35, 2 * 60 * 60), # 权重 >= 0.35：2小时
    (0.25, 4 * 60 * 60), # 权重 >= 0.25：4小时
]

# 单次巡逻最多处理几个话题（避免 API 消耗过多）
MAX_PATROL_TOPICS = 5

# 已推送内容的去重窗口（秒）——24小时内同一话题不重复推送同质内容
DEDUP_WINDOW = 24 * 60 * 60


def _load_config() -> str:
    config_path = os.path.join(ROOT_DIR, "config.json")
    return get_openrouter_api_key(config_path)


def _http_post(url: str, headers: dict, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    with urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_patrol_state() -> dict:
    """加载巡逻状态：记录每个话题上次巡逻时间 + 已推内容摘要"""
    if not os.path.exists(PATROL_STATE_PATH):
        return {"topics": {}, "pushed_summaries": []}
    try:
        with open(PATROL_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"topics": {}, "pushed_summaries": []}


def _save_patrol_state(state: dict) -> None:
    tmp = PATROL_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PATROL_STATE_PATH)


def _get_patrol_interval(weight: float) -> int:
    """根据话题权重返回巡逻间隔秒数"""
    for threshold, interval in PATROL_INTERVALS:
        if weight >= threshold:
            return interval
    return 999999  # 不巡逻


def _should_patrol(topic_key: str, weight: float, state: dict) -> bool:
    """判断该话题现在是否需要巡逻"""
    interval = _get_patrol_interval(weight)
    last_patrol = state.get("topics", {}).get(topic_key, {}).get("last_patrol", 0)
    return (time.time() - last_patrol) >= interval


def _is_duplicate(summary: str, state: dict) -> bool:
    """判断内容是否已推送过（简单字符串相似度）"""
    now = time.time()
    pushed = state.get("pushed_summaries", [])
    # 清理过期记录
    pushed = [p for p in pushed if now - p.get("ts", 0) < DEDUP_WINDOW]

    summary_chars = set(summary[:50])
    for p in pushed:
        p_chars = set(p.get("text", "")[:50])
        overlap = len(summary_chars & p_chars)
        if overlap >= 15:  # 前50字有15个字符重叠 → 认为是重复
            return True
    return False


def _fetch_new_content(api_key: str, topic: str, domain: str, last_pushed_at: float) -> Optional[dict]:
    """
    用 perplexity 搜索话题最新内容，判断是否有新增有价值的信息

    返回：{"has_new": bool, "title": str, "points": [...], "reason": str}
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    from datetime import timedelta
    cutoff_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    hours_since = int((time.time() - last_pushed_at) / 3600) if last_pushed_at > 0 else 48

    system = (
        "你是一个资讯监控助手。判断给定话题是否有新的、有价值的进展，只输出JSON。"
        f"当前时间：{now_str}。"
        f"严格限制：只能使用{cutoff_date}之后发布的内容，更早的文章一律忽略。"
    )
    user = f"""
监控话题：{topic}（所属领域：{domain}）
距上次推送：约{hours_since}小时

请联网搜索该话题在过去{min(hours_since, 48)}小时内的最新动态。

判断标准（必须同时满足才算"有新内容"）：
1. 是真正新发生的事件或进展，不是重复旧闻
2. 有实质信息量，不是空泛评论
3. 对关注该话题的用户有价值

输出格式（严格JSON）：
{{
  "has_new": true,
  "title": "15字以内的推送标题",
  "points": [
    {{"text": "要点1，50字以内", "sourceUrl": "https://...", "sourceName": "来源名"}},
    {{"text": "要点2，50字以内", "sourceUrl": "https://...", "sourceName": "来源名"}},
    {{"text": "要点3，50字以内", "sourceUrl": "https://...", "sourceName": "来源名"}}
  ],
  "reason": "一句话说明为什么这是新内容"
}}

如果没有新内容，输出：{{"has_new": false, "reason": "没有新进展"}}
""".strip()

    payload = {
        "model": "perplexity/sonar-pro",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        data = _http_post("https://openrouter.ai/api/v1/chat/completions", headers, payload)
        raw = data["choices"][0]["message"]["content"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        return result
    except Exception as e:
        print(f"[patrol] 内容获取失败 ({topic}): {e}", flush=True)
        return None


def get_patrol_candidates() -> List[dict]:
    """
    从注意力图谱中读取需要巡逻的话题列表
    优先 level2（话题级），level1 只在没有高权重 level2 子话题时才巡逻
    """
    import attention
    try:
        summary = attention.get_graph_summary()
        nodes = summary.get("nodes", [])
    except Exception as e:
        print(f"[patrol] 读取图谱失败: {e}", flush=True)
        return []

    state = _load_patrol_state()

    # 统计每个 level1 下有多少高权重 level2 子话题
    level2_by_domain: Dict[str, int] = {}
    for node in nodes:
        if node.get("level") == "level2" and node.get("weight", 0) >= PATROL_WEIGHT_THRESHOLD:
            parent = node.get("key", "").split(":")[1] if ":" in node.get("key", "") else ""
            # 找到这个 level2 节点的 parent（level1 label）
            # parent 信息存在节点的 parent 字段
    # 重新从图谱原始数据里取 parent
    import attention as _att
    try:
        raw_graph = _att._load_graph()
        raw_nodes = raw_graph.get("nodes", {})
    except Exception:
        raw_nodes = {}

    # 统计每个 level1 label 下有多少高权重 level2
    l1_has_l2: set = set()
    for key, node in raw_nodes.items():
        if node.get("level") == "level2":
            parent_label = node.get("parent", "")
            w = node.get("weight", 0)
            if w >= PATROL_WEIGHT_THRESHOLD and parent_label:
                l1_has_l2.add(parent_label)

    candidates = []
    for node in nodes:
        weight = node.get("weight", 0)
        if weight < PATROL_WEIGHT_THRESHOLD:
            continue

        level = node.get("level", "")
        label = node.get("label", "")

        # level3 不巡逻（太具体，变化快）
        if level == "level3":
            continue

        # level1 如果已经有高权重的 level2 子话题在巡逻，跳过 level1
        # 避免"体育"和"NBA篮球"同时推，只推精细的那个
        if level == "level1" and label in l1_has_l2:
            continue

        topic_key = node.get("key", "")
        if not _should_patrol(topic_key, weight, state):
            continue

        last_pushed_at = state.get("topics", {}).get(topic_key, {}).get("last_pushed", 0)
        candidates.append({
            "key": topic_key,
            "label": label,
            "level": level,
            "weight": weight,
            "avg_engagement": node.get("avg_engagement", 0.7),
            "last_pushed_at": last_pushed_at,
        })

    # level2 优先，同级按权重 × engagement 排序
    candidates.sort(key=lambda x: (
        0 if x["level"] == "level2" else 1,
        -(x["weight"] * (0.5 + x["avg_engagement"]))
    ))
    return candidates[:MAX_PATROL_TOPICS]


def run_patrol(write_feed_fn) -> int:
    """
    执行一次巡逻，发现新内容则写入 feed
    write_feed_fn：回调函数，接收 (topic, trigger_intent, domain) 写入 feed

    返回推送了几条新内容
    """
    candidates = get_patrol_candidates()
    if not candidates:
        return 0

    print(f"[patrol] 开始巡逻，候选话题: {[c['label'] for c in candidates]}", flush=True)

    try:
        api_key = _load_config()
    except Exception as e:
        print(f"[patrol] 加载配置失败: {e}", flush=True)
        return 0

    state = _load_patrol_state()
    pushed_count = 0

    for candidate in candidates:
        topic = candidate["label"]
        topic_key = candidate["key"]
        level = candidate["level"]
        last_pushed_at = candidate["last_pushed_at"]

        # level1 领域级：用更宽泛的搜索词
        domain = topic if level == "level1" else ""

        print(f"[patrol] 巡逻话题: {topic} (weight={candidate['weight']:.3f})", flush=True)

        result = _fetch_new_content(api_key, topic, domain, last_pushed_at)

        # 更新巡逻时间（无论有没有新内容）
        if topic_key not in state["topics"]:
            state["topics"][topic_key] = {}
        state["topics"][topic_key]["last_patrol"] = time.time()

        if not result or not result.get("has_new", False):
            print(f"[patrol] {topic}: 无新内容 ({result.get('reason', '') if result else '请求失败'})", flush=True)
            _save_patrol_state(state)
            continue

        # 检查去重
        title = result.get("title", topic)
        points = result.get("points", [])
        if not points:
            continue

        first_point_text = points[0].get("text", "") if points else ""
        if _is_duplicate(title + first_point_text, state):
            print(f"[patrol] {topic}: 内容重复，跳过", flush=True)
            _save_patrol_state(state)
            continue

        print(f"[patrol] ✓ {topic}: 发现新内容 → {title}", flush=True)

        # 写入 feed
        try:
            trigger_intent = f"你关注的「{topic}」有新动态"
            write_feed_fn(
                topic=topic,
                trigger_intent=trigger_intent,
                points=points,
                title=title,
            )
            pushed_count += 1

            # 记录已推送
            state["topics"][topic_key]["last_pushed"] = time.time()
            pushed_summaries = state.get("pushed_summaries", [])
            pushed_summaries.append({
                "text": title + first_point_text,
                "ts": time.time(),
                "topic": topic,
            })
            # 只保留最近200条
            state["pushed_summaries"] = pushed_summaries[-200:]

        except Exception as e:
            print(f"[patrol] 写入 feed 失败: {e}", flush=True)

        _save_patrol_state(state)

        # 每个话题之间稍微间隔，避免 API 并发
        time.sleep(1)

    print(f"[patrol] 巡逻完成，推送 {pushed_count} 条", flush=True)
    return pushed_count


# ── 后台巡逻线程 ──────────────────────────────────────────────────

_patrol_thread: Optional[threading.Thread] = None
_patrol_running = False


def start_patrol_loop(write_feed_fn, check_interval: int = 10 * 60):
    """
    启动后台巡逻线程
    check_interval：多久检查一次是否有话题需要巡逻（默认10分钟）
    """
    global _patrol_thread, _patrol_running

    if _patrol_running:
        return

    _patrol_running = True

    def _loop():
        print(f"[patrol] 后台巡逻线程已启动，检查间隔 {check_interval//60} 分钟", flush=True)
        while _patrol_running:
            try:
                run_patrol(write_feed_fn)
            except Exception as e:
                print(f"[patrol] 巡逻异常: {e}", flush=True)
            # 分段 sleep，方便停止
            for _ in range(check_interval):
                if not _patrol_running:
                    break
                time.sleep(1)

    _patrol_thread = threading.Thread(target=_loop, daemon=True)
    _patrol_thread.start()


def stop_patrol_loop():
    global _patrol_running
    _patrol_running = False
    print("[patrol] 后台巡逻线程已停止", flush=True)
