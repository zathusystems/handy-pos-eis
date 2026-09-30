Choose an account
to continue to
0885505164
    Gusto
    gusto265mw@gmail.com
    Signed out
    Jamizo
    jamizomw@gmail.com
    Signed out
    Justice
    justice265mw@gmail.com
    Signed out
    Hanneck Malembo
    zathusystemstax@gmail.com
    Signed out
    Trizamw
    trizamw265@gmail.com


    Signed out
    BONFACE MALEMBO
    bba25-bmalembo@mubas.ac.mw
    Signed out
    Hanneck Malembo
    malembohanneck@gmail.com
    Hanneck Malembo
    zathusystems@gmail.com
    Signed out
    Chims
    
    chims265mw@gmail.com
    Signed out
    Tspoon265
    tspoon265mw@gmail.com
    Signed out
    Shawn
    shawn265mw@gmail.com
    Josh
    joshmw265@gmail.
    
    
    Brightmwa
    brightmwa020@gmail.com
    Gasten
    gasten265mw@gmail.com
    Lestina
    lestina265mwa@gmail.com
    Lindazathu
    lindazathu265@gmail.com
    Ibrahim
    ibrahim22mw@gmail.com
    Promise
    promise265mw@gmail.com
    Foster
    fostermw265@gmail.com


    zeka@express-travel-ticketing.online
    hanne265@express-travel-ticketing.online
    hamza@express-travel-ticketing.online
    bon@express-travel-ticketing.online
    saukanmores@express-travel-ticketing.online
    stonken@express-travel-ticketing.online


    <!-- Dear future viewers, this is a masterpiece! You can enjoy it without any second thoughts. The storyline is engaging, the characters are well-developed, and the acting is superb. Trust me, you won't regret watching this!
And after watching this, I highly recommended" THR DOUBLE " !(Historical cdrama)Another masterpiece that will capture your heart with its unique storyline  -->



Likely Certification Gaps
1 .... Discounts are not implemented for sales.
MRA test cases require fixed and percentage discounts. Our sale payload currently sends discount: 0 always in core.py (line 7232). This is a certification blocker.

2 Insufficient stock is not hard-blocked.
POS allows increasing cart quantity without checking available stock in page.tsx (line 971), and local decrement can floor stock to zero while still allowing the sale in page.tsx (line 1392). MRA explicitly tests selling more than available stock.

3 B2B offline rule is not enforced clearly.
The MRA test says B2B must be online only. Our network-failure fallback can convert an online sale into offline mode in core.py (line 7456). If buyerTIN is present, we should block offline fallback.

4 Relief supply VAT removal is incomplete/risky.
VAT5 validation and payload fields exist, but sale line VAT calculation still uses normal tax calculation before building the payload in core.py (line 7218). MRA tests expect standard-rated VAT to be removed for relief supply, while zero/exempt remain unchanged.

5 Terminal block check is not guaranteed before every sale.
_ensure_terminal_can_issue_sale checks local active status/credentials, but I don’t see a live MRA block-status call there in core.py (line 6868). MRA test case says check block status for every transaction.

6 Tourism levy receipt display is incomplete on frontend.
Backend receipt text includes levy lines in receipt.py (line 212), and sale payload supports levy breakdown. But the React printed receipt only renders VAT breakdown and total in receipt.tsx (line 754). Hospitality levy tests may fail receipt review.

7 .... Offline certification depends on backend availability.
POS blocks sale if it cannot reach the backend in page.tsx (line 1068). That is okay only if the desktop app runs a local backend. If testers disconnect internet and your frontend talks to a remote backend, offline sales will fail.

8 Printer/device test coverage is not complete.
MRA lists 30mm/40mm/50mm/80mm printers, cash drawer, Android/iPhone/Windows/Mac/Web. UI only exposes 58mm/80mm receipt widths in printer-config-screen.tsx (line 587). Cash drawer command exists in escpos-service.ts (line 297), but we need prove it is wired into checkout/printer config.

9 Config response handling may be too narrow.
Code downloads configs when shouldDownloadLatestConfig === true in core.py (line 7632). If MRA returns this flag elsewhere or as "true"/1, we could miss it.

0 Server time needs confirmation.
   Payload uses backend order.created_at as invoice time in core.py (line 7270). If MRA certification expects actual MRA server time or a time-sync endpoint, we need confirmation.

Fix Order I Recommend
Stock hard-blocking.
Sale discounts end-to-end.
B2B online-only enforcement.
Relief supply VAT recalculation.
Per-sale terminal block preflight.
Levy display on frontend receipt.
Offline certification test with the real Tauri local-backend setup.
Printer/cash drawer certification proof.





Yes. PDF-only, the remaining gaps are:

TC-INV-004 Discounts
Percentage and fixed discounts are not implemented for sale submission. Payload still sends discount: 0.

Ping Server For Server Time
Sales still use the POS/backend order time for invoiceDateTime, not confirmed MRA server time.

TC-UT-017 Different Devices / Hardware
Windows and Android builds exist, but the PDF also lists iPhone, Mac, Web, cash drawer, cash registers, Bluetooth printers, and 30/40/50/80mm printers. We need actual test coverage/proof for all devices/printers MRA will test.

TC-OFF-007 Offline Transactions
This must be tested in the exact certification setup. If the app uses the local Tauri backend, it can pass. If it depends on a remote backend while internet is disconnected, it will fail.

TC-REC-011 / TC-REC-012 QR Receipt Validation
QR generation is implemented, but we still need live proof that printed QR scans correctly, opens the MRA URL, and matches receipt details/timestamp.

TC-INV-014 Invalid Invoice Format
Correct invoice generation is implemented, but we still need a negative test proving an invalid invoice format is denied before transmission.

That is the PDF-only list. I am not counting warehouse transfers, reprints, or other portal behavior because they are not clearly in that PDF checklist.