from django.core.management.base import BaseCommand

from apps.events.models import Pair


class Command(BaseCommand):
    help = "Aggregates and counts Direct vs TURN path metrics for pairs."

    def handle(self, *args, **options):
        direct_count = Pair.objects.filter(path_type=Pair.PathType.DIRECT).count()
        turn_count = Pair.objects.filter(path_type=Pair.PathType.TURN).count()
        total = direct_count + turn_count

        direct_share = (direct_count / total * 100) if total > 0 else 0
        turn_share = (turn_count / total * 100) if total > 0 else 0

        self.stdout.write(
            self.style.SUCCESS(f"Direct Paths: {direct_count} ({direct_share:.1f}%)")
        )
        self.stdout.write(
            self.style.SUCCESS(f"TURN Paths: {turn_count} ({turn_share:.1f}%)")
        )
