from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0031_purchaseorder_eis_stock_receipt_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='mraproductmapping',
            name='mra_levies',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='MRA levy metadata for this product, normalized as levyTypeId/levyRate rows',
            ),
        ),
    ]
