import uuid

from django.core.validators import MinValueValidator
from django.db import migrations, models
import django.db.models.deletion


def _julian_date(value):
    date_value = value.date()
    year = date_value.year
    month = date_value.month
    day = date_value.day
    if month <= 2:
        year -= 1
        month += 12
    century = year // 100
    correction = 2 - century + (century // 4)
    return int((365.25 * (year + 4716)) // 1 + (30.6001 * (month + 1)) // 1 + day + correction - 1524)


def backfill_invoice_julian_dates_and_sequences(apps, schema_editor):
    MRAInvoice = apps.get_model('mra_eis', 'MRAInvoice')
    FiscalInvoiceSequence = apps.get_model('mra_eis', 'FiscalInvoiceSequence')

    sequence_max = {}
    for invoice in MRAInvoice.objects.all().iterator():
        if not invoice.invoice_date:
            continue
        julian_date = _julian_date(invoice.invoice_date)
        if invoice.fiscal_julian_date != julian_date:
            invoice.fiscal_julian_date = julian_date
            invoice.save(update_fields=['fiscal_julian_date'])

        key = (invoice.terminal_id, julian_date)
        sequence_max[key] = max(sequence_max.get(key, 0), int(invoice.invoice_number or 0))

    for (terminal_id, julian_date), last_sequence in sequence_max.items():
        sequence, _created = FiscalInvoiceSequence.objects.get_or_create(
            terminal_id=terminal_id,
            julian_date=julian_date,
            defaults={'last_sequence': last_sequence},
        )
        if sequence.last_sequence < last_sequence:
            sequence.last_sequence = last_sequence
            sequence.save(update_fields=['last_sequence'])


class Migration(migrations.Migration):

    dependencies = [
        ('mra_eis', '0006_sync_retry_queue_purchase_receipt_choice'),
    ]

    operations = [
        migrations.CreateModel(
            name='FiscalInvoiceSequence',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('julian_date', models.PositiveIntegerField(help_text='MRA Julian date encoded in the fiscal invoice number')),
                ('last_sequence', models.BigIntegerField(default=0, validators=[MinValueValidator(0)])),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('terminal', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fiscal_sequences', to='mra_eis.terminal')),
            ],
            options={
                'ordering': ['-julian_date'],
                'unique_together': {('terminal', 'julian_date')},
            },
        ),
        migrations.AddField(
            model_name='mrainvoice',
            name='fiscal_julian_date',
            field=models.PositiveIntegerField(blank=True, help_text='MRA Julian date used with invoice_number for daily fiscal sequencing', null=True),
        ),
        migrations.AlterField(
            model_name='mrainvoice',
            name='invoice_number',
            field=models.BigIntegerField(help_text='Sequential invoice number per terminal fiscal day'),
        ),
        migrations.RunPython(backfill_invoice_julian_dates_and_sequences, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name='mrainvoice',
            unique_together={('terminal', 'fiscal_julian_date', 'invoice_number')},
        ),
        migrations.AddIndex(
            model_name='fiscalinvoicesequence',
            index=models.Index(fields=['terminal', 'julian_date'], name='mra_eis_fis_termina_58f345_idx'),
        ),
        migrations.AddIndex(
            model_name='mrainvoice',
            index=models.Index(fields=['terminal', 'fiscal_julian_date', 'invoice_number'], name='mra_eis_mra_termina_0de798_idx'),
        ),
    ]
