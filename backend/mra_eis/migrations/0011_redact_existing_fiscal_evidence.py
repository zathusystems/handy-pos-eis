from django.db import migrations


def redact_existing_fiscal_evidence(apps, schema_editor):
    from mra_eis.security import redact_sensitive_data, redact_sensitive_text

    MRAInvoice = apps.get_model('mra_eis', 'MRAInvoice')
    OfflineInvoiceQueue = apps.get_model('mra_eis', 'OfflineInvoiceQueue')
    OfflineAuditLog = apps.get_model('mra_eis', 'OfflineAuditLog')
    InvoiceAuditLog = apps.get_model('mra_eis', 'InvoiceAuditLog')
    ConfigurationSyncLog = apps.get_model('mra_eis', 'ConfigurationSyncLog')

    for invoice in MRAInvoice.objects.all().iterator():
        redacted_response = redact_sensitive_data(invoice.mra_response)
        if redacted_response != invoice.mra_response:
            MRAInvoice.objects.filter(pk=invoice.pk).update(mra_response=redacted_response)

    for queue_entry in OfflineInvoiceQueue.objects.all().iterator():
        redacted_error = redact_sensitive_text(queue_entry.last_sync_error)
        if redacted_error != queue_entry.last_sync_error:
            OfflineInvoiceQueue.objects.filter(pk=queue_entry.pk).update(
                last_sync_error=redacted_error
            )

    for model in (OfflineAuditLog, InvoiceAuditLog):
        for audit in model.objects.all().iterator():
            redacted_details = redact_sensitive_data(audit.details)
            if redacted_details != audit.details:
                model.objects.filter(pk=audit.pk).update(details=redacted_details)

    for sync_log in ConfigurationSyncLog.objects.all().iterator():
        redacted_error = redact_sensitive_text(sync_log.error_message)
        if redacted_error != sync_log.error_message:
            ConfigurationSyncLog.objects.filter(pk=sync_log.pk).update(
                error_message=redacted_error
            )


class Migration(migrations.Migration):

    dependencies = [
        ('mra_eis', '0010_encrypt_terminal_credentials_and_redact_audits'),
    ]

    operations = [
        migrations.RunPython(redact_existing_fiscal_evidence, migrations.RunPython.noop),
    ]
