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
        self.assertEqual(response.content.count(b'data-password-icon'), 1)
        self.assertEqual(response.content.count(b'class="password-visibility-toggle"'), 1)
        self.assertContains(response, "data-login-password-feedback")
        self.assertContains(response, 'passwordInput.type = revealPassword ? "text" : "password"')

    def test_registration_uses_account_type_tabs_without_role_dropdown(self):
        response = self.client.get(reverse("accounts:register"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-role="intern"')
        self.assertContains(response, 'data-role="company"')
        self.assertContains(response, 'name="student_id"')
        self.assertContains(response, 'name="university"')
        self.assertContains(response, 'name="course"')
        self.assertContains(response, 'name="year_level"')
        self.assertContains(response, 'name="placement_type"')
        self.assertContains(response, 'name="company_name"')
        self.assertContains(response, 'name="company_address"')
        self.assertContains(response, 'name="barangay"')
        self.assertContains(response, 'name="city_municipality"')
        self.assertContains(response, 'name="province"')
        self.assertContains(response, 'name="postal_code"')
        self.assertContains(response, 'name="industry_type"')
        self.assertContains(response, 'name="contact_person_name"')
        self.assertContains(response, 'name="contact_person_position"')
        self.assertContains(response, 'name="official_email"')
        self.assertContains(response, 'name="contact_number"')
        self.assertContains(response, 'name="company_website"')
        self.assertContains(response, 'name="moa_reference_number"')
        self.assertContains(response, '<input type="hidden" name="role"')
        self.assertContains(response, 'value="intern"')
        self.assertContains(response, 'aria-controls="id_password1"')
        self.assertContains(response, 'aria-controls="id_password2"')
        self.assertContains(response, '<select name="year_level"')
        self.assertContains(response, '<option value="1">1st Year</option>')
        self.assertContains(response, '<option value="2">2nd Year</option>')
        self.assertContains(response, '<option value="3">3rd Year</option>')
        self.assertContains(response, '<option value="4">4th Year</option>')
        self.assertEqual(response.content.count(b'data-password-icon'), 2)
        self.assertNotContains(response, "data-password-visible-icon")
        self.assertContains(response, "Passwords do not match.")

    def test_registration_rejects_year_level_outside_the_four_choices(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "invalid-year-intern",
                "first_name": "New",
                "last_name": "Intern",
                "email": "invalid-year@example.test",
                "student_id": "STU-REG-004",
                "university": "Example University",
                "course": "Computer Science",
                "year_level": "5",
                "placement_type": "platform",
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("year_level", response.context["form"].errors)
        self.assertFalse(User.objects.filter(username="invalid-year-intern").exists())

    def test_registration_password_toggles_preserve_new_password_inputs(self):
        response = self.client.get(reverse("accounts:register"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-password-toggle')
        self.assertContains(response, 'aria-controls="id_password1"')
        self.assertContains(response, 'aria-controls="id_password2"')

    def test_registration_without_hidden_role_defaults_to_intern(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "new-intern",
                "first_name": "New",
                "last_name": "Intern",
                "email": "new@example.test",
                "student_id": "STU-REG-001",
                "university": "Example University",
                "course": "Computer Science",
                "year_level": "3",
                "placement_type": "platform",
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
        )

        self.assertRedirects(response, reverse("accounts:login"))
        user = User.objects.get(username="new-intern")
        self.assertEqual(user.role, User.Role.INTERN)
        self.assertEqual(user.intern_profile.student_id, "STU-REG-001")
        self.assertEqual(user.intern_profile.university, "Example University")
        self.assertEqual(user.intern_profile.course, "Computer Science")
        self.assertEqual(user.intern_profile.year_level, 3)
        self.assertEqual(user.intern_profile.placement_type, "platform")

    def test_intern_registration_requires_academic_profile(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "incomplete-intern",
                "first_name": "New",
                "last_name": "Intern",
                "email": "incomplete@example.test",
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="incomplete-intern").exists())

    def test_registration_validates_phone_and_allows_common_name_punctuation(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "valid-phone-intern",
                "first_name": "Anne-Marie",
                "last_name": "O’Neil",
                "email": "valid-phone@example.test",
                "phone": "0917 123 4567",
                "student_id": "STU-REG-002",
                "university": "Example University",
                "course": "Computer Science",
                "year_level": "3",
                "placement_type": "platform",
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
        )

        self.assertRedirects(response, reverse("accounts:login"))
        user = User.objects.get(username="valid-phone-intern")
        self.assertEqual(user.phone, "+639171234567")

    def test_registration_rejects_invalid_phone_and_email(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "invalid-contact-intern",
                "first_name": "Valid",
                "last_name": "Name",
                "email": "not-an-email",
                "phone": "0917ABC4567",
                "student_id": "STU-REG-003",
                "university": "Example University",
                "course": "Computer Science",
                "year_level": "3",
                "placement_type": "platform",
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid email address.")
        self.assertContains(response, "Enter a Philippine phone number")
        self.assertFalse(User.objects.filter(username="invalid-contact-intern").exists())

    def test_company_registration_requires_location_fields(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "incomplete-company",
                "role": User.Role.COMPANY,
                "company_name": "Example Company",
                "company_address": "12 Green Road",
                "barangay": "San Isidro",
                "province": "Metro Manila",
                "industry_type": "it",
                "contact_person_name": "Jamie Smith",
                "contact_person_position": "HR Manager",
                "official_email": "company@example.test",
                "contact_number": "09171234567",
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("city_municipality", response.context["form"].errors)
        self.assertFalse(User.objects.filter(username="incomplete-company").exists())

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
                "role": User.Role.COMPANY,
                "company_name": "North Studio",
                "company_address": "12 Green Road, Springfield",
                "barangay": "San Isidro",
                "city_municipality": "Makati",
                "province": "Metro Manila",
                "postal_code": "1200",
                "industry_type": "it",
                "contact_person_name": "Jamie Smith",
                "contact_person_position": "HR Manager",
                "official_email": "north@example.test",
                "contact_number": "0917-123-4567",
                "company_website": "https://north.example.test",
                "moa_reference_number": "MOA-2026-001",
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
        )
        self.assertRedirects(response, reverse("accounts:login"))
        user = User.objects.get(username="new-company")
        self.assertEqual(user.role, User.Role.COMPANY)
        self.assertEqual(user.email, "north@example.test")
        self.assertEqual(user.phone, "+639171234567")
        self.assertEqual(user.first_name, "Jamie")
        self.assertEqual(user.last_name, "Smith")
        self.assertEqual(user.company_profile.organization, "North Studio")
        self.assertEqual(user.company_profile.company_name, "North Studio")
        self.assertEqual(user.company_profile.company_address, "12 Green Road, Springfield")
        self.assertEqual(user.company_profile.barangay, "San Isidro")
        self.assertEqual(user.company_profile.city_municipality, "Makati")
        self.assertEqual(user.company_profile.province, "Metro Manila")
        self.assertEqual(user.company_profile.postal_code, "1200")
        self.assertEqual(user.company_profile.industry_type, "it")
        self.assertEqual(user.company_profile.contact_person_name, "Jamie Smith")
        self.assertEqual(user.company_profile.contact_person_position, "HR Manager")
        self.assertEqual(user.company_profile.official_email, "north@example.test")
        self.assertEqual(user.company_profile.contact_number, "+639171234567")
        self.assertEqual(user.company_profile.company_website, "https://north.example.test")
        self.assertEqual(user.company_profile.moa_reference_number, "MOA-2026-001")

    def test_intern_cannot_open_coordinator_routes(self):
        user = User.objects.create_user(username="intern", password="Safe-example-Password-927!")
        self.client.force_login(user)
        response = self.client.get(reverse("portal:coordinator_dashboard"))
        self.assertEqual(response.status_code, 403)