from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import timedelta
from threading import Lock
from urllib.parse import parse_qs

from asgiref.sync import sync_to_async
from channels.auth import login
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils import timezone

from apps.matches.models import Lobby, LobbyState, Match, MatchState
from apps.trivia.models import Question, QuestionDifficulty, QuestionType
from apps.trivia.services.opentdb import CATEGORIES
from worker.hex_utils import build_hex_cells
from worker.question_provider import build_csv_lobby_metadata

from .publisher import GameInputMessage, publish_game_input, publish_start_game_payload


Player = get_user_model()
ACTIVE_LOBBY_STATES = [LobbyState.OPEN, LobbyState.FULL, LobbyState.IN_MATCH]
DIRECTORY_GROUP_NAME = "lobby_directory"
ACTIVE_MATCH_STATES = [
    MatchState.QUEUED,
    MatchState.BASE_SELECTION,
    MatchState.EXPANSION,
    MatchState.DUEL,
    MatchState.BASE_SIEGE,
]

MATCH_PRESENCE: dict[str, dict[int, int]] = defaultdict(dict)
MATCH_PRESENCE_LOCK = Lock()


def current_connected_player_ids(match_id: str) -> list[int]:
    with MATCH_PRESENCE_LOCK:
        presence = MATCH_PRESENCE.get(match_id, {})
        return sorted(player_id for player_id, count in presence.items() if count > 0)


def update_match_presence(match_id: str, player_id: int, connected: bool) -> tuple[bool, list[int]]:
    with MATCH_PRESENCE_LOCK:
        presence = MATCH_PRESENCE[match_id]
        previous_count = presence.get(player_id, 0)
        if connected:
            presence[player_id] = previous_count + 1
        elif previous_count <= 1:
            presence.pop(player_id, None)
        else:
            presence[player_id] = previous_count - 1

        if not presence:
            MATCH_PRESENCE.pop(match_id, None)
        connected_ids = sorted(player_id for player_id, count in presence.items() if count > 0)

    became_visible = connected and previous_count == 0
    became_hidden = (not connected) and previous_count > 0 and player_id not in connected_ids
    return became_visible or became_hidden, connected_ids


def clear_match_presence(match_id: str) -> None:
    with MATCH_PRESENCE_LOCK:
        MATCH_PRESENCE.pop(match_id, None)


def serialize_directory_lobby(lobby: Lobby) -> dict:
    return {
        "id": str(lobby.public_id),
        "name": lobby.name,
        "state": lobby.state,
        "host": {"id": lobby.host_id, "name": lobby.host.game_name},
        "guest": {"id": lobby.guest_id, "name": lobby.guest.game_name} if lobby.guest_id else None,
        "can_join": lobby.state == LobbyState.OPEN and lobby.guest_id is None,
        "settings": {
            "test_mode": lobby.test_mode,
            "map_radius": lobby.map_radius,
            "battle_rounds": lobby.battle_rounds,
            "timer_seconds": lobby.timer_seconds,
        },
    }


class GuestPlayerMixin:
    async def ensure_player(self):
        self.player = await self._ensure_player()
        self.player_group_name = f"player_{self.player.id}"

    async def _ensure_player(self):
        user = self.scope["user"]
        if user.is_authenticated:
            return await self._get_player(user.id)

        guest_token = self._get_guest_token()
        if guest_token:
            player = await self._get_player_by_guest_token(guest_token)
            if player is not None:
                await login(self.scope, player)
                await database_sync_to_async(self.scope["session"].save)()
                return player

        player = await self._create_guest_player()
        await login(self.scope, player)
        await database_sync_to_async(self.scope["session"].save)()
        return player

    def _get_guest_token(self):
        raw_query = self.scope.get("query_string", b"").decode("utf-8")
        values = parse_qs(raw_query).get("guest_token")
        if not values:
            return None
        return values[0]

    @database_sync_to_async
    def _get_player(self, player_id):
        return Player.objects.get(pk=player_id)

    @database_sync_to_async
    def _get_player_by_guest_token(self, guest_token):
        try:
            return Player.objects.get(guest_token=guest_token, is_guest=True)
        except Player.DoesNotExist:
            return None

    @database_sync_to_async
    def _create_guest_player(self):
        stamp = timezone.now().strftime("%Y%m%d%H%M%S%f")
        return Player.objects.create_user(username=f"guest-{stamp}", password=None, is_guest=True)


class MatchmakingConsumer(GuestPlayerMixin, AsyncJsonWebsocketConsumer):
    async def connect(self):
        await self.ensure_player()
        self.lobby_group_name = None
        await self.accept()
        await self.channel_layer.group_add(self.player_group_name, self.channel_name)
        await self.channel_layer.group_add(DIRECTORY_GROUP_NAME, self.channel_name)
        await self.send_json(
            {
                "type": "connection.ready",
                "player": self._serialize_player(self.player),
                "options": await self._get_lobby_options(),
                "active_lobby": None,
                "active_match": None,
                "active_lobbies": await self._list_active_lobbies(),
            }
        )

    async def disconnect(self, close_code):
        if hasattr(self, "player"):
            current_lobby = await self._get_active_lobby_for_player(self.player.id)
            if current_lobby is not None and current_lobby.state in {LobbyState.OPEN, LobbyState.FULL}:
                try:
                    result = await self._leave_lobby(self.player.id, current_lobby.public_id)
                except ValidationError:
                    result = None
                if result is not None:
                    remaining_lobby, _ = result
                    await self._broadcast_lobby_snapshot(remaining_lobby)
                await self._broadcast_lobby_directory()
        if hasattr(self, "player_group_name"):
            await self.channel_layer.group_discard(self.player_group_name, self.channel_name)
        await self.channel_layer.group_discard(DIRECTORY_GROUP_NAME, self.channel_name)
        if self.lobby_group_name:
            await self.channel_layer.group_discard(self.lobby_group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = content.get("action")
        if action == "set_profile":
            await self._handle_set_profile(content)
            return
        if action == "create_lobby":
            await self._handle_create_lobby(content)
            return
        if action == "join_lobby":
            await self._handle_join_lobby(content)
            return
        if action == "leave_lobby":
            await self._handle_leave_lobby()
            return
        if action == "start_game":
            await self._handle_start_game()
            return
        if action == "kill_all_lobbies":
            await self._handle_kill_all_lobbies()
            return
        if action == "reset_identity":
            await self._handle_reset_identity()
            return
        if action == "list_lobbies":
            await self._send_lobby_directory()
            return
        if action == "ping":
            await self.send_json({"type": "pong"})
            return
        await self.send_json({"type": "error", "message": "Unsupported action."})

    async def _handle_set_profile(self, content):
        display_name = str(content.get("display_name", "")).strip()
        if len(display_name) < 2 or len(display_name) > 32:
            await self.send_json({"type": "error", "message": "Name must be 2-32 characters."})
            return
        self.player = await self._update_player_display_name(self.player.id, display_name)
        await self.send_json({"type": "profile.updated", "player": self._serialize_player(self.player)})
        active_lobby = await self._get_active_lobby_for_player(self.player.id)
        if active_lobby is not None:
            await self._broadcast_lobby_snapshot(active_lobby)
        await self._broadcast_lobby_directory()

    async def _handle_create_lobby(self, content):
        existing = await self._get_active_lobby_for_player(self.player.id)
        if existing is not None:
            await self.send_json({"type": "error", "message": "Leave your current lobby first."})
            return
        try:
            lobby = await self._create_lobby(
                player_id=self.player.id,
                lobby_name=str(content.get("lobby_name", "")).strip(),
                categories=content.get("categories") or [],
                question_types=content.get("question_types") or [],
                difficulties=content.get("difficulties") or [],
                test_mode=bool(content.get("test_mode")),
                map_radius=int(content.get("map_radius") or 3),
                battle_rounds=int(content.get("battle_rounds") or 12),
                timer_seconds=int(content.get("timer_seconds") or 20),
            )
        except ValidationError as exc:
            await self.send_json({"type": "error", "message": exc.message})
            return
        await self._switch_lobby_group(lobby.public_id)
        await self._broadcast_lobby_snapshot(lobby)
        await self._broadcast_lobby_directory()

    async def _handle_join_lobby(self, content):
        existing = await self._get_active_lobby_for_player(self.player.id)
        if existing is not None:
            await self.send_json({"type": "error", "message": "Leave your current lobby first."})
            return
        lobby_name = str(content.get("lobby_name", "")).strip()
        try:
            lobby = await self._join_lobby(self.player.id, lobby_name)
        except ValidationError as exc:
            await self.send_json({"type": "error", "message": exc.message})
            return
        await self._switch_lobby_group(lobby.public_id)
        await self._broadcast_lobby_snapshot(lobby)
        await self._broadcast_lobby_directory()

    async def _handle_leave_lobby(self):
        current_lobby = await self._get_active_lobby_for_player(self.player.id)
        if current_lobby is None:
            await self.send_json({"type": "error", "message": "You are not in a lobby."})
            return
        if current_lobby.state == LobbyState.IN_MATCH:
            await self.send_json({"type": "error", "message": "You cannot leave while a match is active."})
            return
        result = await self._leave_lobby(self.player.id, current_lobby.public_id)
        await self.channel_layer.group_discard(f"lobby_{current_lobby.public_id}", self.channel_name)
        self.lobby_group_name = None
        if result is None:
            await self.send_json({"type": "lobby.closed"})
            await self._broadcast_lobby_directory()
            return
        remaining_lobby, departed_player_id = result
        if departed_player_id == self.player.id:
            await self.send_json({"type": "lobby.closed"})
        await self._broadcast_lobby_snapshot(remaining_lobby)
        await self._broadcast_lobby_directory()

    async def _handle_start_game(self):
        lobby = await self._get_active_lobby_for_player(self.player.id)
        if lobby is None:
            await self.send_json({"type": "error", "message": "Join a lobby first."})
            return
        if lobby.state == LobbyState.IN_MATCH:
            await self.send_json({"type": "error", "message": "Game is already starting."})
            return
        started_at = timezone.now()
        ends_at = started_at + timedelta(seconds=3)
        countdown_payload = {
            "type": "lobby.start_countdown",
            "lobby_id": str(lobby.public_id),
            "started_at": started_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "message": "Starting game in...",
        }
        await self.send_json(countdown_payload)
        if lobby.guest_id is not None:
            await self.channel_layer.group_send(
                f"player_{lobby.guest_id}",
                {"type": "player.event", "payload": countdown_payload},
            )
        await asyncio.sleep(3)
        try:
            match = await self._start_game_from_lobby(lobby.public_id, self.player.id)
        except ValidationError as exc:
            await self.send_json({"type": "error", "message": exc.message})
            return
        await self._broadcast_lobby_snapshot(await self._get_lobby_by_public_id(lobby.public_id))
        payload = {
            "type": "match.assigned",
            "match_id": str(match.public_id),
            "current_state": match.current_state,
            "map_data": match.map_data,
            "player_ids": [match.player1_id, match.player2_id],
            "players": await self._serialize_match_players(match.public_id),
        }
        for player_id in [match.player1_id, match.player2_id]:
            await self.channel_layer.group_send(
                f"player_{player_id}",
                {"type": "player.event", "payload": payload},
            )
        await self._broadcast_lobby_directory()

    async def _handle_kill_all_lobbies(self):
        snapshot = await self._kill_all_active_lobbies(self.player.id)
        for match_info in snapshot["matches"]:
            clear_match_presence(match_info["match_id"])
            await sync_to_async(publish_game_input, thread_sensitive=False)(
                GameInputMessage(
                    match_id=match_info["match_id"],
                    player_id=self.player.id,
                    round_number=0,
                    action="abandon_match",
                    value="dev_kill_all_lobbies",
                    received_at=timezone.now().isoformat(),
                )
            )
            await self.channel_layer.group_send(
                f"match_{match_info['match_id']}",
                {
                    "type": "match.event",
                    "payload": {
                        "type": "match.finished",
                        "match_id": match_info["match_id"],
                        "current_state": MatchState.FINISHED,
                        "map_data": match_info["map_data"],
                        "leaderboard": match_info["leaderboard"],
                        "reason": "dev_reset",
                        "message": "All active lobbies were cleared from the dev menu.",
                    },
                },
            )

        for player_id in snapshot["player_ids"]:
            await self.channel_layer.group_send(
                f"player_{player_id}",
                {
                    "type": "player.event",
                    "payload": {
                        "type": "lobby.closed",
                        "reason": "dev_reset",
                        "message": "All active lobbies were cleared from the dev menu.",
                    },
                },
            )

        await self._broadcast_lobby_directory()
        await self.send_json(
            {
                "type": "dev.lobbies_cleared",
                "lobby_count": snapshot["lobby_count"],
                "match_count": snapshot["match_count"],
            }
        )

    async def _handle_reset_identity(self):
        current_lobby = await self._get_active_lobby_for_player(self.player.id)
        if current_lobby is not None and current_lobby.state == LobbyState.IN_MATCH:
            await self.send_json({"type": "error", "message": "Leave the active match before resetting your identity."})
            return

        if current_lobby is not None:
            result = await self._leave_lobby(self.player.id, current_lobby.public_id)
            await self.channel_layer.group_discard(f"lobby_{current_lobby.public_id}", self.channel_name)
            self.lobby_group_name = None
            if result is None:
                await self._broadcast_lobby_directory()
            else:
                remaining_lobby, _ = result
                await self._broadcast_lobby_snapshot(remaining_lobby)
                await self._broadcast_lobby_directory()

        previous_player_group_name = self.player_group_name
        self.player = await self._create_guest_player()
        await login(self.scope, self.player)
        await database_sync_to_async(self.scope["session"].save)()
        self.player_group_name = f"player_{self.player.id}"
        await self.channel_layer.group_discard(previous_player_group_name, self.channel_name)
        await self.channel_layer.group_add(self.player_group_name, self.channel_name)
        await self.send_json(
            {
                "type": "profile.reset",
                "player": self._serialize_player(self.player),
                "active_lobbies": await self._list_active_lobbies(),
            }
        )

    async def _switch_lobby_group(self, lobby_public_id):
        new_group_name = f"lobby_{lobby_public_id}"
        if self.lobby_group_name == new_group_name:
            return
        if self.lobby_group_name:
            await self.channel_layer.group_discard(self.lobby_group_name, self.channel_name)
        self.lobby_group_name = new_group_name
        await self.channel_layer.group_add(self.lobby_group_name, self.channel_name)

    async def _broadcast_lobby_snapshot(self, lobby):
        await self.channel_layer.group_send(
            f"lobby_{lobby.public_id}",
            {"type": "lobby.event", "payload": {"type": "lobby.snapshot", "lobby": self._serialize_lobby(lobby)}},
        )

    async def _send_lobby_directory(self):
        await self.send_json({"type": "lobby.directory", "lobbies": await self._list_active_lobbies()})

    async def _broadcast_lobby_directory(self):
        await self.channel_layer.group_send(
            DIRECTORY_GROUP_NAME,
            {
                "type": "directory.event",
                "payload": {"type": "lobby.directory", "lobbies": await self._list_active_lobbies()},
            },
        )

    async def player_event(self, event):
        await self.send_json(event["payload"])

    async def lobby_event(self, event):
        await self.send_json(event["payload"])

    async def directory_event(self, event):
        await self.send_json(event["payload"])

    @database_sync_to_async
    def _get_active_lobby_for_player(self, player_id):
        return (
            Lobby.objects.select_related("host", "guest")
            .filter(Q(host_id=player_id) | Q(guest_id=player_id), state__in=ACTIVE_LOBBY_STATES)
            .order_by("-updated_at")
            .first()
        )

    @database_sync_to_async
    def _get_lobby_by_public_id(self, lobby_public_id):
        return Lobby.objects.select_related("host", "guest").get(public_id=lobby_public_id)

    @database_sync_to_async
    def _list_active_lobbies(self):
        lobbies = (
            Lobby.objects.select_related("host", "guest")
            .filter(state__in=ACTIVE_LOBBY_STATES)
            .order_by("-updated_at")
        )
        return [serialize_directory_lobby(lobby) for lobby in lobbies]

    @database_sync_to_async
    def _kill_all_active_lobbies(self, actor_player_id):
        active_lobbies = list(
            Lobby.objects.select_related("host", "guest")
            .filter(state__in=ACTIVE_LOBBY_STATES)
        )
        active_matches = list(
            Match.objects.select_related("lobby")
            .filter(current_state__in=ACTIVE_MATCH_STATES)
        )

        player_ids: set[int] = set()
        for lobby in active_lobbies:
            if lobby.host_id:
                player_ids.add(lobby.host_id)
            if lobby.guest_id:
                player_ids.add(lobby.guest_id)
            lobby.state = LobbyState.CLOSED
            lobby.save(update_fields=["state", "updated_at"])

        match_payloads = []
        for match in active_matches:
            map_data = match.map_data or {}
            map_data["current_question"] = None
            map_data["question_window"] = None
            map_data["current_selection_prompt"] = None
            map_data["pause_window"] = None
            map_data["pause_message"] = None
            match.map_data = map_data
            match.current_state = MatchState.FINISHED
            match.result_data = {
                "reason": "dev_reset",
                "message": "All active lobbies were cleared from the dev menu.",
                "finished_at": timezone.now().isoformat(),
                "leaderboard": map_data.get("leaderboard") or [],
                "actor_player_id": actor_player_id,
            }
            match.save(update_fields=["map_data", "current_state", "result_data", "updated_at"])
            player_ids.update([match.player1_id, match.player2_id])
            match_payloads.append(
                {
                    "match_id": str(match.public_id),
                    "map_data": map_data,
                    "leaderboard": match.result_data.get("leaderboard") or [],
                }
            )

        return {
            "player_ids": sorted(player_ids),
            "matches": match_payloads,
            "lobby_count": len(active_lobbies),
            "match_count": len(active_matches),
        }

    @database_sync_to_async
    def _update_player_display_name(self, player_id, display_name):
        player = Player.objects.get(pk=player_id)
        player.display_name = display_name
        player.save(update_fields=["display_name", "updated_at"])
        return player

    @database_sync_to_async
    def _create_lobby(self, player_id, lobby_name, categories, question_types, difficulties, test_mode, map_radius, battle_rounds, timer_seconds):
        if not lobby_name:
            raise ValidationError("Lobby name is required.")
        if Lobby.objects.filter(name__iexact=lobby_name, state__in=ACTIVE_LOBBY_STATES).exists():
            raise ValidationError("That lobby name is already in use.")
        if map_radius not in {2, 3, 4}:
            raise ValidationError("Map size must be small, medium, or large.")
        if battle_rounds not in {6, 12, 18}:
            raise ValidationError("Max battle rounds must be quick, average, or long.")
        if timer_seconds not in {10, 20, 30}:
            raise ValidationError("Game speed must be fast, average, or slow.")
        host = Player.objects.get(pk=player_id)
        lobby = Lobby.objects.create(
            name=lobby_name,
            host=host,
            state=LobbyState.OPEN,
            categories=self._sanitize_categories(categories),
            question_types=self._sanitize_question_types(question_types),
            difficulties=self._sanitize_difficulties(difficulties),
            test_mode=test_mode,
            map_radius=map_radius,
            battle_rounds=battle_rounds,
            timer_seconds=timer_seconds,
        )
        return Lobby.objects.select_related("host", "guest").get(pk=lobby.pk)

    @database_sync_to_async
    def _join_lobby(self, player_id, lobby_name):
        if not lobby_name:
            raise ValidationError("Lobby name is required.")
        lobby = (
            Lobby.objects.select_related("host", "guest")
            .filter(name__iexact=lobby_name, state__in=[LobbyState.OPEN, LobbyState.FULL])
            .order_by("-updated_at")
            .first()
        )
        if lobby is None:
            raise ValidationError("Lobby not found.")
        if lobby.host_id == player_id or lobby.guest_id == player_id:
            return lobby
        if lobby.guest_id is not None:
            raise ValidationError("Lobby is already full.")
        lobby.guest_id = player_id
        lobby.state = LobbyState.FULL
        lobby.save(update_fields=["guest", "state", "updated_at"])
        return Lobby.objects.select_related("host", "guest").get(pk=lobby.pk)

    @database_sync_to_async
    def _leave_lobby(self, player_id, lobby_public_id):
        lobby = Lobby.objects.select_related("host", "guest").get(public_id=lobby_public_id)
        if lobby.host_id == player_id:
            if lobby.guest_id is None:
                lobby.state = LobbyState.CLOSED
                lobby.save(update_fields=["state", "updated_at"])
                return None
            lobby.host = lobby.guest
            lobby.guest = None
            lobby.state = LobbyState.OPEN
            lobby.save(update_fields=["host", "guest", "state", "updated_at"])
            return Lobby.objects.select_related("host", "guest").get(pk=lobby.pk), player_id
        if lobby.guest_id == player_id:
            lobby.guest = None
            lobby.state = LobbyState.OPEN
            lobby.save(update_fields=["guest", "state", "updated_at"])
            return Lobby.objects.select_related("host", "guest").get(pk=lobby.pk), player_id
        raise ValidationError("You are not in that lobby.")

    @database_sync_to_async
    def _start_game_from_lobby(self, lobby_public_id, requester_id):
        lobby = Lobby.objects.select_related("host", "guest").get(public_id=lobby_public_id)
        if lobby.host_id != requester_id:
            raise ValidationError("Only the lobby host can start the game.")
        if lobby.guest_id is None:
            raise ValidationError("A second player must join before starting.")
        active_match = (
            Match.objects.filter(lobby=lobby, current_state__in=ACTIVE_MATCH_STATES)
            .order_by("-updated_at")
            .first()
        )
        if active_match is not None:
            return active_match

        settings_data = {
            "categories": lobby.categories,
            "question_types": lobby.question_types,
            "difficulties": lobby.difficulties,
            "test_mode": lobby.test_mode,
            "map_radius": lobby.map_radius,
            "battle_rounds": lobby.battle_rounds,
            "timer_seconds": lobby.timer_seconds,
            "lobby_name": lobby.name,
        }
        player_ids = [lobby.host_id, lobby.guest_id]
        average_elo = int((lobby.host.elo_rating + lobby.guest.elo_rating) / 2)
        match = Match.objects.create(
            lobby=lobby,
            player1=lobby.host,
            player2=lobby.guest,
            current_state=MatchState.QUEUED,
            map_data=self._build_initial_map_data(player_ids, settings_data),
            settings_data=settings_data,
        )
        lobby.state = LobbyState.IN_MATCH
        lobby.save(update_fields=["state", "updated_at"])
        publish_start_game_payload(
            {
                "event": "start_game",
                "match_id": str(match.public_id),
                "player_ids": player_ids,
                "average_elo": average_elo,
                "map_data": match.map_data,
                "settings_data": settings_data,
            }
        )
        return Match.objects.select_related("player1", "player2", "lobby").get(pk=match.pk)

    @database_sync_to_async
    def _serialize_lobby_or_none(self, lobby):
        return None if lobby is None else self._serialize_lobby(lobby)

    @database_sync_to_async
    def _serialize_match_players(self, match_public_id):
        match = Match.objects.select_related("player1", "player2").get(public_id=match_public_id)
        return {
            str(match.player1_id): {"id": match.player1_id, "name": match.player1.game_name},
            str(match.player2_id): {"id": match.player2_id, "name": match.player2.game_name},
        }

    @database_sync_to_async
    def _get_lobby_options(self):
        csv_metadata = build_csv_lobby_metadata()
        return {
            "categories": csv_metadata["categories"],
            "question_types": [
                {"value": QuestionType.MULTIPLE_CHOICE, "label": "Multiple Choice", "info_text": csv_metadata["question_type_info"][QuestionType.MULTIPLE_CHOICE]},
                {"value": QuestionType.TRUE_FALSE, "label": "True or False", "info_text": csv_metadata["question_type_info"][QuestionType.TRUE_FALSE]},
                {"value": QuestionType.ESTIMATE, "label": "Estimate", "info_text": csv_metadata["question_type_info"][QuestionType.ESTIMATE]},
            ],
            "difficulties": [
                {"value": value, "label": label} for value, label in QuestionDifficulty.choices
            ],
            "map_sizes": [
                {"value": 2, "label": "Small - radius 2"},
                {"value": 3, "label": "Medium - radius 3"},
                {"value": 4, "label": "Large - radius 4"},
            ],
            "battle_round_options": [
                {"value": 6, "label": "Quick - 6"},
                {"value": 12, "label": "Average - 12"},
                {"value": 18, "label": "Long - 18"},
            ],
            "game_speed_options": [
                {"value": 10, "label": "Fast - 10s"},
                {"value": 20, "label": "Average - 20s"},
                {"value": 30, "label": "Slow - 30s"},
            ],
        }

    def _serialize_player(self, player):
        return {
            "id": player.id,
            "username": player.username,
            "display_name": player.game_name,
            "elo_rating": player.elo_rating,
            "is_guest": player.is_guest,
            "guest_token": str(player.guest_token),
        }

    def _serialize_lobby(self, lobby):
        return {
            "id": str(lobby.public_id),
            "name": lobby.name,
            "state": lobby.state,
            "host": {"id": lobby.host_id, "name": lobby.host.game_name},
            "guest": {"id": lobby.guest_id, "name": lobby.guest.game_name} if lobby.guest_id else None,
            "settings": {
                "categories": lobby.categories,
                "question_types": lobby.question_types,
                "difficulties": lobby.difficulties,
                "test_mode": lobby.test_mode,
                "map_radius": lobby.map_radius,
                "battle_rounds": lobby.battle_rounds,
                "timer_seconds": lobby.timer_seconds,
            },
            "can_start": bool(lobby.host_id and lobby.guest_id),
        }

    def _serialize_match(self, match):
        return {
            "id": str(match.public_id),
            "current_state": match.current_state,
            "map_data": match.map_data,
            "result_data": match.result_data,
            "player_ids": [match.player1_id, match.player2_id],
            "players": {
                str(match.player1_id): {"id": match.player1_id, "name": match.player1.game_name},
                str(match.player2_id): {"id": match.player2_id, "name": match.player2.game_name},
            },
            "connected_player_ids": current_connected_player_ids(str(match.public_id)),
        }

    def _build_initial_map_data(self, player_ids, settings_data):
        radius = int(settings_data.get("map_radius") or 3)
        cells = build_hex_cells(radius)
        return {
            "map_radius": radius,
            "cells": cells,
            "stats": {
                str(player_id): {
                    "correct_answers": 0,
                    "won_questions": 0,
                    "territories_won": 0,
                    "answers_submitted": 0,
                    "total_response_ms": 0,
                }
                for player_id in player_ids
            },
            "active_player_id": None,
            "valid_targets": [],
            "selection_mode": None,
            "current_selection_prompt": None,
            "current_question": None,
            "question_window": None,
            "pause_window": None,
            "pause_message": None,
            "current_context": None,
            "current_round_number": 0,
            "pick_order": [],
            "battle_rounds_played": 0,
            "round_cap": int(settings_data.get("battle_rounds") or 12),
            "leaderboard": [],
        }

    @staticmethod
    def _sanitize_categories(values):
        allowed = {category for category, category_id in CATEGORIES.items() if category_id is not None}
        return sorted({value for value in values if value in allowed})

    @staticmethod
    def _sanitize_question_types(values):
        allowed = set(QuestionType.values)
        return sorted({value for value in values if value in allowed})

    @staticmethod
    def _sanitize_difficulties(values):
        allowed = set(QuestionDifficulty.values)
        return sorted({value for value in values if value in allowed})


class MatchConsumer(GuestPlayerMixin, AsyncJsonWebsocketConsumer):
    async def connect(self):
        await self.ensure_player()
        self.match_uuid = self.scope["url_route"]["kwargs"]["match_id"]
        self.match_id = str(self.match_uuid)
        try:
            self.match = await self._get_match_for_player(self.match_uuid, self.player.id)
        except PermissionDenied:
            await self.close(code=4403)
            return
        except Match.DoesNotExist:
            await self.close(code=4404)
            return

        self.match_group_name = f"match_{self.match_id}"
        await self.accept()
        await self.channel_layer.group_add(self.match_group_name, self.channel_name)
        presence_changed, connected_player_ids = update_match_presence(self.match_id, self.player.id, connected=True)
        await self.send_json(
            {
                "type": "match.ready",
                "match": await self._serialize_match(self.match_uuid),
                "player": self._serialize_player(self.player),
            }
        )
        if presence_changed:
            await self.channel_layer.group_send(
                self.match_group_name,
                {
                    "type": "match.event",
                    "payload": {
                        "type": "player.presence",
                        "match_id": self.match_id,
                        "player_id": self.player.id,
                        "connected": True,
                        "connected_player_ids": connected_player_ids,
                    },
                },
            )

    async def disconnect(self, close_code):
        if hasattr(self, "match_group_name"):
            await self.channel_layer.group_discard(self.match_group_name, self.channel_name)
            presence_changed, connected_player_ids = update_match_presence(self.match_id, self.player.id, connected=False)
            if presence_changed:
                await self.channel_layer.group_send(
                    self.match_group_name,
                    {
                        "type": "match.event",
                        "payload": {
                            "type": "player.presence",
                            "match_id": self.match_id,
                            "player_id": self.player.id,
                            "connected": False,
                            "connected_player_ids": connected_player_ids,
                        },
                    },
                )
            abandon_payload = await self._abandon_match(self.match_uuid, self.player.id)
            if abandon_payload is not None:
                await self._stop_match_worker("disconnect")
                clear_match_presence(self.match_id)
                await self.channel_layer.group_send(
                    self.match_group_name,
                    {
                        "type": "match.event",
                        "payload": abandon_payload["match_payload"],
                    },
                )
                for target_player_id in abandon_payload["player_ids"]:
                    await self.channel_layer.group_send(
                        f"player_{target_player_id}",
                        {
                            "type": "player.event",
                            "payload": abandon_payload["lobby_payload"],
                        },
                    )
                await self._broadcast_lobby_directory()

    async def receive_json(self, content, **kwargs):
        action = content.get("action")
        if action == "submit_answer":
            await self._handle_input(content, input_action="submit_answer", value=str(content.get("answer", "")).strip())
            return
        if action == "select_territory":
            await self._handle_input(content, input_action="select_territory", value=str(content.get("territory_id", "")).strip())
            return
        if action == "leave_match":
            payload = await self._abandon_match(self.match_uuid, self.player.id)
            if payload is not None:
                await self._stop_match_worker("leave_match")
                clear_match_presence(self.match_id)
                await self.channel_layer.group_send(
                    self.match_group_name,
                    {
                        "type": "match.event",
                        "payload": payload["match_payload"],
                    },
                )
                for target_player_id in payload["player_ids"]:
                    await self.channel_layer.group_send(
                        f"player_{target_player_id}",
                        {
                            "type": "player.event",
                            "payload": payload["lobby_payload"],
                        },
                    )
                await self._broadcast_lobby_directory()
            await self.send_json({"type": "match.left", "match_id": self.match_id})
            await self.close()
            return
        if action == "ping":
            await self.send_json({"type": "pong"})
            return
        await self.send_json({"type": "error", "message": "Unsupported action."})

    async def _handle_input(self, content, input_action: str, value: str):
        payload = GameInputMessage(
            match_id=self.match_id,
            player_id=self.player.id,
            round_number=int(content.get("round_number", 1)),
            action=input_action,
            value=value,
            received_at=timezone.now().isoformat(),
        )
        await sync_to_async(publish_game_input, thread_sensitive=False)(payload)
        await self.send_json(
            {
                "type": "input.submitted",
                "match_id": payload.match_id,
                "player_id": payload.player_id,
                "round_number": payload.round_number,
                "action": payload.action,
                "received_at": payload.received_at,
            }
        )

    async def match_event(self, event):
        await self.send_json(event["payload"])

    async def _broadcast_lobby_directory(self):
        await self.channel_layer.group_send(
            DIRECTORY_GROUP_NAME,
            {
                "type": "directory.event",
                "payload": {"type": "lobby.directory", "lobbies": await self._list_active_lobbies()},
            },
        )

    @database_sync_to_async
    def _list_active_lobbies(self):
        lobbies = (
            Lobby.objects.select_related("host", "guest")
            .filter(state__in=ACTIVE_LOBBY_STATES)
            .order_by("-updated_at")
        )
        return [serialize_directory_lobby(lobby) for lobby in lobbies]

    @database_sync_to_async
    def _get_match_for_player(self, match_id, player_id):
        match = Match.objects.get(public_id=match_id)
        if player_id not in {match.player1_id, match.player2_id}:
            raise PermissionDenied()
        return match

    @database_sync_to_async
    def _serialize_match(self, match_id):
        match = Match.objects.select_related("player1", "player2", "lobby").get(public_id=match_id)
        return {
            "id": str(match.public_id),
            "current_state": match.current_state,
            "map_data": match.map_data,
            "result_data": match.result_data,
            "player_ids": [match.player1_id, match.player2_id],
            "players": {
                str(match.player1_id): {"id": match.player1_id, "name": match.player1.game_name},
                str(match.player2_id): {"id": match.player2_id, "name": match.player2.game_name},
            },
            "settings": match.settings_data,
            "lobby_name": match.lobby.name if match.lobby_id else None,
            "connected_player_ids": current_connected_player_ids(str(match.public_id)),
        }

    @database_sync_to_async
    def _abandon_match(self, match_id, departing_player_id):
        match = Match.objects.select_related("player1", "player2", "lobby", "lobby__host", "lobby__guest").get(public_id=match_id)
        departing_player = match.player1 if match.player1_id == departing_player_id else match.player2
        player_ids = [match.player1_id, match.player2_id]
        message = f"{departing_player.game_name} left the match."

        active_match = match.current_state in ACTIVE_MATCH_STATES
        active_lobby = match.lobby is not None and match.lobby.state in ACTIVE_LOBBY_STATES
        if not active_match and not active_lobby:
            return None

        if active_match:
            map_data = match.map_data or {}
            map_data["current_question"] = None
            map_data["question_window"] = None
            map_data["current_selection_prompt"] = None
            map_data["pause_window"] = None
            map_data["pause_message"] = None
            match.map_data = map_data
            match.current_state = MatchState.FINISHED
            match.result_data = {
                "reason": "player_left",
                "departing_player_id": departing_player_id,
                "message": message,
                "finished_at": timezone.now().isoformat(),
                "leaderboard": map_data.get("leaderboard") or [],
            }
            match.save(update_fields=["map_data", "current_state", "result_data", "updated_at"])

        if active_lobby:
            match.lobby.state = LobbyState.CLOSED
            match.lobby.save(update_fields=["state", "updated_at"])

        return {
            "player_ids": player_ids,
            "match_payload": {
                "type": "match.finished",
                "match_id": str(match.public_id),
                "current_state": MatchState.FINISHED,
                "map_data": match.map_data,
                "leaderboard": (match.result_data or {}).get("leaderboard") or [],
                "reason": "player_left",
                "message": message,
                "departing_player_id": departing_player_id,
                "countdown_seconds": 3,
            },
            "lobby_payload": {
                "type": "lobby.closed",
                "reason": "player_left",
                "message": message,
                "countdown_seconds": 3,
            },
        }

    async def _stop_match_worker(self, reason):
        payload = GameInputMessage(
            match_id=self.match_id,
            player_id=self.player.id,
            round_number=0,
            action="abandon_match",
            value=reason,
            received_at=timezone.now().isoformat(),
        )
        await sync_to_async(publish_game_input, thread_sensitive=False)(payload)

    def _serialize_player(self, player):
        return {
            "id": player.id,
            "username": player.username,
            "display_name": player.game_name,
            "elo_rating": player.elo_rating,
            "is_guest": player.is_guest,
            "guest_token": str(player.guest_token),
        }
