from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Player
from apps.matches.models import Lobby, Match


class Command(BaseCommand):
    help = "Delete demo state so the app can boot from a clean slate."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep-guests",
            action="store_true",
            help="Keep guest players and only clear lobbies and matches.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be deleted without changing the database.",
        )

    def handle(self, *args, **options):
        keep_guests = options["keep_guests"]
        dry_run = options["dry_run"]

        match_count = Match.objects.count()
        lobby_count = Lobby.objects.count()
        guest_count = 0 if keep_guests else Player.objects.filter(is_guest=True).count()

        message = f"{match_count} matches, {lobby_count} lobbies"
        if not keep_guests:
            message += f", {guest_count} guest players"

        if dry_run:
            self.stdout.write(self.style.WARNING(f"Dry run: would delete {message}."))
            return

        with transaction.atomic():
            Match.objects.all().delete()
            Lobby.objects.all().delete()
            if not keep_guests:
                Player.objects.filter(is_guest=True).delete()

        self.stdout.write(self.style.SUCCESS(f"Deleted {message}."))
