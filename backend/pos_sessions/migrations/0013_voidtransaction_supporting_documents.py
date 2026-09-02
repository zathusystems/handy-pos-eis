from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pos_sessions', '0012_order_eis_special_sale_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='voidtransaction',
            name='supporting_documents',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Optional supporting document references sent with the MRA EIS void/cancel request',
            ),
        ),
    ]
