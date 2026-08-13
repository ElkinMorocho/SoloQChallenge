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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from players import PLAYERS

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
CACHE_ROOT = BASE_DIR / "cache"
MATCH_CACHE_DIR = CACHE_ROOT / "matches"
RANKING_CACHE_FILE = CACHE_ROOT / "ranking.json"
MATCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)

RIOT_API_KEY = os.getenv("RIOT_API_KEY", "").strip()
PLATFORM = os.getenv("RIOT_PLATFORM", "la1").strip().lower()
REGION = os.getenv("RIOT_REGION", "americas").strip().lower()
TZ_NAME = os.getenv("CHALLENGE_TIMEZONE", "America/Guayaquil").strip()
CHALLENGE_YEAR = int(os.getenv("CHALLENGE_YEAR", "2026"))
CHALLENGE_MONTH = int(os.getenv("CHALLENGE_MONTH", "8"))
CACHE_SECONDS = max(60, int(os.getenv("CACHE_SECONDS", "300")))

QUEUE_ID_SOLOQ = 420
QUEUE_TYPE_SOLOQ = "RANKED_SOLO_5x5"
DDRAGON_CACHE_SECONDS = 6 * 60 * 60

app = FastAPI(title="Los Gotish - SoloQ Challenge")
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
}
_ranking_cache = TTLCache(maxsize=1, ttl=180)
_live_cache = TTLCache(maxsize=20, ttl=60)
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


async def get_live_game(client: httpx.AsyncClient, puuid: str) -> dict[str, Any]:
    cache_key = ("live", puuid)
    cached = _live_cache.get(cache_key)
    if cached is not None:
        return cached

    spectator_url = (
        f"https://{PLATFORM}.api.riotgames.com"
        f"/lol/spectator/v5/active-games/by-summoner/{quote(puuid, safe='')}"
    )
    data = await riot_get(client, spectator_url)
    if data is None:
        result = {"in_game": False}
    else:
        blue_team = []
        red_team = []
        for participant in data.get("participants", []):
            entry = {
                "summonerName": participant.get("summonerName"),
                "championId": participant.get("championId"),
                "championName": participant.get("championName"),
                "teamId": participant.get("teamId"),
            }
            if participant.get("teamId") == 100:
                blue_team.append(entry)
            elif participant.get("teamId") == 200:
                red_team.append(entry)

        result = {
            "in_game": True,
            "gameMode": data.get("gameMode"),
            "gameLength": data.get("gameLength"),
            "gameType": data.get("gameType"),
            "blue_team": blue_team,
            "red_team": red_team,
        }

    _live_cache[cache_key] = result
    return result


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
    match_ids_url = (
        f"https://{REGION}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/{quote(puuid, safe='')}/ids"
    )
    match_ids = await riot_get(client, match_ids_url, params={"count": count}) or []

    output: list[dict[str, Any]] = []
    for match_id in match_ids[:count]:
        match = await get_match(client, match_id)
        if not match:
            continue
        detail = build_match_details(
            match_id,
            match,
            puuid=puuid,
            version=version,
            champion_map=champion_map,
            item_map=item_map,
        )
        if detail:
            output.append(detail)

    return output


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
        "history": history,
        "count": safe_count,
        "updatedAt": int(time.time()),
    }


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
