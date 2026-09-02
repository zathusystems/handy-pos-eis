from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0033_supplier_contact_region'),
    ]

    operations = [
        migrations.AddField(
            model_name='mraproductmapping',
            name='is_product',
            field=models.BooleanField(
                default=True,
                help_text='True for physical products; false for MRA service items that do not carry stock.',
            ),
        ),
    ]
