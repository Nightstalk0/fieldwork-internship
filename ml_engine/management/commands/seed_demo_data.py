from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from portal.models import CompanyProfile, InternProfile, Posting


class Command(BaseCommand):
    help = "Create a small idempotent demo organization, intern, and published opportunity."

    def add_arguments(self, parser):
        parser.add_argument("--password", default=None, help="Set a shared password for the demo accounts.")

    @transaction.atomic
    def handle(self, *args, **options):
        user_model = get_user_model()
        password = options["password"]
        company_user, company_created = user_model.objects.get_or_create(
            username="demo-company",
            defaults={"email": "company@example.test", "first_name": "Demo", "last_name": "Company", "role": "company"},
        )
        intern_user, intern_created = user_model.objects.get_or_create(
            username="demo-intern",
            defaults={"email": "intern@example.test", "first_name": "Demo", "last_name": "Intern", "role": "intern"},
        )
        for user, created in ((company_user, company_created), (intern_user, intern_created)):
            if created and password:
                user.set_password(password)
            elif created:
                user.set_unusable_password()
            if created:
                user.save()

        company, _ = CompanyProfile.objects.update_or_create(
            user=company_user,
            defaults={"organization": "Northstar Studio", "website": "https://example.test", "verified": True},
        )
        InternProfile.objects.get_or_create(user=intern_user, defaults={"university": "Example University", "course": "Information Systems"})
        Posting.objects.get_or_create(
            company=company,
            title="Junior Data Operations Intern",
            defaults={
                "description": "Support reporting, data quality checks, and process documentation.",
                "location": "Hybrid",
                "remote": True,
                "openings": 2,
                "status": Posting.Status.PUBLISHED,
            },
        )
        self.stdout.write(self.style.SUCCESS("Demo company, intern, and opportunity are ready."))
        if not password:
            self.stdout.write("Demo accounts have unusable passwords. Re-run with --password to enable login.")