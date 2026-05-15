from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


async def relay_game_event_async(payload: dict) -> None:
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    match_id = payload.get("match_id")
    player_ids = payload.get("player_ids", [])

    if match_id is not None:
        await channel_layer.group_send(
            f"match_{match_id}",
            {"type": "match.event", "payload": payload},
        )

    for player_id in player_ids:
        await channel_layer.group_send(
            f"player_{player_id}",
            {"type": "player.event", "payload": payload},
        )


def relay_game_event(payload: dict) -> None:
    async_to_sync(relay_game_event_async)(payload)
