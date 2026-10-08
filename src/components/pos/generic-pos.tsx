'use client';

import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { useLiveQuery } from 'dexie-react-hooks';
import {
  Plus,
  Minus,
  Trash2,
  UserPlus,
  CreditCard,
  DollarSign,
  ShoppingBasket,
  Package,
  Smartphone,
  CheckCircle,
  Eye,
  Loader2,
  Printer,
  Wifi,
  WifiOff,
  Download,
} from 'lucide-react';
import type { CartItem, PaymentMethod } from '@/app/dashboard/pos/page';
import type { InventoryItem, Order, TaxRate } from '@/lib/db';
import { useCurrency } from '@/hooks/use-currency';
import { authFetch } from '@/lib/auth-fetch';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardFooter,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  DialogClose,
  DialogFooter,
  DialogDescription,
} from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { Input } from '../ui/input';
import { Receipt } from './receipt';
import { PrinterConfigModal } from './printer-config-modal';
import { db } from '@/lib/db';
import { useToast } from '@/hooks/use-toast';
import { getOfflineBusinessProfile, resolveOfflineBusinessId } from '@/lib/business-profile';
import {
  PRINTER_CONFIG_UPDATED_EVENT,
  normalizePrinterPaperWidth,
  type PrinterConfig,
  type PrinterPaperWidth,
  type PrinterSettings,
} from '@/lib/services/printer-service';
import { getNextReceiptCopyNumber, markReceiptPrinted } from '@/lib/services/receipt-copy-service';
import { safeLocalStorageGetItem } from '@/lib/safe-local-storage';
import { formatInventoryQuantity } from '@/lib/quantity-format';
import {
  canShareThermalReceiptPdf,
  downloadThermalReceiptPdf,
  generateThermalReceiptPdf,
  shareThermalReceiptPdf,
} from '@/lib/thermal-receipt-pdf';
import { ReceiptPdfExportDialog } from './receipt-pdf-export-dialog';

export type BuyerDetails = {
  name?: string;
  phone?: string;
  tin?: string;
  authorizationCode?: string;
  isExport?: boolean;
  isReliefSupply?: boolean;
  vat5ProjectNumber?: string;
  vat5CertificateNumber?: string;
  vat5Quantity?: number;
};

export interface PosProps {
  inventory: InventoryItem[];
  displayItems?: InventoryItem[];
  emptyStateTitle?: string;
  emptyStateDescription?: string;
  cart: CartItem[];
  onAddToCart: (item: InventoryItem, quantity?: number, price?: number) => void | Promise<void>;
  onUpdateQuantity: (itemId: string, quantity: number) => void;
  onApplyDiscount?: (itemId: string, discount: AppliedDiscount | null) => void;
  onClearCart: () => void;
  onCheckout: (paymentMethod: PaymentMethod, tip: number, buyerDetails?: BuyerDetails) => Promise<Order | null>;
  productIcon?: React.ReactNode;
  viewMode?: 'grid' | 'list';
  defaultTaxRate?: TaxRate;
  eisEnabled?: boolean;
  blockSalesIfTaxMappingMissing?: boolean;
  isEisInvoiceSubmissionBlocked?: boolean;
  eisInvoiceSubmissionBlockedMessage?: string;
  branchId?: string;
}

type ReceiptDisplaySettings = {
  showHeader: boolean;
  showFooter: boolean;
  showQRCode: boolean;
  showItemDetails: boolean;
  showTaxBreakdown: boolean;
};

type DiscountRule = {
  id: string;
  name: string;
  discount_type?: 'percentage' | 'fixed';
  discountType?: 'percentage' | 'fixed';
  value: number | string;
  applies_to?: 'all' | 'products' | 'categories';
  appliesTo?: 'all' | 'products' | 'categories';
  product_ids?: string[];
  productIds?: string[];
  categories?: string[];
  is_active?: boolean;
  isActive?: boolean;
};

export type AppliedDiscount = {
  ruleId: string;
  name: string;
  type: 'percentage' | 'fixed';
  value: number;
};

const toFiniteNumber = (value: unknown, fallback = 0): number => {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const getCartDiscount = (item: CartItem): AppliedDiscount | null => {
  const ruleId = String((item as any).discountRuleId || (item as any).discount_rule_id || '').trim();
  const name = String((item as any).discountName || (item as any).discount_name || '').trim();
  const rawType = String((item as any).discountType || (item as any).discount_type || '').trim().toLowerCase();
  const type = rawType === 'fixed' ? 'fixed' : rawType === 'percentage' ? 'percentage' : '';
  const value = toFiniteNumber((item as any).discountValue ?? (item as any).discount_value, 0);
  if (!ruleId || !type || value <= 0) return null;
  return { ruleId, name, type, value };
};

const calculateDiscountAmount = (lineAmount: number, discount?: AppliedDiscount | null): number => {
  const base = Math.max(0, toFiniteNumber(lineAmount, 0));
  if (!discount) return 0;
  const value = Math.max(0, toFiniteNumber(discount.value, 0));
  const amount = discount.type === 'percentage' ? base * value / 100 : value;
  return Math.min(base, Math.round(amount * 100) / 100);
};

const resolveCartDiscountAmount = (item: CartItem): number => {
  const explicit = toFiniteNumber((item as any).discountAmount ?? (item as any).discount_amount, NaN);
  const lineAmount = resolveCartLineTotal(item);
  if (Number.isFinite(explicit) && explicit > 0) return Math.min(lineAmount, explicit);
  return calculateDiscountAmount(lineAmount, getCartDiscount(item));
};

const isCartDiscountInvalid = (item: CartItem): boolean => {
  const lineAmount = resolveCartLineTotal(item);
  const discount = getCartDiscount(item);
  if (!discount || lineAmount <= 0) return false;

  if (discount.type === 'percentage' && discount.value >= 100) return true;
  if (discount.type === 'fixed' && discount.value >= lineAmount) return true;

  return resolveCartDiscountAmount(item) >= lineAmount;
};

const normalizeDiscountRule = (rule: any): DiscountRule | null => {
  const id = String(rule?.id || '').trim();
  const name = String(rule?.name || '').trim();
  const type = String(rule?.discount_type ?? rule?.discountType ?? '').trim().toLowerCase();
  const value = toFiniteNumber(rule?.value, 0);
  if (!id || !name || !['percentage', 'fixed'].includes(type) || value <= 0) return null;
  return {
    id,
    name,
    discount_type: type as DiscountRule['discount_type'],
    value,
    applies_to: String(rule?.applies_to ?? rule?.appliesTo ?? 'all').trim().toLowerCase() as DiscountRule['applies_to'],
    product_ids: Array.isArray(rule?.product_ids) ? rule.product_ids.map(String) : Array.isArray(rule?.productIds) ? rule.productIds.map(String) : [],
    categories: Array.isArray(rule?.categories) ? rule.categories.map(String) : [],
    is_active: Boolean(rule?.is_active ?? rule?.isActive ?? true),
  };
};

const discountAppliesToItem = (rule: DiscountRule, item: CartItem): boolean => {
  const appliesTo = rule.applies_to ?? rule.appliesTo ?? 'all';
  if (appliesTo === 'all') return true;
  if (appliesTo === 'products') {
    const productIds = (rule.product_ids ?? rule.productIds ?? []).map(String);
    const itemIds = [item.id, item.inventoryItemId, (item as any).inventory_item_id].map((value) => String(value || '').trim());
    return itemIds.some((id) => id && productIds.includes(id));
  }
  if (appliesTo === 'categories') {
    const categories = (rule.categories ?? []).map((value) => String(value || '').trim().toLowerCase());
    return categories.includes(String(item.category || '').trim().toLowerCase());
  }
  return false;
};

const DEFAULT_RECEIPT_DISPLAY_SETTINGS: ReceiptDisplaySettings = {
  showHeader: true,
  showFooter: true,
  showQRCode: true,
  showItemDetails: true,
  showTaxBreakdown: true,
};

const normalizeBuyerDetails = (details?: BuyerDetails | null): BuyerDetails | undefined => {
  if (!details) {
    return undefined;
  }

  const name = details.name?.trim();
  const phone = details.phone?.trim();
  const tin = details.tin?.trim();
  const authorizationCode = details.authorizationCode?.trim();
  const vat5ProjectNumber = details.vat5ProjectNumber?.trim();
  const vat5CertificateNumber = details.vat5CertificateNumber?.trim();
  const vat5Quantity = Number(details.vat5Quantity);
  const hasVat5Quantity = Number.isFinite(vat5Quantity) && vat5Quantity > 0;
  const isExport = details.isExport === true;
  const isReliefSupply = details.isReliefSupply === true;

  if (!name && !phone && !tin && !authorizationCode && !isExport && !isReliefSupply && !vat5ProjectNumber && !vat5CertificateNumber && !hasVat5Quantity) {
    return undefined;
  }

  return {
    name: name || undefined,
    phone: phone || undefined,
    tin: tin || undefined,
    authorizationCode: authorizationCode || undefined,
    isExport,
    isReliefSupply,
    vat5ProjectNumber: vat5ProjectNumber || undefined,
    vat5CertificateNumber: vat5CertificateNumber || undefined,
    vat5Quantity: hasVat5Quantity ? vat5Quantity : undefined,
  };
};

const hasCompleteFiscalInvoiceNumber = (value: string | null | undefined): boolean => {
  const fiscal = String(value ?? '').trim();
  if (!fiscal) {
    return false;
  }
  if (/^(pending|draft|null|undefined)$/i.test(fiscal)) {
    return false;
  }

  const suffix = fiscal.split('-').pop() ?? '';
  if (!/^\d+$/.test(suffix)) {
    return true;
  }

  return Number.parseInt(suffix, 10) > 0;
};

const toReceiptString = (value: unknown): string => String(value ?? '').trim();

const isReceiptValidationUrl = (value: unknown): boolean => {
  const raw = toReceiptString(value);
  return /^https?:\/\//i.test(raw) || /receiptvalidation\/validate/i.test(raw);
};

const parseReceiptJson = (value: string): unknown | null => {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
};

const findNestedStringByKeys = (source: unknown, keys: string[]): string => {
  const preferredKeys = new Set(keys.map((key) => key.replace(/[^a-z0-9]/gi, '').toLowerCase()));
  const normalizeKey = (key: string): string => key.replace(/[^a-z0-9]/gi, '').toLowerCase();
  const queue: unknown[] = [source];
  const seen = new Set<unknown>();

  while (queue.length > 0) {
    const current = queue.shift();
    if (current === null || current === undefined) {
      continue;
    }

    if (typeof current === 'string') {
      const raw = current.trim();
      const parsed = parseReceiptJson(raw);
      if (parsed && typeof parsed === 'object') {
        queue.push(parsed);
      }
      continue;
    }

    if (typeof current !== 'object' || seen.has(current)) {
      continue;
    }
    seen.add(current);

    if (Array.isArray(current)) {
      queue.push(...current);
      continue;
    }

    for (const [key, value] of Object.entries(current as Record<string, unknown>)) {
      if (preferredKeys.has(normalizeKey(key)) && value !== undefined && value !== null && String(value).trim() !== '') {
        return String(value).trim();
      }
      if (value && typeof value === 'object') {
        queue.push(value);
      } else if (typeof value === 'string') {
        const parsed = parseReceiptJson(value.trim());
        if (parsed && typeof parsed === 'object') {
          queue.push(parsed);
        }
      }
    }
  }

  return '';
};

const extractFiscalInvoiceNumber = (order: Partial<Order> | null | undefined): string => {
  return findNestedStringByKeys(order, [
    'fiscalInvoiceNumber',
    'fiscal_invoice_number',
    'invoiceNumber',
    'invoice_number',
    'receiptNumber',
    'receipt_number',
  ]);
};

const findReceiptValidationUrl = (source: unknown): string => {
  const queue: unknown[] = [source];
  const seen = new Set<unknown>();
  const preferredKeys = new Set([
    'validationurl',
    'mraValidationurl',
    'mravalidationurl',
    'offlinevalidationurl',
    'offlinevalidationURL',
    'qrcodepayload',
    'qrpayload',
  ]);
  const normalizeKey = (key: string): string => key.replace(/[^a-z0-9]/gi, '').toLowerCase();

  while (queue.length > 0) {
    const current = queue.shift();
    if (current === null || current === undefined) {
      continue;
    }

    if (typeof current === 'string') {
      const raw = current.trim();
      if (isReceiptValidationUrl(raw)) {
        return raw;
      }
      const parsed = parseReceiptJson(raw);
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
      const normalizedKey = normalizeKey(key);
      if (preferredKeys.has(normalizedKey) && isReceiptValidationUrl(value)) {
        return toReceiptString(value);
      }
      if (value && typeof value === 'object') {
        queue.push(value);
      } else if (typeof value === 'string') {
        const parsed = parseReceiptJson(value.trim());
        if (parsed && typeof parsed === 'object') {
          queue.push(parsed);
        }
      }
    }
  }

  return '';
};

const extractReceiptSignature = (order: Partial<Order> | null | undefined): string => {
  return findNestedStringByKeys(order, [
    'digitalSignature',
    'digital_signature',
    'offlineSignature',
    'offline_signature',
    'offlineSignatureHash',
    'offline_signature_hash',
    'invoiceSignature',
    'invoice_signature',
    'signature',
  ]);
};

const extractReceiptValidationPayload = (order: Partial<Order> | null | undefined): string => {
  return findReceiptValidationUrl(order);
};

const hasFiscalReceiptPrintData = (order: Partial<Order> | null | undefined): boolean => {
  const fiscalNumber = extractFiscalInvoiceNumber(order);
  const validationPayload = extractReceiptValidationPayload(order);
  const signature = extractReceiptSignature(order);

  return (
    hasCompleteFiscalInvoiceNumber(fiscalNumber) &&
    Boolean(validationPayload || signature)
  );
};

const getMraSubmissionState = (order: Partial<Order> | null | undefined): string => {
  const metadata =
    (order as any)?.eisValidationMetadata ||
    (order as any)?.eis_validation_metadata ||
    {};
  const submission =
    (order as any)?.mraSubmission ||
    (order as any)?.mra_submission ||
    metadata?.mraSubmission ||
    metadata?.mra_submission ||
    {};
  return String(submission?.state || '').trim().toLowerCase();
};

const isOfflineQueuedReceipt = (order: Partial<Order> | null | undefined): boolean => {
  const status = String((order as any)?.eisStatus ?? (order as any)?.eis_status ?? '').trim().toUpperCase();
  const syncState = String((order as any)?.eisSyncState ?? (order as any)?.eis_sync_state ?? '').trim().toUpperCase();
  const state = getMraSubmissionState(order);
  return (
    state === 'offline_queued' ||
    syncState === 'OFFLINE_QUEUED' ||
    status.includes('OFFLINE') ||
    status.includes('QUEUED')
  );
};

const refreshFiscalReceiptDataFromBackend = async (order: Order): Promise<Order | null> => {
  const orderId = String((order as any)?.id ?? '').trim();
  if (!orderId || typeof window === 'undefined') {
    return null;
  }

  const tokensStr = window.localStorage.getItem('handypos-auth-tokens');
  const tokens = tokensStr ? JSON.parse(tokensStr) : null;
  const accessToken = tokens?.access;
  if (!accessToken) {
    return null;
  }

  const response = await fetch(
    `${process.env.NEXT_PUBLIC_API_URL || 'https://pos3.express-travel-ticketing.online/api'}/sessions/orders/${orderId}/`,
    {
      headers: {
        Authorization: `Bearer ${accessToken}`,
      },
    }
  );

  if (!response.ok) {
    return null;
  }

  const data = await response.json();
  const fiscalInvoiceNumber = extractFiscalInvoiceNumber(data);
  const qrCodePayload = extractReceiptValidationPayload(data);
  const digitalSignature = extractReceiptSignature(data);
  const eisStatus = findNestedStringByKeys(data, ['eis_status', 'eisStatus', 'status']);
  const eisUuid = findNestedStringByKeys(data, ['eis_uuid', 'eisUuid', 'uuid']);
  const eisSubmittedAt = findNestedStringByKeys(data, ['eis_submitted_at', 'eisSubmittedAt', 'submittedAt']);
  const eisValidationMetadata =
    (data as any)?.eis_validation_metadata ??
    (data as any)?.eisValidationMetadata ??
    (data as any)?.order?.eis_validation_metadata ??
    (data as any)?.order?.eisValidationMetadata ??
    (data as any)?.data?.eis_validation_metadata ??
    (data as any)?.data?.eisValidationMetadata;
  const mraSubmission =
    (data as any)?.mra_submission ??
    (data as any)?.mraSubmission ??
    eisValidationMetadata?.mra_submission ??
    eisValidationMetadata?.mraSubmission ??
    (data as any)?.order?.mra_submission ??
    (data as any)?.order?.mraSubmission;

  const update: Record<string, unknown> = {};
  const setUpdate = (snakeKey: string, camelKey: string, value: unknown) => {
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      update[snakeKey] = value;
      update[camelKey] = value;
    }
  };

  setUpdate('fiscal_invoice_number', 'fiscalInvoiceNumber', fiscalInvoiceNumber);
  setUpdate('qr_code_payload', 'qrCodePayload', qrCodePayload);
  setUpdate('digital_signature', 'digitalSignature', digitalSignature);
  setUpdate('eis_status', 'eisStatus', eisStatus);
  setUpdate('eis_uuid', 'eisUuid', eisUuid);
  setUpdate('eis_submitted_at', 'eisSubmittedAt', eisSubmittedAt);
  if (eisValidationMetadata) {
    update.eis_validation_metadata = eisValidationMetadata;
    update.eisValidationMetadata = eisValidationMetadata;
  }
  if (mraSubmission) {
    update.mra_submission = mraSubmission;
    update.mraSubmission = mraSubmission;
  }

  if (!Object.keys(update).length) {
    return null;
  }

  await db.orders.update(orderId, update as any);
  return {
    ...order,
    ...update,
  } as Order;
};

const resolveCompletedSaleSubmissionDisplay = (
  order: Partial<Order> | null | undefined,
  isBrowserOnline: boolean,
  eisEnabled?: boolean
): {
  label: string;
  description: string;
  tone: 'accepted' | 'pending' | 'offline' | 'rejected';
} => {
  const status = String((order as any)?.eisStatus ?? (order as any)?.eis_status ?? '').trim().toUpperCase();
  const syncStatus = String((order as any)?.syncStatus ?? (order as any)?.sync_status ?? '').trim().toLowerCase();
  const syncError = String((order as any)?.syncError ?? (order as any)?.sync_error ?? '').trim();
  const metadata =
    (order as any)?.eisValidationMetadata ||
    (order as any)?.eis_validation_metadata ||
    {};
  const mraSubmission =
    (order as any)?.mraSubmission ||
    (order as any)?.mra_submission ||
    metadata?.mraSubmission ||
    metadata?.mra_submission ||
    {};
  const submissionState = getMraSubmissionState(order);
  const submissionMessage = String(mraSubmission?.message || '').trim();
  const hasFiscalData = hasFiscalReceiptPrintData(order);

  if (eisEnabled) {
    if (status === 'REJECTED' || submissionState === 'rejected' || syncStatus === 'failed') {
      return {
        label: submissionState === 'rejected' || status === 'REJECTED' ? 'Receipt Rejected' : 'Receipt Failed',
        description: submissionMessage || syncError || 'The legal receipt was not issued.',
        tone: 'rejected',
      };
    }

    if (submissionState === 'accepted' || status === 'ACCEPTED' || (status === 'SUBMITTED' && hasFiscalData)) {
      return {
        label: status === 'ACCEPTED' ? 'Receipt Accepted' : 'Receipt Submitted',
        description: submissionMessage || 'Legal receipt details are available for this sale.',
        tone: 'accepted',
      };
    }

    if (
      submissionState === 'offline_queued' ||
      String((order as any)?.eisSyncState ?? (order as any)?.eis_sync_state ?? '').trim().toUpperCase() === 'OFFLINE_QUEUED' ||
      mraSubmission?.queued_offline === true ||
      mraSubmission?.queuedOffline === true ||
      status.includes('OFFLINE') ||
      status.includes('QUEUED') ||
      (status === 'PENDING' && hasFiscalData) ||
      !isBrowserOnline
    ) {
      return {
        label: 'Offline Queued',
        description: submissionMessage || (hasFiscalData ? 'Queued for upload.' : 'Will upload later.'),
        tone: 'offline',
      };
    }

    return {
      label: 'Receipt Pending',
      description: 'Submitting receipt.',
      tone: 'pending',
    };
  }

  return isBrowserOnline
    ? {
        label: 'Online Sale',
        description: 'Ready to submit.',
        tone: 'accepted',
      }
    : {
        label: 'Offline Sale',
        description: 'Will sync later.',
        tone: 'offline',
      };
};

const completedSaleSubmissionBadgeClass = (tone: 'accepted' | 'pending' | 'offline' | 'rejected'): string => {
  if (tone === 'accepted') return 'border-emerald-200 bg-emerald-50 text-emerald-700';
  if (tone === 'offline') return 'border-amber-200 bg-amber-50 text-amber-800';
  if (tone === 'rejected') return 'border-red-200 bg-red-50 text-red-700';
  return 'border-sky-200 bg-sky-50 text-sky-700';
};

const normalizeInventoryReference = (value: unknown): string => {
  if (value === undefined || value === null) {
    return '';
  }

  if (typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    const nestedValue =
      obj.id ??
      obj.pk ??
      obj.uuid ??
      obj.inventory_item_id ??
      obj.inventoryItemId;

    return String(nestedValue ?? '').trim();
  }

  return String(value).trim();
};

const resolveMappingInventoryItemId = (mapping: any): string => {
  if (!mapping || typeof mapping !== 'object') {
    return '';
  }

  const candidates = [
    mapping.inventoryItemId,
    mapping.inventory_item_id,
    mapping.inventoryItem,
    mapping.inventory_item,
  ];

  for (const candidate of candidates) {
    const normalized = normalizeInventoryReference(candidate);
    if (normalized) {
      return normalized;
    }
  }

  return '';
};

const resolveCartInventoryItemId = (cartItem: { id?: string; inventoryItemId?: string }): string => {
  const explicitInventoryId = String(cartItem.inventoryItemId || '').trim();
  if (explicitInventoryId) {
    return explicitInventoryId;
  }

  const rawLineId = String(cartItem.id || '').trim();
  if (!rawLineId) {
    return '';
  }

  // Backward compatibility for legacy synthetic line ids like "<inventoryId>::cart::<ts>".
  return rawLineId.split('::cart::')[0] || rawLineId;
};

const mappingReadinessRank = (mapping: any): number => {
  if (!mapping) {
    return -1;
  }

  const approved = Boolean(mapping.isApproved ?? mapping.is_approved);
  const synced = Boolean(mapping.mraSynced ?? mapping.mra_synced);

  if (approved && synced) {
    return 3;
  }
  if (approved) {
    return 2;
  }
  if (synced) {
    return 1;
  }
  return 0;
};

const choosePreferredMapping = (current: any, candidate: any): any => {
  if (!current) {
    return candidate;
  }

  const currentRank = mappingReadinessRank(current);
  const candidateRank = mappingReadinessRank(candidate);
  if (candidateRank > currentRank) {
    return candidate;
  }

  if (candidateRank < currentRank) {
    return current;
  }

  const currentUpdatedAt = new Date(current.updatedAt || current.updated_at || current.lastSyncedAt || current.last_synced_at || current.createdAt || current.created_at || 0).getTime();
  const candidateUpdatedAt = new Date(candidate.updatedAt || candidate.updated_at || candidate.lastSyncedAt || candidate.last_synced_at || candidate.createdAt || candidate.created_at || 0).getTime();

  return candidateUpdatedAt >= currentUpdatedAt ? candidate : current;
};

const buildMappingLookup = (mappings: any[]): Map<string, any> => {
  const lookup = new Map<string, any>();

  for (const mapping of mappings) {
    const key = resolveMappingInventoryItemId(mapping);
    if (!key) {
      continue;
    }

    lookup.set(key, choosePreferredMapping(lookup.get(key), mapping));
  }

  return lookup;
};

const normalizeBranchIdentifier = (value: unknown): string => {
  if (value && typeof value === 'object') {
    const maybeId = (value as any).id ?? (value as any).branch_id ?? (value as any).branchId ?? (value as any).branch;
    if (maybeId !== undefined && maybeId !== value) {
      return normalizeBranchIdentifier(maybeId);
    }
    return '';
  }

  const normalized = String(value ?? '').trim();
  if (!normalized) return '';
  if (normalized === '[object Object]') return '';

  const brnMatch = /^BRN-(\d+)$/i.exec(normalized);
  if (brnMatch) return brnMatch[1];

  const legacyMatch = /^branch-(\d+)$/i.exec(normalized);
  if (legacyMatch) return legacyMatch[1];

  return normalized;
};

type CachedMraPingStatus = {
  enabled: boolean;
  isOnline: boolean | null;
  checkedAt: string;
  serverTime?: string;
  terminalId?: string;
  terminalLabel?: string;
  terminalStatus?: string;
  error?: string;
};

const MRA_PING_STORAGE_PREFIX = 'handypos-mra-ping-status';

const getMraPingStorageKey = (businessId: string, branchId: string): string => (
  `${MRA_PING_STORAGE_PREFIX}:${businessId}:${normalizeBranchIdentifier(branchId)}`
);

const readCachedMraPingStatus = (businessId?: unknown, branchId?: unknown): CachedMraPingStatus | null => {
  if (typeof window === 'undefined') return null;
  const normalizedBusinessId = String(businessId || '').trim();
  const normalizedBranchId = normalizeBranchIdentifier(branchId);
  if (!normalizedBusinessId || !normalizedBranchId) return null;

  try {
    const raw = localStorage.getItem(getMraPingStorageKey(normalizedBusinessId, normalizedBranchId));
    return raw ? (JSON.parse(raw) as CachedMraPingStatus) : null;
  } catch {
    return null;
  }
};

const isCachedMraOffline = (status: CachedMraPingStatus | null): boolean => (
  Boolean(status?.enabled) && status?.isOnline === false
);

const isLikelyNetworkError = (message?: string): boolean => {
  const normalized = String(message || '').toLowerCase();
  return [
    'failed to fetch',
    'network',
    'networkerror',
    'offline',
    'name resolution',
    'failed to resolve',
    'max retries exceeded',
    'connection refused',
    'connection aborted',
    'connection reset',
    'timeout',
    'timed out',
    'temporarily unavailable',
    'could not reach',
  ].some((marker) => normalized.includes(marker));
};

type NormalizedTaxType = 'standard' | 'zero' | 'exempt' | 'unmapped';
type NormalizedTaxCalculationMethod = 'inclusive' | 'exclusive' | 'not_applicable' | 'unmapped';
type MappingStatus = 'ready' | 'pending' | 'unmapped';
type TaxCalculationBasis = 'gross_inclusive' | 'net_exclusive' | 'not_applicable' | 'unmapped';

type ProductTaxMappingDetail = {
  rate: number;
  taxAmount: number;
  netAmount: number;
  grossAmount: number;
  lineAmount: number;
  taxType: NormalizedTaxType;
  taxCalculationMethod: NormalizedTaxCalculationMethod;
  taxCalculationBasis: TaxCalculationBasis;
  taxableAmount: number;
  mappingStatus: MappingStatus;
  mappingId?: string;
  mappingBranchId?: string;
  mappingSource?: 'local' | 'default' | 'none';
};

type CartItemTaxDetail = {
  amount: number;
  rate: number;
  taxType: NormalizedTaxType;
  method: NormalizedTaxCalculationMethod;
  status: MappingStatus;
};

const normalizeMappedTaxType = (value: unknown): NormalizedTaxType => {
  const normalized = String(value || '').trim().toLowerCase();
  if (
    normalized === 'zero' ||
    normalized === 'zero_rated' ||
    normalized === 'zero-rated' ||
    normalized === 'vat_zero'
  ) {
    return 'zero';
  }
  if (normalized === 'exempt' || normalized === 'vat_exempt') {
    return 'exempt';
  }
  return 'standard';
};

const normalizeTaxCalculationMethod = (value: unknown): 'inclusive' | 'exclusive' => {
  const normalized = String(value || '').trim().toLowerCase();
  return normalized.startsWith('excl') ? 'exclusive' : 'inclusive';
};

const resolveMappingTaxMethod = (mapping: any): 'inclusive' | 'exclusive' => {
  if (!mapping) return 'inclusive';
  return normalizeTaxCalculationMethod(
    mapping.taxCalculationMethod ??
      mapping.tax_calculation_method ??
      mapping.calculationMethod ??
      mapping.calculation_method
  );
};

const formatTaxTypeLabel = (taxType: NormalizedTaxType): string => {
  if (taxType === 'zero') return 'Zero Rated';
  if (taxType === 'exempt') return 'Exempt';
  if (taxType === 'standard') return 'Standard';
  return 'Not Mapped';
};

const formatTaxMethodLabel = (method: NormalizedTaxCalculationMethod): string => {
  if (method === 'inclusive') return 'Inclusive';
  if (method === 'exclusive') return 'Exclusive';
  return 'N/A';
};

const formatTaxBasisLabel = (basis: TaxCalculationBasis): string => {
  if (basis === 'gross_inclusive') return 'Gross Price (Tax Included)';
  if (basis === 'net_exclusive') return 'Net Price (Tax Added)';
  if (basis === 'not_applicable') return 'Not Applicable';
  return 'Not Available';
};

const formatMappingStatusLabel = (status: MappingStatus): string => {
  if (status === 'ready') return 'Ready';
  if (status === 'pending') return 'Pending Approval/Sync';
  return 'No Mapping';
};

const formatTaxConditionLabel = (
  taxType: NormalizedTaxType,
  method: NormalizedTaxCalculationMethod,
  status: MappingStatus
): string => {
  if (status !== 'ready') {
    return formatMappingStatusLabel(status);
  }

  if (taxType === 'standard') {
    return `${formatTaxTypeLabel(taxType)} • ${formatTaxMethodLabel(method)}`;
  }

  return `${formatTaxTypeLabel(taxType)} • N/A`;
};

const toFiniteMoneyNumber = (value: unknown, fallback = 0): number => {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const resolveCartLineTotal = (item: CartItem): number => {
  const price = Math.max(0, toFiniteMoneyNumber(item.price, 0));
  const quantity = Math.max(0, toFiniteMoneyNumber(item.quantity, 0));
  return item.isVariablePrice ? price : price * quantity;
};

const ProductCard = ({
  item,
  onAddToCart,
  productIcon,
  currencyFormatter,
  canAddToCart,
  stockInfo,
  stockTone,
}: {
  item: InventoryItem;
  onAddToCart: (item: InventoryItem) => void;
  productIcon: React.ReactNode;
  currencyFormatter: (amount: number) => string;
  canAddToCart: boolean;
  stockInfo: string;
  stockTone: 'available' | 'warning' | 'out';
}) => {
  const price = item.price || 0;
  
  return (
    <Card
      className={cn(
        "flex cursor-pointer flex-col overflow-hidden transition-all hover:shadow-md",
        !canAddToCart && "opacity-50 cursor-not-allowed"
      )}
      role="button"
    >
      <div className="flex h-24 items-center justify-center bg-muted">
        {productIcon}
      </div>
      <CardContent className="flex-1 p-3">
        <p className="font-semibold">{item.name}</p>
        <p className="text-sm text-muted-foreground">{item.category}</p>
        <p className={cn(
          "text-xs mt-1 font-medium",
          stockTone === 'available'
            ? "text-green-600"
            : stockTone === 'warning'
              ? "text-amber-600"
              : "text-red-600"
        )}>
          {stockInfo}
        </p>
      </CardContent>
      <CardFooter className="flex items-center justify-between p-3 pt-0">
        <p className="text-base font-bold text-primary">
          {currencyFormatter(price)}
          {item.isVariablePrice && <span className="text-xs font-normal text-muted-foreground">/{item.unitType}</span>}
        </p>
        {item.isVariablePrice && <Badge variant="outline">By Weight</Badge>}
      </CardFooter>
    </Card>
  );
};

const CartItemView = ({
  item,
  onUpdateQuantity,
  onApplyDiscount,
  currencyFormatter,
  taxDetail,
  showTaxStatus,
  discountRules = [],
}: {
  item: CartItem;
  onUpdateQuantity: (itemId: string, quantity: number) => void;
  onApplyDiscount?: (itemId: string, discount: AppliedDiscount | null) => void;
  currencyFormatter: (amount: number) => string;
  taxDetail?: CartItemTaxDetail;
  showTaxStatus?: boolean;
  discountRules?: DiscountRule[];
}) => {
  const total = resolveCartLineTotal(item);
  const appliedDiscount = getCartDiscount(item);
  const discountAmount = resolveCartDiscountAmount(item);
  const hasInvalidDiscount = isCartDiscountInvalid(item);
  const discountedTotal = Math.max(0, total - discountAmount);
  const isVariable = item.isVariablePrice;
  const hasStockSnapshot = item.stockUnits !== undefined && item.stockUnits !== null;
  const maxStockQuantity = Math.max(0, toFiniteMoneyNumber(item.stockUnits, 0));
  const hasQuantityLimit = !item.isProduced && !isVariable && hasStockSnapshot;
  const hasReachedStockLimit = hasQuantityLimit && item.quantity >= maxStockQuantity - 0.0001;
  const taxRateLabel = taxDetail && Number.isFinite(taxDetail.rate) ? `${taxDetail.rate.toFixed(2)}%` : '0%';
  const taxMethodLabel =
    taxDetail?.method === 'exclusive'
      ? 'EXC'
      : taxDetail?.method === 'inclusive'
        ? 'INC'
        : 'N/A';
  const taxDescriptor = taxDetail
    ? taxDetail.taxType === 'standard'
      ? `VAT ${taxRateLabel}${taxMethodLabel !== 'N/A' ? ` (${taxMethodLabel})` : ''}`
      : taxDetail.taxType === 'unmapped'
        ? 'Tax'
        : `${formatTaxTypeLabel(taxDetail.taxType)} VAT`
    : '';
  const taxStatusLabel =
    taxDetail && showTaxStatus && taxDetail.status !== 'ready'
      ? ` • ${formatMappingStatusLabel(taxDetail.status)}`
      : '';

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-start gap-3 py-3">
      <div className="min-w-0 space-y-1">
        <p className="font-medium truncate">{item.name}</p>
        <p className="text-sm text-muted-foreground break-words">
          {isVariable
            ? `${item.quantity.toFixed(3)} ${item.unitType} @ ${currencyFormatter(item.price / item.quantity)}/${item.unitType}`
            : currencyFormatter(item.price)}
        </p>
      </div>
      {!isVariable && (
        <div className="flex items-center gap-2">
          <Button
            size="icon"
            variant="outline"
            className="h-7 w-7"
            onClick={() => onUpdateQuantity(item.id, item.quantity - 1)}
          >
            <Minus className="h-4 w-4" />
          </Button>
          <span className="w-6 text-center font-bold">{item.quantity}</span>
          <Button
            size="icon"
            variant="outline"
            className="h-7 w-7"
            onClick={() => onUpdateQuantity(item.id, item.quantity + 1)}
            disabled={hasReachedStockLimit}
            title={hasReachedStockLimit ? `${formatInventoryQuantity(maxStockQuantity, { preferWholeNumbers: true })} available` : 'Increase quantity'}
          >
            <Plus className="h-4 w-4" />
          </Button>
        </div>
      )}
      <div className="flex w-32 flex-col items-end gap-1 text-right">
        <div className="flex items-center gap-2">
          <p className="font-semibold">{currencyFormatter(discountedTotal)}</p>
          <Button
            size="icon"
            variant="ghost"
            className="h-7 w-7 text-muted-foreground hover:text-destructive"
            onClick={() => onUpdateQuantity(item.id, 0)}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
        {showTaxStatus && taxDetail && (
          <p className="text-xs text-muted-foreground">
            {taxDescriptor}
            {taxStatusLabel}: <span className="font-medium text-foreground">{currencyFormatter(taxDetail.amount)}</span>
          </p>
        )}
        {discountAmount > 0 && (
          <p className={cn(
            'text-[11px]',
            hasInvalidDiscount ? 'text-destructive' : 'text-green-700 dark:text-green-500'
          )}>
            Discount: -{currencyFormatter(discountAmount)}
          </p>
        )}
        {hasInvalidDiscount && (
          <p className="text-[11px] font-medium text-destructive">
            Discount must be less than item total.
          </p>
        )}
      </div>
      {onApplyDiscount && discountRules.length > 0 && (
        <div className="col-span-3">
          <select
            value={appliedDiscount?.ruleId || ''}
            onChange={(event) => {
              const selected = discountRules.find((rule) => rule.id === event.target.value);
              if (!selected) {
                onApplyDiscount(item.id, null);
                return;
              }
              const type = (selected.discount_type ?? selected.discountType) === 'fixed' ? 'fixed' : 'percentage';
              onApplyDiscount(item.id, {
                ruleId: selected.id,
                name: selected.name,
                type,
                value: toFiniteNumber(selected.value, 0),
              });
            }}
            className="h-8 w-full rounded-md border bg-background px-2 text-xs"
          >
            <option value="">No discount</option>
            {discountRules.map((rule) => {
              const type = (rule.discount_type ?? rule.discountType) === 'fixed' ? 'fixed' : 'percentage';
              const value = toFiniteNumber(rule.value, 0);
              const label = type === 'percentage' ? `${value}%` : currencyFormatter(value);
              return (
                <option key={rule.id} value={rule.id}>
                  {rule.name} ({label})
                </option>
              );
            })}
          </select>
        </div>
      )}
    </div>
  );
};

const PaymentDialog = ({
    subtotal,
    tax,
    taxLabel,
    defaultTaxRate,
    onCheckout,
    onClose,
    currencyFormatter,
    resetToken,
    cart,
    eisEnabled,
    blockSalesIfTaxMappingMissing,
    isEisInvoiceSubmissionBlocked = false,
    eisInvoiceSubmissionBlockedMessage = 'POS server unreachable.',
    branchId,
    onConfigurePrinter,
}: {
    subtotal: number;
    tax: number;
    taxLabel: string;
    onCheckout: (paymentMethod: PaymentMethod, tip: number, buyerDetails?: BuyerDetails) => Promise<Order | null>;
    onClose: () => void;
    currencyFormatter: (amount: number) => string;
    resetToken: number;
    cart?: CartItem[];
    eisEnabled?: boolean;
    blockSalesIfTaxMappingMissing?: boolean;
    isEisInvoiceSubmissionBlocked?: boolean;
    eisInvoiceSubmissionBlockedMessage?: string;
    branchId?: string;
    onConfigurePrinter: () => void;
    defaultTaxRate?: TaxRate | null;
}) => {
    const { toast } = useToast();
    const [step, setStep] = useState<'payment' | 'confirmation'>('payment');
    const [completedOrder, setCompletedOrder] = useState<Order | null>(null);
    const [selectedPaymentMethod, setSelectedPaymentMethod] = useState<PaymentMethod | null>(null);
    const [cashPaid, setCashPaid] = useState<number | string>('');
    const [buyerName, setBuyerName] = useState('');
    const [buyerPhone, setBuyerPhone] = useState('');
    const [buyerTin, setBuyerTin] = useState('');
    const [buyerAuthorizationCode, setBuyerAuthorizationCode] = useState('');
    const [isExportSale, setIsExportSale] = useState(false);
    const [isReliefSupply, setIsReliefSupply] = useState(false);
    const [vat5ProjectNumber, setVat5ProjectNumber] = useState('');
    const [vat5CertificateNumber, setVat5CertificateNumber] = useState('');
    const [vat5Quantity, setVat5Quantity] = useState<number | string>('');
    const [calculatedTax, setCalculatedTax] = useState(tax);
    const [calculatedNetAmount, setCalculatedNetAmount] = useState(subtotal);
    const [calculatedGrossAmount, setCalculatedGrossAmount] = useState(subtotal + tax);
    const [calculatedTaxLabel, setCalculatedTaxLabel] = useState(taxLabel);
    const [productTaxMappings, setProductTaxMappings] = useState<Record<string, ProductTaxMappingDetail>>({});
    const [unmappedProducts, setUnmappedProducts] = useState<string[]>([]);
    const [isProcessingPayment, setIsProcessingPayment] = useState(false);
    const [mraPingStatus, setMraPingStatus] = useState<CachedMraPingStatus | null>(null);
    const [isBrowserOnline, setIsBrowserOnline] = useState(
        () => (typeof navigator === 'undefined' ? true : navigator.onLine !== false)
    );
    const shouldUseEisTaxMappings = Boolean(eisEnabled);
    const shouldEnforceTaxMapping = shouldUseEisTaxMappings && blockSalesIfTaxMappingMissing === true;
    const defaultTaxRateDecimal = defaultTaxRate ? defaultTaxRate.rate / 100 : 0;
    const activeBranchId = useMemo(
        () => branchId ?? safeLocalStorageGetItem('handypos-active-branch') ?? 'main',
        [branchId]
    );
    const normalizedActiveBranchId = useMemo(
        () => normalizeBranchIdentifier(activeBranchId),
        [activeBranchId]
    );
    const mappingRefreshAttemptedRef = useRef(false);
    const mappingItemFetchAttemptedRef = useRef(false);
    const businessIdForMraStatus = useMemo(() => resolveOfflineBusinessId(), []);

    useEffect(() => {
        if (typeof window === 'undefined') return;

        const updateBrowserOnline = () => {
            setIsBrowserOnline(typeof navigator === 'undefined' ? true : navigator.onLine !== false);
        };

        updateBrowserOnline();
        window.addEventListener('online', updateBrowserOnline);
        window.addEventListener('offline', updateBrowserOnline);

        return () => {
            window.removeEventListener('online', updateBrowserOnline);
            window.removeEventListener('offline', updateBrowserOnline);
        };
    }, []);

    useEffect(() => {
        if (!shouldUseEisTaxMappings || !businessIdForMraStatus || !normalizedActiveBranchId) {
            setMraPingStatus(null);
            return;
        }

        const refreshCachedStatus = () => {
            setMraPingStatus(readCachedMraPingStatus(businessIdForMraStatus, normalizedActiveBranchId));
        };

        refreshCachedStatus();
        window.addEventListener('focus', refreshCachedStatus);
        window.addEventListener('storage', refreshCachedStatus);
        window.addEventListener('handypos-sync-status-changed', refreshCachedStatus);

        return () => {
            window.removeEventListener('focus', refreshCachedStatus);
            window.removeEventListener('storage', refreshCachedStatus);
            window.removeEventListener('handypos-sync-status-changed', refreshCachedStatus);
        };
    }, [businessIdForMraStatus, normalizedActiveBranchId, shouldUseEisTaxMappings]);

    const isMraOffline = shouldUseEisTaxMappings && isCachedMraOffline(mraPingStatus);
    const isMraOnline = shouldUseEisTaxMappings && mraPingStatus?.enabled === true && mraPingStatus?.isOnline === true;
    const connectivityTone: 'online' | 'offline' | 'blocked' | 'unknown' = isEisInvoiceSubmissionBlocked
        ? 'blocked'
        : shouldUseEisTaxMappings
            ? isMraOffline
                ? 'offline'
                : isMraOnline
                    ? 'online'
                    : 'unknown'
            : isBrowserOnline
                ? 'online'
                : 'offline';
    const saleConnectivityLabel = isEisInvoiceSubmissionBlocked
        ? 'POS Server Unavailable'
        : shouldUseEisTaxMappings
            ? isMraOffline
                ? 'MRA Offline'
                : isMraOnline
                    ? 'MRA Online'
                    : 'MRA Status Unknown'
            : isBrowserOnline
                ? 'Online'
                : 'Offline';
    const saleConnectivityDescription = isEisInvoiceSubmissionBlocked
        ? eisInvoiceSubmissionBlockedMessage
        : shouldUseEisTaxMappings
            ? isMraOffline
                ? 'MRA EIS unavailable; signs offline and queues for upload.'
                : isMraOnline
                    ? 'MRA EIS reachable; submits receipt.'
                    : 'MRA EIS status not checked.'
            : isBrowserOnline
                ? 'Submits to POS server.'
                : 'Will sync later.';
    const taxMethodSummary = useMemo(() => {
        const methods = new Set<'inclusive' | 'exclusive'>();
        Object.values(productTaxMappings).forEach((mapping) => {
            if (mapping.taxCalculationMethod === 'inclusive' || mapping.taxCalculationMethod === 'exclusive') {
                methods.add(mapping.taxCalculationMethod);
            }
        });

        if (methods.size === 1) {
            return methods.has('exclusive') ? 'Exclusive' : 'Inclusive';
        }
        if (methods.size > 1) {
            return 'Mixed';
        }
        if (!shouldUseEisTaxMappings && defaultTaxRateDecimal > 0) {
            return 'Default (Inclusive)';
        }
        return 'N/A';
    }, [productTaxMappings, shouldUseEisTaxMappings, defaultTaxRateDecimal]);
    useEffect(() => {
        setStep('payment');
        setCompletedOrder(null);
        setSelectedPaymentMethod(null);
        setCashPaid('');
        setBuyerName('');
        setBuyerPhone('');
        setBuyerTin('');
        setCalculatedTax(tax);
        setCalculatedNetAmount(subtotal);
        setCalculatedGrossAmount(subtotal + tax);
        setCalculatedTaxLabel(taxLabel);
        setProductTaxMappings({});
        setUnmappedProducts([]);
        setIsProcessingPayment(false);
        mappingRefreshAttemptedRef.current = false;
        mappingItemFetchAttemptedRef.current = false;
    }, [resetToken, subtotal, tax, taxLabel]);

    useEffect(() => {
        let cancelled = false;

        const calculateCorrectTax = async () => {
            const effectiveTaxLabel = shouldUseEisTaxMappings
                ? 'VAT Amount'
                : taxLabel;

            if (!cart || cart.length === 0) {

                if (!cancelled) {
                    setCalculatedTax(tax);
                    setCalculatedNetAmount(subtotal);
                    setCalculatedGrossAmount(subtotal + tax);
                    setCalculatedTaxLabel(effectiveTaxLabel);
                    setProductTaxMappings({});
                    setUnmappedProducts([]);
                }
                return;
            }

            try {
                const shouldScopeByBranch =
                    Boolean(normalizedActiveBranchId) &&
                    !['main', 'main-branch', 'main_branch'].includes(normalizedActiveBranchId.toLowerCase());
                const localMappings = await db.mraMappings.toArray();
                const scopedMappings = localMappings.filter((mapping) => {
                    const mappingBranchId = normalizeBranchIdentifier(
                        (mapping as any).branchId ??
                        (mapping as any).branch_id ??
                        (mapping as any).branch
                    );

                    // Backward compatibility: keep unscoped local mappings,
                    // but prefer branch-scoped mappings when metadata exists.
                    if (!mappingBranchId) {
                        return true;
                    }
                    if (!shouldScopeByBranch) {
                        return true;
                    }
                    return mappingBranchId === normalizedActiveBranchId;
                });

                let mappingByItemId = buildMappingLookup(scopedMappings);
                const missingMappingKeys: string[] = [];

                for (const cartItem of cart) {
                    const primaryInventoryItemId = String(cartItem.inventoryItemId || '').trim();
                    const fallbackInventoryItemId = resolveCartInventoryItemId(cartItem) || String(cartItem.id || '').trim();
                    const preferredMappingKey = primaryInventoryItemId || fallbackInventoryItemId;
                    let localMapping = mappingByItemId.get(preferredMappingKey);
                    if (!localMapping && fallbackInventoryItemId && fallbackInventoryItemId !== preferredMappingKey) {
                        localMapping = mappingByItemId.get(fallbackInventoryItemId);
                    }
                    if (!localMapping && preferredMappingKey) {
                        missingMappingKeys.push(preferredMappingKey);
                    }
                }

                if (
                    missingMappingKeys.length > 0 &&
                    !mappingRefreshAttemptedRef.current &&
                    isBrowserOnline &&
                    branchId
                ) {
                    mappingRefreshAttemptedRef.current = true;
                    try {
                        const backendBranchId = normalizeBranchIdentifier(branchId);
                        if (backendBranchId) {
                            const mappingsResponse = await authFetch.fetch<any>(
                                `/inventory/mra-mappings/?branch_id=${encodeURIComponent(backendBranchId)}`
                            );
                            const refreshedMappings = Array.isArray(mappingsResponse)
                                ? mappingsResponse
                                : Array.isArray(mappingsResponse?.results)
                                    ? mappingsResponse.results
                                    : [];
                            const nowIso = new Date().toISOString();

                            for (const rawMapping of refreshedMappings) {
                                const mappingItemId = resolveMappingInventoryItemId(rawMapping);
                                if (!mappingItemId) {
                                    continue;
                                }

                                const rawTaxType = rawMapping.mra_tax_type ?? rawMapping.mraTaxType;
                                const taxType =
                                    rawTaxType === 'zero' || rawTaxType === 'exempt'
                                        ? rawTaxType
                                        : 'standard';
                                const calculationMethod = resolveMappingTaxMethod(rawMapping);

                                await db.mraMappings.put({
                                    id: String(rawMapping.id || `${mappingItemId}-mapping`),
                                    inventoryItemId: mappingItemId,
                                    branchId: normalizeBranchIdentifier(
                                        rawMapping.branch ??
                                        rawMapping.branch_id ??
                                        backendBranchId
                                    ) || undefined,
                                    mraProductCode: rawMapping.mra_product_code || rawMapping.mraProductCode || '',
                                    mraProductName: rawMapping.mra_product_name || rawMapping.mraProductName || '',
                                    mraTaxType: taxType,
                                    mraTaxRate: Number(rawMapping.mra_tax_rate ?? rawMapping.mraTaxRate ?? 0),
                                    mraUnitMeasure: rawMapping.mra_unit_measure || rawMapping.mraUnitMeasure || '',
                                    taxCalculationMethod: calculationMethod,
                                    isApproved: Boolean(rawMapping.is_approved ?? rawMapping.isApproved),
                                    approvedAt: rawMapping.approved_at || rawMapping.approvedAt || undefined,
                                    mraSynced: Boolean(rawMapping.mra_synced ?? rawMapping.mraSynced),
                                    lastSyncedAt: rawMapping.last_synced_at || rawMapping.lastSyncedAt || undefined,
                                    createdAt: rawMapping.created_at || rawMapping.createdAt || nowIso,
                                    updatedAt: nowIso,
                                    _dirty: false,
                                    _synced_at: nowIso,
                                });
                            }

                            const refreshedLocalMappings = await db.mraMappings.toArray();
                            const refreshedScopedMappings = refreshedLocalMappings.filter((mapping) => {
                                const mappingBranchId = normalizeBranchIdentifier(
                                    (mapping as any).branchId ??
                                    (mapping as any).branch_id ??
                                    (mapping as any).branch
                                );

                                if (!mappingBranchId) {
                                    return true;
                                }
                                if (!shouldScopeByBranch) {
                                    return true;
                                }
                                return mappingBranchId === normalizedActiveBranchId;
                            });
                            mappingByItemId = buildMappingLookup(refreshedScopedMappings);
                        }
                    } catch (refreshError) {
                        console.warn('[PaymentDialog] Failed to refresh MRA mappings:', refreshError);
                    }
                }

                if (
                    missingMappingKeys.length > 0 &&
                    !mappingItemFetchAttemptedRef.current &&
                    isBrowserOnline &&
                    branchId
                ) {
                    mappingItemFetchAttemptedRef.current = true;
                    try {
                        const backendBranchId = normalizeBranchIdentifier(branchId);
                        if (backendBranchId) {
                            const unresolvedKeys: string[] = [];
                            for (const cartItem of cart) {
                                const primaryInventoryItemId = String(cartItem.inventoryItemId || '').trim();
                                const fallbackInventoryItemId = resolveCartInventoryItemId(cartItem) || String(cartItem.id || '').trim();
                                const preferredMappingKey = primaryInventoryItemId || fallbackInventoryItemId;
                                let localMapping = mappingByItemId.get(preferredMappingKey);
                                if (!localMapping && fallbackInventoryItemId && fallbackInventoryItemId !== preferredMappingKey) {
                                    localMapping = mappingByItemId.get(fallbackInventoryItemId);
                                }
                                if (!localMapping && preferredMappingKey) {
                                    unresolvedKeys.push(preferredMappingKey);
                                }
                            }

                            for (const inventoryItemId of unresolvedKeys) {
                                try {
                                    const response = await authFetch.fetch<any>(
                                        `/inventory/mra-mappings/?inventory_item=${encodeURIComponent(inventoryItemId)}&branch_id=${encodeURIComponent(backendBranchId)}`
                                    );
                                    const mappings = Array.isArray(response)
                                        ? response
                                        : Array.isArray(response?.results)
                                            ? response.results
                                            : [];
                                    if (!mappings.length) {
                                        continue;
                                    }

                                    const readyMapping =
                                        mappings.find((m: any) => Boolean(m.is_approved ?? m.isApproved) && Boolean(m.mra_synced ?? m.mraSynced)) ||
                                        mappings[0];

                                    const rawTaxType = readyMapping.mra_tax_type ?? readyMapping.mraTaxType;
                                    const taxType =
                                        rawTaxType === 'zero' || rawTaxType === 'exempt'
                                            ? rawTaxType
                                            : 'standard';
                                    const calculationMethod = resolveMappingTaxMethod(readyMapping);
                                    const nowIso = new Date().toISOString();

                                    await db.mraMappings.put({
                                        id: String(readyMapping.id || `${inventoryItemId}-mapping`),
                                        inventoryItemId,
                                        branchId: normalizeBranchIdentifier(
                                            readyMapping.branch ??
                                            readyMapping.branch_id ??
                                            backendBranchId
                                        ) || undefined,
                                        mraProductCode: readyMapping.mra_product_code || readyMapping.mraProductCode || '',
                                        mraProductName: readyMapping.mra_product_name || readyMapping.mraProductName || '',
                                        mraTaxType: taxType,
                                        mraTaxRate: Number(readyMapping.mra_tax_rate ?? readyMapping.mraTaxRate ?? 0),
                                        mraUnitMeasure: readyMapping.mra_unit_measure || readyMapping.mraUnitMeasure || '',
                                        taxCalculationMethod: calculationMethod,
                                        isApproved: Boolean(readyMapping.is_approved ?? readyMapping.isApproved),
                                        approvedAt: readyMapping.approved_at || readyMapping.approvedAt || undefined,
                                        mraSynced: Boolean(readyMapping.mra_synced ?? readyMapping.mraSynced),
                                        lastSyncedAt: readyMapping.last_synced_at || readyMapping.lastSyncedAt || undefined,
                                        createdAt: readyMapping.created_at || readyMapping.createdAt || nowIso,
                                        updatedAt: nowIso,
                                        _dirty: false,
                                        _synced_at: nowIso,
                                    });
                                } catch (itemError) {
                                    console.warn('[PaymentDialog] Failed to fetch mapping for item:', inventoryItemId, itemError);
                                }
                            }

                            const refreshedLocalMappings = await db.mraMappings.toArray();
                            const refreshedScopedMappings = refreshedLocalMappings.filter((mapping) => {
                                const mappingBranchId = normalizeBranchIdentifier(
                                    (mapping as any).branchId ??
                                    (mapping as any).branch_id ??
                                    (mapping as any).branch
                                );

                                if (!mappingBranchId) {
                                    return true;
                                }
                                if (!shouldScopeByBranch) {
                                    return true;
                                }
                                return mappingBranchId === normalizedActiveBranchId;
                            });
                            mappingByItemId = buildMappingLookup(refreshedScopedMappings);
                        }
                    } catch (refreshError) {
                        console.warn('[PaymentDialog] Failed to fetch per-item MRA mappings:', refreshError);
                    }
                }

                const perItemFetchedMappings = new Map<string, any>();
                let totalTax = 0;
                let totalNet = 0;
                let totalGross = 0;
                const mappings: Record<string, ProductTaxMappingDetail> = {};
                const unmapped: string[] = [];
                
                for (const cartItem of cart) {
                    const itemId = String(cartItem.id);
                    const primaryInventoryItemId = String(cartItem.inventoryItemId || '').trim();
                    const fallbackInventoryItemId = resolveCartInventoryItemId(cartItem) || String(cartItem.id || '').trim();

                    // Prefer the canonical inventory item id from cart metadata,
                    // then fall back to cart line id for legacy entries.
                    const preferredMappingKey = primaryInventoryItemId || fallbackInventoryItemId;
                    let localMapping = mappingByItemId.get(preferredMappingKey);
                    if (!localMapping && fallbackInventoryItemId && fallbackInventoryItemId !== preferredMappingKey) {
                        localMapping = mappingByItemId.get(fallbackInventoryItemId);
                    }
                    if (!localMapping && preferredMappingKey && perItemFetchedMappings.has(preferredMappingKey)) {
                        localMapping = perItemFetchedMappings.get(preferredMappingKey);
                    }
                    if (!localMapping && preferredMappingKey && isBrowserOnline && branchId) {
                        try {
                            const backendBranchId = normalizeBranchIdentifier(branchId);
                            const response = await authFetch.fetch<any>(
                                `/inventory/mra-mappings/?inventory_item=${encodeURIComponent(preferredMappingKey)}&branch_id=${encodeURIComponent(backendBranchId)}`
                            );
                            const fetchedMappings = Array.isArray(response)
                                ? response
                                : Array.isArray(response?.results)
                                    ? response.results
                                    : [];
                            if (fetchedMappings.length) {
                                const readyMapping =
                                    fetchedMappings.find((m: any) => Boolean(m.is_approved ?? m.isApproved) && Boolean(m.mra_synced ?? m.mraSynced)) ||
                                    fetchedMappings[0];
                                perItemFetchedMappings.set(preferredMappingKey, readyMapping);
                                localMapping = readyMapping;
                            }
                        } catch (fetchError) {
                            console.warn('[PaymentDialog] Failed to fetch mapping for cart item:', preferredMappingKey, fetchError);
                        }
                    }
                    const lineAmountBeforeDiscount = resolveCartLineTotal(cartItem);
                    const discountAmount = resolveCartDiscountAmount(cartItem);
                    const lineAmount = Math.max(0, lineAmountBeforeDiscount - discountAmount);
                    let itemTax = 0;
                    let itemNet = lineAmount;
                    let itemGross = lineAmount;
                    let taxRate = 0;
                    let taxCalculationBasis: TaxCalculationBasis = 'not_applicable';
                    const isApproved = Boolean(localMapping?.isApproved ?? localMapping?.is_approved);
                    const isSynced = Boolean(localMapping?.mraSynced ?? localMapping?.mra_synced);
                    
                    if (localMapping && isApproved && isSynced) {
                        const taxType = normalizeMappedTaxType(localMapping.mraTaxType || localMapping.mra_tax_type);
                        taxRate = Number(localMapping.mraTaxRate ?? localMapping.mra_tax_rate ?? 0);
                        const normalizedRate = Number.isFinite(taxRate) ? taxRate : 0;
                        let taxCalculationMethod: NormalizedTaxCalculationMethod = 'not_applicable';

                        if (taxType === 'zero' || taxType === 'exempt') {
                            itemTax = 0;
                            itemNet = lineAmount;
                            itemGross = lineAmount;
                            taxCalculationBasis = 'not_applicable';

                        } else {
                            taxCalculationMethod = resolveMappingTaxMethod(localMapping);
                            const effectiveTaxRate = normalizedRate / 100;
                            
                            if (taxCalculationMethod === 'exclusive') {
                                itemTax = lineAmount * effectiveTaxRate;
                                itemNet = lineAmount;
                                itemGross = lineAmount + itemTax;
                                taxCalculationBasis = 'net_exclusive';

                            } else {
                                itemTax = effectiveTaxRate > 0
                                    ? lineAmount * effectiveTaxRate / (1 + effectiveTaxRate)
                                    : 0;
                                itemGross = lineAmount;
                                itemNet = lineAmount - itemTax;
                                taxCalculationBasis = 'gross_inclusive';

                            }
                        }
                        
                        totalTax += itemTax;
                        totalNet += itemNet;
                        totalGross += itemGross;
                        mappings[itemId] = {
                            rate: normalizedRate,
                            taxAmount: itemTax,
                            netAmount: itemNet,
                            grossAmount: itemGross,
                            lineAmount,
                            taxType,
                            taxCalculationMethod,
                            taxCalculationBasis,
                            taxableAmount: itemNet,
                            mappingStatus: 'ready',
                            mappingId: localMapping?.id ? String(localMapping.id) : undefined,
                            mappingBranchId: String(
                                localMapping?.branchId ??
                                localMapping?.branch_id ??
                                localMapping?.branch ??
                                ''
                            ).trim() || undefined,
                            mappingSource: 'local',
                        };
                    } else {
                        const hasLocalMapping = Boolean(localMapping);
                        const mappingStatus: MappingStatus = hasLocalMapping ? 'pending' : 'unmapped';
                        const reasonSuffix = hasLocalMapping ? ' (mapping pending approval/sync)' : '';

                        if (shouldEnforceTaxMapping) {
                            unmapped.push(`${cartItem.name}${reasonSuffix}`);
                        }
                        const fallbackTaxType = hasLocalMapping
                            ? normalizeMappedTaxType(localMapping.mraTaxType || localMapping.mra_tax_type)
                            : 'standard';
                        const fallbackRate = hasLocalMapping
                            ? Number(localMapping.mraTaxRate ?? localMapping.mra_tax_rate ?? 0)
                            : 0;
                        const normalizedFallbackRate = Number.isFinite(fallbackRate) ? fallbackRate : 0;
                        const fallbackMethod = hasLocalMapping
                            ? resolveMappingTaxMethod(localMapping)
                            : 'inclusive';

                        if (!shouldUseEisTaxMappings && hasLocalMapping && (fallbackTaxType === 'zero' || fallbackTaxType === 'exempt')) {
                            itemTax = 0;
                            itemNet = lineAmount;
                            itemGross = lineAmount;
                            taxCalculationBasis = 'not_applicable';
                            totalNet += itemNet;
                            totalGross += itemGross;
                            mappings[itemId] = {
                                rate: normalizedFallbackRate,
                                taxAmount: 0,
                                netAmount: itemNet,
                                grossAmount: itemGross,
                                lineAmount,
                                taxType: fallbackTaxType,
                                taxCalculationMethod: 'not_applicable',
                                taxCalculationBasis,
                                taxableAmount: itemNet,
                                mappingStatus,
                                mappingId: localMapping?.id ? String(localMapping.id) : undefined,
                                mappingBranchId: String(
                                    localMapping?.branchId ??
                                    localMapping?.branch_id ??
                                    localMapping?.branch ??
                                    ''
                                ).trim() || undefined,
                                mappingSource: 'local',
                            };
                        } else if (!shouldUseEisTaxMappings && hasLocalMapping && normalizedFallbackRate > 0) {
                            const effectiveTaxRate = normalizedFallbackRate / 100;
                            if (fallbackMethod === 'exclusive') {
                                itemTax = lineAmount * effectiveTaxRate;
                                itemNet = lineAmount;
                                itemGross = lineAmount + itemTax;
                                taxCalculationBasis = 'net_exclusive';
                            } else {
                                itemTax = effectiveTaxRate > 0
                                    ? lineAmount * effectiveTaxRate / (1 + effectiveTaxRate)
                                    : 0;
                                itemGross = lineAmount;
                                itemNet = lineAmount - itemTax;
                                taxCalculationBasis = 'gross_inclusive';
                            }
                            totalTax += itemTax;
                            totalNet += itemNet;
                            totalGross += itemGross;
                            mappings[itemId] = {
                                rate: normalizedFallbackRate,
                                taxAmount: itemTax,
                                netAmount: itemNet,
                                grossAmount: itemGross,
                                lineAmount,
                                taxType: fallbackTaxType,
                                taxCalculationMethod: fallbackMethod,
                                taxCalculationBasis,
                                taxableAmount: itemNet,
                                mappingStatus,
                                mappingId: localMapping?.id ? String(localMapping.id) : undefined,
                                mappingBranchId: String(
                                    localMapping?.branchId ??
                                    localMapping?.branch_id ??
                                    localMapping?.branch ??
                                    ''
                                ).trim() || undefined,
                                mappingSource: 'local',
                            };
                        } else if (!shouldUseEisTaxMappings && defaultTaxRateDecimal > 0) {
                            itemTax = lineAmount * defaultTaxRateDecimal / (1 + defaultTaxRateDecimal);
                            itemGross = lineAmount;
                            itemNet = lineAmount - itemTax;
                            taxCalculationBasis = 'gross_inclusive';
                            totalTax += itemTax;
                            totalNet += itemNet;
                            totalGross += itemGross;
                            mappings[itemId] = {
                                rate: defaultTaxRateDecimal * 100,
                                taxAmount: itemTax,
                                netAmount: itemNet,
                                grossAmount: itemGross,
                                lineAmount,
                                taxType: 'standard',
                                taxCalculationMethod: 'inclusive',
                                taxCalculationBasis,
                                taxableAmount: itemNet,
                                mappingStatus,
                                mappingSource: 'default',
                            };
                        } else {
                            totalNet += lineAmount;
                            totalGross += lineAmount;
                            mappings[itemId] = {
                                rate: 0, 
                                taxAmount: 0,
                                netAmount: lineAmount,
                                grossAmount: lineAmount,
                                lineAmount,
                                taxType: 'unmapped',
                                taxCalculationMethod: 'unmapped',
                                taxCalculationBasis: 'unmapped',
                                taxableAmount: lineAmount,
                                mappingStatus,
                                mappingSource: 'none',
                            };
                        }
                    }
                }

                if (!cancelled) {
                    setCalculatedTax(totalTax);
                    setCalculatedNetAmount(totalNet);
                    setCalculatedGrossAmount(totalGross);
                    setCalculatedTaxLabel(effectiveTaxLabel);
                    setProductTaxMappings(mappings);
                    setUnmappedProducts(shouldEnforceTaxMapping ? unmapped : []);
                }
            } catch (error) {
                console.error('[PaymentDialog] Error calculating tax:', error);
                if (!cancelled) {
                    setCalculatedTax(tax);
                    setCalculatedNetAmount(subtotal);
                    setCalculatedGrossAmount(subtotal + tax);
                    setCalculatedTaxLabel(effectiveTaxLabel);
                    setProductTaxMappings({});
                    setUnmappedProducts([]);
                }
            }
        };

        calculateCorrectTax();

        return () => {
            cancelled = true;
        };
    }, [cart, subtotal, tax, taxLabel, shouldUseEisTaxMappings, shouldEnforceTaxMapping, defaultTaxRateDecimal, branchId, normalizedActiveBranchId, isBrowserOnline]);

    const total = calculatedGrossAmount;
    const hasBlockingUnmapped = shouldEnforceTaxMapping && unmappedProducts.length > 0;
    const businessSettings = useLiveQuery(async () => getOfflineBusinessProfile(), []);
    const change = typeof cashPaid === 'number' && cashPaid > 0 ? cashPaid - total : 0;
    const receiptStyleTaxBreakdown = useMemo(() => {
        const breakdown = new Map<string, {
            rate: number;
            method: 'inclusive' | 'exclusive' | 'not_applicable';
            taxableValue: number;
            vatAmount: number;
            count: number;
        }>();

        for (const item of cart || []) {
            const mapping = productTaxMappings[String(item.id)];
            if (!mapping || mapping.taxCalculationMethod === 'unmapped') {
                continue;
            }

            const rate = Number.isFinite(mapping.rate) ? mapping.rate : 0;
            const method: 'inclusive' | 'exclusive' | 'not_applicable' =
                mapping.taxCalculationMethod === 'exclusive'
                    ? 'exclusive'
                    : mapping.taxCalculationMethod === 'inclusive'
                        ? 'inclusive'
                        : 'not_applicable';
            const key = `${rate}-${method}`;
            const taxableValue = Number.isFinite(mapping.taxableAmount)
                ? mapping.taxableAmount
                : mapping.netAmount;

            const existing = breakdown.get(key);
            if (existing) {
                existing.taxableValue += taxableValue;
                existing.vatAmount += mapping.taxAmount;
                existing.count += 1;
                continue;
            }

            breakdown.set(key, {
                rate,
                method,
                taxableValue,
                vatAmount: mapping.taxAmount,
                count: 1,
            });
        }

        return Array.from(breakdown.values()).sort((a, b) => b.rate - a.rate);
    }, [cart, productTaxMappings]);

    const taxMethodRateSummary = useMemo(() => {
        if (receiptStyleTaxBreakdown.length === 0) return '';
        return receiptStyleTaxBreakdown
            .map((tax) => {
                const methodShortLabel =
                    tax.method === 'exclusive'
                        ? 'EXC'
                        : tax.method === 'inclusive'
                            ? 'INC'
                            : 'N/A';
                const displayTaxRate = (Number.isFinite(tax.rate) ? tax.rate : 0).toFixed(2);
                const countLabel = tax.count > 1 ? ` (x${tax.count})` : '';
                return `${methodShortLabel} ${displayTaxRate}%${countLabel}`;
            })
            .join(' · ');
    }, [receiptStyleTaxBreakdown]);

    const validateEisSaleFieldsBeforeCheckout = async (buyerDetails: BuyerDetails | undefined): Promise<boolean> => {
        const tin = String(buyerDetails?.tin || '').trim();
        const authorizationCode = String(buyerDetails?.authorizationCode || '').trim();
        const isRelief = buyerDetails?.isReliefSupply === true;
        const vat5ProjectNumber = String(buyerDetails?.vat5ProjectNumber || '').trim();
        const vat5CertificateNumber = String(buyerDetails?.vat5CertificateNumber || '').trim();
        const vat5Quantity = Number(buyerDetails?.vat5Quantity);
        if (!shouldUseEisTaxMappings || (!tin && !authorizationCode && !isRelief)) {
            return true;
        }

        const businessId = resolveOfflineBusinessId();
        if (!businessId) {
            toast({
                variant: 'destructive',
                title: 'Cannot validate sale details',
            });
            return false;
        }

        if (isCachedMraOffline(mraPingStatus)) {
            toast({
                variant: 'destructive',
                title: isRelief ? 'Relief sale needs MRA online' : 'B2B needs MRA online',
            });
            return false;
        }

        if (isRelief && (!vat5ProjectNumber || !vat5CertificateNumber || !Number.isFinite(vat5Quantity) || vat5Quantity <= 0)) {
            toast({
                variant: 'destructive',
                title: 'VAT5 details required',
            });
            return false;
        }

        try {
            if (tin) {
                const tinResult = await authFetch.fetch<any>(
                    `/mra-eis/utilities/check-tin-authorization/?business_id=${encodeURIComponent(businessId)}`,
                    {
                        method: 'POST',
                        body: JSON.stringify({ tin }),
                    }
                );

                if (tinResult?.tin_exists === false) {
                    toast({
                        variant: 'destructive',
                        title: 'Buyer TIN not found',
                    });
                    return false;
                }

                if (tinResult?.requires_authorization_code && !authorizationCode) {
                    toast({
                        variant: 'destructive',
                        title: 'Authorization code required',
                    });
                    return false;
                }
            }

            if (authorizationCode) {
                const authResult = await authFetch.fetch<any>(
                    `/mra-eis/utilities/validate-authorization-code/?business_id=${encodeURIComponent(businessId)}`,
                    {
                        method: 'POST',
                        body: JSON.stringify({ authorizationCode }),
                    }
                );

                if (authResult?.checked && authResult?.is_valid === false) {
                    toast({
                        variant: 'destructive',
                        title: 'Invalid authorization code',
                    });
                    return false;
                }
            }

            if (isRelief) {
                const vat5Result = await authFetch.fetch<any>(
                    `/mra-eis/utilities/validate-vat5/?business_id=${encodeURIComponent(businessId)}`,
                    {
                        method: 'POST',
                        body: JSON.stringify({
                            projectNumber: vat5ProjectNumber,
                            certificateNumber: vat5CertificateNumber,
                            quantity: vat5Quantity,
                        }),
                    }
                );

                if (vat5Result?.checked && vat5Result?.is_valid === false) {
                    toast({
                        variant: 'destructive',
                        title: 'Invalid VAT5 certificate',
                    });
                    return false;
                }
            }

            return true;
        } catch (error: any) {
            const message = String(error?.message || '');
            const isNetwork = isLikelyNetworkError(message);
            toast({
                variant: 'destructive',
                title: isNetwork
                    ? isRelief ? 'Relief sale needs MRA online' : 'B2B needs MRA online'
                    : isRelief ? 'VAT5 validation failed' : 'Buyer validation failed',
                description: isNetwork ? undefined : 'Check EIS details.',
            });
            return false;
        }
    };

    const handlePayment = async (method: PaymentMethod) => {
        if (isProcessingPayment) {
            return;
        }

        if (isEisInvoiceSubmissionBlocked) {
            toast({
                variant: 'destructive',
                title: 'Connection required for sale',
                description: eisInvoiceSubmissionBlockedMessage,
            });
            return;
        }

        setIsProcessingPayment(true);
        try {
            const buyerDetails = normalizeBuyerDetails({
                name: buyerName,
                phone: buyerPhone,
                tin: buyerTin,
                authorizationCode: buyerAuthorizationCode,
                isExport: isExportSale,
                isReliefSupply,
                vat5ProjectNumber,
                vat5CertificateNumber,
                vat5Quantity: typeof vat5Quantity === 'number' ? vat5Quantity : Number.parseFloat(String(vat5Quantity || '')),
            });
            const buyerIsValid = await validateEisSaleFieldsBeforeCheckout(buyerDetails);
            if (!buyerIsValid) {
                return;
            }
            const order = await onCheckout(method, 0, buyerDetails);
            if (order) {
                const normalizedCashPaid =
                    typeof cashPaid === 'number'
                        ? cashPaid
                        : Number.parseFloat(String(cashPaid ?? ''));
                const hasCashPaid = Number.isFinite(normalizedCashPaid) && normalizedCashPaid > 0;
                const cashChange = hasCashPaid ? Math.max(0, normalizedCashPaid - total) : 0;

                const orderWithPaymentDetails: Order =
                    method === 'Cash' && hasCashPaid
                        ? ({
                              ...order,
                              cashPaid: normalizedCashPaid,
                              cash_paid: normalizedCashPaid,
                              amountTendered: normalizedCashPaid,
                              amount_tendered: normalizedCashPaid,
                              amountReceived: normalizedCashPaid,
                              amount_received: normalizedCashPaid,
                              change: cashChange,
                              changeAmount: cashChange,
                              change_amount: cashChange,
                          } as Order)
                        : order;

                if (method === 'Cash' && hasCashPaid) {
                    try {
                        await db.orders.update(order.id, {
                            cashPaid: normalizedCashPaid,
                            cash_paid: normalizedCashPaid,
                            amountTendered: normalizedCashPaid,
                            amount_tendered: normalizedCashPaid,
                            amountReceived: normalizedCashPaid,
                            amount_received: normalizedCashPaid,
                            change: cashChange,
                            changeAmount: cashChange,
                            change_amount: cashChange,
                        } as any);
                    } catch (paymentMetaError) {
                        console.warn('[PaymentDialog] Failed to persist cash payment metadata on order:', paymentMetaError);
                    }
                }

                setCompletedOrder(orderWithPaymentDetails);
                setStep('confirmation');
            }
        } finally {
            setIsProcessingPayment(false);
        }
    }
    
    const [isPrinting, setIsPrinting] = useState(false);
    const [autoPrintHandled, setAutoPrintHandled] = useState(false);
    const [isAutoPrintRunning, setIsAutoPrintRunning] = useState(false);
    const [isReceiptPreviewOpen, setIsReceiptPreviewOpen] = useState(false);
    const [isReceiptPdfExportOptionsOpen, setIsReceiptPdfExportOptionsOpen] = useState(false);
    const [isPreparingReceiptPreview, setIsPreparingReceiptPreview] = useState(false);
    const [isExportingReceiptPdf, setIsExportingReceiptPdf] = useState(false);
    const [hasDefaultPrinter, setHasDefaultPrinter] = useState<boolean | null>(null);
    const [receiptPaperWidth, setReceiptPaperWidth] = useState<PrinterPaperWidth>('80mm');
    const [receiptDisplaySettings, setReceiptDisplaySettings] = useState<ReceiptDisplaySettings>(DEFAULT_RECEIPT_DISPLAY_SETTINGS);
    const [receiptCopyNumber, setReceiptCopyNumber] = useState(1);
    const isPrintBusy = isPrinting || isAutoPrintRunning;
    const autoPrintOrderRef = useRef<string | null>(null);
    const cashDrawerOpenedOrderRef = useRef<string | null>(null);
    const printJobLockRef = useRef(false);
    const onCloseRef = useRef(onClose);

    useEffect(() => {
        onCloseRef.current = onClose;
    }, [onClose]);

    useEffect(() => {
        setHasDefaultPrinter(null);
        autoPrintOrderRef.current = null;
        cashDrawerOpenedOrderRef.current = null;
        setReceiptPaperWidth('80mm');
        setReceiptDisplaySettings(DEFAULT_RECEIPT_DISPLAY_SETTINGS);
        setReceiptCopyNumber(1);
        setIsReceiptPreviewOpen(false);
        setIsPreparingReceiptPreview(false);
    }, [resetToken]);

    const applyPrinterSettingsToReceipt = useCallback(
        (
            settings?: Partial<PrinterSettings> | null,
            fallbackPaperWidth: PrinterPaperWidth = '80mm'
        ): PrinterPaperWidth => {
            const resolvedPaperWidth = normalizePrinterPaperWidth(
                settings?.receiptPaperWidth,
                fallbackPaperWidth
            );

            setReceiptPaperWidth(resolvedPaperWidth);
            setReceiptDisplaySettings({
                showHeader: settings?.printHeader ?? true,
                showFooter: settings?.printFooter ?? true,
                showQRCode: settings?.printQRCode ?? true,
                showItemDetails: settings?.printItemDetails ?? true,
                showTaxBreakdown: settings?.printTaxBreakdown ?? true,
            });

            return resolvedPaperWidth;
        },
        []
    );

    const refreshPrinterState = useCallback(async () => {
        const { printerService } = await import('@/lib/services/printer-service');
        const [defaultPrinter, currentSettings] = await Promise.all([
            printerService.getDefaultPrinter(activeBranchId),
            printerService.getPrinterSettings(activeBranchId),
        ]);

        setHasDefaultPrinter(!!defaultPrinter);
        applyPrinterSettingsToReceipt(
            currentSettings,
            normalizePrinterPaperWidth(defaultPrinter?.paperWidth)
        );
    }, [activeBranchId, applyPrinterSettingsToReceipt]);

    const maybeOpenCashDrawerForSale = useCallback(
        async (
            orderToOpen: Order,
            settings: PrinterSettings,
            defaultPrinter: PrinterConfig | null
        ): Promise<void> => {
            const orderId = String((orderToOpen as any)?.id ?? '').trim();
            const paymentMethod = String((orderToOpen as any)?.paymentMethod ?? (orderToOpen as any)?.payment_method ?? '').trim().toLowerCase();

            if (!orderId || paymentMethod !== 'cash' || !settings.openCashDrawerOnCashSale || !defaultPrinter) {
                return;
            }

            if (cashDrawerOpenedOrderRef.current === orderId) {
                return;
            }

            try {
                const { unifiedPrintingService } = await import('@/lib/services/unified-printing-service');
                const result = await unifiedPrintingService.openCashDrawer(defaultPrinter);
                if (result.success) {
                    cashDrawerOpenedOrderRef.current = orderId;
                    return;
                }

                console.warn('[CashDrawer]', result.message);
            } catch (error) {
                console.warn('[CashDrawer] Failed to open cash drawer:', error);
            }
        },
        []
    );

    const waitForFiscalReceiptData = useCallback(
        async (orderToPrint: Order, timeoutMs: number = 45000): Promise<Order> => {
            if (!orderToPrint?.id) {
                return orderToPrint;
            }

            if (hasFiscalReceiptPrintData(orderToPrint)) {
                return orderToPrint;
            }

            const startedAt = Date.now();
            let latestKnownOrder: Order = orderToPrint;
            let nextBackendRefreshAt = 0;

            while (Date.now() - startedAt < timeoutMs) {
                const latestOrder = await db.orders.get(orderToPrint.id);
                if (latestOrder) {
                    latestKnownOrder = latestOrder as Order;
                    if (hasFiscalReceiptPrintData(latestKnownOrder)) {
                        return latestKnownOrder;
                    }
                }

                if (Date.now() >= nextBackendRefreshAt) {
                    nextBackendRefreshAt = Date.now() + 1000;
                    try {
                        const refreshedOrder = await refreshFiscalReceiptDataFromBackend(latestKnownOrder);
                        if (refreshedOrder) {
                            latestKnownOrder = refreshedOrder;
                            if (hasFiscalReceiptPrintData(latestKnownOrder)) {
                                return latestKnownOrder;
                            }
                        }
                    } catch (error) {
                        console.warn('[Receipt] Backend fiscal receipt refresh failed:', error);
                    }
                }

                await new Promise((resolve) => setTimeout(resolve, 300));
            }

            return latestKnownOrder;
        },
        []
    );

    const handlePrintReceipt = useCallback(async (): Promise<boolean> => {
        if (printJobLockRef.current) {

            return false;
        }

        printJobLockRef.current = true;
        try {
            setIsPrinting(true);
            const { printerService } = await import('@/lib/services/printer-service');
            const { silentPrintService } = await import('@/lib/services/silent-print-service');

            const activeOrder = completedOrder as Order | null;
            if (!activeOrder) {
                toast({
                    variant: 'destructive',
                    title: 'Print Failed',
                    description: 'No completed order found to print.',
                });
                return false;
            }
            const activeOrderId = String((activeOrder as any)?.id ?? '').trim();

            if (eisEnabled) {
                if (!hasFiscalReceiptPrintData(activeOrder)) {
                    toast({
                        title: 'Preparing Fiscal Receipt',
                        description: 'Preparing receipt...',
                    });

                    const latestOrder = await waitForFiscalReceiptData(activeOrder);

                    if (!hasFiscalReceiptPrintData(latestOrder)) {
                        const offlineQueued = isOfflineQueuedReceipt(latestOrder);
                        toast({
                            variant: offlineQueued ? 'default' : 'destructive',
                            title: offlineQueued ? 'Offline Receipt Queued' : 'Fiscal Receipt Pending',
                            description: offlineQueued
                                ? 'Receipt is queued for backend replay.'
                                : 'Try again shortly.',
                        });
                        return false;
                    }

                    setCompletedOrder(latestOrder);
                    // Give React a moment to render updated receipt data before capturing HTML.
                    await new Promise((resolve) => setTimeout(resolve, 150));
                }
            }

            const [settings, defaultPrinter] = await Promise.all([
                printerService.getPrinterSettings(activeBranchId),
                printerService.getDefaultPrinter(activeBranchId),
            ]);
            const selectedPaperWidth = applyPrinterSettingsToReceipt(
                settings,
                normalizePrinterPaperWidth(defaultPrinter?.paperWidth)
            );
            
            if (!defaultPrinter) {
                console.error('No default printer configured');
                toast({
                    title: 'Printing Skipped',
                    description: 'No default printer is configured yet.',
                });
                return false;
            }

            const configuredCopies = Number.isFinite(Number(settings.printCopies))
                ? Number(settings.printCopies)
                : 1;
            const copiesToPrint = Math.max(1, Math.floor(configuredCopies));
            const startingCopyNumber = getNextReceiptCopyNumber(activeOrderId);
            const isBluetoothPrinter =
                defaultPrinter.connectionType === 'bluetooth' ||
                String(defaultPrinter.id || '').toLowerCase().startsWith('bt:');
            const printAttemptTimeoutMs = isBluetoothPrinter ? 60000 : 20000;

            toast({
                title: 'Printing...',
                description: `Sending ${copiesToPrint} receipt${copiesToPrint > 1 ? 's' : ''} to ${defaultPrinter.name}`,
            });

            setReceiptPaperWidth(selectedPaperWidth);
            await new Promise((resolve) => {
                if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
                    window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve(undefined)));
                } else {
                    setTimeout(resolve, 150);
                }
            });

            // Try silent printing first (works with Tauri/Electron or auto-submit)
            const availableMethods = silentPrintService.getAvailableMethods();

            let printedCopies = 0;
            let failedResult: { timedOut: boolean; message?: string } | null = null;

            for (let copyIndex = 0; copyIndex < copiesToPrint; copyIndex += 1) {
                const currentCopyNumber = startingCopyNumber + copyIndex;
                setReceiptCopyNumber(currentCopyNumber);

                // Wait for receipt component to re-render with updated ORIGINAL/COPY marker.
                await new Promise((resolve) => setTimeout(resolve, 100));

                const receiptElement = document.getElementById('receipt-printable-area');
                const printContents = receiptElement?.outerHTML || receiptElement?.innerHTML;

                if (!printContents || printContents.trim().length === 0) {
                    console.error('Receipt content not found or empty');
                    failedResult = { timedOut: false };
                    break;
                }

                const printOptions = {
                    printerName: defaultPrinter.name,
                    printerId: defaultPrinter.id,
                    copies: 1,
                    paperSize: selectedPaperWidth,
                    printerPaperSize: selectedPaperWidth,
                };

                // Never keep the UI busy forever if native printing hangs.
                const printAttempt = Promise.race([
                    silentPrintService
                        .printSilentlyViaSystem(printContents, printOptions)
                        .then((success) => ({ success, timedOut: false })),
                    new Promise<{ success: false; timedOut: true }>((resolve) =>
                        setTimeout(() => resolve({ success: false, timedOut: true }), printAttemptTimeoutMs)
                    ),
                ]);

                const result = await printAttempt;
                if (!result.success) {
                    failedResult = {
                        timedOut: result.timedOut,
                        message: result.timedOut
                            ? undefined
                            : silentPrintService.getLastError() || undefined,
                    };
                    break;
                }

                printedCopies += 1;
            }

            if (printedCopies > 0) {
                markReceiptPrinted(activeOrderId, printedCopies);
            }

            const isCompleteSuccess = printedCopies === copiesToPrint && failedResult === null;
            if (isCompleteSuccess) {
                await maybeOpenCashDrawerForSale(activeOrder, settings, defaultPrinter);
                const printedTypeLabel = startingCopyNumber > 1 ? 'Receipt copy printed' : 'Original receipt printed';
                toast({
                    title: 'Print Successful',
                    description: copiesToPrint > 1
                        ? `${printedCopies} receipts sent to ${defaultPrinter.name}`
                        : `${printedTypeLabel} to ${defaultPrinter.name}`,
                });
                return true;
            }

            console.warn('Print failed');
            const failedDescription = failedResult?.timedOut
                ? 'Printer timed out.'
                : failedResult?.message || 'Print failed.';
            toast({
                variant: 'destructive',
                title: failedResult?.timedOut ? 'Print Timed Out' : 'Print Failed',
                description: printedCopies > 0
                    ? `${printedCopies} printed. ${failedDescription}`
                    : failedDescription,
            });
            return false;
        } catch (error) {
            console.error('Error printing receipt:', error);
            toast({
                variant: 'destructive',
                title: 'Print Error',
                description: 'Print failed.',
            });
            return false;
        } finally {
            setIsPrinting(false);
            printJobLockRef.current = false;
        }
    }, [activeBranchId, toast, applyPrinterSettingsToReceipt, completedOrder, eisEnabled, maybeOpenCashDrawerForSale, waitForFiscalReceiptData]);

    const handleViewMraReceipt = useCallback(async () => {
        const activeOrder = completedOrder as Order | null;
        if (!activeOrder) {
            return;
        }

        try {
            setIsPreparingReceiptPreview(true);
            let receiptOrder = activeOrder;

            if (eisEnabled && !hasFiscalReceiptPrintData(receiptOrder)) {
                toast({
                    title: 'Preparing Receipt',
                    description: 'Waiting for legal receipt details...',
                });

                receiptOrder = await waitForFiscalReceiptData(receiptOrder, 45000);
                setCompletedOrder(receiptOrder);
            }

            const fiscalNumber = extractFiscalInvoiceNumber(receiptOrder);
            const qrPayload = extractReceiptValidationPayload(receiptOrder);
            const signature = extractReceiptSignature(receiptOrder);
            const status = String((receiptOrder as any)?.eisStatus ?? (receiptOrder as any)?.eis_status ?? '').trim().toUpperCase();
            const hasReceiptData = Boolean((fiscalNumber && qrPayload) || signature || ['SUBMITTED', 'ACCEPTED'].includes(status));

            if (eisEnabled && !hasFiscalReceiptPrintData(receiptOrder)) {
                const offlineQueued = isOfflineQueuedReceipt(receiptOrder);
                toast({
                    variant: offlineQueued ? 'default' : 'destructive',
                    title: offlineQueued ? 'Offline Receipt Queued' : 'Receipt Not Ready',
                    description: offlineQueued
                        ? 'Receipt is queued for backend replay.'
                        : 'Try again shortly.',
                });
                return;
            }

            if (!eisEnabled && !hasReceiptData) {
                toast({
                    variant: 'destructive',
                    title: 'Receipt Not Ready',
                    description: 'Try again shortly.',
                });
                return;
            }

            const { printerService } = await import('@/lib/services/printer-service');
            const [defaultPrinter, currentSettings] = await Promise.all([
                printerService.getDefaultPrinter(activeBranchId),
                printerService.getPrinterSettings(activeBranchId),
            ]);
            applyPrinterSettingsToReceipt(
                currentSettings,
                normalizePrinterPaperWidth(defaultPrinter?.paperWidth)
            );
            setReceiptCopyNumber(1);
            setIsReceiptPreviewOpen(true);
        } finally {
            setIsPreparingReceiptPreview(false);
        }
    }, [activeBranchId, applyPrinterSettingsToReceipt, completedOrder, eisEnabled, toast, waitForFiscalReceiptData]);

    const prepareReceiptPdf = useCallback(async () => {
        const activeOrder = completedOrder as Order | null;
        if (!activeOrder) {
            throw new Error('No completed order found to export.');
        }

        let receiptOrder = activeOrder;
        if (eisEnabled && !hasFiscalReceiptPrintData(receiptOrder)) {
            receiptOrder = await waitForFiscalReceiptData(receiptOrder, 45000);
            if (!hasFiscalReceiptPrintData(receiptOrder)) {
                toast({
                    variant: isOfflineQueuedReceipt(receiptOrder) ? 'default' : 'destructive',
                    title: isOfflineQueuedReceipt(receiptOrder) ? 'Offline Receipt Queued' : 'Receipt Not Ready',
                    description: isOfflineQueuedReceipt(receiptOrder)
                        ? 'The receipt is queued for backend replay.'
                        : 'The fiscal receipt is not ready yet.',
                });
                return null;
            }
            setCompletedOrder(receiptOrder);
        }

        setReceiptCopyNumber(1);
        await new Promise((resolve) => setTimeout(resolve, 120));
        const elementId = document.getElementById('mra-receipt-preview-area')
            ? 'mra-receipt-preview-area'
            : 'receipt-printable-area';
        const orderNumber = String(
            (receiptOrder as any).fiscalInvoiceNumber ??
            (receiptOrder as any).fiscal_invoice_number ??
            (receiptOrder as any).orderNumber ??
            (receiptOrder as any).order_number ??
            'receipt'
        ).trim();
        return generateThermalReceiptPdf({
            elementId,
            paperWidth: receiptPaperWidth,
            filename: `handypos-receipt-${orderNumber}`,
        });
    }, [completedOrder, eisEnabled, receiptPaperWidth, toast, waitForFiscalReceiptData]);

    const handleDownloadReceiptPdf = useCallback(async () => {
        setIsReceiptPdfExportOptionsOpen(false);
        try {
            setIsExportingReceiptPdf(true);
            const document = await prepareReceiptPdf();
            if (!document) return;
            const result = await downloadThermalReceiptPdf(document);
            toast({
                title: 'Receipt PDF Downloaded',
                description: result.location === 'tauri'
                    ? `Saved to ${result.path}`
                    : `${result.filename} downloaded.`,
            });
        } catch (error) {
            console.error('[Receipt PDF] Download failed:', error);
            toast({
                variant: 'destructive',
                title: 'PDF Download Failed',
                description: error instanceof Error ? error.message : 'Could not download the thermal receipt PDF.',
            });
        } finally {
            setIsExportingReceiptPdf(false);
        }
    }, [prepareReceiptPdf, toast]);

    const handleShareReceiptPdf = useCallback(async () => {
        setIsReceiptPdfExportOptionsOpen(false);
        try {
            setIsExportingReceiptPdf(true);
            const document = await prepareReceiptPdf();
            if (!document) return;
            await shareThermalReceiptPdf(document);
            toast({ title: 'Receipt PDF Shared', description: 'Choose an app from the share sheet.' });
        } catch (error) {
            if (error instanceof DOMException && error.name === 'AbortError') return;
            console.error('[Receipt PDF] Share failed:', error);
            toast({
                variant: 'destructive',
                title: 'PDF Share Failed',
                description: error instanceof Error ? error.message : 'Could not share the thermal receipt PDF.',
            });
        } finally {
            setIsExportingReceiptPdf(false);
        }
    }, [prepareReceiptPdf, toast]);

    const handleExportReceiptPdf = useCallback(() => {
        setIsReceiptPdfExportOptionsOpen(true);
    }, []);

    useEffect(() => {
        if (step !== 'confirmation' || !completedOrder || autoPrintHandled) {
            return;
        }

        const orderId = String((completedOrder as any)?.id || '');
        if (!orderId) {
            return;
        }

        // Guard against effect re-runs (StrictMode + parent re-renders): auto-print once per order.
        if (autoPrintOrderRef.current === orderId) {
            return;
        }
        autoPrintOrderRef.current = orderId;

        let cancelled = false;

        const maybeAutoPrint = async () => {
            try {
                setIsAutoPrintRunning(true);
                const { printerService } = await import('@/lib/services/printer-service');
                const settings = await printerService.getPrinterSettings(activeBranchId);

                if (cancelled) {
                    return;
                }
                applyPrinterSettingsToReceipt(settings);

                if (settings.autoprint) {
                    const success = await handlePrintReceipt();
                    if (success && !cancelled) {
                        onCloseRef.current();
                    }
                }
            } catch (error) {
                console.error('[Print] Failed to evaluate auto-print settings:', error);
            } finally {
                // Always clear busy state, even if this run was cancelled by a re-render.
                setIsAutoPrintRunning(false);
                if (!cancelled) {
                    setAutoPrintHandled(true);
                }
            }
        };

        maybeAutoPrint();

        return () => {
            cancelled = true;
        };
    }, [step, completedOrder, autoPrintHandled, handlePrintReceipt, applyPrinterSettingsToReceipt, activeBranchId]);

    useEffect(() => {
        if (step === 'payment') {
            setAutoPrintHandled(false);
            setIsAutoPrintRunning(false);
            autoPrintOrderRef.current = null;
        }
    }, [step]);

    useEffect(() => {
        if (step !== 'confirmation' || !completedOrder) {
            return;
        }

        let cancelled = false;

        const checkDefaultPrinter = async () => {
            try {
                await refreshPrinterState();
            } catch (error) {
                console.warn('[Print] Failed to check default printer:', error);
                if (!cancelled) {
                    setHasDefaultPrinter(null);
                }
            }
        };

        checkDefaultPrinter();

        return () => {
            cancelled = true;
        };
    }, [step, completedOrder, refreshPrinterState, activeBranchId]);

    useEffect(() => {
        if (step !== 'confirmation' || !completedOrder) {
            return;
        }

        let cancelled = false;
        let refreshCount = 0;
        const orderId = String((completedOrder as any)?.id || '').trim();
        if (!orderId) {
            return;
        }

        const refreshCompletedOrder = async () => {
            try {
                const latestOrder = await db.orders.get(orderId);
                if (!latestOrder || cancelled) {
                    return;
                }

                setCompletedOrder((previousOrder) => {
                    if (!previousOrder || String((previousOrder as any)?.id || '') !== orderId) {
                        return previousOrder;
                    }
                    return {
                        ...previousOrder,
                        ...(latestOrder as Order),
                    } as Order;
                });
            } catch (error) {
                console.warn('[Receipt] Failed to refresh completed order state:', error);
            }
        };

        void refreshCompletedOrder();
        const intervalId = window.setInterval(() => {
            refreshCount += 1;
            void refreshCompletedOrder();
            if (refreshCount >= 60) {
                window.clearInterval(intervalId);
            }
        }, 1000);

        return () => {
            cancelled = true;
            window.clearInterval(intervalId);
        };
    }, [step, completedOrder?.id]);

    useEffect(() => {
        if (step !== 'confirmation' || !completedOrder) {
            return;
        }

        const handlePrinterUpdate = (event: Event) => {
            const customEvent = event as CustomEvent<{ branchId?: string }>;
            const updatedBranchId = String(customEvent.detail?.branchId || '').trim();
            if (updatedBranchId && updatedBranchId !== activeBranchId) {
                return;
            }

            void refreshPrinterState().catch((error) => {
                console.warn('[Print] Failed to refresh printer state after settings update:', error);
            });
        };

        window.addEventListener(PRINTER_CONFIG_UPDATED_EVENT, handlePrinterUpdate);
        return () => window.removeEventListener(PRINTER_CONFIG_UPDATED_EVENT, handlePrinterUpdate);
    }, [step, completedOrder, refreshPrinterState, activeBranchId]);
    
    if (step === 'confirmation' && completedOrder) {
        const displayOrderNumber = (completedOrder as any).orderNumber ?? (completedOrder as any).order_number ?? '-';

        // Ensure completedOrder has all tax data from cart items
        const sourceItems = Array.isArray((completedOrder as any).items) ? (completedOrder as any).items : [];
        const enrichedOrder = {
            ...completedOrder,
            orderNumber: (completedOrder as any).orderNumber ?? (completedOrder as any).order_number,
            createdAt: (completedOrder as any).createdAt ?? (completedOrder as any).created_at,
            sessionId: (completedOrder as any).sessionId ?? (completedOrder as any).session_id ?? (completedOrder as any).session,
            paymentMethod: (completedOrder as any).paymentMethod ?? (completedOrder as any).payment_method,
            subtotal: (completedOrder as any).subtotal ?? (completedOrder as any).net_amount ?? 0,
            total: (completedOrder as any).total ?? (completedOrder as any).gross_amount ?? 0,
            fiscalInvoiceNumber: (completedOrder as any).fiscalInvoiceNumber ?? (completedOrder as any).fiscal_invoice_number,
            eisStatus: (completedOrder as any).eisStatus ?? (completedOrder as any).eis_status,
            eisSyncState: (completedOrder as any).eisSyncState ?? (completedOrder as any).eis_sync_state,
            eisUuid: (completedOrder as any).eisUuid ?? (completedOrder as any).eis_uuid,
            eisSubmittedAt: (completedOrder as any).eisSubmittedAt ?? (completedOrder as any).eis_submitted_at,
            qrCodePayload: (completedOrder as any).qrCodePayload ?? (completedOrder as any).qr_code_payload,
            digitalSignature: (completedOrder as any).digitalSignature ?? (completedOrder as any).digital_signature,
            items: sourceItems.map((item: any) => ({
                ...item,
                // Ensure all tax fields are present
                tax_rate: item.tax_rate || item.taxRate || 0,
                tax_type: item.tax_type || item.taxType || 'standard',
                tax_calculation_method: item.tax_calculation_method || item.taxCalculationMethod || 'inclusive',
                subtotal: item.subtotal || 0,
                tax_amount: item.tax_amount || item.taxAmount || 0,
                total: item.total || 0,
            })) || []
        };
        const enrichedFiscalNumber = extractFiscalInvoiceNumber(enrichedOrder);
        const enrichedQrPayload = extractReceiptValidationPayload(enrichedOrder);
        const enrichedSignature = extractReceiptSignature(enrichedOrder);
        const enrichedEisStatus = String((enrichedOrder as any).eisStatus ?? (enrichedOrder as any).eis_status ?? '').trim().toUpperCase();
        const completedSaleSubmission = resolveCompletedSaleSubmissionDisplay(
            enrichedOrder,
            isBrowserOnline,
            shouldUseEisTaxMappings
        );
        const hasPrintableFiscalReceipt = hasFiscalReceiptPrintData(enrichedOrder);
        const isEisReceiptPrintBlocked = Boolean(eisEnabled && !hasPrintableFiscalReceipt);
        const canViewMraReceipt = Boolean(
            eisEnabled ||
            enrichedFiscalNumber ||
            enrichedQrPayload ||
            enrichedSignature ||
            enrichedEisStatus
        );

        return (
             <DialogContent className="tauri-android-safe-bottom max-h-[calc(100vh-1rem)] max-h-[calc(100dvh-1rem)] w-[calc(100vw-1rem)] max-w-md overflow-y-auto p-4 sm:max-w-lg sm:p-6">
                <DialogHeader>
                    <DialogTitle className="flex items-center justify-center text-center">
                        <CheckCircle className="h-12 w-12 text-green-500" />
                    </DialogTitle>
                </DialogHeader>
                <div className="text-center py-4">
                    <h2 className="text-xl font-semibold">Payment Successful</h2>
                    <p className="text-muted-foreground">Order #{displayOrderNumber} has been created.</p>
                    <div className="mt-3 flex flex-col items-center gap-1">
                        <Badge
                            variant="outline"
                            className={cn(
                                'gap-1.5 text-xs font-semibold',
                                completedSaleSubmissionBadgeClass(completedSaleSubmission.tone)
                            )}
                        >
                            {completedSaleSubmission.tone === 'offline' ? (
                                <WifiOff className="h-3.5 w-3.5" />
                            ) : (
                                <Wifi className="h-3.5 w-3.5" />
                            )}
                            {completedSaleSubmission.label}
                        </Badge>
                        <p className={cn(
                            'max-w-sm text-xs',
                            completedSaleSubmission.tone === 'offline'
                                ? 'text-amber-700'
                                : completedSaleSubmission.tone === 'rejected'
                                    ? 'text-red-700'
                                    : 'text-muted-foreground'
                        )}>
                            {completedSaleSubmission.description}
                        </p>
                    </div>
                    {hasDefaultPrinter === false && (
                        <p className="mt-2 text-sm text-muted-foreground">
                            Printing skipped: no default printer is configured yet.
                        </p>
                    )}
                </div>
                 <div className="hidden">
                    <Receipt
                        order={enrichedOrder}
                        business={businessSettings}
                        currencyFormatter={currencyFormatter}
                        paperWidth={receiptPaperWidth}
                        showHeader={receiptDisplaySettings.showHeader}
                        showFooter={receiptDisplaySettings.showFooter}
                        showQRCode={receiptDisplaySettings.showQRCode}
                        showItemDetails={receiptDisplaySettings.showItemDetails}
                        showTaxBreakdown={receiptDisplaySettings.showTaxBreakdown}
                        copyNumber={receiptCopyNumber}
                    />
                 </div>
                <ReceiptPdfExportDialog
                    open={isReceiptPdfExportOptionsOpen}
                    onOpenChange={setIsReceiptPdfExportOptionsOpen}
                    canShare={canShareThermalReceiptPdf()}
                    isBusy={isExportingReceiptPdf}
                    onDownload={handleDownloadReceiptPdf}
                    onShare={handleShareReceiptPdf}
                />
                <Dialog open={isReceiptPreviewOpen} onOpenChange={setIsReceiptPreviewOpen}>
                    <DialogContent className="tauri-android-safe-bottom max-h-[calc(100vh-1rem)] max-h-[calc(100dvh-1rem)] w-[calc(100vw-1rem)] max-w-[420px] overflow-y-auto p-4 sm:p-6">
                        <DialogHeader>
                            <DialogTitle>Receipt</DialogTitle>
                            <DialogDescription>
                                Fiscal receipt preview for order #{displayOrderNumber}.
                            </DialogDescription>
                        </DialogHeader>
                        <div className="flex justify-center rounded-md bg-muted/40 py-4">
                            <Receipt
                                order={enrichedOrder}
                                business={businessSettings}
                                currencyFormatter={currencyFormatter}
                                paperWidth={receiptPaperWidth}
                                showHeader
                                showFooter
                                showQRCode
                                showItemDetails
                                showTaxBreakdown
                                copyNumber={1}
                                elementId="mra-receipt-preview-area"
                            />
                        </div>
                        <DialogFooter className="gap-2 sm:space-x-0">
                            <Button className="w-full sm:w-auto" variant="outline" onClick={() => setIsReceiptPreviewOpen(false)}>
                                Close
                            </Button>
                            <Button className="w-full sm:w-auto" variant="secondary" onClick={handleExportReceiptPdf} disabled={isExportingReceiptPdf || isPrintBusy}>
                                {isExportingReceiptPdf ? (
                                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                ) : (
                                    <Download className="mr-2 h-4 w-4" />
                                )}
                                {isExportingReceiptPdf ? 'Exporting...' : 'Export PDF'}
                            </Button>
                            <Button className="w-full sm:w-auto" onClick={handlePrintReceipt} disabled={isPrintBusy || hasDefaultPrinter === false || isEisReceiptPrintBlocked}>
                                {isPrintBusy ? (
                                    <>
                                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                        Printing...
                                    </>
                                ) : (
                                    <>
                                        <Printer className="mr-2 h-4 w-4" />
                                        Print
                                    </>
                                )}
                            </Button>
                        </DialogFooter>
                    </DialogContent>
                </Dialog>

                <DialogFooter className="gap-2 border-t pt-4 sm:flex-wrap sm:justify-center sm:space-x-0">
                    <Button className="w-full sm:w-auto" variant="outline" onClick={onClose} disabled={isPrintBusy}>New Order</Button>
                    {canViewMraReceipt && (
                        <Button
                            className="w-full sm:w-auto"
                            variant="secondary"
                            onClick={handleViewMraReceipt}
                            disabled={isPrintBusy || isPreparingReceiptPreview}
                        >
                            {isPreparingReceiptPreview ? (
                                <>
                                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                    Preparing...
                                </>
                            ) : (
                                <>
                                    <Eye className="mr-2 h-4 w-4" />
                                    View Receipt
                                </>
                            )}
                        </Button>
                    )}
                    <Button
                        className="w-full sm:w-auto"
                        variant="secondary"
                        onClick={handleExportReceiptPdf}
                        disabled={isExportingReceiptPdf || isPrintBusy}
                    >
                        {isExportingReceiptPdf ? (
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        ) : (
                            <Download className="mr-2 h-4 w-4" />
                        )}
                        {isExportingReceiptPdf ? 'Exporting...' : 'Export PDF'}
                    </Button>
                    {hasDefaultPrinter === false && (
                        <Button className="w-full sm:w-auto" variant="secondary" onClick={onConfigurePrinter} disabled={isPrintBusy}>
                            <Printer className="mr-2 h-4 w-4" />
                            Configure Printer
                        </Button>
                    )}
                    <Button onClick={handlePrintReceipt} disabled={isPrintBusy || hasDefaultPrinter === false || isEisReceiptPrintBlocked} className="w-full bg-blue-600 hover:bg-blue-700 sm:w-auto">
                        {isPrintBusy ? (
                            <>
                                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                Printing...
                            </>
                        ) : (
                            <>
                                <DollarSign className="mr-2 h-4 w-4" />
                                Print Receipt
                            </>
                        )}
                    </Button>
                </DialogFooter>
             </DialogContent>
        )
    }

    return (
        <DialogContent className="tauri-android-safe-bottom flex max-h-[calc(100vh-1rem)] max-h-[calc(100dvh-1rem)] w-[calc(100vw-1rem)] max-w-2xl flex-col overflow-hidden p-4 sm:p-6">
            <DialogHeader>
                <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                        <DialogTitle className="text-xl">Complete Payment</DialogTitle>
                        <DialogDescription>Select the payment method</DialogDescription>
                    </div>
                    <Badge
                        variant="outline"
                        className={cn(
                            'w-fit gap-1.5 text-xs font-semibold',
                            connectivityTone === 'online'
                                ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                                : connectivityTone === 'blocked'
                                    ? 'border-red-200 bg-red-50 text-red-700'
                                    : connectivityTone === 'unknown'
                                        ? 'border-slate-200 bg-slate-50 text-slate-700'
                                        : 'border-amber-200 bg-amber-50 text-amber-800'
                        )}
                    >
                        {connectivityTone === 'online' ? <Wifi className="h-3.5 w-3.5" /> : <WifiOff className="h-3.5 w-3.5" />}
                        {saleConnectivityLabel}
                    </Badge>
                </div>
                <p className={cn(
                    'text-xs',
                    connectivityTone === 'online' || connectivityTone === 'unknown' ? 'text-muted-foreground' : connectivityTone === 'blocked' ? 'text-red-700' : 'text-amber-700'
                )}>
                    {saleConnectivityDescription}
                </p>
            </DialogHeader>
            <div className="space-y-4 overflow-y-auto py-3 pb-5 flex-1 hide-scrollbar">
                <div className="space-y-1 rounded-lg border bg-muted/30 p-3">
                    <div className="flex justify-between text-xs"><span>Net Amount</span><span>{currencyFormatter(calculatedNetAmount)}</span></div>
                    <div className="flex justify-between text-xs"><span>{calculatedTaxLabel || 'VAT Amount'}</span><span className="text-green-600 font-semibold">{currencyFormatter(calculatedTax)}</span></div>
                    {taxMethodRateSummary && (
                        <div className="flex justify-between text-[11px] text-muted-foreground">
                            <span>Methods & Rates</span>
                            <span className="text-right">{taxMethodRateSummary}</span>
                        </div>
                    )}
                    <div className="flex justify-between text-sm font-semibold"><span>Gross Amount (Including VAT)</span><span>{currencyFormatter(calculatedGrossAmount)}</span></div>
                    <Separator className="my-1" />
                    <div className="flex justify-between text-lg font-bold text-primary"><span>Total Amount Due</span><span>{currencyFormatter(total)}</span></div>
                </div>

                {isEisInvoiceSubmissionBlocked && (
                    <div className="flex items-start gap-2 rounded-lg border border-yellow-300 bg-yellow-50 p-3 text-sm text-yellow-900 dark:border-yellow-800 dark:bg-yellow-950/30 dark:text-yellow-100">
                        <WifiOff className="mt-0.5 h-4 w-4 shrink-0" />
                        <span>{eisInvoiceSubmissionBlockedMessage}</span>
                    </div>
                )}

                <div className="space-y-3 rounded-lg border bg-muted/30 p-3">
                    <h4 className="text-sm font-medium">Buyer Details (optional)</h4>
                    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                        <div className="space-y-1">
                            <label className="text-xs font-medium text-muted-foreground">Buyer Name</label>
                            <Input
                                placeholder="Enter buyer name"
                                value={buyerName}
                                onChange={(e) => setBuyerName(e.target.value)}
                                disabled={isProcessingPayment}
                            />
                        </div>
                        <div className="space-y-1">
                            <label className="text-xs font-medium text-muted-foreground">Buyer TIN</label>
                            <Input
                                placeholder="Enter buyer TIN"
                                value={buyerTin}
                                onChange={(e) => setBuyerTin(e.target.value)}
                                disabled={isProcessingPayment}
                            />
                        </div>
                    </div>
	                    <div className="space-y-1">
	                        <label className="text-xs font-medium text-muted-foreground">Authorization Code</label>
	                        <Input
	                            placeholder="Buyer code"
	                            value={buyerAuthorizationCode}
	                            onChange={(e) => setBuyerAuthorizationCode(e.target.value)}
	                            disabled={isProcessingPayment}
	                        />
	                    </div>
	                    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
	                        <label className="flex h-10 items-center gap-2 rounded-md border px-3 text-sm">
	                            <input
	                                type="checkbox"
	                                className="h-4 w-4"
	                                checked={isExportSale}
	                                onChange={(e) => setIsExportSale(e.target.checked)}
	                                disabled={isProcessingPayment}
	                            />
	                            Export
	                        </label>
	                        <label className="flex h-10 items-center gap-2 rounded-md border px-3 text-sm">
	                            <input
	                                type="checkbox"
	                                className="h-4 w-4"
	                                checked={isReliefSupply}
	                                onChange={(e) => setIsReliefSupply(e.target.checked)}
	                                disabled={isProcessingPayment}
	                            />
	                            VAT Relief
	                        </label>
	                    </div>
	                    {isReliefSupply && (
	                        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
	                            <div className="space-y-1">
	                                <label className="text-xs font-medium text-muted-foreground">Project No.</label>
	                                <Input
	                                    value={vat5ProjectNumber}
	                                    onChange={(e) => setVat5ProjectNumber(e.target.value)}
	                                    disabled={isProcessingPayment}
	                                />
	                            </div>
	                            <div className="space-y-1">
	                                <label className="text-xs font-medium text-muted-foreground">Certificate No.</label>
	                                <Input
	                                    value={vat5CertificateNumber}
	                                    onChange={(e) => setVat5CertificateNumber(e.target.value)}
	                                    disabled={isProcessingPayment}
	                                />
	                            </div>
	                            <div className="space-y-1">
	                                <label className="text-xs font-medium text-muted-foreground">Quantity</label>
	                                <Input
	                                    type="number"
	                                    min="0"
	                                    step="0.001"
	                                    value={vat5Quantity}
	                                    onChange={(e) => setVat5Quantity(e.target.value)}
	                                    disabled={isProcessingPayment}
	                                />
	                            </div>
	                        </div>
	                    )}
	                </div>

                <div>
                    <h4 className="text-sm font-medium mb-2">Payment Method</h4>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                       <Button size="default" variant={selectedPaymentMethod === 'Cash' ? 'default' : 'outline'} onClick={() => setSelectedPaymentMethod('Cash')} className="text-sm h-11" disabled={isProcessingPayment || isEisInvoiceSubmissionBlocked}><DollarSign className="mr-1 h-4 w-4"/>Cash</Button>
                       <Button size="default" variant={selectedPaymentMethod === 'Card' ? 'default' : 'outline'} onClick={() => setSelectedPaymentMethod('Card')} className="text-sm h-11" disabled={isProcessingPayment || isEisInvoiceSubmissionBlocked}><CreditCard className="mr-1 h-4 w-4"/>Card</Button>
                       <Button size="default" variant={selectedPaymentMethod === 'Mobile Money' ? 'default' : 'outline'} onClick={() => setSelectedPaymentMethod('Mobile Money')} className="text-sm h-11" disabled={isProcessingPayment || isEisInvoiceSubmissionBlocked}><Smartphone className="mr-1 h-4 w-4"/>Mobile</Button>
                       <Button size="default" variant={selectedPaymentMethod === 'On Account' ? 'default' : 'outline'} onClick={() => setSelectedPaymentMethod('On Account')} className="text-sm h-11" disabled={isProcessingPayment || isEisInvoiceSubmissionBlocked}><UserPlus className="mr-1 h-4 w-4"/>Account</Button>
                    </div>
                </div>

                {shouldEnforceTaxMapping && unmappedProducts.length > 0 && (
                    <div className="rounded-lg border border-red-200 bg-red-50 dark:bg-red-950/20 p-3">
                        <h4 className="text-sm font-semibold mb-2 text-red-900 dark:text-red-100">Mapping required</h4>
                        <p className="text-xs text-red-800 dark:text-red-200 mb-2">
                            Fix these products first:
                        </p>
                        <ul className="text-xs text-red-800 dark:text-red-200 space-y-1">
                            {unmappedProducts.map((productName, idx) => (
                                <li key={idx}>• {productName}</li>
                            ))}
                        </ul>
                        <p className="text-xs text-red-700 dark:text-red-300 mt-2 font-medium">
                            Map or remove them.
                        </p>
                    </div>
                )}

                {selectedPaymentMethod === 'Cash' && (
                    <div className="space-y-3 rounded-lg border bg-blue-50 dark:bg-blue-950/30 p-4">
                        <div>
                            <label className="text-sm font-medium">Cash Paid</label>
                            <Input 
                                type="number" 
                                step="0.01" 
                                placeholder="Enter amount paid" 
                                value={cashPaid} 
                                onChange={(e) => setCashPaid(e.target.value ? parseFloat(e.target.value) : '')}
                                className="mt-2 text-lg font-semibold h-11"
                                disabled={hasBlockingUnmapped || isProcessingPayment || isEisInvoiceSubmissionBlocked}
                            />
                        </div>
                        <Separator />
                        <div className="space-y-2">
                            <div className="flex justify-between text-sm">
                                <span>Amount Due</span>
                                <span className="font-semibold">{currencyFormatter(total)}</span>
                            </div>
                            <div className={cn("flex justify-between text-lg font-bold", change >= 0 ? 'text-green-600' : 'text-red-600')}>
                                <span>Change</span>
                                <span>{currencyFormatter(Math.max(0, change))}</span>
                            </div>
                        </div>
                        <Button 
                            size="lg" 
                            className="w-full bg-green-600 hover:bg-green-700 text-base h-12" 
                            onClick={() => handlePayment('Cash')} 
                            disabled={typeof cashPaid !== 'number' || cashPaid < total || hasBlockingUnmapped || isProcessingPayment || isEisInvoiceSubmissionBlocked}
                            title={isEisInvoiceSubmissionBlocked ? eisInvoiceSubmissionBlockedMessage : hasBlockingUnmapped ? 'Cannot complete payment: unmapped products in cart' : ''}
                        >
                            {isProcessingPayment ? (
                                <>
                                    <Loader2 className="mr-2 h-5 w-5 animate-spin" />
                                    Processing Payment...
                                </>
                            ) : (
                                <>
                                    <CreditCard className="mr-2 h-5 w-5" />
                                    Complete Payment
                                </>
                            )}
                        </Button>
                    </div>
                )}

                {selectedPaymentMethod && selectedPaymentMethod !== 'Cash' && (
                    <Button 
                        size="lg" 
                        className="w-full bg-green-600 hover:bg-green-700 text-base h-12" 
                        onClick={() => handlePayment(selectedPaymentMethod)} 
                        disabled={hasBlockingUnmapped || isProcessingPayment || isEisInvoiceSubmissionBlocked}
                        title={isEisInvoiceSubmissionBlocked ? eisInvoiceSubmissionBlockedMessage : hasBlockingUnmapped ? 'Cannot complete payment: unmapped products in cart' : ''}
                    >
                        {isProcessingPayment ? (
                            <>
                                <Loader2 className="mr-2 h-5 w-5 animate-spin" />
                                Processing Payment...
                            </>
                        ) : (
                            <>
                                <CreditCard className="mr-2 h-5 w-5" />
                                Complete Payment
                            </>
                        )}
                    </Button>
                )}
            </div>
        </DialogContent>
    )
}

export const GenericPos = ({
  inventory,
  displayItems,
  emptyStateTitle = 'No products found',
  emptyStateDescription = '',
  cart,
  onAddToCart,
  onUpdateQuantity,
  onApplyDiscount,
  onClearCart,
  onCheckout,
  productIcon = <Package className="h-8 w-8 text-muted-foreground" />,
  viewMode = 'grid',
  defaultTaxRate,
  eisEnabled = false,
  blockSalesIfTaxMappingMissing = false,
  isEisInvoiceSubmissionBlocked = false,
  eisInvoiceSubmissionBlockedMessage = 'POS server unreachable.',
  branchId,
}: PosProps) => {
  const [isPaymentDialogOpen, setPaymentDialogOpen] = useState(false);
  const [showPrinterConfig, setShowPrinterConfig] = useState(false);
  const [paymentSessionId, setPaymentSessionId] = useState(0);
  const [discountRules, setDiscountRules] = useState<DiscountRule[]>([]);
  const { format: formatCurrency } = useCurrency();
  const shouldUseEisTaxMappings = Boolean(eisEnabled);
  const shouldEnforceTaxMapping = shouldUseEisTaxMappings && blockSalesIfTaxMappingMissing === true;
  const activeBranchId = useMemo(
    () => branchId ?? safeLocalStorageGetItem('handypos-active-branch') ?? 'main',
    [branchId]
  );
  const normalizedActiveBranchId = useMemo(
    () => normalizeBranchIdentifier(activeBranchId),
    [activeBranchId]
  );
  
  const defaultTaxRateDecimal = defaultTaxRate ? defaultTaxRate.rate / 100 : 0;
  const taxLabel = defaultTaxRate ? `${defaultTaxRate.name} (${defaultTaxRate.rate}%)` : 'Tax';

  const mraMappings = useLiveQuery(() => db.mraMappings.toArray());

  useEffect(() => {
    let cancelled = false;

    const loadDiscountRules = async () => {
      try {
        const params = new URLSearchParams({ active: 'true' });
        if (branchId) {
          params.set('branch_id', normalizeBranchIdentifier(branchId));
        }
        const response = await authFetch.fetch<any>(`/sessions/discounts/?${params.toString()}`);
        const rows = Array.isArray(response)
          ? response
          : Array.isArray(response?.results)
            ? response.results
            : [];
        const normalized = rows
          .map(normalizeDiscountRule)
          .filter((rule): rule is DiscountRule => Boolean(rule));
        if (!cancelled) {
          setDiscountRules(normalized);
        }
      } catch (error) {
        console.warn('[POS] Failed to load discount rules:', error);
        if (!cancelled) {
          setDiscountRules([]);
        }
      }
    };

    void loadDiscountRules();

    return () => {
      cancelled = true;
    };
  }, [branchId]);

  const mappingByItemId = useMemo(() => {
    const shouldScopeByBranch =
      Boolean(normalizedActiveBranchId) &&
      !['main', 'main-branch', 'main_branch'].includes(normalizedActiveBranchId.toLowerCase());

    const scopedMappings = (mraMappings || []).filter((mapping) => {
      const mappingBranchId = normalizeBranchIdentifier(
        (mapping as any).branchId ??
        (mapping as any).branch_id ??
        (mapping as any).branch
      );

      if (!mappingBranchId) {
        return true;
      }
      if (!shouldScopeByBranch) {
        return true;
      }
      return mappingBranchId === normalizedActiveBranchId;
    });

    return buildMappingLookup(scopedMappings);
  }, [mraMappings, normalizedActiveBranchId]);

  const cartSummary = useMemo(() => {
    if (!cart || cart.length === 0) {
      return {
        net: 0,
        tax: 0,
        gross: 0,
        methodSummary: 'N/A' as const,
        perItemTax: {} as Record<string, CartItemTaxDetail>,
      };
    }

    let totalTax = 0;
    let totalNet = 0;
    let totalGross = 0;
    const methods = new Set<'inclusive' | 'exclusive'>();
    const perItemTax: Record<string, CartItemTaxDetail> = {};

    for (const cartItem of cart) {
      const itemKey = String(cartItem.id || '');
      const primaryInventoryItemId = String(cartItem.inventoryItemId || '').trim();
      const fallbackInventoryItemId = resolveCartInventoryItemId(cartItem) || String(cartItem.id || '').trim();
      const preferredMappingKey = primaryInventoryItemId || fallbackInventoryItemId;
      let localMapping = mappingByItemId.get(preferredMappingKey);
      if (!localMapping && fallbackInventoryItemId && fallbackInventoryItemId !== preferredMappingKey) {
        localMapping = mappingByItemId.get(fallbackInventoryItemId);
      }

      const lineAmountBeforeDiscount = resolveCartLineTotal(cartItem);
      const discountAmount = resolveCartDiscountAmount(cartItem);
      const lineAmount = Math.max(0, lineAmountBeforeDiscount - discountAmount);
      let itemTax = 0;
      let itemNet = lineAmount;
      let itemGross = lineAmount;
      let taxType: NormalizedTaxType = 'unmapped';
      let method: NormalizedTaxCalculationMethod = 'unmapped';
      let ratePercent = 0;
      let status: MappingStatus = 'unmapped';

      const hasMapping = Boolean(localMapping);
      const isApproved = Boolean(localMapping?.isApproved ?? localMapping?.is_approved);
      const isSynced = Boolean(localMapping?.mraSynced ?? localMapping?.mra_synced);
      const fallbackTaxType = hasMapping
        ? normalizeMappedTaxType(localMapping.mraTaxType || localMapping.mra_tax_type)
        : 'standard';
      const fallbackRate = hasMapping
        ? Number(localMapping.mraTaxRate ?? localMapping.mra_tax_rate ?? 0)
        : 0;
      const normalizedFallbackRate = Number.isFinite(fallbackRate) ? fallbackRate : 0;
      const fallbackMethod = hasMapping ? resolveMappingTaxMethod(localMapping) : 'inclusive';

      if (hasMapping) {
        status = isApproved && isSynced ? 'ready' : 'pending';
      }

      if (hasMapping && isApproved && isSynced) {
        taxType = normalizeMappedTaxType(localMapping.mraTaxType || localMapping.mra_tax_type);
        const rawRate = Number(localMapping.mraTaxRate ?? localMapping.mra_tax_rate ?? 0);
        const normalizedRate = Number.isFinite(rawRate) ? rawRate : 0;
        ratePercent = normalizedRate;
        if (taxType === 'zero' || taxType === 'exempt' || normalizedRate <= 0) {
          itemTax = 0;
          itemNet = lineAmount;
          itemGross = lineAmount;
          method = 'not_applicable';
        } else {
          method = resolveMappingTaxMethod(localMapping);
          const effectiveRate = normalizedRate / 100;
          methods.add(method);
          if (method === 'exclusive') {
            itemTax = lineAmount * effectiveRate;
            itemNet = lineAmount;
            itemGross = lineAmount + itemTax;
          } else {
            itemTax = effectiveRate > 0 ? lineAmount * effectiveRate / (1 + effectiveRate) : 0;
            itemGross = lineAmount;
            itemNet = lineAmount - itemTax;
          }
        }
      } else if (!shouldUseEisTaxMappings) {
        taxType = fallbackTaxType;
        ratePercent = normalizedFallbackRate;
        if (hasMapping && (fallbackTaxType === 'zero' || fallbackTaxType === 'exempt')) {
          itemTax = 0;
          itemNet = lineAmount;
          itemGross = lineAmount;
          method = 'not_applicable';
        } else if (hasMapping && normalizedFallbackRate > 0) {
          const effectiveRate = normalizedFallbackRate / 100;
          methods.add(fallbackMethod);
          method = fallbackMethod;
          if (fallbackMethod === 'exclusive') {
            itemTax = lineAmount * effectiveRate;
            itemNet = lineAmount;
            itemGross = lineAmount + itemTax;
          } else {
            itemTax = effectiveRate > 0 ? lineAmount * effectiveRate / (1 + effectiveRate) : 0;
            itemGross = lineAmount;
            itemNet = lineAmount - itemTax;
          }
        } else if (defaultTaxRateDecimal > 0) {
          taxType = 'standard';
          ratePercent = defaultTaxRateDecimal * 100;
          method = 'inclusive';
          itemTax = lineAmount * defaultTaxRateDecimal / (1 + defaultTaxRateDecimal);
          itemGross = lineAmount;
          itemNet = lineAmount - itemTax;
        } else {
          itemTax = 0;
          itemNet = lineAmount;
          itemGross = lineAmount;
          taxType = 'unmapped';
          method = 'unmapped';
          ratePercent = 0;
        }
      } else if (hasMapping) {
        taxType = fallbackTaxType;
        ratePercent = normalizedFallbackRate;
        method = (fallbackTaxType === 'zero' || fallbackTaxType === 'exempt')
          ? 'not_applicable'
          : fallbackMethod;
      }

      totalTax += itemTax;
      totalNet += itemNet;
      totalGross += itemGross;
      if (itemKey) {
        perItemTax[itemKey] = {
          amount: itemTax,
          rate: ratePercent,
          taxType,
          method,
          status,
        };
      }
    }

    let methodSummary: 'Inclusive' | 'Exclusive' | 'Mixed' | 'Default (Inclusive)' | 'N/A' = 'N/A';
    if (methods.size === 1) {
      methodSummary = methods.has('exclusive') ? 'Exclusive' : 'Inclusive';
    } else if (methods.size > 1) {
      methodSummary = 'Mixed';
    } else if (!shouldUseEisTaxMappings && defaultTaxRateDecimal > 0) {
      methodSummary = 'Default (Inclusive)';
    }

    return {
      net: totalNet,
      tax: totalTax,
      gross: totalGross,
      methodSummary,
      perItemTax,
    };
  }, [cart, mappingByItemId, defaultTaxRateDecimal, shouldUseEisTaxMappings]);

  const subtotal = cartSummary.net;
  const tax = cartSummary.tax;
  const total = cartSummary.gross;
  const cartTaxLabel = shouldUseEisTaxMappings ? 'VAT Amount' : (taxLabel || 'VAT Amount');
  const hasItemsInCart = cart.length > 0;
  const invalidDiscountItems = useMemo(
    () => cart.filter((item) => isCartDiscountInvalid(item)),
    [cart]
  );

  const getMRAMappingStatus = useCallback((itemId: string): {
    hasMapping: boolean;
    isApproved: boolean;
    isSynced: boolean;
    isValid: boolean;
    mapping?: any;
  } => {
    const mapping = mappingByItemId.get(String(itemId));
    if (!mapping) {
      return {
        hasMapping: false,
        isApproved: false,
        isSynced: false,
        isValid: false,
      };
    }

    const isApproved = Boolean(mapping.isApproved ?? mapping.is_approved);
    const isSynced = Boolean(mapping.mraSynced ?? mapping.mra_synced);
    return {
      hasMapping: true,
      isApproved,
      isSynced,
      isValid: isApproved && isSynced,
      mapping,
    };
  }, [mappingByItemId]);

  const hasValidMRAMapping = (itemId: string): boolean => {
    const status = getMRAMappingStatus(itemId);

    if (!status.hasMapping) {

      return false;
    }

    if (!status.isValid) {

      return false;
    }

    return true;
  };

  const canProduceItem = (item: InventoryItem): boolean => {
    if (!item.isProduced || !item.recipe || item.recipe.length === 0) {
      return true;
    }
    
    const canProduce = item.recipe.every((recipeItem: any) => {
      const ingredientId = recipeItem.ingredientId;
      const requiredQuantity = recipeItem.quantity || 0;
      
      const ingredient = inventory.find(i => i.id === ingredientId);
      
      if (!ingredient) {
        console.warn(`Ingredient not found in inventory: ${recipeItem.name} (ID: ${ingredientId})`);
        return false;
      }
      
      const availableStock = ingredient.stockUnits || 0;
      const hasSufficientStock = availableStock >= requiredQuantity;
      
      return hasSufficientStock;
    });
    
    return canProduce;
  };

  const getStockInfo = (
    item: InventoryItem
  ): {
    text: string;
    canAddToCart: boolean;
    hasMRAMapping: boolean;
    stockTone: 'available' | 'warning' | 'out';
  } => {
    // First check stock/availability regardless of MRA mapping
    let stockText = '';
    let hasStock = false;
    
    if (item.itemType === 'sellable' && item.isProduced && item.recipe && item.recipe.length > 0) {
      const available = canProduceItem(item);
      stockText = available ? '✓ Available' : '✗ Out of Stock';
      hasStock = available;
    } else {
      const remaining = Number(item.stockUnits || 0);
      hasStock = remaining > 0;
      stockText = `${formatInventoryQuantity(remaining, { preferWholeNumbers: true, maximumFractionDigits: 3 })} ${item.unitType || 'units'} remaining`;
    }
    
    if (!shouldEnforceTaxMapping) {
      return {
        text: stockText,
        canAddToCart: hasStock,
        hasMRAMapping: true,
        stockTone: hasStock ? 'available' : 'out',
      };
    }

    // Then check MRA mapping (only when enforcement is enabled)
    const mraStatus = getMRAMappingStatus(item.id);
    const hasMRAMapping = mraStatus.isValid;

    if (!hasMRAMapping) {
      const mappingWarning = !mraStatus.hasMapping
        ? '⚠️ No MRA Mapping'
        : (!mraStatus.isApproved ? '⚠️ MRA Pending Approval' : '⚠️ MRA Not Synced');
      return {
        text: mappingWarning,
        // Let the add-to-cart handler do the final backend-aware validation.
        // This avoids dead product clicks when the local mapping cache is stale.
        canAddToCart: hasStock,
        hasMRAMapping: false,
        stockTone: hasStock ? 'warning' : 'out',
      };
    }
    
    // If has MRA mapping, show stock info
    return {
      text: stockText,
      canAddToCart: hasStock,
      hasMRAMapping: true,
      stockTone: hasStock ? 'available' : 'out',
    };
  };

  const renderProductGrid = () => {
    const itemsToDisplay = displayItems || inventory.filter(item => item.itemType === 'sellable');

    if (itemsToDisplay.length === 0) {
      return (
        <div className="flex h-full flex-col items-center justify-center text-center">
          <Package className="h-16 w-16 text-muted-foreground/30" />
          <p className="mt-4 text-muted-foreground">{emptyStateTitle}</p>
          {emptyStateDescription ? (
            <p className="mt-2 text-sm text-muted-foreground">{emptyStateDescription}</p>
          ) : null}
        </div>
      );
    }
    
    return (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
        {itemsToDisplay.map((item) => {
          const { text: stockInfo, canAddToCart, stockTone } = getStockInfo(item);
          return (
            <div
              key={item.id}
              onClick={async (e) => {
                e.stopPropagation();
                if (canAddToCart) {
                  await onAddToCart(item);
                }
              }}
              role="button"
              tabIndex={0}
              onKeyDown={async (e) => {
                if ((e.key === 'Enter' || e.key === ' ') && canAddToCart) {
                  e.preventDefault();
                  await onAddToCart(item);
                }
              }}
            >
              <ProductCard
                item={item}
                onAddToCart={() => {}}
                productIcon={productIcon}
                currencyFormatter={formatCurrency}
                canAddToCart={canAddToCart}
                stockInfo={stockInfo}
                stockTone={stockTone}
              />
            </div>
          );
        })}
      </div>
    );
  };

  const renderProductList = () => {
    const itemsToDisplay = displayItems || inventory.filter(item => item.itemType === 'sellable');

    if (itemsToDisplay.length === 0) {
      return (
        <div className="flex h-full flex-col items-center justify-center text-center">
          <Package className="h-16 w-16 text-muted-foreground/30" />
          <p className="mt-4 text-muted-foreground">{emptyStateTitle}</p>
          {emptyStateDescription ? (
            <p className="mt-2 text-sm text-muted-foreground">{emptyStateDescription}</p>
          ) : null}
        </div>
      );
    }
    
    return (
      <div className="space-y-2">
        {itemsToDisplay.map((item) => {
          const { text: stockInfo, canAddToCart, stockTone } = getStockInfo(item);
          return (
            <div 
              key={item.id} 
              className={cn(
                "flex cursor-pointer flex-col gap-3 rounded-md border p-3 hover:bg-muted sm:flex-row sm:items-center sm:gap-4 sm:p-2",
                !canAddToCart && "opacity-50 cursor-not-allowed"
              )} 
              onClick={async () => canAddToCart && await onAddToCart(item)}
              role="button"
              tabIndex={0}
              onKeyDown={async (event) => {
                if ((event.key === 'Enter' || event.key === ' ') && canAddToCart) {
                  event.preventDefault();
                  await onAddToCart(item);
                }
              }}
            >
              <div className="flex min-w-0 items-start gap-3 sm:flex-1 sm:items-center">
                <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md bg-muted/50 sm:h-12 sm:w-12">
                  {productIcon}
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate font-semibold">{item.name}</p>
                  <p className="truncate text-sm text-muted-foreground">{item.category}</p>
                  <p className={cn(
                    "mt-1 text-xs font-medium",
                    stockTone === 'available'
                      ? "text-green-600"
                      : stockTone === 'warning'
                        ? "text-amber-600"
                        : "text-red-600"
                  )}>
                    {stockInfo}
                  </p>
                </div>
              </div>
              <p className="text-right font-bold text-primary sm:w-32">{formatCurrency(item.price || 0)}</p>
            </div>
          );
        })}
      </div>
    );
  };

  const renderCartItems = () => {
    if (cart.length === 0) {
      return (
        <div className="flex flex-col items-center justify-center text-center h-full">
          <ShoppingBasket className="h-16 w-16 text-muted-foreground/30" />
          <p className="mt-4 text-muted-foreground">Select products to start a new sale.</p>
        </div>
      );
    }
    return (
      <div className="space-y-2">
        {cart.map((item) => (
          <CartItemView
            key={item.id}
            item={item}
            onUpdateQuantity={onUpdateQuantity}
            onApplyDiscount={onApplyDiscount}
            currencyFormatter={formatCurrency}
            taxDetail={cartSummary.perItemTax[String(item.id)]}
            showTaxStatus={false}
            discountRules={discountRules.filter((rule) => discountAppliesToItem(rule, item))}
          />
        ))}
      </div>
    );
  };

  const renderCartFooter = () => (
    <div className="tauri-android-safe-bottom flex flex-col gap-4 bg-muted/50 p-4">
      <div className="space-y-1 text-sm">
        <div className="flex w-full items-center justify-between gap-2">
          <span className="flex-shrink-0 text-muted-foreground">Subtotal (Excl VAT)</span>
          <span className="flex-shrink-0 text-right">{formatCurrency(subtotal)}</span>
        </div>
        {cart.some((item) => resolveCartDiscountAmount(item) > 0) && (
          <div className="flex w-full items-center justify-between gap-2">
            <span className="flex-shrink-0 text-muted-foreground">Discount</span>
            <span className="flex-shrink-0 text-right text-green-700 dark:text-green-500">
              -{formatCurrency(cart.reduce((sum, item) => sum + resolveCartDiscountAmount(item), 0))}
            </span>
          </div>
        )}
        <div className="flex w-full items-center justify-between gap-2">
          <span className="flex-shrink-0 text-muted-foreground">{cartTaxLabel}</span>
          <span className="flex-shrink-0 text-right font-semibold text-green-600">{formatCurrency(tax)}</span>
        </div>
      </div>
      <div className="flex w-full items-center justify-between gap-2 text-lg font-bold">
        <span className="flex-shrink-0">Total (Incl VAT)</span>
        <span className="flex-shrink-0 text-right">{formatCurrency(total)}</span>
      </div>
      {isEisInvoiceSubmissionBlocked && (
        <div className="flex items-start gap-2 rounded-md border border-yellow-300 bg-yellow-50 px-3 py-2 text-sm text-yellow-900 dark:border-yellow-800 dark:bg-yellow-950/30 dark:text-yellow-100">
          <WifiOff className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="min-w-0">{eisInvoiceSubmissionBlockedMessage}</span>
        </div>
      )}
      {invalidDiscountItems.length > 0 && (
        <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-900 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-100">
          Discount must be less than item total.
        </div>
      )}
      <Button
        size="lg"
        className="bg-green-600 hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-60"
        onClick={() => { setPaymentSessionId((id) => id + 1); setPaymentDialogOpen(true); }}
        disabled={isEisInvoiceSubmissionBlocked || invalidDiscountItems.length > 0}
      >
        <CreditCard className="mr-2 h-5 w-5" /> Payment
      </Button>
    </div>
  );

  const renderDesktopCart = () => (
    <Card className="flex h-full flex-col overflow-hidden">
      <CardHeader className="flex flex-row items-center justify-between border-b p-2 shrink-0">
        <CardTitle className="text-base">Current Order</CardTitle>
        <div className="flex items-center gap-2">
            <Button variant="ghost" size="icon" className="text-destructive h-8 w-8" onClick={onClearCart}><Trash2 className="h-4 w-4" /></Button>
        </div>
      </CardHeader>
      <div className="flex-1 min-h-0 overflow-hidden">
        <div className="h-full overflow-y-scroll hide-scrollbar p-4">
          {renderCartItems()}
        </div>
      </div>
      <div className="shrink-0 border-t">{renderCartFooter()}</div>
    </Card>
  );

  const renderMobileCartDialog = () => (
    <Dialog>
      <DialogTrigger asChild>
        <Button size="lg" className="tauri-android-floating-bottom fixed bottom-4 right-4 z-10 h-14 w-auto rounded-full shadow-lg lg:hidden">
          <span>View Cart ({cart.reduce((acc, item) => acc + item.quantity, 0)})</span>
          <Separator orientation="vertical" className="mx-3 h-6" />
          <span className="font-bold">{formatCurrency(total)}</span>
        </Button>
      </DialogTrigger>
      <DialogContent className="tauri-android-safe-bottom tauri-android-sidebar-safe-top left-0 top-0 m-0 flex h-screen h-[100dvh] max-h-screen max-h-[100dvh] w-full max-w-full translate-x-0 translate-y-0 flex-col gap-0 overflow-hidden rounded-none border-0 p-0 sm:left-1/2 sm:top-1/2 sm:h-auto sm:max-h-[90vh] sm:max-w-full sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-lg sm:border">
        <DialogHeader className="p-4 border-b">
          <div className="flex items-center justify-between">
            <DialogTitle>Current Order</DialogTitle>
            <DialogClose asChild>
              <Button variant="ghost" size="icon" onClick={onClearCart}><Trash2 className="text-destructive" /></Button>
            </DialogClose>
          </div>
          
        </DialogHeader>
        <div className="flex-1 overflow-y-auto p-4">{renderCartItems()}</div>
        <div className="mt-auto border-t">{renderCartFooter()}</div>
      </DialogContent>
    </Dialog>
  );
  
  const handlePaymentDialogClose = () => {
    setPaymentDialogOpen(false);
  }

  return (
    <>
    <div
      className={cn(
        'grid h-full w-full grid-cols-1 gap-6 transition-all duration-300 min-h-0',
        hasItemsInCart && 'lg:grid-cols-[1fr_420px]'
      )}
    >
      <Card className="h-full w-full overflow-hidden min-h-0">
        <CardContent className="h-full w-full overflow-y-scroll overflow-x-hidden hide-scrollbar p-4 min-h-0">
          {viewMode === 'grid' ? renderProductGrid() : renderProductList()}
        </CardContent>
      </Card>
      
      {hasItemsInCart && (
        <div className="hidden lg:flex lg:flex-col h-full w-full min-h-0">
          {renderDesktopCart()}
        </div>
      )}

      {hasItemsInCart && renderMobileCartDialog()}
    </div>
    <Dialog open={isPaymentDialogOpen} onOpenChange={setPaymentDialogOpen}>
        <PaymentDialog 
            subtotal={subtotal}
            tax={tax}
            taxLabel={taxLabel}
            defaultTaxRate={defaultTaxRate}
            onCheckout={onCheckout}
            onClose={handlePaymentDialogClose}
            currencyFormatter={formatCurrency}
            resetToken={paymentSessionId}
            cart={cart}
            eisEnabled={eisEnabled}
            blockSalesIfTaxMappingMissing={blockSalesIfTaxMappingMissing}
            isEisInvoiceSubmissionBlocked={isEisInvoiceSubmissionBlocked}
            eisInvoiceSubmissionBlockedMessage={eisInvoiceSubmissionBlockedMessage}
            branchId={branchId}
            onConfigurePrinter={() => setShowPrinterConfig(true)}
        />
    </Dialog>
    <PrinterConfigModal isOpen={showPrinterConfig} onOpenChange={setShowPrinterConfig} />
    </>
  );
};
