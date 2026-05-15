import json
import logging
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from worker.game_thread import GameThread, SubmittedInput  # noqa: E402
from worker.mq import GAME_INPUTS_QUEUE, START_GAME_QUEUE, declare_topology, get_connection  # noqa: E402


logger = logging.getLogger(__name__)


class GameWorkerService:
    def __init__(self) -> None:
        self.active_games: dict[str, GameThread] = {}

    def run(self) -> None:
        connection = get_connection()
        channel = connection.channel()
        declare_topology(channel)
        channel.basic_qos(prefetch_count=1)
        channel.basic_consume(queue=START_GAME_QUEUE, on_message_callback=self._on_start_game)
        channel.basic_consume(queue=GAME_INPUTS_QUEUE, on_message_callback=self._on_input)
        logger.info("Game worker listening on '%s' and '%s'", START_GAME_QUEUE, GAME_INPUTS_QUEUE)
        try:
            channel.start_consuming()
        finally:
            connection.close()

    def _on_start_game(self, channel, method, properties, body) -> None:
        payload = json.loads(body.decode("utf-8"))
        match_id = payload["match_id"]
        if match_id not in self.active_games:
            thread = GameThread(
                match_id=match_id,
                player_ids=payload["player_ids"],
                average_elo=payload["average_elo"],
                settings_data=payload.get("settings_data") or {},
                timer_seconds=int((payload.get("settings_data") or {}).get("timer_seconds") or os.getenv("GAME_TIMER_SECONDS", "20")),
            )
            self.active_games[match_id] = thread
            thread.start()
            logger.info("Started game thread for match %s", match_id)
        channel.basic_ack(delivery_tag=method.delivery_tag)

    def _on_input(self, channel, method, properties, body) -> None:
        payload = json.loads(body.decode("utf-8"))
        match_id = payload["match_id"]
        thread = self.active_games.get(match_id)
        if thread is not None:
            if payload["action"] == "abandon_match":
                thread.stop()
                self.active_games.pop(match_id, None)
                channel.basic_ack(delivery_tag=method.delivery_tag)
                return
            thread.submit_input(
                SubmittedInput(
                    player_id=payload["player_id"],
                    round_number=payload["round_number"],
                    action=payload["action"],
                    value=payload["value"],
                    received_at=payload["received_at"],
                )
            )
        channel.basic_ack(delivery_tag=method.delivery_tag)