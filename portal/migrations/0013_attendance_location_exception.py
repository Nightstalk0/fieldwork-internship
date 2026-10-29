from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0012_attendancelog_within_location"),
    ]

    operations = [
        migrations.AddField(
            model_name="attendancelog",
            name="clock_in_distance_meters",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="attendancelog",
            name="clock_in_allowed_radius_meters",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="attendancelog",
            name="clock_in_location_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="attendancelog",
            name="clock_out_distance_meters",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="attendancelog",
            name="clock_out_allowed_radius_meters",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="attendancelog",
            name="clock_out_location_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="attendancefacecapture",
            name="location_status",
            field=models.CharField(
                choices=[
                    ("unknown", "Location status not recorded"),
                    ("inside", "Within worksite radius"),
                    ("outside", "Outside worksite radius"),
                    ("unavailable", "GPS unavailable"),
                ],
                default="unknown",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="attendancefacecapture",
            name="distance_meters",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="attendancefacecapture",
            name="allowed_radius_meters",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="attendancefacecapture",
            name="location_reason",
            field=models.TextField(blank=True),
        ),
    ]
