"""
多尺度注意力图谱与意图提取

兼容旧结构：
- nodes: 继续提供 level1/level2/level3 节点，供 patrol.py 和 maslow.py 使用
- 新增 evidence_log / intent_nodes，用于证据驱动的意图记忆
"""
import json
import math
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from settings import get_openrouter_api_key

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
ATTENTION_PATH = os.path.join(ROOT_DIR, "attention.json")

GRAPH_VERSION = 2
MAX_MENTIONS_LOG = 200
MAX_EVIDENCE_LOG = 500
MAX_FEEDBACK_LOG = 200
MAX_SESSION_BUFFER = 20

# 旧 level 节点仍然用于兼容 patrol/maslow。阈值略保守，避免误推。
THRESHOLD = {
    "level1": 0.82,
    "level2": 0.66,
    "level3": 0.48,
}

COOLDOWN = {
    "level1": 60 * 60,
    "level2": 45 * 60,
    "level3": 30 * 60,
}

INTENT_PUSH_THRESHOLD = 0.68
MIN_CANDIDATE_CONFIDENCE = 0.35
MIN_TRIGGER_CONFIDENCE = 0.45
MIN_NOVELTY = 0.22

DECAY_PROFILES = {
    "real_time_event": {
        "short_half_life_hours": 3,
        "mid_half_life_days": 0.75,
        "long_half_life_days": 3,
        "max_age_days": 2,
        "cooldown_seconds": 30 * 60,
        "urgency": 0.92,
    },
    "hot_topic": {
        "short_half_life_hours": 18,
        "mid_half_life_days": 2,
        "long_half_life_days": 7,
        "max_age_days": 14,
        "cooldown_seconds": 60 * 60,
        "urgency": 0.72,
    },
    "short_task": {
        "short_half_life_hours": 24,
        "mid_half_life_days": 3,
        "long_half_life_days": 10,
        "max_age_days": 14,
        "cooldown_seconds": 2 * 60 * 60,
        "urgency": 0.50,
    },
    "seasonal_interest": {
        "short_half_life_hours": 48,
        "mid_half_life_days": 7,
        "long_half_life_days": 30,
        "max_age_days": 60,
        "cooldown_seconds": 2 * 60 * 60,
        "urgency": 0.45,
    },
    "stable_interest": {
        "short_half_life_hours": 72,
        "mid_half_life_days": 30,
        "long_half_life_days": 120,
        "max_age_days": 365,
        "cooldown_seconds": 6 * 60 * 60,
        "urgency": 0.28,
    },
    "identity_preference": {
        "short_half_life_hours": 168,
        "mid_half_life_days": 90,
        "long_half_life_days": 240,
        "max_age_days": 730,
        "cooldown_seconds": 24 * 60 * 60,
        "urgency": 0.18,
    },
}

ACTION_ENGAGEMENT = {
    "active_search": 0.95,
    "active_open": 0.80,
    "active_read": 0.82,
    "passive_exposure": 0.35,
    "system_noise": 0.05,
    "negative_signal": 0.0,
}

ACTION_ALIASES = {
    "search": "active_search",
    "query": "active_search",
    "open": "active_open",
    "click": "active_open",
    "read": "active_read",
    "view": "active_read",
    "browse": "active_read",
    "exposure": "passive_exposure",
    "passive": "passive_exposure",
    "system": "system_noise",
    "tool": "system_noise",
    "dismiss": "negative_signal",
    "not_interested": "negative_signal",
}

NEGATIVE_FEEDBACK_ACTIONS = {"dismiss", "not_interested", "block_topic", "hide", "negative_signal"}
POSITIVE_FEEDBACK_ACTIONS = {"click", "read", "save", "share", "open"}

_last_triggered: Dict[str, float] = {}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
    if value is None:
        return default
    return bool(value)


def _clean_label(value: Any, fallback: str = "其他", max_len: int = 18) -> str:
    text = str(value or fallback).strip()
    text = text.replace("\n", " ").replace("\r", " ")
    text = " ".join(text.split())
    if not text:
        return fallback
    return text[:max_len]


def _load_config() -> str:
    config_path = os.path.join(ROOT_DIR, "config.json")
    return get_openrouter_api_key(config_path)


def _http_post(url: str, headers: dict, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize_action_type(action_type: Optional[str], intent_text: str = "") -> str:
    if action_type:
        raw = str(action_type).strip().lower()
        if raw in ACTION_ENGAGEMENT:
            return raw
        if raw in ACTION_ALIASES:
            return ACTION_ALIASES[raw]

    text = intent_text.lower()
    active_search_words = ["搜索", "检索", "查询", "主动输入", "search"]
    active_read_words = ["阅读", "查看", "点开", "点击", "打开", "展开", "看完", "read", "click"]
    passive_words = ["刷到", "路过", "曝光", "弹出", "通知", "广告", "划过", "passive"]
    system_words = ["锁屏", "系统", "设置", "充电", "蓝牙", "wifi", "通知栏"]

    if any(word in text for word in system_words):
        return "system_noise"
    if any(word in text for word in passive_words):
        return "passive_exposure"
    if any(word in text for word in active_search_words):
        return "active_search"
    if any(word in text for word in active_read_words):
        return "active_read"
    return "active_read"


def _blend_engagement(model_engagement: float, action_type: str, duration_sec: Optional[float]) -> float:
    action_engagement = ACTION_ENGAGEMENT.get(action_type, 0.7)
    duration_bonus = 0.0
    if duration_sec is not None:
        duration_bonus = _clamp(_safe_float(duration_sec, 0.0) / 180.0, 0.0, 0.12)
    blended = model_engagement * 0.55 + action_engagement * 0.45 + duration_bonus
    if action_type == "negative_signal":
        return 0.0
    if action_type == "system_noise":
        return min(blended, 0.1)
    return _clamp(blended)


def _infer_intent_goal(intent_type: str, time_sensitive: bool, text: str = "") -> str:
    lowered = text.lower()
    if intent_type in {"tool", "system"}:
        return "task_completion" if intent_type == "tool" else "system_operation"
    if any(word in lowered for word in ["对比", "比较", "购买", "买", "价格", "预订", "订票"]):
        return "compare_decision"
    if any(word in lowered for word in ["学习", "教程", "分析", "策略", "原理", "课程"]):
        return "learn_analysis"
    if time_sensitive:
        return "follow_event_update"
    if intent_type == "entertainment":
        return "entertainment_browse"
    return "follow_news"


def _infer_decay_profile(
    intent_goal: str,
    time_sensitive: bool,
    freshness_hours: int,
    domain: str,
    topic: str,
    entity: str,
) -> str:
    text = f"{domain} {topic} {entity}"
    if intent_goal in {"task_completion", "compare_decision"}:
        return "short_task"
    if intent_goal == "learn_analysis":
        return "stable_interest"
    if any(word in text for word in ["比分", "赛果", "股价", "行情", "突发", "战报", "直播"]):
        return "real_time_event"
    if time_sensitive and freshness_hours <= 12:
        return "real_time_event"
    if time_sensitive or freshness_hours <= 48:
        return "hot_topic"
    if any(word in text for word in ["季后赛", "赛事", "赛季", "综艺", "电视剧", "产品"]):
        return "seasonal_interest"
    if any(word in text for word in ["AI", "科技", "篮球", "德州", "扑克", "机器人", "设计", "编程"]):
        return "stable_interest"
    return "seasonal_interest"


def _candidate_from_levels(levels: Dict[str, Any], intent_text: str = "") -> Dict[str, Any]:
    domain = _clean_label(levels.get("level1"), "其他", 8)
    topic = _clean_label(levels.get("level2"), domain, 16)
    entity = _clean_label(levels.get("level3"), topic, 18)
    time_sensitive = _as_bool(levels.get("time_sensitive"), False)
    freshness_hours = max(1, _safe_int(levels.get("freshness_hours"), 48))
    intent_type = str(levels.get("intent_type", "content")).strip()
    goal = _infer_intent_goal(intent_type, time_sensitive, intent_text)
    confidence = _clamp(_safe_float(levels.get("confidence"), 0.72))
    decay_profile = str(levels.get("decay_profile") or "").strip()
    if decay_profile not in DECAY_PROFILES:
        decay_profile = _infer_decay_profile(goal, time_sensitive, freshness_hours, domain, topic, entity)
    return {
        "domain": domain,
        "topic": topic,
        "entity": entity,
        "intent_goal": goal,
        "confidence": confidence,
        "time_sensitive": time_sensitive,
        "freshness_hours": freshness_hours,
        "decay_profile": decay_profile,
    }


def _normalize_candidate(candidate: Dict[str, Any], levels: Dict[str, Any], intent_text: str) -> Dict[str, Any]:
    domain = _clean_label(candidate.get("domain") or candidate.get("level1") or levels.get("level1"), "其他", 8)
    topic = _clean_label(candidate.get("topic") or candidate.get("level2") or levels.get("level2"), domain, 16)
    entity = _clean_label(candidate.get("entity") or candidate.get("level3") or levels.get("level3"), topic, 18)
    goal = _clean_label(candidate.get("intent_goal") or candidate.get("goal"), "follow_news", 32)
    time_sensitive = _as_bool(candidate.get("time_sensitive"), _as_bool(levels.get("time_sensitive"), False))
    freshness_hours = max(1, _safe_int(candidate.get("freshness_hours"), _safe_int(levels.get("freshness_hours"), 48)))
    confidence = _clamp(_safe_float(candidate.get("confidence"), 0.5))
    decay_profile = str(candidate.get("decay_profile") or "").strip()
    if decay_profile not in DECAY_PROFILES:
        decay_profile = _infer_decay_profile(goal, time_sensitive, freshness_hours, domain, topic, entity)
    return {
        "domain": domain,
        "topic": topic,
        "entity": entity,
        "intent_goal": goal,
        "confidence": confidence,
        "time_sensitive": time_sensitive,
        "freshness_hours": freshness_hours,
        "decay_profile": decay_profile,
        "reason": str(candidate.get("reason", ""))[:120],
    }


def _normalize_extraction(result: Dict[str, Any], intent_text: str) -> Dict[str, Any]:
    levels = {
        "level1": _clean_label(result.get("level1") or result.get("domain"), "其他", 8),
        "level2": _clean_label(result.get("level2") or result.get("topic"), "其他", 16),
        "level3": _clean_label(result.get("level3") or result.get("entity"), "其他", 18),
        "valuable": _as_bool(result.get("valuable"), True),
        "time_sensitive": _as_bool(result.get("time_sensitive"), False),
        "freshness_hours": max(1, _safe_int(result.get("freshness_hours"), 48)),
        "intent_type": str(result.get("intent_type", "content")).strip(),
        "engagement": _clamp(_safe_float(result.get("engagement"), 0.7)),
        "is_passing": _as_bool(result.get("is_passing"), False),
    }

    raw_candidates = result.get("candidates")
    candidates: List[Dict[str, Any]] = []
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            if isinstance(item, dict):
                normalized = _normalize_candidate(item, levels, intent_text)
                if normalized["confidence"] >= MIN_CANDIDATE_CONFIDENCE:
                    candidates.append(normalized)

    if not candidates:
        candidates.append(_candidate_from_levels(levels, intent_text))

    candidates.sort(key=lambda item: item.get("confidence", 0), reverse=True)
    primary = candidates[0]
    levels["level1"] = primary["domain"]
    levels["level2"] = primary["topic"]
    levels["level3"] = primary["entity"]
    levels["time_sensitive"] = primary["time_sensitive"]
    levels["freshness_hours"] = primary["freshness_hours"]
    levels["decay_profile"] = primary["decay_profile"]
    levels["intent_goal"] = primary["intent_goal"]
    levels["confidence"] = primary["confidence"]
    levels["candidates"] = candidates
    return levels


def _extract_levels(intent_text: str, api_key: str) -> Dict[str, Any]:
    """从行为描述中提取候选意图，同时保留旧 level1/2/3 兼容字段。"""
    system = "你是一个意图分析器，从用户行为中提取可验证的兴趣意图。只输出JSON，不要其他内容。"
    user = f"""
从以下用户行为中提取候选意图：
{intent_text}

输出格式（严格JSON）：
{{
  "valuable": true,
  "level1": "大领域",
  "level2": "具体话题",
  "level3": "具体实体或事件",
  "intent_type": "content",
  "engagement": 0.7,
  "is_passing": false,
  "time_sensitive": true,
  "freshness_hours": 24,
  "candidates": [
    {{
      "domain": "体育",
      "topic": "NBA季后赛",
      "entity": "湖人vs火箭",
      "intent_goal": "follow_event_update",
      "confidence": 0.84,
      "time_sensitive": true,
      "freshness_hours": 12,
      "decay_profile": "real_time_event",
      "reason": "用户主动阅读赛后分析"
    }}
  ]
}}

规则：
- level1 是宽泛领域，不超过4个字，如体育、科技、金融、娱乐、生活。
- level2 是具体话题，不超过8个字，如NBA季后赛、AI动态、德州扑克。
- level3 是最具体的实体或事件，不超过10个字。
- intent_type 只能是 content、entertainment、tool、system。
- intent_goal 只能优先从以下选择：follow_news、follow_event_update、learn_analysis、compare_decision、entertainment_browse、task_completion、system_operation、accidental_exposure。
- decay_profile 只能是：real_time_event、hot_topic、short_task、seasonal_interest、stable_interest、identity_preference。
- 主动搜索/阅读 engagement 取 0.7-1.0；刷到/路过取 0.2-0.4；系统噪音取 0-0.1。
- 如果是系统、工具、锁屏、通知、广告路过，valuable=false 或 intent_goal=system_operation/accidental_exposure。
- 候选意图最多3个，按置信度从高到低排序。
""".strip()

    payload = {
        "model": "x-ai/grok-4.3",
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
        content = data["choices"][0]["message"]["content"].strip()
        content = content.replace("```json", "").replace("```", "").strip()
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("LLM result is not an object")
        return _normalize_extraction(result, intent_text)
    except Exception as e:
        print(f"[attention] 意图提取失败: {e}", flush=True)
        fallback = {
            "level1": "其他",
            "level2": "其他",
            "level3": "其他",
            "valuable": True,
            "time_sensitive": False,
            "freshness_hours": 48,
            "intent_type": "content",
            "engagement": 0.7,
            "is_passing": False,
        }
        return _normalize_extraction(fallback, intent_text)


def _empty_graph() -> dict:
    return {
        "version": GRAPH_VERSION,
        "nodes": {},
        "intent_nodes": {},
        "evidence_log": [],
        "feedback_log": [],
        "sessions": [],
        "updated_at": "",
    }


def _load_graph() -> dict:
    if not os.path.exists(ATTENTION_PATH):
        return _empty_graph()
    try:
        with open(ATTENTION_PATH, "r", encoding="utf-8") as f:
            graph = json.load(f)
        if not isinstance(graph, dict):
            return _empty_graph()
        return _ensure_graph_shape(graph)
    except Exception:
        return _empty_graph()


def _ensure_graph_shape(graph: dict) -> dict:
    graph.setdefault("version", GRAPH_VERSION)
    graph.setdefault("nodes", {})
    graph.setdefault("intent_nodes", {})
    graph.setdefault("evidence_log", [])
    graph.setdefault("feedback_log", [])
    graph.setdefault("sessions", [])
    graph.setdefault("updated_at", "")
    return graph


def _save_graph(graph: dict) -> None:
    graph["version"] = GRAPH_VERSION
    tmp = ATTENTION_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ATTENTION_PATH)


def _profile_for_node(node: dict) -> dict:
    profile_name = node.get("decay_profile")
    if profile_name in DECAY_PROFILES:
        return DECAY_PROFILES[profile_name]
    level = node.get("level", "")
    if level == "level1":
        return DECAY_PROFILES["stable_interest"]
    if level == "level2":
        return DECAY_PROFILES["seasonal_interest"]
    return DECAY_PROFILES["hot_topic"]


def _fallback_log(node: dict, now_ts: float) -> List[dict]:
    log = node.get("mentions_log", [])
    if log:
        return log
    last_seen = _safe_float(node.get("last_seen"), now_ts)
    mentions = max(1, _safe_int(node.get("mentions"), 1))
    avg_engagement = _clamp(_safe_float(node.get("avg_engagement"), 0.7))
    confidence = _clamp(_safe_float(node.get("confidence"), 0.65))
    return [
        {
            "ts": last_seen - i * 3600,
            "engagement": avg_engagement,
            "confidence": confidence,
            "action_type": "active_read",
        }
        for i in range(min(mentions, 10))
    ]


def _score_windows(node: dict, now_ts: Optional[float] = None) -> Dict[str, float]:
    now = now_ts or time.time()
    profile = _profile_for_node(node)
    log = _fallback_log(node, now)
    max_age_seconds = profile["max_age_days"] * 86400

    short_score = 0.0
    mid_score = 0.0
    long_score = 0.0
    for entry in log:
        ts = _safe_float(entry.get("ts"), now)
        age_seconds = max(0.0, now - ts)
        if age_seconds > max_age_seconds:
            continue
        engagement = _clamp(_safe_float(entry.get("engagement"), 0.7))
        confidence = _clamp(_safe_float(entry.get("confidence"), node.get("confidence", 0.65)))
        signal = engagement * (0.55 + confidence * 0.45)
        age_hours = age_seconds / 3600
        age_days = age_seconds / 86400

        short_decay = math.exp(-0.693 * age_hours / profile["short_half_life_hours"])
        mid_decay = math.exp(-0.693 * age_days / profile["mid_half_life_days"])
        long_decay = math.exp(-0.693 * age_days / profile["long_half_life_days"])
        short_score += signal * short_decay
        mid_score += signal * mid_decay
        long_score += signal * long_decay

    short_norm = _clamp(short_score / 2.4)
    mid_norm = _clamp(mid_score / 5.0)
    long_norm = _clamp(long_score / 10.0)
    return {
        "short": short_norm,
        "mid": mid_norm,
        "long": long_norm,
    }


def _calc_weight(node: dict) -> float:
    """统一权重计算，兼容旧节点和新 intent_nodes。"""
    windows = _score_windows(node)
    confidence = _clamp(_safe_float(node.get("confidence"), 0.65))
    active = _safe_int(node.get("active_count"), 0)
    passive = _safe_int(node.get("passing_count", node.get("passive_count", 0)), 0)
    negative = _safe_int(node.get("negative_count"), 0)
    total = max(1, active + passive + negative)

    active_bonus = min(active / 6.0, 1.0) * 0.12
    platforms = node.get("platforms") or node.get("sources") or []
    platform_bonus = min(len(platforms) / 3.0, 1.0) * 0.08
    passing_penalty = max(0.0, passive / total - 0.45) * 0.35
    negative_penalty = min(negative / 3.0, 1.0) * 0.35

    fatigue_penalty = 0.0
    last_pushed = _safe_float(node.get("last_pushed"), 0)
    if last_pushed:
        elapsed = time.time() - last_pushed
        cooldown = _profile_for_node(node).get("cooldown_seconds", 3600)
        if elapsed < cooldown:
            fatigue_penalty = (1 - elapsed / cooldown) * 0.18

    score = (
        windows["short"] * 0.45
        + windows["mid"] * 0.30
        + windows["long"] * 0.15
        + confidence * 0.10
        + active_bonus
        + platform_bonus
        - passing_penalty
        - negative_penalty
        - fatigue_penalty
    )
    return round(_clamp(score, 0.0, 1.2), 4)


def _state_for_node(node: dict, threshold: float, now_ts: Optional[float] = None) -> str:
    now = now_ts or time.time()
    if _safe_int(node.get("negative_count"), 0) >= 3:
        return "rejected"
    cooldown_until = _safe_float(node.get("cooldown_until"), 0)
    if cooldown_until > now:
        return "saturated"
    score = _safe_float(node.get("score", node.get("weight")), 0)
    last_seen = _safe_float(node.get("last_seen"), now)
    age_days = (now - last_seen) / 86400
    if score >= threshold:
        return "active"
    if score >= threshold * 0.7:
        return "warming"
    if age_days > 30 and score < threshold * 0.35:
        return "dormant"
    if _safe_int(node.get("evidence_count", node.get("mentions", 0)), 0) <= 1:
        return "observed"
    return "cooling"


def _find_similar(nodes: Dict[str, dict], level_key: str, label: str) -> Optional[str]:
    label_chars = set(label)
    for key, node in nodes.items():
        if not key.startswith(level_key + ":"):
            continue
        existing = str(node.get("label", ""))
        if label in existing or existing in label:
            return key
        if len(label_chars & set(existing)) >= 3:
            return key
    return None


def _append_signal(
    node: dict,
    evidence_id: str,
    now_ts: float,
    engagement: float,
    confidence: float,
    action_type: str,
) -> None:
    log = node.get("mentions_log", [])
    log.append({
        "ts": now_ts,
        "engagement": round(engagement, 3),
        "confidence": round(confidence, 3),
        "action_type": action_type,
        "evidence_id": evidence_id,
    })
    profile = _profile_for_node(node)
    cutoff = now_ts - profile["max_age_days"] * 86400
    log = [entry for entry in log if _safe_float(entry.get("ts"), 0) >= cutoff]
    node["mentions_log"] = log[-MAX_MENTIONS_LOG:]


def _update_legacy_nodes(
    graph: dict,
    candidate: dict,
    evidence: dict,
) -> Tuple[Dict[str, float], Dict[str, str]]:
    nodes = graph.get("nodes", {})
    now_ts = evidence["timestamp"]
    engagement = evidence["engagement"]
    action_type = evidence["action_type"]
    confidence = candidate["confidence"]
    is_passing = evidence.get("is_passing", False) or action_type == "passive_exposure" or engagement <= 0.3
    labels = {
        "level1": candidate["domain"],
        "level2": candidate["topic"],
        "level3": candidate["entity"],
    }
    weights: Dict[str, float] = {}
    keys: Dict[str, str] = {}

    for level_key, label in labels.items():
        similar_key = _find_similar(nodes, level_key, label)
        if similar_key:
            node_key = similar_key
            if len(label) < len(str(nodes[node_key].get("label", label))):
                nodes[node_key]["label"] = label
        else:
            node_key = f"{level_key}:{label}"
        if node_key not in nodes:
            nodes[node_key] = {
                "label": label,
                "level": level_key,
                "parent": candidate["domain"] if level_key == "level2" else (candidate["topic"] if level_key == "level3" else None),
                "mentions": 0,
                "passing_count": 0,
                "active_count": 0,
                "negative_count": 0,
                "last_seen": now_ts,
                "platforms": [],
                "weight": 0.0,
                "avg_engagement": engagement,
                "confidence": confidence,
                "decay_profile": candidate["decay_profile"],
                "intent_goal": candidate["intent_goal"],
                "mentions_log": [],
            }

        node = nodes[node_key]
        node["mentions"] = _safe_int(node.get("mentions"), 0) + 1
        node["last_seen"] = now_ts
        node["confidence"] = node.get("confidence", confidence) * 0.7 + confidence * 0.3
        node["decay_profile"] = candidate["decay_profile"]
        node["intent_goal"] = candidate["intent_goal"]

        prev_engagement = _safe_float(node.get("avg_engagement"), engagement)
        node["avg_engagement"] = round(prev_engagement * 0.7 + engagement * 0.3, 3)

        if action_type == "negative_signal":
            node["negative_count"] = _safe_int(node.get("negative_count"), 0) + 1
        elif is_passing:
            node["passing_count"] = _safe_int(node.get("passing_count"), 0) + 1
        else:
            node["active_count"] = _safe_int(node.get("active_count"), 0) + 1

        platforms = node.get("platforms", [])
        app = evidence.get("app")
        if app and app not in platforms:
            platforms.append(app)
        node["platforms"] = platforms

        _append_signal(node, evidence["id"], now_ts, engagement, confidence, action_type)
        node["weight"] = _calc_weight(node)
        node["state"] = _state_for_node(node, THRESHOLD[level_key], now_ts)
        weights[level_key] = node["weight"]
        keys[level_key] = node_key

    graph["nodes"] = nodes
    return weights, keys


def _intent_node_key(candidate: dict) -> str:
    topic = str(candidate["topic"]).replace(":", " ")
    goal = str(candidate["intent_goal"]).replace(":", " ")
    entity = str(candidate.get("entity") or "").replace(":", " ")
    if entity and entity != topic:
        return f"intent:{topic}:{goal}:{entity}"
    return f"intent:{topic}:{goal}"


def _update_intent_nodes(graph: dict, candidates: List[dict], evidence: dict) -> Optional[str]:
    nodes = graph.get("intent_nodes", {})
    now_ts = evidence["timestamp"]
    engagement = evidence["engagement"]
    action_type = evidence["action_type"]
    primary_key = None

    for idx, candidate in enumerate(candidates):
        confidence = candidate["confidence"]
        if confidence < MIN_CANDIDATE_CONFIDENCE:
            continue

        node_key = _intent_node_key(candidate)
        if idx == 0:
            primary_key = node_key
        if node_key not in nodes:
            nodes[node_key] = {
                "key": node_key,
                "domain": candidate["domain"],
                "topic": candidate["topic"],
                "entity": candidate["entity"],
                "intent_goal": candidate["intent_goal"],
                "decay_profile": candidate["decay_profile"],
                "state": "observed",
                "score": 0.0,
                "confidence": confidence,
                "evidence_count": 0,
                "active_count": 0,
                "passive_count": 0,
                "passing_count": 0,
                "negative_count": 0,
                "last_seen": now_ts,
                "last_pushed": 0,
                "cooldown_until": 0,
                "sources": [],
                "mentions_log": [],
                "time_sensitive": candidate["time_sensitive"],
                "freshness_hours": candidate["freshness_hours"],
            }

        node = nodes[node_key]
        node["domain"] = candidate["domain"]
        node["topic"] = candidate["topic"]
        node["entity"] = candidate["entity"]
        node["intent_goal"] = candidate["intent_goal"]
        node["decay_profile"] = candidate["decay_profile"]
        node["time_sensitive"] = candidate["time_sensitive"]
        node["freshness_hours"] = candidate["freshness_hours"]
        node["last_seen"] = now_ts
        node["evidence_count"] = _safe_int(node.get("evidence_count"), 0) + 1
        node["confidence"] = round(_safe_float(node.get("confidence"), confidence) * 0.7 + confidence * 0.3, 3)

        if action_type == "negative_signal":
            node["negative_count"] = _safe_int(node.get("negative_count"), 0) + 1
        elif evidence.get("is_passing") or action_type == "passive_exposure" or engagement <= 0.3:
            node["passive_count"] = _safe_int(node.get("passive_count"), 0) + 1
            node["passing_count"] = _safe_int(node.get("passing_count"), 0) + 1
        else:
            node["active_count"] = _safe_int(node.get("active_count"), 0) + 1

        sources = node.get("sources", [])
        app = evidence.get("app")
        if app and app not in sources:
            sources.append(app)
        node["sources"] = sources

        _append_signal(node, evidence["id"], now_ts, engagement, confidence, action_type)
        node["score"] = _calc_weight(node)
        node["state"] = _state_for_node(node, INTENT_PUSH_THRESHOLD, now_ts)

    graph["intent_nodes"] = nodes
    return primary_key


def _clean_low_signal_nodes(graph: dict) -> None:
    nodes = graph.get("nodes", {})
    remove_keys = []
    for key, node in nodes.items():
        passing = _safe_int(node.get("passing_count"), 0)
        active = _safe_int(node.get("active_count"), 0)
        mentions = _safe_int(node.get("mentions"), 0)
        weight = _safe_float(node.get("weight"), 0)
        if mentions >= 3 and passing > active * 3 and weight < 0.12:
            remove_keys.append(key)
            print(f"[attention] 清洗低信号节点: {node.get('label')} passing={passing} active={active}", flush=True)
    for key in remove_keys:
        nodes.pop(key, None)

    intent_nodes = graph.get("intent_nodes", {})
    for key, node in list(intent_nodes.items()):
        passive = _safe_int(node.get("passive_count", node.get("passing_count")), 0)
        active = _safe_int(node.get("active_count"), 0)
        negative = _safe_int(node.get("negative_count"), 0)
        score = _safe_float(node.get("score"), 0)
        if negative >= 3:
            node["state"] = "rejected"
        elif passive >= 4 and active == 0 and score < 0.12:
            intent_nodes.pop(key, None)


def _update_session(graph: dict, evidence: dict, primary: dict) -> None:
    now_ts = evidence["timestamp"]
    sessions = graph.get("sessions", [])
    buffer = graph.get("_session_buffer", [])
    buffer.append({
        "intent": evidence["raw_intent"][:80],
        "l1": primary["domain"],
        "l2": primary["topic"],
        "l3": primary["entity"],
        "intent_goal": primary["intent_goal"],
        "engagement": evidence["engagement"],
        "confidence": primary["confidence"],
        "ts": now_ts,
    })
    buffer = buffer[-MAX_SESSION_BUFFER:]

    session_start = _safe_float(graph.get("_session_start"), now_ts)
    elapsed_session_min = (now_ts - session_start) / 60
    should_summarize = elapsed_session_min >= 30 or len(buffer) >= 10

    if should_summarize and len(buffer) >= 3:
        topic_weights: Dict[str, float] = {}
        domain_counts: Dict[str, int] = {}
        for item in buffer:
            topic = item["l2"]
            topic_weights[topic] = topic_weights.get(topic, 0) + item["engagement"] * item.get("confidence", 0.7)
            domain = item["l1"]
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
        top_topics = sorted(topic_weights.items(), key=lambda item: -item[1])[:3]
        dominant_domain = sorted(domain_counts.items(), key=lambda item: -item[1])[0][0]
        avg_engagement = sum(item["engagement"] for item in buffer) / len(buffer)
        sessions.append({
            "start": session_start,
            "end": now_ts,
            "duration_min": round(elapsed_session_min, 1),
            "intent_count": len(buffer),
            "dominant_domain": dominant_domain,
            "top_topics": [topic for topic, _ in top_topics],
            "avg_engagement": round(avg_engagement, 2),
        })
        graph["sessions"] = sessions[-20:]
        graph["_session_buffer"] = []
        graph["_session_start"] = now_ts
    else:
        graph["_session_buffer"] = buffer
        graph.setdefault("_session_start", now_ts)


def update(
    intent_text: str,
    app_package: str,
    action_type: Optional[str] = None,
    duration_sec: Optional[float] = None,
    url: Optional[str] = None,
    title: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> Dict[str, Any]:
    """更新注意力图谱，返回兼容旧接口的 levels/weights 和新意图信息。"""
    api_key = _load_config()
    levels = _extract_levels(intent_text, api_key)
    candidates = levels.get("candidates", [])
    primary = candidates[0] if candidates else _candidate_from_levels(levels, intent_text)
    raw_action = _normalize_action_type(action_type, intent_text)
    now_ts = _safe_float(timestamp, time.time()) if timestamp is not None else time.time()
    model_engagement = _clamp(_safe_float(levels.get("engagement"), 0.7))
    engagement = _blend_engagement(model_engagement, raw_action, duration_sec)
    is_passing = _as_bool(levels.get("is_passing"), False) or raw_action == "passive_exposure" or engagement <= 0.3

    evidence_id = f"evt-{int(now_ts * 1000)}"
    evidence = {
        "id": evidence_id,
        "raw_intent": intent_text,
        "app": app_package,
        "action_type": raw_action,
        "timestamp": now_ts,
        "time": datetime.fromtimestamp(now_ts).isoformat(),
        "engagement": round(engagement, 3),
        "duration_sec": duration_sec,
        "url": url,
        "title": title,
        "is_passing": is_passing,
        "valuable": levels.get("valuable", True),
        "candidates": [
            {
                "domain": item["domain"],
                "topic": item["topic"],
                "entity": item["entity"],
                "intent_goal": item["intent_goal"],
                "confidence": item["confidence"],
                "decay_profile": item["decay_profile"],
            }
            for item in candidates[:3]
        ],
    }

    graph = _load_graph()
    evidence_log = graph.get("evidence_log", [])
    evidence_log.append(evidence)
    graph["evidence_log"] = evidence_log[-MAX_EVIDENCE_LOG:]

    valuable = _as_bool(levels.get("valuable"), True)
    intent_type = str(levels.get("intent_type", "content"))
    blocked_goal = primary.get("intent_goal") in {"task_completion", "system_operation", "accidental_exposure"}
    if not valuable or intent_type in {"tool", "system"} or raw_action == "system_noise" or blocked_goal:
        graph["updated_at"] = datetime.now().isoformat()
        _save_graph(graph)
        print(f"[attention] 低价值/系统意图，记录 evidence 但不入图: {intent_text[:50]}", flush=True)
        return {
            "levels": levels,
            "weights": {"level1": 0, "level2": 0, "level3": 0},
            "valuable": False,
            "evidence": evidence,
            "primary_node_key": None,
        }

    levels["engagement"] = engagement
    levels["is_passing"] = is_passing
    print(
        f"[attention] 意图: {primary['domain']} → {primary['topic']} → {primary['entity']} "
        f"goal={primary['intent_goal']} eng={engagement:.2f} conf={primary['confidence']:.2f}",
        flush=True,
    )

    weights, legacy_keys = _update_legacy_nodes(graph, primary, evidence)
    primary_node_key = _update_intent_nodes(graph, candidates, evidence)
    _clean_low_signal_nodes(graph)
    _update_session(graph, evidence, primary)

    graph["updated_at"] = datetime.now().isoformat()
    _save_graph(graph)

    return {
        "levels": levels,
        "weights": weights,
        "legacy_keys": legacy_keys,
        "evidence": evidence,
        "primary_node_key": primary_node_key,
        "primary_candidate": primary,
    }


def _estimate_novelty(node: dict, now_ts: float) -> float:
    last_pushed = _safe_float(node.get("last_pushed"), 0)
    if last_pushed <= 0:
        return 1.0
    freshness_hours = max(1, _safe_int(node.get("freshness_hours"), 48))
    elapsed_hours = (now_ts - last_pushed) / 3600
    return _clamp(elapsed_hours / max(1, freshness_hours))


def _fatigue_score(node: dict, now_ts: float) -> float:
    last_pushed = _safe_float(node.get("last_pushed"), 0)
    if last_pushed <= 0:
        return 0.0
    cooldown = _profile_for_node(node).get("cooldown_seconds", 3600)
    elapsed = now_ts - last_pushed
    if elapsed >= cooldown:
        return 0.0
    return _clamp(1 - elapsed / cooldown)


def _push_score(node: dict, now_ts: Optional[float] = None) -> Dict[str, float]:
    now = now_ts or time.time()
    relevance = _clamp(_safe_float(node.get("score", node.get("weight")), 0))
    novelty = _estimate_novelty(node, now)
    urgency = _profile_for_node(node).get("urgency", 0.5)
    confidence = _clamp(_safe_float(node.get("confidence"), 0.65))
    fatigue = _fatigue_score(node, now)
    score = relevance * 0.35 + novelty * 0.25 + urgency * 0.20 + confidence * 0.15 - fatigue * 0.25
    return {
        "score": round(_clamp(score), 4),
        "relevance": round(relevance, 4),
        "novelty": round(novelty, 4),
        "urgency": round(urgency, 4),
        "confidence": round(confidence, 4),
        "fatigue": round(fatigue, 4),
    }


def get_trigger(
    intent_text: str,
    app_package: str,
    action_type: Optional[str] = None,
    duration_sec: Optional[float] = None,
    url: Optional[str] = None,
    title: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> Optional[Tuple[str, str, str, bool, int]]:
    """更新图谱并判断是否触发推送。返回旧格式 tuple 以兼容 server.py。"""
    result = update(
        intent_text,
        app_package,
        action_type=action_type,
        duration_sec=duration_sec,
        url=url,
        title=title,
        timestamp=timestamp,
    )
    if not result.get("valuable", True):
        return None

    levels = result["levels"]
    primary_key = result.get("primary_node_key")
    now_ts = time.time()

    if primary_key:
        graph = _load_graph()
        node = graph.get("intent_nodes", {}).get(primary_key)
        if node:
            metrics = _push_score(node, now_ts)
            state = node.get("state", "observed")
            cooldown_until = _safe_float(node.get("cooldown_until"), 0)
            if (
                state in {"warming", "active", "dormant"}
                and metrics["score"] >= INTENT_PUSH_THRESHOLD
                and metrics["novelty"] >= MIN_NOVELTY
                and metrics["confidence"] >= MIN_TRIGGER_CONFIDENCE
                and cooldown_until <= now_ts
            ):
                profile = _profile_for_node(node)
                node["last_pushed"] = now_ts
                node["cooldown_until"] = now_ts + profile.get("cooldown_seconds", 3600)
                node["state"] = "saturated"
                graph["intent_nodes"][primary_key] = node

                # 同步 legacy 节点的推送时间，降低短时间重复触发。
                for level_name, key in result.get("legacy_keys", {}).items():
                    legacy = graph.get("nodes", {}).get(key)
                    if legacy:
                        legacy["last_pushed"] = now_ts
                        legacy["cooldown_until"] = node["cooldown_until"]
                        legacy["weight"] = _calc_weight(legacy)
                        legacy["state"] = _state_for_node(legacy, THRESHOLD.get(legacy.get("level"), 0.6), now_ts)
                        _last_triggered[f"{level_name}:{legacy.get('label')}"] = now_ts

                graph["updated_at"] = datetime.now().isoformat()
                _save_graph(graph)
                print(f"[attention] ✓ 触发意图 {primary_key} push_score={metrics['score']}", flush=True)
                return (
                    "intent",
                    node.get("topic", levels["level2"]),
                    node.get("domain", levels["level1"]),
                    _as_bool(node.get("time_sensitive"), levels.get("time_sensitive", False)),
                    _safe_int(node.get("freshness_hours"), levels.get("freshness_hours", 48)),
                )
            print(
                f"[attention] 未触发: state={state} push_score={metrics['score']} "
                f"novelty={metrics['novelty']} confidence={metrics['confidence']}",
                flush=True,
            )

    # 兼容旧 level 阈值：在 intent push 未达标时，仍允许强烈 level3/2 触发。
    weights = result.get("weights", {})
    single_engagement = _safe_float(levels.get("engagement"), 0.7)
    single_is_passing = _as_bool(levels.get("is_passing"), False)
    if single_is_passing and single_engagement <= 0.25:
        print(f"[attention] 被动路过，不触发推送 engagement={single_engagement:.2f}", flush=True)
        return None

    for level_key in ["level3", "level2", "level1"]:
        label = levels[level_key]
        weight = _safe_float(weights.get(level_key), 0)
        threshold = THRESHOLD[level_key]
        if weight < threshold:
            continue

        legacy_key = result.get("legacy_keys", {}).get(level_key)
        if legacy_key:
            legacy_graph = _load_graph()
            legacy_node = legacy_graph.get("nodes", {}).get(legacy_key, {})
            if legacy_node.get("state") == "saturated" or _safe_float(legacy_node.get("cooldown_until"), 0) > now_ts:
                print(f"[attention] {legacy_key} 已进入疲劳期，跳过 legacy 触发", flush=True)
                continue

        cooldown_key = f"{level_key}:{label}"
        last = _last_triggered.get(cooldown_key, 0)
        cooldown = COOLDOWN[level_key]
        if now_ts - last < cooldown:
            remaining = int((cooldown - (now_ts - last)) / 60)
            print(f"[attention] {cooldown_key} 冷却中，还需 {remaining} 分钟", flush=True)
            continue

        _last_triggered[cooldown_key] = now_ts
        print(f"[attention] ✓ 触发 legacy {level_key} '{label}' weight={weight:.3f}", flush=True)
        return (
            level_key,
            label,
            levels["level1"],
            _as_bool(levels.get("time_sensitive"), False),
            _safe_int(levels.get("freshness_hours"), 48),
        )

    return None


def apply_feedback(
    feed_id: Optional[str],
    action: str,
    reason: Optional[str] = None,
    topic: Optional[str] = None,
    trigger_node: Optional[str] = None,
) -> Dict[str, Any]:
    """接收用户反馈并反向调整意图节点。"""
    graph = _load_graph()
    now_ts = time.time()
    action_norm = _normalize_action_type(action, "")
    feedback = {
        "feed_id": feed_id,
        "action": action,
        "reason": reason,
        "topic": topic,
        "trigger_node": trigger_node,
        "ts": now_ts,
        "time": datetime.now().isoformat(),
    }
    feedback_log = graph.get("feedback_log", [])
    feedback_log.append(feedback)
    graph["feedback_log"] = feedback_log[-MAX_FEEDBACK_LOG:]

    affected: List[str] = []
    intent_nodes = graph.get("intent_nodes", {})
    candidate_keys = []
    if trigger_node and trigger_node in intent_nodes:
        candidate_keys.append(trigger_node)
    if topic:
        candidate_keys.extend([
            key for key, node in intent_nodes.items()
            if topic in str(node.get("topic", "")) or topic in str(node.get("entity", ""))
        ])

    for key in dict.fromkeys(candidate_keys):
        node = intent_nodes.get(key)
        if not node:
            continue
        if action in NEGATIVE_FEEDBACK_ACTIONS or action_norm == "negative_signal":
            node["negative_count"] = _safe_int(node.get("negative_count"), 0) + 1
            node["cooldown_until"] = max(_safe_float(node.get("cooldown_until"), 0), now_ts + 24 * 60 * 60)
        elif action in POSITIVE_FEEDBACK_ACTIONS:
            node["active_count"] = _safe_int(node.get("active_count"), 0) + 1
            _append_signal(node, f"feedback-{int(now_ts * 1000)}", now_ts, 0.85, node.get("confidence", 0.7), "active_read")

        node["score"] = _calc_weight(node)
        node["state"] = _state_for_node(node, INTENT_PUSH_THRESHOLD, now_ts)
        intent_nodes[key] = node
        affected.append(key)

    graph["intent_nodes"] = intent_nodes

    if topic:
        for key, node in graph.get("nodes", {}).items():
            if topic not in str(node.get("label", "")):
                continue
            if action in NEGATIVE_FEEDBACK_ACTIONS or action_norm == "negative_signal":
                node["negative_count"] = _safe_int(node.get("negative_count"), 0) + 1
                node["cooldown_until"] = max(_safe_float(node.get("cooldown_until"), 0), now_ts + 24 * 60 * 60)
            elif action in POSITIVE_FEEDBACK_ACTIONS:
                node["active_count"] = _safe_int(node.get("active_count"), 0) + 1
                _append_signal(node, f"feedback-{int(now_ts * 1000)}", now_ts, 0.85, node.get("confidence", 0.7), "active_read")
            node["weight"] = _calc_weight(node)
            node["state"] = _state_for_node(node, THRESHOLD.get(node.get("level"), 0.6), now_ts)

    graph["updated_at"] = datetime.now().isoformat()
    _save_graph(graph)
    return {"ok": True, "affected": affected, "feedback": feedback}


def _bucket_hour(hour: int) -> str:
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 23:
        return "evening"
    return "night"


def _content_preferences_from_modes(intent_modes: Dict[str, float]) -> List[dict]:
    mapping = {
        "follow_event_update": ("即时动态", 1.0),
        "follow_news": ("资讯简报", 0.85),
        "learn_analysis": ("深度分析", 0.95),
        "compare_decision": ("对比决策", 0.9),
        "entertainment_browse": ("轻量内容", 0.65),
    }
    prefs: Dict[str, float] = {}
    for goal, score in intent_modes.items():
        label, multiplier = mapping.get(goal, ("资讯简报", 0.5))
        prefs[label] = prefs.get(label, 0.0) + score * multiplier
    return [
        {"type": label, "score": round(score, 3)}
        for label, score in sorted(prefs.items(), key=lambda item: -item[1])[:5]
    ]


def _fallback_legacy_profile(graph: dict) -> List[dict]:
    items = []
    for key, node in graph.get("nodes", {}).items():
        if node.get("level") == "level1":
            continue
        weight = _safe_float(node.get("weight"), 0)
        if weight < 0.25:
            continue
        items.append({
            "key": key,
            "topic": node.get("label", key),
            "domain": node.get("parent") or node.get("label"),
            "score": round(weight, 3),
            "confidence": round(_safe_float(node.get("confidence"), 0.55), 2),
            "source": "legacy_node",
        })
    return sorted(items, key=lambda item: -item["score"])[:10]


def get_recommendation_profile() -> dict:
    """构建推荐层可直接使用的偏好画像，而不是抽象人格标签。"""
    graph = _load_graph()
    intent_nodes = graph.get("intent_nodes", {})
    evidence_log = graph.get("evidence_log", [])

    stable_interests = []
    short_term_focus = []
    negative_constraints = []
    intent_mode_scores: Dict[str, float] = {}
    domain_scores: Dict[str, float] = {}

    for key, node in intent_nodes.items():
        state = node.get("state", "observed")
        score = _safe_float(node.get("score"), 0)
        confidence = _safe_float(node.get("confidence"), 0.65)
        goal = str(node.get("intent_goal") or "follow_news")
        domain = str(node.get("domain") or "其他")
        topic = str(node.get("topic") or key)
        entity = str(node.get("entity") or "")
        decay_profile = str(node.get("decay_profile") or "")
        negative_count = _safe_int(node.get("negative_count"), 0)

        weighted = score * (0.5 + confidence)
        if state != "rejected":
            intent_mode_scores[goal] = intent_mode_scores.get(goal, 0.0) + weighted
            domain_scores[domain] = domain_scores.get(domain, 0.0) + weighted

        base = {
            "key": key,
            "topic": topic,
            "entity": entity,
            "domain": domain,
            "intent_goal": goal,
            "score": round(score, 3),
            "confidence": round(confidence, 2),
            "state": state,
            "decay_profile": decay_profile,
        }

        if state == "rejected" or negative_count > 0:
            negative_constraints.append({
                "topic": topic,
                "entity": entity,
                "reason": "negative_feedback" if negative_count else "rejected",
                "negative_count": negative_count,
            })
            continue

        if decay_profile in {"stable_interest", "identity_preference", "seasonal_interest"} and score >= 0.22:
            stable_interests.append(base)

        if state in {"warming", "active", "saturated"} and score >= 0.30:
            short_term_focus.append(base)

    if not stable_interests:
        stable_interests = _fallback_legacy_profile(graph)
        for item in stable_interests:
            domain_scores[item["domain"]] = domain_scores.get(item["domain"], 0.0) + item["score"]

    intent_modes = [
        {"intent_goal": goal, "score": round(score, 3)}
        for goal, score in sorted(intent_mode_scores.items(), key=lambda item: -item[1])
    ]
    dominant_domains = [
        {"domain": domain, "score": round(score, 3)}
        for domain, score in sorted(domain_scores.items(), key=lambda item: -item[1])[:5]
    ]

    period_scores: Dict[str, float] = {}
    for evidence in evidence_log[-200:]:
        ts = _safe_float(evidence.get("timestamp"), 0)
        if not ts:
            continue
        hour = datetime.fromtimestamp(ts).hour
        bucket = _bucket_hour(hour)
        period_scores[bucket] = period_scores.get(bucket, 0.0) + _safe_float(evidence.get("engagement"), 0.5)

    temporal_patterns = [
        {"period": period, "score": round(score, 3)}
        for period, score in sorted(period_scores.items(), key=lambda item: -item[1])
    ]

    mode_lookup = {item["intent_goal"]: item["score"] for item in intent_modes}
    service_signal = mode_lookup.get("learn_analysis", 0.0) + mode_lookup.get("compare_decision", 0.0)
    event_signal = mode_lookup.get("follow_event_update", 0.0)
    digest_signal = sum(item["score"] for item in stable_interests[:5])

    recommendation_policy = {
        "service_cards": "only_when_need_signal" if service_signal > 0.2 else "paused_until_need_signal",
        "instant_push": "time_sensitive_only" if event_signal > 0.2 else "limited",
        "digest": "daily" if digest_signal > 0.5 else "when_enough_signal",
        "exploration": "conservative",
    }

    return {
        "stable_interests": sorted(stable_interests, key=lambda item: -item.get("score", 0))[:10],
        "short_term_focus": sorted(short_term_focus, key=lambda item: -item.get("score", 0))[:10],
        "intent_modes": intent_modes[:8],
        "dominant_domains": dominant_domains,
        "preferred_content": _content_preferences_from_modes(mode_lookup),
        "negative_constraints": negative_constraints[:10],
        "temporal_patterns": temporal_patterns,
        "recommendation_policy": recommendation_policy,
        "updated_at": graph.get("updated_at", ""),
    }


def get_graph_summary() -> dict:
    """返回图谱摘要，供调试和巡逻模块使用。"""
    graph = _load_graph()
    nodes = graph.get("nodes", {})
    intent_nodes = graph.get("intent_nodes", {})
    sessions = graph.get("sessions", [])
    now = time.time()

    legacy_summary = []
    for key, node in sorted(nodes.items(), key=lambda item: -_safe_float(item[1].get("weight"), 0)):
        windows = _score_windows(node, now)
        legacy_summary.append({
            "key": key,
            "label": node.get("label", key),
            "level": node.get("level", "level2"),
            "parent": node.get("parent"),
            "weight": round(_safe_float(node.get("weight"), 0), 3),
            "state": node.get("state", "observed"),
            "mentions": _safe_int(node.get("mentions"), 0),
            "active_count": _safe_int(node.get("active_count"), 0),
            "passing_count": _safe_int(node.get("passing_count"), 0),
            "negative_count": _safe_int(node.get("negative_count"), 0),
            "avg_engagement": round(_safe_float(node.get("avg_engagement"), 0.7), 2),
            "confidence": round(_safe_float(node.get("confidence"), 0.65), 2),
            "short_score": round(windows["short"], 3),
            "mid_score": round(windows["mid"], 3),
            "long_score": round(windows["long"], 3),
            "decay_profile": node.get("decay_profile"),
            "intent_goal": node.get("intent_goal"),
            "platforms": node.get("platforms", []),
        })

    intent_summary = []
    for key, node in sorted(intent_nodes.items(), key=lambda item: -_safe_float(item[1].get("score"), 0)):
        metrics = _push_score(node, now)
        intent_summary.append({
            "key": key,
            "domain": node.get("domain"),
            "topic": node.get("topic"),
            "entity": node.get("entity"),
            "intent_goal": node.get("intent_goal"),
            "state": node.get("state"),
            "score": round(_safe_float(node.get("score"), 0), 3),
            "push_score": metrics["score"],
            "confidence": round(_safe_float(node.get("confidence"), 0.65), 2),
            "evidence_count": _safe_int(node.get("evidence_count"), 0),
            "active_count": _safe_int(node.get("active_count"), 0),
            "passive_count": _safe_int(node.get("passive_count"), 0),
            "negative_count": _safe_int(node.get("negative_count"), 0),
            "decay_profile": node.get("decay_profile"),
            "time_sensitive": _as_bool(node.get("time_sensitive"), False),
            "freshness_hours": _safe_int(node.get("freshness_hours"), 48),
            "last_seen": node.get("last_seen"),
            "last_pushed": node.get("last_pushed"),
        })

    hot_intents = [
        item for item in intent_summary
        if item["state"] in {"warming", "active", "saturated"} and item["score"] >= 0.4
    ][:10]
    long_term_preferences = [
        item for item in intent_summary
        if item["decay_profile"] in {"stable_interest", "identity_preference"} and item["score"] >= 0.25
    ][:10]

    return {
        "version": graph.get("version", GRAPH_VERSION),
        "nodes": legacy_summary,
        "intent_nodes": intent_summary[:50],
        "hot_intents": hot_intents,
        "long_term_preferences": long_term_preferences,
        "sessions": sessions[-10:],
        "evidence_count": len(graph.get("evidence_log", [])),
        "feedback_count": len(graph.get("feedback_log", [])),
        "updated_at": graph.get("updated_at", ""),
    }
