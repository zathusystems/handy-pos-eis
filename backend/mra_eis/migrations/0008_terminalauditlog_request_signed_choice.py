from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mra_eis', '0007_daily_fiscal_invoice_sequence'),
    ]

    operations = [
        migrations.AlterField(
            model_name='terminalauditlog',
            name='action',
            field=models.CharField(
                choices=[
                    ('activated', 'Terminal Activated'),
                    ('token_refreshed', 'Token Refreshed'),
                    ('online_status_changed', 'Online Status Changed'),
                    ('configuration_updated', 'Configuration Updated'),
                    ('mra_request_signed', 'MRA Request Signed'),
                    ('suspended', 'Terminal Suspended'),
                    ('deactivated', 'Terminal Deactivated'),
                ],
                max_length=50,
            ),
        ),
    ]
