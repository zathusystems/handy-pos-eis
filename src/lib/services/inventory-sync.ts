'use client';

import { db, type InventoryItem } from '@/lib/db';
import { authFetch } from '@/lib/auth-fetch';
import { ensureTauriDeviceIdentity, getDeviceSerial } from '@/lib/device-identity';
import { recordMraMappingCacheRefresh } from '@/lib/mra-mapping-cache';
import {
  getMraStockReconciliationWarnings,
  storeMraStockReconciliationWarnings,
  type StockReconciliationWarning,
} from '@/lib/services/stock-reconciliation';

function toBackendBranchId(id: string): string {
  const normalized = String(id || '').trim();
  if (!normalized) return normalized;

  const brnMatch = /^BRN-(\d+)$/i.exec(normalized);
  if (brnMatch) return brnMatch[1];

  const legacyBranchMatch = /^branch-(\d+)$/i.exec(normalized);
  if (legacyBranchMatch) return legacyBranchMatch[1];

  if (/^\d+$/.test(normalized)) return normalized;
  return normalized;
}

function normalizeBranchId(id: string): string {
  return toBackendBranchId(id).trim().toLowerCase();
}

function extractApiList<T>(response: any): T[] {
  if (Array.isArray(response)) return response as T[];
  if (Array.isArray(response?.results)) return response.results as T[];
  if (Array.isArray(response?.data)) return response.data as T[];
  return [];
}

function getApiBranchId(item: any): string {
  const rawBranch = item?.branch;
  if (rawBranch && typeof rawBranch === 'object') {
    return String(rawBranch.id ?? rawBranch.pk ?? rawBranch.branch_id ?? '').trim();
  }
  return String(rawBranch ?? item?.branch_id ?? item?.branchId ?? '').trim();
}

function getApiDeviceSerial(item: any): string {
  return String(item?.device_serial ?? item?.deviceSerial ?? item?.mac_address ?? item?.macAddress ?? '').trim();
}

function findCurrentDeviceTerminal(terminals: any[], branchId: string): any {
  const currentDeviceSerial = getDeviceSerial().toLowerCase();
  const normalizedBranchId = normalizeBranchId(branchId);
  const branchTerminals = terminals.filter(
    (item) => normalizeBranchId(getApiBranchId(item)) === normalizedBranchId
  );

  return (
    branchTerminals.find((item) => (
      String(item?.status || '').toLowerCase() === 'active' &&
      getApiDeviceSerial(item).toLowerCase() === currentDeviceSerial
    )) ||
    branchTerminals.find((item) => getApiDeviceSerial(item).toLowerCase() === currentDeviceSerial) ||
    null
  );
}

export function getMraTerminalSyncError(terminals: any[], branchId: string): string {
  const normalizedBranchId = normalizeBranchId(branchId);
  const branchTerminals = terminals.filter(
    (item) => normalizeBranchId(getApiBranchId(item)) === normalizedBranchId
  );

  if (branchTerminals.length === 0) {
    return 'No EIS terminal is linked to this branch.';
  }

  const currentDeviceSerial = getDeviceSerial().toLowerCase();
  const deviceTerminal = branchTerminals.find(
    (item) => getApiDeviceSerial(item).toLowerCase() === currentDeviceSerial
  );

  if (!deviceTerminal) {
    return 'This device is not activated for the selected branch.';
  }

  if (String(deviceTerminal?.status || '').toLowerCase() !== 'active') {
    return 'The EIS terminal for this branch is inactive.';
  }

  return 'The EIS terminal for this branch is not ready.';
}

export function getMraProductSyncError(error: unknown): string {
  const rawMessage = error instanceof Error
    ? error.message
    : typeof error === 'string'
      ? error
      : '';
  const message = rawMessage.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  const normalized = message.toLowerCase();

  if (!message || /^http \d{3}$/i.test(message)) {
    return 'MRA EIS is temporarily unavailable. Try again later.';
  }

  if (
    normalized.includes('500.30') ||
    normalized.includes('gateway timeout') ||
    normalized.includes('temporarily unavailable') ||
    normalized.includes('mra request failed') ||
    normalized.includes('html server error') ||
    normalized.includes('<!doctype')
  ) {
    return 'MRA EIS is temporarily unavailable. Try again later.';
  }

  if (message.length > 240) {
    return 'MRA EIS returned an unexpected response. Try again later.';
  }

  return message;
}

/**
 * Convert snake_case to camelCase
 */
function snakeToCamel(obj: any): any {
  if (Array.isArray(obj)) {
    return obj.map(item => snakeToCamel(item));
  }
  
  if (obj !== null && typeof obj === 'object') {
    const converted: any = {};
    for (const [key, value] of Object.entries(obj)) {
      const camelKey = key.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
      converted[camelKey] = snakeToCamel(value);
    }
    return converted;
  }
  
  return obj;
}

function toOptionalNumber(value: unknown): number | undefined {
  if (value === null || value === undefined || value === '') {
    return undefined;
  }

  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function toBoolean(value: unknown, fallback = false): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (['true', '1', 'yes', 'y'].includes(normalized)) return true;
    if (['false', '0', 'no', 'n'].includes(normalized)) return false;
  }
  return fallback;
}

function normalizeTaxCalculationMethod(value: unknown): 'inclusive' | 'exclusive' {
  const normalized = String(value ?? '').trim().toLowerCase();
  return normalized.startsWith('excl') ? 'exclusive' : 'inclusive';
}

function extractPaginatedItems<T>(result: any, label: string): { items: T[]; next: string | null } {
  if (Array.isArray(result)) {
    return { items: result, next: null };
  }

  if (result && Array.isArray(result.results)) {
    return {
      items: result.results,
      next: typeof result.next === 'string' && result.next.trim().length > 0 ? result.next : null,
    };
  }

  throw new Error(`Unexpected ${label} response format`);
}

async function fetchPaginatedResults<T>(initialUrl: string, label: string): Promise<T[]> {
  const collected: T[] = [];
  const visitedUrls = new Set<string>();
  let nextUrl: string | null = initialUrl;
  let page = 1;

  while (nextUrl) {
    if (visitedUrls.has(nextUrl)) {
      throw new Error(`Pagination loop detected while fetching ${label}`);
    }

    visitedUrls.add(nextUrl);
    const result = await authFetch.fetch<any>(nextUrl, { method: 'GET' });
    const { items, next } = extractPaginatedItems<T>(result, label);

    console.log(`[InventorySync] Fetched ${label} page ${page} with ${items.length} records`);
    collected.push(...items);
    nextUrl = next;
    page += 1;
  }

  return collected;
}

function normalizeInventoryProduct(
  backendProduct: any,
  branchId: string,
  existingProduct?: InventoryItem
): InventoryItem | null {
  const converted = snakeToCamel(backendProduct);
  const id = String(converted.id ?? backendProduct?.id ?? existingProduct?.id ?? '').trim();
  if (!id) {
    return null;
  }

  const itemTypeRaw = String(
    converted.itemType ?? backendProduct?.item_type ?? existingProduct?.itemType ?? 'sellable'
  ).trim().toLowerCase();
  const itemType: InventoryItem['itemType'] = itemTypeRaw === 'ingredient' ? 'ingredient' : 'sellable';

  const stockUnits = toOptionalNumber(
    converted.stockUnits ?? backendProduct?.stock_units ?? existingProduct?.stockUnits
  ) ?? 0;
  const reorderLevel = toOptionalNumber(
    converted.reorderLevel ?? backendProduct?.reorder_level ?? existingProduct?.reorderLevel
  ) ?? 0;
  const cost = toOptionalNumber(converted.cost ?? backendProduct?.cost ?? existingProduct?.cost);
  const price = toOptionalNumber(converted.price ?? backendProduct?.price ?? existingProduct?.price);
  const explicitValue = toOptionalNumber(converted.value ?? backendProduct?.value ?? existingProduct?.value);
  const value = explicitValue ?? Number((stockUnits * (cost ?? 0)).toFixed(2));

  const isProduced = toBoolean(
    converted.isProduced ?? backendProduct?.is_produced ?? existingProduct?.isProduced,
    false
  );
  const isSoldInPortions = toBoolean(
    converted.isSoldInPortions ?? backendProduct?.is_sold_in_portions ?? existingProduct?.isSoldInPortions,
    false
  );
  const portionsPerUnit = isSoldInPortions
    ? toOptionalNumber(
        converted.portionsPerUnit ??
        backendProduct?.portions_per_unit ??
        existingProduct?.portionsPerUnit
      )
    : undefined;
  const rawPortionName = String(
    converted.portionName ??
    backendProduct?.portion_name ??
    existingProduct?.portionName ??
    ''
  ).trim();
  const portionName = isSoldInPortions && rawPortionName ? rawPortionName : undefined;

  const recipe = isProduced
    ? (Array.isArray(converted.recipe) ? converted.recipe : (Array.isArray(existingProduct?.recipe) ? existingProduct?.recipe : []))
    : [];

  return {
    ...(existingProduct || {}),
    ...converted,
    id,
    branchId: String(existingProduct?.branchId || branchId).trim() || branchId,
    name: String(converted.name ?? existingProduct?.name ?? 'Unnamed Item').trim() || 'Unnamed Item',
    category: String(converted.category ?? existingProduct?.category ?? 'General').trim() || 'General',
    itemType,
    stockUnits,
    unitType: String(converted.unitType ?? existingProduct?.unitType ?? 'unit').trim() || 'unit',
    reorderLevel,
    cost,
    price: itemType === 'sellable' ? price : undefined,
    value,
    status: converted.status ?? existingProduct?.status,
    supplier: converted.supplier ?? existingProduct?.supplier,
    manufacturer: converted.manufacturer ?? existingProduct?.manufacturer,
    batch: converted.batch ?? existingProduct?.batch,
    brand: converted.brand ?? existingProduct?.brand,
    packSize: toOptionalNumber(converted.packSize ?? existingProduct?.packSize),
    productCode: converted.productCode ?? existingProduct?.productCode,
    barcode: converted.barcode ?? existingProduct?.barcode,
    sku: converted.sku ?? existingProduct?.sku,
    expiry: converted.expiry ?? existingProduct?.expiry,
    isVariablePrice: toBoolean(
      converted.isVariablePrice ?? backendProduct?.is_variable_price ?? existingProduct?.isVariablePrice,
      false
    ),
    isFuel: toBoolean(
      converted.isFuel ?? backendProduct?.is_fuel ?? existingProduct?.isFuel,
      false
    ),
    isOil: toBoolean(
      converted.isOil ?? backendProduct?.is_oil ?? existingProduct?.isOil,
      false
    ),
    isProduced,
    onMenu: toBoolean(converted.onMenu ?? backendProduct?.on_menu ?? existingProduct?.onMenu, false),
    isSoldInPortions,
    portionName,
    portionsPerUnit,
    recipe,
    _dirty: false,
    _operation: undefined,
    _synced_at: new Date().toISOString(),
  };
}

/**
 * Fetch products from backend and merge with local inventory
 * Also fetches and syncs MRA mappings
 */
export async function syncInventoryFromBackend(branchId: string): Promise<{
  synced: number;
  updated: number;
  created: number;
  mraMappingsSynced?: number;
  stockReconciliationWarnings?: StockReconciliationWarning[];
  error?: string;
}> {
  try {

    const backendBranchId = toBackendBranchId(branchId);

    console.log('[InventorySync] Starting sync for branch:', branchId, 'backend ID:', backendBranchId);

    // Fetch products from backend
    const products = await fetchPaginatedResults<any>(
      `/inventory/products/?branch_id=${backendBranchId}`,
      'inventory products'
    );

    let created = 0;
    let updated = 0;

    // Process each backend product
    for (const backendProduct of products) {
      const backendId = String(backendProduct?.id ?? '').trim();
      if (!backendId) {
        console.warn('[InventorySync] Skipping backend product without id:', backendProduct);
        continue;
      }

      const localProduct = await db.inventory.get(backendId);
      if (localProduct?._dirty) {
        console.log('[InventorySync] Skipping overwrite for dirty local product:', backendId);
        continue;
      }

      const normalizedProduct = normalizeInventoryProduct(backendProduct, branchId, localProduct);
      if (!normalizedProduct) {
        continue;
      }

      if (localProduct) {
        // Update existing product with backend data
        await db.inventory.put(normalizedProduct);
        updated++;
      } else {
        // Create new product from backend
        await db.inventory.add(normalizedProduct);
        created++;
      }
    }

    console.log('[InventorySync] Synced products:', products.length, 'created:', created, 'updated:', updated);

    // CRITICAL: Also fetch and sync MRA mappings
    // IMPORTANT: Clear old mappings first to ensure approval status changes are reflected
    let mraMappingsSynced = 0;
    try {
      console.log('[InventorySync] Fetching MRA mappings for branch:', backendBranchId);

      const mraMappings = await fetchPaginatedResults<any>(
        `/inventory/mra-mappings/?branch_id=${backendBranchId}`,
        'inventory MRA mappings'
      );

      console.log('[InventorySync] Received MRA mappings:', mraMappings.length);

      // Store each MRA mapping in local database with fresh data from backend
      for (const mapping of mraMappings) {
        try {
          // Convert snake_case to camelCase
          const convertedMapping = snakeToCamel(mapping);

          const inventoryItemId = String(
            convertedMapping.inventoryItemId ??
            convertedMapping.inventoryItem ??
            mapping.inventory_item ??
            ''
          ).trim();

          if (!inventoryItemId) {
            console.warn('[InventorySync] Skipping MRA mapping without inventory item id:', mapping);
            continue;
          }

          const mappingToStore = {
            ...convertedMapping,
            inventoryItemId,
            branchId: String(
              convertedMapping.branchId ??
              convertedMapping.branch ??
              mapping.branch_id ??
              mapping.branch ??
              branchId ??
              ''
            ).trim() || undefined,
            _dirty: false,
            _synced_at: new Date().toISOString()
          };

          (mappingToStore as any).isProduct = mapping.is_product ?? mapping.isProduct ?? convertedMapping.isProduct ?? true;

          mappingToStore.taxCalculationMethod = normalizeTaxCalculationMethod(
            mappingToStore.taxCalculationMethod ??
            (mappingToStore as any).tax_calculation_method ??
            mapping.tax_calculation_method ??
            (mapping as any).calculation_method ??
            (mapping as any).calculationMethod
          );

          delete (mappingToStore as any).inventoryItem;

          console.log('[InventorySync] Storing MRA mapping:', mappingToStore.id, 'for product:', mappingToStore.inventoryItemId, 'approved:', mappingToStore.isApproved, 'synced:', mappingToStore.mraSynced);

          await db.mraMappings.put(mappingToStore);
          
          mraMappingsSynced++;
          console.log('[InventorySync] ✓ Stored MRA mapping for product:', mappingToStore.inventoryItemId, 'approved:', mappingToStore.isApproved);
        } catch (error) {
          console.error('[InventorySync] Error storing MRA mapping:', mapping.id, error);
        }
      }

      console.log('[InventorySync] Successfully synced', mraMappingsSynced, 'MRA mappings');
      recordMraMappingCacheRefresh(branchId, {
        inventoryItemCount: mraMappings.length,
        missingItemCount: 0,
      });
    } catch (error) {
      console.error('[InventorySync] Failed to fetch MRA mappings:', error);
    }

    const stockReconciliationWarnings = await getMraStockReconciliationWarnings(branchId);
    storeMraStockReconciliationWarnings(stockReconciliationWarnings);

    return {
      synced: products.length,
      updated,
      created,
      mraMappingsSynced,
      stockReconciliationWarnings,
    };
  } catch (error) {
    console.error('Failed to sync inventory from backend:', error);
    return {
      synced: 0,
      updated: 0,
      created: 0,
      error: error instanceof Error ? error.message : 'Unknown error',
    };
  }
}

export async function refreshInventoryFromMraApprovedProducts(
  branchId: string,
  options: { refreshFromMra?: boolean; syncLocal?: boolean } = {}
): Promise<{
  ok: boolean;
  created: number;
  updated: number;
  synced: number;
  mraMappingsSynced?: number;
  stockReconciliationWarnings?: StockReconciliationWarning[];
  error?: string;
}> {
  const normalizedBranchId = toBackendBranchId(branchId);
  if (!normalizedBranchId) {
    return {
      ok: false,
      created: 0,
      updated: 0,
      synced: 0,
      error: 'Select a branch first.',
    };
  }

  try {
    await ensureTauriDeviceIdentity();
    const terminalsResponse = await authFetch.fetch<any>('/mra-eis/terminals/');
    const terminals = extractApiList<any>(terminalsResponse);
    const terminal = findCurrentDeviceTerminal(terminals, normalizedBranchId);

    if (!terminal?.id || String(terminal?.status || '').toLowerCase() !== 'active') {
      return {
        ok: false,
        created: 0,
        updated: 0,
        synced: 0,
        error: getMraTerminalSyncError(terminals, normalizedBranchId),
      };
    }

    const pullResponse = await authFetch.fetch<any>(
      `/mra-eis/terminals/${terminal.id}/pull_approved_products/`,
      {
        method: 'POST',
        body: JSON.stringify({ refreshFromMra: options.refreshFromMra !== false }),
      }
    );

    if (options.syncLocal === false) {
      return {
        ok: true,
        created: Number(pullResponse?.created ?? 0),
        updated: Number(pullResponse?.updated ?? 0),
        synced: 0,
      };
    }

    const syncResult = await syncInventoryFromBackend(branchId);
    if (syncResult.error) {
      return {
        ok: false,
        created: Number(pullResponse?.created ?? 0),
        updated: Number(pullResponse?.updated ?? 0),
        synced: syncResult.synced,
        mraMappingsSynced: syncResult.mraMappingsSynced,
        stockReconciliationWarnings: syncResult.stockReconciliationWarnings,
        error: syncResult.error,
      };
    }

    return {
      ok: true,
      created: Number(pullResponse?.created ?? syncResult.created ?? 0),
      updated: Number(pullResponse?.updated ?? syncResult.updated ?? 0),
      synced: syncResult.synced,
      mraMappingsSynced: syncResult.mraMappingsSynced,
      stockReconciliationWarnings: syncResult.stockReconciliationWarnings,
    };
  } catch (error) {
    console.error('[InventorySync] Failed to refresh MRA approved products:', error);
    return {
      ok: false,
      created: 0,
      updated: 0,
      synced: 0,
      error: getMraProductSyncError(error),
    };
  }
}

/**
 * Get sync status - check if there are pending syncs
 */
export function getInventorySyncStatus(): {
  hasPendingSync: boolean;
  lastSyncTime?: number;
} {
  try {
    if (typeof window === 'undefined') {
      return { hasPendingSync: false };
    }

    const lastSync = localStorage.getItem('handypos-inventory-last-sync');
    const lastSyncTime = lastSync ? parseInt(lastSync, 10) : undefined;

    // Consider sync stale if older than 5 minutes
    const isStale = !lastSyncTime || Date.now() - lastSyncTime > 5 * 60 * 1000;

    return {
      hasPendingSync: isStale,
      lastSyncTime,
    };
  } catch (error) {
    console.error('Failed to get inventory sync status:', error);
    return { hasPendingSync: false };
  }
}

/**
 * Mark inventory as synced
 */
export function markInventorySynced(): void {
  try {
    if (typeof window === 'undefined') return;
    localStorage.setItem('handypos-inventory-last-sync', String(Date.now()));
  } catch (error) {
    console.error('Failed to mark inventory as synced:', error);
  }
}
