import asyncio
import html
import json
import os
import random
import re
import time
import weakref
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query

from database import SessionLocal
from middleware import auth_middleware
from models.feed_cache import FeedCache
from saavn_core.saavn_service import SaavnService

load_dotenv()
router = APIRouter()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# By default Echo talks to JioSaavn itself (saavn_core/), so there is no second
# Render service to wake up, no extra network hop, and no 429/502 between them.
# To go back to a separate jiosaavn-api service set SAAVN_MODE=remote and
# SAAVN_API_URL=https://your-saavn-service.onrender.com in the environment.
SAAVN_API_URL = (os.getenv("SAAVN_API_URL") or "").rstrip("/")
USE_REMOTE = os.getenv("SAAVN_MODE", "embedded").strip().lower() == "remote" and bool(SAAVN_API_URL)

# Same placeholder colour as Jamendo tracks (6 chars, no '#').
DEFAULT_HEX = "4A3F8F"

# Each Discover row merges a few curated search queries. The app sends a
# section key; only these are accepted. Tweak the query strings freely.
SECTIONS = {
    "trending": ["Top Hindi songs", "Arijit Singh", "Pritam"],
    "new": ["New Hindi songs", "Latest Bollywood", "Vishal Mishra"],
    "punjabi": ["Punjabi hits", "Diljit Dosanjh", "AP Dhillon", "Sidhu Moose Wala"],
    "romantic": ["Hindi romantic songs", "Atif Aslam", "Jubin Nautiyal", "Darshan Raval"],
    "retro": ["Kishore Kumar", "Lata Mangeshkar", "Mohammed Rafi", "R D Burman"],
    "arijit": ["Arijit Singh romantic", "Arijit Singh sad", "Arijit Singh new", "Arijit Singh 2024"],
    "party": ["Bollywood party songs", "Badshah", "Yo Yo Honey Singh", "Neha Kakkar"],
    "tamil": ["Tamil hits", "Anirudh Ravichander", "A R Rahman Tamil", "Yuvan Shankar Raja"],
    "telugu": ["Telugu hits", "Devi Sri Prasad", "Thaman S", "Sid Sriram"],
}
WARM_SECTIONS = ("trending", "new")   # kept fresh by the /health keep-alive ping

UPSTREAM_LIMIT = 25      # songs per search for the Search tab
SECTION_UPSTREAM = 20    # songs per query for Discover rows
POOL_MAX = 90            # songs kept per section; the app pages through them
MIN_SONGS_TO_STORE = 8   # never overwrite a good pool with a tiny result

CACHE_TTL_SECONDS = 600              # in-memory per-query cache
FEED_FRESH = timedelta(minutes=30)   # older pool -> refresh in the background
REFRESH_RETRY_AFTER = 120            # after a failed refresh, wait before retrying
BREAKER_SECONDS = 30                 # after upstream failures, fail fast for a bit
SEARCH_TIMEOUT = 45                  # a search never keeps the app waiting longer

BUSY_MESSAGE = "Music service is busy right now. Try again in a moment."

_cache: dict = {}                    # (query, upstream, page) -> (timestamp, songs)
_refreshing: set = set()             # sections being refreshed right now
_refresh_blocked_until: dict = {}    # section -> unix time
_last_refresh_ok: dict = {}          # section -> unix time (this process only)
_tasks: set = set()                  # keeps background tasks from being garbage collected
_breaker_until = 0.0
_sems: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()  # event loop -> Semaphore


def _sem() -> asyncio.Semaphore:
    """At most 2 searches talk to JioSaavn at once. Created lazily for the
    running event loop (a Semaphore made at import time can be bound to the
    wrong loop on Python 3.9)."""
    loop = asyncio.get_running_loop()
    sem = _sems.get(loop)
    if sem is None:
        sem = _sems[loop] = asyncio.Semaphore(2)
    return sem


def _spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# ---------------------------------------------------------------------------
# Postgres pool cache (table: feed_cache, key "feed:<section>")
# ---------------------------------------------------------------------------

def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _db_get(key: str):
    db = SessionLocal()
    try:
        row = db.query(FeedCache).filter_by(key=key).first()
        if not row:
            return None, None
        return json.loads(row.value), _aware(row.updated_at)
    finally:
        db.close()


def _db_set(key: str, songs: list):
    db = SessionLocal()
    try:
        db.merge(
            FeedCache(
                key=key,
                value=json.dumps(songs),
                updated_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()


def _local_search(q: str, limit: int) -> list:
    """Fallback for /search when JioSaavn can't be reached: look through the
    songs already stored for the Discover rows."""
    words = _clean_query(q).lower().split()
    if not words:
        return []
    db = SessionLocal()
    try:
        rows = db.query(FeedCache).filter(FeedCache.key.like("feed:%")).all()
    finally:
        db.close()
    out, seen = [], set()
    for row in rows:
        try:
            songs = json.loads(row.value)
        except ValueError:
            continue
        for s in songs:
            hay = f"{s.get('song_name', '')} {s.get('artist', '')}".lower()
            if s.get("id") not in seen and all(w in hay for w in words):
                seen.add(s.get("id"))
                out.append(s)
                if len(out) >= limit:
                    return out
    return out


# ---------------------------------------------------------------------------
# Saavn -> Echo song shape
# ---------------------------------------------------------------------------

def _big_image(url: str) -> str:
    """Saavn sends 150x150 thumbnails by default; ask the CDN for 500x500."""
    if not url:
        return ""
    return (
        url.replace("150x150", "500x500").replace("50x50", "500x500")
    ).replace("http://", "https://")


def to_song(track: dict) -> Optional[dict]:
    """Reshape a Saavn song into the same JSON shape as an uploaded song
    (id, song_name, artist, thumbnail_url, song_url, hex_code) so the Flutter
    app can parse it with the existing SongModel.

    Returns None when the track has no playable URL."""
    stream = (track.get("media_url") or "").strip()
    if not stream or not track.get("id"):
        return None

    artist = track.get("primary_artists") or track.get("singers") or track.get("music") or ""
    return {
        "id": f"saavn_{track['id']}",
        "song_name": html.unescape(track.get("song") or ""),
        "artist": html.unescape(artist),
        "thumbnail_url": _big_image(track.get("image") or ""),
        "song_url": stream.replace("http://", "https://"),
        "hex_code": DEFAULT_HEX,
    }


def _dedupe_key(song: dict) -> tuple:
    """Saavn lists the same track once per album (film album, compilations,
    "Best of" playlists...), each with its own id. Treat songs with the same
    title and the same set of artists as one."""
    title = re.sub(r"\s*\((?:from|film|movie)\b[^)]*\)", "", song["song_name"], flags=re.I)
    artists = frozenset(a.strip().lower() for a in song["artist"].split(",") if a.strip())
    return (title.strip().lower(), artists)


def _clean_query(q: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[&#?+%/\\]", " ", q)).strip()


def _merge(*lists) -> list:
    """Concatenate song lists, dropping duplicates (earlier lists win)."""
    merged, seen = [], set()
    for lst in lists:
        for song in lst or []:
            k = _dedupe_key(song)
            if k not in seen:
                seen.add(k)
                merged.append(song)
    return merged


# ---------------------------------------------------------------------------
# Upstream fetching
# ---------------------------------------------------------------------------

async def _search_remote(query: str, upstream: int, page: int) -> list:
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15)) as client:
        res = await client.get(
            f"{SAAVN_API_URL}/song/",
            params={
                "query": query,
                "lyrics": "false",
                "songdata": "true",
                "limit": min(upstream, 40),
                "page": page,
                "slim": "true",
            },
        )
    if res.status_code != 200:
        raise ValueError(f"saavn service answered HTTP {res.status_code}")
    return res.json()


async def _search_upstream(query: str, upstream: int, page: int) -> list:
    """Raw Saavn songs for a query. Two attempts, then a short 'circuit
    breaker' so a struggling upstream isn't hammered by many queries at once."""
    global _breaker_until
    if time.time() < _breaker_until:
        raise HTTPException(503, BUSY_MESSAGE)

    for attempt in (1, 2):
        try:
            async with _sem():
                if USE_REMOTE:
                    body = await _search_remote(query, upstream, page)
                else:
                    body = await asyncio.to_thread(
                        lambda: SaavnService.search_songs(
                            query,
                            include_lyrics=False,
                            full_data=True,
                            limit=upstream,
                            page=page,
                        )
                    )
            if not isinstance(body, list):
                raise ValueError("unexpected response type")
            _breaker_until = 0.0
            return body
        except Exception as e:
            print(f"[saavn] attempt {attempt}/2 failed: {type(e).__name__}: {e!r} query={query!r}")
            if attempt == 1:
                await asyncio.sleep(3)
    _breaker_until = time.time() + BREAKER_SECONDS
    raise HTTPException(503, BUSY_MESSAGE)


async def fetch_songs(query: str, limit: int, upstream: int = UPSTREAM_LIMIT, page: int = 1) -> list:
    query = _clean_query(query)
    if not query:
        return []

    cached = _cache.get((query, upstream, page))
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1][:limit]

    body = await _search_upstream(query, upstream, page)

    songs = _merge([s for s in (to_song(raw) for raw in body) if s])
    print(f"[saavn] query={query!r} page={page} raw={len(body)} usable={len(songs)}")

    # Don't cache empty answers, so a fixed problem shows up immediately.
    if songs:
        _cache[(query, upstream, page)] = (time.time(), songs)
    return songs[:limit]


async def fetch_section(queries: list, limit: int, page: int = 1) -> list:
    """Run every query of a section (at most 2 at once, see _sem), merge them
    round-robin so no single query fills the whole row, drop duplicates."""
    results = await asyncio.gather(
        *(fetch_songs(q, 50, SECTION_UPSTREAM, page) for q in queries),
        return_exceptions=True,
    )
    lists = [r for r in results if isinstance(r, list)]
    interleaved = []
    for i in range(max((len(r) for r in lists), default=0)):
        for r in lists:
            if i < len(r):
                interleaved.append(r[i])
    merged = _merge(interleaved)
    if not merged:
        # Every query failed: surface the first real error instead of [].
        for r in results:
            if isinstance(r, HTTPException):
                raise r
    return merged[:limit]


# ---------------------------------------------------------------------------
# Section pools
# ---------------------------------------------------------------------------

async def _refresh_section(section: str):
    """Fetch fresh songs for a section and MERGE them into its stored pool, so
    the pool keeps growing/rotating instead of repeating the same songs. Only
    one refresh per section runs at a time; after a failure it waits a while."""
    if section in _refreshing:
        return
    if time.time() < _refresh_blocked_until.get(section, 0):
        return
    _refreshing.add(section)
    ok = False
    key = f"feed:{section}"
    try:
        old, _ = await asyncio.to_thread(_db_get, key)
        # Rotate through upstream result pages 1..3 so new songs keep arriving.
        round_no = 1 if not old else int(time.time() // 1800) % 3 + 1
        fresh = await fetch_section(SECTIONS[section], POOL_MAX, round_no)
        if len(fresh) >= MIN_SONGS_TO_STORE:
            pool = _merge(fresh, old)[:POOL_MAX]
            await asyncio.to_thread(_db_set, key, pool)
            _last_refresh_ok[section] = time.time()
            ok = True
        else:
            print(f"[saavn] refresh {section}: only {len(fresh)} songs, not stored")
    except Exception as e:
        print(f"[saavn] refresh {section} failed: {e!r}")
    finally:
        _refreshing.discard(section)
        if not ok:
            _refresh_blocked_until[section] = time.time() + REFRESH_RETRY_AFTER


def warm_stale():
    """Called by /health (the keep-alive ping): refresh the home rows in the
    background if this process hasn't refreshed them for a while. Memory-only
    check, so the ping itself never touches the database."""
    now = time.time()
    for section in WARM_SECTIONS:
        if now - _last_refresh_ok.get(section, 0) > FEED_FRESH.total_seconds():
            _spawn(_refresh_section(section))


def _view(pool: list, section: str, page: int, limit: int) -> list:
    """Page N of a section's pool. Page 1 is the pool as stored (newest first);
    each further page is the next slice, and once every slice has been shown
    the pool is reshuffled, so pulling to refresh keeps showing new songs."""
    n = len(pool)
    if n <= limit:
        items = list(pool)
        if page > 1:
            random.Random(f"{section}:{page}").shuffle(items)
        return items
    per_cycle = -(-n // limit)  # ceil
    cycle, idx = divmod(page - 1, per_cycle)
    items = list(pool)
    if cycle > 0:
        random.Random(f"{section}:{cycle}").shuffle(items)
    chunk = items[idx * limit: idx * limit + limit]
    if len(chunk) < limit:  # last slice: wrap around to fill the row
        chunk += items[: limit - len(chunk)]
    return chunk


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/feed")
async def feed(
    section: str = "trending",
    limit: int = Query(20, ge=1, le=50),
    page: int = Query(1, ge=1, le=50),
    auth_details=Depends(auth_middleware.AuthMiddleware),
):
    if section not in SECTIONS:
        raise HTTPException(400, f"section must be one of {sorted(SECTIONS)}")

    key = f"feed:{section}"
    pool, updated = await asyncio.to_thread(_db_get, key)

    if pool:
        # Answer instantly from the stored pool; refresh in the background when old.
        if updated is None or datetime.now(timezone.utc) - updated > FEED_FRESH:
            _spawn(_refresh_section(section))
        return _view(pool, section, page, limit)

    # Very first request for this section: nothing stored yet, so wait for the
    # fetch (ours, or one another request already started).
    if section in _refreshing:
        for _ in range(100):
            await asyncio.sleep(1)
            if section not in _refreshing:
                break
    else:
        await _refresh_section(section)

    pool, _ = await asyncio.to_thread(_db_get, key)
    if not pool:
        raise HTTPException(503, "Songs are still loading. Pull down to refresh in a moment.")
    return _view(pool, section, page, limit)


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(20, ge=1, le=50),
    page: int = Query(1, ge=1, le=10),
    auth_details=Depends(auth_middleware.AuthMiddleware),
):
    q = q.strip()
    try:
        return await asyncio.wait_for(fetch_songs(q, limit, page=page), timeout=SEARCH_TIMEOUT)
    except (HTTPException, asyncio.TimeoutError) as e:
        # JioSaavn is slow or unreachable: show matches from songs we already have.
        local = await asyncio.to_thread(_local_search, q, limit)
        if local:
            return local
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(503, "Search is taking too long. Try again in a moment.")
