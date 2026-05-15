import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class Player(AbstractUser):
    display_name = models.CharField(max_length=32, blank=True)
    elo_rating = models.PositiveIntegerField(default=1200)
    is_guest = models.BooleanField(default=False)
    guest_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["username"]

    @property
    def game_name(self) -> str:
        return self.display_name or self.username

    def __str__(self) -> str:
        return self.game_name
