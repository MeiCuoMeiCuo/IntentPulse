import importlib
import json
import os
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import attention
import maslow
import patrol

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
FEED_PATH = os.path.join(ROOT_DIR, "feed.json")

def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

IGNORE_KEYWORDS = [
    "锁屏", "设备已锁定", "系统界面", "锁定状态",
    "指纹", "PIN码", "解锁", "查看锁屏", "锁屏界面",
    "通知概览", "查看通知", "网络信号", "充电", "天气",
    "查找中心", "离线设备", "蓝牙", "WiFi", "飞行模式",
    "系统通知", "待办事项", "系统时间", "系统设置",
    "开发者选项", "USB调试", "无障碍", "辅助功能",
    "亮度", "音量", "勿扰", "专注模式",
    "查看时间", "查看日期", "状态栏", "下拉通知"
]

def _should_ignore(intent: str) -> bool:
    text = intent.lower()
    return any(kw in text for kw in IGNORE_KEYWORDS)

def _trigger_feed_update(level_key: str, label: str, intent: str, time_sensitive: bool = False, freshness_hours: int = 48) -> int:
    try:
        main_mod = importlib.import_module("main")
        importlib.reload(main_mod)
        main_fn = getattr(main_mod, "main", None)
        if callable(main_fn):
            main_fn(topic=label, trigger_intent=intent, time_sensitive=time_sensitive, freshness_hours=freshness_hours)
    except Exception as e:
        print(f"[feed] update failed: {e}", flush=True)
        return 0

    if not os.path.exists(FEED_PATH):
        return 0
    try:
        data = _read_json(FEED_PATH)
        items = data.get("items", []) if isinstance(data, dict) else []
        return len(items)
    except Exception:
        return 0

_intent_history = []

class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/feed":
            if not os.path.exists(FEED_PATH):
                self._send_json(200, {"items": []})
                return
            self._send_json(200, _read_json(FEED_PATH))
        elif self.path == "/attention":
            summary = attention.get_graph_summary()
            # 附加巡逻状态
            try:
                patrol_state = patrol._load_patrol_state()
                patrol_topics = patrol_state.get("topics", {})
                candidates = patrol.get_patrol_candidates()
                summary["patrol"] = {
                    "active_topics": [c["label"] for c in candidates],
                    "topic_states": {
                        k: {
                            "last_patrol": v.get("last_patrol", 0),
                            "last_pushed": v.get("last_pushed", 0),
                        }
                        for k, v in patrol_topics.items()
                    }
                }
            except Exception:
                summary["patrol"] = {"active_topics": [], "topic_states": {}}
            self._send_json(200, summary)
        elif self.path == "/intents":
            self._send_json(200, {"intents": _intent_history[-50:]})
        elif self.path == "/maslow":
            try:
                profile = maslow.get_profile()
                self._send_json(200, profile)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        elif self.path == "/profile":
            try:
                self._send_json(200, {
                    "recommendation_profile": attention.get_recommendation_profile(),
                    "maslow": maslow.get_profile(),
                })
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        elif self.path == "/services":
            try:
                feed = _read_json(FEED_PATH) if os.path.exists(FEED_PATH) else {}
                self._send_json(200, {
                    "services": feed.get("services", []),
                    "services_updated_at": feed.get("services_updated_at", ""),
                    "services_meta": feed.get("services_meta", {}),
                })
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/services/refresh":
            payload = self._read_json_body()

            def _refresh() -> None:
                try:
                    services_mod = importlib.import_module("services")
                    importlib.reload(services_mod)
                    services_mod.fetch_services(force=bool(payload.get("force", False)))
                    print("[services] manual refresh done", flush=True)
                except Exception as e:
                    print(f"[services] manual refresh error: {e}", flush=True)

            import threading
            threading.Thread(target=_refresh, daemon=True).start()
            self._send_json(202, {"status": "refreshing"})
            return

        if self.path == "/feedback":
            payload = self._read_json_body()
            try:
                result = attention.apply_feedback(
                    feed_id=payload.get("feed_id"),
                    action=str(payload.get("action", "")).strip(),
                    reason=payload.get("reason"),
                    topic=payload.get("topic"),
                    trigger_node=payload.get("trigger_node"),
                )
                self._send_json(200, result)
            except Exception as e:
                print(f"[feedback] error: {e}", flush=True)
                self._send_json(500, {"error": str(e)})
            return

        if self.path != "/intent":
            self._send_json(404, {"error": "not found"})
            return

        payload = self._read_json_body()
        intent = str(payload.get("intent", "")).strip()
        app = str(payload.get("app", "")).strip()
        action_type = payload.get("action_type")
        duration_sec = payload.get("duration_sec")
        url = payload.get("url")
        title = payload.get("title")
        timestamp = payload.get("timestamp")

        if not intent:
            self._send_json(400, {"error": "missing intent"})
            return

        if _should_ignore(intent):
            self._send_json(200, {"ok": True, "ignored": True})
            return

        print(f"[intent] {intent[:80]} app={app}", flush=True)
        # 先存基本信息，分析完再补充 engagement
        intent_record = {
            "intent": intent,
            "app": app,
            "action_type": action_type,
            "duration_sec": duration_sec,
            "time": datetime.now().strftime("%H:%M:%S"),
            "engagement": None,
            "is_passing": None,
            "primary": None,
        }
        _intent_history.append(intent_record)

        try:
            trigger = attention.get_trigger(
                intent,
                app,
                action_type=action_type,
                duration_sec=duration_sec,
                url=url,
                title=title,
                timestamp=timestamp,
            )
        except Exception as e:
            print(f"[attention] error: {e}", flush=True)
            self._send_json(500, {"error": str(e)})
            return

        # 回填 engagement（从 attention 最近一次分析结果取）
        try:
            graph = attention._load_graph()
            buf = graph.get("_session_buffer", [])
            if buf:
                last = buf[-1]
                intent_record["engagement"] = round(last.get("engagement", 0.7), 2)
                intent_record["is_passing"] = last.get("engagement", 0.7) <= 0.3
                intent_record["primary"] = {
                    "domain": last.get("l1"),
                    "topic": last.get("l2"),
                    "entity": last.get("l3"),
                    "intent_goal": last.get("intent_goal"),
                }
        except Exception:
            pass

        pushed = False
        updated = 0

        if trigger:
            level_key, label, level1, time_sensitive, freshness_hours = trigger
            print(f"[trigger] level={level_key} label={label} time_sensitive={time_sensitive} → feed update", flush=True)
            updated = _trigger_feed_update(level_key, label, intent, time_sensitive=time_sensitive, freshness_hours=freshness_hours)
            pushed = True

        # 每次有效意图后异步更新马斯洛画像
        try:
            maslow.rebuild_profile()
        except Exception as e:
            print(f"[maslow] rebuild failed: {e}", flush=True)

        self._send_json(200, {
            "ok": True,
            "pushed": pushed,
            "updated": updated,
            "trigger": {
                "level": trigger[0] if trigger else None,
                "label": trigger[1] if trigger else None,
                "domain": trigger[2] if trigger else None,
            } if trigger else None
        })

    def log_message(self, format: str, *args: Any) -> None:
        return

def _write_feed_for_patrol(topic: str, trigger_intent: str, points: list, title: str) -> None:
    """巡逻模块的 feed 写入回调"""
    try:
        main_mod = importlib.import_module("main")
        importlib.reload(main_mod)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        base_ts_ms = int(time.time() * 1000)

        # 获取配图（从第一个 point 的 sourceUrl 抓 og:image）
        image_urls = []
        fetch_og = getattr(main_mod, "_fetch_og_image", None)
        if fetch_og:
            for p in points[:2]:
                src_url = p.get("sourceUrl") if isinstance(p, dict) else None
                if src_url:
                    try:
                        img = fetch_og(src_url)
                        if img and img not in image_urls:
                            image_urls.append(img)
                    except Exception:
                        pass

        filter_fn = getattr(main_mod, "filter_images", None)
        if filter_fn:
            image_urls = filter_fn(image_urls, max_count=2)

        new_item = {
            "id": f"{base_ts_ms}-patrol",
            "category": title,
            "emoji": "📡",
            "points": points,
            "created_at": now_str,
            "triggerIntent": trigger_intent,
            "imageUrl": image_urls[0] if image_urls else None,
            "imageUrls": image_urls,
            "sourceUrl": points[0].get("sourceUrl") if points else None,
        }

        feed = _read_json(FEED_PATH) if os.path.exists(FEED_PATH) else {}
        items = feed.get("items", []) if isinstance(feed, dict) else []
        items = [new_item] + items
        feed = {"updated_at": now_str, "items": items[:50]}
        _write_json(FEED_PATH, feed)
        print(f"[patrol→feed] 写入: {title}", flush=True)
    except Exception as e:
        print(f"[patrol→feed] 失败: {e}", flush=True)


def main_entry() -> None:
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    print("Server running on port 8080", flush=True)

    # 启动主动巡逻后台线程（每10分钟检查一次）
    patrol.start_patrol_loop(
        write_feed_fn=_write_feed_for_patrol,
        check_interval=10 * 60,
    )

    httpd.serve_forever()

if __name__ == "__main__":
    main_entry()
