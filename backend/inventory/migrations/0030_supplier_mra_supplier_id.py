# Generated for MRA EIS supplier ID persistence

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0029_inventoryitem_is_oil'),
    ]

    operations = [
        migrations.AddField(
            model_name='purchaseorder',
            name='mra_supplier_id',
            field=models.PositiveIntegerField(
                blank=True,
                help_text='Supplier ID assigned by MRA EIS at the time stock was received',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='supplier',
            name='mra_supplier_id',
            field=models.PositiveIntegerField(
                blank=True,
                help_text='Supplier ID assigned by MRA EIS, required for goods receiving when available',
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name='purchaseorder',
            index=models.Index(fields=['mra_supplier_id'], name='inventory_p_mra_sup_3d9da3_idx'),
        ),
        migrations.AddIndex(
            model_name='supplier',
            index=models.Index(fields=['mra_supplier_id'], name='inventory_s_mra_sup_fa42c2_idx'),
        ),
    ]
