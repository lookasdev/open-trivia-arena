import atexit
import json
import logging
import os
import queue
import threading
import time

import pika


MATCHMAKING_QUEUE = "matchmaking"
START_GAME_EXCHANGE = "start_game"
START_GAME_QUEUE = "game_start"
START_GAME_ROUTING_KEY = "game.start"
GAME_INPUTS_QUEUE = "game_inputs"
GAME_EVENTS_EXCHANGE = "game_events"
GAME_EVENTS_QUEUE = "game_events_relay"


logger = logging.getLogger(__name__)


def get_connection() -> pika.BlockingConnection:
    rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    return pika.BlockingConnection(pika.URLParameters(rabbitmq_url))


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


def publish_start_game(channel: pika.adapters.blocking_connection.BlockingChannel, payload: dict) -> None:
    channel.basic_publish(
        exchange=START_GAME_EXCHANGE,
        routing_key=START_GAME_ROUTING_KEY,
        body=json.dumps(payload).encode("utf-8"),
        properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
    )


class WorkerPublisher:
    def __init__(self) -> None:
        self._messages: queue.Queue[tuple[str, str, bytes] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def publish(self, *, exchange: str, routing_key: str, payload: dict) -> None:
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
            self._thread = threading.Thread(target=self._run, name="worker-rabbitmq-publisher", daemon=True)
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
                        logger.exception("Worker RabbitMQ publish failed. Reconnecting publisher channel.")
                        if connection is not None and not connection.is_closed:
                            try:
                                connection.close()
                            except Exception:
                                logger.debug("Failed to close worker RabbitMQ connection cleanly.", exc_info=True)
                        connection = None
                        channel = None
                        time.sleep(1)
                self._messages.task_done()
        finally:
            if connection is not None and not connection.is_closed:
                try:
                    connection.close()
                except Exception:
                    logger.debug("Failed to close worker RabbitMQ publisher connection cleanly.", exc_info=True)


_WORKER_PUBLISHER = WorkerPublisher()
atexit.register(_WORKER_PUBLISHER.close)


def publish_start_game_payload(payload: dict) -> None:
    _WORKER_PUBLISHER.publish(exchange=START_GAME_EXCHANGE, routing_key=START_GAME_ROUTING_KEY, payload=payload)


def publish_game_event(payload: dict, routing_key: str = "match.event") -> None:
    _WORKER_PUBLISHER.publish(exchange=GAME_EVENTS_EXCHANGE, routing_key=routing_key, payload=payload)
