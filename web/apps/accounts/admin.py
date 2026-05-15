from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Player


@admin.register(Player)
class PlayerAdmin(UserAdmin):
    list_display = ("username", "email", "elo_rating", "is_guest", "is_staff")
    list_filter = ("is_guest", "is_staff", "is_superuser", "is_active")
    fieldsets = UserAdmin.fieldsets + (("Game Fields", {"fields": ("elo_rating", "is_guest", "guest_token")}),)
    readonly_fields = ("guest_token",)
