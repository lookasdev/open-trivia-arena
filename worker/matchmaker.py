import json
import logging
import os
from dataclasses import dataclass

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from apps.matches.models import Match, MatchState  # noqa: E402
from apps.accounts.models import Player  # noqa: E402
from worker.mq import MATCHMAKING_QUEUE, declare_topology, get_connection, publish_game_event, publish_start_game  # noqa: E402


logger = logging.getLogger(__name__)


@dataclass
class WaitingPlayer:
    player_id: int
    elo_rating: int
    username: str
    is_guest: bool
    requested_at: str


class MatchmakerService:
    def __init__(self) -> None:
        self.waiting_players: list[WaitingPlayer] = []

    def run(self) -> None:
        connection = get_connection()
        channel = connection.channel()
        declare_topology(channel)
        channel.basic_qos(prefetch_count=1)
        channel.basic_consume(queue=MATCHMAKING_QUEUE, on_message_callback=self._on_message)
        logger.info("Matchmaker listening on RabbitMQ queue '%s'", MATCHMAKING_QUEUE)
        try:
            channel.start_consuming()
        finally:
            connection.close()

    def _on_message(self, channel, method, properties, body) -> None:
        payload = json.loads(body.decode("utf-8"))
        waiting_player = WaitingPlayer(**payload)
        self._enqueue(waiting_player)
        self._try_match(channel)
        channel.basic_ack(delivery_tag=method.delivery_tag)

    def _enqueue(self, waiting_player: WaitingPlayer) -> None:
        self.waiting_players = [player for player in self.waiting_players if player.player_id != waiting_player.player_id]
        self.waiting_players.append(waiting_player)
        logger.info("Queued player %s with ELO %s", waiting_player.player_id, waiting_player.elo_rating)

    def _try_match(self, channel) -> None:
        if len(self.waiting_players) < 2:
            return

        player_a, player_b = self._closest_pair()
        self.waiting_players = [
            player
            for player in self.waiting_players
            if player.player_id not in {player_a.player_id, player_b.player_id}
        ]

        match = self._create_match(player_a.player_id, player_b.player_id)
        payload = {
            "event": "start_game",
            "match_id": str(match.public_id),
            "player_ids": [player_a.player_id, player_b.player_id],
            "average_elo": int((player_a.elo_rating + player_b.elo_rating) / 2),
            "map_data": match.map_data,
        }
        publish_start_game(channel, payload)
        publish_game_event(
            {
                "type": "match.assigned",
                "match_id": str(match.public_id),
                "player_ids": [player_a.player_id, player_b.player_id],
                "current_state": match.current_state,
                "map_data": match.map_data,
            },
            routing_key="player.assigned",
        )
        logger.info("Matched players %s and %s into match %s", player_a.player_id, player_b.player_id, match.public_id)

    def _closest_pair(self) -> tuple[WaitingPlayer, WaitingPlayer]:
        ordered = sorted(self.waiting_players, key=lambda player: (player.elo_rating, player.requested_at))
        best_pair = (ordered[0], ordered[1])
        best_diff = abs(ordered[0].elo_rating - ordered[1].elo_rating)
        for left, right in zip(ordered, ordered[1:]):
            diff = abs(left.elo_rating - right.elo_rating)
            if diff < best_diff:
                best_pair = (left, right)
                best_diff = diff
        return best_pair

    def _create_match(self, player1_id: int, player2_id: int) -> Match:
        player1 = Player.objects.get(pk=player1_id)
        player2 = Player.objects.get(pk=player2_id)
        match = Match.objects.create(
            player1=player1,
            player2=player2,
            current_state=MatchState.QUEUED,
            map_data=self._build_initial_map_data(player1_id, player2_id),
        )
        return match

    def _build_initial_map_data(self, player1_id: int, player2_id: int) -> dict:
        territories = []
        for index in range(12):
            owner = player1_id if index < 6 else player2_id
            base_for_owner = (index == 0 and owner == player1_id) or (index == 6 and owner == player2_id)
            territories.append(
                {
                    "territory_id": index + 1,
                    "owner_id": owner,
                    "hp": 3 if base_for_owner else 1,
                    "is_base": base_for_owner,
                }
            )
        return {"territories": territories}
