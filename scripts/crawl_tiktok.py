"""Checkpoint public TikTok embed captions without retaining creator profiles."""
import argparse
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from happy_path.core.ingestion import scrub_pii


def now():
    return datetime.now(timezone.utc).isoformat()


def extract_caption(html, video_id):
    """Require the exact video identity within embedded structured data."""
    soup = BeautifulSoup(html, "html.parser")

    def visit(value):
        if isinstance(value, dict):
            identity = next((value[k] for k in ("id", "videoId", "itemId") if k in value), None)
            if str(identity) == video_id:
                caption = next((value[k] for k in ("desc", "text", "description") if isinstance(value.get(k), str) and value[k].strip()), None)
                if caption:
                    result = {"description": scrub_pii(caption)}
                    # Do not infer dates from IDs or Google relative dates.
                    timestamp = value.get("createTime")
                    try:
                        result["published_at"] = datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat() if timestamp else None
                    except (ValueError, TypeError, OverflowError, OSError):
                        result["published_at"] = None
                    return result
            for child in value.values():
                result = visit(child)
                if result:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = visit(child)
                if result:
                    return result
        return None

    for script in soup.select('script[type="application/json"], script#__FRONTITY_CONNECT_STATE__, script#SIGI_STATE'):
        try:
            result = visit(json.loads(script.get_text()))
        except (ValueError, TypeError):
            continue
        if result:
            return result
    return None


def export(db, output):
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM embed_crawls ORDER BY collected_at")]
    for row in rows:
        row["payload"] = json.loads(row.pop("payload_json"))
    (output / "embed_crawls.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    attempts = [dict(r) for r in db.execute("SELECT * FROM embed_attempts ORDER BY collected_at")]
    (output / "embed_attempts.json").write_text(json.dumps(attempts, indent=2), encoding="utf-8")
    summary = {"attempted_unique_videos": len(rows), "captions_collected": sum(r["success"] for r in rows),
               "failures": {r["reason"]: sum(x["reason"] == r["reason"] for x in rows) for r in rows if not r["success"]},
               "pending_videos": db.execute("SELECT COUNT(*) FROM videos WHERE video_id NOT IN (SELECT video_id FROM embed_crawls WHERE success=1)").fetchone()[0],
               "production_pois_promoted": 0}
    summary["failed_attempts"] = sum(not a["success"] for a in attempts)
    (output / "crawl_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


async def run(args):
    db = sqlite3.connect(args.output / "discovery.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS embed_crawls(video_id TEXT PRIMARY KEY,source_url TEXT,success INTEGER,http_status INTEGER,reason TEXT,payload_json TEXT,collected_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS embed_attempts(video_id TEXT,success INTEGER,http_status INTEGER,reason TEXT,collected_at TEXT)")
    # Interleave Google query scopes; geographic membership remains unverified.
    buckets = {}
    for corridor, vid, url in db.execute("SELECT DISTINCT s.corridor,v.video_id,v.source_url FROM videos v JOIN hits h USING(video_id) JOIN searches s USING(query) LEFT JOIN embed_crawls e ON e.video_id=v.video_id WHERE e.success IS NULL OR e.success=0 ORDER BY h.rank,v.video_id"):
        buckets.setdefault(corridor, []).append((vid, url))
    queue, seen = [], set()
    for i in range(max(map(len, buckets.values()), default=0)):
        for bucket in buckets.values():
            if i < len(bucket) and bucket[i][0] not in seen:
                seen.add(bucket[i][0])
                queue.append(bucket[i])
    failures = 0
    started = time.monotonic()
    run_status = {"started_at": now(), "scope": len(queue[:args.limit]), "attempted": 0,
                  "new_captions": 0, "state": "running", "pid": __import__("os").getpid()}
    status_path = args.output / "active_crawl.json"

    def save_status(**updates):
        run_status.update(updates)
        run_status["updated_at"] = now()
        status_path.write_text(json.dumps(run_status, indent=2), encoding="utf-8")

    save_status()
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        for vid, url in queue[:args.limit]:
            if time.monotonic() - started >= args.max_runtime_minutes * 60:
                save_status(state="runtime_limit_reached")
                break
            if db.execute("SELECT 1 FROM embed_crawls WHERE video_id=? AND success=1", (vid,)).fetchone():
                continue
            payload, status = {}, None
            try:
                response = await client.get(url)
                status = response.status_code
                body = response.text.lower()
                if status in (429, 503) or "overload-protect triggered" in body:
                    reason = "RATE_LIMITED" if status == 429 else "OVERLOAD_PROTECTION"
                elif status in (401, 403) or "captcha" in body and "verify" in body and len(body) < 10000:
                    reason = "ACCESS_CHALLENGE"
                elif status != 200:
                    reason = f"HTTP_{status}"
                else:
                    payload = extract_caption(response.text, vid) or {}
                    reason = "OK" if payload else "NO_IDENTITY_BOUND_CAPTION"
            except httpx.HTTPError as error:
                reason = type(error).__name__
            with db:
                collected_at = now()
                db.execute("INSERT INTO embed_attempts VALUES (?,?,?,?,?)", (vid, int(reason == "OK"), status, reason, collected_at))
                db.execute("""INSERT INTO embed_crawls VALUES (?,?,?,?,?,?,?)
                  ON CONFLICT(video_id) DO UPDATE SET success=excluded.success,http_status=excluded.http_status,
                    reason=excluded.reason,payload_json=excluded.payload_json,collected_at=excluded.collected_at
                  WHERE excluded.success=1 OR embed_crawls.success=0""",
                  (vid, url, int(reason == "OK"), status, reason, json.dumps(payload, ensure_ascii=False), collected_at))
            print(json.dumps({"video_id": vid, "status": status, "reason": reason}), flush=True)
            save_status(attempted=run_status["attempted"] + 1,
                        new_captions=run_status["new_captions"] + int(reason == "OK"),
                        last_video_id=vid, last_reason=reason)
            export(db, args.output)
            failures = failures + 1 if reason in {"RATE_LIMITED", "OVERLOAD_PROTECTION", "ACCESS_CHALLENGE"} else 0
            if failures >= 3 and not args.full:
                print("Stopping after three consecutive access restrictions; remaining queue preserved.", flush=True)
                save_status(state="access_restricted")
                break
            if reason == "ACCESS_CHALLENGE":
                save_status(state="access_challenge")
                break
            wait = min(900, 45 * 2 ** min(failures - 1, 5)) if failures else args.delay
            if reason in {"RATE_LIMITED", "OVERLOAD_PROTECTION"}:
                try:
                    wait = max(wait, min(3600, int(response.headers.get("retry-after", "0"))))
                except ValueError:
                    pass
            remaining = args.max_runtime_minutes * 60 - (time.monotonic() - started)
            save_status(state="backoff" if failures else "running", wait_seconds=min(wait, max(0, remaining)))
            await asyncio.sleep(min(wait, max(0, remaining)))
        else:
            save_status(state="queue_completed")
    if run_status["state"] in {"running", "backoff"}:
        save_status(state="finished")
    print(json.dumps(export(db, args.output)), flush=True)
    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/research_catalog/tiktok"))
    parser.add_argument("--limit", type=int, default=307)
    parser.add_argument("--delay", type=float, default=10)
    parser.add_argument("--full", action="store_true", help="Continue the entire queue with exponential backoff on 429/503; stop on access challenges.")
    parser.add_argument("--max-runtime-minutes", type=float, default=120)
    asyncio.run(run(parser.parse_args()))
