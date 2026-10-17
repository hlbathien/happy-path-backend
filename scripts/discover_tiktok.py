"""Google-indexed TikTok discovery; metadata only, no media or profile retention."""
import argparse
import asyncio
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from happy_path.core.ingestion import scrub_pii

TOPICS = {
    "hcm_dalat": ["Dambri Bảo Lộc", "Madagui review", "trạm dừng chân Tâm Châu", "Đèo Bảo Lộc đường đi", "Di Linh ăn uống", "Đức Trọng trạm sạc", "Pongour giá vé", "Datanla review", "Đà Lạt bãi đỗ xe", "Dầu Giây trạm dừng chân", "Định Quán quán ăn", "Bảo Lộc trạm sạc"],
    "hcm_nhatrang": ["Vĩnh Hy review", "Cà Ná trạm dừng chân", "Phan Thiết trạm sạc", "Bàu Trắng đường đi", "Mũi Né review", "Phan Rang quán ăn", "Cam Ranh trạm dừng chân", "Nha Trang bãi đỗ xe", "Tuy Phong quán ăn", "Hàm Tân trạm dừng chân", "Hang Rái giá vé", "Suối Tiên Mũi Né review"],
    "hcm_vungtau": ["Long Sơn Vũng Tàu quán ăn", "Suối Nghệ đồng cừu review", "Long Thành trạm dừng chân", "Phú Mỹ trạm sạc", "Bà Rịa quán ăn", "Vũng Tàu bãi đỗ xe", "Hồ Đá Xanh review", "Hải đăng Vũng Tàu đường đi", "Bến thuyền Marina Vũng Tàu review", "Long Sơn nhà bè review", "Vũng Tàu trạm sạc", "Bánh sữa Long Thành review"],
}
VIDEO = re.compile(r"/(?:video|embed)/(\d{15,22})(?:[/?#]|$)")


def video_id(url):
    parts = urlsplit(url)
    if parts.hostname not in {"www.tiktok.com", "tiktok.com", "m.tiktok.com"}:
        return None
    match = VIDEO.search(parts.path)
    return match.group(1) if match else None


def stamp():
    return datetime.now(timezone.utc).isoformat()


async def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.output / "discovery.sqlite3")
    db.executescript('''
    CREATE TABLE IF NOT EXISTS searches(query TEXT PRIMARY KEY,corridor TEXT,search_url TEXT,status INTEGER,reason TEXT,collected_at TEXT);
    CREATE TABLE IF NOT EXISTS videos(video_id TEXT PRIMARY KEY,source_url TEXT,title TEXT,evidence_level TEXT,collected_at TEXT);
    CREATE TABLE IF NOT EXISTS hits(query TEXT,video_id TEXT,rank INTEGER,title TEXT,PRIMARY KEY(query,video_id));
    CREATE TABLE IF NOT EXISTS metadata(video_id TEXT PRIMARY KEY,success INTEGER,reason TEXT,payload_json TEXT,collected_at TEXT);
    ''')
    live_urls = {}
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        async def resolve(href):
            url = urljoin("https://www.google.com", href)
            parts = urlsplit(url)
            if parts.hostname == "www.google.com" and parts.path in {"/goto", "/url"}:
                q = parse_qs(parts.query)
                direct = q.get("q", q.get("url", [""]))[0]
                if direct.startswith("https://"):
                    url = direct
                else:
                    try:
                        response = await client.get(url)
                        url = response.headers.get("location", "")
                    except httpx.HTTPError:
                        return None, None
            return video_id(url), url

        async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as crawler:
            # Interleave corridors so partial runs still cover all three.
            for index in range(max(map(len, TOPICS.values()))):
                for corridor, topics in TOPICS.items():
                    if index >= len(topics):
                        continue
                    query = "site:tiktok.com " + topics[index]
                    previous = db.execute("SELECT reason FROM searches WHERE query=?", (query,)).fetchone()
                    if previous and previous[0] == "OK":
                        continue
                    url = "https://www.google.com/search?" + urlencode({"q": query, "num": 10, "hl": "vi"})
                    try:
                        result = await crawler.arun(url, config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=30000))
                        soup = BeautifulSoup(result.html or "", "html.parser")
                        body = soup.get_text(" ", strip=True).lower()
                        blocked = any(marker in body for marker in ("unusual traffic", "our systems have detected", "không phải là rô-bốt"))
                        reason = "GOOGLE_CHALLENGE" if blocked else "OK" if result.status_code == 200 else "HTTP_ERROR"
                        anchors = [(h.find_parent("a"), h.get_text(" ", strip=True)) for h in soup.select("h3")] if reason == "OK" else []
                        anchors = [(a, title) for a, title in anchors if a and a.get("href")]
                        resolved = await asyncio.gather(*(resolve(a["href"]) for a, _ in anchors))
                        found = 0
                        with db:
                            for rank, ((_, title), (vid, original)) in enumerate(zip(anchors, resolved), 1):
                                if not vid:
                                    continue
                                found += 1
                                live_urls[vid] = original
                                title = scrub_pii(title)
                                db.execute("INSERT OR IGNORE INTO videos VALUES (?,?,?,?,?)", (vid, f"https://www.tiktok.com/embed/{vid}", title, "google_index_only", stamp()))
                                db.execute("INSERT OR REPLACE INTO hits VALUES (?,?,?,?)", (query, vid, rank, title))
                            db.execute("INSERT OR REPLACE INTO searches VALUES (?,?,?,?,?,?)", (query, corridor, url, result.status_code, reason, stamp()))
                        print(json.dumps({"corridor": corridor, "topic": topics[index], "videos": found, "reason": reason}), flush=True)
                        if blocked:
                            break
                    except Exception as error:
                        with db:
                            db.execute("INSERT OR REPLACE INTO searches VALUES (?,?,?,?,?,?)", (query, corridor, url, None, type(error).__name__, stamp()))
                    await asyncio.sleep(2)
                else:
                    continue
                break

    # Bounded, corridor-balanced direct-video pilot. Never retain yt-dlp's author fields.
    for corridor in TOPICS:
        rows = db.execute("SELECT DISTINCT v.video_id,v.source_url FROM videos v JOIN hits h USING(video_id) JOIN searches s USING(query) WHERE s.corridor=? ORDER BY h.rank,v.video_id LIMIT ?", (corridor, args.probes_per_corridor)).fetchall()
        for vid, embed in rows:
            if db.execute("SELECT 1 FROM metadata WHERE video_id=?", (vid,)).fetchone():
                continue
            payload = {}
            try:
                proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "yt_dlp", "--skip-download", "--dump-single-json", "--socket-timeout", "12", "--retries", "0", "--no-warnings", live_urls.get(vid, embed), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    raise
                if proc.returncode == 0:
                    raw = json.loads(stdout)
                    if str(raw.get("id")) != vid:
                        raise ValueError("Video identity mismatch")
                    for key in ("title", "description", "timestamp", "duration", "view_count", "like_count", "comment_count"):
                        value = raw.get(key)
                        payload[key] = scrub_pii(value) if isinstance(value, str) else value
                    reason = "OK"
                else:
                    err = stderr.decode("utf-8", errors="replace").lower()
                    reason = "LOGIN_REQUIRED" if "log in" in err or "login" in err else "PUBLIC_VIDEO_EXTRACTION_FAILED"
            except Exception as error:
                reason = type(error).__name__
            with db:
                db.execute("INSERT INTO metadata VALUES (?,?,?,?,?)", (vid, int(reason == "OK"), reason, json.dumps(payload, ensure_ascii=False), stamp()))
                if reason == "OK":
                    db.execute("UPDATE videos SET evidence_level='direct_video_metadata' WHERE video_id=?", (vid,))
            print(json.dumps({"video_id": vid, "metadata": reason}), flush=True)
    db.row_factory = sqlite3.Row
    for table in ("searches", "videos", "hits", "metadata"):
        (args.output / f"{table}.json").write_text(json.dumps([dict(r) for r in db.execute(f"SELECT * FROM {table}")], ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "searches": db.execute("SELECT COUNT(*) FROM searches").fetchone()[0],
        "successful_searches": db.execute("SELECT COUNT(*) FROM searches WHERE reason='OK'").fetchone()[0],
        "unique_video_links": db.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
        "videos_by_corridor": {c: db.execute("SELECT COUNT(DISTINCT video_id) FROM hits JOIN searches USING(query) WHERE corridor=?", (c,)).fetchone()[0] for c in TOPICS},
        "metadata_attempts": db.execute("SELECT COUNT(*) FROM metadata").fetchone()[0],
        "metadata_successes": db.execute("SELECT COUNT(*) FROM metadata WHERE success=1").fetchone()[0],
        "production_pois_promoted": 0,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)
    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/research_catalog/tiktok"))
    parser.add_argument("--probes-per-corridor", type=int, default=3)
    asyncio.run(run(parser.parse_args()))
