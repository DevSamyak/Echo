import asyncio
import html
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query

from database import SessionLocal
from middleware import auth_middleware
from models.feed_cache import FeedCache

load_dotenv()
router = APIRouter()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Base URL of YOUR deployed jiosaavn-api instance, no trailing slash.
# e.g. http://localhost:8000 while testing, https://echo-saavn.onrender.com later.
SAAVN_API_URL = (os.getenv("SAAVN_API_URL") or "").rstrip("/")

# Same placeholder colour as Jamendo tracks (6 chars, no '#').
DEFAULT_HEX = "4A3F8F"

# jiosaavn-api has no "trending" endpoint, only search, and each search returns
# only a handful of songs. So each Discover row merges a few curated queries.
# The app sends a section key; only these are accepted.
# Tweak the query strings freely, they are just search terms.
# (trending/new use 3 queries to keep the one-time first load short.)
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

# How many songs to request per search from the Saavn service. Duplicates
# (same song on several albums) are removed afterwards, so ask for extra.
UPSTREAM_LIMIT = 25     # for the Search tab
SECTION_UPSTREAM = 8    # per query, for Discover rows

# In-memory per-query cache (lost on restart). Saavn CDN URLs have no
# documented expiry, but keep it short anyway.
CACHE_TTL_SECONDS = 600
_cache: dict = {}  # (query, upstream) -> (timestamp, songs)

# Persistent per-section cache lives in Postgres (table: feed_cache).
FEED_FRESH = timedelta(minutes=30)   # older than this -> refresh in background
MIN_SONGS_TO_STORE = 8               # never overwrite the cache with a tiny result
_refreshing: set = set()             # sections currently being refreshed
_tasks: set = set()                  # keeps background tasks from being garbage collected

# Limit concurrent upstream calls (the free Render instance is tiny).
# Created lazily so it binds to the running event loop (matters on Python 3.9).
_upstream_sem: Optional[asyncio.Semaphore] = None


def _sem() -> asyncio.Semaphore:
    global _upstream_sem
    if _upstream_sem is None:
        _upstream_sem = asyncio.Semaphore(2)
    return _upstream_sem


# ---------------------------------------------------------------------------
# Postgres feed cache helpers
# ---------------------------------------------------------------------------

def _spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _db_get(key: str):
    db = SessionLocal()
    try:
        row = db.query(FeedCache).filter_by(key=key).first()
        return (json.loads(row.value), row.updated_at) if row else (None, None)
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


async def _refresh_section(section: str):
    """Fetch a section from Saavn and store it in Postgres. Safe to call many
    times at once: only one refresh per section runs at a time."""
    if section in _refreshing:
        return
    _refreshing.add(section)
    try:
        songs = await fetch_section(SECTIONS[section], 50)
        if len(songs) >= MIN_SONGS_TO_STORE:
            await asyncio.to_thread(_db_set, f"feed:{section}", songs)
        else:
            print(f"[saavn] refresh {section}: only {len(songs)} songs, not stored")
    except Exception as e:
        print(f"[saavn] refresh {section} failed: {e!r}")
    finally:
        _refreshing.discard(section)


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
    """Reshape a jiosaavn-api song into the same JSON shape as an uploaded
    song (id, song_name, artist, thumbnail_url, song_url, hex_code), so the
    Flutter app can parse it with the existing SongModel.

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
    """Saavn lists the same track once per album (original film album,
    compilations, "Best of" playlists...), each with its own id. Treat songs
    with the same title and the same set of artists as one."""
    title = re.sub(r"\s*\((?:from|film|movie)\b[^)]*\)", "", song["song_name"], flags=re.I)
    artists = frozenset(a.strip().lower() for a in song["artist"].split(",") if a.strip())
    return (title.strip().lower(), artists)


def _clean_query(q: str) -> str:
    """jiosaavn-api pastes the query into a URL without encoding it, so
    characters like & # ? + % would break the upstream request."""
    return re.sub(r"\s+", " ", re.sub(r"[&#?+%/\\]", " ", q)).strip()


# ---------------------------------------------------------------------------
# Upstream fetching
# ---------------------------------------------------------------------------

async def fetch_songs(query: str, limit: int, upstream: int = UPSTREAM_LIMIT) -> list:
    query = _clean_query(query)
    if not query:
        return []
    if not SAAVN_API_URL:
        raise HTTPException(500, "SAAVN_API_URL is not configured on the server")

    cached = _cache.get((query, upstream))
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1][:limit]

    res = None
    try:
        async with _sem():
            async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15)) as client:
                res = await client.get(
                    f"{SAAVN_API_URL}/song/",
                    params={
                        "query": query,
                        "lyrics": "false",
                        "songdata": "true",
                        "limit": upstream,
                    },
                )
        body = res.json()
    except (httpx.HTTPError, ValueError) as e:
        status = res.status_code if res is not None else "n/a"
        print(f"[saavn] upstream failed: {type(e).__name__}: {e!r} query={query!r} status={status}")
        raise HTTPException(502, "Could not reach the Saavn service")
    if res.status_code != 200 or not isinstance(body, list):
        print(f"[saavn] unexpected response: status={res.status_code} query={query!r}")
        raise HTTPException(502, "Saavn service returned an unexpected response")

    songs, seen = [], set()
    for raw in body:
        song = to_song(raw)
        if song and _dedupe_key(song) not in seen:
            seen.add(_dedupe_key(song))
            songs.append(song)

    # Visible in Render's Logs tab.
    print(f"[saavn] query={query!r} raw={len(body)} usable={len(songs)}")

    # Don't cache empty answers, so a fixed problem shows up immediately.
    if songs:
        _cache[(query, upstream)] = (time.time(), songs)
    return songs[:limit]


async def fetch_section(queries: list, limit: int) -> list:
    """Run every query of a section (at most 2 hit Saavn at once, see _sem),
    merge, drop duplicates."""
    results = await asyncio.gather(
        *(fetch_songs(q, 50, SECTION_UPSTREAM) for q in queries),
        return_exceptions=True,
    )
    merged, seen = [], set()
    # Round-robin so one query doesn't fill the whole row.
    lists = [r for r in results if isinstance(r, list)]
    for i in range(max((len(r) for r in lists), default=0)):
        for r in lists:
            if i < len(r) and _dedupe_key(r[i]) not in seen:
                seen.add(_dedupe_key(r[i]))
                merged.append(r[i])
    if not merged:
        # Every query failed: surface the first real error instead of [].
        for r in results:
            if isinstance(r, HTTPException):
                raise r
    return merged[:limit]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/feed")
async def feed(
    section: str = "trending",
    limit: int = Query(20, ge=1, le=50),
    auth_details=Depends(auth_middleware.AuthMiddleware),
):
    if section not in SECTIONS:
        raise HTTPException(400, f"section must be one of {sorted(SECTIONS)}")

    key = f"feed:{section}"
    cached, updated = await asyncio.to_thread(_db_get, key)

    if cached:
        # Answer instantly; refresh in the background if the copy is old.
        if datetime.now(timezone.utc) - updated > FEED_FRESH:
            _spawn(_refresh_section(section))
        return cached[:limit]

    # Very first request for this section: nothing stored yet, so wait for the
    # fetch (ours, or one another request already started).
    if section in _refreshing:
        for _ in range(90):
            await asyncio.sleep(1)
            if section not in _refreshing:
                break
    else:
        await _refresh_section(section)

    cached, _ = await asyncio.to_thread(_db_get, key)
    if not cached:
        raise HTTPException(502, "Could not reach the Saavn service")
    return cached[:limit]


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(20, ge=1, le=50),
    auth_details=Depends(auth_middleware.AuthMiddleware),
):
    return await fetch_songs(q.strip(), limit)