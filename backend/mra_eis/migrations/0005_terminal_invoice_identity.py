# Generated for MRA EIS invoice number identity fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mra_eis', '0004_alter_mraconfiguration_config_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='terminal',
            name='mra_taxpayer_id',
            field=models.BigIntegerField(
                blank=True,
                help_text='Numeric taxpayer ID from MRA activation response; used in fiscal invoice number generation',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='terminal',
            name='terminal_position',
            field=models.PositiveIntegerField(
                blank=True,
                help_text='Terminal position from MRA activation response; used in fiscal invoice number generation',
                null=True,
            ),
        ),
    ]
