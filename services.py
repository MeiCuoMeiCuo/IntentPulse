"""
services.py — 基于意图图谱的服务/商品推荐模块

原则：
- 不再因为话题权重高就推荐服务
- 只在 learn_analysis / compare_decision 等明确需求意图下生成卡片
- 推荐结果写入 feed.json 的 services 字段
"""

import json
import os
import time
import logging
from typing import Optional

from settings import get_openrouter_api_key

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ATTENTION_FILE = os.path.join(BASE_DIR, "attention.json")
FEED_FILE = os.path.join(BASE_DIR, "feed.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

CARD_PRODUCT = "product"
CARD_COURSE = "course"
CARD_VENUE = "venue"
CARD_APP = "app"
CARD_VIDEO = "video"
CARD_MARKET = "market"

SERVICE_ELIGIBLE_GOALS = {"learn_analysis", "compare_decision"}
SERVICE_MIN_SCORE = 0.22
MAX_SERVICE_TOPICS = 3

NODE_RULES = {
    "NBA篮球": ("NBA赛事直播 会员 赛程数据工具", [CARD_APP, CARD_VIDEO], CARD_APP),
    "NBA季后赛": ("NBA季后赛深度分析 视频 数据工具", [CARD_VIDEO, CARD_APP], CARD_VIDEO),
    "德州扑克": ("德州扑克培训课程 GTO软件", [CARD_COURSE, CARD_APP], CARD_COURSE),
    "MTT策略": ("MTT锦标赛策略课程 扑克培训", [CARD_COURSE, CARD_VIDEO], CARD_COURSE),
    "拳击": ("拳击训练课程 拳击装备", [CARD_COURSE, CARD_PRODUCT], CARD_COURSE),
    "BTC": ("比特币 BTC 实时行情 交易所App", [CARD_MARKET, CARD_APP], CARD_MARKET),
    "加密货币": ("加密货币行情 交易所App 风险教育课程", [CARD_MARKET, CARD_APP, CARD_COURSE], CARD_MARKET),
    "比特币": ("比特币实时价格 行情走势", [CARD_MARKET], CARD_MARKET),
    "机器人": ("人形机器人 机器人课程 机器人开发套件", [CARD_COURSE, CARD_PRODUCT], CARD_COURSE),
    "AI动态": ("AI工具 开发者课程 Agent工具", [CARD_APP, CARD_COURSE], CARD_APP),
    "AI新闻": ("AI工具 开发者课程 Agent工具", [CARD_APP, CARD_COURSE], CARD_APP),
    "健身": ("附近健身房 健身课程 训练装备", [CARD_VENUE, CARD_COURSE, CARD_PRODUCT], CARD_VENUE),
}

LEVEL1_RULES = {
    "体育": ("体育赛事深度分析 训练课程 运动装备", [CARD_VIDEO, CARD_COURSE, CARD_PRODUCT], CARD_VIDEO),
    "娱乐": ("流媒体订阅 电影票 娱乐内容平台", [CARD_APP, CARD_PRODUCT], CARD_APP),
    "科技": ("科技工具 开发者课程 AI工具", [CARD_APP, CARD_COURSE], CARD_APP),
    "财经": ("投资行情App 理财课程 风险教育", [CARD_MARKET, CARD_COURSE], CARD_MARKET),
    "生活": ("本地服务 商品购买 课程", [CARD_VENUE, CARD_PRODUCT, CARD_COURSE], CARD_VENUE),
}


def load_config() -> dict:
    return {"openrouter_api_key": get_openrouter_api_key(CONFIG_FILE)}


def load_attention() -> dict:
    try:
        with open(ATTENTION_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"nodes": {}, "intent_nodes": {}}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _goal_intent_match(goal: str) -> float:
    if goal == "compare_decision":
        return 1.0
    if goal == "learn_analysis":
        return 0.86
    return 0.0


def _timing_fit(state: str) -> float:
    if state in {"active", "warming"}:
        return 1.0
    if state == "observed":
        return 0.62
    if state == "saturated":
        return 0.45
    return 0.35


def _candidate_score(node: dict) -> float:
    attention_score = _safe_float(node.get("score", node.get("weight")), 0.0)
    confidence = _safe_float(node.get("confidence"), 0.6)
    goal = str(node.get("intent_goal", ""))
    state = str(node.get("state", "observed"))
    negative_count = _safe_int(node.get("negative_count"), 0)
    fatigue = min(negative_count / 3.0, 1.0)
    score = (
        attention_score * 0.30
        + _goal_intent_match(goal) * 0.25
        + confidence * 0.15
        + _timing_fit(state) * 0.10
        + 0.10
        - fatigue * 0.35
    )
    return round(max(0.0, min(1.0, score)), 4)


def _why_for_candidate(candidate: dict) -> str:
    topic = candidate.get("label") or candidate.get("topic")
    goal = candidate.get("intent_goal")
    if goal == "learn_analysis":
        return f"因为你最近在主动学习或分析「{topic}」，适合推荐深度内容、课程或工具。"
    if goal == "compare_decision":
        return f"因为你最近对「{topic}」表现出比较或决策意图，适合推荐可执行的服务或商品。"
    return f"因为你最近关注「{topic}」，但服务推荐会保持克制。"


def get_service_candidates(attention: dict, allow_legacy: bool = False) -> list[dict]:
    """从 intent_nodes 中挑选明确服务意图，必要时允许旧节点兜底。"""
    candidates = []
    intent_nodes = attention.get("intent_nodes", {})
    for key, node in intent_nodes.items():
        goal = str(node.get("intent_goal", ""))
        if goal not in SERVICE_ELIGIBLE_GOALS:
            continue
        if node.get("state") == "rejected" or _safe_int(node.get("negative_count"), 0) >= 2:
            continue
        attention_score = _safe_float(node.get("score"), 0.0)
        if attention_score < SERVICE_MIN_SCORE:
            continue
        item = {
            "key": key,
            "label": node.get("topic") or key,
            "topic": node.get("topic") or key,
            "entity": node.get("entity"),
            "domain": node.get("domain"),
            "level": "intent",
            "intent_goal": goal,
            "weight": attention_score,
            "confidence": _safe_float(node.get("confidence"), 0.6),
            "state": node.get("state", "observed"),
            "recommendation_score": 0.0,
            "why": "",
        }
        item["recommendation_score"] = _candidate_score(node)
        item["why"] = _why_for_candidate(item)
        candidates.append(item)

    if not candidates and allow_legacy:
        for key, node in attention.get("nodes", {}).items():
            if node.get("level") == "level1":
                continue
            weight = _safe_float(node.get("weight"), 0.0)
            if weight < 0.50:
                continue
            item = {
                "key": key,
                "label": node.get("label", key),
                "topic": node.get("label", key),
                "entity": node.get("label", key),
                "domain": node.get("parent"),
                "level": node.get("level", "level2"),
                "intent_goal": "legacy_affinity",
                "weight": weight,
                "confidence": _safe_float(node.get("confidence"), 0.55),
                "state": node.get("state", "observed"),
                "recommendation_score": round(weight * 0.45, 4),
                "why": f"这是基于旧注意力节点「{node.get('label', key)}」的兜底推荐，建议只用于手动刷新。"
            }
            candidates.append(item)

    candidates.sort(key=lambda item: -item["recommendation_score"])
    return candidates[:MAX_SERVICE_TOPICS]


def pick_query_and_types(label: str, parent: Optional[str], level: str, intent_goal: str = ""):
    for key, rule in NODE_RULES.items():
        if key in label or label in key:
            query, card_types, preferred = rule
            if intent_goal == "learn_analysis":
                card_types = [ct for ct in card_types if ct in {CARD_COURSE, CARD_VIDEO, CARD_APP}] or [CARD_COURSE]
            return query, card_types, preferred

    if intent_goal == "learn_analysis":
        return f"{label} 深度分析 课程 教程 视频", [CARD_COURSE, CARD_VIDEO], CARD_COURSE
    if intent_goal == "compare_decision":
        return f"{label} 推荐 对比 价格 工具 服务", [CARD_PRODUCT, CARD_APP], CARD_PRODUCT

    p = parent or label
    for key, rule in LEVEL1_RULES.items():
        if key in p or p in key:
            return rule
    return f"{label} 相关工具 课程推荐", [CARD_APP, CARD_COURSE], CARD_APP


def build_service_prompt(query: str, card_types: list[str], candidate: dict) -> str:
    type_desc = {
        CARD_PRODUCT: "商品（含价格、购买平台、简介）",
        CARD_COURSE: "课程（含讲师/机构、价格、时长、平台）",
        CARD_VENUE: "店家（含地址、评分、电话、营业时间）",
        CARD_APP: "App（含评分、下载量、平台、核心功能）",
        CARD_VIDEO: "视频内容（含时长、UP主、平台、简介）",
        CARD_MARKET: "行情数据（含当前价格、24h涨跌幅、7日走势描述）",
    }
    wanted = "、".join([type_desc.get(t, t) for t in card_types])
    topic = candidate.get("label", "")
    intent_goal = candidate.get("intent_goal", "")

    return f"""你是一个克制的服务推荐助手。用户当前明确意图是「{intent_goal}」，关注主题是「{topic}」。

只在这个意图确实适合服务、课程、工具、商品时推荐。不要把普通新闻关注强行转成购买建议。

请搜索并返回3~5条真实可用的推荐，类型包括：{wanted}

搜索关键词参考：{query}

严格要求：
1. 必须是真实存在的服务/商品/App/课程/视频，不能编造。
2. action_url 必须是完整 URL。
3. card_type 只能是：{', '.join(card_types)}。
4. 每条推荐都要解释 why，说明它为什么匹配用户当前意图。
5. 只返回 JSON 数组，不要有说明文字。

Schema：
{{
  "card_type": "{card_types[0]}",
  "title": "名称",
  "subtitle": "简短描述（20字内）",
  "price": "价格或 null",
  "rating": 4.5,
  "platform": "平台名",
  "action_label": "行动按钮文字",
  "action_url": "https://...",
  "image_url": "封面图URL或 null",
  "why": "为什么适合这个用户当前意图",
  "meta": {{}}
}}

直接输出 JSON 数组："""


def call_sonar(prompt: str, api_key: str) -> str:
    import requests

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "perplexity/sonar-pro",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2000,
            "temperature": 0.25,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_service_cards(raw: str, card_types: list[str], candidate: dict) -> list[dict]:
    import re

    match = re.search(r"\[[\s\S]*\]", raw)
    if not match:
        return []
    try:
        cards = json.loads(match.group())
    except Exception:
        return []

    result = []
    valid_types = {CARD_PRODUCT, CARD_COURSE, CARD_VENUE, CARD_APP, CARD_VIDEO, CARD_MARKET}
    topic = candidate.get("label", "")
    for c in cards:
        if not isinstance(c, dict):
            continue
        ct = c.get("card_type", card_types[0])
        if ct not in valid_types or ct not in card_types:
            ct = card_types[0]
        action_url = c.get("action_url")
        if action_url and not str(action_url).startswith("http"):
            action_url = None
        result.append({
            "id": f"svc-{int(time.time()*1000)}-{len(result)}",
            "card_type": ct,
            "topic": topic,
            "trigger_node": candidate.get("key"),
            "intent_goal": candidate.get("intent_goal"),
            "topic_weight": candidate.get("weight", 0),
            "recommendation_score": candidate.get("recommendation_score", 0),
            "title": c.get("title", ""),
            "subtitle": c.get("subtitle", ""),
            "price": c.get("price"),
            "rating": c.get("rating"),
            "platform": c.get("platform"),
            "action_label": c.get("action_label", "查看详情"),
            "action_url": action_url,
            "image_url": c.get("image_url"),
            "why": c.get("why") or candidate.get("why"),
            "meta": c.get("meta", {}),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return result


def _write_services(cards: list[dict], candidates: list[dict], status: str) -> None:
    try:
        with open(FEED_FILE, encoding="utf-8") as f:
            feed = json.load(f)
    except Exception:
        feed = {}
    feed["services"] = cards
    feed["services_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    feed["services_meta"] = {
        "status": status,
        "candidate_count": len(candidates),
        "candidates": [
            {
                "topic": c.get("label"),
                "intent_goal": c.get("intent_goal"),
                "recommendation_score": c.get("recommendation_score"),
                "why": c.get("why"),
            }
            for c in candidates
        ],
    }
    tmp = FEED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FEED_FILE)


def fetch_services(force: bool = False) -> list[dict]:
    """
    主入口：读 intent_nodes → 只为明确学习/决策意图生成服务卡片。
    force=True 时允许旧 attention nodes 兜底，方便人工调试。
    """
    cfg = load_config()
    api_key = cfg.get("openrouter_api_key", "")
    if not api_key:
        logger.error("No OpenRouter API key in config.json")
        _write_services([], [], "missing_api_key")
        return []

    attention = load_attention()
    candidates = get_service_candidates(attention, allow_legacy=force)
    if not candidates:
        logger.info("[services] no eligible service intent; writing empty services")
        _write_services([], [], "no_service_intent")
        return []

    all_cards = []
    seen_queries = set()
    for candidate in candidates:
        query, card_types, _ = pick_query_and_types(
            candidate["label"],
            candidate.get("domain"),
            candidate.get("level", "intent"),
            candidate.get("intent_goal", ""),
        )
        if query in seen_queries:
            continue
        seen_queries.add(query)

        logger.info(
            "[services] fetching for %s goal=%s score=%.2f",
            candidate["label"],
            candidate.get("intent_goal"),
            candidate.get("recommendation_score", 0),
        )
        try:
            prompt = build_service_prompt(query, card_types, candidate)
            raw = call_sonar(prompt, api_key)
            all_cards.extend(parse_service_cards(raw, card_types, candidate))
        except Exception as e:
            logger.error(f"[services] failed for 「{candidate['label']}」: {e}")

    _write_services(all_cards, candidates, "ok" if all_cards else "empty_result")
    logger.info(f"[services] total {len(all_cards)} cards written to feed.json")
    return all_cards


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cards = fetch_services(force=True)
    print(json.dumps(cards, ensure_ascii=False, indent=2))
