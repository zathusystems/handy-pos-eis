# Generated for MRA EIS official configuration types

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mra_eis', '0003_sync_retry_queue_operation_choices'),
    ]

    operations = [
        migrations.AlterField(
            model_name='mraconfiguration',
            name='config_type',
            field=models.CharField(
                choices=[
                    ('tax_rules', 'Tax Rules'),
                    ('receipt_format', 'Receipt Format'),
                    ('product_codes', 'Product Codes'),
                    ('system_settings', 'System Settings'),
                    ('global_configuration', 'MRA Global Configuration'),
                    ('terminal_configuration', 'MRA Terminal Configuration'),
                    ('taxpayer_configuration', 'MRA Taxpayer Configuration'),
                    ('terminal_site_products', 'MRA Terminal Site Products'),
                ],
                max_length=50,
            ),
        ),
    ]
