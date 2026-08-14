from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from cachetools import TTLCache
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from players import PLAYERS

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
CACHE_ROOT = BASE_DIR / "cache"
MATCH_CACHE_DIR = CACHE_ROOT / "matches"
RANKING_CACHE_FILE = CACHE_ROOT / "ranking.json"
LP_HISTORY_FILE = CACHE_ROOT / "lp_history.json"
MATCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)

RIOT_API_KEY = os.getenv("RIOT_API_KEY", "").strip()
PLATFORM = os.getenv("RIOT_PLATFORM", "la1").strip().lower()
REGION = os.getenv("RIOT_REGION", "americas").strip().lower()
TZ_NAME = os.getenv("CHALLENGE_TIMEZONE", "America/Guayaquil").strip()
CHALLENGE_YEAR = int(os.getenv("CHALLENGE_YEAR", "2026"))
CHALLENGE_MONTH = int(os.getenv("CHALLENGE_MONTH", "8"))
CACHE_SECONDS = max(60, int(os.getenv("CACHE_SECONDS", "300")))
LP_HISTORY_MAX_POINTS = max(20, int(os.getenv("LP_HISTORY_MAX_POINTS", "120")))
LIVE_RECENT_MATCHES = max(3, min(10, int(os.getenv("LIVE_RECENT_MATCHES", "5"))))

QUEUE_ID_SOLOQ = 420
QUEUE_TYPE_SOLOQ = "RANKED_SOLO_5x5"
DDRAGON_CACHE_SECONDS = 6 * 60 * 60

app = FastAPI(title="Los Gotish - SoloQ Challenge")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# One process is enough for this small private challenge. The lock prevents several
# friends from triggering the same expensive Riot refresh at the same time.
_refresh_lock = asyncio.Lock()
_memory_cache: dict[str, Any] = {"data": None}
_ddragon_cache: dict[str, Any] = {
    "timestamp": 0.0,
    "version": None,
    "champions": None,
    "items": None,
    "live_assets": None,
    "live_assets_version": None,
}
_ranking_cache = TTLCache(maxsize=1, ttl=180)
_live_cache = TTLCache(maxsize=20, ttl=60)
_live_player_cache = TTLCache(maxsize=200, ttl=300)
_history_cache = TTLCache(maxsize=20, ttl=120)


@app.on_event("startup")
async def startup_event() -> None:
    """Do not block app startup on Riot queries.

    Render health checks must receive a quick response. We schedule the first
    refresh in the background so the service opens the port immediately and
    continues warming the cache without delaying startup.
    """
    if not RIOT_API_KEY:
        return

    try:
        asyncio.create_task(_refresh_in_background())
    except RuntimeError:
        pass


def empty_ranking_placeholder() -> dict[str, Any]:
    return {
        "challenge": {
            "name": "Los Gotish - SoloQ Challenge",
            "platform": "LAN",
            "queue": "Ranked Solo/Duo",
            "queueId": QUEUE_ID_SOLOQ,
            "year": CHALLENGE_YEAR,
            "month": CHALLENGE_MONTH,
            "timezone": TZ_NAME,
            "startTime": 0,
            "endTime": 0,
        },
        "summary": {
            "participants": len(PLAYERS),
            "mostGames": None,
            "bestWinrate": None,
        },
        "players": [
            {
                "riotId": player["game_name"] + "#" + player["tag_line"],
                "profileIcon": None,
                "summonerLevel": None,
                "rank": {"label": "Cargando…", "tier": None, "division": None, "lp": 0},
                "challenge": {"games": 0, "wins": 0, "losses": 0, "winrate": 0.0, "top3Champions": [], "mostWinningChampion": None},
                "error": "Cargando datos…",
                "position": idx + 1,
            }
            for idx, player in enumerate(PLAYERS)
        ],
        "updatedAt": int(time.time()),
        "cache": {
            "ttlSeconds": CACHE_SECONDS,
            "ageSeconds": 0,
            "nextRefreshAt": int(time.time()) + CACHE_SECONDS,
            "stale": True,
            "source": "warming",
            "warning": "Se están cargando los datos de Riot. Esto puede tardar unos segundos.",
        },
    }

TIER_VALUE = {
    "IRON": 0,
    "BRONZE": 1,
    "SILVER": 2,
    "GOLD": 3,
    "PLATINUM": 4,
    "EMERALD": 5,
    "DIAMOND": 6,
    "MASTER": 7,
    "GRANDMASTER": 8,
    "CHALLENGER": 9,
}
DIV_VALUE = {"IV": 0, "III": 1, "II": 2, "I": 3}


def riot_headers() -> dict[str, str]:
    return {"X-Riot-Token": RIOT_API_KEY}


def challenge_window() -> tuple[int, int, str]:
    tz = ZoneInfo(TZ_NAME)
    start_dt = datetime(CHALLENGE_YEAR, CHALLENGE_MONTH, 1, 0, 0, 0, tzinfo=tz)

    if CHALLENGE_MONTH == 12:
        month_end = datetime(CHALLENGE_YEAR + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        month_end = datetime(CHALLENGE_YEAR, CHALLENGE_MONTH + 1, 1, 0, 0, 0, tzinfo=tz)

    # While August is still in progress, only count matches played up to the current moment.
    effective_end = min(datetime.now(tz), month_end)
    return int(start_dt.timestamp()), int(effective_end.timestamp()), TZ_NAME


def _read_disk_ranking_cache() -> dict[str, Any] | None:
    try:
        if not RANKING_CACHE_FILE.exists():
            return None
        data = json.loads(RANKING_CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("updatedAt"):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def _get_cached_ranking() -> tuple[dict[str, Any] | None, str]:
    memory = _memory_cache.get("data")
    if isinstance(memory, dict) and memory.get("updatedAt"):
        return memory, "memory"

    disk = _read_disk_ranking_cache()
    if disk:
        _memory_cache["data"] = disk
        return disk, "disk"

    return None, "none"


def _save_ranking_cache(data: dict[str, Any]) -> None:
    _memory_cache["data"] = data
    try:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = RANKING_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(RANKING_CACHE_FILE)
    except OSError:
        # Render Free uses ephemeral storage. The in-memory cache still protects
        # Riot while the instance is alive, so a filesystem failure is non-fatal.
        pass


def _read_lp_history_cache() -> dict[str, list[dict[str, Any]]]:
    try:
        if not LP_HISTORY_FILE.exists():
            return {}
        data = json.loads(LP_HISTORY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        cleaned: dict[str, list[dict[str, Any]]] = {}
        for puuid, entries in data.items():
            if not isinstance(puuid, str) or not isinstance(entries, list):
                continue
            valid_entries = [entry for entry in entries if isinstance(entry, dict)]
            cleaned[puuid] = valid_entries
        return cleaned
    except (OSError, ValueError, TypeError):
        return {}


def _save_lp_history_cache(data: dict[str, list[dict[str, Any]]]) -> None:
    try:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = LP_HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(LP_HISTORY_FILE)
    except OSError:
        pass


def rank_lp_score(tier: str | None, division: str | None, lp: int | None) -> int | None:
    if not tier:
        return None
    tier_value = TIER_VALUE.get(tier)
    if tier_value is None:
        return None

    safe_lp = int(lp or 0)
    if tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
        return tier_value * 400 + 400 + safe_lp

    division_value = DIV_VALUE.get(division or "")
    if division_value is None:
        return tier_value * 400 + safe_lp
    return tier_value * 400 + division_value * 100 + safe_lp


def update_lp_snapshots(ranking_data: dict[str, Any]) -> None:
    history = _read_lp_history_cache()
    updated_at = int(ranking_data.get("updatedAt") or int(time.time()))

    for player in ranking_data.get("players", []):
        if not isinstance(player, dict) or player.get("error"):
            continue

        puuid = player.get("puuid")
        rank = player.get("rank") or {}
        if not puuid or not rank:
            continue

        snapshot = {
            "timestamp": updated_at,
            "lp": int(rank.get("lp") or 0),
            "tier": rank.get("tier"),
            "division": rank.get("division"),
            "label": rank.get("label"),
            "score": rank_lp_score(rank.get("tier"), rank.get("division"), int(rank.get("lp") or 0)),
        }

        entries = history.get(puuid, [])
        last = entries[-1] if entries else None
        is_same = bool(
            last
            and last.get("lp") == snapshot["lp"]
            and last.get("tier") == snapshot["tier"]
            and last.get("division") == snapshot["division"]
        )
        if not is_same:
            entries.append(snapshot)
            history[puuid] = entries[-LP_HISTORY_MAX_POINTS:]

    _save_lp_history_cache(history)


def build_lp_evolution(puuid: str, limit: int = 20) -> dict[str, Any]:
    history = _read_lp_history_cache()
    entries = history.get(puuid, [])
    if not entries:
        return {
            "history": [],
            "lastDelta": None,
            "gained": 0,
            "lost": 0,
            "trend": [],
            "hasData": False,
        }

    sliced = entries[-max(2, min(limit, LP_HISTORY_MAX_POINTS)):]
    points: list[dict[str, Any]] = []
    gained = 0
    lost = 0
    trend: list[int] = []

    previous: dict[str, Any] | None = None
    for current in sliced:
        delta = None
        rank_changed = False
        if previous:
            prev_score = previous.get("score")
            curr_score = current.get("score")
            if isinstance(prev_score, int) and isinstance(curr_score, int):
                delta = curr_score - prev_score
            rank_changed = (
                previous.get("tier") != current.get("tier")
                or previous.get("division") != current.get("division")
            )

        point = {
            "timestamp": int(current.get("timestamp") or 0),
            "lp": int(current.get("lp") or 0),
            "tier": current.get("tier"),
            "division": current.get("division"),
            "label": current.get("label"),
            "delta": delta,
            "rankChanged": rank_changed,
        }
        points.append(point)

        if isinstance(delta, int):
            trend.append(delta)
            if delta > 0:
                gained += delta
            elif delta < 0:
                lost += abs(delta)

        previous = current

    last_delta = points[-1].get("delta") if points else None
    return {
        "history": points,
        "lastDelta": last_delta,
        "gained": gained,
        "lost": lost,
        "trend": trend[-12:],
        "hasData": len(points) > 1,
    }


def _cache_response(
    data: dict[str, Any],
    *,
    source: str,
    stale: bool = False,
    warning: str | None = None,
) -> dict[str, Any]:
    response = copy.deepcopy(data)
    now = int(time.time())
    updated_at = int(response.get("updatedAt", 0) or 0)
    age = max(0, now - updated_at) if updated_at else 0
    response["cache"] = {
        "ttlSeconds": CACHE_SECONDS,
        "ageSeconds": age,
        "nextRefreshAt": updated_at + CACHE_SECONDS if updated_at else now,
        "stale": stale,
        "source": source,
        "warning": warning,
    }
    return response


async def riot_get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> Any:
    # Retry rate limits and temporary server failures. Riot explicitly supplies
    # Retry-After on 429 responses, so honor it instead of hammering the API.
    for attempt in range(6):
        try:
            response = await client.get(url, headers=riot_headers(), params=params, timeout=25)
        except httpx.RequestError as exc:
            if attempt < 5:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            raise HTTPException(
                status_code=503,
                detail=f"No se pudo conectar con Riot Games: {exc.__class__.__name__}.",
            ) from exc

        if response.status_code == 429:
            try:
                wait = float(response.headers.get("Retry-After", "1"))
            except ValueError:
                wait = 1.0
            await asyncio.sleep(max(1.0, wait))
            continue

        if response.status_code in (500, 502, 503, 504):
            await asyncio.sleep(min(2 ** attempt, 8))
            continue

        if response.status_code == 401:
            raise HTTPException(
                status_code=503,
                detail="Riot respondió 401: la API key no se envió correctamente o no es válida.",
            )
        if response.status_code == 403:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Riot respondió 403: la API key fue rechazada. "
                    "Si usas una Development API Key, probablemente expiró y debes regenerarla."
                ),
            )
        if response.status_code == 404:
            return None

        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Riot respondió HTTP {response.status_code} al consultar sus datos.",
            )

        try:
            return response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail="Riot devolvió una respuesta que no se pudo interpretar como JSON.",
            ) from exc

    raise HTTPException(
        status_code=503,
        detail="Riot API está limitando temporalmente las consultas. Intenta nuevamente en unos minutos.",
    )


async def get_ddragon(client: httpx.AsyncClient) -> tuple[str, dict[int, dict[str, str]]]:
    now = time.time()
    if (
        _ddragon_cache.get("version")
        and _ddragon_cache.get("champions")
        and now - float(_ddragon_cache.get("timestamp", 0.0)) < DDRAGON_CACHE_SECONDS
    ):
        return _ddragon_cache["version"], _ddragon_cache["champions"]

    versions = await client.get(
        "https://ddragon.leagueoflegends.com/api/versions.json",
        timeout=15,
    )
    versions.raise_for_status()
    version = versions.json()[0]

    champs = await client.get(
        f"https://ddragon.leagueoflegends.com/cdn/{version}/data/es_MX/champion.json",
        timeout=15,
    )
    if champs.status_code != 200:
        champs = await client.get(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json",
            timeout=15,
        )
    champs.raise_for_status()

    by_id: dict[int, dict[str, str]] = {}
    for champ in champs.json()["data"].values():
        by_id[int(champ["key"])] = {
            "name": champ["name"],
            "image": champ["image"]["full"],
        }

    _ddragon_cache.update({"timestamp": now, "version": version, "champions": by_id})
    return version, by_id


async def get_ddragon_items(client: httpx.AsyncClient, version: str) -> dict[int, dict[str, str]]:
    now = time.time()
    cached_items = _ddragon_cache.get("items")
    cached_version = _ddragon_cache.get("version")
    cached_ts = float(_ddragon_cache.get("timestamp", 0.0))

    if cached_items and cached_version == version and now - cached_ts < DDRAGON_CACHE_SECONDS:
        return cached_items

    items = await client.get(
        f"https://ddragon.leagueoflegends.com/cdn/{version}/data/es_MX/item.json",
        timeout=15,
    )
    if items.status_code != 200:
        items = await client.get(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json",
            timeout=15,
        )
    items.raise_for_status()

    by_id: dict[int, dict[str, str]] = {}
    for item_id, payload in items.json().get("data", {}).items():
        try:
            iid = int(item_id)
        except ValueError:
            continue
        by_id[iid] = {
            "name": payload.get("name", f"Item {item_id}"),
            "image": payload.get("image", {}).get("full", ""),
        }

    _ddragon_cache.update({"timestamp": now, "version": version, "items": by_id})
    return by_id


async def get_ddragon_live_assets(
    client: httpx.AsyncClient,
    version: str,
) -> tuple[dict[int, dict[str, str]], dict[int, dict[str, str]]]:
    """Return summoner spell and rune metadata used by the live-game cards."""
    cached = _ddragon_cache.get("live_assets")
    if cached and _ddragon_cache.get("live_assets_version") == version:
        return cached["spells"], cached["runes"]

    async def fetch_json(path: str, fallback_path: str | None = None) -> Any:
        response = await client.get(path, timeout=15)
        if response.status_code != 200 and fallback_path:
            response = await client.get(fallback_path, timeout=15)
        response.raise_for_status()
        return response.json()

    spells_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/es_MX/summoner.json"
    spells_fallback = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/summoner.json"
    runes_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/es_MX/runesReforged.json"
    runes_fallback = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/runesReforged.json"

    spells_payload, runes_payload = await asyncio.gather(
        fetch_json(spells_url, spells_fallback),
        fetch_json(runes_url, runes_fallback),
    )

    spells: dict[int, dict[str, str]] = {}
    for spell in spells_payload.get("data", {}).values():
        try:
            spell_id = int(spell.get("key", 0))
        except (TypeError, ValueError):
            continue
        spells[spell_id] = {
            "name": spell.get("name", f"Hechizo {spell_id}"),
            "image": spell.get("image", {}).get("full", ""),
        }

    runes: dict[int, dict[str, str]] = {}
    for style in runes_payload:
        style_id = int(style.get("id") or 0)
        if style_id:
            runes[style_id] = {
                "name": style.get("name", f"Runa {style_id}"),
                "icon": style.get("icon", ""),
            }
        for slot in style.get("slots", []):
            for rune in slot.get("runes", []):
                rune_id = int(rune.get("id") or 0)
                if rune_id:
                    runes[rune_id] = {
                        "name": rune.get("name", f"Runa {rune_id}"),
                        "icon": rune.get("icon", ""),
                    }

    live_assets = {"spells": spells, "runes": runes}
    _ddragon_cache.update({
        "live_assets": live_assets,
        "live_assets_version": version,
    })
    return spells, runes


async def resolve_account(
    client: httpx.AsyncClient,
    game_name: str,
    tag_line: str,
) -> dict[str, Any] | None:
    gn = quote(game_name, safe="")
    tl = quote(tag_line, safe="")
    account_url = (
        f"https://{REGION}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{gn}/{tl}"
    )
    account = await riot_get(client, account_url)
    if not account:
        return None

    puuid = account["puuid"]
    summoner_url = (
        f"https://{PLATFORM}.api.riotgames.com"
        f"/lol/summoner/v4/summoners/by-puuid/{quote(puuid, safe='')}"
    )
    summoner = await riot_get(client, summoner_url)
    if not summoner:
        return None

    return {**account, "summoner": summoner}


async def get_solo_rank(client: httpx.AsyncClient, puuid: str) -> dict[str, Any] | None:
    # PUUID endpoint: avoids relying on the legacy summoner id field.
    url = (
        f"https://{PLATFORM}.api.riotgames.com"
        f"/lol/league/v4/entries/by-puuid/{quote(puuid, safe='')}"
    )
    entries = await riot_get(client, url) or []
    return next((x for x in entries if x.get("queueType") == QUEUE_TYPE_SOLOQ), None)


async def get_challenge_match_ids(
    client: httpx.AsyncClient,
    puuid: str,
    start_time: int,
    end_time: int,
) -> list[str]:
    all_ids: list[str] = []
    start = 0

    while True:
        url = (
            f"https://{REGION}.api.riotgames.com"
            f"/lol/match/v5/matches/by-puuid/{quote(puuid, safe='')}/ids"
        )
        batch = await riot_get(
            client,
            url,
            params={
                "queue": QUEUE_ID_SOLOQ,
                "startTime": start_time,
                "endTime": end_time,
                "start": start,
                "count": 100,
            },
        ) or []

        all_ids.extend(batch)
        if len(batch) < 100:
            break

        start += 100
        # Guard against accidental unbounded paging. Five hundred ranked games
        # in one month is already far beyond the intended use of this challenge.
        if start >= 500:
            break

    return list(dict.fromkeys(all_ids))


async def get_match(client: httpx.AsyncClient, match_id: str) -> dict[str, Any] | None:
    cache_file = MATCH_CACHE_DIR / f"{match_id}.json"
    try:
        if cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass

    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{quote(match_id, safe='')}"
    match = await riot_get(client, url)
    if match:
        try:
            cache_file.write_text(json.dumps(match, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return match


def _live_rank_payload(rank: dict[str, Any] | None) -> dict[str, Any]:
    wins = int(rank.get("wins") or 0) if rank else 0
    losses = int(rank.get("losses") or 0) if rank else 0
    games = wins + losses
    return {
        "label": rank_label(rank),
        "tier": rank.get("tier") if rank else None,
        "division": rank.get("rank") if rank else None,
        "lp": int(rank.get("leaguePoints") or 0) if rank else 0,
        "wins": wins,
        "losses": losses,
        "games": games,
        "winrate": round(wins / games * 100, 1) if games else 0.0,
    }


def _empty_live_recent() -> dict[str, Any]:
    return {
        "games": 0,
        "wins": 0,
        "losses": 0,
        "winrate": 0.0,
        "recentForm": [],
        "streak": {"type": "none", "count": 0},
        "avg": {
            "kills": 0.0,
            "deaths": 0.0,
            "assists": 0.0,
            "kda": 0.0,
            "killParticipation": 0.0,
            "csPerMinute": 0.0,
            "vision": 0.0,
            "damagePerMinute": 0.0,
        },
        "mainRole": {"key": None, "label": "Sin datos", "games": 0},
        "_championCounts": {},
    }


async def get_live_recent_stats(
    client: httpx.AsyncClient,
    puuid: str,
    count: int = LIVE_RECENT_MATCHES,
) -> dict[str, Any]:
    """Calculate a compact, factual form snapshot from recent SoloQ matches."""
    match_ids_url = (
        f"https://{REGION}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/{quote(puuid, safe='')}/ids"
    )
    match_ids = await riot_get(
        client,
        match_ids_url,
        params={"queue": QUEUE_ID_SOLOQ, "count": count},
    ) or []

    samples: list[dict[str, Any]] = []
    role_counts: Counter[str] = Counter()
    champion_counts: Counter[int] = Counter()

    for match_id in match_ids[:count]:
        match = await get_match(client, match_id)
        if not match:
            continue
        info = match.get("info", {})
        participant = next(
            (entry for entry in info.get("participants", []) if entry.get("puuid") == puuid),
            None,
        )
        if not participant:
            continue

        team_id = int(participant.get("teamId") or 0)
        team_kills = sum(
            int(entry.get("kills") or 0)
            for entry in info.get("participants", [])
            if int(entry.get("teamId") or 0) == team_id
        )
        kills = int(participant.get("kills") or 0)
        assists = int(participant.get("assists") or 0)
        duration_minutes = max(1 / 60, float(info.get("gameDuration") or 0) / 60)
        cs = int(participant.get("totalMinionsKilled") or 0) + int(
            participant.get("neutralMinionsKilled") or 0
        )
        role = str(
            participant.get("teamPosition")
            or participant.get("individualPosition")
            or ""
        ).upper()
        champion_id = int(participant.get("championId") or 0)
        if role and role != "INVALID":
            role_counts[role] += 1
        if champion_id:
            champion_counts[champion_id] += 1

        samples.append({
            "win": bool(participant.get("win")),
            "kills": kills,
            "deaths": int(participant.get("deaths") or 0),
            "assists": assists,
            "killParticipation": round((kills + assists) / max(1, team_kills) * 100, 1),
            "csPerMinute": round(cs / duration_minutes, 2),
            "vision": int(participant.get("visionScore") or 0),
            "damagePerMinute": round(
                int(participant.get("totalDamageDealtToChampions") or 0) / duration_minutes,
                1,
            ),
        })

    if not samples:
        return _empty_live_recent()

    games = len(samples)
    wins = sum(1 for sample in samples if sample["win"])
    total_deaths = sum(sample["deaths"] for sample in samples)
    streak_win = bool(samples[0]["win"])
    streak_count = 0
    for sample in samples:
        if bool(sample["win"]) != streak_win:
            break
        streak_count += 1

    role_labels = {
        "TOP": "Superior",
        "JUNGLE": "Jungla",
        "MIDDLE": "Central",
        "BOTTOM": "Tirador",
        "UTILITY": "Soporte",
    }
    main_role, main_role_games = role_counts.most_common(1)[0] if role_counts else (None, 0)

    return {
        "games": games,
        "wins": wins,
        "losses": games - wins,
        "winrate": round(wins / games * 100, 1),
        "recentForm": ["W" if sample["win"] else "L" for sample in samples],
        "streak": {"type": "win" if streak_win else "loss", "count": streak_count},
        "avg": {
            "kills": round(sum(sample["kills"] for sample in samples) / games, 1),
            "deaths": round(total_deaths / games, 1),
            "assists": round(sum(sample["assists"] for sample in samples) / games, 1),
            "kda": round(
                (
                    sum(sample["kills"] for sample in samples)
                    + sum(sample["assists"] for sample in samples)
                ) / max(1, total_deaths),
                2,
            ),
            "killParticipation": round(
                sum(sample["killParticipation"] for sample in samples) / games,
                1,
            ),
            "csPerMinute": round(sum(sample["csPerMinute"] for sample in samples) / games, 1),
            "vision": round(sum(sample["vision"] for sample in samples) / games, 1),
            "damagePerMinute": round(
                sum(sample["damagePerMinute"] for sample in samples) / games,
                1,
            ),
        },
        "mainRole": {
            "key": main_role,
            "label": role_labels.get(main_role, "Flexible"),
            "games": main_role_games,
        },
        "_championCounts": dict(champion_counts),
    }


def build_live_insights(
    recent: dict[str, Any],
    *,
    current_champion_id: int,
    current_champion_name: str,
) -> list[dict[str, str]]:
    """Create transparent heuristic labels from the displayed recent metrics."""
    games = int(recent.get("games") or 0)
    if not games:
        return [{"label": "Sin SoloQ reciente", "tone": "neutral"}]

    avg = recent.get("avg") or {}
    streak = recent.get("streak") or {}
    champion_games = int((recent.get("_championCounts") or {}).get(current_champion_id, 0))
    insights: list[dict[str, str]] = []

    def add(label: str, tone: str) -> None:
        if len(insights) < 4 and not any(item["label"] == label for item in insights):
            insights.append({"label": label, "tone": tone})

    if int(streak.get("count") or 0) >= 3:
        add("Buena racha" if streak.get("type") == "win" else "Mala racha", "positive" if streak.get("type") == "win" else "danger")
    if champion_games >= 3:
        add(f"Especialista: {current_champion_name}", "positive")
    if float(avg.get("kills") or 0) >= 7 and float(avg.get("killParticipation") or 0) >= 50:
        add("Jugador agresivo", "positive")
    if float(avg.get("csPerMinute") or 0) >= 7:
        add("Buen farmeo", "positive")
    if float(avg.get("damagePerMinute") or 0) >= 650:
        add("Mucho daño", "positive")
    if float(avg.get("kda") or 0) >= 3.5:
        add("KDA sólido", "positive")
    if float(avg.get("vision") or 0) >= 25:
        add("Buena visión", "positive")
    if float(avg.get("deaths") or 0) >= 7:
        add("Riesgo alto", "danger")
    if games < LIVE_RECENT_MATCHES:
        add("Muestra reducida", "warning")
    if not insights:
        add("Perfil equilibrado", "neutral")
    return insights


async def get_live_player_profile(client: httpx.AsyncClient, puuid: str) -> dict[str, Any]:
    cached = _live_player_cache.get(puuid)
    if cached is not None:
        return copy.deepcopy(cached)

    rank_result, recent_result = await asyncio.gather(
        get_solo_rank(client, puuid),
        get_live_recent_stats(client, puuid),
        return_exceptions=True,
    )
    rank = None if isinstance(rank_result, Exception) else rank_result
    recent = _empty_live_recent() if isinstance(recent_result, Exception) else recent_result
    profile = {
        "rank": _live_rank_payload(rank),
        "recent": recent,
        "partial": isinstance(rank_result, Exception) or isinstance(recent_result, Exception),
    }
    _live_player_cache[puuid] = profile
    return copy.deepcopy(profile)


async def get_live_game(client: httpx.AsyncClient, puuid: str) -> dict[str, Any]:
    request_cache_key = ("live", puuid)
    cached = _live_cache.get(request_cache_key)
    if cached is not None:
        return copy.deepcopy(cached)

    spectator_url = (
        f"https://{PLATFORM}.api.riotgames.com"
        f"/lol/spectator/v5/active-games/by-summoner/{quote(puuid, safe='')}"
    )
    data = await riot_get(client, spectator_url)
    if data is None:
        result = {"in_game": False, "requestedPuuid": puuid, "fetchedAt": int(time.time())}
        _live_cache[request_cache_key] = result
        return copy.deepcopy(result)

    game_id = data.get("gameId")
    game_cache_key = ("live-game", game_id or puuid)
    cached_game = _live_cache.get(game_cache_key)
    if cached_game is not None:
        result = copy.deepcopy(cached_game)
        result["requestedPuuid"] = puuid
        _live_cache[request_cache_key] = result
        return result

    version, champion_map = await get_ddragon(client)
    try:
        spell_map, rune_map = await get_ddragon_live_assets(client, version)
    except (httpx.HTTPError, ValueError, TypeError):
        spell_map, rune_map = {}, {}

    participant_limit = asyncio.Semaphore(4)

    async def enrich(participant: dict[str, Any]) -> dict[str, Any]:
        participant_puuid = str(participant.get("puuid") or "")
        champion_id = int(participant.get("championId") or 0)
        champion_meta = champion_map.get(
            champion_id,
            {"name": participant.get("championName") or f"Campeón {champion_id}", "image": ""},
        )
        profile = {
            "rank": _live_rank_payload(None),
            "recent": _empty_live_recent(),
            "partial": True,
        }
        if participant_puuid:
            async with participant_limit:
                profile = await get_live_player_profile(client, participant_puuid)

        spells: list[dict[str, Any]] = []
        for field in ("spell1Id", "spell2Id"):
            spell_id = int(participant.get(field) or 0)
            meta = spell_map.get(spell_id, {})
            spells.append({
                "id": spell_id,
                "name": meta.get("name", f"Hechizo {spell_id}"),
                "icon": (
                    f"https://ddragon.leagueoflegends.com/cdn/{version}/img/spell/{meta['image']}"
                    if meta.get("image")
                    else None
                ),
            })

        perks = participant.get("perks") or {}
        rune_ids = list(perks.get("perkIds") or [])[:2]
        runes = []
        for rune_id in rune_ids:
            meta = rune_map.get(int(rune_id), {})
            runes.append({
                "id": int(rune_id),
                "name": meta.get("name", f"Runa {rune_id}"),
                "icon": (
                    f"https://ddragon.leagueoflegends.com/cdn/img/{meta['icon']}"
                    if meta.get("icon")
                    else None
                ),
            })

        recent = profile["recent"]
        insights = build_live_insights(
            recent,
            current_champion_id=champion_id,
            current_champion_name=champion_meta["name"],
        )
        public_recent = {key: value for key, value in recent.items() if not key.startswith("_")}
        return {
            "puuid": participant_puuid or None,
            "summonerName": participant_display_name(participant),
            "teamId": int(participant.get("teamId") or 0),
            "profileIcon": profile_icon(version, participant.get("profileIconId")),
            "championId": champion_id,
            "championName": champion_meta["name"],
            "championIcon": champion_icon(version, champion_meta["image"]) if champion_meta.get("image") else None,
            "spells": spells,
            "runes": runes,
            "rank": profile["rank"],
            "recent": public_recent,
            "mainRole": public_recent.get("mainRole"),
            "insights": insights,
            "partial": bool(profile.get("partial")),
        }

    participants = await asyncio.gather(
        *(enrich(participant) for participant in data.get("participants", []))
    )
    role_order = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4}

    def live_team_sort_key(entry: dict[str, Any]) -> int:
        role_key = (entry.get("mainRole") or {}).get("key")
        return role_order.get(role_key, 9)

    blue_team = sorted(
        (entry for entry in participants if entry["teamId"] == 100),
        key=live_team_sort_key,
    )
    red_team = sorted(
        (entry for entry in participants if entry["teamId"] == 200),
        key=live_team_sort_key,
    )

    bans = []
    for ban in data.get("bannedChampions", []):
        champion_id = int(ban.get("championId") or 0)
        meta = champion_map.get(champion_id, {})
        bans.append({
            "teamId": int(ban.get("teamId") or 0),
            "pickTurn": int(ban.get("pickTurn") or 0),
            "championId": champion_id,
            "championName": meta.get("name", f"Campeón {champion_id}"),
        })

    base_result = {
        "in_game": True,
        "gameId": game_id,
        "gameMode": data.get("gameMode"),
        "gameLength": int(data.get("gameLength") or 0),
        "gameStartTime": int(data.get("gameStartTime") or 0),
        "gameType": data.get("gameType"),
        "queueId": int(data.get("gameQueueConfigId") or 0),
        "mapId": int(data.get("mapId") or 0),
        "platformId": data.get("platformId") or PLATFORM.upper(),
        "statsWindow": LIVE_RECENT_MATCHES,
        "fetchedAt": int(time.time()),
        "bans": bans,
        "blue_team": blue_team,
        "red_team": red_team,
    }
    _live_cache[game_cache_key] = base_result
    result = copy.deepcopy(base_result)
    result["requestedPuuid"] = puuid
    _live_cache[request_cache_key] = result
    return copy.deepcopy(result)


async def get_match_history(client: httpx.AsyncClient, puuid: str, count: int = 5) -> list[dict[str, Any]]:
    cache_key = ("history", puuid, count)
    cached = _history_cache.get(cache_key)
    if cached is not None:
        return cached

    match_ids_url = (
        f"https://{REGION}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/{quote(puuid, safe='')}/ids"
    )
    match_ids = await riot_get(client, match_ids_url, params={"count": count}) or []

    history: list[dict[str, Any]] = []
    for match_id in match_ids[:count]:
        match = await get_match(client, match_id)
        if not match:
            continue

        info = match.get("info", {})
        participant = next(
            (p for p in info.get("participants", []) if p.get("puuid") == puuid),
            None,
        )
        if not participant:
            continue

        history.append(
            {
                "matchId": match_id,
                "champion": participant.get("championName"),
                "championId": participant.get("championId"),
                "kills": participant.get("kills"),
                "deaths": participant.get("deaths"),
                "assists": participant.get("assists"),
                "win": participant.get("win"),
                "gameMode": info.get("gameMode"),
                "gameDuration": info.get("gameDuration"),
                "gameCreation": info.get("gameCreation"),
                "queueId": info.get("queueId"),
            }
        )

    _history_cache[cache_key] = history
    return history


def summarize_recent_history(history: list[dict[str, Any]]) -> dict[str, Any]:
    games = len(history)
    wins = sum(1 for game in history if bool(game.get("win")))
    losses = games - wins
    winrate = round((wins / games * 100), 1) if games else 0.0

    streak_type = "none"
    streak_count = 0
    if history:
        latest_win = bool(history[0].get("win"))
        streak_type = "win" if latest_win else "loss"
        for game in history:
            if bool(game.get("win")) != latest_win:
                break
            streak_count += 1

    total_kills = sum(int(game.get("kills") or 0) for game in history)
    total_deaths = sum(int(game.get("deaths") or 0) for game in history)
    total_assists = sum(int(game.get("assists") or 0) for game in history)

    avg_kills = round(total_kills / games, 1) if games else 0.0
    avg_deaths = round(total_deaths / games, 1) if games else 0.0
    avg_assists = round(total_assists / games, 1) if games else 0.0
    avg_kda_ratio = round((total_kills + total_assists) / max(1, total_deaths), 2) if games else 0.0

    champions = Counter(
        game.get("champion") for game in history if isinstance(game.get("champion"), str)
    )
    most_played = champions.most_common(1)[0][0] if champions else None

    return {
        "games": games,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "currentStreak": {
            "type": streak_type,
            "count": streak_count,
            "label": (
                f"{streak_count} {'victorias' if streak_type == 'win' else 'derrotas'}"
                if streak_type in {"win", "loss"} and streak_count > 0
                else "Sin racha"
            ),
        },
        "avg": {
            "kills": avg_kills,
            "deaths": avg_deaths,
            "assists": avg_assists,
            "kda": avg_kda_ratio,
        },
        "mostPlayedChampion": most_played,
        "recentForm": ["W" if bool(game.get("win")) else "L" for game in history[:10]],
    }


def champion_icon(version: str, image_name: str) -> str:
    return f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{image_name}"


def profile_icon(version: str, icon_id: int | None) -> str | None:
    if icon_id is None:
        return None
    return f"https://ddragon.leagueoflegends.com/cdn/{version}/img/profileicon/{icon_id}.png"


def item_icon(version: str, image_name: str) -> str:
    return f"https://ddragon.leagueoflegends.com/cdn/{version}/img/item/{image_name}"


def item_icon_by_id(version: str, item_id: int) -> str:
    return f"https://ddragon.leagueoflegends.com/cdn/{version}/img/item/{item_id}.png"


def participant_display_name(participant: dict[str, Any]) -> str:
    riot_id = (participant.get("riotId") or "").strip()
    if riot_id:
        return riot_id

    summoner_name = (participant.get("summonerName") or "").strip()
    if summoner_name and summoner_name != "?":
        return summoner_name

    riot_game_name = (participant.get("riotIdGameName") or "").strip()
    riot_tag = (participant.get("riotIdTagline") or "").strip()
    if riot_game_name and riot_tag:
        return f"{riot_game_name}#{riot_tag}"
    if riot_game_name:
        return riot_game_name

    fallback_puuid = str(participant.get("puuid") or "")
    if fallback_puuid:
        return f"Jugador {fallback_puuid[:6]}"

    return "Jugador"


def build_participant_payload(
    participant: dict[str, Any],
    *,
    puuid: str,
    version: str,
    champion_map: dict[int, dict[str, str]],
    item_map: dict[int, dict[str, str]],
) -> dict[str, Any]:
    champion_id = int(participant.get("championId") or 0)
    champion_meta = champion_map.get(champion_id, {"name": participant.get("championName") or "?", "image": ""})

    items: list[dict[str, Any]] = []
    for idx in range(7):
        item_id = int(participant.get(f"item{idx}") or 0)
        if item_id <= 0:
            items.append(
                {
                    "slot": idx,
                    "id": 0,
                    "name": None,
                    "icon": None,
                    "filled": False,
                }
            )
            continue
        item_meta = item_map.get(item_id, {"name": f"Item {item_id}", "image": ""})
        items.append(
            {
                "slot": idx,
                "id": item_id,
                "name": item_meta["name"],
                "icon": item_icon_by_id(version, item_id),
                "filled": True,
            }
        )

    cs = int(participant.get("totalMinionsKilled") or 0) + int(participant.get("neutralMinionsKilled") or 0)
    return {
        "summonerName": participant_display_name(participant),
        "championName": champion_meta["name"],
        "championIcon": champion_icon(version, champion_meta["image"]) if champion_meta.get("image") else None,
        "kills": int(participant.get("kills") or 0),
        "deaths": int(participant.get("deaths") or 0),
        "assists": int(participant.get("assists") or 0),
        "damage": int(participant.get("totalDamageDealtToChampions") or 0),
        "cs": cs,
        "items": items,
        "win": bool(participant.get("win")),
        "isPlayer": participant.get("puuid") == puuid,
    }


def build_match_details(
    match_id: str,
    match: dict[str, Any],
    *,
    puuid: str,
    version: str,
    champion_map: dict[int, dict[str, str]],
    item_map: dict[int, dict[str, str]],
) -> dict[str, Any] | None:
    info = match.get("info", {})
    participants = info.get("participants", [])
    player = next((p for p in participants if p.get("puuid") == puuid), None)
    if not player:
        return None

    blue_team: list[dict[str, Any]] = []
    red_team: list[dict[str, Any]] = []
    team_kills = {100: 0, 200: 0}

    for participant in participants:
        team_id = int(participant.get("teamId") or 0)
        if team_id in team_kills:
            team_kills[team_id] += int(participant.get("kills") or 0)

    for participant in participants:
        team_id = int(participant.get("teamId") or 0)
        payload = build_participant_payload(
            participant,
            puuid=puuid,
            version=version,
            champion_map=champion_map,
            item_map=item_map,
        )
        if team_id == 100:
            blue_team.append(payload)
        elif team_id == 200:
            red_team.append(payload)

    player_team_id = int(player.get("teamId") or 100)
    player_team_kills = max(1, team_kills.get(player_team_id, 0))
    player_kp = round(((int(player.get("kills") or 0) + int(player.get("assists") or 0)) / player_team_kills) * 100, 1)
    game_duration = int(info.get("gameDuration") or 0)
    game_creation = int(info.get("gameCreation") or 0)
    game_end = int(info.get("gameEndTimestamp") or (game_creation + game_duration * 1000))

    return {
        "matchId": match_id,
        "queueId": info.get("queueId"),
        "gameMode": info.get("gameMode"),
        "gameDuration": game_duration,
        "gameCreation": game_creation,
        "gameEnd": game_end,
        "win": bool(player.get("win")),
        "champion": player.get("championName"),
        "championIcon": next((p["championIcon"] for p in blue_team + red_team if p.get("isPlayer")), None),
        "kills": int(player.get("kills") or 0),
        "deaths": int(player.get("deaths") or 0),
        "assists": int(player.get("assists") or 0),
        "damage": int(player.get("totalDamageDealtToChampions") or 0),
        "cs": int(player.get("totalMinionsKilled") or 0) + int(player.get("neutralMinionsKilled") or 0),
        "killParticipation": player_kp,
        "teamKills": team_kills,
        "composition": {
            "blue": blue_team,
            "red": red_team,
        },
    }


async def get_player_detailed_history(
    client: httpx.AsyncClient,
    puuid: str,
    *,
    count: int,
    version: str,
    champion_map: dict[int, dict[str, str]],
    item_map: dict[int, dict[str, str]],
) -> list[dict[str, Any]]:
    cache_key = ("details", puuid, count)
    cached = _history_cache.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)

    match_ids_url = (
        f"https://{REGION}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/{quote(puuid, safe='')}/ids"
    )
    match_ids = await riot_get(client, match_ids_url, params={"count": count}) or []

    match_limit = asyncio.Semaphore(4)

    async def load_detail(match_id: str) -> dict[str, Any] | None:
        async with match_limit:
            match = await get_match(client, match_id)
        if not match:
            return None
        return build_match_details(
            match_id,
            match,
            puuid=puuid,
            version=version,
            champion_map=champion_map,
            item_map=item_map,
        )

    details = await asyncio.gather(*(load_detail(match_id) for match_id in match_ids[:count]))
    output = [detail for detail in details if detail is not None]
    _history_cache[cache_key] = output
    return copy.deepcopy(output)


def rank_score(rank: dict[str, Any] | None) -> tuple[int, int, int]:
    if not rank:
        return (-1, -1, -1)
    tier = TIER_VALUE.get(rank.get("tier", ""), -1)
    division = DIV_VALUE.get(rank.get("rank", ""), 4 if tier >= 7 else -1)
    lp = int(rank.get("leaguePoints", 0))
    return (tier, division, lp)


def rank_label(rank: dict[str, Any] | None) -> str:
    if not rank:
        return "Sin clasificación"
    tier_es = {
        "IRON": "Hierro",
        "BRONZE": "Bronce",
        "SILVER": "Plata",
        "GOLD": "Oro",
        "PLATINUM": "Platino",
        "EMERALD": "Esmeralda",
        "DIAMOND": "Diamante",
        "MASTER": "Master",
        "GRANDMASTER": "Grandmaster",
        "CHALLENGER": "Challenger",
    }.get(rank.get("tier", ""), rank.get("tier", ""))
    division = rank.get("rank", "")
    if rank.get("tier") in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
        division = ""
    return f"{tier_es} {division}".strip()


async def build_player(
    client: httpx.AsyncClient,
    raw_player: dict[str, str],
    version: str,
    champion_map: dict[int, dict[str, str]],
    start_time: int,
    end_time: int,
) -> dict[str, Any]:
    riot_id = f'{raw_player["game_name"]}#{raw_player["tag_line"]}'
    account = await resolve_account(client, raw_player["game_name"], raw_player["tag_line"])

    if not account:
        return {
            "riotId": riot_id,
            "error": "No se encontró esta cuenta en LAN.",
            "rankScore": (-1, -1, -1),
        }

    puuid = account["puuid"]
    summoner = account["summoner"]
    rank = await get_solo_rank(client, puuid)
    match_ids = await get_challenge_match_ids(client, puuid, start_time, end_time)

    games = 0
    wins = 0
    champ_games: Counter[int] = Counter()
    champ_wins: Counter[int] = Counter()

    for match_id in match_ids:
        match = await get_match(client, match_id)
        if not match:
            continue

        info = match.get("info", {})
        if info.get("queueId") != QUEUE_ID_SOLOQ:
            continue

        participant = next(
            (p for p in info.get("participants", []) if p.get("puuid") == puuid),
            None,
        )
        if not participant:
            continue

        champion_id = int(participant.get("championId", 0))
        did_win = bool(participant.get("win"))

        games += 1
        if did_win:
            wins += 1
        if champion_id:
            champ_games[champion_id] += 1
            if did_win:
                champ_wins[champion_id] += 1

    losses = games - wins
    winrate = round((wins / games * 100), 1) if games else 0.0

    top3: list[dict[str, Any]] = []
    for champion_id, count in champ_games.most_common(3):
        meta = champion_map.get(
            champion_id,
            {"name": f"Champion {champion_id}", "image": ""},
        )
        top3.append(
            {
                "id": champion_id,
                "name": meta["name"],
                "games": count,
                "wins": champ_wins[champion_id],
                "icon": champion_icon(version, meta["image"]) if meta["image"] else None,
            }
        )

    best = None
    if champ_wins:
        # More victories wins the comparison. In a tie, prefer the champion with
        # more games so the result remains intuitive and deterministic.
        best_id = max(champ_wins, key=lambda cid: (champ_wins[cid], champ_games[cid], -cid))
        meta = champion_map.get(best_id, {"name": f"Champion {best_id}", "image": ""})
        best = {
            "id": best_id,
            "name": meta["name"],
            "wins": champ_wins[best_id],
            "games": champ_games[best_id],
            "icon": champion_icon(version, meta["image"]) if meta["image"] else None,
        }

    return {
        "riotId": (
            f'{account.get("gameName", raw_player["game_name"])}'
            f'#{account.get("tagLine", raw_player["tag_line"])}'
        ),
        "puuid": puuid,
        "profileIcon": profile_icon(version, summoner.get("profileIconId")),
        "summonerLevel": summoner.get("summonerLevel"),
        "rank": {
            "label": rank_label(rank),
            "tier": rank.get("tier") if rank else None,
            "division": rank.get("rank") if rank else None,
            "lp": rank.get("leaguePoints") if rank else 0,
        },
        "rankScore": rank_score(rank),
        "challenge": {
            "games": games,
            "wins": wins,
            "losses": losses,
            "winrate": winrate,
            "top3Champions": top3,
            "mostWinningChampion": best,
        },
        "error": None,
    }


async def build_ranking() -> dict[str, Any]:
    start_time, end_time, timezone_name = challenge_window()

    async with httpx.AsyncClient() as client:
        version, champion_map = await get_ddragon(client)

        # Intentionally sequential. A Development/Personal key has a low two-minute
        # budget and this avoids creating bursts while a cold cache is rebuilt.
        players: list[dict[str, Any]] = []
        for raw_player in PLAYERS:
            players.append(
                await build_player(
                    client,
                    raw_player,
                    version,
                    champion_map,
                    start_time,
                    end_time,
                )
            )

    players.sort(key=lambda p: tuple(p.get("rankScore", (-1, -1, -1))), reverse=True)

    for index, player in enumerate(players, 1):
        player["position"] = index
        player.pop("rankScore", None)

    valid = [p for p in players if not p.get("error")]
    leader_games = max(valid, key=lambda p: p["challenge"]["games"], default=None)
    leader_wr_pool = [p for p in valid if p["challenge"]["games"] > 0]
    leader_wr = max(
        leader_wr_pool,
        key=lambda p: (p["challenge"]["winrate"], p["challenge"]["games"]),
        default=None,
    )

    return {
        "challenge": {
            "name": "Los Gotish - SoloQ Challenge",
            "platform": "LAN",
            "queue": "Ranked Solo/Duo",
            "queueId": QUEUE_ID_SOLOQ,
            "year": CHALLENGE_YEAR,
            "month": CHALLENGE_MONTH,
            "timezone": timezone_name,
            "startTime": start_time,
            "endTime": end_time,
        },
        "summary": {
            "participants": len(PLAYERS),
            "mostGames": leader_games["riotId"] if leader_games else None,
            "bestWinrate": leader_wr["riotId"] if leader_wr else None,
        },
        "players": players,
        "updatedAt": int(time.time()),
    }


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


async def _refresh_in_background() -> None:
    """Refresh the ranking cache in background without blocking requests.

    This function acquires the same lock used for synchronous refreshes so
    parallel executions remain serialized. Exceptions are caught and logged
    to avoid crashing the background task.
    """
    async with _refresh_lock:
        try:
            fresh = await build_ranking()
        except Exception as exc:  # pylint: disable=broad-except
            print("Background refresh failed:", exc)
            return
        update_lp_snapshots(fresh)
        _save_ranking_cache(fresh)

@app.get("/health")
async def health():
    # Render can check this endpoint without consuming Riot API requests.
    return {"status": "ok"}


@app.get("/api/live/{puuid}")
async def live_game(puuid: str):
    if not RIOT_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Falta RIOT_API_KEY.",
        )

    async with httpx.AsyncClient() as client:
        return await get_live_game(client, puuid)


@app.get("/api/history/{puuid}")
async def match_history(puuid: str, count: int = 5):
    if not RIOT_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Falta RIOT_API_KEY.",
        )

    async with httpx.AsyncClient() as client:
        return await get_match_history(client, puuid, count=count)


@app.get("/api/player/{puuid}/details")
async def player_details(puuid: str, count: int = 10):
    if not RIOT_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Falta RIOT_API_KEY.",
        )

    safe_count = max(1, min(count, 20))
    lp_evolution = build_lp_evolution(puuid, limit=24)
    async with httpx.AsyncClient() as client:
        version, champion_map = await get_ddragon(client)
        item_map = await get_ddragon_items(client, version)
        history = await get_player_detailed_history(
            client,
            puuid,
            count=safe_count,
            version=version,
            champion_map=champion_map,
            item_map=item_map,
        )
        summary = summarize_recent_history(history)

    return {
        "summary": summary,
        "lpEvolution": lp_evolution,
        "history": history,
        "count": safe_count,
        "updatedAt": int(time.time()),
    }


@app.get("/api/player/{puuid}/lp-history")
async def player_lp_history(puuid: str, limit: int = 24):
    safe_limit = max(2, min(limit, LP_HISTORY_MAX_POINTS))
    return build_lp_evolution(puuid, limit=safe_limit)


@app.get("/api/ranking")
async def ranking():
    cached, source = _get_cached_ranking()
    now = int(time.time())

    if cached:
        age = max(0, now - int(cached.get("updatedAt", 0)))
        if age < CACHE_SECONDS:
            return _cache_response(cached, source=source)

        # Cache expired. If a refresh is already running, serve the stale cache.
        if _refresh_lock.locked():
            return _cache_response(
                cached,
                source=source,
                stale=True,
                warning="Hay una actualización en curso; se muestran los últimos datos disponibles.",
            )

        # Start a background refresh and immediately return the stale cache so
        # clients are not blocked while Riot is consulted.
        try:
            asyncio.create_task(_refresh_in_background())
        except RuntimeError:
            # In extremely constrained environments create_task may fail; fall
            # back to a blocking refresh in that case.
            async with _refresh_lock:
                if not RIOT_API_KEY:
                    return _cache_response(
                        cached,
                        source=source,
                        stale=True,
                        warning="Falta RIOT_API_KEY; se muestran los últimos datos guardados.",
                    )
                fresh = await build_ranking()
                update_lp_snapshots(fresh)
                _save_ranking_cache(fresh)
                return _cache_response(fresh, source="riot")

        return _cache_response(
            cached,
            source=source,
            stale=True,
            warning="Se está actualizando en segundo plano; mostrando los últimos datos.",
        )

    # No cache exists; avoid blocking the browser forever on a cold start.
    if not RIOT_API_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "Falta RIOT_API_KEY. En local configúrala en .env; "
                "en Render agrégala en Environment."
            ),
        )

    # Best-effort placeholder so the browser does not sit on an endless loading state
    # while the first full Riot refresh is still being gathered.
    placeholder = empty_ranking_placeholder()
    try:
        asyncio.create_task(_refresh_in_background())
    except RuntimeError:
        pass
    return placeholder


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host=host, port=port, reload=True)
