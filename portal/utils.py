import math
from io import BytesIO

import qrcode
from django.core import signing
from django.utils import timezone
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from .models import AuditLog


def haversine_distance_km(latitude_a, longitude_a, latitude_b, longitude_b):
    earth_radius_km = 6371.0088
    lat_a, lat_b = math.radians(float(latitude_a)), math.radians(float(latitude_b))
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(float(longitude_b) - float(longitude_a))
    haversine = math.sin(delta_lat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    return 2 * earth_radius_km * math.asin(math.sqrt(min(1, haversine)))


def audit(actor, action, instance, **details):
    return AuditLog.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        object_type=instance._meta.label,
        object_id=str(instance.pk or ""),
        details=details,
    )


def attendance_pdf(intern, logs):
    output = BytesIO()
    document = canvas.Canvas(output, pagesize=letter)
    document.setTitle(f"Attendance record - {intern.user.get_full_name() or intern.user.username}")
    document.setFont("Helvetica-Bold", 16)
    document.drawString(48, 748, "Internship attendance record")
    document.setFont("Helvetica", 10)
    document.drawString(48, 728, f"Intern: {intern.user.get_full_name() or intern.user.username}")
    y = 690
    document.setFont("Helvetica-Bold", 9)
    document.drawString(48, y, "Date")
    document.drawString(160, y, "Time in")
    document.drawString(270, y, "Time out")
    document.drawString(385, y, "Hours")
    document.drawString(465, y, "Status")
    document.setFont("Helvetica", 9)
    for entry in logs:
        y -= 18
        if y < 60:
            document.showPage()
            y = 740
        document.drawString(48, y, entry.work_date.isoformat())
        document.drawString(160, y, timezone.localtime(entry.clock_in).strftime("%H:%M") if entry.clock_in else "-")
        document.drawString(270, y, timezone.localtime(entry.clock_out).strftime("%H:%M") if entry.clock_out else "-")
        document.drawString(385, y, str(entry.hours_worked))
        document.drawString(465, y, entry.get_status_display())
    document.save()
    output.seek(0)
    return output


def certificate_pdf(intern):
    token = signing.dumps({"intern_id": intern.pk, "issued_at": timezone.now().isoformat()}, salt="intern-certificate")
    qr_image = qrcode.make(token)
    qr_buffer = BytesIO()
    qr_image.save(qr_buffer, format="PNG")
    qr_buffer.seek(0)

    output = BytesIO()
    document = canvas.Canvas(output, pagesize=letter)
    document.setTitle("Internship completion certificate")
    document.setFont("Helvetica-Bold", 22)
    document.drawCentredString(306, 675, "Certificate of Completion")
    document.setFont("Helvetica", 12)
    document.drawCentredString(306, 620, "This certifies that")
    document.setFont("Helvetica-Bold", 18)
    document.drawCentredString(306, 585, intern.user.get_full_name() or intern.user.username)
    document.setFont("Helvetica", 12)
    document.drawCentredString(306, 545, "has completed the internship program.")
    document.drawImage(qr_buffer, 478, 82, width=82, height=82)
    document.setFont("Helvetica", 8)
    document.drawString(48, 96, f"Issued {timezone.localdate().isoformat()} | Ref {token[:18]}")
    document.save()
    output.seek(0)
    return output