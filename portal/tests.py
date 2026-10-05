import tempfile
from datetime import datetime, timezone as datetime_timezone
from io import BytesIO
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from docx import Document as DocxDocument

from accounts.models import User
from .models import Application, AttendanceLog, CompanyProfile, InternProfile, Posting, RiskAssessment, Scorecard, WeeklyReport
from .utils import haversine_distance_km
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

    def test_company_dashboard_shows_pending_applicants(self):
        Application.objects.create(intern=self.intern, posting=self.posting)
        self.client.force_login(self.company_user)

        response = self.client.get(reverse("portal:company_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.company.organization)
        self.assertContains(response, self.intern_user.username)
        self.assertContains(response, self.posting.title)
        self.assertEqual(response.context["pending_application_count"], 1)

    def test_role_dashboard_routes_use_role_specific_paths(self):
        self.assertEqual(reverse("portal:intern_dashboard"), "/intern/dashboard/")
        self.assertEqual(reverse("portal:company_dashboard"), "/supervisor/dashboard/")
        self.assertEqual(reverse("portal:coordinator_dashboard"), "/admin/dashboard/")

    def test_intern_dashboard_shows_approved_hour_progress(self):
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
        self.assertEqual(response.context["target_hours"], 120)
        self.assertContains(response, "OJT hour progress")

    def test_intern_progress_requires_both_time_approvals(self):
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

    def test_coordinator_dashboard_lists_intern_and_quick_filters(self):
        coordinator = User.objects.create_user(
            username="coordinator",
            password="Safe-example-Password-927!",
            role=User.Role.COORDINATOR,
        )
        self.client.force_login(coordinator)

        response = self.client.get(reverse("portal:coordinator_dashboard"))

        self.assertContains(response, "Intern directory")
        self.assertContains(response, "Application status")
        self.assertContains(response, "Risk level")
        self.assertContains(response, self.intern_user.username)

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
                "organization": "Updated Host Organization",
                "website": "https://example.org",
                "address": "12 Fieldwork Road",
            },
        )

        self.assertRedirects(response, reverse("portal:company_profile"))
        self.company.refresh_from_db()
        self.assertEqual(self.company.organization, "Updated Host Organization")
        self.assertEqual(self.company.website, "https://example.org")
        self.assertEqual(self.company.address, "12 Fieldwork Road")

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
        self.assertContains(response, "1 record")

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

    def test_clock_in_uses_server_time_and_saves_note_without_gps(self):
        fixed_now = datetime(2026, 1, 5, 9, 30, tzinfo=datetime_timezone.utc)
        self.client.force_login(self.intern_user)
        with patch("portal.views.timezone.now", return_value=fixed_now):
            response = self.client.post(
                reverse("portal:attendance"),
                {"action": "clock_in", "notes": "Started inventory review", "clock_in": "1999-01-01T00:00:00Z"},
            )
        self.assertRedirects(response, reverse("portal:attendance"))
        log = AttendanceLog.objects.get(intern=self.intern)
        self.assertEqual(log.clock_in, fixed_now)
        self.assertEqual(log.notes, "Started inventory review")
        self.assertIsNone(log.clock_in_latitude)

    def test_clock_out_without_clock_in_does_not_create_empty_record(self):
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
        self.client.force_login(self.intern_user)
        response = self.client.post(
            reverse("portal:apply", args=(self.posting.pk,)),
            {"cover_letter": "Interested", "resume": SimpleUploadedFile("fake.pdf", b"not a real PDF")},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Application.objects.filter(intern=self.intern, posting=self.posting).exists())

    def test_blank_student_ids_do_not_collide(self):
        self.intern.student_id = ""
        self.intern.save()
        User.objects.create_user(username="intern-two", password="Safe-example-Password-927!")
        self.assertEqual(InternProfile.objects.filter(student_id__isnull=True).count(), 2)