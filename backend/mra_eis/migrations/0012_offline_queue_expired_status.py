from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mra_eis', '0011_redact_existing_fiscal_evidence'),
    ]

    operations = [
        migrations.AlterField(
            model_name='offlineinvoicequeue',
            name='status',
            field=models.CharField(
                choices=[
                    ('queued', 'Queued'),
                    ('syncing', 'Syncing'),
                    ('synced', 'Synced'),
                    ('failed', 'Failed'),
                    ('expired', 'Expired - MRA time limit exceeded'),
                ],
                default='queued',
                max_length=20,
            ),
        ),
    ]
