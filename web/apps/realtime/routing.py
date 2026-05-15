from django.urls import path

from .consumers import MatchConsumer, MatchmakingConsumer


websocket_urlpatterns = [
	path("ws/matchmaking/", MatchmakingConsumer.as_asgi()),
	path("ws/matches/<uuid:match_id>/", MatchConsumer.as_asgi()),
]
