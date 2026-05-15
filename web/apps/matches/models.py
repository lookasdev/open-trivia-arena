import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class LobbyState(models.TextChoices):
    OPEN = "open", "Open"
    FULL = "full", "Full"
    IN_MATCH = "in_match", "In Match"
    CLOSED = "closed", "Closed"


class MatchState(models.TextChoices):
    QUEUED = "queued", "Queued"
    BASE_SELECTION = "base_selection", "Base Selection"
    EXPANSION = "expansion", "Expansion"
    DUEL = "duel", "Duel"
    BASE_SIEGE = "base_siege", "Base Siege"
    FINISHED = "finished", "Finished"


class Lobby(models.Model):
    public_id = models.UUIDField(unique=True, editable=False, null=True, blank=True)
    name = models.CharField(max_length=64)
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="hosted_lobbies",
    )
    guest = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="joined_lobbies",
        null=True,
        blank=True,
    )
    state = models.CharField(max_length=16, choices=LobbyState.choices, default=LobbyState.OPEN)
    categories = models.JSONField(default=list, blank=True)
    question_types = models.JSONField(default=list, blank=True)
    difficulties = models.JSONField(default=list, blank=True)
    test_mode = models.BooleanField(default=False)
    map_radius = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(2), MaxValueValidator(5)],
    )
    battle_rounds = models.PositiveSmallIntegerField(
        default=12,
        validators=[MinValueValidator(1), MaxValueValidator(50)],
    )
    timer_seconds = models.PositiveSmallIntegerField(
        default=20,
        validators=[MinValueValidator(10), MaxValueValidator(30)],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self) -> None:
        super().clean()
        if self.host_id and self.host_id == self.guest_id:
            raise ValidationError("A lobby host cannot also occupy the guest slot.")

    def save(self, *args, **kwargs):
        if self.public_id is None:
            self.public_id = uuid.uuid4()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class Match(models.Model):
    public_id = models.UUIDField(unique=True, editable=False, null=True, blank=True)
    lobby = models.ForeignKey(
        "matches.Lobby",
        on_delete=models.SET_NULL,
        related_name="matches",
        null=True,
        blank=True,
    )
    player1 = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="matches_as_player1",
    )
    player2 = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="matches_as_player2",
    )
    current_state = models.CharField(
        max_length=32,
        choices=MatchState.choices,
        default=MatchState.QUEUED,
    )
    map_data = models.JSONField(default=dict, blank=True)
    settings_data = models.JSONField(default=dict, blank=True)
    result_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self) -> None:
        super().clean()
        if self.player1_id and self.player1_id == self.player2_id:
            raise ValidationError("A player cannot be matched against themselves.")

    def save(self, *args, **kwargs):
        if self.public_id is None:
            self.public_id = uuid.uuid4()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Match {self.public_id}: {self.player1} vs {self.player2}"
