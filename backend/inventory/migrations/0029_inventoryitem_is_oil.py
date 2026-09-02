from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0028_make_mra_code_optional'),
    ]

    operations = [
        migrations.AddField(
            model_name='inventoryitem',
            name='is_oil',
            field=models.BooleanField(default=False),
        ),
    ]
