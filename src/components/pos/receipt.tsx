
'use client';

import React from 'react';
import { useLiveQuery } from 'dexie-react-hooks';
import { format } from 'date-fns';
import QRCode from 'react-qr-code';
import { db, type Order, type Business } from '@/lib/db';
import { getOfflineBusinessProfile } from '@/lib/business-profile';
import { normalizePrinterPaperWidth, type PrinterPaperWidth } from '@/lib/services/printer-service';

interface ReceiptProps {
    order: Order;
    business?: Business;
    currencyFormatter: (amount: number) => string;
    paperWidth?: PrinterPaperWidth;
    showQRCode?: boolean;
    showHeader?: boolean;
    showFooter?: boolean;
    showItemDetails?: boolean;
    showTaxBreakdown?: boolean;
    copyNumber?: number; // 1 = Original, 2+ = Copy
    elementId?: string;
    enablePrintStyles?: boolean;
}

export const Receipt = ({ 
  order, 
  business, 
  currencyFormatter,
  paperWidth = '80mm',
  showQRCode = true,
  showHeader = true,
  showFooter = true,
  showItemDetails = true,
  showTaxBreakdown = true,
  copyNumber = 1,
  elementId = 'receipt-printable-area',
  enablePrintStyles = true,
}: ReceiptProps) => {
  const toFiniteNumber = (value: unknown, fallback: number = 0): number => {
    if (typeof value === 'number') {
      return Number.isFinite(value) ? value : fallback;
    }

    if (typeof value === 'string') {
      const trimmed = value.trim();
      if (!trimmed) {
        return fallback;
      }
      const normalized = trimmed.replace(/[^0-9.-]/g, '');
      const parsed = Number.parseFloat(normalized);
      return Number.isFinite(parsed) ? parsed : fallback;
    }

    const parsed = Number.parseFloat(String(value ?? ''));
    return Number.isFinite(parsed) ? parsed : fallback;
  };

  const toOptionalFiniteNumber = (value: unknown): number | null => {
    if (value === null || value === undefined) {
      return null;
    }
    if (typeof value === 'string' && value.trim() === '') {
      return null;
    }
    const parsed = toFiniteNumber(value, Number.NaN);
    return Number.isFinite(parsed) ? parsed : null;
  };

  const toTrimmedString = (value: unknown): string => {
    if (value === null || value === undefined) {
      return '';
    }
    return String(value).trim();
  };

  const resolveCashierReceiptLabel = (...candidates: Array<unknown>): string => {
    for (const candidate of candidates) {
      const raw = toTrimmedString(candidate);
      if (!raw) {
        continue;
      }

      const exactNumericMatch = raw.match(/^\d+$/);
      if (exactNumericMatch) {
        const parsed = Number.parseInt(exactNumericMatch[0], 10);
        if (Number.isFinite(parsed) && parsed > 0) {
          return `Cashier ${parsed}`;
        }
      }
    }

    return 'Cashier';
  };

  const resolveBuyerField = (...candidates: Array<unknown>): string => {
    for (const candidate of candidates) {
      const trimmed = toTrimmedString(candidate);
      if (trimmed) {
        return trimmed;
      }
    }
    return '';
  };

  const formatSignaturePreview = (value: string): string => {
    if (!value) {
      return '';
    }
    if (value.length <= 24) {
      return value;
    }
    return `${value.slice(0, 12)}...${value.slice(-8)}`;
  };

  const normalizeReceiptKey = (value: string): string => value.replace(/[^a-z0-9]/gi, '').toLowerCase();

  const parseJsonCandidate = (value: string): unknown | null => {
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  };

  const isValidationUrl = (value: unknown): boolean => {
    const raw = toTrimmedString(value);
    return /^https?:\/\//i.test(raw) || /receiptvalidation\/validate/i.test(raw);
  };

  const findNestedString = (source: unknown, keys: string[]): string => {
    const wantedKeys = new Set(keys.map(normalizeReceiptKey));
    const queue: unknown[] = [source];
    const seen = new Set<unknown>();

    while (queue.length > 0) {
      const current = queue.shift();
      if (current === null || current === undefined) {
        continue;
      }

      if (typeof current === 'string') {
        const parsed = parseJsonCandidate(current.trim());
        if (parsed && typeof parsed === 'object') {
          queue.push(parsed);
        }
        continue;
      }

      if (typeof current !== 'object') {
        continue;
      }

      if (seen.has(current)) {
        continue;
      }
      seen.add(current);

      if (Array.isArray(current)) {
        queue.push(...current);
        continue;
      }

      for (const [key, value] of Object.entries(current as Record<string, unknown>)) {
        const normalizedKey = normalizeReceiptKey(key);
        if (wantedKeys.has(normalizedKey)) {
          const resolved = toTrimmedString(value);
          if (resolved) {
            return resolved;
          }
        }
        if (value && typeof value === 'object') {
          queue.push(value);
        } else if (typeof value === 'string' && value.trim().startsWith('{')) {
          const parsed = parseJsonCandidate(value.trim());
          if (parsed && typeof parsed === 'object') {
            queue.push(parsed);
          }
        }
      }
    }

    return '';
  };

  const findNestedArrays = (source: unknown, keys: string[]): unknown[][] => {
    const wantedKeys = new Set(keys.map(normalizeReceiptKey));
    const matches: unknown[][] = [];
    const queue: unknown[] = [source];
    const seen = new Set<unknown>();

    while (queue.length > 0) {
      const current = queue.shift();
      if (current === null || current === undefined) {
        continue;
      }

      if (typeof current === 'string') {
        const parsed = parseJsonCandidate(current.trim());
        if (parsed && typeof parsed === 'object') {
          queue.push(parsed);
        }
        continue;
      }

      if (typeof current !== 'object') {
        continue;
      }

      if (seen.has(current)) {
        continue;
      }
      seen.add(current);

      if (Array.isArray(current)) {
        queue.push(...current);
        continue;
      }

      for (const [key, value] of Object.entries(current as Record<string, unknown>)) {
        const normalizedKey = normalizeReceiptKey(key);
        if (wantedKeys.has(normalizedKey) && Array.isArray(value)) {
          matches.push(value);
        }

        if (value && typeof value === 'object') {
          queue.push(value);
        } else if (typeof value === 'string' && value.trim().startsWith('{')) {
          const parsed = parseJsonCandidate(value.trim());
          if (parsed && typeof parsed === 'object') {
            queue.push(parsed);
          }
        }
      }
    }

    return matches;
  };

  const resolveReceiptValidationPayload = (...candidates: unknown[]): { payload: string; mode: 'online' | 'offline' | 'unknown' } => {
    const onlineKeys = ['validationURL', 'validationUrl', 'validation_url', 'mraValidationURL', 'mra_validation_url'];
    const offlineKeys = ['offlineValidationURL', 'offlineValidationUrl', 'offline_validation_url'];
    const qrKeys = ['qrCodePayload', 'qr_code_payload', 'qrPayload', 'qr_payload'];

    for (const candidate of candidates) {
      const onlineUrl = findNestedString(candidate, onlineKeys);
      if (isValidationUrl(onlineUrl)) {
        return { payload: onlineUrl, mode: 'online' };
      }
    }

    for (const candidate of candidates) {
      const offlineUrl = findNestedString(candidate, offlineKeys);
      if (isValidationUrl(offlineUrl)) {
        return { payload: offlineUrl, mode: 'offline' };
      }
    }

    for (const candidate of candidates) {
      const rawCandidate = toTrimmedString(candidate);
      if (isValidationUrl(rawCandidate)) {
        return { payload: rawCandidate, mode: 'unknown' };
      }

      const nestedQrPayload = findNestedString(candidate, qrKeys);
      if (isValidationUrl(nestedQrPayload)) {
        return { payload: nestedQrPayload, mode: 'unknown' };
      }
    }

    return { payload: '', mode: 'unknown' };
  };

  const formatSafeCurrency = (value: unknown): string => currencyFormatter(toFiniteNumber(value, 0));
  const offlineBusiness = useLiveQuery(async () => getOfflineBusinessProfile(), []);
  const receiptSession = useLiveQuery(async () => {
    const sessionId = toTrimmedString(
      (order as any).sessionId ??
      (order as any).session_id ??
      (order as any).session
    );
    if (!sessionId) {
      return null;
    }
    return db.sessions.get(sessionId);
  }, [(order as any).sessionId, (order as any).session_id, (order as any).session]);

  const resolvedBusiness = business || offlineBusiness || undefined;
  const businessName = resolvedBusiness?.name?.trim() || 'Business Name';
  const businessNameDisplay = businessName.toUpperCase();
  const compactBusinessName = businessNameDisplay.replace(/\s+/g, ' ').trim();
  const businessNameLength = compactBusinessName.length;
  const businessAddress = resolvedBusiness?.address?.trim();
  const businessPhone = resolvedBusiness?.phone?.trim();
  const businessEmail = resolvedBusiness?.email?.trim();
  const sellerTin = toTrimmedString(
    (order as any).sellerTIN ??
    (order as any).sellerTin ??
    (order as any).seller_tin ??
    (resolvedBusiness as any)?.tin ??
    (resolvedBusiness as any)?.taxPin ??
    (resolvedBusiness as any)?.tax_pin
  );

  const orderNumberDisplay = toTrimmedString((order as any).orderNumber ?? (order as any).order_number) || '-';
  const orderDateRaw = toTrimmedString((order as any).createdAt ?? (order as any).created_at);
  const parsedOrderDate = orderDateRaw ? new Date(orderDateRaw) : new Date();
  const orderDate = Number.isNaN(parsedOrderDate.getTime()) ? new Date() : parsedOrderDate;
  const paymentMethodDisplay = toTrimmedString((order as any).paymentMethod ?? (order as any).payment_method);
  const normalizedPaymentMethod = paymentMethodDisplay.toLowerCase();
  const isCashPayment = normalizedPaymentMethod === 'cash' || normalizedPaymentMethod.includes('cash');
  const buyerName = resolveBuyerField(
    (order as any).customerName,
    (order as any).customer_name,
    (order as any).buyerName,
    (order as any).buyer_name
  );
  const buyerTin = resolveBuyerField(
    (order as any).customerTin,
    (order as any).customer_tin,
    (order as any).buyerTin,
    (order as any).buyer_tin
  );

  const fiscalInvoiceNumber = toTrimmedString(
    (order as any).fiscalInvoiceNumber ?? (order as any).fiscal_invoice_number
  );
  const eisUuid = toTrimmedString((order as any).eisUuid ?? (order as any).eis_uuid);
  const rawEisStatus = toTrimmedString((order as any).eisStatus ?? (order as any).eis_status);
  const eisStatus = rawEisStatus.toUpperCase() || 'PENDING';
  const digitalSignature = toTrimmedString(
    (order as any).digitalSignature ?? (order as any).digital_signature
  );
  const signaturePreview = formatSignaturePreview(digitalSignature);
  const branchIdDisplay = toTrimmedString(
    (order as any).branchId ??
    (order as any).branch_id ??
    (receiptSession as any)?.branchId ??
    (receiptSession as any)?.branch_id ??
    (receiptSession as any)?.branch
  );
  const cashierReceiptLabel = resolveCashierReceiptLabel(
    receiptSession?.userId,
    (receiptSession as any)?.user_id,
    (order as any)?.createdById,
    (order as any)?.created_by_id,
    (order as any)?.userId,
    (order as any)?.user_id
  );
  const pumpName = toTrimmedString(
    (order as any).pumpName ??
    (order as any).pump_name ??
    (receiptSession as any)?.pumpName ??
    (receiptSession as any)?.pump_name
  );
  const orderTypeRaw = toTrimmedString((order as any).orderType ?? (order as any).order_type).toLowerCase();
  const receiptType = ((): string => {
    if (order.status === 'Voided' || order.status === 'Cancelled') {
      return 'VOID';
    }
    if (orderTypeRaw.includes('return') || orderTypeRaw.includes('refund')) {
      return 'RETURN';
    }
    if (orderTypeRaw.includes('adjust')) {
      return 'ADJUSTMENT';
    }
    return 'SALE';
  })();
  const fiscalDayNumber = format(orderDate, 'yyyyMMdd');

  const normalizedOrderSubtotal = toFiniteNumber(order.subtotal ?? (order as any).subtotal, 0);
  const normalizedOrderTotal = toFiniteNumber(order.total ?? (order as any).total, normalizedOrderSubtotal);
  const normalizedOrderNet = toFiniteNumber(
    (order as any).netAmount ?? (order as any).net_amount,
    normalizedOrderSubtotal
  );
  const normalizedOrderTax = toFiniteNumber(
    (order as any).tax ??
    (order as any).vatAmount ??
    (order as any).vat_amount,
    0
  );
  const orderItems = Array.isArray((order as any).items) ? (order as any).items : [];
  const receiptOrderDiscount = Math.max(0, toFiniteNumber((order as any).discount_amount ?? (order as any).discountAmount, 0));
  const receiptItemDiscountTotal = orderItems.reduce(
    (sum, item) => sum + Math.max(0, toFiniteNumber((item as any).discount_amount ?? (item as any).discountAmount, 0)),
    0
  );
  const receiptDiscountTotal = Math.max(receiptOrderDiscount, receiptItemDiscountTotal);
  const normalizedFinalPayable = normalizedOrderTotal;
  const explicitChangeAmount = toOptionalFiniteNumber(
    (order as any).change ??
    (order as any).changeAmount ??
    (order as any).change_amount
  );
  const tenderedCashAmount = toOptionalFiniteNumber(
    (order as any).amountTendered ??
    (order as any).amount_tendered ??
    (order as any).amountReceived ??
    (order as any).amount_received ??
    (order as any).cashPaid ??
    (order as any).cash_paid
  );
  const computedChangeAmount =
    explicitChangeAmount !== null
      ? explicitChangeAmount
      : tenderedCashAmount !== null
      ? tenderedCashAmount - normalizedFinalPayable
      : 0;
  const receiptPaidAmount =
    isCashPayment && tenderedCashAmount !== null && tenderedCashAmount > 0
      ? tenderedCashAmount
      : 0;
  const receiptChangeAmount =
    isCashPayment && computedChangeAmount > 0.0001 ? computedChangeAmount : 0;
  const isOnAccountPayment = normalizedPaymentMethod.includes('account');
  const receiptAmountPaid =
    receiptPaidAmount > 0
      ? receiptPaidAmount
      : isOnAccountPayment
      ? 0
      : normalizedFinalPayable;
  const receiptChangeDisplay = receiptChangeAmount > 0 ? receiptChangeAmount : 0;

  const totalItemVat = orderItems.reduce(
    (acc, item) => acc + toFiniteNumber((item as any).itemTax ?? item.tax_amount ?? item.taxAmount, 0),
    0
  );
  const hasPerItemTax = totalItemVat > 0;

  const validationMetadata =
    (order as any).eisValidationMetadata ??
    (order as any).eis_validation_metadata ??
    {};
  const validationSources = [
    (order as any).eisValidationMetadata,
    (order as any).eis_validation_metadata,
    (order as any).mraSubmission,
    (order as any).mra_submission,
    (order as any).mraResponse,
    (order as any).mra_response,
  ];
  const validationPayload = resolveReceiptValidationPayload(
    (order as any).qrCodePayload,
    (order as any).qr_code_payload,
    ...validationSources
  );
  const hasSubmittedEisStatus = ['SUBMITTED', 'ACCEPTED'].includes(eisStatus);
  const hasRejectedEisStatus = eisStatus === 'REJECTED';
  const isFiscalizedReceipt = Boolean(
    fiscalInvoiceNumber ||
    eisUuid ||
    digitalSignature ||
    validationPayload.payload ||
    hasSubmittedEisStatus
  );
  const hasEisVerificationData = isFiscalizedReceipt || hasRejectedEisStatus;
  const qrPayload = validationPayload.payload;
  const effectiveShowHeader = showHeader || isFiscalizedReceipt;
  const effectiveShowQRCode = showQRCode || isFiscalizedReceipt;
  const effectiveShowItemDetails = showItemDetails || isFiscalizedReceipt;
  const effectiveShowTaxBreakdown = showTaxBreakdown || isFiscalizedReceipt;
  const effectiveShowFooter = showFooter;
  const missingFiscalText = hasSubmittedEisStatus ? 'N/A' : 'PENDING';
  const fiscalInvoiceNumberDisplay = fiscalInvoiceNumber || missingFiscalText;
  const validationUrlDisplay = qrPayload || missingFiscalText;
  const fiscalStatusDisplay = eisStatus || (isFiscalizedReceipt ? missingFiscalText : 'N/A');
  const shouldRenderQr = Boolean(effectiveShowQRCode && qrPayload);

  // Calculate tax breakdown by rate for MRA compliance
  const calculateTaxBreakdown = () => {
    const breakdown: Record<string, { 
      taxableValue: number; 
      taxRate: number; 
      vatAmount: number; 
      method: string;
      count: number;
    }> = {};
    
    orderItems.forEach((item) => {
      const taxMethod = (item.tax_calculation_method || item.taxCalculationMethod) === 'exclusive' ? 'exclusive' : 'inclusive';
      const normalizedTaxType = String(item.tax_type ?? item.taxType ?? '').trim().toLowerCase();
      const isZeroOrExempt =
        normalizedTaxType === 'zero' ||
        normalizedTaxType === 'zero_rated' ||
        normalizedTaxType === 'zero-rated' ||
        normalizedTaxType === 'vat_zero' ||
        normalizedTaxType === 'exempt' ||
        normalizedTaxType === 'vat_exempt';

      const itemQuantity = Math.max(1, toFiniteNumber(item.quantity, 1));
      const itemPrice = toFiniteNumber(item.price, 0);
      const computedSubtotal = itemPrice * itemQuantity;
      const itemSubtotal = toFiniteNumber(item.subtotal, computedSubtotal);
      const itemTaxAmount = toFiniteNumber(item.tax_amount ?? item.taxAmount, 0);
      const explicitTaxRate = toOptionalFiniteNumber(item.tax_rate ?? item.taxRate);

      let resolvedTaxRate = 0;
      if (isZeroOrExempt) {
        resolvedTaxRate = 0;
      } else if (explicitTaxRate !== null && explicitTaxRate > 0) {
        resolvedTaxRate = explicitTaxRate;
      } else if (itemTaxAmount > 0 && itemSubtotal > 0) {
        // Backfill rate from amounts when rate snapshot is missing on the item.
        resolvedTaxRate = (itemTaxAmount / itemSubtotal) * 100;
      } else if (explicitTaxRate !== null && explicitTaxRate >= 0) {
        resolvedTaxRate = explicitTaxRate;
      }

      const normalizedTaxRate = Number.isFinite(resolvedTaxRate)
        ? Number(resolvedTaxRate.toFixed(2))
        : 0;
      const rateKey = `${normalizedTaxRate}-${taxMethod}`;
      
      if (!breakdown[rateKey]) {
        breakdown[rateKey] = { 
          taxableValue: 0, 
          taxRate: normalizedTaxRate,
          vatAmount: 0, 
          method: taxMethod,
          count: 0
        };
      }
      breakdown[rateKey].taxableValue += itemSubtotal;
      breakdown[rateKey].vatAmount += itemTaxAmount;
      breakdown[rateKey].count += 1;
    });
    
    const entries = Object.entries(breakdown);
    if (entries.length === 0 && (normalizedOrderNet > 0 || normalizedOrderTax > 0 || normalizedOrderTotal > 0)) {
      const orderTaxRate = toOptionalFiniteNumber(
        (order as any).taxRateValue ??
        (order as any).tax_rate_value ??
        (order as any).taxRate ??
        (order as any).tax_rate
      );
      const inferredTaxRate =
        orderTaxRate !== null
          ? orderTaxRate
          : normalizedOrderTax > 0 && normalizedOrderNet > 0
          ? Number(((normalizedOrderTax / normalizedOrderNet) * 100).toFixed(2))
          : 0;
      return [{
        rate: inferredTaxRate,
        method: String(((order as any).taxCalculationMethod ?? (order as any).tax_calculation_method) || 'inclusive') === 'exclusive'
          ? 'exclusive'
          : 'inclusive',
        taxableValue: normalizedOrderNet || Math.max(0, normalizedOrderTotal - normalizedOrderTax),
        vatAmount: normalizedOrderTax,
        count: orderItems.length || 1,
      }];
    }

    return entries
      .sort(([keyA], [keyB]) => {
        const rateA = parseFloat(keyA.split('-')[0]);
        const rateB = parseFloat(keyB.split('-')[0]);
        return rateB - rateA;
      })
      .map(([, data]) => ({
        rate: data.taxRate,
        method: data.method,
        taxableValue: data.taxableValue,
        vatAmount: data.vatAmount,
        count: data.count
      }));
  };

  const taxBreakdown = calculateTaxBreakdown();
  const receiptVatTotal = hasPerItemTax ? totalItemVat : normalizedOrderTax;
  const resolvedPaperWidth = normalizePrinterPaperWidth(paperWidth);
  const receiptLayout: Record<PrinterPaperWidth, {
    containerWidthClass: string;
    contentPaddingClass: string;
    bodyTextClass: string;
    metaTextClass: string;
    businessNameTextClass: string;
    longBusinessNameTextClass: string;
    payableTextClass: string;
    inlineValueMaxWidthClass: string;
    fontSizePx: number;
    qrSize: string;
    qrMinHeight: string;
    lineWidth: number;
    compactTextMax: number;
  }> = {
    '30mm': {
      containerWidthClass: 'w-[112px]',
      contentPaddingClass: 'px-1 py-2',
      bodyTextClass: 'text-[7px]',
      metaTextClass: 'text-[6px]',
      businessNameTextClass: 'text-[7px]',
      longBusinessNameTextClass: 'text-[7px] tracking-normal',
      payableTextClass: 'text-[9px]',
      inlineValueMaxWidthClass: 'min-w-0 max-w-[52px]',
      fontSizePx: 6,
      qrSize: '18mm',
      qrMinHeight: '20mm',
      lineWidth: 16,
      compactTextMax: 12,
    },
    '40mm': {
      containerWidthClass: 'w-[150px]',
      contentPaddingClass: 'px-1.5 py-2',
      bodyTextClass: 'text-[8px]',
      metaTextClass: 'text-[7px]',
      businessNameTextClass: 'text-[8px]',
      longBusinessNameTextClass: 'text-[8px] tracking-normal',
      payableTextClass: 'text-[10px]',
      inlineValueMaxWidthClass: 'min-w-0 max-w-[72px]',
      fontSizePx: 7,
      qrSize: '20mm',
      qrMinHeight: '22mm',
      lineWidth: 21,
      compactTextMax: 16,
    },
    '50mm': {
      containerWidthClass: 'w-[188px]',
      contentPaddingClass: 'px-2 py-2',
      bodyTextClass: 'text-[8px]',
      metaTextClass: 'text-[7px]',
      businessNameTextClass: 'text-[8px]',
      longBusinessNameTextClass: 'text-[8px] tracking-normal',
      payableTextClass: 'text-[10px]',
      inlineValueMaxWidthClass: 'min-w-0 max-w-[92px]',
      fontSizePx: 8,
      qrSize: '22mm',
      qrMinHeight: '24mm',
      lineWidth: 25,
      compactTextMax: 20,
    },
    '58mm': {
      containerWidthClass: 'w-[218px]',
      contentPaddingClass: 'px-2 py-2',
      bodyTextClass: 'text-[9.5px]',
      metaTextClass: 'text-[8.5px]',
      businessNameTextClass: 'text-[9.5px]',
      longBusinessNameTextClass: 'text-[9.5px] tracking-normal',
      payableTextClass: 'text-[13px]',
      inlineValueMaxWidthClass: 'min-w-0 max-w-[108px]',
      fontSizePx: 9.5,
      qrSize: '24mm',
      qrMinHeight: '26mm',
      lineWidth: 32,
      compactTextMax: 14,
    },
    '80mm': {
      containerWidthClass: 'w-[300px]',
      contentPaddingClass: 'px-3 py-2',
      bodyTextClass: 'text-[10px]',
      metaTextClass: 'text-[9px]',
      businessNameTextClass: 'text-[10px]',
      longBusinessNameTextClass: 'text-[10px] tracking-normal',
      payableTextClass: 'text-sm',
      inlineValueMaxWidthClass: 'min-w-0 max-w-[170px]',
      fontSizePx: 10,
      qrSize: '28mm',
      qrMinHeight: '30mm',
      lineWidth: 42,
      compactTextMax: 30,
    },
  };
  const layout = receiptLayout[resolvedPaperWidth];
  const containerWidthClass = layout.containerWidthClass;
  const contentPaddingClass = layout.contentPaddingClass;
  const bodyTextClass = layout.bodyTextClass;
  const metaTextClass = layout.metaTextClass;
  const businessNameTextClass = layout.businessNameTextClass;
  const businessNameWidthClass = businessNameLength > 30 ? layout.longBusinessNameTextClass : '';
  const payableTextClass = layout.payableTextClass;
  const inlineValueMaxWidthClass = layout.inlineValueMaxWidthClass;
  const qrSizeStyle = {
    width: layout.qrSize,
    height: layout.qrSize,
  };
  const qrContainerStyle = {
    minHeight: layout.qrMinHeight,
  };
  const printContentWidth = resolvedPaperWidth;
  // Keep divider width aligned with native ESC/POS formatter widths
  // to prevent hard-wrap in printed output.
  const receiptLineWidth = layout.lineWidth;
  const sectionDotRule = '-'.repeat(receiptLineWidth);
  const sectionSpacingClass = 'mt-3 mb-3';
  const makeSectionBanner = (title: string): string => {
    return title.trim().toUpperCase();
  };
  const renderDotRuleLine = () => (
    <p
      className={`block w-full overflow-hidden whitespace-nowrap text-center ${metaTextClass} leading-none`}
      style={{ overflowWrap: 'normal', wordBreak: 'normal' }}
    >
      {sectionDotRule}
    </p>
  );
  const renderSectionDivider = () => (
    <div className="mb-1">
      {renderDotRuleLine()}
    </div>
  );
  const renderSectionTitleBlock = (title: string) => (
    <div className="mb-1 space-y-0.5">
      {renderDotRuleLine()}
      <p className={`text-center ${metaTextClass} font-semibold tracking-wide`}>{makeSectionBanner(title)}</p>
      {renderDotRuleLine()}
    </div>
  );

  const isCopyReceipt = copyNumber > 1;
  const receiptTypeLabel = `COPY${copyNumber > 2 ? ` #${copyNumber}` : ''}`;

  const formatReceiptAmount = (value: unknown): string => {
    return toFiniteNumber(value, 0).toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  };
  const formatReceiptQuantity = (value: unknown): string => {
    const parsed = toFiniteNumber(value, 0);
    if (Math.abs(parsed - Math.round(parsed)) < 0.0001) {
      return String(Math.round(parsed));
    }
    return parsed.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
  };
  const compactReceiptText = (value: unknown, maxLength = layout.compactTextMax): string => {
    const raw = toTrimmedString(value).replace(/\s+/g, ' ');
    if (!raw) return 'ITEM';
    if (raw.length <= maxLength) return raw.toUpperCase();
    return `${raw.slice(0, Math.max(0, maxLength - 3)).trimEnd().toUpperCase()}...`;
  };
  const resolveTaxCode = (rate: unknown, taxType?: unknown): string => {
    const normalizedRate = toFiniteNumber(rate, 0);
    const normalizedType = toTrimmedString(taxType).toLowerCase();
    if (normalizedType.includes('exempt')) return 'E';
    if (normalizedRate <= 0 || normalizedType.includes('zero') || normalizedType.includes('non')) return 'B';
    return 'A';
  };
  const formatReceiptRate = (value: unknown): string => {
    const formatted = toFiniteNumber(value, 0).toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
    return formatted || '0';
  };
  const receiptNumberDisplay = fiscalInvoiceNumber || orderNumberDisplay;
  const localReceiptSequence = orderNumberDisplay !== '-'
    ? orderNumberDisplay
    : toTrimmedString((order as any).id) || '-';
  const posReferenceDisplay = `${format(orderDate, 'yyyyMMdd-HHmmss')}-${localReceiptSequence}`;
  const sellerAddressLines = (businessAddress || '')
    .split(/\n|,/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 3);
  const taxpayerConfiguration =
    validationMetadata && typeof validationMetadata === 'object'
      ? ((validationMetadata as any).taxpayerConfiguration ??
          (validationMetadata as any).taxpayer_configuration ??
          (validationMetadata as any).taxpayer)
      : null;
  const explicitVatRegistered =
    (taxpayerConfiguration && typeof taxpayerConfiguration === 'object'
      ? ((taxpayerConfiguration as any).isVATRegistered ??
          (taxpayerConfiguration as any).is_vat_registered ??
          (taxpayerConfiguration as any).vatRegistered)
      : undefined) ??
    (order as any).sellerVatRegistered ??
    (order as any).seller_vat_registered ??
    (resolvedBusiness as any)?.vatRegistered ??
    (resolvedBusiness as any)?.vat_registered;
  const hasExplicitVatRegistration = explicitVatRegistered !== undefined && explicitVatRegistered !== null && explicitVatRegistered !== '';
  const isSellerVatRegistered = hasExplicitVatRegistration
    ? explicitVatRegistered === true || String(explicitVatRegistered).toLowerCase() === 'true'
    : false;
  const vatRegistrationLabel = toTrimmedString(
    (order as any).sellerVatStatus ??
    (order as any).seller_vat_status ??
    (order as any).vatStatus ??
    (order as any).vat_status
  ) || (isSellerVatRegistered ? '*VAT REGISTERED*' : '*NON VAT REGISTERED*');
  const taxOfficeLabel = toTrimmedString(
    (order as any).taxOffice ??
    (order as any).tax_office ??
    (order as any).mraTaxOffice ??
    (order as any).mra_tax_office
  );
  const legalReceiptTitle = isFiscalizedReceipt ? '*** START OF LEGAL RECEIPT ***' : '*** START OF RECEIPT ***';
  const legalReceiptEndTitle = isFiscalizedReceipt ? '*** END OF LEGAL RECEIPT ***' : '*** END OF RECEIPT ***';
  const legalTaxBreakdown = taxBreakdown.map((tax) => {
    const code = resolveTaxCode(tax.rate);
    return {
      code,
      rate: toFiniteNumber(tax.rate, 0),
      taxableValue: toFiniteNumber(tax.taxableValue, 0),
      vatAmount: toFiniteNumber(tax.vatAmount, 0),
    };
  });
  const normalizeLevyBreakdown = (...sources: unknown[]) => {
    const rows: Array<{ levyTypeId: string; levyRate: number; levyAmount: number }> = [];
    const seen = new Set<string>();
    const levyKeys = ['levyBreakDown', 'levyBreakdown', 'levy_breakdown'];

    const appendRows = (items: unknown[]) => {
      for (const item of items) {
        if (!item || typeof item !== 'object' || Array.isArray(item)) {
          continue;
        }

        const row = item as Record<string, unknown>;
        const levyTypeId = toTrimmedString(
          row.levyTypeId ??
          row.levy_type_id ??
          row.levyId ??
          row.levy_id ??
          row.typeId ??
          row.type_id
        ) || 'LEVY';
        const levyRate = toFiniteNumber(row.levyRate ?? row.levy_rate ?? row.rate ?? row.percentage, 0);
        const levyAmount = toFiniteNumber(row.levyAmount ?? row.levy_amount ?? row.amount, 0);
        if (levyAmount <= 0) {
          continue;
        }

        const dedupeKey = `${levyTypeId.toUpperCase()}|${levyRate.toFixed(4)}|${levyAmount.toFixed(4)}`;
        if (seen.has(dedupeKey)) {
          continue;
        }
        seen.add(dedupeKey);
        rows.push({ levyTypeId, levyRate, levyAmount });
      }
    };

    for (const source of sources) {
      if (!source) {
        continue;
      }

      if (Array.isArray(source)) {
        appendRows(source);
        continue;
      }

      if (typeof source === 'object') {
        const record = source as Record<string, unknown>;
        for (const key of levyKeys) {
          const directRows = record[key];
          if (Array.isArray(directRows)) {
            appendRows(directRows);
          }
        }
      }

      for (const nestedRows of findNestedArrays(source, levyKeys)) {
        appendRows(nestedRows);
      }
    }

    return rows;
  };
  const legalLevyBreakdown = normalizeLevyBreakdown(
    (order as any).levyBreakDown,
    (order as any).levyBreakdown,
    (order as any).levy_breakdown,
    ...validationSources
  );
  const tenderedAmount = receiptAmountPaid > 0 ? receiptAmountPaid : normalizedFinalPayable;
  const legalRule = '-'.repeat(Math.max(16, receiptLineWidth - 2));
  const thermalLine = (text = '') => text.replace(/\s+/g, ' ').trim();
  const centerThermal = (text: string) => {
    const value = thermalLine(text);
    if (value.length >= receiptLineWidth) return value;
    return `${' '.repeat(Math.floor((receiptLineWidth - value.length) / 2))}${value}`;
  };
  const alignThermal = (left: string, right: string) => {
    const cleanLeft = thermalLine(left);
    const cleanRight = thermalLine(right);
    if (!cleanLeft) return cleanRight;
    if (!cleanRight) return cleanLeft;
    if (cleanLeft.length + cleanRight.length + 1 >= receiptLineWidth) {
      const rightLine =
        cleanRight.length < receiptLineWidth
          ? `${' '.repeat(receiptLineWidth - cleanRight.length)}${cleanRight}`
          : cleanRight;
      return `${cleanLeft}\n${rightLine}`;
    }
    return `${cleanLeft}${' '.repeat(receiptLineWidth - cleanLeft.length - cleanRight.length)}${cleanRight}`;
  };
  const thermalAmount = (value: unknown) => formatReceiptAmount(value);
  const thermalTextLines: string[] = [];
  if (effectiveShowHeader) {
    thermalTextLines.push(
      centerThermal(legalReceiptTitle),
      centerThermal(businessNameDisplay)
    );
    if (sellerAddressLines.length > 0) {
      sellerAddressLines.forEach((line) => thermalTextLines.push(centerThermal(line.toUpperCase())));
    } else {
      thermalTextLines.push(centerThermal('ADDRESS: N/A'));
    }
    thermalTextLines.push(
      centerThermal(`CELL: ${businessPhone || 'N/A'}`),
      centerThermal(`EMAIL: ${businessEmail || 'N/A'}`),
      centerThermal(`TIN: ${sellerTin || 'N/A'}`),
      centerThermal(vatRegistrationLabel.toUpperCase())
    );
    if (isCopyReceipt) thermalTextLines.push(centerThermal(receiptTypeLabel));
    if (taxOfficeLabel) thermalTextLines.push(centerThermal(taxOfficeLabel.toUpperCase()));
    if (pumpName) thermalTextLines.push(centerThermal(`PUMP: ${pumpName.toUpperCase()}`));
    thermalTextLines.push('');
  }
  thermalTextLines.push(
    alignThermal('Buyers Name:', buyerName),
    alignThermal('Buyers Tin:', buyerTin),
    alignThermal('Receipt Number:', receiptNumberDisplay),
    alignThermal('POS Ref:', posReferenceDisplay),
    legalRule
  );
  if (effectiveShowItemDetails) {
    orderItems.forEach((item) => {
      const itemPrice = toFiniteNumber(item.price, 0);
      const itemQuantity = Math.max(1, toFiniteNumber(item.quantity, 1));
      const itemTotal = toFiniteNumber(item.total, itemPrice * itemQuantity);
      const itemSubtotal = toFiniteNumber(item.subtotal, Math.max(0, itemTotal - toFiniteNumber(item.tax_amount ?? item.taxAmount, 0)));
      const itemVat = toFiniteNumber(item.tax_amount ?? item.taxAmount, Math.max(0, itemTotal - itemSubtotal));
      const itemTaxRate = toFiniteNumber(item.tax_rate ?? item.taxRate, itemVat > 0 && itemSubtotal > 0 ? (itemVat / itemSubtotal) * 100 : 0);
      const itemTaxCode = resolveTaxCode(itemTaxRate, item.tax_type ?? item.taxType);
      const itemDiscount = Math.max(0, toFiniteNumber(item.discount_amount ?? item.discountAmount, 0));
      const itemDiscountName = String(item.discount_name ?? item.discountName ?? 'Discount').trim() || 'Discount';
      thermalTextLines.push(
        alignThermal(`${formatReceiptQuantity(itemQuantity)} X ${thermalAmount(itemPrice)}`, `${thermalAmount(itemTotal)} ${itemTaxCode}`),
        compactReceiptText(item.name, 14)
      );
      if (itemDiscount > 0) {
        thermalTextLines.push(alignThermal(compactReceiptText(itemDiscountName, Math.max(8, receiptLineWidth - 14)), `-${thermalAmount(itemDiscount)}`));
      }
    });
    thermalTextLines.push(legalRule);
  }
  if (effectiveShowTaxBreakdown && (legalTaxBreakdown.length > 0 || legalLevyBreakdown.length > 0)) {
    legalTaxBreakdown.forEach((tax) => {
      const rateLabel = `${tax.code}-${formatReceiptRate(tax.rate)}%`;
      thermalTextLines.push(
        alignThermal(`TAXABLE ${rateLabel}`, thermalAmount(tax.taxableValue)),
        alignThermal(`VAT ${rateLabel}`, thermalAmount(tax.vatAmount))
      );
    });
    thermalTextLines.push(alignThermal('TOTAL VAT:', thermalAmount(receiptVatTotal)));
    legalLevyBreakdown.forEach((levy) => {
      thermalTextLines.push(alignThermal(`LEVY ${levy.levyTypeId}-${formatReceiptRate(levy.levyRate)}%`, thermalAmount(levy.levyAmount)));
    });
    thermalTextLines.push(legalRule);
  }
  if (receiptDiscountTotal > 0) {
    thermalTextLines.push(alignThermal('TOTAL DISCOUNT:', thermalAmount(receiptDiscountTotal)));
  }
  thermalTextLines.push(
    alignThermal('TOTAL:', thermalAmount(normalizedFinalPayable)),
    alignThermal('Amount Tendered:', thermalAmount(tenderedAmount)),
    alignThermal('Change:', thermalAmount(receiptChangeDisplay))
  );
  if (paymentMethodDisplay) {
    thermalTextLines.push(alignThermal('Payment:', paymentMethodDisplay));
  }
  thermalTextLines.push(
    '',
    centerThermal(`DATE: ${format(orderDate, 'yyyy-MM-dd')} TIME: ${format(orderDate, 'HH:mm:ss')}`)
  );
  if (hasEisVerificationData) {
    thermalTextLines.push(centerThermal('Scan Here For Receipt Details'));
  }
  if (!shouldRenderQr && hasEisVerificationData) {
    thermalTextLines.push(centerThermal('QR PENDING'));
  }
  if (effectiveShowFooter) {
    thermalTextLines.push('', centerThermal(legalReceiptEndTitle), legalRule, centerThermal('THANK YOU!'));
  }
  const thermalReceiptText = thermalTextLines.join('\n').replace(/\n{3,}/g, '\n\n');
  const receiptRootClass = `${containerWidthClass} ${contentPaddingClass} bg-white text-black font-mono ${bodyTextClass} leading-tight`;
  const receiptRootStyle: React.CSSProperties = {
    fontSize: `${layout.fontSizePx}px`,
  };

  return (
    <div
      id={elementId}
      className={receiptRootClass}
      style={receiptRootStyle}
      data-eis-qr-payload={qrPayload || undefined}
      data-eis-validation-mode={validationPayload.mode}
      data-thermal-receipt-text={encodeURIComponent(thermalReceiptText)}
    >
      <style jsx global>{`
        #${elementId},
        #${elementId} * {
          overflow-wrap: anywhere;
          word-break: break-word;
          letter-spacing: 0;
        }
        #${elementId} .receipt-legal-marker,
        #${elementId} .receipt-vat-status {
          display: block;
          width: 100%;
          text-align: center !important;
          font-weight: 700 !important;
        }
        #${elementId} .receipt-total-row,
        #${elementId} .receipt-total-row * {
          font-weight: 700 !important;
        }
        #${elementId} .receipt-qr-container {
          display: flex !important;
          width: 100% !important;
          justify-content: center !important;
          align-items: center !important;
          text-align: center !important;
        }
        #${elementId} .receipt-qr-code {
          display: block !important;
          margin-left: auto !important;
          margin-right: auto !important;
        }

        ${enablePrintStyles ? `
        @media print {
          body * {
            visibility: hidden;
          }
          #${elementId}, #${elementId} * {
            visibility: visible;
          }
          #${elementId} {
            position: absolute;
            left: 50%;
            top: 0;
            transform: translateX(-50%);
            width: ${printContentWidth};
            margin: 0;
            padding: 0;
          }
          @page {
            margin: 0;
            size: ${resolvedPaperWidth} auto;
          }
        }
        ` : ''}
      `}</style>

      {effectiveShowHeader && (
        <div className="mt-1 text-center">
         
         
          <p className="receipt-legal-marker mt-2 whitespace-nowrap text-center font-bold leading-tight">{legalReceiptTitle}</p>
          <p className={`${businessNameTextClass} ${businessNameWidthClass} font-bold leading-tight`}>{businessNameDisplay}</p>
          {sellerAddressLines.length > 0 ? (
            sellerAddressLines.map((line, index) => (
              <p key={`${line}-${index}`} className={`${metaTextClass} leading-tight`}>
                {line.toUpperCase()}
              </p>
            ))
          ) : (
            <p className={`${metaTextClass} leading-tight`}>ADDRESS: N/A</p>
          )}
          <p className={`${metaTextClass} leading-tight`}>CELL: {businessPhone || 'N/A'}</p>
          <p className={`${metaTextClass} leading-tight`}>EMAIL: {businessEmail || 'N/A'}</p>
          <p className={`${bodyTextClass} leading-tight`}>TIN: {sellerTin || 'N/A'}</p>
          <p className={`receipt-vat-status ${bodyTextClass} font-bold leading-tight`}>{vatRegistrationLabel.toUpperCase()}</p>
          {isCopyReceipt && (
            <p className={`${bodyTextClass} text-center font-bold leading-tight`}>
              {receiptTypeLabel}
            </p>
          )}
          {taxOfficeLabel && <p className={`${metaTextClass} leading-tight`}>{taxOfficeLabel.toUpperCase()}</p>}
          {pumpName && <p className={`${metaTextClass} leading-tight`}>PUMP: {pumpName.toUpperCase()}</p>}
        </div>
      )}

      <div className={`mt-5 space-y-0.5 ${bodyTextClass}`}>
        <div className="grid grid-cols-[auto_1fr] gap-x-2">
          <span>Buyers Name:</span>
          <span className="text-right break-words">{buyerName || 'Walk-in Customer'}</span>
        </div>
        <div className="grid grid-cols-[auto_1fr] gap-x-2">
          <span>Buyers Tin:</span>
          <span className="text-right break-all">{buyerTin || 'N/A'}</span>
        </div>
        <div className="grid grid-cols-[auto_1fr] gap-x-2">
          <span>Receipt Number:</span>
          <span className="text-right break-all font-semibold">{receiptNumberDisplay}</span>
        </div>
        <div className="grid grid-cols-[auto_1fr] gap-x-2">
          <span>POS Ref:</span>
          <span className="text-right break-all font-semibold">{posReferenceDisplay}</span>
        </div>
        {!isFiscalizedReceipt && (
          <div className="grid grid-cols-[auto_1fr] gap-x-2">
            <span>Receipt Status:</span>
            <span className="text-right font-semibold">{fiscalStatusDisplay}</span>
          </div>
        )}
      </div>

      {effectiveShowItemDetails && (
        <div className={`mt-2 ${bodyTextClass}`}>
          <p className="whitespace-nowrap text-center leading-none">{legalRule}</p>
          {orderItems.map((item, index) => {
            const itemPrice = toFiniteNumber(item.price, 0);
            const itemQuantity = Math.max(1, toFiniteNumber(item.quantity, 1));
            const itemTotal = toFiniteNumber(item.total, itemPrice * itemQuantity);
            const itemSubtotal = toFiniteNumber(item.subtotal, Math.max(0, itemTotal - toFiniteNumber(item.tax_amount ?? item.taxAmount, 0)));
            const itemVat = toFiniteNumber(item.tax_amount ?? item.taxAmount, Math.max(0, itemTotal - itemSubtotal));
            const itemTaxRate = toFiniteNumber(item.tax_rate ?? item.taxRate, itemVat > 0 && itemSubtotal > 0 ? (itemVat / itemSubtotal) * 100 : 0);
            const itemTaxCode = resolveTaxCode(itemTaxRate, item.tax_type ?? item.taxType);
            const itemDiscount = Math.max(0, toFiniteNumber(item.discount_amount ?? item.discountAmount, 0));
            const itemDiscountName = String(item.discount_name ?? item.discountName ?? 'Discount').trim() || 'Discount';

            return (
              <div key={`${item.id}-${index}`} className="mb-1">
                <div className="flex items-start justify-between gap-2">
                  <span className="whitespace-nowrap">{formatReceiptQuantity(itemQuantity)} X {formatReceiptAmount(itemPrice)}</span>
                  <span className="whitespace-nowrap text-right font-semibold">{formatReceiptAmount(itemTotal)} {itemTaxCode}</span>
                </div>
                <p className="leading-tight">{compactReceiptText(item.name)}</p>
                {itemDiscount > 0 && (
                  <div className="flex items-start justify-between gap-2 text-[0.9em]">
                    <span className="truncate">{compactReceiptText(itemDiscountName).toUpperCase()}</span>
                    <span className="whitespace-nowrap text-right">-{formatReceiptAmount(itemDiscount)}</span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {effectiveShowTaxBreakdown && (legalTaxBreakdown.length > 0 || legalLevyBreakdown.length > 0) && (
        <div className={`mt-2 ${bodyTextClass}`}>
          <p className="whitespace-nowrap text-center leading-none">{legalRule}</p>
          {legalTaxBreakdown.map((tax, index) => {
            const rateText = formatReceiptRate(tax.rate);
            const rateLabel = `${tax.code}-${rateText}%`;
            return (
              <React.Fragment key={`${rateLabel}-${index}`}>
                <div className="flex justify-between gap-2">
                  <span>TAXABLE {rateLabel}</span>
                  <span>{formatReceiptAmount(tax.taxableValue)}</span>
                </div>
                <div className="flex justify-between gap-2">
                  <span>VAT {rateLabel}</span>
                  <span>{formatReceiptAmount(tax.vatAmount)}</span>
                </div>
              </React.Fragment>
            );
          })}
          <div className="flex justify-between gap-2 font-semibold">
            <span>TOTAL VAT:</span>
            <span>{formatReceiptAmount(receiptVatTotal)}</span>
          </div>
          {legalLevyBreakdown.map((levy, index) => (
            <div key={`${levy.levyTypeId}-${levy.levyRate}-${index}`} className="flex justify-between gap-2">
              <span>LEVY {levy.levyTypeId}-{formatReceiptRate(levy.levyRate)}%</span>
              <span>{formatReceiptAmount(levy.levyAmount)}</span>
            </div>
          ))}
        </div>
      )}

      <div className={`mt-2 ${bodyTextClass}`}>
        <p className="whitespace-nowrap text-center leading-none">{legalRule}</p>
        {receiptDiscountTotal > 0 && (
          <div className="flex justify-between gap-2">
            <span>TOTAL DISCOUNT:</span>
            <span>{formatReceiptAmount(receiptDiscountTotal)}</span>
          </div>
        )}
        <div className="receipt-total-row flex justify-between gap-2 font-bold">
          <span>TOTAL:</span>
          <span>{formatReceiptAmount(normalizedFinalPayable)}</span>
        </div>
        <div className="flex justify-between gap-2">
          <span>Amount Tendered:</span>
          <span>{formatReceiptAmount(tenderedAmount)}</span>
        </div>
        <div className="flex justify-between gap-2">
          <span>Change:</span>
          <span>{formatReceiptAmount(receiptChangeDisplay)}</span>
        </div>
        {paymentMethodDisplay && (
          <div className="flex justify-between gap-2">
            <span>Payment:</span>
            <span>{paymentMethodDisplay}</span>
          </div>
        )}
      </div>

      <div className={`mt-5 text-center ${bodyTextClass}`}>
        <p>DATE: {format(orderDate, 'yyyy-MM-dd')} TIME: {format(orderDate, 'HH:mm:ss')}</p>
        {hasEisVerificationData && <p>Scan Here For Receipt Details</p>}
        {shouldRenderQr ? (
          <div className="receipt-qr-container flex flex-col items-center justify-center pt-2" style={qrContainerStyle}>
            <div className="receipt-qr-code bg-white p-1" style={qrSizeStyle} aria-label="Receipt validation QR code">
              <QRCode
                value={qrPayload}
                size={256}
                level="M"
                style={{ height: '100%', width: '100%' }}
              />
            </div>
          </div>
        ) : hasEisVerificationData ? (
          <p className={`${metaTextClass} mt-2 font-semibold`}>QR PENDING</p>
        ) : null}
      </div>

      {effectiveShowFooter && (
        <div className={`mt-5 text-center ${bodyTextClass}`}>
          <p className="receipt-legal-marker whitespace-nowrap text-center font-bold">{legalReceiptEndTitle}</p>
          <p className="mt-2">THANK YOU!</p>
        </div>
      )}
    </div>

  );
};
