from django.contrib import admin

from .models import Match


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ("public_id", "player1", "player2", "current_state", "created_at")
    list_filter = ("current_state",)
    search_fields = ("public_id", "player1__username", "player2__username")

