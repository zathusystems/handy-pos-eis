from django.db import migrations

import mra_eis.fields


def secure_existing_mra_data(apps, schema_editor):
    from mra_eis.fields import encrypt_mra_credential
    from mra_eis.security import redact_sensitive_data, redact_sensitive_text

    Terminal = apps.get_model('mra_eis', 'Terminal')
    TerminalAuditLog = apps.get_model('mra_eis', 'TerminalAuditLog')
    MRAAPIError = apps.get_model('mra_eis', 'MRAAPIError')

    for terminal in Terminal.objects.all().iterator():
        Terminal.objects.filter(pk=terminal.pk).update(
            mra_api_key=encrypt_mra_credential(terminal.mra_api_key),
            mra_token=encrypt_mra_credential(terminal.mra_token),
        )

    for audit in TerminalAuditLog.objects.all().iterator():
        redacted_details = redact_sensitive_data(audit.details)
        if redacted_details != audit.details:
            TerminalAuditLog.objects.filter(pk=audit.pk).update(details=redacted_details)

    for api_error in MRAAPIError.objects.all().iterator():
        redacted_message = redact_sensitive_text(api_error.error_message)
        if redacted_message != api_error.error_message:
            MRAAPIError.objects.filter(pk=api_error.pk).update(error_message=redacted_message)


class Migration(migrations.Migration):

    dependencies = [
        ('mra_eis', '0009_allow_multiple_terminals_per_branch'),
    ]

    operations = [
        migrations.AlterField(
            model_name='terminal',
            name='mra_api_key',
            field=mra_eis.fields.EncryptedTextField(
                help_text='API key for MRA communication (encrypted at rest)'
            ),
        ),
        migrations.AlterField(
            model_name='terminal',
            name='mra_token',
            field=mra_eis.fields.EncryptedTextField(
                blank=True,
                help_text='Current authentication token from MRA (encrypted at rest)',
            ),
        ),
        migrations.RunPython(secure_existing_mra_data, migrations.RunPython.noop),
    ]
