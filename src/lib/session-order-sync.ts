'use client';

import { authFetch } from '@/lib/auth-fetch';
import { db, type Order, type OrderItem } from '@/lib/db';

const toNumber = (value: unknown, fallback: number = 0): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const toOptionalString = (value: unknown): string | undefined => {
  if (value === undefined || value === null) {
    return undefined;
  }

  const normalized = String(value).trim();
  return normalized || undefined;
};

const toIsoString = (value: unknown, fallback?: string): string => {
  const normalized = toOptionalString(value);
  if (!normalized) {
    return fallback || new Date().toISOString();
  }
  return normalized;
};

const getOrderItemInventoryId = (item: any): string | undefined => {
  const rawId =
    item?.inventoryItemId ??
    item?.inventory_item_id ??
    item?.inventoryItem ??
    item?.inventory_item;
  return toOptionalString(rawId);
};

const mapBackendOrderItemToLocal = (item: any): OrderItem => {
  const inventoryItemId = getOrderItemInventoryId(item);
  const mraProductCode = toOptionalString(item?.mra_product_code ?? item?.mraProductCode);
  const vatCategory = toOptionalString(item?.vat_category ?? item?.vatCategory);
  const taxType = toOptionalString(item?.tax_type ?? item?.taxType);
  const taxCalculationMethod = toOptionalString(
    item?.tax_calculation_method ?? item?.taxCalculationMethod
  ) as OrderItem['taxCalculationMethod'];
  const taxRate = toNumber(item?.tax_rate ?? item?.taxRate);
  const subtotal = toNumber(item?.subtotal);
  const taxAmount = toNumber(item?.tax_amount ?? item?.taxAmount);

  return {
    id: String(item?.id ?? ''),
    inventoryItemId,
    name: String(item?.name ?? 'Unknown Item'),
    quantity: toNumber(item?.quantity),
    notes: String(item?.notes ?? ''),
    price: toNumber(item?.price),
    mraProductCode,
    mra_product_code: mraProductCode,
    vatCategory,
    vat_category: vatCategory,
    taxRate,
    tax_rate: taxRate,
    taxType,
    tax_type: taxType,
    taxCalculationMethod,
    tax_calculation_method: taxCalculationMethod,
    subtotal,
    taxAmount,
    tax_amount: taxAmount,
    total: toNumber(item?.total),
  };
};

const mapBackendOrderToLocal = (
  order: any,
  fallbackSessionId: string,
  fallbackBranchId: string
): Order => {
  const createdAt = toIsoString(order?.created_at ?? order?.createdAt);
  const updatedAt = toIsoString(order?.updated_at ?? order?.updatedAt, createdAt);
  const orderType = toOptionalString(order?.order_type ?? order?.orderType) as Order['orderType'];
  const paymentMethod = toOptionalString(
    order?.payment_method ?? order?.paymentMethod
  ) as Order['paymentMethod'];
  const taxRateName = toOptionalString(order?.tax_rate_name ?? order?.taxRateName);
  const taxType = toOptionalString(order?.tax_type ?? order?.taxType) as Order['taxType'];
  const fiscalInvoiceNumber = toOptionalString(
    order?.fiscal_invoice_number ?? order?.fiscalInvoiceNumber
  );
  const eisUuid = toOptionalString(order?.eis_uuid ?? order?.eisUuid);
  const eisStatus = toOptionalString(order?.eis_status ?? order?.eisStatus) as Order['eisStatus'];
  const eisSyncState = toOptionalString(order?.eis_sync_state ?? order?.eisSyncState);
  const eisSubmittedAt = toOptionalString(
    order?.eis_submitted_at ?? order?.eisSubmittedAt
  );
  const qrCodePayload = toOptionalString(order?.qr_code_payload ?? order?.qrCodePayload);
  const digitalSignature = toOptionalString(
    order?.digital_signature ?? order?.digitalSignature
  );
  const eisValidationMetadata =
    order?.eis_validation_metadata ?? order?.eisValidationMetadata;
  const mraSubmission =
    order?.mra_submission ??
    order?.mraSubmission ??
    eisValidationMetadata?.mra_submission ??
    eisValidationMetadata?.mraSubmission;
  const isFiscalLocked = Boolean(
    order?.is_fiscal_locked ?? order?.isFiscalLocked ?? false
  );
  const items = Array.isArray(order?.items)
    ? order.items.map((item: any) => mapBackendOrderItemToLocal(item))
    : [];

  return {
    id: String(order?.id ?? ''),
    orderNumber: toNumber(order?.order_number ?? order?.orderNumber),
    branchId: String(order?.branch ?? fallbackBranchId ?? ''),
    sessionId: String(order?.session ?? fallbackSessionId ?? ''),
    pumpName: toOptionalString(order?.pump_name ?? order?.pumpName),
    orderType,
    items,
    status: String(order?.status ?? 'Completed') as Order['status'],
    subtotal: toNumber(order?.subtotal),
    total: toNumber(order?.total),
    tax: toNumber(order?.vat_amount ?? order?.vatAmount ?? order?.tax),
    tip: toNumber(order?.tip),
    paymentMethod,
    customerName: toOptionalString(order?.customer_name ?? order?.customerName),
    customer_name: toOptionalString(order?.customer_name ?? order?.customerName),
    customerPhone: toOptionalString(order?.customer_phone ?? order?.customerPhone),
    customer_phone: toOptionalString(order?.customer_phone ?? order?.customerPhone),
    customerTin: toOptionalString(order?.customer_tin ?? order?.customerTin),
    customer_tin: toOptionalString(order?.customer_tin ?? order?.customerTin),
    customerEmail: toOptionalString(order?.customer_email ?? order?.customerEmail),
    customer_email: toOptionalString(order?.customer_email ?? order?.customerEmail),
    customerAddress: toOptionalString(order?.customer_address ?? order?.customerAddress),
    customer_address: toOptionalString(order?.customer_address ?? order?.customerAddress),
    customerNotes: toOptionalString(order?.customer_notes ?? order?.customerNotes),
    customer_notes: toOptionalString(order?.customer_notes ?? order?.customerNotes),
    buyerName: toOptionalString(order?.buyer_name ?? order?.buyerName),
    buyer_name: toOptionalString(order?.buyer_name ?? order?.buyerName),
    buyerTin: toOptionalString(order?.buyer_tin ?? order?.buyerTin),
    buyer_tin: toOptionalString(order?.buyer_tin ?? order?.buyerTin),
    cogs: toNumber(order?.cogs),
    taxRateName,
    tax_rate_name: taxRateName,
    taxRateValue: toNumber(order?.tax_rate_value ?? order?.taxRateValue),
    tax_rate_value: toNumber(order?.tax_rate_value ?? order?.taxRateValue),
    taxType,
    tax_type: taxType,
    vatAmount: toNumber(order?.vat_amount ?? order?.vatAmount),
    vat_amount: toNumber(order?.vat_amount ?? order?.vatAmount),
    netAmount: toNumber(order?.net_amount ?? order?.netAmount),
    net_amount: toNumber(order?.net_amount ?? order?.netAmount),
    grossAmount: toNumber(order?.gross_amount ?? order?.grossAmount),
    gross_amount: toNumber(order?.gross_amount ?? order?.grossAmount),
    fiscalInvoiceNumber,
    fiscal_invoice_number: fiscalInvoiceNumber,
    eisUuid,
    eis_uuid: eisUuid,
    eisStatus,
    eis_status: eisStatus,
    eisSyncState,
    eis_sync_state: eisSyncState,
    eisValidationMetadata,
    eis_validation_metadata: eisValidationMetadata,
    mraSubmission,
    mra_submission: mraSubmission,
    eisSubmittedAt,
    eis_submitted_at: eisSubmittedAt,
    qrCodePayload,
    qr_code_payload: qrCodePayload,
    digitalSignature,
    digital_signature: digitalSignature,
    isFiscalLocked: isFiscalLocked,
    is_fiscal_locked: isFiscalLocked,
    createdAt,
    updatedAt,
  };
};

export const syncSessionOrdersToLocalDb = async ({
  sessionId,
  branchId,
}: {
  sessionId: string;
  branchId: string;
}): Promise<Order[]> => {
  const response = await authFetch.fetch<any>(
    `/sessions/orders/?session_id=${encodeURIComponent(String(sessionId))}`
  );
  const backendOrders = Array.isArray(response?.results)
    ? response.results
    : Array.isArray(response)
      ? response
      : [];

  const resolvedOrders: Order[] = [];

  for (const order of backendOrders) {
    const localOrder = mapBackendOrderToLocal(order, sessionId, branchId);
    const existingOrder = await db.orders.get(localOrder.id);

    if (existingOrder?._dirty) {
      resolvedOrders.push(existingOrder);
      continue;
    }

    await db.orders.put(localOrder);
    resolvedOrders.push(localOrder);
  }

  return resolvedOrders;
};
