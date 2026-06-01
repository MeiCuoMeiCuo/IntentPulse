import json
import sys
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ENDPOINT = "https://api.balldontlie.io/v1/games"


def _get_today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _http_get_json(url: str) -> dict:
    req = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _map_status(status: str) -> str:
    s = (status or "").strip().lower()
    if "final" in s:
        return "已结束"
    if "in progress" in s or "halftime" in s:
        return "进行中"
    return "未开始"


def fetch_today_games(today: str) -> list:
    qs = urlencode({"dates[]": today, "per_page": 100})
    url = f"{ENDPOINT}?{qs}"
    data = _http_get_json(url)
    games = data.get("data", [])
    return games if isinstance(games, list) else []


def format_game_line(game: dict) -> str:
    home = (game.get("home_team") or {}).get("full_name") or "主队"
    away = (game.get("visitor_team") or {}).get("full_name") or "客队"
    home_score = game.get("home_team_score")
    away_score = game.get("visitor_team_score")

    try:
        home_score = int(home_score)
    except Exception:
        home_score = 0

    try:
        away_score = int(away_score)
    except Exception:
        away_score = 0

    status_cn = _map_status(str(game.get("status", "")))
    return f"主队 {home} {home_score} - {away_score} 客队 {away}（状态：{status_cn}）"


def main() -> int:
    today = _get_today_str()
    print("🏀 今日NBA战报")
    try:
        games = fetch_today_games(today)
    except HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        print(f"[请求失败] HTTP {getattr(e, 'code', '')} {msg}", file=sys.stderr)
        return 2
    except URLError as e:
        print(f"[网络错误] {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[未知错误] {e}", file=sys.stderr)
        return 2

    if not games:
        print(f"今天（{today}）暂无比赛数据。")
        return 0

    for g in games:
        print(format_game_line(g))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

