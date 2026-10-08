

'use client';

import React, { useState, useMemo, useEffect } from 'react';
import { useLiveQuery } from 'dexie-react-hooks';
import { Search, LayoutGrid, List, AlertTriangle, Loader2, Printer, Barcode, Camera } from 'lucide-react';
import { useRouter } from 'next/navigation';

import { db, type InventoryItem, type Order, type Session, type TaxRate } from '@/lib/db';
import { type BusinessType } from '@/lib/inventory/config';
import { PharmacyPos } from '@/components/pos/pharmacy-pos';
import { RestaurantPos } from '@/components/pos/restaurant-pos';
import { BarLiquorPos } from '@/components/pos/bar-liquor-pos';
import { SupermarketPos } from '@/components/pos/supermarket-pos';
import { GroceryPos } from '@/components/pos/grocery-pos';
import { BeautySalonPos } from '@/components/pos/beauty-salon-pos';
import type { BuyerDetails } from '@/components/pos/generic-pos';
import { TakeOrdersPanel } from '@/components/pos/take-orders-panel';
import { PrinterConfigModal } from '@/components/pos/printer-config-modal';
import { ScannerConfigModal } from '@/components/pos/scanner-config-modal';
import { CameraBarcodeScannerModal } from '@/components/pos/camera-barcode-scanner-modal';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useToast } from '@/hooks/use-toast';
import { useAuth } from '@/hooks/use-auth';
import { getBackendConnectionIssue, useBackendReachability } from '@/hooks/use-backend-reachability';
import { authFetch } from '@/lib/auth-fetch';
import { isEisSaleNotCreated } from '@/lib/eis-submission';
import { v4 as uuidv4 } from 'uuid';
import {
  saveSaleToLocalStorage,
  addPendingSale,
  markSaleAsSynced,
  markSaleAsFailed,
  removeRejectedSaleLocally,
} from '@/lib/services/sales-service';
import { syncInventoryFromBackend } from '@/lib/services/inventory-sync';
import { isTauriApp } from '@/lib/tauri-init';
import { logAuditAction } from '@/lib/audit';
import { warmBranchMraMappingCache } from '@/lib/mra-mapping-cache';
import { isWarehouseBranchId } from '@/lib/branch-context';

export type CartItem = InventoryItem & {
  quantity: number;
  price: number;
  notes?: string;
  // Preserve original inventory item ID for cart entries that need a unique line ID.
  inventoryItemId?: string;
  discountRuleId?: string;
  discount_rule_id?: string;
  discountName?: string;
  discount_name?: string;
  discountType?: 'percentage' | 'fixed' | string;
  discount_type?: 'percentage' | 'fixed' | string;
  discountValue?: number;
  discount_value?: number;
  discountAmount?: number;
  discount_amount?: number;
};
export type PaymentMethod = Order['paymentMethod'];

const LOCAL_STORAGE_KEYS = {
    ACTIVE_BRANCH: 'handypos-active-branch'
};

const normalizeBranchId = (value?: string | number | null): string => {
  const normalized = String(value ?? '').trim();
  if (!normalized) return '';

  const brnMatch = /^BRN-(\d+)$/i.exec(normalized);
  if (brnMatch) return brnMatch[1];

  const legacyMatch = /^branch-(\d+)$/i.exec(normalized);
  if (legacyMatch) return legacyMatch[1];

  return normalized;
};

const getBranchIdCandidates = (branchId?: string | number | null): string[] => {
  const normalized = normalizeBranchId(branchId);
  if (!normalized) return [];

  const candidates = new Set<string>([normalized, String(branchId ?? '').trim()]);
  if (/^\d+$/.test(normalized)) {
    candidates.add(`BRN-${normalized}`);
    candidates.add(`branch-${normalized}`);
  }

  return Array.from(candidates).filter((candidate) => candidate.length > 0);
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

  const currentUpdatedAt = new Date(
    current.updatedAt ||
      current.updated_at ||
      current.lastSyncedAt ||
      current.last_synced_at ||
      current.createdAt ||
      current.created_at ||
      0
  ).getTime();
  const candidateUpdatedAt = new Date(
    candidate.updatedAt ||
      candidate.updated_at ||
      candidate.lastSyncedAt ||
      candidate.last_synced_at ||
      candidate.createdAt ||
      candidate.created_at ||
      0
  ).getTime();

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

const isServiceInventoryItem = (item: Partial<InventoryItem> | null | undefined): boolean => {
  const category = String((item as any)?.category || '').trim().toLowerCase();
  return category.includes('service');
};

const resolveMappingBranchId = (mapping: any): string => {
  return normalizeBranchId(
    mapping?.branchId ??
      mapping?.branch_id ??
      mapping?.branch
  );
};

const filterMappingsForBranch = (mappings: any[], branchId?: string | number | null): any[] => {
  const normalizedBranchId = normalizeBranchId(branchId);
  const shouldScopeByBranch =
    Boolean(normalizedBranchId) &&
    !['main', 'main-branch', 'main_branch'].includes(normalizedBranchId.toLowerCase());

  return mappings.filter((mapping) => {
    const mappingBranchId = resolveMappingBranchId(mapping);
    if (!mappingBranchId) {
      return true;
    }
    if (!shouldScopeByBranch) {
      return true;
    }
    return mappingBranchId === normalizedBranchId;
  });
};

const pickPreferredMapping = (mappings: any[]): any => {
  let preferred: any = undefined;
  for (const mapping of mappings) {
    preferred = choosePreferredMapping(preferred, mapping);
  }
  return preferred;
};

const extractMappingsFromResponse = (response: any): any[] => {
  if (Array.isArray(response)) {
    return response;
  }

  if (Array.isArray(response?.results)) {
    return response.results;
  }

  return [];
};

type NormalizedTaxType = 'standard' | 'zero' | 'exempt';
type NormalizedTaxCalculationMethod = 'inclusive' | 'exclusive';

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

const normalizeTaxCalculationMethod = (value: unknown): NormalizedTaxCalculationMethod => {
  return String(value || '').trim().toLowerCase() === 'exclusive' ? 'exclusive' : 'inclusive';
};

const toFiniteNumber = (value: unknown, fallback = 0): number => {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const toPositiveNumber = (value: unknown, fallback = 0): number => {
  const parsed = toFiniteNumber(value, fallback);
  return parsed > 0 ? parsed : fallback;
};

const toNonNegativeNumber = (value: unknown, fallback = 0): number => {
  const parsed = toFiniteNumber(value, fallback);
  return parsed >= 0 ? parsed : fallback;
};

const resolveCartLineTotal = (cartItem: CartItem): number => {
  const price = toNonNegativeNumber(cartItem.price, 0);
  const quantity = toPositiveNumber(cartItem.quantity, 0);
  return cartItem.isVariablePrice ? price : price * quantity;
};

const formatStockQuantity = (value: unknown): string => {
  const quantity = toNonNegativeNumber(value, 0);
  if (Math.abs(quantity - Math.round(quantity)) < 0.0001) {
    return String(Math.round(quantity));
  }
  return quantity.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
};

const resolvePurchaseVatAmount = (batch: any, grossTotal: number): number => {
  const providedTaxAmount = toFiniteNumber(batch?.taxAmount, Number.NaN);
  if (Number.isFinite(providedTaxAmount)) {
    return providedTaxAmount;
  }
  const taxRate = toNonNegativeNumber(batch?.taxRate, 0);
  if (taxRate <= 0 || grossTotal <= 0) return 0;
  const divisor = 1 + taxRate / 100;
  if (divisor <= 0) return 0;
  return grossTotal - grossTotal / divisor;
};

const resolveNetUnitCostFromBatch = (batch: any): number => {
  const receivedQty = toNonNegativeNumber(batch?.quantityReceived, 0);
  const fallbackQty = receivedQty > 0 ? receivedQty : toNonNegativeNumber(batch?.quantityRemaining, 0);
  const unitCost = toNonNegativeNumber(batch?.costPerUnit, 0);
  if (fallbackQty <= 0) return unitCost;

  const grossTotalCandidate = toFiniteNumber(batch?.totalCost, Number.NaN);
  const grossTotal = Number.isFinite(grossTotalCandidate) ? grossTotalCandidate : unitCost * fallbackQty;
  const vatAmount = resolvePurchaseVatAmount(batch, grossTotal);
  const netTotal = Math.max(0, grossTotal - vatAmount);
  return netTotal / fallbackQty;
};

const toBoolean = (value: unknown, fallback = false): boolean => {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (['true', '1', 'yes', 'y'].includes(normalized)) return true;
    if (['false', '0', 'no', 'n'].includes(normalized)) return false;
  }
  return fallback;
};

const normalizeText = (value: unknown): string => String(value ?? '').trim().toLowerCase();

const isAllProductType = (value: string): boolean =>
  value === '' || value === 'all' || value === 'all products' || value === 'all items';

const resolveBlockSalesIfTaxMappingMissing = (source: any): boolean | null => {
  if (!source || typeof source !== 'object') return null;

  const rawBlock = source.blockSalesIfTaxMappingMissing ?? source.block_sales_if_tax_mapping_missing;
  if (rawBlock !== undefined) {
    return toBoolean(rawBlock, false);
  }

  const rawAllow = source.allowSalesWithoutTaxMapping ?? source.allow_sales_without_tax_mapping;
  if (rawAllow !== undefined) {
    return !toBoolean(rawAllow, false);
  }

  return null;
};

const resolveEnableEis = (source: any): boolean | null => {
  if (!source || typeof source !== 'object') return null;

  const raw =
    source.enableEis ??
    source.enable_eis ??
    source.eisEnabled ??
    source.eis_enabled;
  if (raw !== undefined) {
    return toBoolean(raw, false);
  }

  return null;
};

const resolveTaxpayerVatRegistered = (source: any): boolean | null => {
  if (!source || typeof source !== 'object') return null;

  const rawVatRegistered = source.vatRegistered ?? source.vat_registered;
  if (rawVatRegistered !== undefined) {
    return toBoolean(rawVatRegistered, false);
  }

  const taxpayerType = String(source.mraTaxpayerType ?? source.mra_taxpayer_type ?? '').trim().toUpperCase();
  if (taxpayerType === 'VAT') return true;
  if (taxpayerType === 'NON_VAT') return false;

  return null;
};

const extractBackendErrorMessage = (errorData: any, fallback: string): string => {
  const readValue = (value: unknown): string => {
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) return value.map(readValue).filter(Boolean).join(', ');
    if (value && typeof value === 'object') {
      return Object.values(value).map(readValue).filter(Boolean).join(', ');
    }
    return '';
  };

  const message =
    readValue(errorData?.error) ||
    readValue(errorData?.detail) ||
    readValue(errorData?.message) ||
    readValue(errorData?.non_field_errors);

  return message || fallback;
};

const cleanUserFacingOrderError = (message: string): string => {
  const raw = String(message || '').trim();
  if (!raw) return 'Sale could not be completed. Please try again.';

  if (/backend rejected order|http \d+|mra request failed|traceback|stack|nameresolutionerror|max retries exceeded/i.test(raw)) {
    return 'Sale could not be completed. Please try again.';
  }

  return raw.length > 180 ? `${raw.slice(0, 177).trim()}...` : raw;
};

const isRetryBlockedBackendOrderError = (statusCode: number, message: string): boolean => {
  if (statusCode < 400 || statusCode >= 500) return false;

  const normalized = message.toLowerCase();
  return [
    'mra',
    'eis',
    'non-vat',
    'standard vat',
    'tax mapping',
    'compliance policy',
    'cannot submit an empty pos order',
  ].some((needle) => normalized.includes(needle));
};

const isConfirmedMraRejection = (payload: any): boolean => {
  const diagnostic = payload?.mra_submission ?? payload?.mraSubmission;
  return (
    String(payload?.reason || '').trim().toLowerCase() === 'eis_rejected' ||
    String(payload?.eis_status ?? payload?.eisStatus ?? '').trim().toUpperCase() === 'REJECTED' ||
    String(diagnostic?.state || '').trim().toLowerCase() === 'rejected' ||
    String(diagnostic?.eis_status ?? diagnostic?.eisStatus ?? '').trim().toUpperCase() === 'REJECTED'
  );
};

const isReceiptValidationUrl = (value: unknown): boolean => {
  const raw = String(value ?? '').trim();
  return /^https?:\/\//i.test(raw) || /receiptvalidation\/validate/i.test(raw);
};

const findReceiptValidationUrl = (source: unknown): string => {
  const queue: unknown[] = [source];
  const seen = new Set<unknown>();

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
      if (raw.startsWith('{') || raw.startsWith('[')) {
        try {
          queue.push(JSON.parse(raw));
        } catch {
          // Ignore non-JSON strings.
        }
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

    for (const value of Object.values(current as Record<string, unknown>)) {
      if (isReceiptValidationUrl(value)) {
        return String(value).trim();
      }
      if (value && typeof value === 'object') {
        queue.push(value);
      } else if (typeof value === 'string' && (value.trim().startsWith('{') || value.trim().startsWith('['))) {
        try {
          queue.push(JSON.parse(value.trim()));
        } catch {
          // Ignore non-JSON strings.
        }
      }
    }
  }

  return '';
};

const findReceiptStringByKeys = (source: unknown, keys: string[]): string => {
  const wantedKeys = new Set(keys.map((key) => key.replace(/[^a-z0-9]/gi, '').toLowerCase()));
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
      if (raw.startsWith('{') || raw.startsWith('[')) {
        try {
          queue.push(JSON.parse(raw));
        } catch {
          // Ignore non-JSON strings.
        }
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
      if (wantedKeys.has(normalizeKey(key)) && value !== undefined && value !== null && String(value).trim() !== '') {
        return String(value).trim();
      }
      if (value && typeof value === 'object') {
        queue.push(value);
      } else if (typeof value === 'string' && (value.trim().startsWith('{') || value.trim().startsWith('['))) {
        try {
          queue.push(JSON.parse(value.trim()));
        } catch {
          // Ignore non-JSON strings.
        }
      }
    }
  }

  return '';
};

const hasFiscalReceiptData = (order: Partial<Order> | Record<string, any> | null | undefined): boolean => {
  if (!order) {
    return false;
  }

  const fiscalNumber = findReceiptStringByKeys(order, [
    'fiscalInvoiceNumber',
    'fiscal_invoice_number',
    'invoiceNumber',
    'invoice_number',
    'receiptNumber',
    'receipt_number',
  ]);
  const validationUrl = findReceiptValidationUrl(order);
  const signature = findReceiptStringByKeys(order, [
    'digitalSignature',
    'digital_signature',
    'offlineSignature',
    'offline_signature',
    'invoiceSignature',
    'invoice_signature',
    'signature',
  ]);

  return Boolean(fiscalNumber && (validationUrl || signature));
};

const readBackendOrderField = (source: any, keys: string[]): any => {
  const preferredKeys = new Set(keys.map((key) => key.replace(/[^a-z0-9]/gi, '').toLowerCase()));
  const normalizeKey = (key: string): string => key.replace(/[^a-z0-9]/gi, '').toLowerCase();
  const queue: any[] = [source];
  const seen = new Set<any>();

  while (queue.length > 0) {
    const candidate = queue.shift();
    if (candidate === null || candidate === undefined) {
      continue;
    }

    if (typeof candidate === 'string') {
      const raw = candidate.trim();
      if (raw.startsWith('{') || raw.startsWith('[')) {
        try {
          queue.push(JSON.parse(raw));
        } catch {
          // Ignore non-JSON strings.
        }
      }
      continue;
    }

    if (typeof candidate !== 'object' || seen.has(candidate)) {
      continue;
    }
    seen.add(candidate);

    if (Array.isArray(candidate)) {
      queue.push(...candidate);
      continue;
    }

    for (const [key, value] of Object.entries(candidate)) {
      if (preferredKeys.has(normalizeKey(key)) && value !== undefined && value !== null && String(value).trim() !== '') {
        return value;
      }
      if (value && typeof value === 'object') {
        queue.push(value);
      } else if (typeof value === 'string') {
        const raw = value.trim();
        if (raw.startsWith('{') || raw.startsWith('[')) {
          try {
            queue.push(JSON.parse(raw));
          } catch {
            // Ignore non-JSON strings.
          }
        }
      }
    }
  }

  return undefined;
};

const normalizeBackendOrderResponse = (data: any) => {
  const fiscalInvoiceNumber = readBackendOrderField(data, [
    'fiscal_invoice_number',
    'fiscalInvoiceNumber',
    'invoiceNumber',
    'receiptNumber',
  ]);
  const eisStatus = readBackendOrderField(data, ['eis_status', 'eisStatus', 'status']);
  const eisUuid = readBackendOrderField(data, ['eis_uuid', 'eisUuid', 'uuid']);
  const eisSubmittedAt = readBackendOrderField(data, ['eis_submitted_at', 'eisSubmittedAt', 'submittedAt']);
  const eisSyncState = readBackendOrderField(data, ['eis_sync_state', 'eisSyncState']);
  const fiscalReceiptState = readBackendOrderField(data, ['fiscal_receipt_state', 'fiscalReceiptState']);
  const fiscalReceiptReady = readBackendOrderField(data, ['fiscal_receipt_ready', 'fiscalReceiptReady']);
  const qrCodePayload =
    readBackendOrderField(data, [
      'qr_code_payload',
      'qrCodePayload',
      'validationUrl',
      'validationURL',
      'offline_validation_url',
      'offlineValidationUrl',
      'offlineValidationURL',
      'mraValidationUrl',
      'mraValidationURL',
    ]) ||
    findReceiptValidationUrl(data);
  const digitalSignature = readBackendOrderField(data, [
    'digital_signature',
    'digitalSignature',
    'offline_signature',
    'offlineSignature',
    'offline_signature_hash',
    'offlineSignatureHash',
    'invoiceSignature',
    'signature',
  ]);
  const netAmount = readBackendOrderField(data, ['net_amount', 'netAmount']);
  const vatAmount = readBackendOrderField(data, ['vat_amount', 'vatAmount', 'tax']);
  const grossAmount = readBackendOrderField(data, ['gross_amount', 'grossAmount', 'total']);
  const eisValidationMetadata =
    data?.eis_validation_metadata ??
    data?.eisValidationMetadata ??
    data?.order?.eis_validation_metadata ??
    data?.order?.eisValidationMetadata ??
    data?.data?.eis_validation_metadata ??
    data?.data?.eisValidationMetadata;
  const mraSubmission =
    data?.mra_submission ??
    data?.mraSubmission ??
    eisValidationMetadata?.mra_submission ??
    eisValidationMetadata?.mraSubmission ??
    data?.order?.mra_submission ??
    data?.order?.mraSubmission;

  return {
    fiscalInvoiceNumber,
    eisStatus,
    eisSyncState,
    fiscalReceiptState,
    fiscalReceiptReady,
    eisUuid,
    eisSubmittedAt,
    qrCodePayload,
    digitalSignature,
    netAmount,
    vatAmount,
    grossAmount,
    eisValidationMetadata,
    mraSubmission,
  };
};


export default function PosPage() {
  const [currentBusinessType, setCurrentBusinessType] = useState<BusinessType>('Restaurant');
  const [cart, setCart] = useState<CartItem[]>([]);
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid');
  const [takeOrderIdsInCart, setTakeOrderIdsInCart] = useState<string[]>([]);
  const [showPrinterConfig, setShowPrinterConfig] = useState(false);
  const [showScannerConfig, setShowScannerConfig] = useState(false);
  const [showCameraScanner, setShowCameraScanner] = useState(false);
  const [isAndroidTauri, setIsAndroidTauri] = useState(false);
  const [blockSalesIfTaxMappingMissing, setBlockSalesIfTaxMappingMissing] = useState(false);
  const [eisEnabled, setEisEnabled] = useState(false);
  const {
    isReachable: isBackendReachable,
    error: backendReachabilityError,
    checkNow: checkBackendConnectionNow,
  } = useBackendReachability({ intervalMs: 10000 });
  const [isBackendUnavailableStable, setIsBackendUnavailableStable] = useState(false);
  const [taxpayerVatRegistered, setTaxpayerVatRegistered] = useState<boolean | null>(null);
  const { toast } = useToast();
  const router = useRouter();
  const { user, business } = useAuth();
  const [activeBranchId, setActiveBranchId] = useState<string | null>(null);
  
  useEffect(() => {
    const branchId = localStorage.getItem(LOCAL_STORAGE_KEYS.ACTIVE_BRANCH);
    setActiveBranchId(branchId || 'main');
  }, []);

  useEffect(() => {
    if (typeof navigator !== 'undefined' && navigator.onLine === false) {
      setIsBackendUnavailableStable(true);
      return;
    }

    if (isBackendReachable) {
      setIsBackendUnavailableStable(false);
      return;
    }

    const timeoutId = window.setTimeout(() => {
      setIsBackendUnavailableStable(true);
    }, 12000);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [isBackendReachable]);

  useEffect(() => {
    if (!business?.id) return;

    const applyCachedPolicy = () => {
      if (typeof window === 'undefined') return;
      try {
        const cachedSettingsRaw = window.localStorage.getItem('handypos-business-settings');
        if (!cachedSettingsRaw) return;
        const parsed = JSON.parse(cachedSettingsRaw);
        const storedBusinessId = String(parsed?.businessId || '').trim();
        if (storedBusinessId && storedBusinessId !== String(business.id)) {
          return;
        }
        const cachedValue = resolveBlockSalesIfTaxMappingMissing(parsed);
        if (cachedValue !== null) {
          setBlockSalesIfTaxMappingMissing(cachedValue);
        }
        const cachedEisValue = resolveEnableEis(parsed);
        if (cachedEisValue !== null) {
          setEisEnabled(cachedEisValue);
        }
        const cachedVatRegistered = resolveTaxpayerVatRegistered(parsed);
        if (cachedVatRegistered !== null) {
          setTaxpayerVatRegistered(cachedVatRegistered);
        }
      } catch (error) {
        console.warn('[POS Page] Failed to parse cached tax mapping policy:', error);
      }
    };

    const loadPolicyFromBackend = async () => {
      applyCachedPolicy();
      try {
        const backendBusiness = await authFetch.fetch<any>(`/business/businesses/${business.id}/`);
        const backendValue = resolveBlockSalesIfTaxMappingMissing(backendBusiness);
        if (backendValue !== null) {
          setBlockSalesIfTaxMappingMissing(backendValue);
        }
        const backendEisValue = resolveEnableEis(backendBusiness);
        if (backendEisValue !== null) {
          setEisEnabled(backendEisValue);
        }
        const backendVatRegistered = resolveTaxpayerVatRegistered(backendBusiness);
        if (backendVatRegistered !== null) {
          setTaxpayerVatRegistered(backendVatRegistered);
        }
      } catch (error) {
        console.warn('[POS Page] Failed to fetch tax mapping policy from backend:', error);
      }
    };

    loadPolicyFromBackend();
  }, [business?.id]);

  useEffect(() => {
    const detectAndroidTauri = () => {
      const ua = navigator.userAgent || '';
      const isAndroidUserAgent = /android/i.test(ua);
      const hasAndroidTauriAttr =
        document.documentElement.getAttribute('data-tauri-android') === 'true';
      const isAndroidTauriRuntime =
        hasAndroidTauriAttr || (isTauriApp() && isAndroidUserAgent);
      // Fallback to Android UA so camera scanning remains accessible even if Tauri markers
      // are delayed or unavailable on some WebView builds.
      setIsAndroidTauri(isAndroidTauriRuntime || isAndroidUserAgent);
    };

    detectAndroidTauri();
    const retryTimer = window.setTimeout(detectAndroidTauri, 600);
    return () => window.clearTimeout(retryTimer);
  }, []);

  // Sync inventory and MRA mappings from backend when page loads
  useEffect(() => {
    if (activeBranchId && !isWarehouseBranchId(activeBranchId)) {
      console.log('[POS Page] Syncing inventory and MRA mappings for branch:', activeBranchId);
      syncInventoryFromBackend(activeBranchId).then(result => {
        console.log('[POS Page] Inventory sync result:', result);
        if (result.error) {
          console.warn('[POS Page] Sync error:', result.error);
        }
      });
    }
  }, [activeBranchId]);

  // Load business type from database
  useEffect(() => {
    const loadBusinessType = async () => {
      if (business?.id) {
        const businessData = await db.business.get(business.id);
        if (businessData?.type) {
          const typeMap: Record<string, BusinessType> = {
            pharmacy: 'Pharmacy',
            restaurant: 'Restaurant',
            bar_liquor: 'Bar & Liquor',
            'bar & liquor': 'Bar & Liquor',
            supermarket: 'Supermarket',
            grocery: 'Grocery',
            beauty_salon: 'Beauty Salon and Spa',
            'beauty salon and spa': 'Beauty Salon and Spa',
            general_retail: 'General Retail',
            'general retail': 'General Retail',
            generic: 'General Retail',
          };
          const mappedType = typeMap[String(businessData.type).toLowerCase()] || 'Grocery';
          setCurrentBusinessType(mappedType);
        }
      }
    };
    loadBusinessType();
  }, [business?.id]);

  const resolveActiveSessionForUser = async (): Promise<Session | null> => {
    if (!activeBranchId || !user?.uid) return null;

    const normalizedActiveBranchId = normalizeBranchId(activeBranchId);
    const currentUserId = String(user.uid);
    const currentUserEmail = String(user.email || '').trim().toLowerCase();
    const activeSessions = await db.sessions
      .where('status')
      .equals('active')
      .toArray();

    return activeSessions
      .filter((session) => {
        if (normalizeBranchId(session.branchId) !== normalizedActiveBranchId) {
          return false;
        }

        const sessionUserId = String(session.userId || '');
        const sessionUserEmail = String(session.userEmail || '').trim().toLowerCase();
        return sessionUserId === currentUserId || (currentUserEmail !== '' && sessionUserEmail === currentUserEmail);
      })
      .sort((a, b) => new Date(b.startedAt).getTime() - new Date(a.startedAt).getTime())[0] ?? null;
  };

  const activeSession = useLiveQuery(
    async () => resolveActiveSessionForUser(),
    [activeBranchId, user?.uid, user?.email],
    null
  );
  
  const allInventory = useLiveQuery(
    () => {
      if (!activeBranchId) return [];
      const candidates = getBranchIdCandidates(activeBranchId);
      if (candidates.length === 0) return [];
      if (candidates.length === 1) {
        return db.inventory.where({ branchId: candidates[0] }).toArray().then((items) => (
          items.filter((item) => item._operation !== 'delete')
        ));
      }
      return db.inventory.where('branchId').anyOf(candidates).toArray().then((items) => (
        items.filter((item) => item._operation !== 'delete')
      ));
    },
    [activeBranchId]
  );

  const localMraMappingByItemId = useLiveQuery(
    async () => {
      if (!activeBranchId) return new Map<string, any>();
      const mappings = await db.mraMappings.toArray();
      return buildMappingLookup(filterMappingsForBranch(mappings, activeBranchId));
    },
    [activeBranchId],
    new Map<string, any>()
  );

  useEffect(() => {
    if (!activeBranchId || (!eisEnabled && !blockSalesIfTaxMappingMissing)) {
      return;
    }

    const inventoryItemIds = (allInventory || [])
      .filter((item) => item.itemType === 'sellable')
      .map((item) => String(item.id || '').trim())
      .filter((itemId) => itemId.length > 0);

    if (inventoryItemIds.length === 0) {
      return;
    }

    let cancelled = false;

    const warmMraCache = async () => {
      const result = await warmBranchMraMappingCache({
        branchId: activeBranchId,
        inventoryItemIds,
        logPrefix: '[POS Page]',
      });

      if (!cancelled && result.refreshed) {
        console.log('[POS Page] MRA cache warm result:', result);
      }
    };

    void warmMraCache();

    return () => {
      cancelled = true;
    };
  }, [activeBranchId, allInventory, blockSalesIfTaxMappingMissing, eisEnabled]);
  
  const defaultTaxRate = useLiveQuery(
    async () => {
      if (!business?.id) return null;

      const taxes = await db.taxes
        .where('businessId')
        .equals(String(business.id))
        .toArray();

      const activeTaxes = taxes.filter((tax) => tax.isActive !== false);
      const defaultTax = activeTaxes.find((tax) => tax.isDefault);
      if (defaultTax) return defaultTax;

      return activeTaxes
        .sort((a, b) => {
          const timeA = Date.parse(a.updatedAt || a.createdAt || '');
          const timeB = Date.parse(b.updatedAt || b.createdAt || '');
          return (Number.isFinite(timeB) ? timeB : 0) - (Number.isFinite(timeA) ? timeA : 0);
        })[0] ?? null;
    },
    [business?.id],
    null
  );


  const sellableItems = useMemo(
    () => {
      if (!allInventory) return [];
      const isCashier = user?.role === 'Cashier';
      const isFuelAttendant = Boolean(user?.isFuelAttendant);

      // Show all sellable items regardless of onMenu status
      return allInventory.filter((item) => {
        if (item.itemType !== 'sellable') return false;
        const isFuelItem = Boolean(item.isFuel);
        if (isFuelAttendant && !isFuelItem) return false;
        if (!isFuelAttendant && isFuelItem) return false;
        return true;
      });
    },
    [allInventory, currentBusinessType, user?.role, user?.isFuelAttendant]
  );
  
  const resolveInventoryItemId = (cartItem: CartItem): string => {
    if (cartItem.inventoryItemId) return String(cartItem.inventoryItemId);

    const rawId = String(cartItem.id);
    if (allInventory?.some((inventoryItem) => String(inventoryItem.id) === rawId)) {
      return rawId;
    }

    // Backward-compatibility for legacy cart IDs stored as "<inventoryId>-<timestamp>".
    const matchedInventoryId = allInventory
      ?.map((inventoryItem) => String(inventoryItem.id))
      .filter((inventoryId) => rawId.startsWith(`${inventoryId}-`))
      .sort((a, b) => b.length - a.length)[0];

    return matchedInventoryId || rawId;
  };

  const findInventoryItemById = (inventoryItemId: string): InventoryItem | undefined => {
    const normalizedId = String(inventoryItemId || '').trim();
    if (!normalizedId) return undefined;
    return allInventory?.find((inventoryItem) => String(inventoryItem.id) === normalizedId);
  };

  const isServiceSaleItem = (item: InventoryItem | CartItem): boolean => {
    if (isServiceInventoryItem(item)) return true;
    const itemId = String((item as CartItem).inventoryItemId || item.id || '').trim();
    const mapping = itemId ? localMraMappingByItemId?.get(itemId) : null;
    return mapping?.isProduct === false || mapping?.is_product === false;
  };

  const getStockTargetsForSaleLine = (item: InventoryItem, quantity: number) => {
    const requestedQuantity = toPositiveNumber(quantity, 0);
    if (requestedQuantity <= 0) return [];
    if (isServiceSaleItem(item)) return [];

    if (item.itemType === 'sellable' && item.isProduced && Array.isArray(item.recipe) && item.recipe.length > 0) {
      return item.recipe
        .map((recipeItem: any) => {
          const ingredientId = String(recipeItem?.ingredientId || recipeItem?.ingredient_id || recipeItem?.id || '').trim();
          const perUnitQuantity = toPositiveNumber(recipeItem?.quantity, 0);
          if (!ingredientId || perUnitQuantity <= 0) return null;
          return {
            id: ingredientId,
            name: String(recipeItem?.name || '').trim(),
            quantity: perUnitQuantity * requestedQuantity,
          };
        })
        .filter(Boolean) as Array<{ id: string; name: string; quantity: number }>;
    }

    return [{ id: String(item.id), name: item.name, quantity: requestedQuantity }];
  };

  const getCartStockIssues = (cartSnapshot: CartItem[]) => {
    const requirements = new Map<string, { name: string; quantity: number }>();

    for (const cartItem of cartSnapshot) {
      const inventoryItemId = resolveInventoryItemId(cartItem);
      const inventoryItem = findInventoryItemById(inventoryItemId) || cartItem;
      const targets = getStockTargetsForSaleLine(inventoryItem, cartItem.quantity);

      for (const target of targets) {
        const existing = requirements.get(target.id) || { name: target.name, quantity: 0 };
        requirements.set(target.id, {
          name: existing.name || target.name,
          quantity: existing.quantity + target.quantity,
        });
      }
    }

    const issues: string[] = [];
    requirements.forEach((requirement, targetId) => {
      const inventoryItem = findInventoryItemById(targetId);
      const available = toNonNegativeNumber(inventoryItem?.stockUnits, 0);
      if (!inventoryItem || requirement.quantity > available + 0.0001) {
        const name = inventoryItem?.name || requirement.name || 'Product';
        issues.push(
          `${name}: ${formatStockQuantity(available)} available, ${formatStockQuantity(requirement.quantity)} requested.`
        );
      }
    });

    return issues;
  };

  const showStockIssueToast = (issue: string) => {
    toast({
      variant: 'destructive',
      title: 'Insufficient stock',
      description: issue,
    });
  };

  const handleAddToCart = async (item: InventoryItem, quantity: number = 1, price?: number, notes?: string, takeOrderId?: string) => {
    if (blockSalesIfTaxMappingMissing) {
      // ALWAYS check if product has APPROVED AND SYNCED MRA mapping
      // Backend requires BOTH is_approved AND mra_synced to be true for sale
      try {
        console.log('[POS Page] Checking MRA mapping for product:', item.id);
        
        let isReadyForSale = false;
        let mappingStatus = 'unknown';
        
        // First try local database
        try {
          const itemId = String(item.id || '').trim();
          const directLocalMappings = await db.mraMappings
            .where('inventoryItemId')
            .equals(itemId)
            .toArray();
          let localMapping = pickPreferredMapping(filterMappingsForBranch(directLocalMappings, activeBranchId));

          if (!localMapping) {
            const allMappings = await db.mraMappings.toArray();
            const scopedMappings = filterMappingsForBranch(allMappings, activeBranchId);
            const lookup = buildMappingLookup(scopedMappings);
            localMapping = lookup.get(itemId);
          }

          const localApproved = Boolean(localMapping?.isApproved ?? localMapping?.is_approved);
          const localSynced = Boolean(localMapping?.mraSynced ?? localMapping?.mra_synced);
          
          if (localMapping && localApproved && localSynced) {
            console.log('[POS Page] ✓ Found APPROVED & SYNCED MRA mapping in local database for:', item.name);
            isReadyForSale = true;
            mappingStatus = 'ready';
          } else if (localMapping && !localApproved) {
            console.log('[POS Page] ⚠ MRA mapping found but NOT APPROVED for:', item.name);
            mappingStatus = 'pending';
          } else if (localMapping && !localSynced) {
            console.log('[POS Page] ⚠ MRA mapping found but NOT SYNCED for:', item.name);
            mappingStatus = 'unsynced';
          } else {
            console.log('[POS Page] ℹ No MRA mapping in local database for:', item.name);
            mappingStatus = 'missing';
          }
        } catch (dbError) {
          console.warn('[POS Page] Error checking local database:', dbError);
          mappingStatus = 'missing';
        }
        
        // If not ready locally, try API to get latest status
        if (!isReadyForSale && isBackendReachable) {
          try {
            const backendBranchId = normalizeBranchId(activeBranchId);
            let mappings: any[] = [];

            if (backendBranchId) {
              const scopedResponse = await authFetch.fetch<any>(
                `/inventory/mra-mappings/?inventory_item=${encodeURIComponent(String(item.id))}&branch_id=${encodeURIComponent(backendBranchId)}`
              );
              mappings = extractMappingsFromResponse(scopedResponse);
            }

            let readyMapping = mappings.find(
              (m) => Boolean(m.is_approved ?? m.isApproved) && Boolean(m.mra_synced ?? m.mraSynced)
            );

            if (!readyMapping) {
              const fallbackResponse = await authFetch.fetch<any>(
                `/inventory/mra-mappings/?inventory_item=${encodeURIComponent(String(item.id))}`
              );
              const fallbackMappings = extractMappingsFromResponse(fallbackResponse);
              if (fallbackMappings.length > 0) {
                mappings = [...mappings, ...fallbackMappings];
                readyMapping = mappings.find(
                  (m) => Boolean(m.is_approved ?? m.isApproved) && Boolean(m.mra_synced ?? m.mraSynced)
                );
              }
            }

            console.log('[POS Page] API response for MRA mappings:', mappings.length, 'mappings found');

            if (mappings.length === 0) {
              console.log('[POS Page] ✗ No MRA mappings found in API for product:', item.id);
              mappingStatus = 'missing';
              isReadyForSale = false;
            } else {
              // Check if any mapping is BOTH approved AND synced
              if (readyMapping) {
                console.log('[POS Page] ✓ Found APPROVED & SYNCED MRA mapping from API for:', item.name);
                isReadyForSale = true;
                mappingStatus = 'ready';

                // Cache backend-verified mapping locally to avoid repeat API checks.
                try {
                  const taxType = readyMapping.mra_tax_type === 'zero' || readyMapping.mra_tax_type === 'exempt'
                    ? readyMapping.mra_tax_type
                    : (readyMapping.mraTaxType === 'zero' || readyMapping.mraTaxType === 'exempt' ? readyMapping.mraTaxType : 'standard');
                  const calculationMethod = String(
                    readyMapping.tax_calculation_method ??
                    readyMapping.taxCalculationMethod ??
                    readyMapping.calculation_method ??
                    readyMapping.calculationMethod ??
                    ''
                  ).trim().toLowerCase().startsWith('excl')
                    ? 'exclusive'
                    : 'inclusive';
                  const nowIso = new Date().toISOString();
                  const mappingItemId = resolveMappingInventoryItemId(readyMapping) || String(item.id);

                  await db.mraMappings.put({
                    id: String(readyMapping.id || `${mappingItemId}-mapping`),
                    inventoryItemId: mappingItemId,
                    branchId: normalizeBranchId(
                      readyMapping.branch ??
                      readyMapping.branch_id ??
                      backendBranchId
                    ) || undefined,
                    mraProductCode: readyMapping.mra_product_code || readyMapping.mraProductCode || '',
                    mraProductName: readyMapping.mra_product_name || readyMapping.mraProductName || item.name,
                    mraTaxType: taxType,
                    mraTaxRate: Number(readyMapping.mra_tax_rate ?? readyMapping.mraTaxRate ?? 0),
                    mraUnitMeasure: readyMapping.mra_unit_measure || readyMapping.mraUnitMeasure || '',
                    taxCalculationMethod: calculationMethod,
                    isProduct: readyMapping.is_product ?? readyMapping.isProduct ?? true,
                    isApproved: Boolean(readyMapping.is_approved ?? readyMapping.isApproved),
                    approvedAt: readyMapping.approved_at || readyMapping.approvedAt || undefined,
                    mraSynced: Boolean(readyMapping.mra_synced ?? readyMapping.mraSynced),
                    lastSyncedAt: readyMapping.last_synced_at || readyMapping.lastSyncedAt || nowIso,
                    createdAt: readyMapping.created_at || readyMapping.createdAt || nowIso,
                    updatedAt: nowIso,
                  });
                } catch (cacheError) {
                  console.warn('[POS Page] Failed to cache MRA mapping after API check:', cacheError);
                }
              } else {
                // Check what's wrong
                const approvedButNotSynced = mappings.find(
                  (m) => Boolean(m.is_approved ?? m.isApproved) && !Boolean(m.mra_synced ?? m.mraSynced)
                );
                if (approvedButNotSynced) {
                  console.log('[POS Page] ⚠ MRA mapping APPROVED but NOT SYNCED for:', item.name);
                  mappingStatus = 'unsynced';
                } else {
                  console.log('[POS Page] ⚠ MRA mappings found but NONE are approved for:', item.name);
                  mappingStatus = 'pending';
                }
              }
            }
          } catch (error) {
            console.error('[POS Page] Error checking MRA mapping from API:', error);
            // If API fails and we're online, don't allow the sale
            toast({
              variant: 'destructive',
              title: 'Mapping check failed',
              description: 'Try again.',
            });
            return;
          }
        }
        
        // Block sale if not ready for sale
        if (!isReadyForSale) {
          let errorTitle = 'Mapping required';
          let errorDescription = `${item.name}: mapping required.`;
          
          if (mappingStatus === 'pending') {
            errorTitle = 'Mapping pending';
            errorDescription = `${item.name}: approve mapping.`;
          } else if (mappingStatus === 'unsynced') {
            errorTitle = 'Mapping not synced';
            errorDescription = `${item.name}: sync mapping.`;
          } else if (mappingStatus === 'missing') {
            errorTitle = 'Mapping missing';
            errorDescription = `${item.name}: map product.`;
          }
          
          toast({
            variant: 'destructive',
            title: errorTitle,
            description: errorDescription,
          });
          console.log('[POS Page] ✗ BLOCKED add to cart - MRA mapping not ready for:', item.name, '(status:', mappingStatus + ')');
          return;
        }
      } catch (error) {
        console.error('[POS Page] Unexpected error checking MRA mapping:', error);
        toast({
          variant: 'destructive',
          title: 'Mapping check failed',
          description: 'Try again.',
        });
        return;
      }
    } else {
      console.log('[POS Page] Tax mapping enforcement disabled, skipping MRA mapping validation for:', item.name);
    }

    const existingItemIndex = cart.findIndex((cartItem) => {
      const inventoryItemId = String(cartItem.inventoryItemId || cartItem.id);
      return inventoryItemId === String(item.id) && !cartItem.notes;
    });

    const itemPrice = price !== undefined ? price : (item.price || 0);
    const effectiveQuantity = toPositiveNumber(quantity, 1);
    let nextCart: CartItem[];

    if (existingItemIndex > -1 && !item.isVariablePrice && !notes) {
      nextCart = cart.map((cartItem, index) => (
        index === existingItemIndex
          ? { ...cartItem, quantity: cartItem.quantity + effectiveQuantity }
          : cartItem
      ));
    } else {
      const cartLineId = (!item.isVariablePrice && !notes)
        ? item.id
        : `${item.id}::cart::${Date.now()}`;
      const cartItem: CartItem = {
        ...item,
        id: cartLineId,
        inventoryItemId: item.id,
        quantity: effectiveQuantity,
        price: itemPrice,
        notes,
      };
      nextCart = [...cart, cartItem];
    }

    const stockIssues = getCartStockIssues(nextCart);
    if (stockIssues.length > 0) {
      showStockIssueToast(stockIssues[0]);
      return;
    }

    setCart(nextCart);

    if (takeOrderId && !takeOrderIdsInCart.includes(takeOrderId)) {
      setTakeOrderIdsInCart(prev => [...prev, takeOrderId]);
    }

    // Provide immediate tactile feedback after a successful add-to-cart action.
    if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
      navigator.vibrate(45);
    }
  };
  
  const handleUpdateQuantity = (itemId: string, newQuantity: number) => {
    const nextCart = newQuantity <= 0
      ? cart.filter((cartItem) => cartItem.id !== itemId)
      : cart.map((cartItem) =>
          cartItem.id === itemId
            ? {
                ...cartItem,
                quantity: newQuantity,
              }
            : cartItem
        );

    const stockIssues = getCartStockIssues(nextCart);
    if (stockIssues.length > 0) {
      showStockIssueToast(stockIssues[0]);
      return;
    }

    if (newQuantity <= 0) {
      setCart(nextCart);
    } else {
      setCart(nextCart);
    }
  };

  const handleClearCart = () => setCart([]);

  const normalizeBarcodeValue = (value: string): string => value.trim().replace(/\s+/g, '');

  const handleCameraBarcodeDetected = async (barcode: string): Promise<boolean> => {
    const normalizedScannedBarcode = normalizeBarcodeValue(barcode);
    if (!normalizedScannedBarcode) {
      return false;
    }

    const matchedProduct = sellableItems.find((item) => {
      const itemBarcode = normalizeBarcodeValue(String(item.barcode || ''));
      return itemBarcode !== '' && itemBarcode === normalizedScannedBarcode;
    });

    if (!matchedProduct) {
      toast({
        variant: 'destructive',
        title: 'Product Not Found',
        description: `No sellable product found with barcode: ${barcode}`,
      });
      return false;
    }

    await handleAddToCart(matchedProduct, 1);
    return true;
  };

  const handleScannerAction = () => {
    if (isAndroidTauri) {
      setShowCameraScanner(true);
      return;
    }

    setShowScannerConfig(true);
  };

  const handleCreateOrder = async (paymentMethod: PaymentMethod, tip: number, buyerDetails?: BuyerDetails): Promise<Order | null> => {
    void tip;
    const appliedTip = 0;
    if (isWarehouseBranchId(activeBranchId)) {
      toast({
        variant: 'destructive',
        title: 'Warehouse selected',
        description: 'Switch to a branch to make sales.',
      });
      return null;
    }
    if (!cart.length) {
      toast({ variant: 'destructive', title: 'Cart is empty' });
      return null;
    }
    const cartStockIssues = getCartStockIssues(cart);
    if (cartStockIssues.length > 0) {
      showStockIssueToast(cartStockIssues[0]);
      return null;
    }
    if (!activeBranchId) {
       toast({ variant: 'destructive', title: 'No active branch', description: 'Could not determine the active branch.' });
       return null;
    }
    const sessionForOrder = await resolveActiveSessionForUser();
    if (!sessionForOrder) {
      toast({ variant: 'destructive', title: 'No active session', description: 'Please start a session to record sales.' });
      return null;
    }

    const buyerName = buyerDetails?.name?.trim();
    const buyerPhone = buyerDetails?.phone?.trim();
    const buyerTin = buyerDetails?.tin?.trim();
    const buyerAuthorizationCode = buyerDetails?.authorizationCode?.trim();
    const vat5ProjectNumber = buyerDetails?.vat5ProjectNumber?.trim();
    const vat5CertificateNumber = buyerDetails?.vat5CertificateNumber?.trim();
    const vat5Quantity = Number(buyerDetails?.vat5Quantity);
    const buyerFields: Partial<Order> = {};
    const buyerPayload: Record<string, string | number | boolean> = {};

    if (buyerName) {
      buyerFields.customerName = buyerName;
      buyerFields.customer_name = buyerName;
      buyerPayload.customer_name = buyerName;
    }
    if (buyerPhone) {
      buyerFields.customerPhone = buyerPhone;
      buyerFields.customer_phone = buyerPhone;
      buyerPayload.customer_phone = buyerPhone;
    }
    if (buyerTin) {
      buyerFields.customerTin = buyerTin;
      buyerFields.customer_tin = buyerTin;
      buyerFields.buyerTin = buyerTin;
      buyerFields.buyer_tin = buyerTin;
      buyerPayload.customer_tin = buyerTin;
      buyerPayload.buyer_tin = buyerTin;
    }
    if (buyerAuthorizationCode) {
      buyerFields.buyerAuthorizationCode = buyerAuthorizationCode;
      buyerFields.buyer_authorization_code = buyerAuthorizationCode;
      buyerPayload.buyer_authorization_code = buyerAuthorizationCode;
    }
    if (buyerDetails?.isExport) {
      buyerFields.isExport = true;
      buyerFields.is_export = true;
      buyerPayload.is_export = true;
    }
    if (buyerDetails?.isReliefSupply) {
      buyerFields.isReliefSupply = true;
      buyerFields.is_relief_supply = true;
      buyerPayload.is_relief_supply = true;
    }
    if (vat5ProjectNumber) {
      buyerFields.vat5ProjectNumber = vat5ProjectNumber;
      buyerFields.vat5_project_number = vat5ProjectNumber;
      buyerPayload.vat5_project_number = vat5ProjectNumber;
    }
    if (vat5CertificateNumber) {
      buyerFields.vat5CertificateNumber = vat5CertificateNumber;
      buyerFields.vat5_certificate_number = vat5CertificateNumber;
      buyerPayload.vat5_certificate_number = vat5CertificateNumber;
    }
    if (Number.isFinite(vat5Quantity) && vat5Quantity > 0) {
      buyerFields.vat5Quantity = vat5Quantity;
      buyerFields.vat5_quantity = vat5Quantity;
      buyerPayload.vat5_quantity = vat5Quantity;
    }

    const defaultTaxRateAmount = defaultTaxRate ? defaultTaxRate.rate / 100 : 0;
    let shouldUseMappings = eisEnabled || blockSalesIfTaxMappingMissing === true;
    let shouldEnforceTaxMapping = shouldUseMappings;
    let mappingByItemId = new Map<string, any>();

    if (shouldUseMappings) {
      try {
        const normalizedBranchId = normalizeBranchId(activeBranchId);
        const shouldScopeByBranch =
          Boolean(normalizedBranchId) &&
          !['main', 'main-branch', 'main_branch'].includes(normalizedBranchId.toLowerCase());
        const localMappings = await db.mraMappings.toArray();
        const scopedMappings = localMappings.filter((mapping) => {
          const mappingBranchId = normalizeBranchId(
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
          return mappingBranchId === normalizedBranchId;
        });
        mappingByItemId = buildMappingLookup(scopedMappings);
      } catch (mappingError) {
        console.warn('[POS Page] Failed to load local MRA mappings; EIS sales will be blocked if mappings are unavailable:', mappingError);
        shouldUseMappings = eisEnabled || blockSalesIfTaxMappingMissing === true;
        shouldEnforceTaxMapping = shouldUseMappings;
        mappingByItemId = new Map<string, any>();
      }
    }

    const unmappedProducts: string[] = [];
    let totalTax = 0;
    let totalNet = 0;
    let totalGross = 0;

    const computedLineItems = cart.map((cartItem) => {
      const quantity = toPositiveNumber(cartItem.quantity, 0);
      const unitPriceRaw = toNonNegativeNumber(cartItem.price, 0);
      const lineAmount = resolveCartLineTotal(cartItem);

      let lineTax = 0;
      let lineNet = lineAmount;
      let lineGross = lineAmount;
      let taxRate = 0;
      let taxType: NormalizedTaxType = 'standard';
      let taxCalculationMethod: NormalizedTaxCalculationMethod = 'inclusive';

      if (shouldUseMappings) {
        const primaryInventoryItemId = String(cartItem.inventoryItemId || '').trim();
        const fallbackInventoryItemId = resolveCartInventoryItemId(cartItem) || String(cartItem.id || '').trim();
        const preferredMappingKey = primaryInventoryItemId || fallbackInventoryItemId;
        let localMapping = mappingByItemId.get(preferredMappingKey);
        if (!localMapping && fallbackInventoryItemId && fallbackInventoryItemId !== preferredMappingKey) {
          localMapping = mappingByItemId.get(fallbackInventoryItemId);
        }

        const isApproved = Boolean(localMapping?.isApproved ?? localMapping?.is_approved);
        const isSynced = Boolean(localMapping?.mraSynced ?? localMapping?.mra_synced);

        if (localMapping && isApproved && isSynced) {
          taxType = normalizeMappedTaxType(localMapping.mraTaxType || localMapping.mra_tax_type);
          taxRate = toFiniteNumber(localMapping.mraTaxRate ?? localMapping.mra_tax_rate ?? 0, 0);
          taxCalculationMethod = normalizeTaxCalculationMethod(
            localMapping.taxCalculationMethod || localMapping.tax_calculation_method
          );
          const effectiveRate = taxRate / 100;

          if (taxType === 'zero' || taxType === 'exempt' || effectiveRate <= 0) {
            lineTax = 0;
            lineNet = lineAmount;
            lineGross = lineAmount;
          } else if (taxCalculationMethod === 'exclusive') {
            lineTax = lineAmount * effectiveRate;
            lineNet = lineAmount;
            lineGross = lineAmount + lineTax;
          } else {
            lineTax = effectiveRate > 0 ? (lineAmount * effectiveRate) / (1 + effectiveRate) : 0;
            lineGross = lineAmount;
            lineNet = lineAmount - lineTax;
          }
        } else {
          const hasLocalMapping = Boolean(localMapping);
          const reasonSuffix = hasLocalMapping ? ' (mapping pending approval/sync)' : '';
          if (shouldEnforceTaxMapping) {
            unmappedProducts.push(`${cartItem.name}${reasonSuffix}`);
          }
          if (!shouldEnforceTaxMapping && defaultTaxRateAmount > 0) {
            taxRate = defaultTaxRateAmount * 100;
            taxType = 'standard';
            taxCalculationMethod = 'inclusive';
            lineTax = (lineAmount * defaultTaxRateAmount) / (1 + defaultTaxRateAmount);
            lineGross = lineAmount;
            lineNet = lineAmount - lineTax;
          }
        }
      } else if (defaultTaxRateAmount > 0) {
        taxRate = defaultTaxRateAmount * 100;
        taxType = 'standard';
        taxCalculationMethod = 'inclusive';
        lineTax = (lineAmount * defaultTaxRateAmount) / (1 + defaultTaxRateAmount);
        lineGross = lineAmount;
        lineNet = lineAmount - lineTax;
      }

      totalTax += lineTax;
      totalNet += lineNet;
      totalGross += lineGross;

      const unitPrice = quantity > 0 ? lineAmount / quantity : unitPriceRaw;

      return {
        cartItem,
        inventoryItemId: resolveInventoryItemId(cartItem),
        quantity,
        unitPrice,
        lineNet,
        lineTax,
        lineGross,
        taxRate,
        taxType,
        taxCalculationMethod,
      };
    });

    if (shouldEnforceTaxMapping && unmappedProducts.length > 0) {
      toast({
        variant: 'destructive',
        title: 'Product Setup Required',
        description: `Cannot complete sale. Setup missing for: ${unmappedProducts.join(', ')}`,
      });
      return null;
    }

    const subtotal = Number(totalNet.toFixed(2));
    const tax = Number(totalTax.toFixed(2));
    const total = Number(totalGross.toFixed(2));
    let orderCogs = 0;
    let finalOrder: Order | null = null;
    let orderForBackend: any = null;
    const localInventoryConsumption: Array<{
      inventoryItemId: string;
      quantity: number;
      purchaseHistoryId?: string | number;
    }> = [];

    try {
      await db.transaction('rw', db.inventory, db.orders, db.sessions, db.purchaseHistory, async () => {
        // 1. Decrement stock using FIFO
        for (const cartItem of cart) {
          const originalItemId = resolveInventoryItemId(cartItem);
          const originalItem = await db.inventory
            .where('branchId')
            .equals(activeBranchId)
            .filter(i => String(i.id) === String(originalItemId))
            .first();

          if (!originalItem) {
            throw new Error(`Inventory item not found for sale line ${originalItemId}.`);
          }

          const cartQuantity = toPositiveNumber(cartItem.quantity, 0);
          if (cartQuantity <= 0) {
            throw new Error(`Invalid quantity for ${cartItem.name}.`);
          }
          if (isServiceSaleItem(originalItem)) {
            continue;
          }

          const itemsToDecrement = (
            originalItem.itemType === 'sellable' &&
            originalItem.isProduced &&
            originalItem.recipe?.length
          )
            ? originalItem.recipe
                .map(ri => {
                  const ingredientId = String(ri?.ingredientId || '');
                  const ingredientQty = toPositiveNumber(ri?.quantity, 0);
                  return {
                    id: ingredientId,
                    quantity: ingredientQty * cartQuantity,
                  };
                })
                .filter(entry => entry.id && entry.quantity > 0)
            : [{ id: originalItemId, quantity: cartQuantity }];

          for (const itemToDecrement of itemsToDecrement) {
            let quantityToDecrement = toPositiveNumber(itemToDecrement.quantity, 0);
            if (quantityToDecrement <= 0) {
              console.warn('[Order] Skipping invalid item decrement quantity:', itemToDecrement);
              continue;
            }

            const inventoryItemToUpdate = await db.inventory
              .where('branchId')
              .equals(activeBranchId)
              .filter(item => String(item.id) === String(itemToDecrement.id))
              .first();

            if (!inventoryItemToUpdate) {
              throw new Error(`Inventory item not found for ${itemToDecrement.id}.`);
            }

            let batches = await db.purchaseHistory
              .where({ branchId: activeBranchId, productId: itemToDecrement.id as any })
              .and(batch => (batch.quantityRemaining || 0) > 0)
              .toArray();

            // Fallback handles string/number mismatches for product IDs.
            if (batches.length === 0) {
              batches = await db.purchaseHistory
                .where('branchId')
                .equals(activeBranchId)
                .filter(batch =>
                  String(batch.productId) === String(itemToDecrement.id) &&
                  (batch.quantityRemaining || 0) > 0
                )
                .toArray();
            }

            const sortedBatches = batches.sort(
              (a, b) => new Date(a.receivedDate).getTime() - new Date(b.receivedDate).getTime()
            );

            const availableBatchQuantity = sortedBatches.reduce(
              (sum, batch) => sum + toNonNegativeNumber(batch.quantityRemaining, 0),
              0
            );
            const availableQuantity = sortedBatches.length > 0
              ? availableBatchQuantity
              : toNonNegativeNumber(inventoryItemToUpdate.stockUnits, 0);
            if (quantityToDecrement > availableQuantity + 0.0001) {
              throw new Error(
                `Insufficient stock: ${inventoryItemToUpdate.name} has ${formatStockQuantity(availableQuantity)}, ` +
                `requested ${formatStockQuantity(quantityToDecrement)}.`
              );
            }

            let totalDecrementedFromBatches = 0;
            for (const batch of sortedBatches) {
              if (quantityToDecrement <= 0) break;

              const batchQuantityRemaining = toNonNegativeNumber(batch.quantityRemaining, 0);
              if (batchQuantityRemaining <= 0) continue;

              const decrementAmount = Math.min(quantityToDecrement, batchQuantityRemaining);
              if (!Number.isFinite(decrementAmount) || decrementAmount <= 0) continue;

              await db.purchaseHistory.update(batch.id!, {
                quantityRemaining: Math.max(0, batchQuantityRemaining - decrementAmount),
                _dirty: true,
                _operation: 'update',
              });
              localInventoryConsumption.push({
                inventoryItemId: String(itemToDecrement.id),
                quantity: decrementAmount,
                purchaseHistoryId: batch.id,
              });

              const netUnitCost = resolveNetUnitCostFromBatch(batch);
              orderCogs += decrementAmount * netUnitCost;

              quantityToDecrement -= decrementAmount;
              totalDecrementedFromBatches += decrementAmount;
            }

            // If batches are missing/partial, deduct remaining quantity directly from inventory.
            let fallbackInventoryDecrement = 0;
            if (quantityToDecrement > 0) {
              const currentItemStock = toNonNegativeNumber(inventoryItemToUpdate.stockUnits, 0);
              const availableAfterBatch = Math.max(
                0,
                currentItemStock - totalDecrementedFromBatches
              );
              fallbackInventoryDecrement = Math.min(quantityToDecrement, availableAfterBatch);
              quantityToDecrement -= fallbackInventoryDecrement;

              if (fallbackInventoryDecrement > 0) {
                orderCogs += fallbackInventoryDecrement * toNonNegativeNumber(inventoryItemToUpdate.cost, 0);
                localInventoryConsumption.push({
                  inventoryItemId: String(itemToDecrement.id),
                  quantity: fallbackInventoryDecrement,
                });
              }
            }

            const totalInventoryDecrement = totalDecrementedFromBatches + fallbackInventoryDecrement;
            if (totalInventoryDecrement > 0) {
              const currentStock = toNonNegativeNumber(inventoryItemToUpdate.stockUnits, 0);
              const newStock = Math.max(0, currentStock - totalInventoryDecrement);
              const reorderLevel = inventoryItemToUpdate.reorderLevel || 0;
              const status = newStock <= 0
                ? 'Out of Stock'
                : newStock <= reorderLevel
                  ? 'Low Stock'
                  : 'In Stock';

              await db.inventory.update(inventoryItemToUpdate.id, {
                stockUnits: newStock,
                status,
                _dirty: true,
                _operation: 'update',
              });

              console.log('[Sync] Updated inventory item stock after sale:', inventoryItemToUpdate.id, 'decremented:', totalInventoryDecrement, 'new stock:', newStock);
            }

            if (quantityToDecrement > 0) {
              console.warn('[Order] Sale consumed more stock than tracked quantity for item:', itemToDecrement.id, 'remaining unmet quantity:', quantityToDecrement);
            }
          }
        }

        // 2. Create the order with UUID
        const existingBranchOrders = await db.orders.where('branchId').equals(activeBranchId).toArray();
        const maxKnownOrderNumber = existingBranchOrders.reduce((maxValue, orderRecord) => {
          const candidates = [
            Number((orderRecord as any).orderNumber),
            Number((orderRecord as any).order_number),
          ];
          for (const candidate of candidates) {
            if (Number.isFinite(candidate) && candidate > maxValue) {
              maxValue = candidate;
            }
          }
          return maxValue;
        }, 100);
        const nextOrderNumber = maxKnownOrderNumber + 1;
        const isKitchenOrder = currentBusinessType === 'Restaurant' || currentBusinessType === 'Bar & Liquor' || user?.role === 'Waiter';

        const newOrder: Order = {
          id: uuidv4(),
          orderNumber: nextOrderNumber,
          branchId: activeBranchId,
          sessionId: sessionForOrder.id,
          pumpName: sessionForOrder.pumpName,
          orderType: 'sale',  // Mark as POS sale
          items: computedLineItems.map((line) => ({
            id: uuidv4(),
            inventoryItemId: line.inventoryItemId,
            name: line.cartItem.name,
            quantity: line.quantity,
            price: Number(line.unitPrice.toFixed(2)),
            notes: line.cartItem.notes || '',
            taxRate: Number(line.taxRate.toFixed(2)),
            taxType: line.taxType,
            taxCalculationMethod: line.taxCalculationMethod,
            subtotal: Number(line.lineNet.toFixed(2)),
            taxAmount: Number(line.lineTax.toFixed(2)),
            total: Number(line.lineGross.toFixed(2)),
          })),
          status: isKitchenOrder ? 'New' : 'Completed',
          paymentMethod: paymentMethod,
          ...buyerFields,
          subtotal,
          tax,
          tip: appliedTip,
          total,
          cogs: orderCogs,
          localInventoryConsumption: localInventoryConsumption.length > 0
            ? localInventoryConsumption
            : undefined,
          eis_status: eisEnabled ? 'PENDING' : undefined,
          eisStatus: eisEnabled ? 'PENDING' : undefined,
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
        };

        // Mark order as dirty for sync
        const orderWithSync: Order = {
          ...newOrder,
          _dirty: true,
          _operation: 'create'
        };
        await db.orders.add(orderWithSync);
        finalOrder = orderWithSync;
        console.log('[Sync] Marked order as dirty:', newOrder.id);

        // 3. Update the session with sync flags
        const sessionUpdate: Partial<Session> = {
            totalSales: (sessionForOrder.totalSales || 0) + subtotal,
            totalTips: sessionForOrder.totalTips || 0,
            _dirty: true,
            _operation: 'update'
        };

        const saleAmount = total;
        switch(paymentMethod) {
            case 'Cash':
                sessionUpdate.totalCashSales = (sessionForOrder.totalCashSales || 0) + saleAmount;
                sessionUpdate.expectedCash = (sessionForOrder.expectedCash || 0) + saleAmount;
                break;
            case 'Card':
                 sessionUpdate.totalCardSales = (sessionForOrder.totalCardSales || 0) + saleAmount;
                 break;
            case 'Mobile Money':
                 sessionUpdate.totalMobileMoneySales = (sessionForOrder.totalMobileMoneySales || 0) + saleAmount;
                 break;
            case 'On Account':
                 sessionUpdate.totalOnAccountSales = (sessionForOrder.totalOnAccountSales || 0) + saleAmount;
                 break;
            case 'Other':
                 sessionUpdate.totalOtherSales = (sessionForOrder.totalOtherSales || 0) + saleAmount;
                 break;
        }

        await db.sessions.update(sessionForOrder.id, sessionUpdate);
        console.log('[Sync] Marked session as dirty:', sessionForOrder.id);
      });

      if (finalOrder && user) {
        await logAuditAction({
          userId: user.uid,
          userName: user.displayName || user.email || 'System',
          branchId: finalOrder.branchId,
          actionType: 'ORDER_CREATE',
          entityType: 'Order',
          entityId: finalOrder.id,
          details: {
            orderNumber: finalOrder.orderNumber,
            total: finalOrder.total,
            paymentMethod: finalOrder.paymentMethod,
            items: finalOrder.items.length,
          },
        });
      }

      // 4. Build backend order data AFTER transaction completes
      if (finalOrder && sessionForOrder) {
        orderForBackend = {
          id: finalOrder.id,
          order_number: finalOrder.orderNumber,
          order_type: finalOrder.orderType,
          status: finalOrder.status,
          payment_method: finalOrder.paymentMethod,
          pump_name: finalOrder.pumpName,
          ...buyerPayload,
          subtotal: finalOrder.subtotal,
          tax: finalOrder.tax,
          tip: finalOrder.tip,
          total: finalOrder.total,
          cogs: finalOrder.cogs,
          session: sessionForOrder.id,
          branch: sessionForOrder.branchId, // Use session's branchId which is the UUID
          // CRITICAL: Include items with prices so backend can calculate totals
          items: computedLineItems.map((line) => ({
            id: uuidv4(),
            inventoryItemId: line.inventoryItemId,
            name: line.cartItem.name,
            quantity: line.quantity,
            price: Number(line.unitPrice.toFixed(2)),
            subtotal: Number(line.lineNet.toFixed(2)),
            taxAmount: Number(line.lineTax.toFixed(2)),
            total: Number(line.lineGross.toFixed(2)),
            taxRate: Number(line.taxRate.toFixed(2)),
            taxType: line.taxType,
            taxCalculationMethod: line.taxCalculationMethod,
            notes: line.cartItem.notes || '',
          }))
        };
        console.log('[Order] Built backend order:', orderForBackend);
      }

      // 5. Save sale to localStorage with branch information
      if (finalOrder) {
        saveSaleToLocalStorage(finalOrder, activeBranchId);
        addPendingSale(finalOrder);
      }

      // 6. Submit to backend. EIS sales must wait for fiscal receipt data before the success screen.
      if (finalOrder && orderForBackend) {
        const finalOrderId = finalOrder.id;
        const payload = orderForBackend;

        const submitOrderToBackend = async (): Promise<Order | null> => {
          try {
            console.log('[Order] Sending to backend with correct format:', JSON.stringify(payload));
            
            const fullUrl = `${process.env.NEXT_PUBLIC_API_URL || 'https://pos3.express-travel-ticketing.online/api'}/sessions/orders/`;
            const headers: Record<string, string> = {
              'Content-Type': 'application/json',
            };
            
            const tokensStr = localStorage.getItem('handypos-auth-tokens');
            if (tokensStr) {
              const tokens = JSON.parse(tokensStr);
              if (tokens.access) {
                headers.Authorization = `Bearer ${tokens.access}`;
              }
            }
            
            const response = await fetch(fullUrl, {
              method: 'POST',
              headers,
              body: JSON.stringify(payload),
            });
            
            if (response.ok) {
              const data = await response.json();
              console.log('[Order] Successfully synced order to backend:', finalOrderId, data);
              const normalizedResponse = normalizeBackendOrderResponse(data);
              const mraSubmission = normalizedResponse.mraSubmission;
              const mraSubmissionState = String(mraSubmission?.state || '').trim().toLowerCase();
              const eisSyncState = String(normalizedResponse.eisSyncState || '').trim().toUpperCase();
              const fiscalReceiptState = String(normalizedResponse.fiscalReceiptState || '').trim().toUpperCase();
              const fiscalReceiptReady = normalizedResponse.fiscalReceiptReady === true;
              const mraSubmissionMessage = String(mraSubmission?.message || '').trim();
              console.info('[Order] Fiscal response state:', {
                orderId: finalOrderId,
                mraSubmissionState,
                eisSyncState,
                fiscalReceiptState,
                fiscalReceiptReady,
                queuedOffline: mraSubmission?.queued_offline ?? mraSubmission?.queuedOffline,
                fiscalInvoiceNumber: normalizedResponse.fiscalInvoiceNumber,
                hasQrCodePayload: Boolean(normalizedResponse.qrCodePayload),
                hasDigitalSignature: Boolean(normalizedResponse.digitalSignature),
              });
              const mraIncomplete = Boolean(
                mraSubmissionState &&
                !['accepted', 'offline_queued'].includes(mraSubmissionState)
              );

              const saleWasNotCreated = isEisSaleNotCreated({
                ...normalizedResponse,
                mraSubmission,
                mra_submission: mraSubmission,
              });

              if (saleWasNotCreated) {
                const rejectionMessage = mraSubmissionMessage || 'MRA rejected the fiscal sale.';
                await removeRejectedSaleLocally(finalOrderId, rejectionMessage);
                toast({
                  variant: 'destructive',
                  title: isConfirmedMraRejection({
                    ...normalizedResponse,
                    mraSubmission,
                    mra_submission: mraSubmission,
                  }) ? 'Sale Rejected by MRA' : 'Sale Not Completed',
                  description: rejectionMessage,
                });
                return null;
              }

              const fiscalUpdate: Record<string, any> = {
                _dirty: false,
                _operation: undefined,
                _synced_at: new Date().toISOString(),
                syncStatus: mraIncomplete ? 'failed' : 'synced',
                syncError: mraIncomplete ? (mraSubmissionMessage || 'Legal receipt submission is not complete.') : undefined,
                syncRetryBlocked: mraIncomplete && mraSubmissionState === 'rejected',
                syncFailedAt: mraIncomplete ? new Date().toISOString() : undefined,
              };
              const setFiscalUpdate = (snakeKey: string, camelKey: string, value: unknown) => {
                if (value !== undefined && value !== null && String(value).trim() !== '') {
                  fiscalUpdate[snakeKey] = value;
                  fiscalUpdate[camelKey] = value;
                }
              };
              setFiscalUpdate('fiscal_invoice_number', 'fiscalInvoiceNumber', normalizedResponse.fiscalInvoiceNumber);
              setFiscalUpdate('eis_status', 'eisStatus', normalizedResponse.eisStatus);
              setFiscalUpdate('eis_sync_state', 'eisSyncState', normalizedResponse.eisSyncState);
              setFiscalUpdate('eis_uuid', 'eisUuid', normalizedResponse.eisUuid);
              setFiscalUpdate('eis_submitted_at', 'eisSubmittedAt', normalizedResponse.eisSubmittedAt);
              setFiscalUpdate('qr_code_payload', 'qrCodePayload', normalizedResponse.qrCodePayload);
              setFiscalUpdate('digital_signature', 'digitalSignature', normalizedResponse.digitalSignature);
              setFiscalUpdate('net_amount', 'netAmount', normalizedResponse.netAmount);
              setFiscalUpdate('vat_amount', 'vatAmount', normalizedResponse.vatAmount);
              setFiscalUpdate('gross_amount', 'grossAmount', normalizedResponse.grossAmount);
              if (normalizedResponse.eisValidationMetadata) {
                fiscalUpdate.eis_validation_metadata = normalizedResponse.eisValidationMetadata;
                fiscalUpdate.eisValidationMetadata = normalizedResponse.eisValidationMetadata;
              }
              if (mraSubmission) {
                fiscalUpdate.mra_submission = mraSubmission;
                fiscalUpdate.mraSubmission = mraSubmission;
              }

              await db.orders.update(finalOrderId, fiscalUpdate);

              if (mraIncomplete) {
                markSaleAsFailed(
                  finalOrderId,
                  mraSubmissionMessage || 'Legal receipt submission is not complete.',
                  { retryBlocked: mraSubmissionState === 'rejected' }
                );
              } else {
                markSaleAsSynced(finalOrderId);
              }
              if (mraSubmissionState === 'offline_queued') {
                toast({
                  title: 'Offline Receipt Queued',
                  description: mraSubmissionMessage || 'Sale was signed offline and queued for upload.',
                });
              } else if (mraSubmissionState && mraSubmissionState !== 'accepted') {
                toast({
                  variant: mraSubmissionState === 'rejected' ? 'destructive' : 'default',
                  title: mraSubmissionState === 'rejected' ? 'Sale Rejected' : 'Receipt Submission Pending',
                  description: mraSubmissionMessage || 'Legal receipt submission is not complete.',
                });
              }

              const latestOrder = await db.orders.get(finalOrderId);
              const orderWithFiscalData = (latestOrder || {
                ...finalOrder,
                ...data,
                fiscalInvoiceNumber: normalizedResponse.fiscalInvoiceNumber,
                fiscal_invoice_number: normalizedResponse.fiscalInvoiceNumber,
                qrCodePayload: normalizedResponse.qrCodePayload,
                qr_code_payload: normalizedResponse.qrCodePayload,
                digitalSignature: normalizedResponse.digitalSignature,
                digital_signature: normalizedResponse.digitalSignature,
                eisStatus: normalizedResponse.eisStatus,
                eis_status: normalizedResponse.eisStatus,
                eisValidationMetadata: normalizedResponse.eisValidationMetadata,
                eis_validation_metadata: normalizedResponse.eisValidationMetadata,
              }) as Order;
              const fiscalReadinessCandidate = {
                ...orderWithFiscalData,
                ...fiscalUpdate,
                ...normalizedResponse,
                mraSubmission,
                mra_submission: mraSubmission,
              } as Order;

              const isOfflineQueued =
                mraSubmissionState === 'offline_queued' ||
                eisSyncState === 'OFFLINE_QUEUED' ||
                fiscalReceiptState === 'OFFLINE_QUEUED' ||
                fiscalReceiptReady;
              if (eisEnabled && (mraIncomplete || (!isOfflineQueued && !hasFiscalReceiptData(fiscalReadinessCandidate)))) {
                const failureMessage = mraSubmissionMessage || 'Legal receipt details are not ready.';
                markSaleAsFailed(finalOrderId, failureMessage, { retryBlocked: mraSubmissionState === 'rejected' });
                toast({
                  variant: 'destructive',
                  title: 'Legal Receipt Not Ready',
                  description: failureMessage,
                });
                return null;
              }

              return fiscalReadinessCandidate;
            } else {
              const errorData = await response.json().catch(() => ({}));
              const errorMessage = extractBackendErrorMessage(
                errorData,
                'Sale could not be completed. Please try again.'
              );
              const userMessage = cleanUserFacingOrderError(errorMessage);
              if (eisEnabled && isEisSaleNotCreated(errorData)) {
                await removeRejectedSaleLocally(finalOrderId, userMessage);
                toast({
                  variant: 'destructive',
                  title: isConfirmedMraRejection(errorData)
                    ? 'Sale Rejected by MRA'
                    : 'Sale Not Completed',
                  description: userMessage,
                });
                return null;
              }
              const retryBlocked = isRetryBlockedBackendOrderError(response.status, errorMessage);
              console.warn('[Order] Backend rejected order:', response.status, errorData);
              markSaleAsFailed(finalOrderId, userMessage, { retryBlocked });
              toast({
                variant: 'destructive',
                title: eisEnabled ? 'Legal Receipt Not Issued' : 'Sale Not Synced',
                description: userMessage,
              });
              return null;
            }
          } catch (syncError) {
            console.warn('[Order] Failed to sync order, but local save succeeded:', syncError);
            const errorMessage = syncError instanceof Error ? syncError.message : 'Unknown error';
            markSaleAsFailed(finalOrderId, errorMessage);
            if (eisEnabled) {
              toast({
                variant: 'destructive',
                title: 'Legal Receipt Not Issued',
                description: 'Sync sale first.',
              });
            }
            return null;
          }
        };

        if (eisEnabled) {
          const fiscalizedOrder = await submitOrderToBackend();
          if (fiscalizedOrder) {
            finalOrder = fiscalizedOrder;
          } else {
            // Do not let the provisional local order fall through to the
            // success toast/modal when fiscal submission failed or was rejected.
            finalOrder = null;
          }
        } else {
          void submitOrderToBackend();
        }
      } else {
        console.warn('[Order] Cannot sync - finalOrder or orderForBackend is null', { finalOrder, orderForBackend });
      }

      if (eisEnabled && !finalOrder) {
        return null;
      }

      // 7. Mark take orders as completed
      if (takeOrderIdsInCart.length > 0) {
        try {
          for (const takeOrderId of takeOrderIdsInCart) {
            await db.takeOrders.update(takeOrderId, {
              status: 'Completed',
              _dirty: true,
              _operation: 'update'
            });
            console.log('[TakeOrder] Marked take order as completed:', takeOrderId);
          }
          // Clear the take order IDs from cart
          setTakeOrderIdsInCart([]);
        } catch (error) {
          console.warn('[TakeOrder] Failed to mark take orders as completed:', error);
        }
      }

      const displayOrderNumber = (finalOrder as any)?.orderNumber ?? (finalOrder as any)?.order_number ?? '-';
      toast({
        title: `Order #${displayOrderNumber} Created`,
        description: `${paymentMethod} sale completed for ${total.toFixed(2)}.`,
      });

      handleClearCart();

      // Return latest order snapshot to include fiscal fields in receipt.
      if (finalOrder?.id) {
        const latestOrder = await db.orders.get(finalOrder.id);
        if (latestOrder) {
          return latestOrder as Order;
        }
      }

      return finalOrder;

    } catch (error) {
      console.error('Failed to create order:', error);
      toast({
        variant: 'destructive',
        title: 'Error creating order',
        description: error instanceof Error ? error.message : 'An unknown error occurred.',
      });
      return null;
    }
  };


  const renderPosForBusiness = () => {
    if (!allInventory) {
        return (
          <div className="flex h-full items-center justify-center">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </div>
        )
    }

    // Debug: Log inventory data
    console.log('[POS Debug] allInventory:', allInventory);
    console.log('[POS Debug] sellableItems:', sellableItems);
    console.log('[POS Debug] currentBusinessType:', currentBusinessType);

    if (sellableItems.length === 0) {
      return (
        <Card className="flex h-full items-center justify-center">
          <CardContent className="text-center">
            <p className="text-muted-foreground mb-4">No products available for {currentBusinessType}</p>
            <p className="text-sm text-muted-foreground">Add items to inventory and mark them as "on menu" to display them here.</p>
          </CardContent>
        </Card>
      );
    }

    const posProps = {
      inventory: sellableItems || [],
      cart,
      branchId: activeBranchId,
      onAddToCart: handleAddToCart,
      onUpdateQuantity: handleUpdateQuantity,
      onClearCart: handleClearCart,
      onCheckout: handleCreateOrder,
      viewMode,
      defaultTaxRate,
      eisEnabled,
      blockSalesIfTaxMappingMissing,
      isEisInvoiceSubmissionBlocked: isBackendUnavailableStable,
      eisInvoiceSubmissionBlockedMessage: getBackendConnectionIssue({
        isReachable: !isBackendUnavailableStable,
        error: backendReachabilityError,
      }).description,
    };

    switch (currentBusinessType) {
      case 'Pharmacy':
        return <PharmacyPos {...posProps} />;
      case 'Restaurant':
        return <RestaurantPos {...posProps} />;
      case 'Bar & Liquor':
        return <BarLiquorPos {...posProps} />;
      case 'Supermarket':
        return <SupermarketPos {...posProps} />;
      case 'Grocery':
        return <GroceryPos {...posProps} />;
      case 'Beauty Salon and Spa':
        return <BeautySalonPos {...posProps} />;
      default:
        return <p>No POS configuration for this business type.</p>;
    }
  };

  if (!activeBranchId) {
     return (
        <div className="flex h-full items-center justify-center">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
        </div>
    )
  }

  if (!user) {
      return (
        <div className="flex h-full items-center justify-center">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
        </div>
      )
  }

  if (isWarehouseBranchId(activeBranchId)) {
    return (
      <div className="flex h-full items-center justify-center">
        <Card className="w-full max-w-md text-center">
          <CardHeader>
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
              <AlertTriangle />
            </div>
            <CardTitle className="mt-4 text-xl">Warehouse Selected</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-muted-foreground">
              Switch to a branch to make sales.
            </p>
            <Button className="mt-6" onClick={() => router.push('/dashboard/inventory')}>
              View Warehouse Stock
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!activeSession) {
    return (
        <div className="flex h-full items-center justify-center">
            <Card className="w-full max-w-md text-center">
                <CardHeader>
                    <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-yellow-100 text-yellow-600 dark:bg-yellow-900/30 dark:text-yellow-400">
                        <AlertTriangle />
                    </div>
                    <CardTitle className="mt-4 text-xl">No Active Session</CardTitle>
                </CardHeader>
                <CardContent>
                    <p className="text-muted-foreground">
                        You must start a new session before you can make sales.
                    </p>
                    <Button className="mt-6" onClick={() => router.push('/dashboard/sessions')}>
                        Go to Sessions
                    </Button>
                </CardContent>
            </Card>
        </div>
    )
  }

  return (
    <>
    <div className="flex h-full flex-col gap-4">
       <div className="flex flex-col items-stretch gap-4 sm:flex-row sm:items-center">
        <div className="flex-1">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="Search for products or scan barcode..."
              className="w-full pl-10"
            />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {activeBranchId && (
            <TakeOrdersPanel
              branchId={activeBranchId}
              onAddToCart={handleAddToCart}
              cartItems={cart}
            />
          )}
          <Button 
            variant="outline" 
            className="h-10 w-10 p-0"
            title={isAndroidTauri ? 'Scan Barcode' : 'Configure Scanner'}
            onClick={handleScannerAction}
          >
            {isAndroidTauri ? <Camera className="h-4 w-4" /> : <Barcode className="h-4 w-4" />}
          </Button>
          <Button 
            variant="outline" 
            className="h-10 w-10 p-0"
            title="Configure Printer"
            onClick={() => setShowPrinterConfig(true)}
          >
            <Printer className="h-4 w-4" />
          </Button>
          <div className="hidden items-center gap-2 sm:flex">
             <ToggleGroup type="single" value={viewMode} onValueChange={(value) => value && setViewMode(value as any)} defaultValue="grid" aria-label="View mode">
              <ToggleGroupItem value="grid" aria-label="Grid view">
                <LayoutGrid />
              </ToggleGroupItem>
              <ToggleGroupItem value="list" aria-label="List view">
                <List />
              </ToggleGroupItem>
            </ToggleGroup>
          </div>
          <Select
            value={currentBusinessType}
            onValueChange={(value) =>
              setCurrentBusinessType(value as BusinessType)
            }
          >
            <SelectTrigger className="h-10 w-[10.5rem] sm:w-auto">
              <SelectValue placeholder="Switch Business Type" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="Pharmacy">Pharmacy</SelectItem>
              <SelectItem value="Restaurant">Restaurant</SelectItem>
              <SelectItem value="Bar & Liquor">Bar & Liquor</SelectItem>
              <SelectItem value="Supermarket">Supermarket</SelectItem>
              <SelectItem value="Grocery">Grocery</SelectItem>
              <SelectItem value="Beauty Salon and Spa">
                Beauty Salon and Spa
              </SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="flex-1 overflow-hidden">{renderPosForBusiness()}</div>
    </div>
    <ScannerConfigModal isOpen={showScannerConfig} onOpenChange={setShowScannerConfig} />
    <CameraBarcodeScannerModal
      isOpen={showCameraScanner}
      onOpenChange={setShowCameraScanner}
      onBarcodeDetected={handleCameraBarcodeDetected}
    />
    <PrinterConfigModal isOpen={showPrinterConfig} onOpenChange={setShowPrinterConfig} />
    </>
  );
}
