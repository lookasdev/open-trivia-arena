import atexit
import json
import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass

import pika
from django.conf import settings


logger = logging.getLogger(__name__)


MATCHMAKING_QUEUE = "matchmaking"
START_GAME_EXCHANGE = "start_game"
START_GAME_QUEUE = "game_start"
START_GAME_ROUTING_KEY = "game.start"
GAME_INPUTS_QUEUE = "game_inputs"
GAME_EVENTS_EXCHANGE = "game_events"
GAME_EVENTS_QUEUE = "game_events_relay"
GAME_EVENTS_ROUTING_KEY = "match.event"


def get_connection() -> pika.BlockingConnection:
    return pika.BlockingConnection(pika.URLParameters(settings.RABBITMQ_URL))


def declare_topology(channel: pika.adapters.blocking_connection.BlockingChannel) -> None:
    channel.queue_declare(queue=MATCHMAKING_QUEUE, durable=True)
    channel.exchange_declare(exchange=START_GAME_EXCHANGE, exchange_type="direct", durable=True)
    channel.queue_declare(queue=START_GAME_QUEUE, durable=True)
    channel.queue_bind(queue=START_GAME_QUEUE, exchange=START_GAME_EXCHANGE, routing_key=START_GAME_ROUTING_KEY)
    channel.queue_declare(queue=GAME_INPUTS_QUEUE, durable=True)
    channel.exchange_declare(exchange=GAME_EVENTS_EXCHANGE, exchange_type="topic", durable=True)
    channel.queue_declare(queue=GAME_EVENTS_QUEUE, durable=True)
    channel.queue_bind(queue=GAME_EVENTS_QUEUE, exchange=GAME_EVENTS_EXCHANGE, routing_key="match.*")
    channel.queue_bind(queue=GAME_EVENTS_QUEUE, exchange=GAME_EVENTS_EXCHANGE, routing_key="player.*")


@dataclass(frozen=True)
class MatchmakingMessage:
    player_id: int
    elo_rating: int
    username: str
    is_guest: bool
    requested_at: str


@dataclass(frozen=True)
class GameInputMessage:
    match_id: str
    player_id: int
    round_number: int
    action: str
    value: str
    received_at: str


class RealtimePublisher:
    def __init__(self) -> None:
        self._messages: queue.Queue[tuple[str, str, bytes] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def publish_json(self, *, exchange: str, routing_key: str, payload: dict) -> None:
        self._ensure_started()
        self._messages.put((exchange, routing_key, json.dumps(payload).encode("utf-8")))

    def close(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._thread = None
        self._messages.put(None)
        thread.join(timeout=5)

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="realtime-rabbitmq-publisher", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        connection = None
        channel = None
        try:
            while True:
                message = self._messages.get()
                if message is None:
                    self._messages.task_done()
                    break
                exchange, routing_key, body = message
                while True:
                    try:
                        if connection is None or connection.is_closed or channel is None or channel.is_closed:
                            connection = get_connection()
                            channel = connection.channel()
                            declare_topology(channel)
                        channel.basic_publish(
                            exchange=exchange,
                            routing_key=routing_key,
                            body=body,
                            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
                        )
                        break
                    except Exception:
                        logger.exception("Realtime RabbitMQ publish failed. Reconnecting publisher channel.")
                        if connection is not None and not connection.is_closed:
                            try:
                                connection.close()
                            except Exception:
                                logger.debug("Failed to close realtime RabbitMQ connection cleanly.", exc_info=True)
                        connection = None
                        channel = None
                        time.sleep(1)
                self._messages.task_done()
        finally:
            if connection is not None and not connection.is_closed:
                try:
                    connection.close()
                except Exception:
                    logger.debug("Failed to close realtime RabbitMQ publisher connection cleanly.", exc_info=True)


_REALTIME_PUBLISHER = RealtimePublisher()
atexit.register(_REALTIME_PUBLISHER.close)


def publish_matchmaking_message(message: MatchmakingMessage) -> None:
    _REALTIME_PUBLISHER.publish_json(exchange="", routing_key=MATCHMAKING_QUEUE, payload=asdict(message))


def publish_game_input(message: GameInputMessage) -> None:
    _REALTIME_PUBLISHER.publish_json(exchange="", routing_key=GAME_INPUTS_QUEUE, payload=asdict(message))


def publish_start_game_payload(payload: dict) -> None:
    _REALTIME_PUBLISHER.publish_json(exchange=START_GAME_EXCHANGE, routing_key=START_GAME_ROUTING_KEY, payload=payload)
