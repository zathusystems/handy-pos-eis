# Generated for MRA EIS terminal-per-device compliance.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mra_eis', '0008_terminalauditlog_request_signed_choice'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='terminal',
            unique_together=set(),
        ),
        migrations.AddIndex(
            model_name='terminal',
            index=models.Index(fields=['business', 'branch', 'device_serial'], name='mra_eis_ter_busines_d1fdd0_idx'),
        ),
    ]
