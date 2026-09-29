import os
import time
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query

from middleware import auth_middleware

load_dotenv()
router = APIRouter()

JAMENDO_URL = "https://api.jamendo.com/v3.0"
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID")

# Jamendo has no colour info, so every track gets this until the client
# extracts a colour from the artwork. 6 chars, no '#', same as uploaded songs.
DEFAULT_HEX = "4A3F8F"

# Only these sort orders are accepted from the app.
ALLOWED_ORDERS = {
    "popularity_week",
    "popularity_month",
    "popularity_total",
    "releasedate_desc",
}

# Tiny in-memory cache so the home screen doesn't hit Jamendo on every open.
CACHE_TTL_SECONDS = 300
_cache: dict = {}  # cache_key -> (timestamp, songs)


def to_song(track: dict) -> dict:
    """Reshape a Jamendo track into the same JSON shape as an uploaded song
    (id, song_name, artist, thumbnail_url, song_url, hex_code), so the
    Flutter app can parse it with the existing SongModel."""
    return {
        "id": f"jamendo_{track['id']}",
        "song_name": track.get("name") or "",
        "artist": track.get("artist_name") or "",
        "thumbnail_url": track.get("image") or track.get("album_image") or "",
        "song_url": track.get("audio") or "",
        "hex_code": DEFAULT_HEX,
    }


async def fetch_tracks(params: dict) -> list:
    if not JAMENDO_CLIENT_ID:
        raise HTTPException(500, "JAMENDO_CLIENT_ID is not configured on the server")

    query = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        **params,
    }

    cache_key = tuple(sorted((k, str(v)) for k, v in params.items()))
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(f"{JAMENDO_URL}/tracks/", params=query)
        body = res.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Could not reach Jamendo")

    # Jamendo reports its own errors inside a 200 response.
    headers = body.get("headers", {})
    if res.status_code != 200 or headers.get("status") != "success":
        raise HTTPException(502, headers.get("error_message") or "Jamendo request failed")

    results = body.get("results", [])
    songs = [to_song(t) for t in results if t.get("audio")]
    # Visible in Render's Logs tab: how many tracks Jamendo sent vs how many
    # had a stream URL.
    print(f"[jamendo] params={params} raw={len(results)} usable={len(songs)}")
    if not songs:
        print(f"[jamendo] headers={headers}")
        if results:
            print(f"[jamendo] first result keys={list(results[0].keys())}")

    # Don't cache empty answers, so a fixed problem shows up immediately.
    if songs:
        _cache[cache_key] = (time.time(), songs)
    return songs


@router.get("/tracks")
async def list_tracks(
    order: str = "popularity_week",
    tag: Optional[str] = None,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    auth_details=Depends(auth_middleware.AuthMiddleware),
):
    if order not in ALLOWED_ORDERS:
        raise HTTPException(400, f"order must be one of {sorted(ALLOWED_ORDERS)}")
    params = {"order": order, "limit": limit}
    if offset:
        params["offset"] = offset
    if tag:
        params["tags"] = tag
    return await fetch_tracks(params)


@router.get("/search")
async def search_tracks(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    auth_details=Depends(auth_middleware.AuthMiddleware),
):
    params = {"search": q, "limit": limit}
    if offset:
        params["offset"] = offset
    return await fetch_tracks(params)