# Generated for purchase receipt retry operation label

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mra_eis', '0005_terminal_invoice_identity'),
    ]

    operations = [
        migrations.AlterField(
            model_name='syncretryqueue',
            name='operation_type',
            field=models.CharField(
                choices=[
                    ('submit_invoice', 'Submit Invoice'),
                    ('submit_pos_order', 'Submit POS Order'),
                    ('sync_offline_invoices', 'Sync Offline Invoices'),
                    ('refresh_token', 'Refresh Token'),
                    ('fetch_configuration', 'Fetch Configuration'),
                    ('submit_credit_note', 'Submit Credit Note'),
                    ('submit_debit_note', 'Submit Debit Note'),
                    ('submit_void_transaction', 'Submit Void Transaction'),
                    ('submit_stock_payload', 'Submit Stock Payload'),
                    ('submit_purchase_item_receipt', 'Submit Purchase Item Receipt'),
                ],
                max_length=50,
            ),
        ),
    ]
