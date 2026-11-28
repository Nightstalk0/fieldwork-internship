import base64
import tempfile
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from docx import Document as DocxDocument
from PIL import Image
from cryptography.fernet import Fernet

from accounts.models import User
from ml_engine.face_recognition import FaceImageError, decrypt_embedding, encrypt_embedding
from .forms import (
    ApplicationSubmissionForm,
    CompanyProfileForm,
    DailyReportReviewForm,
    InternProfileForm,
    PostingForm,
    ScorecardSelectionForm,
    WorksiteLocationForm,
)
from .models import AcademicPeriod, Application, AttendanceFaceCapture, AttendanceLog, CompanyProfile, DailyReport, FACE_CONSENT_VERSION, FaceEnrollment, FaceEnrollmentSubmission, FaceReenrollmentRequest, InternProfile, Posting, RiskAssessment, Scorecard, WeeklyReport, WorksiteLocation
from .utils import haversine_distance_km
from .views import (
    _attendance_face_verification_token,
    _attendance_location_status,
    _worksites_for_attendance,
)
from .validators import FileSizeAndTypeValidator


class PortalWorkflowTests(TestCase):
    def setUp(self):
        self.intern_user = User.objects.create_user(username="intern", password="Safe-example-Password-927!")
        self.intern = InternProfile.objects.get(user=self.intern_user)
        self.company_user = User.objects.create_user(
            username="company",
            password="Safe-example-Password-927!",
            role=User.Role.COMPANY,
            first_name="Host",
            last_name="Company",
        )
        self.company = CompanyProfile.objects.get(user=self.company_user)
        self.posting = Posting.objects.create(
            company=self.company,
            title="Operations Intern",
            description="Support the placement team.",
            location="Remote",
            status=Posting.Status.PUBLISHED,
        )

    def _camera_data_url(self):
        image = BytesIO()
        Image.new("RGB", (100, 100), "white").save(image, format="JPEG")
        return "data:image/jpeg;base64," + base64.b64encode(image.getvalue()).decode("ascii")

    def _complete_intern_details(self, *, external=False):
        self.intern.student_id = "TEST-STUDENT-001"
        self.intern.university = "Example University"
        self.intern.course = "Information Technology"
        self.intern.year_level = 3
        self.intern.placement_type = (
            InternProfile.PlacementType.EXTERNAL if external else InternProfile.PlacementType.PLATFORM
        )
        self.intern.external_host = "External Host" if external else ""
        self.intern.save()

    def _complete_intern_profile(self, *, external=False):
        self._complete_intern_details(external=external)
        FaceEnrollment.objects.update_or_create(
            intern=self.intern,
            defaults={
                "encrypted_embedding": b"encrypted test embedding",
                "enrolled_by": self.intern_user,
                "consent_confirmed_at": timezone.now(),
                "consent_text_version": FACE_CONSENT_VERSION,
            },
        )
    def _enroll_attendance_location(self, intern=None):
        intern = intern or self.intern
        if intern.placement_type == InternProfile.PlacementType.EXTERNAL:
            return WorksiteLocation.objects.create(
                intern=intern,
                name="External training site",
                address="1 Training Road",
                latitude="14.600000",
                longitude="121.000000",
                created_by=self.company_user,
            )
        application, _ = Application.objects.get_or_create(
            intern=intern,
            posting=self.posting,
            defaults={"status": Application.Status.ACCEPTED},
        )
        if application.status != Application.Status.ACCEPTED:
            application.status = Application.Status.ACCEPTED
            application.save(update_fields=("status",))
        return WorksiteLocation.objects.create(
            intern=intern,
            application=application,
            name="Main office",
            address="1 Main Road",
            latitude="14.600000",
            longitude="121.000000",
            created_by=self.company_user,
        )

    def _attendance_payload(self, action, **extra):
        return {
            "action": action,
            f"{action}_latitude": "14.600000",
            f"{action}_longitude": "121.000000",
            f"{action}_accuracy_meters": "10",
            **extra,
        }

    def test_company_dashboard_focuses_on_attendance_and_worksites(self):
        Application.objects.create(intern=self.intern, posting=self.posting)
        self.client.force_login(self.company_user)

        response = self.client.get(reverse("portal:company_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.company.organization)
        self.assertContains(response, "Attendance monitoring")
        self.assertContains(response, "Open DTR queue")
        self.assertContains(response, "Approved worksite locations")
        self.assertEqual(response.context["active_worksite_count"], 0)

    def test_company_users_cannot_submit_or_deactivate_worksites(self):
        self.client.force_login(self.company_user)
        url = reverse("portal:company_worksites")
        payload = {
            "action": "create",
            "name": "North office",
            "address": "10 North Road",
            "latitude": "14.600000",
            "longitude": "121.000000",
            "radius_meters": "100",
            "reported_accuracy_meters": "20",
            "confirm_location": "on",
        }

        created = self.client.post(url, payload)

        self.assertEqual(created.status_code, 403)
        self.assertFalse(WorksiteLocation.objects.filter(company=self.company).exists())

        worksite = WorksiteLocation.objects.create(
            company=self.company,
            name="Approved office",
            address="10 North Road",
            latitude="14.600000",
            longitude="121.000000",
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
            is_active=True,
        )
        deactivated = self.client.post(url, {
            "action": "deactivate",
            "worksite_id": worksite.pk,
        })

        self.assertEqual(deactivated.status_code, 403)
        worksite.refresh_from_db()
        self.assertTrue(worksite.is_active)

    def test_company_worksite_page_shows_approved_location_read_only(self):
        worksite = WorksiteLocation.objects.create(
            company=self.company,
            name="North office",
            address="10 North Road, Barangay San Isidro, Tanauan, Batangas",
            latitude="14.600000",
            longitude="121.000000",
            radius_meters=150,
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
            is_active=True,
        )
        WorksiteLocation.objects.create(
            company=self.company,
            name="Former office",
            address="2 Old Road, Tanauan, Batangas",
            latitude="14.610000",
            longitude="121.010000",
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
            is_active=False,
        )
        self.client.force_login(self.company_user)
        response = self.client.get(reverse("portal:company_worksites"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, worksite.address)
        self.assertContains(response, "Approved · Active")
        self.assertContains(response, "Approved · Inactive")
        self.assertContains(response, "150 m")
        self.assertContains(response, 'class="worksite-map company-worksite-map"')
        self.assertContains(response, "tile.openstreetmap.fr/hot")
        self.assertNotContains(response, "id=\"worksite-form\"")
        self.assertNotContains(response, "id=\"id_address\"")

    def test_company_worksite_page_explains_intern_and_admin_workflow_when_empty(self):
        self.client.force_login(self.company_user)
        response = self.client.get(reverse("portal:company_worksites"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No approved worksite is on file yet.")
        self.assertContains(response, "The Intern must submit the location")
        self.assertContains(response, "an Admin must approve it")
        self.assertNotContains(response, 'id="worksite-form"')

    def test_intern_worksite_page_displays_approved_intern_worksite_read_only(self):
        self._complete_intern_details()
        self.company.company_name = "Assigned Host Company"
        self.company.save()
        application = Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        worksite = WorksiteLocation.objects.create(
            intern=self.intern,
            application=application,
            name="Assigned Host Company worksite",
            address="1 Main Road",
            latitude="14.600000",
            longitude="121.000000",
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
            is_active=True,
        )
        self.client.force_login(self.intern_user)

        profile = self.client.get(reverse("portal:profile"))
        response = self.client.get(reverse("portal:intern_worksite_registration"))

        self.assertEqual(profile.status_code, 200)
        self.assertContains(profile, 'name="placement_type"')
        self.assertContains(profile, 'name="student_id"')
        self.assertNotContains(profile, 'name="university"')
        self.assertContains(profile, "Richwell Colleges, Incorporated")
        self.assertContains(profile, 'name="course"')
        self.assertContains(profile, 'name="resume"')
        self.assertNotContains(profile, "OJT worksite location")
        self.assertNotContains(profile, "intern-worksite-request-form")
        self.assertNotContains(profile, worksite.address)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assigned Host Company")
        self.assertContains(response, worksite.name)
        self.assertContains(response, worksite.address)
        self.assertContains(response, "Approved · Official attendance geofence")
        self.assertContains(response, "Official geofence radius: 100 m")
        self.assertContains(response, 'class="worksite-map intern-approved-worksite-map"')
        self.assertContains(response, 'data-latitude="14.600000"')
        self.assertContains(response, 'data-longitude="121.000000"')
        self.assertContains(response, "tile.openstreetmap.fr/hot")
        self.assertContains(response, "OpenStreetMap France")
        self.assertNotContains(response, "No approved OJT worksite has been assigned")
        self.assertNotContains(response, "Request a worksite change")
        self.assertNotContains(response, "Get current location")
        self.assertNotContains(response, 'id="intern-worksite-request-form"')

    def test_intern_worksite_page_shows_pending_and_unassigned_states(self):
        self.client.force_login(self.intern_user)
        profile = self.client.get(reverse("portal:profile"))
        no_assignment = self.client.get(reverse("portal:intern_worksite_registration"))
        self.assertNotContains(profile, "No approved OJT worksite")
        self.assertNotContains(profile, "intern-worksite-request-form")
        self.assertContains(
            no_assignment,
            "No approved OJT worksite is on file yet. Submit your worksite details below for Admin review.",
        )
        self.assertContains(no_assignment, 'id="intern-worksite-request-form"')
        self.assertContains(no_assignment, 'id="intern-worksite-map"')
        self.assertNotContains(no_assignment, "An accepted internship assignment is required")
        self.assertNotContains(no_assignment, "intern-approved-worksite-map")

        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        pending_company_site = WorksiteLocation.objects.create(
            company=self.company,
            name="Pending Company worksite",
            address="9 Company Road",
            latitude="14.600000",
            longitude="121.000000",
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
            is_active=False,
            created_by=self.company_user,
        )
        pending_company_response = self.client.get(reverse("portal:intern_worksite_registration"))
        self.assertContains(
            pending_company_response,
            "No approved OJT worksite is on file yet. Submit your worksite details below for Admin review.",
        )
        self.assertNotContains(pending_company_response, pending_company_site.name)
        self.assertNotContains(pending_company_response, "intern-approved-worksite-map")

        Application.objects.filter(intern=self.intern).delete()
        self._complete_intern_details(external=True)
        pending_site = WorksiteLocation.objects.create(
            intern=self.intern,
            name="Pending external OJT site",
            address="7 Placement Road",
            latitude="14.700000",
            longitude="121.100000",
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
            is_active=False,
            created_by=self.intern_user,
        )
        pending_response = self.client.get(reverse("portal:intern_worksite_registration"))
        self.assertContains(
            pending_response,
            "No approved OJT worksite is on file yet. Submit your worksite details below for Admin review.",
        )
        self.assertContains(pending_response, "Pending Admin Approval")
        self.assertContains(pending_response, pending_site.name)
        self.assertNotContains(pending_response, "intern-approved-worksite-map")

    def test_intern_without_assignment_can_submit_worksite_for_admin_approval(self):
        self.client.force_login(self.intern_user)
        self.assertFalse(Application.objects.filter(intern=self.intern).exists())
        self.assertFalse(WorksiteLocation.objects.filter(intern=self.intern).exists())

        page = self.client.get(reverse("portal:intern_worksite_registration"))
        self.assertContains(page, 'id="intern-worksite-request-form"')
        self.assertContains(page, "Search address on OpenStreetMap")

        address = "Richwell Colleges, Plaridel, Bulacan, Philippines"
        response = self.client.post(reverse("portal:intern_worksite_registration"), {
            "name": "New Intern Worksite",
            "address": address,
            "latitude": "14.700000",
            "longitude": "121.100000",
            "reported_accuracy_meters": "18",
            "confirm_location": "on",
        })

        self.assertRedirects(response, reverse("portal:intern_worksite_registration"))
        worksite = WorksiteLocation.objects.get(intern=self.intern)
        self.assertIsNone(worksite.application_id)
        self.assertIsNone(worksite.company_id)
        self.assertEqual(worksite.address, address)
        self.assertEqual(worksite.approval_status, WorksiteLocation.ApprovalStatus.PENDING)
        self.assertFalse(worksite.is_active)
        self.assertEqual(
            WorksiteLocation.objects.filter(intern=self.intern).count(),
            1,
        )
        blocked_resubmission = self.client.post(reverse("portal:intern_worksite_registration"), {
            "name": "Forged second location",
            "address": "999 Modified Road",
            "latitude": "15.000000",
            "longitude": "122.000000",
            "reported_accuracy_meters": "18",
            "confirm_location": "on",
        }, follow=True)
        self.assertContains(blocked_resubmission, "submission is locked")
        worksite.refresh_from_db()
        self.assertEqual(worksite.name, "New Intern Worksite")
        self.assertEqual(worksite.address, address)
        self.assertEqual(WorksiteLocation.objects.filter(intern=self.intern).count(), 1)
        pending_page = self.client.get(reverse("portal:intern_worksite_registration"))
        self.assertContains(pending_page, "Pending Admin Approval")
        self.assertContains(pending_page, address)
        pending_attendance = self.client.post(
            reverse("portal:attendance_face_preview"),
            self._attendance_payload("clock_in", face_image=self._camera_data_url()),
        )
        self.assertEqual(pending_attendance.status_code, 409)

        admin = User.objects.create_user(username="no-assignment-site-admin", role=User.Role.COORDINATOR)
        self.client.force_login(admin)
        approval_url = reverse("portal:admin_approvals")
        approved = self.client.post(approval_url, {
            "action": "approve_worksite",
            "request_id": worksite.pk,
        })
        self.assertRedirects(approved, approval_url)
        worksite.refresh_from_db()
        self.assertEqual(worksite.approval_status, WorksiteLocation.ApprovalStatus.APPROVED)
        self.assertTrue(worksite.is_active)

        self.client.force_login(self.intern_user)
        with patch(
            "portal.views._attendance_face_check",
            return_value=(AttendanceLog.FaceCheckStatus.MATCHED, 0.82, None),
        ):
            approved_attendance = self.client.post(
                reverse("portal:attendance_face_preview"),
                self._attendance_payload(
                    "clock_in",
                    clock_in_latitude="14.700000",
                    clock_in_longitude="121.100000",
                    face_image=self._camera_data_url(),
                ),
            )
        self.assertEqual(approved_attendance.status_code, 200)
        self.assertEqual(approved_attendance.json()["location_status"], AttendanceFaceCapture.LocationStatus.INSIDE)

    def test_intern_and_admin_sidebar_icons_have_consistent_active_states(self):
        map_pin = '<path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0Z"/>'
        self.client.force_login(self.intern_user)
        regular_profile = self.client.get(reverse("portal:profile"))
        regular_content = regular_profile.content.decode()
        profile_link_start = regular_content.index('href="/intern/profile/" ')
        profile_link = regular_content[profile_link_start:regular_content.index("</a>", profile_link_start)]
        self.assertIn('class="nav-link-active" aria-current="page"', profile_link)

        intern_profile = self.client.get(reverse("portal:intern_worksite_registration"))
        intern_content = intern_profile.content.decode()
        intern_link_start = intern_content.index('href="/intern/profile/worksite-location/"')
        intern_link = intern_content[intern_link_start:intern_content.index("</a>", intern_link_start)]
        self.assertIn("Worksite Location", intern_link)
        self.assertIn('class="nav-icon" width="20" height="20"', intern_link)
        self.assertIn(map_pin, intern_link)
        self.assertIn('class="nav-link-active" aria-current="page"', intern_link)
        legacy_worksite_link = self.client.get(reverse("portal:profile"), {"section": "worksite"})
        self.assertRedirects(legacy_worksite_link, reverse("portal:intern_worksite_registration"))

        admin = User.objects.create_user(username="worksite-sidebar-admin", role=User.Role.COORDINATOR)
        self.client.force_login(admin)
        admin_dashboard = self.client.get(reverse("portal:coordinator_dashboard"))
        dashboard_content = admin_dashboard.content.decode()
        dashboard_link_start = dashboard_content.index('href="/admin/dashboard/"')
        dashboard_link = dashboard_content[dashboard_link_start:dashboard_content.index("</a>", dashboard_link_start)]
        self.assertIn('class="nav-link-active" aria-current="page"', dashboard_link)

        admin_approvals = self.client.get(reverse("portal:admin_approvals"))
        admin_content = admin_approvals.content.decode()
        admin_link_start = admin_content.index('href="/admin/approvals/"')
        admin_link = admin_content[admin_link_start:admin_content.index("</a>", admin_link_start)]
        self.assertIn("Admin approvals", admin_link)
        self.assertIn('class="nav-icon" width="20" height="20"', admin_link)
        self.assertIn('<circle cx="12" cy="12" r="9"/>', admin_link)
        self.assertIn('class="nav-link-active" aria-current="page"', admin_link)
        journals = self.client.get(reverse("portal:admin_daily_reports"))
        journals_content = journals.content.decode()
        journal_link_start = journals_content.index('href="/admin/daily-reports/"')
        journal_link = journals_content[journal_link_start:journals_content.index("</a>", journal_link_start)]
        self.assertIn("All intern journals", journal_link)
        self.assertIn('class="nav-icon" width="20" height="20"', journal_link)
        self.assertIn('class="nav-link-active" aria-current="page"', journal_link)

    def test_company_and_admin_worksite_management_pages_remain_available(self):
        self.client.force_login(self.company_user)
        company_response = self.client.get(reverse("portal:company_worksites"))
        self.assertEqual(company_response.status_code, 200)
        self.assertContains(company_response, "No approved worksite is on file yet.")
        self.assertNotContains(company_response, "Search address on OpenStreetMap")
        self.assertNotContains(company_response, "Get current location")

        admin = User.objects.create_user(username="worksite-page-admin", role=User.Role.COORDINATOR)
        self.assertTrue(admin.is_admin)
        self.client.force_login(admin)
        admin_response = self.client.get(reverse("portal:company_worksites"))
        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, "Register directly approved sites for a selected Company")
        self.assertContains(admin_response, "Search address on OpenStreetMap")
        self.assertContains(admin_response, "Get current location")
        created = self.client.post(reverse("portal:company_worksites"), {
            "action": "create",
            "company": self.company.pk,
            "name": "Admin registered branch",
            "address": "11 Admin Road",
            "latitude": "14.610000",
            "longitude": "121.010000",
            "radius_meters": "100",
            "reported_accuracy_meters": "18",
            "confirm_location": "on",
        })
        self.assertRedirects(created, reverse("portal:company_worksites"))
        admin_site = WorksiteLocation.objects.get(name="Admin registered branch")
        self.assertEqual(admin_site.company, self.company)
        self.assertEqual(admin_site.approval_status, WorksiteLocation.ApprovalStatus.APPROVED)
        self.assertTrue(admin_site.is_active)

    def test_intern_submission_is_hidden_until_approval_and_used_for_attendance(self):
        application = Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        current_site = WorksiteLocation.objects.create(
            company=self.company,
            name="Current office",
            address="10 Old Road",
            latitude="14.600000",
            longitude="121.000000",
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
            is_active=True,
        )
        self.client.force_login(self.company_user)
        worksites_url = reverse("portal:company_worksites")
        company_attempt = self.client.post(worksites_url, {
            "action": "create",
            "name": "Relocated office",
            "address": "20 New Road",
            "latitude": "14.700000",
            "longitude": "121.100000",
            "radius_meters": "100",
            "reported_accuracy_meters": "15",
            "confirm_location": "on",
        })
        self.assertEqual(company_attempt.status_code, 403)
        self.assertFalse(WorksiteLocation.objects.filter(name="Relocated office").exists())

        self.client.force_login(self.intern_user)
        requested = self.client.post(reverse("portal:intern_worksite_registration"), {
            "application": application.pk,
            "name": "Relocated office",
            "address": "20 New Road, Tanauan, Batangas",
            "latitude": "14.700000",
            "longitude": "121.100000",
            "reported_accuracy_meters": "15",
            "confirm_location": "on",
        })
        self.assertRedirects(requested, reverse("portal:intern_worksite_registration"))
        replacement = WorksiteLocation.objects.get(name="Relocated office")
        self.assertEqual(replacement.approval_status, WorksiteLocation.ApprovalStatus.PENDING)
        self.assertFalse(replacement.is_active)
        current_site.refresh_from_db()
        self.assertTrue(current_site.is_active)

        self.client.force_login(self.company_user)
        company_page = self.client.get(worksites_url)
        self.assertNotContains(company_page, "Relocated office")

        self.client.force_login(User.objects.create_user(
            username="location-approver",
            role=User.Role.COORDINATOR,
        ))
        approval_url = reverse("portal:admin_approvals")
        self.assertContains(self.client.get(approval_url), "Relocated office")
        approved = self.client.post(approval_url, {
            "action": "approve_worksite",
            "request_id": replacement.pk,
        })

        self.assertRedirects(approved, approval_url)
        replacement.refresh_from_db()
        current_site.refresh_from_db()
        self.assertTrue(replacement.is_active)
        self.assertEqual(replacement.approval_status, WorksiteLocation.ApprovalStatus.APPROVED)
        self.assertTrue(current_site.is_active)

        self.client.force_login(self.company_user)
        approved_company_page = self.client.get(worksites_url)
        self.assertContains(approved_company_page, "20 New Road, Tanauan, Batangas")

        self._complete_intern_profile()
        self.client.force_login(self.intern_user)
        with patch(
            "portal.views._attendance_face_check",
            return_value=(AttendanceLog.FaceCheckStatus.MATCHED, 0.82, b"encrypted frame"),
        ):
            attendance = self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload(
                    "clock_in",
                    clock_in_latitude="14.700000",
                    clock_in_longitude="121.100000",
                ),
            )
        self.assertRedirects(attendance, reverse("portal:attendance"))
        log = AttendanceLog.objects.get(intern=self.intern)
        self.assertTrue(log.time_in_approved)
        self.assertEqual(log.clock_in_distance_meters, 0)

    def test_admin_worksite_form_uses_openstreetmap_without_google_maps(self):
        self.client.force_login(self.company_user)

        response = self.client.get(reverse("portal:company_worksites"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="worksite-map"')
        self.assertNotContains(response, "nominatim.openstreetmap.org/search")

        admin = User.objects.create_user(username="admin-worksite-map", role=User.Role.COORDINATOR)
        self.client.force_login(admin)
        admin_response = self.client.get(reverse("portal:company_worksites"))
        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, 'id="worksite-map"')
        self.assertContains(admin_response, 'id="id_address"')
        self.assertContains(admin_response, 'id="search-worksite-address"')
        self.assertContains(admin_response, "nominatim.openstreetmap.org/search")
        self.assertContains(admin_response, "tile.openstreetmap.fr/hot")
        self.assertContains(admin_response, "OpenStreetMap France")
        self.assertNotContains(admin_response, "maps.googleapis.com")
        self.assertNotContains(admin_response, "libraries=places")
        self.assertContains(admin_response, "dragend")
        self.assertContains(admin_response, "exact entrance")
        self.assertContains(admin_response, "Get current location")
        self.assertContains(admin_response, "confirm_location")

    def test_haversine_worksite_check_allows_gps_drift_and_defaults_null_radius(self):
        worksite = SimpleNamespace(
            latitude=14.6,
            longitude=121.0,
            radius_meters=None,
        )

        inside_status, inside_distance, inside_radius = _attendance_location_status(
            [worksite],
            14.6004,
            121.0,
            10,
        )
        outside_status, outside_distance, outside_radius = _attendance_location_status(
            [worksite],
            14.6011,
            121.0,
            10,
        )
        poor_accuracy_status, _, _ = _attendance_location_status(
            [worksite],
            14.6004,
            121.0,
            31,
        )

        self.assertEqual(inside_status, AttendanceFaceCapture.LocationStatus.INSIDE)
        self.assertLess(inside_distance, inside_radius)
        self.assertEqual(inside_radius, 100)
        self.assertEqual(outside_status, AttendanceFaceCapture.LocationStatus.OUTSIDE)
        self.assertGreater(outside_distance, outside_radius)
        self.assertEqual(poor_accuracy_status, AttendanceFaceCapture.LocationStatus.UNAVAILABLE)

    def test_external_intern_worksite_cannot_be_submitted_by_admin(self):
        self._complete_intern_details(external=True)
        coordinator = User.objects.create_user(username="worksite-coordinator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)
        url = reverse("portal:coordinator_external_worksites")

        response = self.client.post(url, {
            "action": "create",
            "intern": self.intern.pk,
            "name": "External host office",
            "address": "20 Host Road",
            "latitude": "14.600000",
            "longitude": "121.000000",
            "radius_meters": "100",
            "confirm_location": "on",
        })

        self.assertEqual(response.status_code, 403)
        self.assertFalse(WorksiteLocation.objects.filter(intern=self.intern).exists())
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_external_intern_can_submit_worksite_location_for_admin_approval(self):
        self._complete_intern_details(external=True)
        self.client.force_login(self.intern_user)
        url = reverse("portal:intern_worksite_registration")
        profile = self.client.get(reverse("portal:profile"))
        self.assertNotContains(profile, 'id="intern-worksite-request-form"')
        worksite_page = self.client.get(url)
        self.assertContains(worksite_page, 'id="intern-worksite-request-form"')
        self.assertContains(worksite_page, "Search address on OpenStreetMap")
        self.assertContains(worksite_page, "Get current location")
        self.assertTrue(worksite_page.context["worksite_form"].fields["latitude"].widget.is_hidden)
        self.assertTrue(worksite_page.context["worksite_form"].fields["longitude"].widget.is_hidden)
        self.assertContains(worksite_page, 'id="id_latitude"')
        self.assertContains(worksite_page, 'id="id_longitude"')
        self.assertContains(worksite_page, 'id="intern-worksite-map"')
        self.assertContains(worksite_page, 'countrycodes: "ph"')
        self.assertContains(worksite_page, "tile.openstreetmap.fr/hot")
        self.assertContains(worksite_page, 'role="listbox"')
        self.assertContains(worksite_page, 'setAttribute("role", "option")')
        self.assertContains(worksite_page, 'internWorksiteMarker.on("dragend"')
        self.assertContains(worksite_page, 'nominatim.openstreetmap.org/reverse')
        self.assertContains(worksite_page, '"accept-language": "en-PH,en"')
        self.assertContains(worksite_page, "await reverseGeocodeWorksiteAddress(latitude, longitude, accuracy)")
        self.assertContains(worksite_page, "addressField.value = readableAddress")
        self.assertContains(worksite_page, "Your existing address was kept.")
        complete_address = "Richwell Colleges, Plaridel, Bulacan, Central Luzon, 3004, Philippines"
        payload = {
            "name": "Intern's OJT office",
            "address": complete_address,
            "latitude": "14.700000",
            "longitude": "121.100000",
            "reported_accuracy_meters": "18",
            "confirm_location": "on",
        }

        response = self.client.post(url, payload)

        self.assertRedirects(response, url)
        worksite = WorksiteLocation.objects.get(intern=self.intern)
        self.assertIsNone(worksite.company_id)
        self.assertIsNone(worksite.application_id)
        self.assertEqual(worksite.approval_status, WorksiteLocation.ApprovalStatus.PENDING)
        self.assertFalse(worksite.is_active)
        self.assertEqual(worksite.radius_meters, 100)
        self.assertEqual(worksite.reported_accuracy_meters, 18)
        self.assertEqual(worksite.address, complete_address)
        worksite_page = self.client.get(url)
        self.assertContains(worksite_page, "No approved OJT worksite is on file yet")
        self.assertContains(worksite_page, "Pending Admin Approval")
        self.assertContains(worksite_page, complete_address)

    def test_platform_intern_submission_is_pending_and_cannot_edit_company_worksite(self):
        self._complete_intern_details()
        application = Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        official_site = WorksiteLocation.objects.create(
            company=self.company,
            name="Official office",
            address="1 Company Road",
            latitude="14.600000",
            longitude="121.000000",
            is_active=True,
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
        )
        self.client.force_login(self.intern_user)
        response = self.client.post(reverse("portal:intern_worksite_registration"), {
            "application": application.pk,
            "name": "Requested OJT office",
            "address": "25 Training Avenue",
            "latitude": "14.700000",
            "longitude": "121.100000",
            "reported_accuracy_meters": "12",
            "confirm_location": "on",
        })

        self.assertRedirects(response, reverse("portal:intern_worksite_registration"))
        request = WorksiteLocation.objects.get(intern=self.intern)
        self.assertEqual(request.application, application)
        self.assertEqual(request.approval_status, WorksiteLocation.ApprovalStatus.PENDING)
        self.assertFalse(request.is_active)
        self.assertEqual(self.client.get(reverse("portal:company_worksites")).status_code, 403)
        edit_attempt = self.client.post(reverse("portal:company_worksites"), {
            "action": "create",
            "worksite_id": official_site.pk,
            "name": "Forged replacement",
            "address": "999 Other Road",
            "latitude": "15.000000",
            "longitude": "122.000000",
            "radius_meters": "5000",
        })
        self.assertEqual(edit_attempt.status_code, 403)
        official_site.refresh_from_db()
        self.assertTrue(official_site.is_active)
        self.assertEqual(official_site.approval_status, WorksiteLocation.ApprovalStatus.APPROVED)
        self.assertEqual(str(official_site.latitude), "14.600000")
        self.assertEqual(str(official_site.longitude), "121.000000")
        self.assertEqual(official_site.radius_meters, 100)

    def test_admin_role_can_approve_and_reset_intern_worksites(self):
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        worksite = WorksiteLocation.objects.create(
            intern=self.intern,
            name="Intern-requested office",
            address="30 Training Avenue",
            latitude="14.700000",
            longitude="121.100000",
            radius_meters=100,
            reported_accuracy_meters=18,
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
            is_active=False,
            created_by=self.intern_user,
        )
        self.client.force_login(self.company_user)
        url = reverse("portal:company_worksites")

        queue = self.client.get(url)
        self.assertNotContains(queue, "Intern-requested office")
        self.assertNotContains(queue, ">Approve<")
        company_attempt = self.client.post(url, {
            "action": "review_intern_request",
            "decision": "approve",
            "worksite_id": worksite.pk,
        })

        self.assertEqual(company_attempt.status_code, 403)
        worksite.refresh_from_db()
        self.assertEqual(worksite.approval_status, WorksiteLocation.ApprovalStatus.PENDING)
        self.assertFalse(worksite.is_active)

        admin = User.objects.create_superuser(username="worksite-admin", password="Safe-example-Password-927!")
        self.client.force_login(admin)
        approve_url = reverse("portal:admin_approvals")
        approved = self.client.post(approve_url, {
            "action": "approve_worksite",
            "request_id": worksite.pk,
        })

        self.assertRedirects(approved, approve_url)
        worksite.refresh_from_db()
        self.assertEqual(worksite.approval_status, WorksiteLocation.ApprovalStatus.APPROVED)
        self.assertTrue(worksite.is_active)
        self.assertEqual(worksite.reviewed_by, admin)

        coordinator = User.objects.create_user(username="not-admin-coordinator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)
        self.assertEqual(self.client.get(approve_url).status_code, 200)
        reset_url = reverse("portal:coordinator_reset_user_worksite", args=(self.intern.pk,))
        self.assertRedirects(self.client.post(
            reset_url,
            {"review_reason": "Please submit an updated worksite address."},
        ), reverse("portal:coordinator_users"))
        worksite.refresh_from_db()
        self.assertFalse(worksite.is_active)

        self.client.force_login(admin)
        self.assertEqual(self.client.get(approve_url).status_code, 200)
        self.assertRedirects(self.client.post(
            reset_url,
            {"review_reason": "Please submit an updated worksite address."},
        ), reverse("portal:coordinator_users"))

        self.client.force_login(self.intern_user)
        response = self.client.post(reverse("portal:intern_worksite_registration"), {
            "application": Application.objects.get(intern=self.intern).pk,
            "name": "Replacement OJT office",
            "address": "40 Training Avenue",
            "latitude": "14.710000",
            "longitude": "121.110000",
            "reported_accuracy_meters": "12",
            "confirm_location": "on",
        })
        self.assertRedirects(response, reverse("portal:intern_worksite_registration"))
        self.assertTrue(WorksiteLocation.objects.filter(
            intern=self.intern,
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
        ).exists())

    def test_external_worksite_requires_intern_submission_before_admin_approval(self):
        self._complete_intern_details(external=True)
        coordinator = User.objects.create_user(username="external-site-reviewer", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)
        external_sites_url = reverse("portal:coordinator_external_worksites")
        admin_created = self.client.post(external_sites_url, {
            "action": "create",
            "name": "External OJT site",
            "address": "50 Placement Road",
            "latitude": "14.700000",
            "longitude": "121.100000",
            "radius_meters": "100",
            "reported_accuracy_meters": "24",
            "confirm_location": "on",
        })
        self.assertEqual(admin_created.status_code, 403)
        self.assertFalse(WorksiteLocation.objects.filter(intern=self.intern).exists())

        self.client.force_login(self.intern_user)
        submitted = self.client.post(reverse("portal:intern_worksite_registration"), {
            "name": "External OJT site",
            "address": "50 Placement Road",
            "latitude": "14.700000",
            "longitude": "121.100000",
            "reported_accuracy_meters": "24",
            "confirm_location": "on",
        })
        self.assertRedirects(submitted, reverse("portal:intern_worksite_registration"))
        worksite = WorksiteLocation.objects.get(intern=self.intern)
        self.assertEqual(worksite.approval_status, WorksiteLocation.ApprovalStatus.PENDING)

        self.client.force_login(coordinator)
        approve_url = reverse("portal:admin_approvals")
        self.assertContains(self.client.get(approve_url), "External OJT site")
        approved = self.client.post(reverse("portal:admin_approvals"), {
            "action": "approve_worksite",
            "request_id": worksite.pk,
        })
        self.assertRedirects(approved, approve_url)
        worksite.refresh_from_db()
        self.assertEqual(worksite.approval_status, WorksiteLocation.ApprovalStatus.APPROVED)
        self.assertTrue(worksite.is_active)
        self.assertEqual(worksite.reviewed_by, coordinator)
        self.client.force_login(self.company_user)
        self.assertEqual(self.client.get(approve_url).status_code, 403)
        self.assertEqual(self.client.post(approve_url, {
            "action": "reject_worksite",
            "request_id": worksite.pk,
            "review_reason": "Company must not review.",
        }).status_code, 403)
        elevated_company = User.objects.create_user(
            username="company-staff-no-admin",
            role=User.Role.COMPANY,
            is_staff=True,
        )
        self.client.force_login(elevated_company)
        self.assertEqual(self.client.get(approve_url).status_code, 403)
        self.assertEqual(self.client.get(external_sites_url).status_code, 403)
        company_page = self.client.get(reverse("portal:company_worksites"))
        self.assertEqual(company_page.status_code, 200)
        self.assertNotContains(company_page, "Admin approvals")

    def test_admin_worksite_review_displays_map_and_requires_reason_for_corrections(self):
        self._complete_intern_details()
        application = Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        worksite = WorksiteLocation.objects.create(
            intern=self.intern,
            application=application,
            name="Review this OJT site",
            address="80 Admin Review Road",
            latitude="14.700000",
            longitude="121.100000",
            reported_accuracy_meters=22,
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
            is_active=False,
            created_by=self.intern_user,
        )
        admin = User.objects.create_user(username="worksite-corrections-admin", role=User.Role.COORDINATOR)
        self.client.force_login(admin)
        approval_url = reverse("portal:admin_approvals")

        page = self.client.get(approval_url)

        self.assertContains(page, self.intern_user.username)
        self.assertContains(page, self.company.company_name)
        self.assertContains(page, worksite.address)
        self.assertContains(page, "data-latitude=\"14.700000\"")
        self.assertContains(page, "data-longitude=\"121.100000\"")
        self.assertContains(page, "OpenStreetMap")
        self.assertContains(page, "tile.openstreetmap.fr/hot")
        self.assertContains(page, "OpenStreetMap France")
        self.assertContains(page, "Pending")
        self.assertContains(page, "±22 m")

        missing_reason = self.client.post(approval_url, {
            "action": "reject_worksite",
            "request_id": worksite.pk,
        })
        self.assertEqual(missing_reason.status_code, 400)
        worksite.refresh_from_db()
        self.assertEqual(worksite.approval_status, WorksiteLocation.ApprovalStatus.PENDING)

        requested = self.client.post(approval_url, {
            "action": "request_worksite_corrections",
            "request_id": worksite.pk,
            "review_reason": "Please confirm the entrance address.",
        })

        self.assertRedirects(requested, approval_url)
        worksite.refresh_from_db()
        self.assertEqual(worksite.approval_status, WorksiteLocation.ApprovalStatus.RESUBMISSION_REQUESTED)
        self.assertFalse(worksite.is_active)
        self.assertEqual(worksite.rejection_reason, "Please confirm the entrance address.")
        self.assertEqual(worksite.reviewed_by, admin)
        self.client.force_login(self.intern_user)
        intern_profile = self.client.get(reverse("portal:intern_worksite_registration"))
        self.assertContains(intern_profile, "Please confirm the entrance address.")
        self.assertContains(intern_profile, 'id="intern-worksite-request-form"')
        self.assertContains(intern_profile, '<details class="worksite-request-details" open>')

        corrected_address = "Richwell Colleges, Plaridel, Bulacan, 3004, Philippines"
        corrected_submission = self.client.post(reverse("portal:intern_worksite_registration"), {
            "application": application.pk,
            "name": "Corrected OJT site",
            "address": corrected_address,
            "latitude": "14.710000",
            "longitude": "121.110000",
            "reported_accuracy_meters": "18",
            "confirm_location": "on",
        })
        self.assertRedirects(corrected_submission, reverse("portal:intern_worksite_registration"))
        corrected_worksite = WorksiteLocation.objects.get(name="Corrected OJT site")
        self.assertEqual(corrected_worksite.address, corrected_address)
        self.assertEqual(corrected_worksite.approval_status, WorksiteLocation.ApprovalStatus.PENDING)
        self.assertFalse(corrected_worksite.is_active)
        profile_with_correction = self.client.get(reverse("portal:intern_worksite_registration"))
        self.assertContains(profile_with_correction, "Pending Admin Approval")
        self.assertContains(profile_with_correction, corrected_address)
        self.client.force_login(admin)
        rejected = self.client.post(approval_url, {
            "action": "reject_worksite",
            "request_id": corrected_worksite.pk,
            "review_reason": "The map pin does not match the listed entrance.",
        })
        self.assertRedirects(rejected, approval_url)
        corrected_worksite.refresh_from_db()
        self.assertEqual(corrected_worksite.approval_status, WorksiteLocation.ApprovalStatus.REJECTED)
        self.client.force_login(self.intern_user)
        rejected_page = self.client.get(reverse("portal:intern_worksite_registration"))
        self.assertContains(rejected_page, "The map pin does not match the listed entrance.")
        self.assertContains(rejected_page, 'id="intern-worksite-request-form"')
        replacement = self.client.post(reverse("portal:intern_worksite_registration"), {
            "application": application.pk,
            "name": "Second corrected OJT site",
            "address": "Richwell Colleges, Plaridel, Bulacan, 3004, Philippines",
            "latitude": "14.720000",
            "longitude": "121.120000",
            "reported_accuracy_meters": "18",
            "confirm_location": "on",
        })
        self.assertRedirects(replacement, reverse("portal:intern_worksite_registration"))
        self.assertEqual(
            WorksiteLocation.objects.filter(
                intern=self.intern,
                approval_status=WorksiteLocation.ApprovalStatus.PENDING,
            ).count(),
            1,
        )

    def test_admin_approval_replaces_previous_intern_site_without_deleting_it(self):
        self._complete_intern_details()
        application = Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        previous = WorksiteLocation.objects.create(
            intern=self.intern,
            application=application,
            name="Previous approved location",
            address="1 Old Road",
            latitude="14.600000",
            longitude="121.000000",
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
            is_active=True,
        )
        request = WorksiteLocation.objects.create(
            intern=self.intern,
            application=application,
            name="New approved location",
            address="2 New Road",
            latitude="14.700000",
            longitude="121.100000",
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
            is_active=False,
            created_by=self.intern_user,
        )
        admin = User.objects.create_user(username="worksite-replacement-admin", role=User.Role.COORDINATOR)
        self.client.force_login(admin)

        response = self.client.post(reverse("portal:admin_approvals"), {
            "action": "approve_worksite",
            "request_id": request.pk,
            "radius_meters": "150",
        })

        self.assertRedirects(response, reverse("portal:admin_approvals"))
        request.refresh_from_db()
        previous.refresh_from_db()
        self.assertEqual(request.approval_status, WorksiteLocation.ApprovalStatus.APPROVED)
        self.assertTrue(request.is_active)
        self.assertEqual(request.radius_meters, 150)
        self.assertEqual(previous.approval_status, WorksiteLocation.ApprovalStatus.APPROVED)
        self.assertFalse(previous.is_active)
        self.assertTrue(WorksiteLocation.objects.filter(pk=previous.pk).exists())
        self.client.force_login(self.intern_user)
        intern_profile = self.client.get(reverse("portal:intern_worksite_registration"))
        self.assertContains(intern_profile, "2 New Road")
        self.assertContains(intern_profile, "Official geofence radius: 150 m")
        self.assertNotContains(intern_profile, 'id="intern-worksite-request-form"')

        self.client.force_login(admin)
        correction = self.client.post(reverse("portal:admin_approvals"), {
            "action": "request_worksite_corrections",
            "request_id": request.pk,
            "review_reason": "Please verify the building entrance before resubmitting.",
        })
        self.assertRedirects(correction, reverse("portal:admin_approvals"))
        request.refresh_from_db()
        self.assertEqual(request.approval_status, WorksiteLocation.ApprovalStatus.RESUBMISSION_REQUESTED)
        self.assertFalse(request.is_active)
        self.assertTrue(WorksiteLocation.objects.filter(pk=request.pk).exists())
        self.client.force_login(self.intern_user)
        correction_page = self.client.get(reverse("portal:intern_worksite_registration"))
        self.assertContains(correction_page, "Please verify the building entrance before resubmitting.")
        self.assertContains(correction_page, 'id="intern-worksite-request-form"')
        resubmitted = self.client.post(reverse("portal:intern_worksite_registration"), {
            "application": application.pk,
            "name": "Corrected location",
            "address": "3 New Road",
            "latitude": "14.710000",
            "longitude": "121.110000",
            "reported_accuracy_meters": "18",
            "confirm_location": "on",
        })
        self.assertRedirects(resubmitted, reverse("portal:intern_worksite_registration"))
        self.assertEqual(
            WorksiteLocation.objects.filter(
                intern=self.intern,
                approval_status=WorksiteLocation.ApprovalStatus.PENDING,
            ).count(),
            1,
        )
        self.assertTrue(WorksiteLocation.objects.filter(pk=request.pk).exists())

    def test_unapproved_company_worksite_cannot_be_used_for_attendance(self):
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        WorksiteLocation.objects.create(
            company=self.company,
            name="Pending office",
            address="Pending Road",
            latitude="14.600000",
            longitude="121.000000",
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
            is_active=False,
        )
        self.client.force_login(self.intern_user)

        preview = self.client.post(
            reverse("portal:attendance_face_preview"),
            self._attendance_payload("clock_in", face_image=self._camera_data_url()),
        )

        self.assertEqual(preview.status_code, 409)
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())
        attendance = self.client.post(
            reverse("portal:attendance"),
            self._attendance_payload("clock_in"),
        )
        self.assertRedirects(attendance, reverse("portal:attendance"))
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())

    def test_platform_attendance_uses_only_approved_intern_submissions(self):
        self._complete_intern_profile()
        personal_site = self._enroll_attendance_location()
        company_site = WorksiteLocation.objects.create(
            company=self.company,
            name="Registered company site",
            address="2 Registered Road",
            latitude="14.700000",
            longitude="121.100000",
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
            is_active=True,
        )
        self.assertEqual(list(_worksites_for_attendance(self.intern)), [personal_site])
        personal_site.is_active = False
        personal_site.save(update_fields=("is_active",))
        self.assertFalse(_worksites_for_attendance(self.intern).exists())
        self.assertTrue(WorksiteLocation.objects.filter(
            pk=company_site.pk,
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
            is_active=True,
        ).exists())
        self.client.force_login(self.intern_user)

        response = self.client.post(
            reverse("portal:attendance_face_preview"),
            self._attendance_payload("clock_in", face_image=self._camera_data_url()),
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())

    def test_attendance_ignores_intern_supplied_company_coordinates_and_flags(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        other_company_user = User.objects.create_user(
            username="other-worksite-company",
            role=User.Role.COMPANY,
        )
        other_company = CompanyProfile.objects.get(user=other_company_user)
        foreign_site = WorksiteLocation.objects.create(
            company=other_company,
            name="Other company's office",
            address="1 Foreign Road",
            latitude="15.000000",
            longitude="122.000000",
            is_active=True,
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
        )
        self.client.force_login(self.intern_user)

        response = self.client.post(reverse("portal:attendance"), {
            "action": "clock_in",
            "company_latitude": "14.600000",
            "company_longitude": "121.000000",
            "location_verified": "true",
            "distance": "0",
        })

        self.assertRedirects(response, reverse("portal:attendance"))
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())
        foreign_location_attempt = self.client.post(
            reverse("portal:attendance"),
            self._attendance_payload(
                "clock_in",
                clock_in_latitude=str(foreign_site.latitude),
                clock_in_longitude=str(foreign_site.longitude),
                company_id=other_company.pk,
                company_latitude=str(foreign_site.latitude),
                company_longitude=str(foreign_site.longitude),
            ),
        )
        self.assertRedirects(foreign_location_attempt, reverse("portal:attendance"))
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())
        invalid_position = self.client.post(
            reverse("portal:attendance"),
            self._attendance_payload(
                "clock_in",
                clock_in_latitude="NaN",
                clock_in_longitude="Infinity",
            ),
        )
        self.assertRedirects(invalid_position, reverse("portal:attendance"))
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())

    def test_academic_period_rejects_reversed_or_overlapping_ranges(self):
        prelim = AcademicPeriod.objects.create(
            academic_year="2026-2027",
            period=AcademicPeriod.Period.PRELIM,
            starts_on="2026-08-01",
            ends_on="2026-09-01",
        )
        overlapping = AcademicPeriod(
            academic_year="2026-2027",
            period=AcademicPeriod.Period.MIDTERM,
            starts_on="2026-09-01",
            ends_on="2026-10-01",
        )

        with self.assertRaises(ValidationError):
            overlapping.full_clean()
        self.assertEqual(prelim.period, AcademicPeriod.Period.PRELIM)

        reversed_range = AcademicPeriod(
            academic_year="2026-2027",
            period=AcademicPeriod.Period.FINAL,
            starts_on="2026-12-01",
            ends_on="2026-11-30",
        )
        with self.assertRaises(ValidationError):
            reversed_range.full_clean()

    def test_dtr_period_filter_weeks_and_486_hour_cap(self):
        periods = [
            AcademicPeriod.objects.create(
                academic_year="2026-2027",
                period=period,
                starts_on=start,
                ends_on=end,
            )
            for period, start, end in (
                (AcademicPeriod.Period.PRELIM, "2026-01-05", "2026-01-05"),
                (AcademicPeriod.Period.MIDTERM, "2026-01-06", "2026-01-06"),
                (AcademicPeriod.Period.PRE_FINAL, "2026-01-07", "2026-01-07"),
                (AcademicPeriod.Period.FINAL, "2026-01-08", "2026-01-08"),
            )
        ]
        for day, hours in enumerate((250, 250, 12, 8), start=5):
            start = datetime(2026, 1, day, 8, tzinfo=datetime_timezone.utc)
            AttendanceLog.objects.create(
                intern=self.intern,
                work_date=f"2026-01-{day:02d}",
                clock_in=start,
                clock_out=start + timedelta(hours=hours),
                time_in_approved=True,
                time_out_approved=True,
            )
        approved_logs = AttendanceLog.objects.filter(intern=self.intern)
        weekly_aggregate = list(approved_logs.approved_hours_by_week())
        self.assertEqual(len(weekly_aggregate), 1)
        self.assertEqual(weekly_aggregate[0]["total_duration"], timedelta(hours=520))
        self.assertEqual(
            approved_logs.approved_hours_for_period(periods[0].starts_on, periods[0].ends_on),
            timedelta(hours=250),
        )
        self.client.force_login(self.intern_user)

        all_periods = self.client.get(reverse("portal:attendance"))

        self.assertEqual(all_periods.status_code, 200)
        self.assertEqual(all_periods.context["approved_rendered_hours"], 486)
        self.assertEqual(
            [entry["hours"] for entry in all_periods.context["period_totals"]],
            [250, 236, 0, 0],
        )
        self.assertEqual(len(all_periods.context["weekly_groups"]), 1)
        self.assertEqual(all_periods.context["weekly_groups"][0]["hours"], 486)
        self.assertContains(all_periods, "Week 1")
        self.assertContains(all_periods, "Location")

        selected_period = self.client.get(
            reverse("portal:attendance"),
            {"period": periods[1].pk},
        )
        self.assertEqual(len(selected_period.context["weekly_groups"]), 1)
        self.assertEqual(selected_period.context["weekly_groups"][0]["hours"], 236)
        self.assertEqual(len(selected_period.context["weekly_groups"][0]["logs"]), 1)
        self.assertEqual(selected_period.context["weekly_groups"][0]["logs"][0].rendered_hours, 236)

    def test_pending_attendance_does_not_count_toward_dtr_progress(self):
        start = datetime(2026, 1, 5, 8, tzinfo=datetime_timezone.utc)
        AttendanceLog.objects.create(
            intern=self.intern,
            work_date="2026-01-05",
            clock_in=start,
            clock_out=start + timedelta(hours=8),
            time_in_approved=True,
            time_out_approved=False,
        )
        self.client.force_login(self.intern_user)

        response = self.client.get(reverse("portal:intern_dashboard"))

        self.assertEqual(response.context["completed_hours"], 0)
        self.assertEqual(response.context["completion_percent"], 0)

    def test_coordinator_dtr_summary_filters_and_caps_approved_hours(self):
        self._complete_intern_details()
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        start = datetime(2026, 1, 5, 8, tzinfo=datetime_timezone.utc)
        AttendanceLog.objects.create(
            intern=self.intern,
            work_date="2026-01-05",
            clock_in=start,
            clock_out=start + timedelta(hours=500),
            time_in_approved=True,
            time_out_approved=True,
        )
        coordinator = User.objects.create_user(username="dtr-summary-coordinator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)

        response = self.client.get(reverse("portal:coordinator_dtr_summary"), {
            "q": "TEST-STUDENT-001",
            "placement": InternProfile.PlacementType.PLATFORM,
            "company": self.company.pk,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["intern_page"].paginator.count, 1)
        listed_intern = response.context["intern_page"].object_list[0]
        self.assertEqual(listed_intern.approved_rendered_hours, 486)
        self.assertEqual(listed_intern.completion_percent, 100)
        self.assertContains(response, "486.00 / 486 h")

    def test_coordinator_dtr_summary_paginates_and_filters_external_hosts(self):
        for index in range(26):
            User.objects.create_user(username=f"dtr-roster-{index}", role=User.Role.INTERN)
        external_user = User.objects.create_user(username="external-dtr-intern", role=User.Role.INTERN)
        external_intern = InternProfile.objects.get(user=external_user)
        external_intern.placement_type = InternProfile.PlacementType.EXTERNAL
        external_intern.external_host = "Northside Training"
        external_intern.save(update_fields=("placement_type", "external_host"))
        coordinator = User.objects.create_user(username="dtr-paginator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)

        page_two = self.client.get(reverse("portal:coordinator_dtr_summary"), {"page": 2})

        self.assertEqual(page_two.context["intern_page"].paginator.count, 28)
        self.assertEqual(len(page_two.context["intern_page"].object_list), 3)
        self.assertContains(page_two, "Page 2 of 2")
        external_filter = self.client.get(
            reverse("portal:coordinator_dtr_summary"),
            {"placement": InternProfile.PlacementType.EXTERNAL, "host": "Northside Training"},
        )
        self.assertEqual(external_filter.context["intern_page"].paginator.count, 1)
        self.assertContains(external_filter, "Northside Training")

    def test_coordinator_user_directory_filters_mixed_roles_and_lists_company(self):
        self._complete_intern_details()
        self.intern.student_id = "DIRECTORY-100"
        self.intern.save(update_fields=("student_id",))
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        FaceEnrollment.objects.create(
            intern=self.intern,
            encrypted_embedding=b"encrypted",
            enrolled_by=self.intern_user,
            consent_confirmed_at=timezone.now(),
        )
        WorksiteLocation.objects.create(
            intern=self.intern,
            name="Directory site",
            address="1 Directory Road",
            latitude="14.600000",
            longitude="121.000000",
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
            is_active=True,
        )
        coordinator = User.objects.create_user(username="directory-admin", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)
        url = reverse("portal:coordinator_users")

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["users_page"].paginator.per_page, 15)
        self.assertContains(response, "User Directory")
        self.assertContains(response, "DIRECTORY-100")
        self.assertContains(response, self.company.organization)
        self.assertContains(response, "Enrolled")
        self.assertContains(response, "View DTR")
        self.assertContains(response, "Reset Face")
        self.assertContains(response, "Request Worksite Correction")

        search = self.client.get(url, {"q": "DIRECTORY-100"})
        self.assertEqual(search.context["users_page"].paginator.count, 1)
        self.assertEqual(search.context["users_page"].object_list[0].pk, self.intern_user.pk)
        company_filter = self.client.get(url, {"role": User.Role.COMPANY})
        self.assertEqual(company_filter.context["users_page"].paginator.count, 1)
        admin_filter = self.client.get(url, {"role": "admin"})
        self.assertEqual(admin_filter.context["users_page"].paginator.count, 1)
        self.assertEqual(admin_filter.context["users_page"].object_list[0].pk, coordinator.pk)
        placement_filter = self.client.get(url, {"placement": InternProfile.PlacementType.EXTERNAL})
        self.assertEqual(placement_filter.context["users_page"].paginator.count, 0)
        enrolled_filter = self.client.get(url, {"face": "enrolled"})
        self.assertEqual(enrolled_filter.context["users_page"].paginator.count, 1)
        face_filter = self.client.get(url, {"face": "not_enrolled"})
        self.assertNotIn(self.intern_user.pk, [user.pk for user in face_filter.context["users_page"]])

    def test_coordinator_user_directory_paginates_at_fifteen(self):
        for index in range(14):
            User.objects.create_user(username=f"directory-intern-{index}", role=User.Role.INTERN)
        coordinator = User.objects.create_user(username="directory-paginator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)

        first_page = self.client.get(reverse("portal:coordinator_users"))

        self.assertEqual(first_page.context["users_page"].paginator.count, 17)
        self.assertEqual(len(first_page.context["users_page"].object_list), 15)
        self.assertContains(first_page, "Page 1 of 2")
        second_page = self.client.get(reverse("portal:coordinator_users"), {"page": 2})
        self.assertEqual(len(second_page.context["users_page"].object_list), 2)

    def test_admin_role_can_reset_face_and_worksite_from_user_directory(self):
        self._complete_intern_details(external=True)
        enrollment = FaceEnrollment.objects.create(
            intern=self.intern,
            encrypted_embedding=b"encrypted",
            enrolled_by=self.intern_user,
            consent_confirmed_at=timezone.now(),
        )
        log = AttendanceLog.objects.create(
            intern=self.intern,
            clock_in_face_status=AttendanceLog.FaceCheckStatus.MATCHED,
            clock_in_face_score=0.8,
        )
        capture = AttendanceFaceCapture.objects.create(
            attendance_log=log,
            event=AttendanceFaceCapture.Event.TIME_IN,
            face_status=AttendanceLog.FaceCheckStatus.MATCHED,
            encrypted_image=b"encrypted image",
        )
        active_site = WorksiteLocation.objects.create(
            intern=self.intern,
            name="Approved site",
            address="1 Road",
            latitude="14.600000",
            longitude="121.000000",
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
            is_active=True,
        )
        pending_site = WorksiteLocation.objects.create(
            intern=self.intern,
            name="Pending site",
            address="2 Road",
            latitude="14.610000",
            longitude="121.010000",
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
            is_active=False,
        )
        coordinator = User.objects.create_user(username="directory-reset-admin", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)

        reset_face = self.client.post(
            reverse("portal:coordinator_reset_user_face", args=(self.intern.pk,)),
        )
        self.assertRedirects(reset_face, reverse("portal:coordinator_users"))
        self.assertFalse(FaceEnrollment.objects.filter(pk=enrollment.pk).exists())
        self.assertTrue(AttendanceFaceCapture.objects.filter(pk=capture.pk).exists())
        log.refresh_from_db()
        self.assertEqual(log.clock_in_face_status, AttendanceLog.FaceCheckStatus.MATCHED)
        self.assertEqual(log.clock_in_face_score, 0.8)
        archived_enrollment = FaceEnrollmentSubmission.objects.get(intern=self.intern)
        self.assertEqual(archived_enrollment.status, FaceEnrollmentSubmission.Status.REJECTED)
        self.assertEqual(archived_enrollment.encrypted_embedding, b"encrypted")

        reset_worksite = self.client.post(
            reverse("portal:coordinator_reset_user_worksite", args=(self.intern.pk,)),
            {"review_reason": "Please submit your current worksite."},
        )

        self.assertRedirects(reset_worksite, reverse("portal:coordinator_users"))
        active_site.refresh_from_db()
        pending_site.refresh_from_db()
        self.assertFalse(active_site.is_active)
        self.assertFalse(pending_site.is_active)
        self.assertEqual(pending_site.approval_status, WorksiteLocation.ApprovalStatus.RESUBMISSION_REQUESTED)

        superuser = User.objects.create_superuser(
            username="directory-reset-superuser",
            password="Safe-example-Password-927!",
        )
        self.client.force_login(superuser)
        self.assertEqual(
            self.client.get(reverse("portal:admin_approvals")).status_code,
            200,
        )

    def test_user_directory_and_reset_actions_are_coordinator_only(self):
        self.client.force_login(self.intern_user)

        response = self.client.get(reverse("portal:coordinator_users"))
        reset = self.client.post(
            reverse("portal:coordinator_reset_user_worksite", args=(self.intern.pk,)),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(reset.status_code, 403)

    def test_company_dtr_detail_is_limited_to_accepted_company_interns(self):
        self._complete_intern_details()
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        self.client.force_login(self.company_user)

        response = self.client.get(
            reverse("portal:company_dtr_detail", args=(self.intern.pk,)),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Weekly DTR breakdown")

    def test_role_dashboard_routes_use_role_specific_paths(self):
        self.assertEqual(reverse("portal:intern_dashboard"), "/intern/dashboard/")
        self.assertEqual(reverse("portal:company_dashboard"), "/supervisor/dashboard/")
        self.assertEqual(reverse("portal:coordinator_dashboard"), "/admin/dashboard/")

    def test_superuser_without_staff_flag_is_sent_to_admin_dashboard(self):
        superuser = User.objects.create_user(
            username="dashboard-superuser",
            password="Safe-example-Password-927!",
            is_superuser=True,
            is_staff=False,
        )
        self.client.force_login(superuser)

        response = self.client.get(reverse("portal:dashboard"))

        self.assertRedirects(response, reverse("portal:coordinator_dashboard"))

    def test_nonstaff_superuser_is_not_treated_as_an_intern_by_face_api(self):
        superuser = User.objects.create_user(
            username="face-api-superuser",
            password="Safe-example-Password-927!",
            is_superuser=True,
            is_staff=False,
        )
        self.client.force_login(superuser)

        response = self.client.post(reverse("portal:face_detection_preview"), {})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "Only interns can use face capture.")

    def test_current_navigation_item_is_highlighted(self):
        self._complete_intern_profile()
        self.client.force_login(self.intern_user)

        response = self.client.get(reverse("portal:intern_dashboard"))

        self.assertContains(
            response,
            'href="/intern/dashboard/" class="nav-link-active" aria-current="page"',
        )
        self.assertContains(response, "Overview</a>")

    def test_intern_can_update_external_host_details_later(self):
        form = InternProfileForm(
            {
                "placement_type": InternProfile.PlacementType.EXTERNAL,
                "external_host": "Northside Design Studio",
                "student_id": "STUDENT-201",
                "university": "Example University",
                "course": "Design",
                "year_level": 4,
            },
            instance=self.intern,
        )

        self.assertTrue(form.is_valid(), form.errors)
        profile = form.save()
        self.assertEqual(profile.placement_type, InternProfile.PlacementType.EXTERNAL)
        self.assertEqual(profile.external_host, "Northside Design Studio")

        draft_form = InternProfileForm(
            {
                "placement_type": InternProfile.PlacementType.EXTERNAL,
                "external_host": "",
                "student_id": "STUDENT-202",
                "university": "Example University",
                "course": "Design",
                "year_level": 4,
            },
            instance=profile,
        )
        self.assertTrue(draft_form.is_valid(), draft_form.errors)
        draft_profile = draft_form.save()
        self.assertEqual(draft_profile.external_host, "")

    def test_intern_profile_year_level_only_accepts_first_through_fourth_year(self):
        form = InternProfileForm(
            {
                "student_id": "",
                "university": "",
                "course": "",
                "year_level": "5",
                "placement_type": InternProfile.PlacementType.PLATFORM,
                "external_host": "",
            },
            instance=self.intern,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("year_level", form.errors)

    def test_intern_can_save_incomplete_profile_without_requirement_gate(self):
        self.client.force_login(self.intern_user)
        response = self.client.post(reverse("portal:profile"), {
            "student_id": "",
            "university": "",
            "course": "",
            "year_level": "",
            "placement_type": InternProfile.PlacementType.PLATFORM,
            "external_host": "",
        })

        self.assertRedirects(response, reverse("portal:profile"))
        self.intern.refresh_from_db()
        self.assertFalse(self.intern.student_id)
        self.assertEqual(self.intern.university, "Richwell Colleges, Incorporated")
        self.assertFalse(self.intern.course)
        self.assertIsNone(self.intern.year_level)
        self.assertEqual(self.client.get(reverse("portal:attendance")).status_code, 200)

    def test_intern_can_save_complete_profile_from_profile_page(self):
        self.intern.university = "Legacy University"
        self.intern.save(update_fields=("university",))
        self.client.force_login(self.intern_user)

        response = self.client.post(reverse("portal:profile"), {
            "student_id": "TEST-STUDENT-SAVE",
            "university": "Different University",
            "course": "Information Technology",
            "year_level": 3,
            "placement_type": InternProfile.PlacementType.PLATFORM,
            "external_host": "",
        })

        self.assertRedirects(response, reverse("portal:profile"))
        profile_page = self.client.get(reverse("portal:profile"))
        self.assertContains(profile_page, "Richwell Colleges, Incorporated")
        self.assertNotContains(profile_page, 'name="university"')
        self.intern.refresh_from_db()
        self.assertEqual(self.intern.student_id, "TEST-STUDENT-SAVE")
        self.assertEqual(self.intern.university, "Legacy University")
        self.assertEqual(self.intern.course, "Information Technology")
        self.assertEqual(self.intern.year_level, 3)

    def test_intern_cannot_change_an_accepted_company_placement_to_external(self):
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        self.client.force_login(self.intern_user)

        response = self.client.post(reverse("portal:profile"), {
            "student_id": "TEST-STUDENT-ASSIGNED",
            "university": "Example University",
            "course": "Information Technology",
            "year_level": 3,
            "placement_type": InternProfile.PlacementType.EXTERNAL,
            "external_host": "Untrusted host",
        })

        self.assertRedirects(response, reverse("portal:profile"))
        self.intern.refresh_from_db()
        self.assertEqual(self.intern.placement_type, InternProfile.PlacementType.PLATFORM)
        self.assertEqual(self.intern.external_host, "")

    def test_profile_save_failure_is_logged_and_rolled_back(self):
        self.client.force_login(self.intern_user)

        with patch("portal.views.audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertLogs("portal.views", level="ERROR") as captured_logs:
                with self.assertRaises(RuntimeError):
                    self.client.post(reverse("portal:profile"), {
                        "student_id": "TEST-STUDENT-ROLLBACK",
                        "university": "Example University",
                        "course": "Information Technology",
                        "year_level": 3,
                        "placement_type": InternProfile.PlacementType.PLATFORM,
                        "external_host": "",
                    })

        self.assertIn("Profile update failed for intern user", captured_logs.output[0])
        self.intern.refresh_from_db()
        self.assertIsNone(self.intern.student_id)

    def test_coordinator_dashboard_shows_attendance_and_external_worksite_status(self):
        self.intern.placement_type = InternProfile.PlacementType.EXTERNAL
        self.intern.external_host = "Northside Design Studio"
        self.intern.save(update_fields=("placement_type", "external_host"))
        clock_in = datetime(2026, 10, 5, 8, tzinfo=datetime_timezone.utc)
        AttendanceLog.objects.create(
            intern=self.intern,
            clock_in=clock_in,
            clock_out=clock_in.replace(hour=16),
            time_in_approved=True,
            time_out_approved=True,
        )
        coordinator = User.objects.create_user(username="coordinator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)

        response = self.client.get(reverse("portal:coordinator_dashboard"))

        self.assertContains(response, "External interns without a worksite")
        self.assertContains(response, "Assign external worksite locations")
        self.assertContains(response, "Daily reports to review")

    def test_external_intern_daily_report_is_reviewed_by_coordinator(self):
        self._complete_intern_profile(external=True)
        self.intern.external_host = "Northside Design Studio"
        self.intern.save(update_fields=("external_host",))
        self.client.force_login(self.intern_user)

        response = self.client.post(
            reverse("portal:daily_report"),
            {
                "accomplishments": "Prepared three design concepts.",
                "challenges": "Waiting for access to the shared drive.",
            },
        )

        self.assertRedirects(response, reverse("portal:daily_report"))
        report = DailyReport.objects.get(intern=self.intern)
        self.assertEqual(report.status, DailyReport.Status.SUBMITTED)

        coordinator = User.objects.create_user(username="coordinator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)
        queue = self.client.get(reverse("portal:coordinator_daily_reports"))
        self.assertContains(queue, "Northside Design Studio")
        reviewed = self.client.post(
            reverse("portal:review_daily_report", args=(report.pk,)),
            {"decision": "review", "supervisor_feedback": "Good progress."},
        )

        self.assertRedirects(reviewed, reverse("portal:coordinator_daily_reports"))
        report.refresh_from_db()
        self.assertEqual(report.status, DailyReport.Status.REVIEWED)
        self.assertEqual(report.supervisor_feedback, "Good progress.")
        self.assertEqual(report.reviewer, coordinator)

    def test_platform_intern_can_submit_daily_report(self):
        self._complete_intern_profile()
        self.client.force_login(self.intern_user)

        response = self.client.post(
            reverse("portal:daily_report"),
            {
                "accomplishments": "Completed the assigned support tasks.",
                "challenges": "",
            },
        )

        self.assertRedirects(response, reverse("portal:daily_report"))
        report = DailyReport.objects.get(intern=self.intern)
        self.assertEqual(report.status, DailyReport.Status.SUBMITTED)

    def test_daily_report_submissions_are_immutable_and_visible_in_daily_weekly_histories(self):
        self._complete_intern_profile()
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        self.client.force_login(self.intern_user)
        report_url = reverse("portal:daily_report")
        first_submission = self.client.post(report_url, {
            "accomplishments": "Completed the inventory review.",
            "challenges": "",
        })
        self.assertRedirects(first_submission, report_url)
        first_report = DailyReport.objects.get(intern=self.intern)

        second_submission = self.client.post(report_url, {
            "accomplishments": "Prepared a handover summary.",
            "challenges": "Waiting for final sign-off.",
        })
        self.assertRedirects(second_submission, report_url)
        self.assertEqual(DailyReport.objects.filter(intern=self.intern).count(), 2)
        first_report.refresh_from_db()
        self.assertEqual(first_report.accomplishments, "Completed the inventory review.")

        now = timezone.now()
        AttendanceLog.objects.create(
            intern=self.intern,
            work_date=timezone.localdate(),
            clock_in=now - timedelta(hours=8),
            clock_out=now,
            clock_in_distance_meters=24,
            clock_in_allowed_radius_meters=100,
            clock_in_within_location=True,
            clock_out_distance_meters=31,
            clock_out_allowed_radius_meters=100,
            clock_out_within_location=True,
            time_in_approved=True,
            time_out_approved=True,
        )
        self.client.force_login(self.company_user)
        attendance_logs = self.client.get(reverse("portal:company_attendance_history"))
        self.assertContains(attendance_logs, "24 m / 100 m")
        self.assertContains(attendance_logs, "Inside")
        company_journals = self.client.get(reverse("portal:company_daily_reports"))
        self.assertContains(company_journals, "Completed the inventory review.")
        self.assertContains(company_journals, "Prepared a handover summary.")
        self.assertContains(company_journals, "8.00")

        weekly_journals = self.client.get(
            reverse("portal:company_daily_reports"),
            {"view": "weekly"},
        )
        self.assertContains(weekly_journals, "Weekly summary")
        self.assertContains(weekly_journals, "Week of")
        self.assertContains(weekly_journals, "8.00 approved hours")

        admin = User.objects.create_superuser(username="journal-admin", password="Safe-example-Password-927!")
        self.client.force_login(admin)
        all_reports = self.client.get(
            reverse("portal:admin_daily_reports"),
            {"q": "intern", "date": timezone.localdate().isoformat()},
        )
        self.assertContains(all_reports, "Daily and Weekly Intern Reports")
        self.assertContains(all_reports, "Search daily reports and weekly summaries from all interns.")
        self.assertContains(all_reports, "Daily Reports")
        self.assertContains(all_reports, "Review Pending Reports")
        self.assertContains(all_reports, reverse("portal:coordinator_daily_reports"))
        self.assertContains(all_reports, 'name="q"')
        self.assertContains(all_reports, 'name="date"')
        self.assertContains(all_reports, "2 daily reports")
        daily_content = all_reports.content.decode()
        daily_tabs_start = daily_content.index('<nav class="report-view-toggle"')
        daily_tabs = daily_content[daily_tabs_start:daily_content.index("</nav>", daily_tabs_start)]
        self.assertIn('button-primary" href="?view=daily', daily_tabs)
        self.assertIn('aria-current="page"', daily_tabs)
        self.assertContains(all_reports, "Completed the inventory review.")
        self.assertContains(all_reports, "Prepared a handover summary.")

        admin_weekly = self.client.get(
            reverse("portal:admin_daily_reports"),
            {"view": "weekly", "q": "intern", "week": timezone.localdate().isoformat()},
        )
        self.assertContains(admin_weekly, "Weekly Summaries")
        self.assertContains(admin_weekly, "1 weekly summary")
        self.assertContains(admin_weekly, "Week of")
        weekly_content = admin_weekly.content.decode()
        weekly_tabs_start = weekly_content.index('<nav class="report-view-toggle"')
        weekly_tabs = weekly_content[weekly_tabs_start:weekly_content.index("</nav>", weekly_tabs_start)]
        self.assertIn('button-primary" href="?view=weekly', weekly_tabs)
        self.assertIn('aria-current="page"', weekly_tabs)

        no_matches = self.client.get(
            reverse("portal:admin_daily_reports"),
            {"q": "no-matching-intern"},
        )
        self.assertContains(no_matches, "No daily reports found for the selected filters.")
        self.assertContains(no_matches, "0 daily reports")

    def test_external_intern_uses_coordinator_workflow_instead_of_company_opportunities(self):
        self._complete_intern_profile(external=True)
        self.intern.external_host = "Northside Design Studio"
        self.intern.save(update_fields=("external_host",))
        self.client.force_login(self.intern_user)

        page = self.client.get(reverse("portal:postings"))
        self.assertContains(page, "Your external placement is managed directly with the OJT Admin.")
        self.assertNotContains(page, self.posting.title)
        self.assertNotContains(page, ">Opportunities</a>")

        response = self.client.post(reverse("portal:apply", args=(self.posting.pk,)))

        self.assertRedirects(response, reverse("portal:postings"))
        self.assertFalse(Application.objects.filter(intern=self.intern).exists())


    def test_external_intern_attendance_appears_in_coordinator_dtr_queue(self):
        self.intern.placement_type = InternProfile.PlacementType.EXTERNAL
        self.intern.external_host = "Northside Design Studio"
        self.intern.save(update_fields=("placement_type", "external_host"))
        log = AttendanceLog.objects.create(intern=self.intern, work_date="2026-10-07", clock_in="2026-10-07T09:00:00Z")
        coordinator = User.objects.create_user(username="coordinator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)

        response = self.client.get(reverse("portal:dtr_queue"))

        self.assertContains(response, "External: Northside Design Studio")
        self.assertContains(response, "Approve time in")
        approved = self.client.post(
            reverse("portal:approve_dtr", args=(log.pk,)),
            {"event": "time_in"},
        )
        self.assertRedirects(approved, reverse("portal:dtr_queue"))
        log.refresh_from_db()
        self.assertTrue(log.time_in_approved)

    def test_company_dtr_queue_excludes_external_placements(self):
        self.intern.placement_type = InternProfile.PlacementType.EXTERNAL
        self.intern.external_host = "Northside Design Studio"
        self.intern.save(update_fields=("placement_type", "external_host"))
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        AttendanceLog.objects.create(
            intern=self.intern,
            work_date="2026-10-07",
            clock_in="2026-10-07T09:00:00Z",
        )
        self.client.force_login(self.company_user)

        response = self.client.get(reverse("portal:company_dtr_queue"))

        self.assertEqual(response.context["logs"].count(), 0)

    def test_new_intern_does_not_receive_ojt_requirements(self):
        other_user = User.objects.create_user(username="new-intern")
        other_intern = InternProfile.objects.get(user=other_user)

        self.assertFalse(other_intern.ojt_requirements.exists())

    def test_legacy_pages_redirect_to_core_workflows(self):
        self.client.force_login(self.intern_user)
        intern_page = self.client.get(reverse("portal:ojt_requirements"))
        self.assertRedirects(intern_page, reverse("portal:daily_report"))
        weekly_page = self.client.get(reverse("portal:reports"))
        self.assertRedirects(weekly_page, reverse("portal:daily_report"))

        self.client.force_login(self.company_user)
        company_page = self.client.get(reverse("portal:company_requirements"))
        self.assertRedirects(company_page, reverse("portal:company_dashboard"))

        coordinator = User.objects.create_user(username="coordinator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)
        coordinator_page = self.client.get(reverse("portal:coordinator_ojt_requirements"))
        self.assertRedirects(coordinator_page, reverse("portal:coordinator_dashboard"))
        weekly_queue = self.client.get(reverse("portal:coordinator_weekly_reports"))
        self.assertRedirects(weekly_queue, reverse("portal:coordinator_daily_reports"))

    def test_attendance_does_not_require_legacy_ojt_approval(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)

        with patch(
            "portal.views._attendance_face_check",
            return_value=(AttendanceLog.FaceCheckStatus.MATCHED, 0.82, b"encrypted frame"),
        ):
            response = self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload("clock_in"),
            )

        self.assertRedirects(response, reverse("portal:attendance"))
        self.assertTrue(AttendanceLog.objects.filter(intern=self.intern).exists())

    def test_outside_location_requires_reason_and_is_sent_for_supervisor_review(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)

        outside = self.client.post(
            reverse("portal:attendance"),
            self._attendance_payload(
                "clock_in",
                clock_in_latitude="14.700000",
            ),
        )
        self.assertRedirects(outside, reverse("portal:attendance"))
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())

        invalid_coordinates = self.client.post(
            reverse("portal:attendance"),
            self._attendance_payload("clock_in", clock_in_latitude="NaN"),
        )
        self.assertRedirects(invalid_coordinates, reverse("portal:attendance"))
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())

        with patch(
            "portal.views._attendance_face_check",
            return_value=(AttendanceLog.FaceCheckStatus.MATCHED, 0.82, b"encrypted frame"),
        ):
            flagged = self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload(
                    "clock_in",
                    clock_in_latitude="14.700000",
                    clock_in_location_reason="The office GPS signal was inaccurate today.",
                ),
            )

        self.assertRedirects(flagged, reverse("portal:attendance"))
        log = AttendanceLog.objects.get(intern=self.intern)
        self.assertEqual(str(log.clock_in_latitude), "14.700000")
        self.assertEqual(str(log.clock_in_longitude), "121.000000")
        self.assertFalse(log.clock_in_within_location)
        self.assertGreater(log.clock_in_distance_meters, log.clock_in_allowed_radius_meters)
        self.assertEqual(log.clock_in_location_reason, "The office GPS signal was inaccurate today.")
        self.assertFalse(log.time_in_approved)
        capture = log.face_captures.get(event=AttendanceFaceCapture.Event.TIME_IN)
        self.assertEqual(capture.location_status, AttendanceFaceCapture.LocationStatus.OUTSIDE)
        self.assertEqual(capture.review_status, AttendanceFaceCapture.ReviewStatus.PENDING)
        self.client.force_login(self.company_user)
        self.assertContains(self.client.get(reverse("portal:company_dtr_queue")), "The office GPS signal was inaccurate today.")

        coordinator = User.objects.create_user(username="location-approver", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)
        approved = self.client.post(
            reverse("portal:review_attendance_face_capture", args=(capture.pk,)),
            {"action": "approve"},
        )
        self.assertRedirects(approved, reverse("portal:dtr_queue"))
        log.refresh_from_db()
        self.assertTrue(log.time_in_approved)
        self.assertFalse(log.clock_in_within_location)

    def test_missing_gps_requires_reason_and_is_queued_as_location_exception(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)

        missing_reason = self.client.post(
            reverse("portal:attendance"),
            {
                "action": "clock_in",
                "clock_in_location_reason": " ",
                "face_image": self._camera_data_url(),
            },
        )
        self.assertRedirects(missing_reason, reverse("portal:attendance"))
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())

        with patch(
            "portal.views._attendance_face_check",
            return_value=(AttendanceLog.FaceCheckStatus.MATCHED, 0.82, b"encrypted frame"),
        ):
            flagged = self.client.post(
                reverse("portal:attendance"),
                {
                    "action": "clock_in",
                    "clock_in_location_reason": "Browser location permission was denied.",
                },
            )

        self.assertRedirects(flagged, reverse("portal:attendance"))
        log = AttendanceLog.objects.get(intern=self.intern)
        capture = log.face_captures.get(event=AttendanceFaceCapture.Event.TIME_IN)
        self.assertIsNone(log.clock_in_latitude)
        self.assertIsNone(log.clock_in_distance_meters)
        self.assertFalse(log.clock_in_within_location)
        self.assertEqual(capture.location_status, AttendanceFaceCapture.LocationStatus.UNAVAILABLE)
        self.assertEqual(capture.location_reason, "Browser location permission was denied.")
        self.assertFalse(log.time_in_approved)
        self.assertIsNotNone(log.clock_in)
        self.assertEqual(capture.review_status, AttendanceFaceCapture.ReviewStatus.PENDING)

    def test_attendance_face_preview_reports_location_state_and_reason_requirement(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)
        with patch(
            "portal.views._attendance_face_check",
            return_value=(AttendanceLog.FaceCheckStatus.MATCHED, 0.82, None),
        ):
            inside = self.client.post(
                reverse("portal:attendance_face_preview"),
                self._attendance_payload(
                    "clock_in",
                    face_image=self._camera_data_url(),
                ),
            )
            outside = self.client.post(
                reverse("portal:attendance_face_preview"),
                self._attendance_payload(
                    "clock_in",
                    clock_in_latitude="14.700000",
                    face_image=self._camera_data_url(),
                ),
            )
            inaccurate = self.client.post(
                reverse("portal:attendance_face_preview"),
                self._attendance_payload(
                    "clock_in",
                    clock_in_accuracy_meters="45",
                    face_image=self._camera_data_url(),
                ),
            )

        self.assertEqual(inside.status_code, 200)
        self.assertEqual(inside.json()["location_status"], AttendanceFaceCapture.LocationStatus.INSIDE)
        self.assertFalse(inside.json()["location_requires_reason"])
        self.assertEqual(outside.status_code, 200)
        self.assertEqual(outside.json()["location_status"], AttendanceFaceCapture.LocationStatus.OUTSIDE)
        self.assertTrue(outside.json()["location_requires_reason"])
        self.assertEqual(inaccurate.json()["location_status"], AttendanceFaceCapture.LocationStatus.UNAVAILABLE)
        self.assertTrue(inaccurate.json()["location_requires_reason"])
        self.assertEqual(inaccurate.json()["max_accuracy_meters"], 30)

    def test_poor_attendance_gps_accuracy_cannot_auto_approve(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)
        attendance_url = reverse("portal:attendance")
        payload = self._attendance_payload("clock_in", clock_in_accuracy_meters="45")

        rejected = self.client.post(attendance_url, payload)

        self.assertRedirects(rejected, attendance_url)
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())

        with patch(
            "portal.views._attendance_face_check",
            return_value=(AttendanceLog.FaceCheckStatus.MATCHED, 0.82, b"encrypted frame"),
        ):
            submitted_for_review = self.client.post(attendance_url, {
                **payload,
                "clock_in_location_reason": "GPS accuracy was unstable at the site.",
            })

        self.assertRedirects(submitted_for_review, attendance_url)
        log = AttendanceLog.objects.get(intern=self.intern)
        capture = log.face_captures.get(event=AttendanceFaceCapture.Event.TIME_IN)
        self.assertEqual(capture.location_status, AttendanceFaceCapture.LocationStatus.UNAVAILABLE)
        self.assertEqual(capture.review_status, AttendanceFaceCapture.ReviewStatus.PENDING)
        self.assertFalse(log.time_in_approved)

    @override_settings(FACE_CAPTURE_DIAGNOSTICS=False)
    def test_attendance_face_preview_classifies_capture_without_recording_attendance(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)
        cases = (
            (AttendanceLog.FaceCheckStatus.MATCHED, 200),
            (AttendanceLog.FaceCheckStatus.NOT_MATCHED, 403),
            (AttendanceLog.FaceCheckStatus.UNAVAILABLE, 400),
        )

        for face_status, expected_status in cases:
            with self.subTest(face_status=face_status), patch(
                "portal.views._attendance_face_check",
                return_value=(face_status, 0.81, b"unused preview image"),
            ) as face_check:
                response = self.client.post(
                    reverse("portal:attendance_face_preview"),
                    {"action": "clock_in", "face_image": self._camera_data_url()},
                )

                self.assertEqual(response.status_code, expected_status)
                if face_status == AttendanceLog.FaceCheckStatus.MATCHED:
                    self.assertEqual(response.json()["quality"], "good")
                    self.assertNotIn("similarity", response.json())
                    self.assertTrue(response.json()["verification_token"])
                else:
                    self.assertIn("error", response.json())
                face_check.assert_called_once()
                self.assertFalse(face_check.call_args.kwargs["retain_image"])

        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())

    @override_settings(FACE_CAPTURE_DIAGNOSTICS=True)
    def test_attendance_face_preview_exposes_similarity_only_in_diagnostic_mode(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)
        with patch(
            "portal.views._attendance_face_check",
            return_value=(AttendanceLog.FaceCheckStatus.MATCHED, 0.81, None),
        ):
            response = self.client.post(
                reverse("portal:attendance_face_preview"),
                {"action": "clock_in", "face_image": self._camera_data_url()},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["similarity"], 0.81)

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_signed_face_verification_token_records_attendance_without_rechecking_the_frame(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)
        face_image = self._camera_data_url()
        token = _attendance_face_verification_token(self.intern, "clock_in", face_image, 0.82)

        with patch("portal.views._attendance_face_check", side_effect=AssertionError("duplicate face check")):
            response = self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload(
                    "clock_in",
                    face_image=face_image,
                    face_verification_token=token,
                ),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            duplicate = self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload(
                    "clock_in",
                    face_image=face_image,
                    face_verification_token=token,
                ),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["attendance_status"], "approved")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(AttendanceLog.objects.get(intern=self.intern).face_captures.count(), 1)

    def test_signed_face_verification_token_cannot_be_reused_with_a_changed_frame(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)
        original_image = self._camera_data_url()
        token = _attendance_face_verification_token(self.intern, "clock_in", original_image, 0.82)

        with patch("portal.views._attendance_face_check", side_effect=AssertionError("invalid token bypassed")):
            response = self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload(
                    "clock_in",
                    face_image=original_image + "changed",
                    face_verification_token=token,
                ),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "face_verification_expired")
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())

    def test_attendance_face_preview_rejects_missing_face_enrollment(self):
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)

        response = self.client.post(
            reverse("portal:attendance_face_preview"),
            {"action": "clock_in", "face_image": self._camera_data_url()},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["face_status"], AttendanceLog.FaceCheckStatus.NOT_ENROLLED)
        self.assertIn("Enroll your face", response.json()["error"])

    def test_attendance_face_preview_returns_json_for_invalid_action(self):
        self._complete_intern_profile()
        self.client.force_login(self.intern_user)

        response = self.client.post(
            reverse("portal:attendance_face_preview"),
            {"action": "invalid", "face_image": self._camera_data_url()},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "Unknown attendance action."})

    def test_company_cannot_use_intern_face_preview_endpoint(self):
        self.client.force_login(self.company_user)

        response = self.client.post(
            reverse("portal:attendance_face_preview"),
            {"action": "clock_in", "face_image": self._camera_data_url()},
        )

        self.assertEqual(response.status_code, 403)

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_intern_can_enroll_own_face_from_profile_after_consent(self):
        self.client.force_login(self.intern_user)
        profile_url = reverse("portal:profile")
        enrollment_url = reverse("portal:intern_face_enrollment")
        pending_worksite = WorksiteLocation.objects.create(
            intern=self.intern,
            name="Independent pending location",
            address="100 Pending Road",
            latitude="14.600000",
            longitude="121.000000",
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
            is_active=False,
        )
        with self.settings(FACE_CAPTURE_DIAGNOSTICS=False):
            profile = self.client.get(profile_url)
        self.assertContains(profile, "Face enrollment for attendance")
        self.assertContains(profile, "Bright, even lighting")
        self.assertContains(profile, "Remove glasses or masks")
        self.assertContains(profile, "Center your face in the oval")
        self.assertContains(profile, "processed in memory")
        self.assertContains(profile, "Capture begins automatically")
        self.assertContains(profile, "two acceptable frames")
        self.assertContains(profile, 'id="manual-capture-enrollment" type="button" disabled hidden')
        self.assertContains(profile, 'id="capture-runtime-diagnostics"')
        self.assertContains(profile, 'id="runtime-stability-counter"')
        self.assertContains(profile, 'id="runtime-backend-request"')
        self.assertContains(profile, 'id="runtime-blocked-reason"')
        self.assertContains(profile, 'faceDetected: "No"')
        self.assertContains(profile, "readyState < 2")
        self.assertContains(profile, "last usable face detection is stale")
        self.assertContains(profile, "Face Detected — Hold still")
        self.assertContains(profile, 'id="enrollment-capture-progress-bar"')
        self.assertNotContains(profile, 'id="capture-face-enrollment"')
        self.assertContains(profile, 'id="submit-face-enrollment" type="submit" disabled')
        self.assertContains(profile, "submitValidatedEnrollment")
        self.assertNotContains(profile, "Development capture diagnostics")
        with self.settings(FACE_CAPTURE_DIAGNOSTICS=True):
            diagnostic_profile = self.client.get(profile_url)
        self.assertContains(diagnostic_profile, "Development capture diagnostics")
        self.assertContains(diagnostic_profile, "Blocked because")
        payload = {"face_image": self._camera_data_url()}

        missing_consent = self.client.post(enrollment_url, payload)
        self.assertRedirects(missing_consent, profile_url)
        self.assertFalse(FaceEnrollment.objects.filter(intern=self.intern).exists())

        payload["consent_confirmed"] = "on"
        with patch("portal.views.create_face_embedding", return_value=b"intern embedding"):
            enrolled = self.client.post(enrollment_url, payload)

        self.assertRedirects(enrolled, profile_url)
        submission = FaceEnrollmentSubmission.objects.get(intern=self.intern)
        self.assertEqual(submission.status, FaceEnrollmentSubmission.Status.PENDING)
        self.assertLessEqual(submission.submitted_at, timezone.now())
        self.assertFalse(FaceEnrollment.objects.filter(intern=self.intern).exists())
        duplicate_submission = self.client.post(enrollment_url, payload, follow=True)
        self.assertContains(duplicate_submission, "locked pending Admin review")
        self.assertEqual(FaceEnrollmentSubmission.objects.filter(intern=self.intern).count(), 1)
        enrolled_profile = self.client.get(profile_url)
        self.assertContains(enrolled_profile, "Pending Admin Review")
        self.assertNotContains(enrolled_profile, 'id="intern-face-enrollment-form"')

        coordinator = User.objects.create_user(username="initial-face-approver", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)
        approvals_url = reverse("portal:admin_approvals")
        approved = self.client.post(approvals_url, {
            "action": "approve_face_submission",
            "request_id": submission.pk,
        })
        self.assertRedirects(approved, approvals_url)
        record = FaceEnrollment.objects.get(intern=self.intern)
        self.assertEqual(record.enrolled_by, self.intern_user)
        self.assertEqual(record.consent_text_version, FACE_CONSENT_VERSION)
        self.assertEqual(decrypt_embedding(record.encrypted_embedding), b"intern embedding")
        submission.refresh_from_db()
        self.assertEqual(submission.status, FaceEnrollmentSubmission.Status.APPROVED)
        pending_worksite.refresh_from_db()
        self.assertEqual(pending_worksite.approval_status, WorksiteLocation.ApprovalStatus.PENDING)
        self.client.force_login(self.intern_user)
        self.assertContains(self.client.get(profile_url), "Enrolled / Approved")

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_admin_can_request_face_retake_without_replacing_current_enrollment(self):
        original_embedding = encrypt_embedding(b"original approved embedding")
        FaceEnrollment.objects.create(
            intern=self.intern,
            encrypted_embedding=original_embedding,
            enrolled_by=self.intern_user,
            consent_confirmed_at=timezone.now(),
        )
        self.client.force_login(User.objects.create_user(
            username="face-retake-admin",
            role=User.Role.COORDINATOR,
        ))
        approvals_url = reverse("portal:admin_approvals")
        retake = self.client.post(approvals_url, {
            "action": "request_face_retake",
            "intern_id": self.intern.pk,
            "review_reason": "Please recapture in brighter lighting.",
        })
        self.assertRedirects(retake, approvals_url)
        request_record = FaceReenrollmentRequest.objects.get(intern=self.intern)
        self.assertEqual(request_record.status, FaceReenrollmentRequest.Status.ADMIN_REQUESTED)
        self.assertEqual(request_record.review_reason, "Please recapture in brighter lighting.")

        self.client.force_login(self.intern_user)
        profile = self.client.get(reverse("portal:profile"))
        self.assertContains(profile, "Retake Requested")
        self.assertContains(profile, "Please recapture in brighter lighting.")
        self.assertContains(profile, 'id="intern-face-enrollment-form"')
        with patch(
            "portal.views.create_face_embedding",
            return_value=b"replacement embedding",
        ), patch("portal.views._face_embedding_is_duplicate", return_value=False), patch(
            "portal.views.cosine_similarity",
            return_value=0.9,
        ):
            submitted = self.client.post(reverse("portal:intern_face_enrollment"), {
                "consent_confirmed": "on",
                "face_image": self._camera_data_url(),
            })
        self.assertRedirects(submitted, reverse("portal:profile"))
        current = FaceEnrollment.objects.get(intern=self.intern)
        self.assertEqual(current.encrypted_embedding, original_embedding)

        submission = FaceEnrollmentSubmission.objects.get(intern=self.intern)
        self.client.force_login(User.objects.get(username="face-retake-admin"))
        approved = self.client.post(approvals_url, {
            "action": "approve_face_submission",
            "request_id": submission.pk,
        })
        self.assertRedirects(approved, approvals_url)
        self.assertEqual(
            decrypt_embedding(FaceEnrollment.objects.get(intern=self.intern).encrypted_embedding),
            b"replacement embedding",
        )
        request_record.refresh_from_db()
        self.assertEqual(request_record.status, FaceReenrollmentRequest.Status.COMPLETED)

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_face_rejection_requires_admin_feedback_and_unlocks_resubmission(self):
        self.client.force_login(self.intern_user)
        with patch("portal.views.create_face_embedding", return_value=b"first face"):
            self.client.post(reverse("portal:intern_face_enrollment"), {
                "consent_confirmed": "on",
                "face_image": self._camera_data_url(),
            })
        submission = FaceEnrollmentSubmission.objects.get(intern=self.intern)
        admin = User.objects.create_user(username="face-rejection-admin", role=User.Role.COORDINATOR)
        self.client.force_login(admin)
        approvals_url = reverse("portal:admin_approvals")

        missing_reason = self.client.post(approvals_url, {
            "action": "reject_face_submission",
            "request_id": submission.pk,
        })
        self.assertEqual(missing_reason.status_code, 400)
        submission.refresh_from_db()
        self.assertEqual(submission.status, FaceEnrollmentSubmission.Status.PENDING)

        rejected = self.client.post(approvals_url, {
            "action": "reject_face_submission",
            "request_id": submission.pk,
            "review_reason": "Please recapture with your full face visible.",
        })
        self.assertRedirects(rejected, approvals_url)
        submission.refresh_from_db()
        self.assertEqual(submission.status, FaceEnrollmentSubmission.Status.REJECTED)
        self.assertEqual(submission.review_reason, "Please recapture with your full face visible.")

        self.client.force_login(self.intern_user)
        profile = self.client.get(reverse("portal:profile"))
        self.assertContains(profile, "Rejected")
        self.assertContains(profile, "Please recapture with your full face visible.")
        self.assertContains(profile, 'id="intern-face-enrollment-form"')
        with (
            patch("portal.views.create_face_embedding", return_value=b"corrected face"),
            patch("portal.views._face_embedding_is_duplicate", return_value=False),
        ):
            resubmitted = self.client.post(reverse("portal:intern_face_enrollment"), {
                "consent_confirmed": "on",
                "face_image": self._camera_data_url(),
            })
        self.assertRedirects(resubmitted, reverse("portal:profile"))
        self.assertEqual(
            FaceEnrollmentSubmission.objects.filter(
                intern=self.intern,
                status=FaceEnrollmentSubmission.Status.PENDING,
            ).count(),
            1,
        )

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_face_reenrollment_is_locked_until_one_admin_approval(self):
        FaceEnrollment.objects.create(
            intern=self.intern,
            encrypted_embedding=encrypt_embedding(b"original embedding"),
            enrolled_by=self.intern_user,
            consent_confirmed_at=timezone.now(),
        )
        self.client.force_login(self.intern_user)
        profile_url = reverse("portal:profile")
        enroll_url = reverse("portal:intern_face_enrollment")
        payload = {
            "consent_confirmed": "on",
            "face_image": self._camera_data_url(),
        }

        locked_profile = self.client.get(profile_url)
        self.assertContains(locked_profile, "Enrolled / Approved")
        self.assertNotContains(locked_profile, 'id="intern-face-enrollment-form"')
        denied_enrollment = self.client.post(enroll_url, payload)
        self.assertRedirects(denied_enrollment, profile_url)
        self.assertEqual(
            decrypt_embedding(FaceEnrollment.objects.get(intern=self.intern).encrypted_embedding),
            b"original embedding",
        )

        request_url = reverse("portal:intern_face_reenrollment_request")
        submitted = self.client.post(request_url, {"reason": "My camera has stopped working."})
        self.assertRedirects(submitted, profile_url)
        request_record = FaceReenrollmentRequest.objects.get(intern=self.intern)
        self.assertEqual(request_record.status, FaceReenrollmentRequest.Status.PENDING)
        self.assertNotContains(self.client.get(profile_url), 'id="intern-face-enrollment-form"')

        coordinator = User.objects.create_user(username="face-coordinator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)
        self.assertEqual(self.client.get(reverse("portal:admin_approvals")).status_code, 200)

        approval_url = reverse("portal:admin_approvals")
        self.assertContains(self.client.get(approval_url), "My camera has stopped working.")
        approved = self.client.post(approval_url, {
            "action": "approve_face_reset",
            "request_id": request_record.pk,
        })
        self.assertRedirects(approved, approval_url)
        admin = User.objects.create_user(
            username="face-admin",
            password="Safe-example-Password-927!",
            is_superuser=True,
            is_staff=False,
        )
        self.client.force_login(admin)
        self.assertEqual(self.client.get(approval_url).status_code, 200)

        self.client.force_login(self.intern_user)
        approved_profile = self.client.get(profile_url)
        self.assertContains(approved_profile, "Retake Requested")
        self.assertContains(approved_profile, 'id="intern-face-enrollment-form"')
        with (
            patch("portal.views.create_face_embedding", return_value=b"replacement embedding"),
            patch("portal.views._face_embedding_is_duplicate", return_value=False),
            patch("portal.views.cosine_similarity", return_value=0.9),
        ):
            enrolled = self.client.post(enroll_url, payload)
        self.assertRedirects(enrolled, profile_url)
        submission = FaceEnrollmentSubmission.objects.get(
            intern=self.intern,
            status=FaceEnrollmentSubmission.Status.PENDING,
        )
        self.assertEqual(
            decrypt_embedding(FaceEnrollment.objects.get(intern=self.intern).encrypted_embedding),
            b"original embedding",
        )
        request_record.refresh_from_db()
        self.assertEqual(request_record.status, FaceReenrollmentRequest.Status.APPROVED)

        duplicate = self.client.post(enroll_url, payload, follow=True)
        self.assertContains(duplicate, "locked pending Admin review")
        self.assertEqual(FaceEnrollmentSubmission.objects.filter(intern=self.intern).count(), 1)

        self.client.force_login(coordinator)
        approved_submission = self.client.post(approval_url, {
            "action": "approve_face_submission",
            "request_id": submission.pk,
        })
        self.assertRedirects(approved_submission, approval_url)
        request_record.refresh_from_db()
        self.assertEqual(request_record.status, FaceReenrollmentRequest.Status.COMPLETED)
        self.assertEqual(
            decrypt_embedding(FaceEnrollment.objects.get(intern=self.intern).encrypted_embedding),
            b"replacement embedding",
        )

        self.client.force_login(self.intern_user)
        with patch("portal.views.create_face_embedding", return_value=b"second replacement"):
            second_enrollment = self.client.post(enroll_url, payload)
        self.assertRedirects(second_enrollment, profile_url)
        self.assertEqual(
            decrypt_embedding(FaceEnrollment.objects.get(intern=self.intern).encrypted_embedding),
            b"replacement embedding",
        )

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_intern_face_enrollment_rejects_embedding_used_by_another_account(self):
        from ml_engine.face_recognition import encrypt_embedding

        other_user = User.objects.create_user(username="other-enrolled-face", role=User.Role.INTERN)
        other_intern = InternProfile.objects.get(user=other_user)
        FaceEnrollment.objects.create(
            intern=other_intern,
            encrypted_embedding=encrypt_embedding(b"existing embedding"),
            enrolled_by=other_user,
            consent_confirmed_at=timezone.now(),
        )
        self.client.force_login(self.intern_user)

        with (
            patch("portal.views.create_face_embedding", return_value=b"duplicate embedding"),
            patch("portal.views.cosine_similarity", return_value=0.8) as similarity,
        ):
            response = self.client.post(
                reverse("portal:intern_face_enrollment"),
                {
                    "consent_confirmed": "on",
                    "face_image": self._camera_data_url(),
                },
                follow=True,
            )

        self.assertContains(response, "This face is already registered under another account.")
        self.assertFalse(FaceEnrollment.objects.filter(intern=self.intern).exists())
        similarity.assert_called_once()

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_intern_cannot_replace_face_enrollment_without_admin_approval(self):
        FaceEnrollment.objects.create(
            intern=self.intern,
            encrypted_embedding=b"own legacy encrypted embedding",
            enrolled_by=self.intern_user,
            consent_confirmed_at=timezone.now(),
        )
        self.client.force_login(self.intern_user)

        with patch("portal.views.create_face_embedding", return_value=b"replacement embedding") as create_embedding:
            response = self.client.post(
                reverse("portal:intern_face_enrollment"),
                {
                    "consent_confirmed": "on",
                    "face_image": self._camera_data_url(),
                },
                follow=True,
            )

        self.assertContains(
            response,
            "locked pending Admin review or authorization",
        )
        create_embedding.assert_not_called()
        record = FaceEnrollment.objects.get(intern=self.intern)
        self.assertEqual(record.encrypted_embedding, b"own legacy encrypted embedding")

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_intern_face_enrollment_preview_requires_detectable_single_face(self):
        self.client.force_login(self.intern_user)
        with patch("portal.views.create_face_embedding", return_value=b"preview embedding") as create_embedding:
            response = self.client.post(
                reverse("portal:face_enrollment_preview"),
                {"face_image": self._camera_data_url()},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quality"], "good")
        self.assertEqual(response.json()["identity_status"], "not_required")
        self.assertTrue(response.json()["label"])
        create_embedding.assert_called_once()
        self.assertFalse(FaceEnrollment.objects.filter(intern=self.intern).exists())

        with patch(
            "portal.views.create_face_embedding",
            side_effect=FaceImageError("Capture must contain exactly one clearly visible face."),
        ):
            poor_capture = self.client.post(
                reverse("portal:face_enrollment_preview"),
                {"face_image": self._camera_data_url()},
            )
        self.assertEqual(poor_capture.status_code, 200)
        self.assertEqual(poor_capture.json()["quality"], "poor")
        self.assertEqual(poor_capture.json()["identity_status"], "not_checked")

    @override_settings(
        FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"),
        FACE_CAPTURE_DIAGNOSTICS=True,
    )
    def test_face_enrollment_preview_matches_existing_reference_and_rejects_another_face(self):
        FaceEnrollment.objects.create(
            intern=self.intern,
            encrypted_embedding=encrypt_embedding(b"existing enrolled face"),
            enrolled_by=self.intern_user,
            consent_confirmed_at=timezone.now(),
        )
        self.client.force_login(self.intern_user)
        preview_url = reverse("portal:face_enrollment_preview")
        payload = {"face_image": self._camera_data_url()}

        with (
            patch("portal.views.create_face_embedding", return_value=b"same face"),
            patch("portal.views.cosine_similarity", return_value=0.82) as match,
        ):
            matched = self.client.post(preview_url, payload)

        self.assertEqual(matched.json()["quality"], "good")
        self.assertEqual(matched.json()["identity_status"], "matched")
        self.assertEqual(matched.json()["label"], "Face Matched")
        self.assertEqual(matched.json()["similarity"], 0.82)
        match.assert_called_once()

        with (
            patch("portal.views.create_face_embedding", return_value=b"different face"),
            patch("portal.views.cosine_similarity", return_value=0.21),
        ):
            rejected = self.client.post(preview_url, payload)

        self.assertEqual(rejected.json()["quality"], "poor")
        self.assertEqual(rejected.json()["identity_status"], "not_matched")
        self.assertEqual(rejected.json()["label"], "Face Not Matched")
        self.assertEqual(rejected.json()["similarity"], 0.21)
        self.assertFalse(FaceEnrollmentSubmission.objects.filter(intern=self.intern).exists())

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_face_reenrollment_submission_rejects_a_face_that_does_not_match_current_reference(self):
        original_embedding = encrypt_embedding(b"existing enrolled face")
        FaceEnrollment.objects.create(
            intern=self.intern,
            encrypted_embedding=original_embedding,
            enrolled_by=self.intern_user,
            consent_confirmed_at=timezone.now(),
        )
        FaceEnrollmentSubmission.objects.create(
            intern=self.intern,
            encrypted_embedding=encrypt_embedding(b"previous rejected submission"),
            status=FaceEnrollmentSubmission.Status.REJECTED,
            review_reason="Please verify your identity with the existing enrollment.",
        )
        self.client.force_login(self.intern_user)

        with (
            patch("portal.views.create_face_embedding", return_value=b"different person"),
            patch("portal.views._face_embedding_is_duplicate", return_value=False),
            patch("portal.views.cosine_similarity", return_value=0.2),
        ):
            response = self.client.post(
                reverse("portal:intern_face_enrollment"),
                {
                    "consent_confirmed": "on",
                    "face_image": self._camera_data_url(),
                },
                follow=True,
            )

        self.assertContains(response, "does not match your current enrollment")
        self.assertEqual(FaceEnrollmentSubmission.objects.filter(intern=self.intern).count(), 1)
        self.assertEqual(FaceEnrollment.objects.get(intern=self.intern).encrypted_embedding, original_embedding)

    def test_live_face_detection_preview_returns_face_count_without_profile_or_enrollment(self):
        self.client.force_login(self.intern_user)
        candidate = {
            "confidence": 0.95,
            "bbox": [10, 10, 80, 90],
            "roll_degrees": 1.0,
            "alignment_ready": True,
            "quality_issue": None,
        }
        with patch(
            "portal.views.inspect_face_candidates",
            return_value=[candidate],
        ) as detector:
            response = self.client.post(
                reverse("portal:face_detection_preview"),
                {"face_image": self._camera_data_url()},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "face_count": 1,
            "face_detected": True,
            "detections": [candidate],
            "image_width": 100,
            "image_height": 100,
        })
        detector.assert_called_once()

    @override_settings(FACE_CAPTURE_DIAGNOSTICS=True)
    def test_live_face_detection_logs_decoded_image_shape_when_no_face_is_found(self):
        self.client.force_login(self.intern_user)
        with (
            patch(
                "portal.views.inspect_face_candidates",
                return_value=[],
            ),
            self.assertLogs("portal.views", level="WARNING") as captured_logs,
        ):
            response = self.client.post(
                reverse("portal:face_detection_preview"),
                {"face_image": self._camera_data_url()},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "face_count": 0,
            "face_detected": False,
            "detections": [],
            "image_width": 100,
            "image_height": 100,
        })
        self.assertIn("image_type=Image", captured_logs.output[0])
        self.assertIn("mode=RGB", captured_logs.output[0])
        self.assertIn("array_shape=(100, 100, 3)", captured_logs.output[0])
        self.assertIn("array_dtype=uint8", captured_logs.output[0])
        self.assertIn("array_empty=False", captured_logs.output[0])
        self.assertIn("face_count=0", captured_logs.output[0])

    def test_face_capture_api_reports_expired_session_as_json(self):
        response = self.client.post(
            reverse("portal:face_detection_preview"),
            {"face_image": self._camera_data_url()},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "Your session expired. Sign in again and retry."})

    def test_intern_dashboard_and_attendance_are_available_without_ojt_approval(self):
        self.client.force_login(self.intern_user)

        dashboard = self.client.get(reverse("portal:intern_dashboard"))
        attendance = self.client.get(reverse("portal:attendance"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(attendance.status_code, 200)
        self.assertContains(attendance, "Register a worksite")
        self.assertNotContains(attendance, "Worksite location is not set up")

    def test_attendance_page_has_face_guide_and_capture_decision_controls(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)

        response = self.client.get(reverse("portal:attendance"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "face-guide")
        self.assertContains(response, "face-detection-guide.js")
        self.assertContains(response, "ellipseValue(centerX, centerY) > 0.70")
        self.assertContains(response, "intervalMs: 900")
        self.assertContains(response, "liveQualityWarning")
        self.assertContains(response, 'currentForm.getAttribute("action")')
        self.assertNotContains(response, "fetchJsonWithTimeout(currentForm.action")
        self.assertNotContains(response, "box.left < guideLeft")
        self.assertContains(response, "Position your face inside the circle")
        self.assertNotContains(response, "face-verification-progress")
        self.assertNotContains(response, "Automatic face verification")
        self.assertNotContains(response, "progress-blocked-reason")
        self.assertNotContains(response, "Face bounding box")
        self.assertNotContains(response, "Face detection callback")
        self.assertContains(response, "Capture Manually")
        self.assertContains(response, 'id="manual-face-capture" type="button" disabled')
        self.assertContains(response, 'data-diagnostics-enabled="false"')
        self.assertContains(response, "if (event.defaultPrevented) return;")
        self.assertContains(response, "Try again now")
        self.assertNotContains(response, "Capture and check")
        self.assertNotContains(response, "Record attendance")
        self.assertContains(response, "A successful single-face match is required")
        self.assertContains(response, "Face Ready")
        self.assertContains(response, "100m zone")
        self.assertContains(response, "Look straight at the camera in good light")
        self.assertNotContains(response, "Face verification required</h2>")
        self.assertNotContains(response, "retained for up to 30 days")

        with self.settings(FACE_CAPTURE_DIAGNOSTICS=True):
            diagnostic_response = self.client.get(reverse("portal:attendance"))
        self.assertContains(diagnostic_response, 'data-diagnostics-enabled="true"')
        self.assertNotContains(diagnostic_response, "face-verification-progress")

    def test_face_mismatch_is_rejected_without_creating_attendance(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)

        with patch(
            "portal.views._attendance_face_check",
            return_value=(AttendanceLog.FaceCheckStatus.NOT_MATCHED, 0.21, b"encrypted test capture"),
        ):
            response = self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload("clock_in"),
                follow=True,
            )

        self.assertEqual(response.redirect_chain[0][0], reverse("portal:attendance"))
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())
        self.assertContains(
            response,
            "Face verification failed. Face does not match your registered profile.",
        )

    def test_unavailable_or_multiple_face_capture_is_rejected(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)

        with patch(
            "portal.views._attendance_face_check",
            return_value=(AttendanceLog.FaceCheckStatus.UNAVAILABLE, None, None),
        ):
            response = self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload("clock_in"),
            )

        self.assertRedirects(response, reverse("portal:attendance"))
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())

    def test_no_camera_frame_is_rejected_without_creating_attendance(self):
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)
        submitted = self.client.post(
            reverse("portal:attendance"),
            self._attendance_payload("clock_in"),
            follow=True,
        )
        self.assertEqual(submitted.redirect_chain[0][0], reverse("portal:attendance"))

        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())
        self.assertContains(
            submitted,
            "Capture a face frame before recording attendance.",
        )

    def test_attendance_page_explains_required_face_verification(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)

        response = self.client.get(reverse("portal:attendance"))

        self.assertContains(response, "Face Ready")
        self.assertNotContains(response, "Face verification required</h2>")
        self.assertNotContains(response, "retained for up to 30 days")
        self.assertContains(response, "Choose Time in or Time out to open the camera.")
        self.assertNotContains(response, "Start camera")
        self.assertNotContains(response, "Capture face for attendance")
        self.assertContains(response, "Verification starts automatically.")
        self.assertNotContains(response, "Capture and check")
        self.assertContains(response, 'name="face_image"')
        self.assertContains(response, 'id="location-reason-dialog"')
        self.assertContains(response, 'minlength="10"')

    def test_successful_face_matches_inside_worksite_auto_approve_each_event(self):
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)
        with patch(
            "portal.views._attendance_face_check",
            side_effect=[
                (AttendanceLog.FaceCheckStatus.MATCHED, 0.81, b"encrypted time-in"),
                (AttendanceLog.FaceCheckStatus.MATCHED, 0.79, b"encrypted time-out"),
            ],
        ):
            time_in_response = self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload("clock_in"),
            )
            self.assertRedirects(time_in_response, reverse("portal:attendance"))
            log = AttendanceLog.objects.get(intern=self.intern)
            time_in_capture = log.face_captures.get(event=AttendanceFaceCapture.Event.TIME_IN)
            self.assertEqual(time_in_capture.review_status, AttendanceFaceCapture.ReviewStatus.NOT_REQUIRED)
            self.assertTrue(log.time_in_approved)

            time_out_response = self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload("clock_out"),
            )
            self.assertRedirects(time_out_response, reverse("portal:attendance"))

        self.assertRedirects(time_out_response, reverse("portal:attendance"))
        log.refresh_from_db()
        time_out_capture = log.face_captures.get(event=AttendanceFaceCapture.Event.TIME_OUT)
        self.assertIsNotNone(log.clock_in)
        self.assertIsNotNone(log.clock_out)
        self.assertTrue(log.time_in_approved)
        self.assertTrue(log.time_out_approved)
        self.assertTrue(log.clock_in_within_location)
        self.assertTrue(log.clock_out_within_location)
        self.assertEqual(time_out_capture.review_status, AttendanceFaceCapture.ReviewStatus.NOT_REQUIRED)
        self.assertEqual(log.face_captures.count(), 2)
        self.assertEqual(
            set(log.face_captures.values_list("review_status", flat=True)),
            {AttendanceFaceCapture.ReviewStatus.NOT_REQUIRED},
        )

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_company_can_review_location_exception_and_view_only_its_capture(self):
        from ml_engine.face_recognition import encrypt_face_capture

        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)
        encrypted_image = encrypt_face_capture(b"test attendance jpeg")
        with patch(
            "portal.views._attendance_face_check",
            return_value=(AttendanceLog.FaceCheckStatus.MATCHED, 0.81, encrypted_image),
        ):
            self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload(
                    "clock_in",
                    clock_in_latitude="14.700000",
                    clock_in_location_reason="The office GPS signal was inaccurate today.",
                ),
            )

        log = AttendanceLog.objects.get(intern=self.intern)
        capture = log.face_captures.get()
        self.assertFalse(log.time_in_approved)
        self.client.force_login(self.company_user)
        queue = self.client.get(reverse("portal:company_dtr_queue"))
        self.assertContains(queue, "Approve time in")
        self.assertContains(queue, "Awaiting company/admin approval")

        image_response = self.client.get(
            reverse("portal:attendance_face_capture_image", args=(capture.pk,))
        )
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response.content, b"test attendance jpeg")
        self.assertEqual(image_response["Cache-Control"], "private, no-store")

        other_user = User.objects.create_user(username="other-face-intern", role=User.Role.INTERN)
        other_intern, _ = InternProfile.objects.get_or_create(user=other_user)
        other_log = AttendanceLog.objects.create(intern=other_intern, work_date="2026-10-09")
        other_capture = AttendanceFaceCapture.objects.create(
            attendance_log=other_log,
            event=AttendanceFaceCapture.Event.TIME_IN,
            face_status=AttendanceLog.FaceCheckStatus.ERROR,
            encrypted_image=encrypted_image,
        )
        forbidden_image = self.client.get(
            reverse("portal:attendance_face_capture_image", args=(other_capture.pk,))
        )
        self.assertEqual(forbidden_image.status_code, 404)

        reviewed = self.client.post(
            reverse("portal:review_attendance_face_capture", args=(capture.pk,)),
            {"action": "approve"},
        )
        self.assertRedirects(reviewed, reverse("portal:company_dtr_queue"))
        log.refresh_from_db()
        capture.refresh_from_db()
        self.assertEqual(log.clock_in, capture.captured_at)
        self.assertTrue(log.time_in_approved)
        self.assertEqual(capture.review_status, AttendanceFaceCapture.ReviewStatus.APPROVED)
        coordinator = User.objects.create_user(username="capture-review-coordinator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)
        coordinator_queue = self.client.get(reverse("portal:dtr_queue"))
        self.assertContains(coordinator_queue, "View capture")
        coordinator_image = self.client.get(
            reverse("portal:attendance_face_capture_image", args=(capture.pk,))
        )
        self.assertEqual(coordinator_image.status_code, 200)
        self.assertEqual(coordinator_image.content, b"test attendance jpeg")

    def test_expired_attendance_capture_image_is_deleted_by_retention_command(self):
        log = AttendanceLog.objects.create(intern=self.intern, work_date="2026-10-09")
        capture = AttendanceFaceCapture.objects.create(
            attendance_log=log,
            event=AttendanceFaceCapture.Event.TIME_IN,
            face_status=AttendanceLog.FaceCheckStatus.ERROR,
            encrypted_image=b"expired encrypted image",
            image_expires_at=datetime(2020, 1, 1, tzinfo=datetime_timezone.utc),
        )

        call_command("purge_face_attendance_images", verbosity=0)

        capture.refresh_from_db()
        self.assertIsNone(capture.encrypted_image)
        self.assertTrue(AttendanceFaceCapture.objects.filter(pk=capture.pk).exists())

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_coordinator_enrollment_requires_consent_and_stores_only_encrypted_embedding(self):
        coordinator = User.objects.create_user(username="face-coordinator", role=User.Role.COORDINATOR)
        self.client.force_login(coordinator)
        url = reverse("portal:coordinator_face_enrollment")
        self.assertContains(self.client.get(url), "Face-recognition pilot enrollment")
        payload = {
            "action": "enroll",
            "intern_id": self.intern.pk,
            "face_image": self._camera_data_url(),
        }

        missing_consent = self.client.post(url, payload)
        self.assertRedirects(missing_consent, url)
        self.assertFalse(FaceEnrollment.objects.filter(intern=self.intern).exists())

        payload["consent_confirmed"] = "on"
        with patch("portal.views.create_face_embedding", return_value=b"test embedding"):
            enrolled = self.client.post(url, payload)

        self.assertRedirects(enrolled, url)
        record = FaceEnrollment.objects.get(intern=self.intern)
        self.assertNotEqual(bytes(record.encrypted_embedding), b"test embedding")
        self.assertEqual(decrypt_embedding(record.encrypted_embedding), b"test embedding")
        self.assertEqual(record.enrolled_by, coordinator)
        self.assertIsNotNone(record.consent_confirmed_at)
        AttendanceLog.objects.create(
            intern=self.intern,
            work_date="2026-10-09",
            clock_in_face_status=AttendanceLog.FaceCheckStatus.MATCHED,
            clock_in_face_score=0.8,
        )

        deleted = self.client.post(url, {
            "action": "delete",
            "intern_id": self.intern.pk,
        })
        self.assertRedirects(deleted, url)
        self.assertFalse(FaceEnrollment.objects.filter(pk=record.pk).exists())
        record = FaceEnrollment.objects.create(
            intern=self.intern,
            encrypted_embedding=record.encrypted_embedding,
            enrolled_by=coordinator,
            consent_confirmed_at=record.consent_confirmed_at,
        )
        admin = User.objects.create_superuser(username="face-reset-superuser", password="Safe-example-Password-927!")
        self.client.force_login(admin)
        deleted = self.client.post(url, {
            "action": "delete",
            "intern_id": self.intern.pk,
        })
        self.assertRedirects(deleted, url)
        self.assertFalse(FaceEnrollment.objects.filter(intern=self.intern).exists())
        self.assertEqual(
            AttendanceLog.objects.get(intern=self.intern).clock_in_face_status,
            AttendanceLog.FaceCheckStatus.MATCHED,
        )
        self.assertEqual(AttendanceLog.objects.get(intern=self.intern).clock_in_face_score, 0.8)




    def test_intern_dashboard_shows_approved_hours_and_core_tasks(self):
        self._complete_intern_profile()
        clock_in = datetime(2026, 10, 5, 8, tzinfo=datetime_timezone.utc)
        AttendanceLog.objects.create(
            intern=self.intern,
            clock_in=clock_in,
            clock_out=clock_in.replace(hour=16),
            time_in_approved=True,
            time_out_approved=True,
        )
        self.client.force_login(self.intern_user)

        response = self.client.get(reverse("portal:intern_dashboard"))

        self.assertEqual(response.context["completed_hours"], 8)
        self.assertContains(response, "Approved hours")
        self.assertContains(response, "Daily reports")

    def test_intern_progress_requires_both_time_approvals(self):
        self._complete_intern_profile()
        clock_in = datetime(2026, 10, 5, 8, tzinfo=datetime_timezone.utc)
        log = AttendanceLog.objects.create(
            intern=self.intern,
            clock_in=clock_in,
            clock_out=clock_in.replace(hour=16),
            time_in_approved=True,
        )
        self.client.force_login(self.intern_user)

        response = self.client.get(reverse("portal:intern_dashboard"))
        self.assertEqual(response.context["completed_hours"], 0)

        log.time_out_approved = True
        log.save(update_fields=("time_out_approved",))
        response = self.client.get(reverse("portal:intern_dashboard"))
        self.assertEqual(response.context["completed_hours"], 8)

    def test_coordinator_dashboard_focuses_on_attendance_reports_and_worksites(self):
        coordinator = User.objects.create_user(
            username="coordinator",
            password="Safe-example-Password-927!",
            role=User.Role.COORDINATOR,
        )
        self.client.force_login(coordinator)

        response = self.client.get(reverse("portal:coordinator_dashboard"))

        self.assertContains(response, "Pending DTR events")
        self.assertContains(response, "Daily reports to review")
        self.assertNotContains(response, "Intern directory")

    def test_company_can_export_accepted_intern_attendance_csv(self):
        self.intern.student_id = "STU-CSV-1"
        self.intern.save(update_fields=("student_id",))
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        clock_in = datetime(2026, 10, 5, 8, tzinfo=datetime_timezone.utc)
        AttendanceLog.objects.create(
            intern=self.intern,
            clock_in=clock_in,
            clock_out=clock_in.replace(hour=16),
            time_in_approved=True,
            time_out_approved=True,
        )
        self.client.force_login(self.company_user)

        response = self.client.get(reverse("portal:company_attendance_export"))

        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertContains(response, "STU-CSV-1")

    def test_company_profile_can_be_updated(self):
        self.client.force_login(self.company_user)

        response = self.client.post(
            reverse("portal:company_profile"),
            {
                "company_name": "Updated Host Organization",
                "company_address": "12 Fieldwork Road",
                "barangay": "San Isidro",
                "city_municipality": "Makati",
                "province": "Metro Manila",
                "postal_code": "1200",
                "industry_type": "it",
                "contact_person_name": "Jamie Smith",
                "contact_person_position": "HR Manager",
                "official_email": "host@example.org",
                "contact_number": "0917-123-4567",
                "company_website": "https://example.org",
                "moa_reference_number": "MOA-2026-002",
            },
        )

        self.assertRedirects(response, reverse("portal:company_profile"))
        self.company.refresh_from_db()
        self.assertEqual(self.company.organization, "Updated Host Organization")
        self.assertEqual(self.company.website, "https://example.org")
        self.assertEqual(self.company.address, "12 Fieldwork Road")
        self.assertEqual(self.company.barangay, "San Isidro")
        self.assertEqual(self.company.city_municipality, "Makati")
        self.assertEqual(self.company.province, "Metro Manila")
        self.assertEqual(self.company.postal_code, "1200")
        self.assertEqual(self.company.official_email, "host@example.org")
        self.assertEqual(self.company.contact_number, "+639171234567")

    def test_company_profile_rejects_invalid_phone_and_preserves_entered_value(self):
        self.client.force_login(self.company_user)
        response = self.client.post(
            reverse("portal:company_profile"),
            {
                "company_name": "Updated Host Organization",
                "company_address": "12 Fieldwork Road",
                "barangay": "San Isidro",
                "city_municipality": "Makati",
                "province": "Metro Manila",
                "postal_code": "",
                "industry_type": "it",
                "contact_person_name": "Jamie Smith",
                "contact_person_position": "HR Manager",
                "official_email": "host@example.org",
                "contact_number": "0917ABC4567",
                "company_website": "",
                "moa_reference_number": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("contact_number", response.context["form"].errors)
        self.assertEqual(
            response.context["form"]["contact_number"].value(),
            "0917ABC4567",
        )

    def test_company_profile_requires_location_fields(self):
        form = CompanyProfileForm(
            data={
                "company_name": "Example Company",
                "company_address": "12 Fieldwork Road",
                "barangay": "San Isidro",
                "city_municipality": "Makati",
                "province": "",
                "postal_code": "",
                "industry_type": "it",
                "contact_person_name": "Jamie Smith",
                "contact_person_position": "HR Manager",
                "official_email": "host@example.org",
                "contact_number": "09171234567",
                "company_website": "",
                "moa_reference_number": "",
            },
            instance=self.company,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("province", form.errors)

    def test_posting_form_rejects_invalid_openings_and_date_order(self):
        base_data = {
            "title": "Operations Intern",
            "description": "Support the placement team.",
            "location": "Makati",
            "remote": "on",
            "openings": "0",
            "start_date": "2026-10-12",
            "end_date": "2026-10-11",
            "status": Posting.Status.DRAFT,
        }
        form = PostingForm(data=base_data)

        self.assertFalse(form.is_valid())
        self.assertIn("openings", form.errors)
        self.assertIn("end_date", form.errors)

    def test_scorecard_week_date_and_worksite_ranges_are_validated(self):
        future_date = (timezone.localdate() + timedelta(days=1)).isoformat()
        selection_form = ScorecardSelectionForm(
            data={"intern_id": self.intern.pk, "week_start": future_date},
            interns=InternProfile.objects.all(),
        )
        worksite_form = WorksiteLocationForm(data={
            "name": "North office",
            "address": "10 North Road",
            "latitude": "14.600000",
            "longitude": "121.000000",
            "radius_meters": "5001",
        })

        self.assertFalse(selection_form.is_valid())
        self.assertIn("week_start", selection_form.errors)
        self.assertFalse(worksite_form.is_valid())
        self.assertIn("radius_meters", worksite_form.errors)

    def test_application_note_length_and_review_feedback_are_validated(self):
        application_form = ApplicationSubmissionForm(data={
            "cover_letter": "x" * 3001,
        })
        review_form = DailyReportReviewForm(data={
            "decision": "request_changes",
            "supervisor_feedback": "  ",
        })

        self.assertFalse(application_form.is_valid())
        self.assertIn("cover_letter", application_form.errors)
        self.assertFalse(review_form.is_valid())
        self.assertIn("supervisor_feedback", review_form.errors)

    def test_applicant_screening_shows_pending_applications(self):
        self.intern.course = "Information Systems"
        self.intern.save(update_fields=("course",))
        application = Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            cover_letter="I am interested in this placement.",
        )
        self.client.force_login(self.company_user)

        response = self.client.get(reverse("portal:applicant_screening"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.intern_user.username)
        self.assertContains(response, "Information Systems")
        self.assertContains(response, "I am interested in this placement.")
        self.assertEqual(list(response.context["pending"]), [application])

    def test_company_can_preview_and_download_own_resume(self):
        pdf_content = b"%PDF-1.7\nprivate resume"
        self.client.force_login(self.company_user)

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            application = Application.objects.create(
                intern=self.intern,
                posting=self.posting,
                resume=SimpleUploadedFile("candidate.pdf", pdf_content, content_type="application/pdf"),
            )

            preview = self.client.get(
                reverse("portal:company_application_resume", args=(application.pk, "preview"))
            )
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview["Content-Type"], "application/pdf")
            self.assertIn("inline", preview["Content-Disposition"])
            self.assertEqual(b"".join(preview.streaming_content), pdf_content)
            self.assertEqual(preview["Cache-Control"], "private, no-store")

            download = self.client.get(
                reverse("portal:company_application_resume", args=(application.pk, "download"))
            )
            self.assertEqual(download.status_code, 200)
            self.assertIn("attachment", download["Content-Disposition"])
            self.assertEqual(b"".join(download.streaming_content), pdf_content)

    def test_company_can_preview_docx_resume_as_html(self):
        document_stream = BytesIO()
        document = DocxDocument()
        document.add_heading("Candidate Resume", level=1)
        document.add_paragraph("Experienced operations intern.")
        document.save(document_stream)
        self.client.force_login(self.company_user)

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            application = Application.objects.create(
                intern=self.intern,
                posting=self.posting,
                resume=SimpleUploadedFile(
                    "candidate.docx",
                    document_stream.getvalue(),
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            )

            applications_response = self.client.get(reverse("portal:company_applications"))
            screening_response = self.client.get(reverse("portal:applicant_screening"))
            self.assertContains(
                applications_response,
                reverse("portal:company_application_resume", args=(application.pk, "preview")),
            )
            self.assertContains(screening_response, "Preview resume")

            response = self.client.get(
                reverse("portal:company_application_resume", args=(application.pk, "preview"))
            )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "portal/company/resume_preview.html")
        self.assertContains(response, "Candidate Resume")
        self.assertContains(response, "Experienced operations intern.")
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_company_cannot_preview_another_company_resume(self):
        other_user = User.objects.create_user(
            username="resume-owner-company",
            password="Safe-example-Password-927!",
            role=User.Role.COMPANY,
        )
        other_company = CompanyProfile.objects.get(user=other_user)
        other_posting = Posting.objects.create(
            company=other_company,
            title="Private Placement",
            description="Private application.",
            location="Remote",
        )
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            application = Application.objects.create(
                intern=self.intern,
                posting=other_posting,
                resume=SimpleUploadedFile("candidate.pdf", b"%PDF-1.7\nprivate resume", content_type="application/pdf"),
            )
            self.client.force_login(self.company_user)

            response = self.client.get(
                reverse("portal:company_application_resume", args=(application.pk, "preview"))
            )

        self.assertEqual(response.status_code, 404)

    def test_company_can_accept_own_pending_application(self):
        application = Application.objects.create(intern=self.intern, posting=self.posting)
        self.client.force_login(self.company_user)

        response = self.client.post(
            reverse("portal:application_decide", args=(application.pk, "accept"))
        )

        self.assertRedirects(response, reverse("portal:applicant_screening"))
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.ACCEPTED)

    def test_company_cannot_decide_another_company_application(self):
        other_user = User.objects.create_user(
            username="other-company",
            password="Safe-example-Password-927!",
            role=User.Role.COMPANY,
        )
        other_company = CompanyProfile.objects.get(user=other_user)
        other_posting = Posting.objects.create(
            company=other_company,
            title="Other Placement",
            description="A different placement.",
            location="Remote",
        )
        application = Application.objects.create(intern=self.intern, posting=other_posting)
        self.client.force_login(self.company_user)

        response = self.client.post(
            reverse("portal:application_decide", args=(application.pk, "accept"))
        )

        self.assertEqual(response.status_code, 404)
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.SUBMITTED)

    def test_company_dtr_only_shows_accepted_interns_and_can_approve(self):
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        own_log = AttendanceLog.objects.create(
            intern=self.intern,
            work_date="2026-10-01",
            clock_in="2026-10-01T09:00:00Z",
            clock_out="2026-10-01T17:00:00Z",
        )
        other_user = User.objects.create_user(username="other-intern", password="Safe-example-Password-927!")
        other_intern = InternProfile.objects.get(user=other_user)
        AttendanceLog.objects.create(intern=other_intern, work_date="2026-10-01")
        self.client.force_login(self.company_user)

        response = self.client.get(reverse("portal:company_dtr_queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.intern_user.username)
        self.assertNotContains(response, other_user.username)
        self.assertContains(response, "2 time events")

        response = self.client.post(
            reverse("portal:company_approve_dtr", args=(own_log.pk,)),
            {"event": "time_in"},
        )

        self.assertRedirects(response, reverse("portal:company_dtr_queue"))
        own_log.refresh_from_db()
        self.assertTrue(own_log.time_in_approved)
        self.assertFalse(own_log.time_out_approved)
        self.assertFalse(own_log.approved)

        self.client.post(
            reverse("portal:company_approve_dtr", args=(own_log.pk,)),
            {"event": "time_out"},
        )
        own_log.refresh_from_db()
        self.assertTrue(own_log.approved)

    def test_incomplete_attendance_allows_its_recorded_time_to_be_approved(self):
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        AttendanceLog.objects.create(
            intern=self.intern,
            work_date="2026-10-01",
            clock_in="2026-10-01T09:00:00Z",
        )
        self.client.force_login(self.company_user)

        response = self.client.get(reverse("portal:company_dtr_queue"))

        self.assertContains(response, "Approve time in")
        self.assertContains(response, "Time out approval")
        self.assertContains(response, "Not recorded")

    def test_company_scorecard_is_limited_to_accepted_interns(self):
        Application.objects.create(
            intern=self.intern,
            posting=self.posting,
            status=Application.Status.ACCEPTED,
        )
        self.client.force_login(self.company_user)

        response = self.client.get(reverse("portal:company_scorecards"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.intern_user.username)

        response = self.client.post(
            reverse("portal:company_scorecards"),
            {
                "intern_id": self.intern.pk,
                "week_start": "2026-10-05",
                "technical": 5,
                "communication": 4,
                "initiative": 3,
                "reliability": 2,
                "feedback": "Strong progress this week.",
            },
        )

        self.assertRedirects(response, reverse("portal:company_scorecards"))
        scorecard = Scorecard.objects.get(company=self.company, intern=self.intern)
        self.assertEqual(scorecard.reviewer, self.company_user)
        self.assertEqual(scorecard.average, 3.5)

        unaccepted_user = User.objects.create_user(username="unaccepted", password="Safe-example-Password-927!")
        unaccepted_intern = InternProfile.objects.get(user=unaccepted_user)
        response = self.client.post(
            reverse("portal:company_scorecards"),
            {
                "intern_id": unaccepted_intern.pk,
                "week_start": "2026-10-05",
                "technical": 5,
                "communication": 5,
                "initiative": 5,
                "reliability": 5,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_coordinator_can_verify_company_and_review_audit_log(self):
        coordinator = User.objects.create_user(
            username="coordinator",
            password="Safe-example-Password-927!",
            role=User.Role.COORDINATOR,
        )
        self.client.force_login(coordinator)

        response = self.client.get(reverse("portal:admin_verify"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.company.organization)

        response = self.client.post(
            reverse("portal:admin_verify_company", args=(self.company.pk, "verify"))
        )
        self.assertRedirects(response, reverse("portal:admin_verify"))
        self.company.refresh_from_db()
        self.assertTrue(self.company.verified)

        response = self.client.get(reverse("portal:admin_audit"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "company.verified")

    def test_application_unique_constraint_blocks_duplicate(self):
        Application.objects.create(intern=self.intern, posting=self.posting)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Application.objects.create(intern=self.intern, posting=self.posting)

    def test_attendance_unique_constraint_blocks_duplicate_day(self):
        AttendanceLog.objects.create(intern=self.intern, work_date="2026-01-05")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AttendanceLog.objects.create(intern=self.intern, work_date="2026-01-05")

    def test_weekly_report_unique_constraint_blocks_duplicate_week(self):
        WeeklyReport.objects.create(intern=self.intern, week_start="2026-01-05", accomplishments="Completed onboarding.")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WeeklyReport.objects.create(intern=self.intern, week_start="2026-01-05", accomplishments="Second report.")

    def test_clock_in_uses_server_time_and_saves_enrolled_worksite_coordinates(self):
        fixed_now = datetime(2026, 1, 5, 9, 30, tzinfo=datetime_timezone.utc)
        self._complete_intern_profile()
        self._enroll_attendance_location()
        self.client.force_login(self.intern_user)
        with (
            patch("portal.views.timezone.now", return_value=fixed_now),
            patch(
                "portal.views._attendance_face_check",
                return_value=(AttendanceLog.FaceCheckStatus.MATCHED, 0.82, b"encrypted frame"),
            ),
        ):
            response = self.client.post(
                reverse("portal:attendance"),
                self._attendance_payload(
                    "clock_in",
                    notes="Started inventory review",
                    clock_in="1999-01-01T00:00:00Z",
                ),
            )
        self.assertRedirects(response, reverse("portal:attendance"))
        log = AttendanceLog.objects.get(intern=self.intern)
        self.assertEqual(log.clock_in, fixed_now)
        self.assertEqual(log.notes, "Started inventory review")
        self.assertEqual(str(log.clock_in_latitude), "14.600000")
        self.assertTrue(log.clock_in_within_location)
        self.assertEqual(log.clock_in_distance_meters, 0)
        self.assertEqual(log.clock_in_allowed_radius_meters, 100)
        self.assertTrue(log.time_in_approved)
        self.assertEqual(log.clock_in_face_status, AttendanceLog.FaceCheckStatus.MATCHED)
        capture = log.face_captures.get(event=AttendanceFaceCapture.Event.TIME_IN)
        self.assertEqual(capture.review_status, AttendanceFaceCapture.ReviewStatus.NOT_REQUIRED)

    def test_clock_out_without_clock_in_does_not_create_empty_record(self):
        self._complete_intern_profile()
        self.client.force_login(self.intern_user)
        response = self.client.post(reverse("portal:attendance"), {"action": "clock_out"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AttendanceLog.objects.filter(intern=self.intern).exists())

    def test_coordinator_can_approve_time_in_before_time_out(self):
        coordinator = User.objects.create_user(
            username="coordinator",
            password="Safe-example-Password-927!",
            role=User.Role.COORDINATOR,
        )
        log = AttendanceLog.objects.create(intern=self.intern, work_date="2026-01-05", clock_in="2026-01-05T09:00:00Z")
        self.client.force_login(coordinator)
        response = self.client.post(
            reverse("portal:approve_dtr", args=(log.pk,)),
            {"event": "time_in"},
        )
        self.assertEqual(response.status_code, 302)
        log.refresh_from_db()
        self.assertTrue(log.time_in_approved)
        self.assertFalse(log.time_out_approved)
        self.assertFalse(log.approved)

    def test_invalid_scorecard_does_not_create_placeholder(self):
        coordinator = User.objects.create_user(
            username="coordinator",
            password="Safe-example-Password-927!",
            role=User.Role.COORDINATOR,
        )
        self.client.force_login(coordinator)
        response = self.client.post(
            reverse("portal:scorecards"),
            {
                "intern_id": self.intern.pk,
                "week_start": "2026-01-05",
                "technical": 0,
                "communication": 3,
                "initiative": 3,
                "reliability": 3,
                "feedback": "Review",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Scorecard.objects.count(), 0)

    def test_analytics_shows_saved_risk_assessments(self):
        coordinator = User.objects.create_user(
            username="coordinator",
            password="Safe-example-Password-927!",
            role=User.Role.COORDINATOR,
        )
        RiskAssessment.objects.create(
            intern=self.intern,
            risk_score=72,
            level=RiskAssessment.Level.HIGH,
            indicators=["missed days"],
        )
        self.client.force_login(coordinator)

        response = self.client.get(reverse("portal:analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.intern_user.username)
        self.assertContains(response, "72 / 100")
        self.assertContains(response, "missed days")

    def test_haversine_returns_equatorial_distance(self):
        self.assertAlmostEqual(haversine_distance_km(0, 0, 0, 1), 111.2, delta=0.2)

    def test_upload_validator_checks_pdf_magic_bytes(self):
        validator = FileSizeAndTypeValidator()
        validator(SimpleUploadedFile("resume.pdf", b"%PDF-1.7\ncontent"))
        with self.assertRaises(ValidationError):
            validator(SimpleUploadedFile("fake.pdf", b"plain text disguised as a PDF"))

    def test_application_view_rejects_disguised_upload(self):
        self._complete_intern_profile()
        self.client.force_login(self.intern_user)
        response = self.client.post(
            reverse("portal:apply", args=(self.posting.pk,)),
            {"cover_letter": "Interested", "resume": SimpleUploadedFile("fake.pdf", b"not a real PDF")},
        )
        self.assertEqual(response.status_code, 200)
        posting = next(item for item in response.context["postings"] if item.pk == self.posting.pk)
        self.assertEqual(posting.apply_form["cover_letter"].value(), "Interested")
        self.assertIn("resume", posting.apply_form.errors)
        self.assertFalse(Application.objects.filter(intern=self.intern, posting=self.posting).exists())

    def test_blank_student_ids_do_not_collide(self):
        self.intern.student_id = ""
        self.intern.save()
        User.objects.create_user(username="intern-two", password="Safe-example-Password-927!")
        self.assertEqual(InternProfile.objects.filter(student_id__isnull=True).count(), 2)