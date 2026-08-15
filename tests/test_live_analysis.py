import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app import (
    _cache_response,
    _interactive_riot_request,
    _spectator_cache,
    build_live_itemization,
    build_team_summary,
    get_spectator_snapshot,
    riot_get,
)


ITEM_IDS = {
    1004, 1054, 1055, 1056, 1101, 1102, 1103, 2003, 2055, 3006, 3020,
    3026, 3031, 3033, 3036, 3046, 3047, 3050, 3053, 3068, 3071, 3072,
    3075, 3089, 3094, 3107, 3109, 3111, 3118, 3123, 3135, 3142, 3143,
    3156, 3157, 3158, 3165, 3190, 3222, 3504, 3814, 3865, 3916, 4401,
    4645, 6333, 6610, 6616, 6617, 6620, 6621, 6653, 6655, 6664, 6665,
    6672, 6692, 6694, 6695, 6697, 6701,
}


def item_map():
    return {
        item_id: {"name": f"Item {item_id}", "gold": 1000, "image": f"{item_id}.png"}
        for item_id in ITEM_IDS
    }


def member(
    name,
    *,
    role="MIDDLE",
    tags=None,
    damage="magic",
    attack=3,
    magic=8,
    defense=4,
):
    return {
        "puuid": f"puuid-{name}",
        "championName": name,
        "championIcon": f"https://example.test/{name}.png",
        "championTags": tags or ["Mage"],
        "championInfo": {"attack": attack, "magic": magic, "defense": defense},
        "damageProfile": damage,
        "mainRole": {"key": role, "label": role.title(), "games": 5},
        "recent": {
            "games": 5,
            "winrate": 60,
            "avg": {
                "kills": 5,
                "deaths": 4,
                "assists": 8,
                "goldPerMinute": 410,
                "damagePerMinute": 620,
                "visionWardsPerMinute": 0.14,
            },
        },
    }


class LiveAnalysisTests(unittest.TestCase):
    def test_team_summary_aggregates_porofessor_style_metrics(self):
        team = [
            member("Ornn", role="TOP", tags=["Tank", "Fighter"], damage="magic", defense=9),
            member("Vi", role="JUNGLE", tags=["Fighter", "Assassin"], damage="physical", attack=8, defense=7),
            member("Orianna", role="MIDDLE", tags=["Mage", "Support"]),
            member("Jinx", role="BOTTOM", tags=["Marksman"], damage="physical", attack=9),
            member("Lulu", role="UTILITY", tags=["Support", "Mage"]),
        ]

        summary = build_team_summary(team)

        self.assertEqual(summary["sampledPlayers"], 5)
        self.assertEqual(summary["averages"]["winrate"], 60.0)
        self.assertEqual(summary["averages"]["goldPerMinute"], 410)
        self.assertEqual(summary["averages"]["kda"], {"kills": 5.0, "deaths": 4.0, "assists": 8.0})
        self.assertGreaterEqual(summary["composition"]["frontline"], 2)
        self.assertIn("Buena línea frontal", {entry["label"] for entry in summary["insights"]})

    def test_yuumi_build_starts_for_support_and_uses_enchanter_core(self):
        yuumi = member("Yuumi", role="UTILITY", tags=["Support", "Mage"])
        ally = [
            member("Jinx", role="BOTTOM", tags=["Marksman"], damage="physical", attack=9),
            yuumi,
        ]
        enemies = [
            member("Nautilus", role="UTILITY", tags=["Tank", "Support"], defense=9),
            member("Ashe", role="BOTTOM", tags=["Marksman"], damage="physical", attack=8),
        ]

        build = build_live_itemization(
            yuumi,
            ally,
            enemies,
            version="test",
            item_map=item_map(),
            game_length=8 * 60,
        )

        self.assertEqual(build["archetype"], "enchanter")
        self.assertEqual(build["currentPhase"], "lane")
        self.assertFalse(build["skipBoots"])
        self.assertEqual(build["starter"][0]["id"], 3865)
        self.assertEqual(build["core"][0]["id"], 6617)
        self.assertIn("yuumi", build["source"]["url"])

    def test_build_adds_composition_counters_and_changes_phase(self):
        carry = member("Jinx", role="BOTTOM", tags=["Marksman"], damage="physical", attack=9)
        enemies = [
            member("Dr. Mundo", role="TOP", tags=["Tank", "Fighter"], defense=9),
            member("Amumu", role="JUNGLE", tags=["Tank", "Mage"], defense=8),
            member("Vladimir", role="MIDDLE", tags=["Mage"]),
            member("Ashe", role="BOTTOM", tags=["Marksman"], damage="physical", attack=8),
            member("Soraka", role="UTILITY", tags=["Support", "Mage"]),
        ]

        build = build_live_itemization(
            carry,
            [carry],
            enemies,
            version="test",
            item_map=item_map(),
            game_length=28 * 60,
        )

        labels = {entry["label"] for entry in build["adaptations"]}
        self.assertEqual(build["currentPhase"], "teamfight")
        self.assertIn("Curación rival", labels)
        self.assertIn("Doble línea frontal", labels)
        self.assertEqual(len(build["phasePlan"]), 3)

    def test_stale_ranking_never_schedules_an_immediate_reload_loop(self):
        with patch("app.time.time", return_value=1_000):
            response = _cache_response(
                {"updatedAt": 100, "players": []},
                source="memory",
                stale=True,
            )

        self.assertEqual(response["cache"]["nextRefreshAt"], 1_060)


class SpectatorStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _spectator_cache.clear()

    async def test_lightweight_spectator_payload_is_reused_by_full_analysis(self):
        spectator_payload = {"gameId": 123, "participants": [{"puuid": "player-1"}]}
        riot_mock = AsyncMock(return_value=spectator_payload)

        with patch("app.riot_get", riot_mock):
            first = await get_spectator_snapshot(object(), "player-1")
            second = await get_spectator_snapshot(object(), "player-1")

        self.assertEqual(first["gameId"], 123)
        self.assertEqual(second["gameId"], 123)
        riot_mock.assert_awaited_once()


class InteractiveRiotTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_rate_limit_fails_fast_for_modal_requests(self):
        response = Mock(status_code=429, headers={"Retry-After": "45"})
        client = Mock()
        client.get = AsyncMock(return_value=response)
        token = _interactive_riot_request.set(True)

        try:
            with self.assertRaises(HTTPException) as raised:
                await riot_get(client, "https://example.test/riot")
        finally:
            _interactive_riot_request.reset(token)

        self.assertEqual(raised.exception.status_code, 429)
        client.get.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
