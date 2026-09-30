
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
    contentPadding: string;
    fontSizePx: number;
    bodyFontSizePx: number;
    metaFontSizePx: number;
    businessNameFontSizePx: number;
    lineHeight: number;
    sectionGap: string;
    rowGap: string;
    itemGap: string;
    ruleGap: string;
    qrSize: string;
    qrMinHeight: string;
    qrPadding: string;
    lineWidth: number;
    compactTextMax: number;
    compactLabels: boolean;
    labelColumnPercent: number;
  }> = {
    '30mm': {
      contentPadding: '2mm 1.5mm',
      fontSizePx: 5.5,
      bodyFontSizePx: 6.5,
      metaFontSizePx: 5.5,
      businessNameFontSizePx: 6.5,
      lineHeight: 1.3,
      sectionGap: '2.5mm',
      rowGap: '0.7mm',
      itemGap: '1.6mm',
      ruleGap: '1.4mm',
      qrSize: '14mm',
      qrMinHeight: '14mm',
      qrPadding: '0.25mm 0.5mm',
      lineWidth: 16,
      compactTextMax: 12,
      compactLabels: true,
      labelColumnPercent: 34,
    },
    '40mm': {
      contentPadding: '2mm 1.5mm',
      fontSizePx: 6.5,
      bodyFontSizePx: 7.5,
      metaFontSizePx: 6.5,
      businessNameFontSizePx: 7.5,
      lineHeight: 1.3,
      sectionGap: '2.5mm',
      rowGap: '0.7mm',
      itemGap: '1.7mm',
      ruleGap: '1.4mm',
      qrSize: '16mm',
      qrMinHeight: '16mm',
      qrPadding: '0.25mm 0.5mm',
      lineWidth: 21,
      compactTextMax: 16,
      compactLabels: true,
      labelColumnPercent: 36,
    },
    '50mm': {
      contentPadding: '2.5mm 2mm',
      fontSizePx: 7.5,
      bodyFontSizePx: 7.5,
      metaFontSizePx: 6.5,
      businessNameFontSizePx: 7.5,
      lineHeight: 1.3,
      sectionGap: '2.75mm',
      rowGap: '0.75mm',
      itemGap: '1.8mm',
      ruleGap: '1.5mm',
      qrSize: '18mm',
      qrMinHeight: '18mm',
      qrPadding: '0.25mm 0.5mm',
      lineWidth: 25,
      compactTextMax: 20,
      compactLabels: true,
      labelColumnPercent: 38,
    },
    '58mm': {
      contentPadding: '2.5mm 2.25mm',
      fontSizePx: 8.5,
      bodyFontSizePx: 8.5,
      metaFontSizePx: 7.5,
      businessNameFontSizePx: 8.5,
      lineHeight: 1.28,
      sectionGap: '2.5mm',
      rowGap: '0.65mm',
      itemGap: '1.7mm',
      ruleGap: '1.3mm',
      qrSize: '18mm',
      qrMinHeight: '18mm',
      qrPadding: '0.25mm 0.5mm',
      lineWidth: 32,
      compactTextMax: 16,
      compactLabels: true,
      labelColumnPercent: 38,
    },
    '80mm': {
      contentPadding: '3mm 3mm',
      fontSizePx: 10,
      bodyFontSizePx: 10,
      metaFontSizePx: 9,
      businessNameFontSizePx: 10,
      lineHeight: 1.38,
      sectionGap: '3.5mm',
      rowGap: '0.9mm',
      itemGap: '2.2mm',
      ruleGap: '1.7mm',
      qrSize: '24mm',
      qrMinHeight: '24mm',
      qrPadding: '1mm',
      lineWidth: 42,
      compactTextMax: 30,
      compactLabels: false,
      labelColumnPercent: 46,
    },
  };
  const layout = receiptLayout[resolvedPaperWidth];
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
  // Narrow rolls need more room for values than long descriptive labels.
  // These abbreviations are intentionally limited to the compact profiles;
  // the 80mm layout keeps the full labels for maximum clarity.
  const receiptLabels = layout.compactLabels
    ? {
        buyer: 'Buyer:',
        buyerTin: 'TIN:',
        receiptNumber: 'Receipt #:',
        posReference: 'Ref:',
        receiptStatus: 'Status:',
        amountTendered: 'Tendered:',
        payment: 'Pay:',
      }
    : {
        buyer: 'Buyers Name:',
        buyerTin: 'Buyers Tin:',
        receiptNumber: 'Receipt Number:',
        posReference: 'POS Ref:',
        receiptStatus: 'Receipt Status:',
        amountTendered: 'Amount Tendered:',
        payment: 'Payment:',
      };

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
  // Keep tax-system legal markers on fiscalized receipts. Non-fiscal receipts
  // omit the decorative markers to avoid adding unnecessary roll length.
  const showReceiptMarkers = isFiscalizedReceipt;
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
    if (!value) return '';

    const chunks: string[] = [];
    let current = '';
    const pushCurrent = () => {
      if (current) {
        chunks.push(current);
        current = '';
      }
    };

    for (const word of value.split(' ')) {
      if (!word) continue;

      if (word.length > receiptLineWidth) {
        pushCurrent();
        for (let offset = 0; offset < word.length; offset += receiptLineWidth) {
          chunks.push(word.slice(offset, offset + receiptLineWidth));
        }
        continue;
      }

      const candidate = current ? `${current} ${word}` : word;
      if (candidate.length > receiptLineWidth) {
        pushCurrent();
      }
      current = current ? `${current} ${word}` : word;
    }
    pushCurrent();

    return chunks
      .map((line) => line.length >= receiptLineWidth
        ? line
        : `${' '.repeat(Math.floor((receiptLineWidth - line.length) / 2))}${line}`)
      .join('\n');
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
    if (showReceiptMarkers) {
      thermalTextLines.push(centerThermal(legalReceiptTitle));
    }
    thermalTextLines.push(centerThermal(businessNameDisplay));
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
    alignThermal(receiptLabels.buyer, buyerName || 'Walk-in Customer'),
    alignThermal(receiptLabels.buyerTin, buyerTin || 'N/A'),
    alignThermal(receiptLabels.receiptNumber, receiptNumberDisplay),
    alignThermal(receiptLabels.posReference, posReferenceDisplay),
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
        compactReceiptText(item.name)
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
    alignThermal(receiptLabels.amountTendered, thermalAmount(tenderedAmount)),
    alignThermal('Change:', thermalAmount(receiptChangeDisplay))
  );
  if (paymentMethodDisplay) {
    thermalTextLines.push(alignThermal(receiptLabels.payment, paymentMethodDisplay));
  }
  thermalTextLines.push(
    '',
    centerThermal(`DATE: ${format(orderDate, 'yyyy-MM-dd')} TIME: ${format(orderDate, 'HH:mm:ss')}`)
  );
  if (effectiveShowFooter && showReceiptMarkers) {
    thermalTextLines.push('', centerThermal(legalReceiptEndTitle), legalRule);
  }
  const thermalReceiptText = thermalTextLines.join('\n').replace(/\n{3,}/g, '\n\n');
  const receiptRootClass = 'receipt-root bg-white text-black font-mono';
  const receiptRootStyle: React.CSSProperties = {
    width: printContentWidth,
    maxWidth: '100%',
    margin: '0 auto',
    padding: layout.contentPadding,
    boxSizing: 'border-box',
    fontFamily: "'Courier New', Courier, monospace",
    fontSize: `${layout.fontSizePx}px`,
    lineHeight: layout.lineHeight,
    color: '#000',
    backgroundColor: '#fff',
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
        #${elementId} *,
        #${elementId} *::before,
        #${elementId} *::after {
          box-sizing: border-box;
          overflow-wrap: anywhere;
          word-break: break-word;
          letter-spacing: 0;
        }

        #${elementId} {
          width: ${printContentWidth};
          max-width: 100%;
          margin: 0 auto;
          padding: ${layout.contentPadding};
          font-family: 'Courier New', Courier, monospace;
          font-size: ${layout.fontSizePx}px;
          line-height: ${layout.lineHeight};
          color: #000;
          background: #fff;
          text-align: left;
        }
        #${elementId} p {
          margin: 0;
        }
        #${elementId} .receipt-center,
        #${elementId} .receipt-legal-marker,
        #${elementId} .receipt-vat-status {
          display: block;
          width: 100%;
          margin-left: auto;
          margin-right: auto;
          text-align: center !important;
        }
        #${elementId} .receipt-vat-status {
          white-space: normal;
        }
        #${elementId} .receipt-header {
          margin: 0 0 ${layout.sectionGap};
          text-align: center;
        }
        #${elementId} .receipt-legal-marker {
          display: block;
          width: 100%;
          max-width: 100%;
          margin-bottom: ${layout.rowGap};
          font-size: ${layout.bodyFontSizePx}px;
          line-height: 1.25;
          font-weight: 700;
          white-space: normal;
        }
        #${elementId} .receipt-business-name {
          margin-bottom: ${layout.rowGap};
          font-size: ${layout.businessNameFontSizePx}px;
          line-height: 1.25;
          font-weight: 700;
          text-align: center;
        }
        #${elementId} .receipt-meta {
          font-size: ${layout.metaFontSizePx}px;
          line-height: 1.35;
          text-align: center;
        }
        #${elementId} .receipt-body {
          font-size: ${layout.bodyFontSizePx}px;
          line-height: ${layout.lineHeight};
        }
        #${elementId} .receipt-copy-marker {
          margin-top: ${layout.rowGap};
          font-size: ${layout.bodyFontSizePx}px;
          font-weight: 700;
          text-align: center;
        }
        #${elementId} .receipt-order-meta {
          display: flex;
          flex-direction: column;
          gap: ${layout.rowGap};
          margin-top: ${layout.sectionGap};
        }
        #${elementId} .receipt-label-row,
        #${elementId} .receipt-value-row {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          align-items: baseline;
          column-gap: 2mm;
          min-width: 0;
        }
        #${elementId} .receipt-label-row {
          grid-template-columns: minmax(0, ${layout.labelColumnPercent}%) minmax(0, 1fr);
        }
        #${elementId} .receipt-label-row > :last-child,
        #${elementId} .receipt-value-row > :last-child {
          min-width: 0;
          text-align: right;
          overflow-wrap: anywhere;
        }
        #${elementId} .receipt-rule {
          width: 100%;
          margin: ${layout.ruleGap} 0;
          overflow: hidden;
          font-size: ${layout.metaFontSizePx}px;
          line-height: 1;
          text-align: center;
          white-space: nowrap;
          overflow-wrap: normal;
          word-break: normal;
        }
        #${elementId} .receipt-items,
        #${elementId} .receipt-tax,
        #${elementId} .receipt-totals,
        #${elementId} .receipt-date,
        #${elementId} .receipt-footer {
          margin-top: ${layout.sectionGap};
        }
        #${elementId} .receipt-thank-you {
          margin-top: ${layout.rowGap};
        }
        #${elementId} .receipt-item {
          margin-bottom: ${layout.itemGap};
        }
        #${elementId} .receipt-item-name {
          margin-top: ${layout.rowGap};
          line-height: ${layout.lineHeight};
        }
        #${elementId} .receipt-discount-row {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          gap: 2mm;
          margin-top: ${layout.rowGap};
          font-size: ${layout.metaFontSizePx}px;
          line-height: 1.3;
        }
        #${elementId} .receipt-discount-row > :last-child,
        #${elementId} .receipt-total-row > :last-child {
          text-align: right;
        }
        #${elementId} .receipt-total-row {
          font-weight: 700;
        }
        #${elementId} .receipt-total-row * {
          font-weight: 700;
        }
        #${elementId} .receipt-qr-container {
          display: flex;
          width: 100%;
          min-height: ${layout.qrMinHeight};
          margin-top: 0;
          justify-content: center;
          align-items: center;
          text-align: center;
        }
        #${elementId} .receipt-qr-code {
          display: block;
          width: ${layout.qrSize};
          height: ${layout.qrSize};
          margin: 0 auto;
          padding: ${layout.qrPadding};
          background: #fff;
        }
        #${elementId} .receipt-qr-code svg {
          display: block;
          width: 100%;
          height: 100%;
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
            position: static;
            width: ${printContentWidth};
            max-width: none;
            top: 0;
            margin: 0 auto;
            padding: ${layout.contentPadding};
          }
          @page {
            margin: 0;
            size: ${resolvedPaperWidth} auto;
          }
        }
        ` : ''}
      `}</style>

      {effectiveShowHeader && (
        <div className="receipt-header receipt-center">
          {showReceiptMarkers && <p className="receipt-legal-marker">{legalReceiptTitle}</p>}
          <p className="receipt-business-name">{businessNameDisplay}</p>
          {sellerAddressLines.length > 0 ? (
            sellerAddressLines.map((line, index) => (
              <p key={`${line}-${index}`} className="receipt-meta">
                {line.toUpperCase()}
              </p>
            ))
          ) : (
            <p className="receipt-meta">ADDRESS: N/A</p>
          )}
          <p className="receipt-meta">CELL: {businessPhone || 'N/A'}</p>
          <p className="receipt-meta">EMAIL: {businessEmail || 'N/A'}</p>
          <p className="receipt-body">TIN: {sellerTin || 'N/A'}</p>
          <p
            className="receipt-vat-status receipt-body"
            style={{ display: 'block', width: '100%', margin: '0 auto', textAlign: 'center' }}
          >
            {vatRegistrationLabel.toUpperCase()}
          </p>
          {isCopyReceipt && (
            <p className="receipt-copy-marker">
              {receiptTypeLabel}
            </p>
          )}
          {taxOfficeLabel && <p className="receipt-meta">{taxOfficeLabel.toUpperCase()}</p>}
          {pumpName && <p className="receipt-meta">PUMP: {pumpName.toUpperCase()}</p>}
        </div>
      )}

      <div className="receipt-order-meta receipt-body">
        <div className="receipt-label-row">
          <span>{receiptLabels.buyer}</span>
          <span>{buyerName || 'Walk-in Customer'}</span>
        </div>
        <div className="receipt-label-row">
          <span>{receiptLabels.buyerTin}</span>
          <span>{buyerTin || 'N/A'}</span>
        </div>
        <div className="receipt-label-row">
          <span>{receiptLabels.receiptNumber}</span>
          <span>{receiptNumberDisplay}</span>
        </div>
        <div className="receipt-label-row">
          <span>{receiptLabels.posReference}</span>
          <span>{posReferenceDisplay}</span>
        </div>
        {!isFiscalizedReceipt && (
          <div className="receipt-label-row">
            <span>{receiptLabels.receiptStatus}</span>
            <span>{fiscalStatusDisplay}</span>
          </div>
        )}
      </div>

      {effectiveShowItemDetails && (
        <div className="receipt-items receipt-body">
          <p className="receipt-rule">{legalRule}</p>
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
              <div key={`${item.id}-${index}`} className="receipt-item">
                <div className="receipt-value-row">
                  <span>{formatReceiptQuantity(itemQuantity)} X {formatReceiptAmount(itemPrice)}</span>
                  <span>{formatReceiptAmount(itemTotal)} {itemTaxCode}</span>
                </div>
                <p className="receipt-item-name">{compactReceiptText(item.name)}</p>
                {itemDiscount > 0 && (
                  <div className="receipt-discount-row">
                    <span>{compactReceiptText(itemDiscountName).toUpperCase()}</span>
                    <span>-{formatReceiptAmount(itemDiscount)}</span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {effectiveShowTaxBreakdown && (legalTaxBreakdown.length > 0 || legalLevyBreakdown.length > 0) && (
        <div className="receipt-tax receipt-body">
          <p className="receipt-rule">{legalRule}</p>
          {legalTaxBreakdown.map((tax, index) => {
            const rateText = formatReceiptRate(tax.rate);
            const rateLabel = `${tax.code}-${rateText}%`;
            return (
              <React.Fragment key={`${rateLabel}-${index}`}>
                <div className="receipt-value-row">
                  <span>TAXABLE {rateLabel}</span>
                  <span>{formatReceiptAmount(tax.taxableValue)}</span>
                </div>
                <div className="receipt-value-row">
                  <span>VAT {rateLabel}</span>
                  <span>{formatReceiptAmount(tax.vatAmount)}</span>
                </div>
              </React.Fragment>
            );
          })}
          <div className="receipt-value-row">
            <span>TOTAL VAT:</span>
            <span>{formatReceiptAmount(receiptVatTotal)}</span>
          </div>
          {legalLevyBreakdown.map((levy, index) => (
            <div key={`${levy.levyTypeId}-${levy.levyRate}-${index}`} className="receipt-value-row">
              <span>LEVY {levy.levyTypeId}-{formatReceiptRate(levy.levyRate)}%</span>
              <span>{formatReceiptAmount(levy.levyAmount)}</span>
            </div>
          ))}
        </div>
      )}

      <div className="receipt-totals receipt-body">
        <p className="receipt-rule">{legalRule}</p>
        {receiptDiscountTotal > 0 && (
          <div className="receipt-value-row">
            <span>TOTAL DISCOUNT:</span>
            <span>{formatReceiptAmount(receiptDiscountTotal)}</span>
          </div>
        )}
        <div className="receipt-value-row receipt-total-row">
          <span>TOTAL:</span>
          <span>{formatReceiptAmount(normalizedFinalPayable)}</span>
        </div>
        <div className="receipt-value-row">
          <span>{receiptLabels.amountTendered}</span>
          <span>{formatReceiptAmount(tenderedAmount)}</span>
        </div>
        <div className="receipt-value-row">
          <span>Change:</span>
          <span>{formatReceiptAmount(receiptChangeDisplay)}</span>
        </div>
        {paymentMethodDisplay && (
          <div className="receipt-value-row">
            <span>{receiptLabels.payment}</span>
            <span>{paymentMethodDisplay}</span>
          </div>
        )}
      </div>

      <div className="receipt-date receipt-center receipt-body">
        <p>DATE: {format(orderDate, 'yyyy-MM-dd')} TIME: {format(orderDate, 'HH:mm:ss')}</p>
        {shouldRenderQr ? (
          <div className="receipt-qr-container" style={qrContainerStyle}>
            <div className="receipt-qr-code" style={qrSizeStyle} aria-label="Receipt validation QR code">
              <QRCode
                value={qrPayload}
                size={256}
                level="M"
                style={{ height: '100%', width: '100%' }}
              />
            </div>
          </div>
        ) : null}
      </div>

      {effectiveShowFooter && showReceiptMarkers && (
        <div className="receipt-footer receipt-center receipt-body">
          <p className="receipt-legal-marker">{legalReceiptEndTitle}</p>
        </div>
      )}
    </div>

  );
};
