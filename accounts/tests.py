from django.test import TestCase
from django.urls import reverse

from .models import User


class AccountFlowTests(TestCase):
    def test_login_page_has_accessible_password_visibility_toggle(self):
        response = self.client.get(reverse("accounts:login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="id_password"')
        self.assertContains(response, 'aria-label="Show password"')
        self.assertContains(response, 'class="password-visibility-toggle"')
        self.assertContains(response, 'passwordInput.type = revealPassword ? "text" : "password"')

    def test_registration_uses_account_type_tabs_without_role_dropdown(self):
        response = self.client.get(reverse("accounts:register"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-role="intern"')
        self.assertContains(response, 'data-role="company"')
        self.assertContains(response, '<input type="hidden" name="role"')
        self.assertContains(response, 'value="intern"')

    def test_registration_without_hidden_role_defaults_to_intern(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "new-intern",
                "first_name": "New",
                "last_name": "Intern",
                "email": "new@example.test",
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
        )

        self.assertRedirects(response, reverse("accounts:login"))
        user = User.objects.get(username="new-intern")
        self.assertEqual(user.role, User.Role.INTERN)
        self.assertTrue(hasattr(user, "intern_profile"))

    def test_admin_can_view_registered_accounts_without_password_data(self):
        User.objects.create_user(username="registered-intern", password="Safe-example-Password-927!")
        User.objects.create_user(
            username="registered-company",
            password="Safe-example-Password-927!",
            role=User.Role.COMPANY,
        )
        admin_user = User.objects.create_superuser(
            username="site-admin",
            email="admin@example.test",
            password="Safe-example-Password-927!",
        )
        self.client.force_login(admin_user)

        response = self.client.get(reverse("admin:accounts_user_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "registered-intern")
        self.assertContains(response, "registered-company")
        self.assertNotContains(response, "pbkdf2_sha256")

    def test_company_registration_creates_company_profile(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "new-company",
                "first_name": "North",
                "last_name": "Studio",
                "email": "north@example.test",
                "phone": "555-0100",
                "role": User.Role.COMPANY,
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
        )
        self.assertRedirects(response, reverse("accounts:login"))
        user = User.objects.get(username="new-company")
        self.assertEqual(user.company_profile.organization, "North Studio")

    def test_intern_cannot_open_coordinator_routes(self):
        user = User.objects.create_user(username="intern", password="Safe-example-Password-927!")
        self.client.force_login(user)
        response = self.client.get(reverse("portal:coordinator_dashboard"))
        self.assertEqual(response.status_code, 403)