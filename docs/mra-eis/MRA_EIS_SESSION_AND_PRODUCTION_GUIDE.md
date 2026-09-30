# HandyPOS MRA EIS Session and Production Guide

This document is a practical reference for explaining HandyPOS EIS behavior during MRA testing and for preparing the backend for production.

It describes the behavior implemented in the project. A successful certification still requires live proof against the MRA environment and the actual production devices.

## 1. One-Minute Explanation

HandyPOS integrates with Malawi Revenue Authority Electronic Invoicing System (MRA EIS).

- The Django backend is the authority for fiscal numbering, tax calculation, MRA communication, offline queueing, replay, and receipt records.
- The frontend provides the POS experience and local-first order handling. It is not the durable EIS replay worker.
- MRA remains the external authority that accepts or rejects the fiscal transaction.
- Each activated POS device is registered as an MRA terminal.
- Each terminal has MRA credentials, a taxpayer identity, a terminal position, configuration versions, and fiscal counters.

## 2. Sale Lifecycle

1. The taxpayer obtains a Terminal Activation Code from MRA.
2. The user activates the terminal in HandyPOS.
3. HandyPOS stores the MRA terminal ID, token, secret, taxpayer information, terminal position, and configuration snapshots.
4. HandyPOS downloads MRA configuration, tax rates, offline limits, taxpayer settings, and approved terminal-site products/services.
5. Local inventory items are mapped to MRA-approved product or service codes.
6. The cashier creates a sale in the POS.
7. The frontend sends the order to the backend.
8. The backend validates the terminal, device binding, terminal block status, configuration freshness, product mappings, buyer details, and stock rules.
9. The backend recalculates each line using MRA mapping data and builds the official MRA payload.
10. The backend allocates the fiscal invoice number and submits the sale to MRA, or queues a permitted B2C sale for offline replay.
11. The fiscal invoice, response, audit data, and receipt data are stored locally.
12. The receipt contains the fiscal number, tax information, QR/validation data, and legal receipt text.

Main implementation files:

- `backend/mra_eis/services/core.py` - terminal, configuration, tax, invoice, submission, replay, correction, and stock services.
- `backend/mra_eis/services/client.py` - MRA HTTP calls, authorization, message hashes, signing, and sanitized errors.
- `backend/pos_sessions/views.py` - creates the local order and invokes backend EIS submission.
- `backend/mra_eis/models.py` - terminals, fiscal sequences, invoices, queues, and audit records.

## 3. Online Sales

For an online sale, the backend:

- Resolves the correct activated terminal for the branch and device.
- Checks whether the terminal is active and blocked.
- Checks that the required MRA configuration is current.
- Verifies that every EIS line has an approved, synchronized MRA mapping.
- Recalculates net amount, VAT, levies, discounts, and invoice total.
- Resolves the MRA tax rate ID for every line.
- Uses the terminal/day fiscal sequence.
- Builds `invoiceHeader`, `invoiceLineItems`, and `invoiceSummary`.
- Sends the transaction to `/api/v1/sales/submit-sales-transaction`.
- Stores the MRA response and refreshes the receipt with the returned validation data.

The frontend must not be treated as the final authority for EIS compliance. Backend validation is repeated even when the frontend has already performed a check.

## 4. Offline B2C Sales and Replay

Offline operation is intended for ordinary B2C sales when MRA is unreachable but the local backend and terminal data are available.

When an offline sale is created:

- The backend allocates a fiscal invoice number.
- An `MRAInvoice` is persisted.
- An `OfflineInvoiceQueue` entry is persisted.
- The invoice status becomes `offline_queued`.
- An offline signature and validation URL are generated.
- The customer can receive a receipt immediately.

The frontend may trigger a convenience sync, but it is not the durable queue processor. Closing the app, signing out, or losing the browser session must not remove the backend queue.

Replay is handled by Celery:

- `mra_eis.tasks.sync_offline_invoices_for_online_terminals` runs every 5 minutes by default.
- `mra_eis.tasks.process_mra_retry_queue` runs every 2 minutes by default.
- The worker submits queued invoices in queue order.
- Successful replay marks the queue entry as `synced` and the invoice as `offline_synced`.
- The receipt is refreshed after successful replay.
- Failed entries remain retryable and record the error and attempt count.

Offline replay checks the MRA last-offline transaction before submitting. The sequence guard stops replay if local and MRA sequences do not line up. This protects against duplicate or out-of-order fiscal numbers.

MRA offline age and cumulative amount limits are read from the stored MRA configuration. A sale is blocked when those limits are exceeded.

An offline QR or validation URL may not verify on the MRA portal until replay has completed. It is an offline receipt artifact until MRA has received and accepted the transaction. The post-replay receipt should contain the official MRA response data.

## 5. B2B, Buyer TIN, and Authorization

When a buyer TIN is supplied:

1. HandyPOS checks whether the TIN exists.
2. HandyPOS checks whether MRA requires a buyer authorization code.
3. The cashier must provide the code when required.
4. HandyPOS validates the authorization code.
5. The backend repeats the validation before submission.
6. Buyer TIN, buyer name, and authorization code are included in the MRA invoice header when applicable.

B2B sales are online-only. If MRA cannot be reached, the sale is blocked rather than issued as an offline B2C receipt.

Relevant frontend validation is in `src/components/pos/generic-pos.tsx`. The authoritative backend validation is `EISSaleComplianceService.validate_order_special_fields` in `backend/mra_eis/services/core.py`.

## 6. Tax, Groups A/B/E, and Levies

MRA-approved mapping and configuration data are authoritative for EIS tax.

Supported behavior includes:

- Standard-rated tax.
- Zero-rated tax.
- Exempt tax.
- Inclusive prices.
- Exclusive prices.
- Mixed tax groups on one invoice.
- Line-level discounts before VAT calculation.
- Tax breakdown grouped by MRA tax rate ID.
- Configured levies and levy breakdowns.
- VAT and non-VAT taxpayer configuration.

The test suite includes mixed Group A, Group B, and Group E payload coverage. Each line keeps its own `taxRateId`, and `invoiceSummary.taxBreakDown` reports each group separately.

For a VAT5 relief supply:

- Project number, certificate number, and positive quantity are required.
- MRA validates the VAT5 certificate.
- Standard VAT is removed from applicable standard-rated lines.
- Zero-rated and exempt lines remain unchanged.
- VAT5 details are sent in `vat5CertificateDetails`.

Levies are taken from MRA mapping/configuration data, calculated from the applicable taxable line amount, grouped by levy type and rate, and sent in `invoiceSummary.levyBreakDown`.

## 7. Products and Services

MRA service items are supported with `isProduct: false`.

Services:

- Do not require stock quantity.
- Do not require batch or expiry details.
- Do not create purchases.
- Do not participate in stock transfers.
- Still require an approved MRA service mapping, price, tax data, and valid fiscal payload.

Physical products continue to use inventory, stock availability, batch, expiry, receiving, and transfer rules where applicable.

## 8. Receipts and Statuses

Receipts include:

- Seller name, address, TIN, and VAT registration label.
- Buyer details when supplied.
- Fiscal invoice number.
- Product or service lines.
- Discounts.
- VAT breakdown.
- Levy breakdown.
- Total, amount tendered, change, date, and time.
- QR code and validation URL.
- `*** START OF LEGAL RECEIPT ***`.
- `*** END OF LEGAL RECEIPT ***`.

Important statuses:

- `PENDING` - not yet confirmed by EIS.
- `SUBMITTED` - submission was confirmed by the backend/MRA response.
- `ACCEPTED` - the local record has been classified as accepted where the response/reconciliation supports it.
- `offline_queued` - offline receipt issued and waiting for replay.
- `offline_synced` - offline transaction replay completed.
- `REJECTED` - MRA rejected the transaction or a non-retryable failure was recorded.

A receipt printing successfully does not, by itself, prove that MRA accepted the transaction. The fiscal submission state and MRA response must be checked.

## 9. Voids, Credit Notes, and Debit Notes

HandyPOS supports:

- Full voids through MRA's cancel-receipt flow.
- Credit notes linked to the original fiscal invoice.
- Debit notes linked to the original fiscal invoice.
- Correction retry records for retryable failures.
- Stock restoration after a successful void when enabled.

Corrections should never be described as deleting the original fiscal invoice. They create an MRA correction record referencing the original transaction.

## 10. Security and Audit

The integration uses:

- One-time Terminal Activation Codes.
- MRA terminal JWT/Bearer tokens.
- Terminal secrets.
- HMAC-SHA512 signatures/message hashes.
- HTTPS and SSL verification.
- Encrypted terminal credentials at rest.
- Device binding.
- Audit records for activation, submission, rejection, queueing, replay, and correction.

Secrets, tokens, and sensitive error content should not be displayed to cashiers or exposed through normal serializers.

## 11. Production Backend Settings

The production settings module is `core.prod-settings`. The exact environment values must be supplied through the server environment file, not committed to Git.

### Required production baseline

```env
DJANGO_SETTINGS_MODULE=core.prod-settings

MRA_EIS_MODE=LIVE
MRA_EIS_BASE_URL=https://eis-api.mra.mw
MRA_EIS_DRY_RUN=False
MRA_EIS_ENABLE_HTTP_CALLS=True
MRA_EIS_ALLOW_LIVE_SUBMISSION=True
MRA_EIS_VERIFY_SSL=True

MRA_EIS_PRODUCT_ID=HandyPOS
MRA_EIS_ACCESS_KEY=<production-access-key>
MRA_EIS_SECRET_KEY=<production-secret-key>
MRA_EIS_CREDENTIAL_ENCRYPTION_KEY=<strong-encryption-key>

MRA_EIS_REQUIRE_LOCAL_TAC=True
MRA_EIS_STRICT_PRODUCT_CODES=True
MRA_EIS_DEFAULT_CURRENCY=MWK
```

### Compliance and sequencing settings

These should remain enabled for production unless MRA gives written instructions otherwise:

```env
MRA_EIS_REQUIRE_FRESH_CONFIG_FOR_SALES=True
MRA_EIS_REQUIRE_OFFLINE_REPLAY_SEQUENCE_GUARD=True
MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES=True
MRA_EIS_ENFORCE_TERMINAL_DEVICE_BINDING=True
MRA_EIS_CHECK_TERMINAL_BLOCK_BEFORE_SALE=True
MRA_EIS_VALIDATE_BUYER_TIN_BEFORE_SALE=True
MRA_EIS_VALIDATE_VAT5_BEFORE_SALE=True
MRA_EIS_ADJUST_STOCK_ON_VOID=True
```

Usually keep this disabled in production:

```env
MRA_EIS_ALWAYS_OFFLINE_B2C=False
```

When set to `True`, ordinary B2C sales are deliberately issued offline even when MRA is reachable. This is a special operating policy, not a replacement for EIS connectivity.

### Message-hash settings

Use the message-hash format confirmed by MRA:

```env
MRA_EIS_MESSAGE_HASH_INPUT_MODE=canonical_json
MRA_EIS_RECORD_MESSAGE_HASH_EVIDENCE=True
MRA_EIS_MESSAGE_HASH_INPUT_CONFIRMED_BY_MRA=True
MRA_EIS_LOG_MESSAGE_HASH_INPUT=False
```

Do not enable raw message-hash input logging in production because payloads may contain sensitive business or buyer data.

### Offline validation URL

```env
MRA_EIS_OFFLINE_VALIDATION_BASE_URL=https://eis-portal.mra.mw/ReceiptValidation/Validate/
```

The test environment must use the dev portal URL instead. Never mix production API credentials with a dev base URL or production credentials with test receipts.

## 12. Required Production Services

The backend needs all of the following:

1. Django/Gunicorn application service.
2. Redis broker/result backend.
3. Celery worker using `celery -A core worker --loglevel=INFO`.
4. Celery beat using `celery -A core beat --loglevel=INFO`.

The worker and beat must use the same code release, virtual environment, environment file, `DJANGO_SETTINGS_MODULE`, Redis URL, and database.

Example service checks:

```bash
systemctl status redis-server
systemctl status <project>-celery
systemctl status <project>-celery-beat
sudo journalctl -u <project>-celery -f
sudo journalctl -u <project>-celery-beat -f
```

The actual service names depend on the deployment. Confirm them with:

```bash
systemctl list-units --type=service | grep -Ei 'celery|redis|gunicorn'
```

Expected replay log messages include:

```text
[MRA REPLAY] start
[MRA REPLAY] attempting
[MRA REPLAY] synced
[MRA REPLAY] complete
```

If the queue remains pending, check the Celery worker first, then beat, Redis, the database connection, the terminal's `is_online` state, MRA API availability, and the offline sequence guard.

## 13. Production Deployment Checklist

- Set `DJANGO_SETTINGS_MODULE=core.prod-settings`.
- Set `MRA_EIS_MODE=LIVE`.
- Use `https://eis-api.mra.mw`, never the dev host.
- Set production MRA credentials.
- Set a valid credential encryption key.
- Confirm SSL verification is enabled.
- Confirm dry-run is disabled.
- Confirm live submission is enabled.
- Confirm the MRA product ID/version is the approved value.
- Activate each production terminal using its own MRA TAC.
- Confirm the terminal is bound to the intended device and branch.
- Sync the latest MRA configuration.
- Sync approved products and services.
- Map every saleable product/service to an approved MRA code.
- Verify the taxpayer VAT/non-VAT configuration.
- Confirm offline limits came from MRA configuration.
- Confirm Redis, Celery worker, and Celery beat are active.
- Test one online B2C sale and verify it in MRA.
- Test one offline B2C sale and replay it after reconnecting.
- Test a buyer TIN sale and authorization-code scenario.
- Test mixed Groups A, B, and E.
- Test a service item.
- Test VAT5 relief if applicable to the taxpayer.
- Test levies/tourism-related products if applicable.
- Test the actual Windows and Android printers.
- Confirm logs do not expose tokens, secrets, or raw sensitive payloads.

## 14. Certification Questions and Short Answers

**Does the frontend submit directly to MRA?**

No. The frontend sends the order to the backend. The backend validates, calculates, sequences, signs, and submits the EIS transaction.

**What happens if the cashier closes the app after an offline sale?**

The backend queue remains persisted. Celery replays it independently of the frontend session.

**Can a B2B sale be issued offline?**

No. Buyer TIN and authorization validation require MRA online confirmation.

**How are duplicate invoice numbers prevented?**

Online and offline receipts use a terminal/day fiscal sequence. Replay also compares the local queue with MRA's last offline sequence.

**How is tax calculated?**

The backend recalculates from MRA-approved product mappings and configuration, then sends line tax rate IDs and the grouped tax breakdown.

**Why might an offline receipt not verify immediately?**

It is provisional until MRA receives it through replay. The receipt should be checked again after replay.

**What does a successful print prove?**

Only that the printer received the receipt. EIS submission status and the MRA response must be checked separately.

## 15. Live Proof Still Required

The code and automated tests cover the main behaviors, but certification should still demonstrate:

- Terminal activation.
- Configuration sync.
- Product and service mapping.
- Online B2C sale.
- Mixed A/B/E tax sale.
- Service-product sale.
- Buyer TIN and authorization-code validation.
- VAT5 relief sale where applicable.
- Levy/tourism-related sale where applicable.
- Offline B2C sale.
- Backend replay after reconnect.
- QR verification after replay.
- Void or correction flow.
- Actual printer output on the devices used for certification.

## 16. Primary Project References

- [MRA official integration checklist](official-integration-checklist.md)
- [Certification test-case implementation](certification-test-case-implementation.md)
- [MRA EIS deployment guide](../../backend/mra_eis/DEPLOYMENT_GUIDE.md)
- [MRA EIS settings template](../../backend/mra_eis/SETTINGS_TEMPLATE.md)
- [Production settings](../../backend/core/prod-settings.py)
- [Celery tasks](../../backend/mra_eis/tasks.py)
- [EIS models](../../backend/mra_eis/models.py)
- [EIS services](../../backend/mra_eis/services/core.py)
