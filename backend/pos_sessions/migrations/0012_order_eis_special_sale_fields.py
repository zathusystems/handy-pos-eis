# Generated for MRA EIS special sale compliance fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pos_sessions', '0011_add_pump_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='buyer_authorization_code',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='eis_validation_metadata',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='order',
            name='is_export',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='order',
            name='is_relief_supply',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='order',
            name='vat5_certificate_number',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='vat5_project_number',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='vat5_quantity',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True),
        ),
    ]
