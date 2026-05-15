import json

from django.core.management.base import BaseCommand

from apps.realtime.bridge import relay_game_event
from apps.realtime.rabbitmq import GAME_EVENTS_QUEUE, declare_topology, get_connection


class Command(BaseCommand):
    help = "Consume RabbitMQ game events and relay them into Channels groups."

    def handle(self, *args, **options):
        connection = get_connection()
        channel = connection.channel()
        declare_topology(channel)
        channel.basic_qos(prefetch_count=1)

        def on_message(channel, method, properties, body):
            payload = json.loads(body.decode("utf-8"))
            relay_game_event(payload)
            channel.basic_ack(delivery_tag=method.delivery_tag)

        channel.basic_consume(queue=GAME_EVENTS_QUEUE, on_message_callback=on_message)
        self.stdout.write(self.style.SUCCESS("Relaying game events from RabbitMQ to Channels"))
        try:
            channel.start_consuming()
        finally:
            connection.close()