from django.db import migrations, models


def copy_legacy_approval(apps, schema_editor):
    AttendanceLog = apps.get_model("portal", "AttendanceLog")
    AttendanceLog.objects.filter(approved=True).update(
        time_in_approved=True,
        time_out_approved=True,
    )


def restore_legacy_approval(apps, schema_editor):
    AttendanceLog = apps.get_model("portal", "AttendanceLog")
    AttendanceLog.objects.filter(
        time_in_approved=True,
        time_out_approved=True,
    ).update(approved=True)


class Migration(migrations.Migration):
    dependencies = [("portal", "0002_remove_scorecard_uniq_scorecard_intern_week_and_more")]

    operations = [
        migrations.AddField(
            model_name="attendancelog",
            name="time_in_approved",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="attendancelog",
            name="time_out_approved",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(copy_legacy_approval, restore_legacy_approval),
        migrations.RemoveField(
            model_name="attendancelog",
            name="approved",
        ),
    ]