from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0011_worksite_location"),
    ]

    operations = [
        migrations.AddField(
            model_name="attendancelog",
            name="clock_in_within_location",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="attendancelog",
            name="clock_out_within_location",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
    ]
