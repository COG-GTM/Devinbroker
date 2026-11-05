"""
News feed configuration — manages the user-customisable RSS feed list.
Feeds are stored in backend/config/news_feeds.json and persist across restarts.
"""
import json
import logging
from pathlib import Path

from services.url_safety import UnsafeURLError, validate_public_url

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "news_feeds.json"
MAX_FEEDS = 25

DEFAULT_FEEDS = [
    {"name": "NPR", "url": "https://feeds.npr.org/1004/rss.xml", "weight": 4},
    {"name": "BBC", "url": "http://feeds.bbci.co.uk/news/world/rss.xml", "weight": 3},
    {"name": "AlJazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "weight": 2},
    {"name": "NYT", "url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "weight": 1},
    {"name": "GDACS", "url": "https://www.gdacs.org/xml/rss.xml", "weight": 5},
    {"name": "NHK", "url": "https://www3.nhk.or.jp/nhkworld/rss/world.xml", "weight": 3},
    {"name": "CNA", "url": "https://www.channelnewsasia.com/rssfeed/8395986", "weight": 3},
    {"name": "Mercopress", "url": "https://en.mercopress.com/rss/", "weight": 3},
    {"name": "FocusTaiwan", "url": "https://focustaiwan.tw/rss", "weight": 5},
    {"name": "Kyodo", "url": "https://english.kyodonews.net/rss/news.xml", "weight": 4},
    {"name": "SCMP", "url": "https://www.scmp.com/rss/91/feed", "weight": 4},
    {"name": "The Diplomat", "url": "https://thediplomat.com/feed/", "weight": 4},
    {"name": "Stars and Stripes", "url": "https://www.stripes.com/feeds/pacific.rss", "weight": 4},
    {"name": "Yonhap", "url": "https://en.yna.co.kr/RSS/news.xml", "weight": 4},
    {"name": "Nikkei Asia", "url": "https://asia.nikkei.com/rss", "weight": 3},
    {"name": "Taipei Times", "url": "https://www.taipeitimes.com/xml/pda.rss", "weight": 4},
    {"name": "Asia Times", "url": "https://asiatimes.com/feed/", "weight": 3},
    {"name": "Defense News", "url": "https://www.defensenews.com/arc/outboundfeeds/rss/", "weight": 3},
    {"name": "Japan Times", "url": "https://www.japantimes.co.jp/feed/", "weight": 3},
]


def get_feeds() -> list[dict]:
    """Load feeds from config file, falling back to defaults."""
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            feeds = data.get("feeds", []) if isinstance(data, dict) else data
            if isinstance(feeds, list) and len(feeds) > 0:
                return feeds
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Failed to read news feed config: {e}")
    return list(DEFAULT_FEEDS)


def validate_feeds(feeds) -> str | None:
    """Validate and normalise *feeds* in place. Returns an error message, or None if valid.

    Feed URLs must be http(s) and resolve only to public addresses, so the
    server cannot be pointed at internal services, cloud metadata or local files.
    """
    if not isinstance(feeds, list):
        return "Feeds must be a list"
    if len(feeds) > MAX_FEEDS:
        return f"At most {MAX_FEEDS} feeds are allowed"
    for f in feeds:
        if not isinstance(f, dict):
            return "Each feed must be an object"
        name = f.get("name", "")
        url = f.get("url", "")
        weight = f.get("weight", 3)
        if not isinstance(name, str) or not isinstance(url, str):
            return "Feed name and url must be strings"
        name, url = name.strip(), url.strip()
        if not name or not url:
            return "Each feed needs a name and url"
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 1 or weight > 5:
            return f"Feed {name!r}: weight must be 1-5"
        try:
            validate_public_url(url)
        except UnsafeURLError as e:
            return f"Feed {name!r}: {e}"
        # Normalise
        f["name"] = name
        f["url"] = url
        f["weight"] = int(weight)
    return None


def _write_feeds(feeds: list[dict]) -> bool:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps({"feeds": feeds}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except OSError as e:
        logger.error(f"Failed to write news feed config: {e}")
        return False


def save_feeds(feeds: list[dict]) -> bool:
    """Validate and save feeds to config file. Returns True on success."""
    error = validate_feeds(feeds)
    if error:
        logger.warning(f"Rejected news feed config: {error}")
        return False
    return _write_feeds(feeds)


def reset_feeds() -> bool:
    """Reset feeds to defaults (trusted, so no DNS lookups are needed)."""
    return _write_feeds([dict(f) for f in DEFAULT_FEEDS])
