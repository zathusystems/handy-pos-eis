# MRA EIS Official Integration Checklist

Updated: 2026-05-16

## Official References

- Developer resources: https://eis-portal.mra.mw/Home/DeveloperResources
- API guide: https://eis-api.mra.mw/docs/
- Pre-integration guide: https://eis-api.mra.mw/docs/developer_pre_integration_guide.htm
- Terminal activation: https://eis-api.mra.mw/docs/terminal_activation.htm
- Activation request: https://eis-api.mra.mw/docs/request_1.htm
- Activation confirmation: https://eis-api.mra.mw/docs/terminal_activated_confirmation.htm
- Latest configuration: https://eis-api.mra.mw/docs/get_latest_configuration.htm
- Sale transaction: https://eis-api.mra.mw/docs/sale_transaction.htm
- Sale request fields: https://eis-api.mra.mw/docs/request_4.htm
- Sales module endpoints: https://eis-api.mra.mw/docs/sales.htm
- Offline receipt signing: https://eis-api.mra.mw/docs/signing_offline_receipts_print.htm
- Offline replay: https://eis-api.mra.mw/docs/submitting_offline_transactions.htm
- Product status: https://eis-api.mra.mw/docs/product_status.htm
- Certification: https://eis-api.mra.mw/docs/api_compliance_certification.htm

## Backend Alignment Done

- Endpoint defaults now follow the official public API paths:
  - `/api/v1/onboarding/activate-terminal`
  - `/api/v1/onboarding/terminal-activated-confirmation`
  - `/api/v1/configuration/get-latest-configs`
  - `/api/v1/sales/submit-sales-transaction`
  - `/api/v1/sales/last-submitted-online-transaction`
  - `/api/v1/sales/last-submitted-offline-transaction`
  - `/api/v1/utilities/product-status`
  - `/api/v1/utilities/get-terminal-site-products`
- Activation payload now uses `terminalActivationCode` and `environment.platform` / `environment.pos`.
- Activation response parsing now stores `activatedTerminal.terminalId`, `terminalCredentials.jwtToken`, `terminalCredentials.secretKey`, and configuration snapshots.
- Confirmation uses `x-signature` as Base64 HMAC-SHA512 over the TAC using the terminal secret.
- Subsequent calls use the terminal JWT bearer token and prepare `x-eis-message-hash`.
- Sales payloads now use the documented `invoiceHeader`, `invoiceLineItems`, and `invoiceSummary` structure.
- Offline POS receipts now generate an MRA-style offline validation URL and `offlineSignature` from `TI`, `N`, `I`, `V`, `T`, and the terminal secret.
- Product mapping sync now validates against `product-status`; approved site products can be pulled from `get-terminal-site-products`.
- Credit note, debit note, and void records reference the original fiscal invoice number/EIS UUID and submit through the official correction/void endpoints.
- Correction APIs are scoped to the authenticated user's accessible businesses and serializers do not expose TAC, JWT, or terminal secret values.

## Lifecycle Scenario Status

- Sales: ready for sandbox testing through the documented `submit-sales-transaction` payload.
- Refunds / credit notes: ready for sandbox testing through `/api/v1/sales/process-credit-debit-note`.
- Voids: ready for sandbox testing through `/api/v1/sales/cancel-receipt`; local stock restoration happens only after the EIS correcting document is recorded.
- Debit notes: ready for sandbox testing through `/api/v1/sales/process-credit-debit-note`.

## Dev Portal Steps

1. Complete/verify the taxpayer profile on `https://dev-eis-portal.mra.mw`.
2. For product businesses, upload initial stock, wait for approval, create branches, and transfer stock to branch sites.
3. For service businesses, register services in the portal and wait for approval.
4. Apply for a terminal for each branch/site and copy the Terminal Activation Code.
5. Set a non-empty POS product ID for HandyPOS, for example `HandyPOS`, as `MRA_EIS_PRODUCT_ID`.
6. In the app EIS settings, enable EIS, select TEST, enter the TAC, and activate the terminal.
7. Run configuration sync with product sync enabled so tax rates, offline limits, site ID, and approved products are saved locally.
8. Map local inventory items to approved MRA product/service codes.
9. Run sandbox POS sales and confirm receipts show the MRA validation URL/QR payload.
10. Collect certification evidence, then submit the app for MRA API compliance certification.

## Required Environment

For sandbox testing:

```env
MRA_EIS_MODE=TEST
MRA_EIS_BASE_URL=https://dev-eis-api.mra.mw
MRA_EIS_DRY_RUN=False
MRA_EIS_ENABLE_HTTP_CALLS=True
MRA_EIS_ALLOW_LIVE_SUBMISSION=True
MRA_EIS_PRODUCT_ID=HandyPOS
MRA_EIS_ACCESS_KEY=<vendor-access-key-if-issued-for-sandbox>
MRA_EIS_STRICT_PRODUCT_CODES=True
```

For production, use `MRA_EIS_MODE=LIVE`, production credentials, and the final POS product ID/version used during MRA approval.

## Remaining Before Go-Live

- Confirm the final product ID/product version text MRA wants shown in terminal activation records.
- Confirm whether the current dev portal requires `Authorization: Bearer <jwtToken>` or raw JWT in the header; the public guide says bearer after activation.
- Confirm the exact `x-eis-message-hash` plaintext rules per endpoint if MRA provides a newer private Swagger/spec in the portal.
- Run successful sandbox activation, configuration sync, product sync, online sale, offline sale, offline replay, and blocked-terminal scenarios.
- Get written confirmation from MRA for refund/credit-note/void submission schema if those scenarios are included in certification.
- Add POS line-level discount capture before a certification discount test; the current POS order model submits `discount: 0`.
- Encrypt or externally vault terminal secrets/API keys before production deployment; serializers already avoid exposing them over API responses.
- Resolve the pre-existing `business` migration conflict so the full automated test suite can run normally.
