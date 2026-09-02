
# HandyPOS MRA EIS Certification Test Case Implementation

Updated: 2026-06-26

This document explains, in simple terms, how HandyPOS handles the main MRA EIS certification test areas and what should be shown during testing.

## 1. Terminal Activation

HandyPOS supports TAC-based terminal activation.

What happens:

- User enters the Terminal Activation Code.
- HandyPOS calls MRA terminal activation.
- The returned terminal ID, token, secret, site details, taxpayer config, and terminal config are saved.
- HandyPOS confirms activation with MRA.
- The activated terminal is bound to the current device identity.

Expected proof:

- Terminal status shows Active.
- Device is allowed to make EIS sales.
- Terminal token and configuration are stored without exposing secrets in the UI.

## 2. Configuration Download

HandyPOS downloads and stores MRA configurations.

What happens:

- Configs are pulled after activation.
- Configs are checked on app/POS startup and before sales.
- If MRA asks for latest config in a response, HandyPOS downloads it.
- MRA offline limits from config are respected.

Important:

- Config freshness is controlled locally by `MRA_EIS_CONFIG_MAX_AGE_HOURS`.
- Offline limits such as maximum offline age and cumulative amount come from MRA config.

Expected proof:

- Manual config sync succeeds.
- Automatic config download happens when MRA requests it.
- Sales are blocked if required configs are missing or stale.

## 3. Product And Tax Mapping

HandyPOS uses MRA-approved product mappings for EIS sales.

What happens:

- Products are pulled from MRA terminal site products.
- Local products are linked to MRA product code, tax type, tax rate, unit, and levies.
- If EIS is enabled, sales require approved/synced product mappings.
- Local manual tax setup is hidden/disabled for EIS businesses.

Expected proof:

- POS product tax comes from MRA mapping.
- Missing or unsynced mappings block EIS sales.
- Non-VAT/VAT taxpayer handling follows synced MRA taxpayer config.

## 4. Online Sales

HandyPOS submits online sales to MRA using the official sale transaction payload.

What happens:

- Each item is calculated line by line.
- Inclusive, exclusive, zero, exempt, and non-rated tax are handled per item.
- Discounts are applied once before VAT calculation.
- Levies are included when configured.
- Fiscal invoice number uses the MRA daily sequence format.
- Terminal block status is checked before sale submission.
- Server time is resolved through MRA ping/time handling when online.

Expected proof:

- Sale is accepted by MRA.
- Receipt shows fiscal receipt number, seller TIN, totals, VAT breakdown, QR, and validation URL.
- MRA portal shows the submitted sale.

## 5. Discounts

HandyPOS supports cashier-selectable discounts created by admins.

What happens:

- Admin creates percentage or fixed discounts.
- Cashier selects an eligible discount in POS.
- Discount is stored on the order and line item.
- MRA payload sends the discount amount on the affected line.
- VAT is recalculated from the discounted amount.

Expected proof:

- A MWK 2,000 item with MWK 500 discount submits as MWK 1,500 total.
- Discount is not subtracted twice.
- Receipt and sale details show the discount clearly.

## 6. B2B Sales

HandyPOS treats buyer TIN sales as B2B.

What happens:

- Buyer TIN is validated before submission.
- Authorization checks are performed when required.
- B2B sales are online only.
- If MRA is unreachable, B2B sale is blocked with a clear message.

Expected proof:

- Valid B2B sale submits online.
- Invalid buyer TIN is blocked before final EIS submission.
- B2B sale does not generate offline receipt.

## 7. VAT Relief / VAT5

HandyPOS supports relief supply fields and VAT5 validation.

What happens:

- Relief sale requires certificate/project details.
- VAT5 details are validated before sale.
- Standard-rated VAT is removed for relief supply.
- Zero/exempt lines remain unchanged.

Expected proof:

- Valid relief sale submits with VAT5 details.
- Invalid certificate details block the sale.
- Receipt/payload show VAT removed only where applicable.

## 8. Offline Sales

HandyPOS supports offline B2C sales when MRA is unreachable but the local backend is reachable.

What happens:

- Offline sale uses cached valid terminal, config, product mapping, and block status.
- B2B and relief sales are not allowed offline.
- Offline receipt gets an offline signature.
- Offline QR/validation URL is generated.
- Invoice is queued for replay.
- Offline sequence and MRA last-offline guard are checked.
- MRA offline age and cumulative amount limits are enforced.

Expected proof:

- Disconnect MRA API only.
- Make normal B2C sale.
- Receipt prints with offline QR/signature.
- Invoice status becomes `offline_queued`.
- Reconnect and sync.
- Invoice becomes `offline_synced`.

## 9. Terminal Blocking

HandyPOS checks terminal block status.

What happens:

- Before online sale, HandyPOS checks MRA terminal blocking message.
- If MRA says blocked, sale is blocked.
- If offline, HandyPOS uses cached block state.
- If cached blocked, offline sale is blocked.

Expected proof:

- Blocked terminal cannot sell.
- Unblocked terminal can sell after refresh/unblock check.

## 10. Receipts And QR

HandyPOS prints MRA-style legal receipts.

Receipt includes:

- Legal receipt start/end text.
- Seller business details.
- Seller TIN.
- VAT registration label from MRA taxpayer config.
- Buyer name/TIN when provided.
- Fiscal receipt number.
- Line items.
- Discounts.
- VAT breakdown.
- Levy breakdown when applicable.
- Total, amount tendered, change.
- Date/time.
- QR code and validation URL.

Expected proof:

- Printed receipt matches preview.
- QR scans successfully.
- QR opens MRA validation details.
- Accepted receipts do not show pending status.

## 11. Voids, Credit Notes, Debit Notes

HandyPOS supports correction flows.

What happens:

- Void cancels the full sale through MRA cancel receipt flow.
- Credit note reduces the original sale.
- Debit note adds charges to the original sale.
- VAT is calculated from the original sale rate.
- Correction failures are queued/retryable where possible.

Expected proof:

- Void appears in MRA cancelled receipts.
- Credit/debit note references the original fiscal receipt.
- Local stock is restored when void succeeds.

## 12. Stock And Inventory

HandyPOS supports EIS stock operations needed for product businesses.

What happens:

- Initial inventory upload supports batching.
- MRA-approved products can be synced.
- Informal purchase can submit stock increase.
- B2B stock transfer receipt can be recorded locally without double-adding stock.
- Warehouse inventory can be viewed.
- Warehouse to branch and branch transfer flows use MRA stock transfer where applicable.
- Insufficient stock blocks sales.

Expected proof:

- Initial stock upload batches correctly.
- Purchase/stock receiving updates EIS where required.
- Selling more than available stock is blocked.
- Warehouse/branch transfer updates stock correctly.

## 13. Last Submissions And Lookup

HandyPOS supports audit tools for MRA checks.

What happens:

- Last online and offline MRA submissions can be checked.
- Local sales can be matched against MRA last submits.
- Receipt lookup by invoice number is available.
- Cancelled receipt lookup is available.

Expected proof:

- Last online/offline submit shows matched local invoice.
- Official receipt lookup returns MRA data.
- Cancelled receipt lookup returns void/cancel requests.

## 14. Printer And Device Support

HandyPOS supports multiple receipt widths and cash drawer commands.

Supported receipt widths:

- 30mm
- 40mm
- 50mm
- 58mm
- 80mm

Supported behavior:

- Receipt preview uses the selected paper width.
- Printing uses the configured default printer.
- Cash drawer can open after cash sale when enabled.
- Windows and Android builds are available.

Expected proof:

- Test print works on the printer used for certification.
- Receipt is not cut before the end.
- Cash drawer opens when enabled.

## 15. Error Handling

HandyPOS keeps cashier messages short and clear.

Examples:

- Device activation required.
- MRA offline.
- Buyer TIN invalid.
- Product mapping missing.
- Stock not enough.
- Config refresh required.
- B2B requires MRA online.

Expected proof:

- Failed EIS checks do not fail silently.
- Cashier sees a clear reason.
- Invalid sales are blocked before creating bad EIS receipts where possible.

## Remaining Certification Proof Needed

These are not necessarily missing code; they are proof needed during certification:

- Submit a real discounted sale and confirm MRA accepts the payload.
- Print and scan online receipt QR.
- Print and scan offline receipt QR.
- Prove offline sale replay after reconnect.
- Prove invalid invoice format is rejected.
- Test the actual printers and devices MRA will inspect.







[Unit]
Description=mwakapos Celery worker
After=network.target redis-server.service
Requires=redis-server.service

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/home/eis_backend/backend
EnvironmentFile=/home/eis_backend/backend/.env
Environment=DJANGO_SETTINGS_MODULE=core.prod-settings
ExecStart=/home/eis_backend/backend/.venv/bin/celery -A core worker --loglevel=INFO
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target




[Unit]
Description=mwakapos Celery beat scheduler
After=network.target redis-server.service mwakapos-celery.service
Requires=redis-server.service

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/home/eis_backend/backend
EnvironmentFile=/home/eis_backend/backend/.env
Environment=DJANGO_SETTINGS_MODULE=core.prod-settings
ExecStart=/home/eis_backend/backend/.venv/bin/celery -A core beat --loglevel=INFO
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target