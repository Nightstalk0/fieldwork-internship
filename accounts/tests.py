from datetime import timedelta
import re
from smtplib import SMTPException
from unittest.mock import patch

from django.core import mail
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from portal.models import INSTITUTION_NAME

from .middleware import RoleBasedRouteGuardMiddleware
from .models import PasswordResetCode, User


class AccountFlowTests(TestCase):
    def test_coordinator_role_displays_as_admin_without_changing_its_stored_value(self):
        coordinator = User.objects.create_user(
            username="existing-admin-role",
            password="Safe-example-Password-927!",
            role=User.Role.COORDINATOR,
        )

        coordinator.refresh_from_db()
        self.assertEqual(coordinator.role, "coordinator")
        self.assertEqual(coordinator.get_role_display(), "Admin")
        self.assertTrue(coordinator.is_admin)

    def test_staff_and_superuser_are_admin_without_changing_their_role(self):
        staff_user = User.objects.create_user(
            username="staff-admin-role",
            password="Safe-example-Password-927!",
            is_staff=True,
        )
        superuser = User.objects.create_user(
            username="superuser-admin-role",
            password="Safe-example-Password-927!",
            is_superuser=True,
            is_staff=False,
        )
        company = User.objects.create_user(
            username="non-admin-company",
            password="Safe-example-Password-927!",
            role=User.Role.COMPANY,
        )

        self.assertTrue(staff_user.is_admin)
        self.assertTrue(superuser.is_admin)
        self.assertEqual(superuser.role, User.Role.INTERN)
        self.assertFalse(company.is_admin)

    def test_route_guard_allows_superuser_to_admin_routes_without_staff_flag(self):
        superuser = User.objects.create_user(
            username="superuser-route-access",
            password="Safe-example-Password-927!",
            is_superuser=True,
            is_staff=False,
        )
        request = RequestFactory().get("/coordinator/dashboard/")
        request.user = superuser
        middleware = RoleBasedRouteGuardMiddleware(lambda _request: HttpResponse("ok"))

        response = middleware(request)

        self.assertEqual(response.status_code, 200)

    def test_route_guard_keeps_coordinator_out_of_intern_routes(self):
        coordinator = User.objects.create_user(
            username="admin-not-intern",
            password="Safe-example-Password-927!",
            role=User.Role.COORDINATOR,
        )
        request = RequestFactory().get("/intern/face/enroll/")
        request.user = coordinator
        middleware = RoleBasedRouteGuardMiddleware(lambda _request: HttpResponse("ok"))

        response = middleware(request)

        self.assertEqual(response.status_code, 403)

    def test_route_guard_allows_admin_worksite_management_but_not_other_company_routes(self):
        coordinator = User.objects.create_user(
            username="admin-company-worksite-access",
            password="Safe-example-Password-927!",
            role=User.Role.COORDINATOR,
        )
        middleware = RoleBasedRouteGuardMiddleware(lambda _request: HttpResponse("ok"))

        worksite_request = RequestFactory().get("/company/worksites/")
        worksite_request.user = coordinator
        worksite_response = middleware(worksite_request)
        self.assertEqual(worksite_response.status_code, 200)

        dashboard_request = RequestFactory().get("/company/dashboard/")
        dashboard_request.user = coordinator
        dashboard_response = middleware(dashboard_request)
        self.assertEqual(dashboard_response.status_code, 403)

    def test_login_page_has_accessible_password_visibility_toggle(self):
        response = self.client.get(reverse("accounts:login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="id_password"')
        self.assertContains(response, 'aria-label="Show password"')
        self.assertContains(response, 'class="password-visibility-toggle"')
        self.assertEqual(response.content.count(b'data-password-icon'), 1)
        self.assertEqual(response.content.count(b'class="password-visibility-toggle"'), 1)
        self.assertContains(response, "data-login-password-feedback")
        self.assertContains(response, "Email or username")
        self.assertContains(response, "Register your company")
        self.assertNotContains(response, "Company Partner Portal")
        self.assertContains(response, 'passwordInput.type = revealPassword ? "text" : "password"')

    def test_legacy_company_login_url_redirects_to_the_main_sign_in_page(self):
        response = self.client.get(reverse("accounts:company_login"))

        self.assertRedirects(
            response,
            reverse("accounts:login"),
            fetch_redirect_response=False,
        )

    def test_main_login_accepts_username_or_email_and_routes_each_role(self):
        test_accounts = (
            ("intern-main-login", "intern@example.test", User.Role.INTERN, "portal:intern_dashboard", "intern-main-login"),
            ("company-main-login", "company@example.test", User.Role.COMPANY, "portal:company_dashboard", "company@example.test"),
            ("admin-main-login", "admin@example.test", User.Role.COORDINATOR, "portal:coordinator_dashboard", "admin-main-login"),
        )
        for username, email, role, dashboard_name, identifier in test_accounts:
            with self.subTest(role=role):
                User.objects.create_user(
                    username=username,
                    email=email,
                    password="Safe-example-Password-927!",
                    role=role,
                )
                response = self.client.post(
                    reverse("accounts:login"),
                    {
                        "username": identifier,
                        "password": "Safe-example-Password-927!",
                    },
                )

                self.assertRedirects(
                    response,
                    reverse("portal:dashboard"),
                    fetch_redirect_response=False,
                )
                dashboard_response = self.client.get(reverse("portal:dashboard"))
                self.assertRedirects(
                    dashboard_response,
                    reverse(dashboard_name),
                    fetch_redirect_response=False,
                )
                self.client.logout()

    def test_company_registration_page_forces_company_role(self):
        response = self.client.post(
            reverse("accounts:company_register"),
            {
                "username": "dedicated-company",
                "role": User.Role.INTERN,
                "company_name": "Dedicated Company",
                "company_address": "12 Green Road",
                "barangay": "San Isidro",
                "city_municipality": "Makati",
                "province": "Metro Manila",
                "industry_type": "it",
                "contact_person_name": "Jamie Smith",
                "contact_person_position": "HR Manager",
                "official_email": "dedicated@example.test",
                "contact_number": "09171234567",
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
        )

        self.assertRedirects(response, reverse("accounts:login"))
        user = User.objects.get(username="dedicated-company")
        self.assertEqual(user.role, User.Role.COMPANY)
        self.assertEqual(user.company_profile.company_name, "Dedicated Company")

    def test_intern_registration_is_separate_from_company_registration(self):
        response = self.client.get(reverse("accounts:register"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="student_id"')
        self.assertNotContains(response, 'name="university"')
        self.assertNotIn("university", response.context["form"].fields)
        self.assertContains(response, INSTITUTION_NAME)
        self.assertContains(response, 'name="course"')
        self.assertContains(response, 'name="year_level"')
        self.assertContains(response, 'name="placement_type"')
        self.assertContains(response, '<input type="hidden" name="role"')
        self.assertContains(response, 'value="intern"')
        self.assertNotContains(response, 'name="company_name"')
        self.assertNotContains(response, 'data-role="company"')
        self.assertContains(response, "Company Partner registration page")
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
                "university": "Different University",
                "course": "Computer Science",
                "year_level": "3",
                "placement_type": "platform",
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
            follow=True,
        )

        self.assertEqual(response.redirect_chain, [(reverse("accounts:login"), 302)])
        self.assertContains(response, f"Account created for {INSTITUTION_NAME}.")
        user = User.objects.get(username="new-intern")
        self.assertEqual(user.role, User.Role.INTERN)
        self.assertEqual(user.intern_profile.student_id, "STU-REG-001")
        self.assertEqual(user.intern_profile.university, INSTITUTION_NAME)
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

    def test_existing_university_data_is_preserved_and_admin_displays_canonical_name(self):
        user = User.objects.create_user(username="legacy-university-intern")
        profile = user.intern_profile
        profile.university = "Legacy University"
        profile.save(update_fields=("university",))
        admin = User.objects.create_superuser(
            username="institution-admin",
            password="Safe-example-Password-927!",
        )
        self.client.force_login(admin)

        response = self.client.get(reverse("admin:portal_internprofile_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, INSTITUTION_NAME)
        self.assertNotContains(response, "Legacy University")
        profile.refresh_from_db()
        self.assertEqual(profile.university, "Legacy University")

    def test_main_registration_cannot_create_a_company_account(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "company-on-intern-form",
                "role": User.Role.COMPANY,
                "company_name": "Company on Intern Form",
                "company_address": "12 Green Road",
                "barangay": "San Isidro",
                "city_municipality": "Makati",
                "province": "Metro Manila",
                "industry_type": "it",
                "contact_person_name": "Jamie Smith",
                "contact_person_position": "HR Manager",
                "official_email": "main-register-company@example.test",
                "contact_number": "09171234567",
                "password1": "Safe-example-Password-927!",
                "password2": "Safe-example-Password-927!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="company-on-intern-form").exists())

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
            reverse("accounts:company_register"),
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
        coordinator = User.objects.create_user(
            username="portal-admin-only",
            password="Safe-example-Password-927!",
            role=User.Role.COORDINATOR,
        )
        self.client.force_login(coordinator)
        self.assertEqual(self.client.get(reverse("admin:accounts_user_changelist")).status_code, 302)

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
            reverse("accounts:company_register"),
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

    def test_login_page_links_to_forgot_password(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertContains(response, reverse("accounts:password_reset"))
        self.assertContains(response, "Forgot your password?")

    def test_forgot_password_email_code_verification_and_reset_flow(self):
        user = User.objects.create_user(
            username="reset-intern",
            email="reset.intern@example.test",
            password="Initial-Password-927!",
        )
        request_url = reverse("accounts:password_reset")
        verify_url = reverse("accounts:password_reset_verify")
        confirm_url = reverse("accounts:password_reset_confirm")

        requested = self.client.post(request_url, {"email": "  RESET.INTERN@example.test  "})
        self.assertRedirects(requested, verify_url)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["reset.intern@example.test"])
        match = re.search(r"\b(\d{6})\b", mail.outbox[0].body)
        self.assertIsNotNone(match)
        code = match.group(1)

        record = PasswordResetCode.objects.get(user=user)
        self.assertNotIn(code, record.code_hash)
        self.assertTrue(record.verify_code(code))

        verified = self.client.post(verify_url, {
            "email": "reset.intern@example.test",
            "code": code,
        })
        self.assertRedirects(verified, confirm_url)

        weak = self.client.post(confirm_url, {
            "new_password1": "12345678",
            "new_password2": "12345678",
        })
        self.assertEqual(weak.status_code, 200)
        self.assertTrue(weak.context["form"].errors)

        reset = self.client.post(confirm_url, {
            "new_password1": "Updated-Strong-Password-2026!",
            "new_password2": "Updated-Strong-Password-2026!",
        })
        self.assertRedirects(reset, reverse("accounts:login"))

        user.refresh_from_db()
        self.assertFalse(user.check_password("Initial-Password-927!"))
        self.assertTrue(user.check_password("Updated-Strong-Password-2026!"))
        record.refresh_from_db()
        self.assertIsNotNone(record.used_at)

        reused_confirm = self.client.get(confirm_url)
        self.assertRedirects(reused_confirm, request_url)
        reused_verify = self.client.post(verify_url, {
            "email": "reset.intern@example.test",
            "code": code,
        })
        self.assertEqual(reused_verify.status_code, 200)
        self.assertContains(reused_verify, "invalid or has expired")

    def test_forgot_password_unknown_email_does_not_leak_account_status(self):
        response = self.client.post(
            reverse("accounts:password_reset"),
            {"email": "nonexistent@example.test"},
            follow=True,
        )
        self.assertRedirects(response, reverse("accounts:password_reset_verify"))
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, "If an active account matches that email")

    def test_forgot_password_code_expires_and_locks_after_five_failed_attempts(self):
        user = User.objects.create_user(
            username="lockout-intern",
            email="lockout@example.test",
            password="Initial-Password-927!",
        )
        self.client.post(reverse("accounts:password_reset"), {"email": "lockout@example.test"})
        code = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)
        wrong_code = "000000" if code != "000000" else "111111"

        verify_url = reverse("accounts:password_reset_verify")
        for _ in range(PasswordResetCode.MAX_FAILED_ATTEMPTS):
            attempt = self.client.post(verify_url, {
                "email": "lockout@example.test",
                "code": wrong_code,
            })
            self.assertEqual(attempt.status_code, 200)

        record = PasswordResetCode.objects.get(user=user)
        self.assertIsNotNone(record.used_at)
        self.assertEqual(record.failed_attempts, PasswordResetCode.MAX_FAILED_ATTEMPTS)

        blocked = self.client.post(verify_url, {
            "email": "lockout@example.test",
            "code": code,
        })
        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, "invalid or has expired")

        PasswordResetCode.objects.filter(user=user).update(
            used_at=None,
            failed_attempts=0,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        expired = self.client.post(verify_url, {
            "email": "lockout@example.test",
            "code": code,
        })
        self.assertEqual(expired.status_code, 200)
        self.assertContains(expired, "invalid or has expired")

    def test_forgot_password_resend_respects_cooldown_and_invalidates_previous_code(self):
        user = User.objects.create_user(
            username="resend-intern",
            email="resend@example.test",
            password="Initial-Password-927!",
        )
        self.client.post(reverse("accounts:password_reset"), {"email": "resend@example.test"})
        first_code = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)

        verify_url = reverse("accounts:password_reset_verify")
        too_soon = self.client.post(
            verify_url,
            {"action": "resend", "email": "resend@example.test"},
            follow=True,
        )
        self.assertContains(too_soon, "Please wait")
        self.assertEqual(len(mail.outbox), 1)

        PasswordResetCode.objects.filter(user=user).update(
            created_at=timezone.now() - timedelta(seconds=PasswordResetCode.RESEND_COOLDOWN_SECONDS + 5),
        )
        resent = self.client.post(
            verify_url,
            {"action": "resend", "email": "resend@example.test"},
            follow=True,
        )
        self.assertContains(resent, "new 6-digit verification code")
        self.assertEqual(len(mail.outbox), 2)
        second_code = re.search(r"\b(\d{6})\b", mail.outbox[1].body).group(1)

        old_rejected = self.client.post(verify_url, {
            "email": "resend@example.test",
            "code": first_code,
        })
        if first_code != second_code:
            self.assertEqual(old_rejected.status_code, 200)
            self.assertContains(old_rejected, "Incorrect verification code")
        new_accepted = self.client.post(verify_url, {
            "email": "resend@example.test",
            "code": second_code,
        })
        self.assertRedirects(new_accepted, reverse("accounts:password_reset_confirm"))

    def test_forgot_password_confirm_requires_verified_session_and_handles_smtp_error(self):
        User.objects.create_user(
            username="smtp-intern",
            email="smtp@example.test",
            password="Initial-Password-927!",
        )
        direct_confirm = self.client.get(reverse("accounts:password_reset_confirm"))
        self.assertRedirects(direct_confirm, reverse("accounts:password_reset"))

        with patch("accounts.views.send_mail", side_effect=SMTPException("SMTP unavailable")):
            failed_send = self.client.post(
                reverse("accounts:password_reset"),
                {"email": "smtp@example.test"},
            )
        self.assertEqual(failed_send.status_code, 200)
        self.assertContains(failed_send, "We could not send the verification email right now.")
        self.assertFalse(
            PasswordResetCode.objects.filter(email="smtp@example.test", used_at__isnull=True).exists()
        )