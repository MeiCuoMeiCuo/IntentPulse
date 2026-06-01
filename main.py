import json
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from settings import get_openrouter_api_key

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "perplexity/sonar-pro"

FIELDS: List[Tuple[str, str]] = [
    ("AI", "🤖"),
    ("BTC", "₿"),
    ("硬件科技", "💻"),
    ("NBA", "🏀"),
    ("时尚", "👗"),
]

FIELD_EMOJI = {name: emoji for name, emoji in FIELDS}


def _load_config(config_path: str) -> Dict[str, str]:
    return {"openrouter_api_key": get_openrouter_api_key(config_path)}


def _http_post_json(url: str, headers: Dict[str, str], payload: Dict) -> Dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    with urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _parse_bullets(content: str) -> List[str]:
    lines = [ln.strip() for ln in str(content).splitlines() if ln.strip()]
    bullets = []
    for ln in lines:
        if ln.startswith("-"):
            bullets.append(ln[1:].strip())
        elif ln.startswith("•"):
            bullets.append(ln[1:].strip())
    bullets = [b for b in bullets if b]
    if len(bullets) < 3:
        candidates = [ln.lstrip("-•0123456789.)").strip() for ln in lines]
        candidates = [c for c in candidates if len(c) > 5]
        bullets = candidates[:3]
    return (bullets + ["（暂无）"] * 3)[:3]


AUTHORITY_SOURCES = """
优先使用以下权威信息源：
- 中文：新华社、人民日报、澎湃新闻、36氪、虎扑、雪球、腾讯体育、新浪体育
- 英文：Reuters、AP News、ESPN、Bloomberg、The Athletic、BBC Sport
- 日文体育：日刊スポーツ、スポーツ報知、ニッカンスポーツ
"""

def _fetch_single_point(api_key: str, query: str, dimension: str, today: str) -> dict:
    """抓取单个维度的要点，返回 text + sourceUrl + sourceName"""
    from datetime import datetime, timedelta
    cutoff_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    system = (
        "你是一名联网资讯助理。只输出JSON，不要其他内容。"
        f"今天是{today}。"
        f"严格限制：只能返回{cutoff_date}之后发布的内容，超过30天的文章一律不使用。"
        f"{AUTHORITY_SOURCES}"
    )
    user = f"""
搜索：{query}
维度：{dimension}

输出格式（严格JSON）：
{{"text": "一句话要点，信息密度高，50字以内", "sourceUrl": "原文URL", "sourceName": "来源名称（如：ESPN、虎扑）", "imageUrl": "文章封面图URL，没有则null", "publishDate": "文章发布日期YYYY-MM-DD"}}

硬性要求（违反则返回空结果）：
- 只使用{cutoff_date}之后发布的文章，禁止使用更早的内容
- sourceUrl：必须是可访问的完整URL
- sourceName：简短的来源名称
- publishDate：文章实际发布日期，不确定则填今天日期
- 如果30天内找不到相关内容，text填"（近期暂无相关资讯）"，其他填null
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
        data = _http_post_json(OPENROUTER_ENDPOINT, headers, payload)
        raw = data["choices"][0]["message"]["content"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        if isinstance(result, list):
            result = result[0] if result else {}
        text = str(result.get("text", "")).strip()
        source_url = result.get("sourceUrl")
        source_name = result.get("sourceName")
        point_image = result.get("imageUrl")
        publish_date = result.get("publishDate", "")

        # 后端二次校验：如果返回了发布日期且超过30天，丢弃
        if publish_date:
            try:
                from datetime import datetime as dt
                pub = dt.strptime(publish_date[:10], "%Y-%m-%d")
                if (datetime.now() - pub).days > 30:
                    print(f"[main] 丢弃过期内容({publish_date}): {text[:30]}", flush=True)
                    return {"text": "（近期暂无相关资讯）", "imageUrl": None, "sourceUrl": None, "sourceName": None}
            except Exception:
                pass

        if point_image and not str(point_image).startswith("http"):
            point_image = None
        if source_url and not str(source_url).startswith("http"):
            source_url = None
        return {
            "text": text or "（获取失败）", "imageUrl": point_image,
            "sourceUrl": source_url,
            "sourceName": source_name,
        }
    except Exception as e:
        print(f"[main] 要点抓取失败({dimension}): {e}", flush=True)
        return {"text": "（获取失败）", "sourceUrl": None, "sourceName": None}


_AD_BLOCKLIST_DOMAINS = {
    "doubleclick.net", "googlesyndication.com", "adnxs.com",
    "taboola.com", "outbrain.com", "zedo.com", "criteo.com",
    "mgid.com", "revcontent.com", "sharethrough.com",
}
_AD_PATH_KEYWORDS = ["/ad/", "/ads/", "/adv/", "/advertisement/", "/sponsored/", "/banner/", "/pagead/", "adunit", "adsystem"]
_LOW_QUALITY_KEYWORDS = ["logo", "avatar", "icon", "favicon", "placeholder", "default", "blank", "noimage", "watermark"]
_GOOD_PATH_PATTERNS = ["/photo/", "/image/", "/img/", "/news/", "/upload/", "/pic/", "/media/", "/content/", "/article/", "/cover/", "/thumb/"]

def _is_ad_image(url: str) -> bool:
    if not url or not url.startswith("http"):
        return True
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc.lower().lstrip("www.")
        path = parsed.path.lower()
        for blocked in _AD_BLOCKLIST_DOMAINS:
            if domain == blocked or domain.endswith("." + blocked):
                return True
        for kw in _AD_PATH_KEYWORDS:
            if kw in path:
                return True
        for kw in _LOW_QUALITY_KEYWORDS:
            if kw in path:
                return True
        if any(path.endswith(ext) for ext in [".svg", ".gif", ".ico", ".bmp"]):
            return True
    except Exception:
        pass
    return False

def _score_image(url: str) -> int:
    score = 0
    lower = url.lower()
    for pattern in _GOOD_PATH_PATTERNS:
        if pattern in lower:
            score += 2
    if any(lower.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
        score += 1
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower()
        if any(cdn in domain for cdn in ["cdn", "img", "image", "photo", "static", "media"]):
            score += 1
    except Exception:
        pass
    return score

def filter_images(urls: List[str], max_count: int = 3) -> List[str]:
    filtered = [u for u in urls if u and not _is_ad_image(u)]
    seen = set()
    deduped = [u for u in filtered if not (u in seen or seen.add(u))]
    return sorted(deduped, key=lambda u: -_score_image(u))[:max_count]


def fetch_intent_based(api_key: str, trigger_intent: str, topic: Optional[str], time_sensitive: bool = False, freshness_hours: int = 48) -> dict:
    """三个维度独立搜索，每个要点有独立来源"""
    today = datetime.now().strftime("%Y-%m-%d")
    base_query = f"{trigger_intent}（{today}）"
    freshness = f"（请优先返回{freshness_hours}小时内的最新资讯，今天是{today}）" if time_sensitive else ""

    dimensions = [
        (f"{base_query} 最新消息 新闻报道{freshness}", "最新事实"),
        (f"{base_query} 深度分析 背景", "深度分析"),
        (f"{base_query} 相关延伸 你可能不知道", "延伸信息"),
    ]

    points = []
    for query, dimension in dimensions:
        point = _fetch_single_point(api_key, query, dimension, today=today)
        points.append(point)
        time.sleep(0.3)

    # 从 points 收集图片 URL
    point_images = [p["imageUrl"] for p in points if p.get("imageUrl") and str(p.get("imageUrl","")).startswith("http")]

    # 取第一个有效的 sourceUrl 作为整体 sourceUrl
    source_url = next(
        (p["sourceUrl"] for p in points if p.get("sourceUrl")),
        None
    )

    return {"points": points, "sourceUrl": source_url, "pointImages": point_images}
def _fetch_og_image(article_url: str) -> Optional[str]:
    """直接从文章页面抓取 og:image"""
    import urllib.request as _req
    import re as _re
    try:
        request = _req.Request(
            article_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MyAgent/1.0)"}
        )
        with _req.urlopen(request, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        match = _re.search(r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\'](https?://[^"\']+)["\']', html)
        if not match:
            match = _re.search(r'<meta[^>]*content=["\'](https?://[^"\']+\.(?:jpg|jpeg|png|webp))["\'][^>]*property=["\']og:image["\']', html)
        if match:
            return match.group(1)
        return None
    except Exception:
        return None


def fetch_image_url(api_key: str, trigger_intent: str, title: str) -> Optional[str]:
    """从新闻文章里抓 og:image 作为配图"""
    import urllib.request as _req
    import html as _html
    import re as _re

    # 第一步：让 perplexity 找一篇相关的新闻文章 URL
    system = "你是一个新闻搜索助手。只输出JSON，不要其他内容。"
    user = f"""
搜索与以下内容最相关的一篇新闻文章：
{title} {trigger_intent}

输出格式（严格JSON）：
{{"articleUrl": "https://..."}}

要求：只输出一个可访问的新闻文章URL，不要其他内容。
""".strip()

    payload = {
        "model": OPENROUTER_MODEL,
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
        data = _http_post_json(OPENROUTER_ENDPOINT, headers, payload)
        content_str = data["choices"][0]["message"]["content"].strip()
        content_str = content_str.replace("```json", "").replace("```", "").strip()
        result = json.loads(content_str)
        article_url = result.get("articleUrl", "")
        if not article_url or not article_url.startswith("http"):
            return None

        # 第二步：抓取文章页面，提取 og:image
        request = _req.Request(
            article_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MyAgent/1.0)"}
        )
        with _req.urlopen(request, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # 找 og:image
        match = _re.search(r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\'](https?://[^"\']+)["\']', html)
        if not match:
            match = _re.search(r'<meta[^>]*content=["\'](https?://[^"\']+\.(?:jpg|jpeg|png|webp))["\'][^>]*property=["\']og:image["\']', html)
        if match:
            return match.group(1)
        return None

    except Exception as e:
        import traceback
        print(f"[main] 配图获取失败: {e}", flush=True)
        traceback.print_exc()
        return None

def generate_title(api_key: str, points: list) -> str:
    import re
    """根据资讯内容生成推送标题"""
    points_text = "\n".join([
        f"{i+1}. " + p.get("text", "") for i, p in enumerate(points) if p.get("text")
    ])
    system = "你是一个资讯标题编辑。只输出标题，不要其他内容。"
    user = f"""根据以下所有要点，生成一个准确的中文推送标题（最多20字）。

要求：
- 必须覆盖所有要点的核心信息，不能只反映第一条
- 如果要点涉及多支球队/多个事件，标题要体现整体
- 直接给出标题，不加引号

要点：
{points_text}"""

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        data = _http_post_json(OPENROUTER_ENDPOINT, headers, payload)
        title = data["choices"][0]["message"]["content"].strip().strip('"')
        title = re.sub(r"\[\d+\]", "", title).strip().strip('"').strip()
        if not title or title.lower() in ["none", "null", "n/a"]:
            return "最新资讯"
        return title[:20]
    except Exception as e:
        print(f"[main] generate_title failed: {e}", flush=True)
        return "最新资讯"

def fetch_topic_based(api_key: str, field_name: str) -> List[str]:
    """抓取某个话题的通用资讯（没有具体意图时使用）"""
    today = datetime.now().strftime("%Y-%m-%d")
    system = (
        "你是一名联网资讯助理。"
        "你的任务是基于可靠来源，挑选并总结用户指定领域在今天最重要的3条资讯。"
    )
    user = f"""
请联网检索并汇总：{field_name} 领域在 {today}（今天）最重要的3条资讯。

要求：
1) 每条用中文写成1句话要点，信息密度高。
2) 只输出3条要点，按重要性排序。
3) 严格使用下面格式输出：
- 要点1
- 要点2
- 要点3
""".strip()

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    data = _http_post_json(OPENROUTER_ENDPOINT, headers, payload)
    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    return _parse_bullets(content)


def _load_feed(feed_path: str) -> Dict:
    try:
        with open(feed_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_feed(feed_path: str, feed: Dict) -> None:
    tmp_path = feed_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, feed_path)


def main(
    topic: Optional[str] = None,
    trigger_intent: Optional[str] = None,
    feed_path: Optional[str] = None,
    time_sensitive: bool = False,
    freshness_hours: int = 48,
) -> List[Dict]:
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    cfg = _load_config(config_path)
    api_key = cfg["openrouter_api_key"]

    if feed_path is None:
        feed_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed.json")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    base_ts_ms = int(time.time() * 1000)

    # 有具体意图 → 只抓一条精准资讯
    if trigger_intent:
        emoji = FIELD_EMOJI.get(topic, "📌") if topic else "📌"
        print(f"[main] 基于意图抓取: {trigger_intent[:50]}...", flush=True)
        source_url = None
        points = []
        try:
            result = fetch_intent_based(api_key, trigger_intent, topic, time_sensitive=time_sensitive, freshness_hours=freshness_hours)
            points = result.get("points", [])
            source_url = result.get("sourceUrl")
            print(f"[main] 抓取完成 points={len(points)} sourceUrl={source_url}", flush=True)
        except Exception as e:
            print(f"[main] 意图抓取失败: {e}", flush=True)
            points = [{"text": "（获取失败）", "sourceUrl": None, "sourceName": None}] * 3
        bullets = [p.get("text", "") for p in points]

        # 动态生成标题（基于资讯内容，不是用户意图）
        try:
            title = generate_title(api_key, points)
        except Exception:
            title = topic or "为你推送"

        # 获取多张配图（从每个 point 的 sourceUrl 抓 og:image）
        raw_image_urls = list(result.get("pointImages", []))
        for p in points:
            if len(raw_image_urls) >= 6:
                break
            p_url = p.get("sourceUrl") if isinstance(p, dict) else None
            if not p_url:
                continue
            try:
                img = _fetch_og_image(p_url)
                if img:
                    raw_image_urls.append(img)
            except Exception:
                pass

        # 兜底：用原来的方式抓一张
        if not raw_image_urls:
            try:
                img = fetch_image_url(api_key, trigger_intent, title)
                if img:
                    raw_image_urls.append(img)
            except Exception:
                pass

        # 过滤广告图 + 质量排序
        image_urls = filter_images(raw_image_urls, max_count=3)
        image_url = image_urls[0] if image_urls else None
        print(f"[main] 配图(过滤后): {image_urls}", flush=True)

        new_item = {
            "id": f"{base_ts_ms}-0",
            "category": title,
            "emoji": emoji,
            "points": points,
            "created_at": now_str,
            "triggerIntent": trigger_intent,
            "imageUrl": image_url,
            "imageUrls": image_urls,
            "sourceUrl": source_url,
        }

        feed = _load_feed(feed_path)
        items = feed.get("items", [])
        items = [new_item] + items
        feed["updated_at"] = now_str
        feed["items"] = items[:50]
        _save_feed(feed_path, feed)

        print(f"[main] 写入 feed.json 完成", flush=True)
        return [new_item]

    # 没有意图 → 全量抓取5个领域
    if topic:
        fields_to_fetch = [(topic, FIELD_EMOJI.get(topic, "📌"))]
    else:
        fields_to_fetch = FIELDS

    sections = []
    for field_name, emoji in fields_to_fetch:
        print(f"[main] 抓取话题: {field_name}", flush=True)
        try:
            bullets = fetch_topic_based(api_key, field_name)
        except Exception as e:
            print(f"[main] 话题抓取失败 {field_name}: {e}", flush=True)
            bullets = ["（获取失败）"] * 3
        sections.append((field_name, emoji, bullets))
        time.sleep(0.5)

    new_items = []
    for idx, (category, emoji, bullets) in enumerate(sections):
        new_items.append({
            "id": f"{base_ts_ms}-{idx}",
            "category": category,
            "emoji": emoji,
            "points": bullets,
            "created_at": now_str,
        })

    feed = _load_feed(feed_path)
    items = feed.get("items", [])
    items = new_items + items
    feed["updated_at"] = now_str
    feed["items"] = items[:50]
    _save_feed(feed_path, feed)

    # 打印报告
    parts = ["🌅 今日资讯简报", ""]
    for name, emoji, bullets in sections:
        parts.append(f"{emoji} {name}")
        for b in bullets:
            parts.append(f"- {b}")
        parts.append("")
    parts.append(f"⏰ 更新时间：{now_str}")
    print("\n".join(parts))

    return new_items


if __name__ == "__main__":
    main()
