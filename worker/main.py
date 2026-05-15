import logging
import os
import time

from worker.game_worker import GameWorkerService
from worker.matchmaker import MatchmakerService


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    worker_role = os.getenv("WORKER_ROLE", "matchmaker")
    logger.info("Worker booted")
    logger.info("RabbitMQ endpoint configured as %s", rabbitmq_url)
    logger.info("Worker role is %s", worker_role)

    if worker_role == "matchmaker":
        MatchmakerService().run()
        return

    if worker_role == "game_worker":
        GameWorkerService().run()
        return

    while True:
        time.sleep(30)


if __name__ == "__main__":
    main()