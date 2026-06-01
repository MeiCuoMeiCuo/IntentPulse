"""
马斯洛用户画像 v2
修正分类逻辑：科技/扑克/体育/NBA → 自我实现（技能、竞技、探索）
内容浏览本身不等于自我实现，以话题领域判断而非行为类型
"""
import json
import os
import time
from datetime import datetime
from typing import Dict, List

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
MASLOW_PATH = os.path.join(ROOT_DIR, "maslow.json")
ATTENTION_PATH = os.path.join(ROOT_DIR, "attention.json")

MASLOW_LEVELS = [
    "生理需求",
    "安全需求",
    "社交需求",
    "尊重需求",
    "自我实现",
]

MASLOW_KEYWORDS: Dict[str, List[str]] = {
    "生理需求": [
        "健康", "饮食", "睡眠", "运动", "健身", "医疗", "养生", "减肥",
        "跑步", "游泳", "骑行", "卡路里", "营养", "体检", "药物", "疾病",
        "中医", "西医", "手术", "康复", "保健",
    ],
    "安全需求": [
        "投资", "理财", "股票", "基金", "保险", "房产", "贷款", "税务",
        "加密货币", "BTC", "ETH", "比特币", "以太坊", "合约", "期货",
        "行情", "美元", "汇率", "财经", "金融", "经济", "资产", "收益",
        "风险", "避险", "通胀", "降息", "央行", "法律", "隐私", "安全",
    ],
    "社交需求": [
        "综艺", "明星", "八卦", "娱乐圈", "偶像", "追剧", "电视剧",
        "短视频", "直播", "网红", "粉丝", "流量", "热搜",
        "社交", "朋友", "聚会", "约会", "恋爱", "婚姻", "家庭",
        "时尚", "潮流", "穿搭", "美妆", "护肤",
    ],
    "尊重需求": [
        "职业", "晋升", "工作", "面试", "简历", "薪资", "职场",
        "管理", "领导", "团队", "项目", "绩效",
        "奢侈", "豪车", "手表", "名牌", "限量", "豪宅",
        "排名", "榜单", "奖项",
    ],
    "自我实现": [
        # 技能与学习
        "学习", "课程", "编程", "代码", "算法", "数学", "物理",
        "AI", "机器学习", "深度学习", "模型", "训练", "科技", "技术",
        "产品", "设计", "架构", "工程",
        # 竞技体育（技能+竞技）
        "拳击", "格斗", "搏击", "MMA", "拳法", "战术",
        "NBA", "篮球", "足球", "网球", "乒乓", "羽毛球",
        "战术分析", "技术统计", "赛季", "季后赛", "联赛", "赛事",
        # 竞技思维
        "扑克", "德州", "GTO", "牌局", "策略", "博弈",
        "国际象棋", "围棋", "电竞",
        # 探索与创作
        "探索", "研究", "哲学", "科学", "宇宙", "历史", "纪录片",
        "创作", "写作", "音乐", "摄影", "绘画", "艺术",
        # 个人成长
        "成长", "自我提升", "冥想", "心理", "认知",
    ],
}

DOMAIN_OVERRIDE: Dict[str, str] = {
    "科技": "自我实现",
    "体育": "自我实现",
    "竞技": "自我实现",
    "学习": "自我实现",
    "金融": "安全需求",
    "健康": "生理需求",
    "医疗": "生理需求",
    "娱乐": "社交需求",
    "时尚": "社交需求",
    "职场": "尊重需求",
}


def _load_attention() -> dict:
    if not os.path.exists(ATTENTION_PATH):
        return {"nodes": {}}
    with open(ATTENTION_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_maslow() -> dict:
    if not os.path.exists(MASLOW_PATH):
        return {
            "profile": {lvl: {"score": 0.0, "mentions": 0, "top_topics": []} for lvl in MASLOW_LEVELS},
            "dominant": None,
            "snapshots": [],
            "updated_at": "",
        }
    with open(MASLOW_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_maslow(data: dict) -> None:
    tmp = MASLOW_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MASLOW_PATH)


def _classify_node(label: str, level: str, parent: str = "") -> List[str]:
    # 1. level1 领域直接 override
    if level == "level1":
        for domain, maslow_level in DOMAIN_OVERRIDE.items():
            if domain in label:
                return [maslow_level]

    # 2. parent 领域 override
    if parent:
        for domain, maslow_level in DOMAIN_OVERRIDE.items():
            if domain in parent:
                return [maslow_level]

    # 3. 关键词匹配（优先匹配高层需求）
    for ml in reversed(MASLOW_LEVELS):
        for kw in MASLOW_KEYWORDS[ml]:
            if kw in label:
                return [ml]

    return ["尊重需求"]


def rebuild_profile() -> dict:
    graph = _load_attention()
    nodes = graph.get("nodes", {})

    level_scores: Dict[str, float] = {lvl: 0.0 for lvl in MASLOW_LEVELS}
    level_mentions: Dict[str, int] = {lvl: 0 for lvl in MASLOW_LEVELS}
    level_topics: Dict[str, List[dict]] = {lvl: [] for lvl in MASLOW_LEVELS}

    for key, node in nodes.items():
        label = node.get("label", "")
        weight = node.get("weight", 0.0)
        mentions = node.get("mentions", 0)
        level = node.get("level", "level2")
        parent = node.get("parent", "") or ""
        avg_engagement = node.get("avg_engagement", 0.7)
        active_count = node.get("active_count", 0)
        passing_count = node.get("passing_count", 0)

        # 路过节点大幅降权
        if passing_count > active_count * 2:
            weight *= 0.3

        multiplier = 1.5 if level == "level1" else (1.0 if level == "level2" else 0.7)
        engagement_bonus = 0.5 + avg_engagement
        effective_weight = weight * multiplier * engagement_bonus

        matched_levels = _classify_node(label, level, parent)
        for ml in matched_levels:
            level_scores[ml] += effective_weight
            level_mentions[ml] += mentions
            level_topics[ml].append({
                "label": label,
                "weight": round(weight, 3),
                "mentions": mentions,
                "engagement": round(avg_engagement, 2),
            })

    total = sum(level_scores.values()) or 1.0
    profile = {}
    for lvl in MASLOW_LEVELS:
        score_pct = round(level_scores[lvl] / total * 100, 1)
        top = sorted(
            level_topics[lvl],
            key=lambda x: -(x["weight"] * (0.5 + x.get("engagement", 0.7)))
        )[:5]
        profile[lvl] = {
            "score": score_pct,
            "mentions": level_mentions[lvl],
            "top_topics": [t["label"] for t in top],
        }

    dominant = max(MASLOW_LEVELS, key=lambda lvl: profile[lvl]["score"])

    maslow_data = _load_maslow()
    snapshots = maslow_data.get("snapshots", [])
    now_str = datetime.now().isoformat()

    should_snapshot = True
    if snapshots:
        last_ts = snapshots[-1].get("time", "")
        try:
            last_epoch = datetime.fromisoformat(last_ts).timestamp()
            if time.time() - last_epoch < 3600:
                should_snapshot = False
        except Exception:
            pass

    if should_snapshot:
        snapshots.append({
            "time": now_str,
            "dominant": dominant,
            "scores": {lvl: profile[lvl]["score"] for lvl in MASLOW_LEVELS},
        })
        snapshots = snapshots[-30:]

    result = {
        "profile": profile,
        "dominant": dominant,
        "snapshots": snapshots,
        "updated_at": now_str,
    }
    _save_maslow(result)
    return result


def get_profile() -> dict:
    maslow_data = _load_maslow()
    attention_data = _load_attention()
    att_updated = attention_data.get("updated_at", "")
    mas_updated = maslow_data.get("updated_at", "")
    if att_updated > mas_updated or not mas_updated:
        return rebuild_profile()
    return maslow_data
