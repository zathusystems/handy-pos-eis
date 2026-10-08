
'use client';

import React, { useState, useEffect } from 'react';
import { useLiveQuery } from 'dexie-react-hooks';
import { useSearchParams } from 'next/navigation';
import {
  Search,
  Filter,
  Package,
  History,
  Trash,
  Loader2,
  Repeat,
} from 'lucide-react';

import { db, type EisStockReceiptSource, type InventoryItem, type PurchaseRecord, type Supplier } from '@/lib/db';
import { type BusinessType } from '@/lib/inventory/config';
import { useIsMobile } from '@/hooks/use-mobile';
import { useAuth } from '@/hooks/use-auth';
import {
  syncInventoryFromBackend,
  refreshInventoryFromMraApprovedProducts,
  getInventorySyncStatus,
  markInventorySynced,
  formatMraProductSyncDiagnostics,
} from '@/lib/services/inventory-sync';
import { toast } from '@/hooks/use-toast';
import { authFetch } from '@/lib/auth-fetch';
import { logAuditAction } from '@/lib/audit';
import { normalizePurchaseBatchQuantities } from '@/lib/purchase-quantity';
import { isWarehouseBranchId } from '@/lib/branch-context';
import {
  getMraStockReconciliationWarnings,
  loadMraStockReconciliationWarnings,
  storeMraStockReconciliationWarnings,
  type StockReconciliationWarning,
} from '@/lib/services/stock-reconciliation';

import { AddProductForm } from './components/product-form';
import type { EditablePurchaseGroup } from './components/purchase-editor-types';
import { ReceiveStockForm } from './components/receive-stock-form';
import { TransferStockForm } from './components/transfer-stock-form';
import { RecordWasteForm } from './components/waste-form';
import { ImportModal } from './components/import-modal';
import { InventoryTab } from './components/inventory-tab';
import { PurchasesTab } from './components/purchases-tab';
import { TransfersTab } from './components/transfers-tab';
import { WasteTab } from './components/waste-tab';
import { MRAMappingsTab } from './components/mra-mappings-tab';
import { WarehouseStockTab } from './components/warehouse-stock-tab';

import {
  Card,
  CardHeader,
} from '@/components/ui/card';
import {
    Tabs,
    TabsContent,
    TabsList,
    TabsTrigger,
} from '@/components/ui/tabs';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Progress } from '@/components/ui/progress';
import { ToastAction } from '@/components/ui/toast';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuCheckboxItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';

const LOCAL_STORAGE_KEYS = {
    ACTIVE_BRANCH: 'handypos-active-branch',
    BUSINESS_SETTINGS: 'handypos-business-settings',
    BRANCHES: 'handypos-branches'
};

const SHOW_WASTE_TAB = false;

type Branch = {
  id: string;
  name: string;
  address: string;
  mraBranchCode?: string;
  mra_branch_code?: string;
  isWarehouse?: boolean;
};

const toBackendBranchId = (id: string): string => {
  const normalized = String(id || '').trim();
  if (!normalized) return normalized;
  if (isWarehouseBranchId(normalized)) return normalized;

  const brnMatch = /^BRN-(\d+)$/i.exec(normalized);
  if (brnMatch) return brnMatch[1];

  const legacyBranchMatch = /^branch-(\d+)$/i.exec(normalized);
  if (legacyBranchMatch) return legacyBranchMatch[1];

  if (/^\d+$/.test(normalized)) return normalized;
  return normalized;
};

const getBranchIdCandidates = (branchId: string): string[] => {
  const normalized = String(branchId || '').trim();
  if (!normalized) return [];

  const backendId = toBackendBranchId(normalized);
  const candidates = new Set<string>([normalized, backendId]);

  if (/^\d+$/.test(backendId)) {
    candidates.add(`BRN-${backendId}`);
    candidates.add(`branch-${backendId}`);
  }

  return Array.from(candidates).filter((candidate) => candidate.length > 0);
};

const getInventoryItemsForBranch = async (branchId: string): Promise<InventoryItem[]> => {
  if (isWarehouseBranchId(branchId)) {
    return [];
  }

  const branchCandidates = getBranchIdCandidates(branchId);
  if (branchCandidates.length === 0) {
    return [];
  }

  if (branchCandidates.length === 1) {
    return db.inventory.where('branchId').equals(branchCandidates[0]).toArray();
  }

  return db.inventory.where('branchId').anyOf(branchCandidates).toArray();
};

const getBranchScopedRows = async (table: any, branchId: string): Promise<any[]> => {
  if (isWarehouseBranchId(branchId)) {
    return [];
  }

  const branchCandidates = getBranchIdCandidates(branchId);
  if (branchCandidates.length === 0) {
    return [];
  }

  if (branchCandidates.length === 1) {
    return table.where('branchId').equals(branchCandidates[0]).toArray();
  }

  return table.where('branchId').anyOf(branchCandidates).toArray();
};

const toSafeNumber = (value: unknown, fallback = 0): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const normalizeTaxRate = (value: unknown): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
};

const readBooleanFlag = (value: unknown): boolean | undefined => {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  const normalized = String(value).trim().toLowerCase();
  if (['true', '1', 'yes', 'y', 'on'].includes(normalized)) return true;
  if (['false', '0', 'no', 'n', 'off'].includes(normalized)) return false;
  return undefined;
};

const resolveTaxMethod = (value: unknown): 'inclusive' | 'exclusive' => (
  String(value || '').trim().toLowerCase() === 'inclusive' ? 'inclusive' : 'exclusive'
);

const roundTo = (value: number, decimals: number): number => {
  if (!Number.isFinite(value)) {
    return 0;
  }

  return Number(value.toFixed(decimals));
};

const getTimestamp = (value: unknown): number => {
  const timestamp = new Date(String(value || '')).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
};

const costLooksVatStripped = (currentCost: number, grossCost: number, taxRate: number): boolean => {
  if (currentCost <= 0 || grossCost <= 0 || taxRate <= 0) {
    return false;
  }

  const expectedNet = roundTo(grossCost / (1 + taxRate / 100), 4);
  const tolerance = Math.max(0.02, Math.abs(expectedNet) * 0.0025);
  return Math.abs(currentCost - expectedNet) <= tolerance;
};

export default function InventoryPage() {
  const { business, user, loading: authLoading } = useAuth();
  const searchParams = useSearchParams();
  const searchInputRef = React.useRef<HTMLInputElement>(null);
  const activeBusinessId = business?.id || user?.businessId || null;
  const [currentBusinessType, setCurrentBusinessType] = useState<BusinessType>('Grocery');
  const [isAddFormOpen, setAddFormOpen] = useState(false);
  const [editingItem, setEditingItem] = useState<InventoryItem | undefined>(undefined);
  const [editingPurchase, setEditingPurchase] = useState<EditablePurchaseGroup | null>(null);
  const [isReceiveStockOpen, setReceiveStockOpen] = useState(false);
  const [isTransferStockOpen, setTransferStockOpen] = useState(false);
  const [isImportModalOpen, setImportModalOpen] = useState(false);
  const [isWasteModalOpen, setWasteModalOpen] = useState(false);
  const [activeBranchId, setActiveBranchId] = useState<string | null>(null);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [isSyncing, setIsSyncing] = useState(false);
  const [syncStatus, setSyncStatus] = useState({ hasPendingSync: false });
  const [activeTab, setActiveTab] = useState('inventory');
  const [businessCurrency, setBusinessCurrency] = useState('USD');
  const [searchTerm, setSearchTerm] = useState('');
  const [isSearchFocused, setIsSearchFocused] = useState(false);
  const [isLoadingData, setIsLoadingData] = useState(false);
  const [isDeleteAllInventoryOpen, setIsDeleteAllInventoryOpen] = useState(false);
  const [isDeletingAllInventory, setIsDeletingAllInventory] = useState(false);
  const [deleteProgress, setDeleteProgress] = useState(0);
  const [deleteStage, setDeleteStage] = useState('');
  const [isRestoreInclusiveCostsOpen, setIsRestoreInclusiveCostsOpen] = useState(false);
  const [isRestoringInclusiveCosts, setIsRestoringInclusiveCosts] = useState(false);
  const [restoreInclusiveCostsProgress, setRestoreInclusiveCostsProgress] = useState(0);
  const [restoreInclusiveCostsStage, setRestoreInclusiveCostsStage] = useState('');
  const [mraRefreshKey, setMraRefreshKey] = useState(0);
  const [receiveStockDefaultSource, setReceiveStockDefaultSource] = useState<EisStockReceiptSource>('pos_goods_receiving');
  const [receiveStockReconciliationDefaults, setReceiveStockReconciliationDefaults] = useState<StockReconciliationWarning[]>([]);
  const hasShownReconciliationPromptRef = React.useRef(false);
  const isCashierInventoryViewer = user?.role === 'Cashier';
  const canManageInventory = !isCashierInventoryViewer;
  const canTransferInventory = true;
  
  useEffect(() => {
    if (typeof window !== 'undefined') {
        const branchId = localStorage.getItem(LOCAL_STORAGE_KEYS.ACTIVE_BRANCH);
        if(branchId) {
            setActiveBranchId(branchId);
        } else {
            // Default to 'main' if no branch is set
            setActiveBranchId('main');
        }
        const storedBranches = localStorage.getItem(LOCAL_STORAGE_KEYS.BRANCHES);
        if (storedBranches) {
          const parsedBranches = JSON.parse(storedBranches);
          setBranches(Array.isArray(parsedBranches) ? parsedBranches : []);
        }
    }
  }, []);

  // Load business type and currency
  useEffect(() => {
    const loadBusinessSettings = async () => {
      if (activeBusinessId) {
        try {
          const businessProfile = await db.business.get(activeBusinessId);
          if (businessProfile) {
            // Load business type - map from backend format to frontend BusinessType
            if (businessProfile.type) {
              const typeMap: Record<string, BusinessType> = {
                'pharmacy': 'Pharmacy',
                'restaurant': 'Restaurant',
                'bar_liquor': 'Bar & Liquor',
                'bar & liquor': 'Bar & Liquor',
                'supermarket': 'Supermarket',
                'grocery': 'Grocery',
                'beauty_salon': 'Beauty Salon and Spa',
                'beauty salon and spa': 'Beauty Salon and Spa',
                'general_retail': 'General Retail',
                'general retail': 'General Retail',
                'generic': 'General Retail',
              };
              const mappedType = typeMap[businessProfile.type.toLowerCase()] || 'General Retail';
              console.log('[InventoryPage] Setting business type to:', mappedType);
              setCurrentBusinessType(mappedType);
            }
            // Load currency
            if (businessProfile.currency) {
              setBusinessCurrency(businessProfile.currency);
            }
          }
        } catch (error) {
          console.error('Failed to load business settings:', error);
        }
      }
    };
    loadBusinessSettings();
  }, [activeBusinessId]);

  // Listen for branch changes from header (custom event) and pull data
  useEffect(() => {
    const handleBranchChange = (e: Event) => {
      const customEvent = e as CustomEvent;
      const branchId = customEvent.detail?.branchId;
      if (branchId) {
        console.log('[InventoryPage] Branch changed to:', branchId);
        setActiveBranchId(branchId);
        if (isWarehouseBranchId(branchId)) {
          setActiveTab('inventory');
        }
      }
    };

    window.addEventListener('branchChanged', handleBranchChange);
    return () => window.removeEventListener('branchChanged', handleBranchChange);
  }, []);

  const pullServerData = async (branchId: string) => {
    if (isWarehouseBranchId(branchId)) {
      setIsLoadingData(false);
      return;
    }

    setIsLoadingData(true);
    try {
      console.log('[InventoryPage] Starting data fetch from backend for branch:', branchId);
      const { syncService } = await import('@/lib/services/sync-service');
      
      const backendBranchId = toBackendBranchId(branchId);

      try {
        if (isEisEnabled) {
          console.log('[InventoryPage] Refreshing inventory from MRA approved products');
          const mraRefresh = await refreshInventoryFromMraApprovedProducts(branchId);
          if (!mraRefresh.ok) {
            console.warn('[InventoryPage] MRA product refresh skipped/failed:', mraRefresh.error);
            await syncService.fetchAllInventoryFromBackend(branchId);
          }
        } else {
          // Fetch all inventory items from backend
          console.log('[InventoryPage] Fetching inventory from backend');
          await syncService.fetchAllInventoryFromBackend(branchId);
        }
        console.log('[InventoryPage] Inventory fetch completed');
      } catch (error) {
        console.error('[InventoryPage] Failed to fetch inventory from backend:', error);
        console.log('[InventoryPage] Falling back to local inventory data');
      }

      try {
        // Fetch suppliers from backend (filtered by current business)
        console.log('[InventoryPage] Fetching suppliers from backend for business:', activeBusinessId);
        const suppliersUrl = activeBusinessId ? `/inventory/suppliers/?business_id=${activeBusinessId}` : '/inventory/suppliers/';
        const suppliersResponse = await authFetch.fetch<any>(suppliersUrl);
        
        let suppliers = [];
        if (suppliersResponse && Array.isArray(suppliersResponse)) {
          suppliers = suppliersResponse;
        } else if (suppliersResponse?.results && Array.isArray(suppliersResponse.results)) {
          suppliers = suppliersResponse.results;
        }

        console.log('[InventoryPage] Received', suppliers.length, 'suppliers from backend');
        for (const supplier of suppliers) {
          await db.suppliers.put({
            id: supplier.id,
            businessId: supplier.business || activeBusinessId || undefined,
            name: supplier.name,
            contactPerson: supplier.contact_person,
            email: supplier.email,
            phone: supplier.phone,
            address: supplier.address,
            createdAt: supplier.created_at,
            updatedAt: supplier.updated_at,
            // MRA EIS Compliance Fields
            supplierTin: supplier.supplier_tin || '',
            vatRegistered: supplier.vat_registered || false,
          });
        }
      } catch (error) {
        console.error('[InventoryPage] Failed to fetch suppliers from backend:', error);
        console.log('[InventoryPage] Falling back to local suppliers data');
      }

      try {
        // Fetch purchase history from backend
        console.log('[InventoryPage] Fetching purchase history from backend');
        const purchaseResponse = await authFetch.fetch<any>(`/inventory/purchase-orders/?branch_id=${backendBranchId}`);
        
        let purchases = [];
        if (purchaseResponse && Array.isArray(purchaseResponse)) {
          purchases = purchaseResponse;
        } else if (purchaseResponse?.results && Array.isArray(purchaseResponse.results)) {
          purchases = purchaseResponse.results;
        }

        console.log('[InventoryPage] Received', purchases.length, 'purchase records from backend');
        
        // ✅ Get all backend purchase IDs to identify which local records to keep
        const backendPurchaseIds = new Set<string>();
        for (const purchase of purchases) {
          if (Array.isArray(purchase.items)) {
            for (const item of purchase.items) {
              if (item?.id !== undefined && item?.id !== null) {
                backendPurchaseIds.add(String(item.id));
              }
            }
          }
        }
        
        // ✅ Delete only records that are NOT dirty (not waiting to sync)
        // and are server-trackable IDs. Keep legacy/local-only numeric IDs.
        const allLocalRecords = await db.purchaseHistory.where({ branchId: branchId }).toArray();
        const isUuidLike = (value: string) => /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
        for (const record of allLocalRecords) {
          const localId = String(record.id ?? '').trim();
          const isServerTrackableId = isUuidLike(localId);

          // If record is not in backend AND not dirty, delete it
          if (isServerTrackableId && !backendPurchaseIds.has(localId) && !record._dirty) {
            await db.purchaseHistory.delete(record.id);
          }
        }
        
        for (const purchase of purchases) {
          // ✅ Get supplier name from backend response (includes null handling)
          let supplierName = purchase.supplier_name || 'No Supplier';
          
          // ✅ If supplier_name is null/undefined but supplier ID exists, try to fetch from local cache
          if (!supplierName || supplierName === 'null') {
            if (purchase.supplier) {
              try {
                const supplier = await db.suppliers.get(purchase.supplier);
                if (supplier) {
                  supplierName = supplier.name;
                  console.log('[InventoryPage] Found supplier from cache:', supplier.name);
                } else {
                  supplierName = 'No Supplier';
                }
              } catch (e) {
                console.warn('[InventoryPage] Could not fetch supplier from cache:', purchase.supplier);
                supplierName = 'No Supplier';
              }
            } else {
              supplierName = 'No Supplier';
            }
          }

          const receivedDate =
            purchase.received_date ||
            purchase.created_at ||
            new Date().toISOString();
          const items = Array.isArray(purchase.items) ? purchase.items : [];

          if (items.length === 0) {
            console.warn('[InventoryPage] Purchase order has no items:', purchase.id);
          }

          for (const poItem of items) {
            const purchaseItemId = poItem.id ? String(poItem.id) : undefined;
            if (!purchaseItemId) {
              console.warn('[InventoryPage] Skipping purchase item without id:', poItem);
              continue;
            }

            const existingRecord = await db.purchaseHistory.get(purchaseItemId as any);
            if (existingRecord?._dirty) {
              console.log('[InventoryPage] Skipping purchase item overwrite because local record is dirty:', purchaseItemId);
              continue;
            }

            // Determine product name
            let productName = poItem.item_name || 'Unknown';
            if (productName === 'Unknown' && poItem.inventory_item) {
              try {
                const inv = await db.inventory.get(poItem.inventory_item);
                if (inv) productName = inv.name;
              } catch {}
            }

            const quantityReceivedRaw = poItem.quantity_received ?? poItem.quantityReceived ?? 0;
            const quantityReceivedParsed = Number(quantityReceivedRaw);
            const parsedQuantityReceived = Number.isFinite(quantityReceivedParsed) ? quantityReceivedParsed : 0;

            const quantityRemainingRaw = poItem.quantity_remaining ?? poItem.quantityRemaining;
            const quantityRemainingParsed = Number(quantityRemainingRaw);
            const hasExplicitRemaining =
              quantityRemainingRaw !== undefined &&
              quantityRemainingRaw !== null &&
              quantityRemainingRaw !== '' &&
              Number.isFinite(quantityRemainingParsed);

            const normalizedQuantities = normalizePurchaseBatchQuantities(
              parsedQuantityReceived,
              hasExplicitRemaining
                ? Math.max(0, quantityRemainingParsed)
                : Math.max(0, existingRecord?.quantityRemaining ?? parsedQuantityReceived)
            );
            const quantityReceived = normalizedQuantities.quantityReceived;
            const quantityRemaining = normalizedQuantities.quantityRemaining;

            const sessionIdRaw = poItem.session_id ?? poItem.sessionId ?? existingRecord?.sessionId;
            const sessionId = sessionIdRaw !== undefined && sessionIdRaw !== null && sessionIdRaw !== ''
              ? String(sessionIdRaw)
              : undefined;

            await db.purchaseHistory.put({
              id: purchaseItemId,
              purchaseOrderId: purchase.id,
              branchId: branchId,
              supplierId: purchase.supplier,  // ✅ Can be null
              supplierName: supplierName,  // ✅ Will be 'No Supplier' if null
              eisStockReceiptSource:
                (purchase as any).eis_stock_receipt_source ||
                (purchase as any).eisStockReceiptSource ||
                existingRecord?.eisStockReceiptSource,
              productId: poItem.inventory_item,
              productName: productName,
              referenceNumber:
                (purchase as any).reference_number ||
                (purchase as any).referenceNumber ||
                existingRecord?.referenceNumber,
              vatAmount: (() => {
                const rawVat =
                  (purchase as any).vat_amount ??
                  (purchase as any).vatAmount ??
                  existingRecord?.vatAmount;
                const parsedVat = Number(rawVat);
                return Number.isFinite(parsedVat) ? parsedVat : undefined;
              })(),
              taxRate: (() => {
                const rawRate = poItem.tax_rate ?? poItem.taxRate;
                const parsed = Number(rawRate);
                return Number.isFinite(parsed) ? parsed : undefined;
              })(),
              taxCalculationMethod:
                poItem.tax_calculation_method ??
                poItem.taxCalculationMethod ??
                existingRecord?.taxCalculationMethod,
              taxAmount: (() => {
                const rawAmount = poItem.tax_amount ?? poItem.taxAmount;
                const parsed = Number(rawAmount);
                return Number.isFinite(parsed) ? parsed : undefined;
              })(),
              quantityReceived: quantityReceived,
              quantityRemaining: quantityRemaining,
              costPerUnit: Number(poItem.cost_per_unit || poItem.costPerUnit || 0),
              totalCost: Number(poItem.total_cost || poItem.totalCost || 0),
              paymentStatus: purchase.payment_status || 'Pending',
              amountDue: Number(purchase.amount_due || 0),
              receivedDate: receivedDate,
              expiryDate: poItem.expiry_date,
              batchNumber: poItem.batch_number,
              sessionId,
              createdAt: purchase.created_at || poItem.created_at || existingRecord?.createdAt,
              updatedAt: purchase.updated_at || poItem.updated_at || existingRecord?.updatedAt,
            });
          }
        }
        
        console.log('[InventoryPage] Purchase history synced to local DB');
      } catch (error) {
        console.error('[InventoryPage] Failed to fetch purchase history from backend:', error);
        console.log('[InventoryPage] Falling back to local purchase history data');
      }

      try {
        // Fetch stock transfers from backend
        console.log('[InventoryPage] Fetching stock transfers from backend');
        const transferResponse = await authFetch.fetch<any>(`/inventory/transfers/?branch_id=${backendBranchId}`);
        
        let transfers = [];
        if (transferResponse && Array.isArray(transferResponse)) {
          transfers = transferResponse;
        } else if (transferResponse?.results && Array.isArray(transferResponse.results)) {
          transfers = transferResponse.results;
        }

        console.log('[InventoryPage] Received', transfers.length, 'stock transfers from backend');
        for (const transfer of transfers) {
          await db.stockTransfers.put({
            id: transfer.id,
            fromBranchId: transfer.from_branch,
            fromBranchName: transfer.from_branch_name,
            toBranchId: transfer.to_branch,
            toBranchName: transfer.to_branch_name,
            itemId: transfer.inventory_item,
            itemName: transfer.item_name,
            quantity: parseFloat(transfer.quantity || 0),
            initiatedBy: transfer.initiated_by,
            createdAt: transfer.created_at,
          });
        }
      } catch (error) {
        console.error('[InventoryPage] Failed to fetch stock transfers from backend:', error);
        console.log('[InventoryPage] Falling back to local stock transfers data');
      }

      try {
        // Fetch waste log from backend
        console.log('[InventoryPage] Fetching waste log from backend');
        const wasteResponse = await authFetch.fetch<any>(`/inventory/waste/?branch_id=${backendBranchId}`);
        
        let wasteRecords = [];
        if (wasteResponse && Array.isArray(wasteResponse)) {
          wasteRecords = wasteResponse;
        } else if (wasteResponse?.results && Array.isArray(wasteResponse.results)) {
          wasteRecords = wasteResponse.results;
        }

        console.log('[InventoryPage] Received', wasteRecords.length, 'waste records from backend');
        for (const waste of wasteRecords) {
          const sessionIdRaw = waste.session_id ?? waste.sessionId ?? waste.session;
          const sessionId = sessionIdRaw !== undefined && sessionIdRaw !== null && sessionIdRaw !== ''
            ? String(sessionIdRaw)
            : undefined;
          const inventoryItemId = String(
            waste.inventory_item ?? waste.inventoryItem ?? waste.item_id ?? waste.itemId ?? ''
          ).trim();
          let itemName =
            waste.item_name ??
            waste.itemName ??
            waste.inventory_item_name ??
            waste.inventoryItemName;

          if ((!itemName || String(itemName).trim() === '') && inventoryItemId) {
            try {
              const inventoryItem = await db.inventory.get(inventoryItemId);
              if (inventoryItem?.name) {
                itemName = inventoryItem.name;
              }
            } catch {
              // Ignore lookup errors and keep fallback below.
            }
          }

          await db.wasteLog.put({
            id: waste.id,
            branchId: branchId,
            itemId: inventoryItemId,
            itemName: String(itemName || 'Unknown Item'),
            quantity: parseFloat(waste.quantity || 0),
            unit: waste.unit,
            cost: parseFloat(waste.cost || 0),
            reason: waste.reason,
            notes: waste.notes,
            // MRA EIS Fields
            affectsTax: waste.affects_tax || false,
            approvedBy: waste.approved_by || '',
            recordedAt: waste.recorded_at,
            recordedBy: waste.recorded_by,
            sessionId,
            createdAt: waste.created_at,
          });
        }
        // Reconcile local waste log with backend: remove local records no longer present remotely
        try {
          const backendWasteIds = new Set<string>(wasteRecords.map((w: any) => w.id));
          const localWaste = await db.wasteLog.where({ branchId: branchId }).toArray();
          for (const rec of localWaste) {
            if (!backendWasteIds.has(rec.id) && !rec._dirty) {
              await db.wasteLog.delete(rec.id);
              console.log('[InventoryPage] Deleted local waste record not on backend:', rec.id);
            }
          }
        } catch (wasteReconcileErr) {
          console.warn('[InventoryPage] Waste log reconciliation skipped/failed:', wasteReconcileErr);
        }
      } catch (error) {
        console.error('[InventoryPage] Failed to fetch waste log from backend:', error);
        console.log('[InventoryPage] Falling back to local waste log data');
      }

      try {
        // Fetch MRA product mappings from backend
        console.log('[InventoryPage] Fetching MRA product mappings from backend');
        const mraResponse = await authFetch.fetch<any>(`/inventory/mra-mappings/?branch_id=${backendBranchId}`);
        
        let mraMappings = [];
        if (mraResponse && Array.isArray(mraResponse)) {
          mraMappings = mraResponse;
        } else if (mraResponse?.results && Array.isArray(mraResponse.results)) {
          mraMappings = mraResponse.results;
        }

        console.log('[InventoryPage] Received', mraMappings.length, 'MRA product mappings from backend');
        // Note: MRA mappings are stored only in backend, not in local database
        // They are fetched on-demand when needed
      } catch (error) {
        console.error('[InventoryPage] Failed to fetch MRA mappings from backend:', error);
        console.log('[InventoryPage] MRA mappings will be fetched from backend on-demand');
      }

      try {
        // Fetch inventory snapshots from backend
        console.log('[InventoryPage] Fetching inventory snapshots from backend');
        const snapshotResponse = await authFetch.fetch<any>(`/inventory/snapshots/?branch_id=${backendBranchId}`);
        
        let snapshots = [];
        if (snapshotResponse && Array.isArray(snapshotResponse)) {
          snapshots = snapshotResponse;
        } else if (snapshotResponse?.results && Array.isArray(snapshotResponse.results)) {
          snapshots = snapshotResponse.results;
        }

        console.log('[InventoryPage] Received', snapshots.length, 'inventory snapshots from backend');
        for (const snapshot of snapshots) {
          await db.inventorySnapshots.put({
            id: snapshot.id,
            inventoryItemId: snapshot.inventory_item,
            branchId: branchId,
            quantityBeforeSale: parseFloat(snapshot.quantity_before_sale || 0),
            quantitySold: parseFloat(snapshot.quantity_sold || 0),
            quantityAfterSale: parseFloat(snapshot.quantity_after_sale || 0),
            relatedInvoiceNumber: snapshot.related_invoice_number,
            relatedOrderId: snapshot.related_order_id,
            productPrice: parseFloat(snapshot.product_price || 0),
            productTaxRate: parseFloat(snapshot.product_tax_rate || 0),
            productTaxType: snapshot.product_tax_type,
            createdAt: snapshot.created_at,
          });
        }
      } catch (error) {
        console.error('[InventoryPage] Failed to fetch inventory snapshots from backend:', error);
        console.log('[InventoryPage] Falling back to local inventory snapshots data');
      }

      console.log('[InventoryPage] Data fetch from backend completed');
    } catch (error) {
      console.error('[InventoryPage] Unexpected error during data fetch:', error);
      console.log('[InventoryPage] Using local data as fallback');
    } finally {
      setIsLoadingData(false);
    }
  };

  // Check sync status on mount and periodically
  useEffect(() => {
    const checkSyncStatus = () => {
      const status = getInventorySyncStatus();
      setSyncStatus(status);
    };

    checkSyncStatus();
    const interval = setInterval(checkSyncStatus, 30000); // Check every 30 seconds
    return () => clearInterval(interval);
  }, []);

  // Read tab parameter from URL
  useEffect(() => {
    const tabParam = searchParams.get('tab');
    if (activeBranchId && isWarehouseBranchId(activeBranchId)) {
      setActiveTab('inventory');
      return;
    }

    const visibleTabs = isCashierInventoryViewer
      ? ['inventory', 'transfers']
      : SHOW_WASTE_TAB
        ? ['inventory', 'purchases', 'transfers', 'waste', 'mra']
        : ['inventory', 'purchases', 'transfers', 'mra'];

    if (tabParam && visibleTabs.includes(tabParam)) {
      setActiveTab(tabParam);
    } else if (!SHOW_WASTE_TAB && tabParam === 'waste') {
      setActiveTab('inventory');
    } else if (!visibleTabs.includes(activeTab)) {
      setActiveTab('inventory');
    }
  }, [activeBranchId, activeTab, isCashierInventoryViewer, searchParams]);

  useEffect(() => {
    if (searchParams.get('reconcile') !== '1') {
      return;
    }
    if (isCashierInventoryViewer || (activeBranchId && isWarehouseBranchId(activeBranchId))) {
      return;
    }

    setActiveTab('purchases');
    setEditingPurchase(null);
    setReceiveStockDefaultSource('supplier_sale');
    setReceiveStockOpen(true);

    if (hasShownReconciliationPromptRef.current) {
      return;
    }
    hasShownReconciliationPromptRef.current = true;

    const showReconciliationToast = (warnings: StockReconciliationWarning[]) => {
      const totalMissing = warnings.reduce((sum, item) => sum + Number(item.missingBatchQuantity || 0), 0);
      toast({
        title: 'Create local batch details',
        description: warnings.length > 0
          ? `${warnings.length} product${warnings.length === 1 ? '' : 's'} need batch/expiry details for ${Number(totalMissing.toFixed(3))} synced unit${Math.abs(totalMissing - 1) < 0.0001 ? '' : 's'}. Split purchases by supplier as needed.`
          : 'Use this receipt mode when EIS already updated stock from a B2B stock transfer and you only need local batch/expiry details.',
      });
    };

    const stored = loadMraStockReconciliationWarnings();
    const storedWarnings = stored?.warnings || [];
    setReceiveStockReconciliationDefaults(storedWarnings);

    const branchIdForWarnings = activeBranchId || localStorage.getItem(LOCAL_STORAGE_KEYS.ACTIVE_BRANCH) || '';
    if (!branchIdForWarnings) {
      showReconciliationToast(storedWarnings);
      return;
    }

    void getMraStockReconciliationWarnings(branchIdForWarnings)
      .then((freshWarnings) => {
        const warnings = freshWarnings.length > 0 ? freshWarnings : storedWarnings;
        setReceiveStockReconciliationDefaults(warnings);
        storeMraStockReconciliationWarnings(warnings);
        showReconciliationToast(warnings);
      })
      .catch((error) => {
        console.warn('[InventoryPage] Failed to refresh MRA stock reconciliation defaults:', error);
        showReconciliationToast(storedWarnings);
      });
  }, [activeBranchId, isCashierInventoryViewer, searchParams]);

  // Read modal parameter from URL
  useEffect(() => {
    if (activeBranchId && isWarehouseBranchId(activeBranchId)) {
      setAddFormOpen(false);
      setReceiveStockOpen(false);
      setTransferStockOpen(false);
      setWasteModalOpen(false);
      return;
    }

    const modalParam = searchParams.get('modal');
    if (modalParam === 'transfer') {
      setTransferStockOpen(true);
    } else if (!canManageInventory) {
      setAddFormOpen(false);
      setReceiveStockOpen(false);
      setWasteModalOpen(false);
      setImportModalOpen(false);
    } else if (modalParam === 'receive') {
      setReceiveStockOpen(true);
    } else if (SHOW_WASTE_TAB && modalParam === 'waste') {
      setWasteModalOpen(true);
    } else if (modalParam === 'add-item') {
      setAddFormOpen(true);
    }
  }, [canManageInventory, searchParams]);

  const handleSyncFromBackend = async () => {
    if (!activeBranchId) return;
    if (isWarehouseBranchId(activeBranchId)) {
      toast({
        title: 'Use Refresh',
        description: 'Warehouse stock comes directly from MRA.',
      });
      return;
    }

    setIsSyncing(true);
    try {
      const result = isEisEnabled
        ? await refreshInventoryFromMraApprovedProducts(activeBranchId)
        : await syncInventoryFromBackend(activeBranchId);
      
      if (result.error) {
        toast({
          variant: 'destructive',
          title: 'Sync Failed',
          description: result.error,
        });
      } else {
        markInventorySynced();
        setSyncStatus({ hasPendingSync: false });
        toast({
          title: 'Sync Complete',
          description: isEisEnabled
            ? `Synced ${result.synced} products from MRA-approved catalog`
            : `Synced ${result.synced} products (${result.created} new, ${result.updated} updated)`,
        });
        const syncDiagnostics = formatMraProductSyncDiagnostics(result);
        if (syncDiagnostics) {
          toast({
            variant: 'destructive',
            title: 'Some EIS products need attention',
            description: syncDiagnostics,
          });
        }
        if ((result.stockReconciliationWarnings || []).length > 0) {
          const warningCount = result.stockReconciliationWarnings?.length || 0;
          toast({
            title: 'EIS stock needs batch details',
            description: `${warningCount} product${warningCount === 1 ? '' : 's'} have EIS stock without matching local batches, usually from B2B stock transfers. Create one or more purchases by supplier to record batch/expiry details.`,
            action: (
              <ToastAction
                altText="Create purchases"
                onClick={() => {
                  setActiveTab('purchases');
                  setEditingPurchase(null);
                  setReceiveStockDefaultSource('supplier_sale');
                  setReceiveStockReconciliationDefaults(result.stockReconciliationWarnings || []);
                  setReceiveStockOpen(true);
                }}
              >
                Create purchases
              </ToastAction>
            ),
          });
        }
      }
    } catch (error) {
      toast({
        variant: 'destructive',
        title: 'Sync Error',
        description: 'Failed to sync inventory from backend',
      });
    } finally {
      setIsSyncing(false);
    }
  };

  
  const inventoryData = useLiveQuery(() => {
    if (!activeBranchId) return [];
    if (isWarehouseBranchId(activeBranchId)) return [];
    console.log('[InventoryPage] Querying inventory for branch:', activeBranchId);
    return getInventoryItemsForBranch(activeBranchId).then(data => {
      // Filter out items marked for deletion
      const activeItems = data.filter(item => item._operation !== 'delete');
      console.log('[InventoryPage] Query result:', activeItems.length, 'items (filtered from', data.length, 'total)');
      return activeItems;
    });
  }, [activeBranchId], []);
  
  const suppliersData = useLiveQuery(() => {
    if (!activeBusinessId) return [];
    return db.suppliers.where('businessId').equals(activeBusinessId).toArray();
  }, [activeBusinessId], []);

  const purchaseHistoryData = useLiveQuery(
    () => {
      if (!activeBranchId) return [];
      if (isWarehouseBranchId(activeBranchId)) return [];
      return db.purchaseHistory
        .where({ branchId: activeBranchId })
        .toArray()
        .then((data) =>
          data
            .filter((record) => record._operation !== 'delete')
            .sort((a, b) => new Date(b.receivedDate).getTime() - new Date(a.receivedDate).getTime())
        )
    },
    [activeBranchId],
    []
  );
  
  const stockTransfersData = useLiveQuery(() => {
      if (!activeBranchId) return [];
      if (isWarehouseBranchId(activeBranchId)) return [];
      return db.stockTransfers
        .where('fromBranchId')
        .equals(activeBranchId)
        .or('toBranchId')
        .equals(activeBranchId)
        .reverse()
        .sortBy('createdAt')
        .then((data) => data.filter((record) => record._operation !== 'delete'));
  }, [activeBranchId], []);

  const wasteLogData = useLiveQuery(() => {
      if (!activeBranchId) return [];
      if (isWarehouseBranchId(activeBranchId)) return [];
      return db.wasteLog
        .where({ branchId: activeBranchId })
        .reverse()
        .sortBy('recordedAt')
        .then((data) => data.filter((record) => record._operation !== 'delete'));
  }, [activeBranchId], []);

  const mraMappingsData = useLiveQuery(() => {
      if (!activeBranchId) return [];
      if (isWarehouseBranchId(activeBranchId)) return [];

      const branchCandidates = new Set(getBranchIdCandidates(activeBranchId));
      const inventoryIds = new Set((inventoryData || []).map((item) => String(item.id)));

      return db.mraMappings
        .toArray()
        .then((data) =>
          data.filter((mapping) => {
            if (mapping._operation === 'delete') {
              return false;
            }

            const mappingBranchId = String(mapping.branchId || '').trim();
            return branchCandidates.has(mappingBranchId) || inventoryIds.has(String(mapping.inventoryItemId || ''));
          })
        );
  }, [activeBranchId, inventoryData], []);

  const businessSettingsRecord = useLiveQuery(
    () => {
      if (!activeBusinessId) return undefined;
      return db.businessSettings.get(String(activeBusinessId));
    },
    [activeBusinessId],
    undefined
  );

  const isEisEnabled = React.useMemo(() => {
    let cachedSettings: any = {};
    if (typeof window !== 'undefined') {
      try {
        const raw = window.localStorage.getItem(LOCAL_STORAGE_KEYS.BUSINESS_SETTINGS);
        cachedSettings = raw ? JSON.parse(raw) : {};
      } catch {
        cachedSettings = {};
      }
    }

    const settingsBelongToBusiness =
      !cachedSettings?.businessId ||
      !activeBusinessId ||
      String(cachedSettings.businessId) === String(activeBusinessId);

    return Boolean(
      readBooleanFlag(businessSettingsRecord?.enableEis) ??
      readBooleanFlag((business as any)?.enableEis) ??
      readBooleanFlag((business as any)?.enable_eis) ??
      (settingsBelongToBusiness
        ? readBooleanFlag(cachedSettings?.enableEis ?? cachedSettings?.enable_eis)
        : undefined) ??
      false
    );
  }, [activeBusinessId, business, businessSettingsRecord]);

  const isMobile = useIsMobile();
  const isWarehouseSelected = Boolean(activeBranchId && isWarehouseBranchId(activeBranchId));
  
  const ingredients = inventoryData?.filter(item => item.itemType === 'ingredient') || [];
  const groupedPurchaseCount = React.useMemo(() => {
    const purchaseGroups = new Set(
      (purchaseHistoryData || []).map((record) => record.purchaseOrderId || `${record.receivedDate}-${record.supplierId}`)
    );
    return purchaseGroups.size;
  }, [purchaseHistoryData]);
  const inventoryCountLabel = `(${inventoryData.length} item${inventoryData.length === 1 ? '' : 's'})`;
  const purchaseCountLabel = `(${groupedPurchaseCount} item${groupedPurchaseCount === 1 ? '' : 's'})`;
  const transferCountLabel = `(${stockTransfersData.length} item${stockTransfersData.length === 1 ? '' : 's'})`;
  const wasteCountLabel = `(${wasteLogData.length} item${wasteLogData.length === 1 ? '' : 's'})`;
  const mraCountLabel = `(${mraMappingsData.length} item${mraMappingsData.length === 1 ? '' : 's'})`;

  // Use one current-context load for branch changes. The branch-change event
  // only updates state so a stale EIS-disabled closure cannot race this fetch.
  useEffect(() => {
    if (!activeBranchId || isWarehouseBranchId(activeBranchId)) {
      return;
    }

    console.log('[InventoryPage] Pulling server data for branch:', activeBranchId, 'EIS enabled:', isEisEnabled);
    pullServerData(activeBranchId);
  }, [isEisEnabled, activeBranchId, activeBusinessId]);

  useEffect(() => {
    if (!isDeleteAllInventoryOpen && !isDeletingAllInventory) {
      setDeleteProgress(0);
      setDeleteStage('');
    }
  }, [isDeleteAllInventoryOpen, isDeletingAllInventory]);

  useEffect(() => {
    if (!isRestoreInclusiveCostsOpen && !isRestoringInclusiveCosts) {
      setRestoreInclusiveCostsProgress(0);
      setRestoreInclusiveCostsStage('');
    }
  }, [isRestoreInclusiveCostsOpen, isRestoringInclusiveCosts]);

  const handleRestoreInclusiveCosts = async () => {
    if (!activeBranchId) {
      return;
    }

    setIsRestoringInclusiveCosts(true);
    setRestoreInclusiveCostsProgress(5);
    setRestoreInclusiveCostsStage('Collecting purchase history');

    try {
      const [inventoryItems, purchaseHistoryRecords] = await Promise.all([
        getInventoryItemsForBranch(activeBranchId),
        getBranchScopedRows(db.purchaseHistory, activeBranchId),
      ]);

      const activeInventoryItems = inventoryItems.filter((item) => item._operation !== 'delete');
      const activePurchaseHistory = (purchaseHistoryRecords as PurchaseRecord[]).filter(
        (record) => record._operation !== 'delete'
      );

      const latestInclusivePurchaseByProduct = new Map<string, PurchaseRecord>();
      for (const record of activePurchaseHistory) {
        const productId = String(record.productId || '').trim();
        const taxRate = normalizeTaxRate(record.taxRate);
        const taxMethod = resolveTaxMethod(record.taxCalculationMethod);
        const grossCost = roundTo(toSafeNumber(record.costPerUnit), 4);

        if (!productId || taxMethod !== 'inclusive' || taxRate <= 0 || grossCost <= 0) {
          continue;
        }

        const existingRecord = latestInclusivePurchaseByProduct.get(productId);
        if (!existingRecord || getTimestamp(record.receivedDate) > getTimestamp(existingRecord.receivedDate)) {
          latestInclusivePurchaseByProduct.set(productId, record);
        }
      }

      setRestoreInclusiveCostsProgress(20);
      setRestoreInclusiveCostsStage('Matching products to inclusive purchases');

      const updates: Array<{
        itemId: string;
        itemName: string;
        previousCost: number;
        restoredCost: number;
        nextValue: number;
        taxRate: number;
        receivedDate: string;
        operation?: InventoryItem['_operation'];
      }> = [];
      let skippedWithoutSource = 0;
      let skippedAlreadyCorrect = 0;
      let skippedWithoutCost = 0;

      for (const item of activeInventoryItems) {
        const itemId = String(item.id || '').trim();
        const currentCost = roundTo(toSafeNumber(item.cost), 4);

        if (!itemId) {
          continue;
        }

        if (currentCost <= 0) {
          skippedWithoutCost += 1;
          continue;
        }

        const sourcePurchase = latestInclusivePurchaseByProduct.get(itemId);
        if (!sourcePurchase) {
          skippedWithoutSource += 1;
          continue;
        }

        const restoredCost = roundTo(toSafeNumber(sourcePurchase.costPerUnit), 4);
        const taxRate = normalizeTaxRate(sourcePurchase.taxRate);

        if (!costLooksVatStripped(currentCost, restoredCost, taxRate)) {
          skippedAlreadyCorrect += 1;
          continue;
        }

        const stockUnits = toSafeNumber(item.stockUnits ?? item.stock_units);
        updates.push({
          itemId,
          itemName: item.name,
          previousCost: currentCost,
          restoredCost,
          nextValue: roundTo(stockUnits * restoredCost, 2),
          taxRate,
          receivedDate: sourcePurchase.receivedDate,
          operation: item._operation,
        });
      }

      if (updates.length === 0) {
        toast({
          title: 'No cost reversals needed',
          description:
            latestInclusivePurchaseByProduct.size === 0
              ? 'No inclusive purchase records were found for this branch.'
              : 'No products still matched the older VAT-stripped cost pattern.',
        });
        setRestoreInclusiveCostsProgress(0);
        setRestoreInclusiveCostsStage('');
        setIsRestoreInclusiveCostsOpen(false);
        return;
      }

      setRestoreInclusiveCostsProgress(30);
      setRestoreInclusiveCostsStage(`Restoring ${updates.length} product costs`);

      await db.transaction('rw', [db.inventory, db.auditLog], async () => {
        for (const [index, update] of updates.entries()) {
          await db.inventory.update(update.itemId, {
            cost: update.restoredCost,
            value: update.nextValue,
            _dirty: true,
            _operation: update.operation === 'create' ? 'create' : 'update',
          });

          const completed = index + 1;
          const progress = 30 + Math.round((completed / updates.length) * 65);
          setRestoreInclusiveCostsProgress(Math.min(95, progress));
          setRestoreInclusiveCostsStage(`Restoring ${completed} of ${updates.length}`);
        }

        await logAuditAction({
          userId: user?.uid || user?.email || 'system',
          userName: user?.displayName || user?.email || 'System',
          branchId: activeBranchId,
          actionType: 'ITEM_UPDATE',
          entityType: 'InventoryItem',
          entityId: `branch:${activeBranchId}`,
          details: {
            operation: 'restore_inclusive_costs',
            updatedItems: updates.length,
            skippedWithoutSource,
            skippedAlreadyCorrect,
            skippedWithoutCost,
            sampleItems: updates.slice(0, 25).map((update) => ({
              itemId: update.itemId,
              itemName: update.itemName,
              previousCost: update.previousCost,
              restoredCost: update.restoredCost,
              taxRate: update.taxRate,
              sourceReceivedDate: update.receivedDate,
            })),
          },
        });
      });

      setRestoreInclusiveCostsProgress(100);
      setRestoreInclusiveCostsStage('Inclusive costs restored');
      setIsRestoreInclusiveCostsOpen(false);

      toast({
        title: 'Inclusive costs restored',
        description: `Updated ${updates.length} product cost${updates.length === 1 ? '' : 's'} and queued them for sync.`,
      });
    } catch (error) {
      console.error('[InventoryPage] Failed to restore inclusive costs:', error);
      setRestoreInclusiveCostsStage('Restore failed');
      toast({
        variant: 'destructive',
        title: 'Restore failed',
        description: 'Could not restore the full inclusive costs for this branch.',
      });
    } finally {
      setIsRestoringInclusiveCosts(false);
    }
  };

  const handleDeleteAllInventoryData = async () => {
    if (!activeBranchId) {
      return;
    }

    const branchCandidates = getBranchIdCandidates(activeBranchId);
    const branchCandidateSet = new Set(branchCandidates);
    const isOnline = typeof window !== 'undefined' && navigator.onLine;

    setIsDeletingAllInventory(true);
    setDeleteProgress(5);
    setDeleteStage('Preparing deletion');
    try {
      const [
        inventoryItems,
        purchaseHistoryRecords,
        purchaseOrders,
        wasteRecords,
        stockTransfers,
        inventorySnapshots,
        stockAudits,
        cartItems,
        allMappings,
      ] = await Promise.all([
        getInventoryItemsForBranch(activeBranchId),
        getBranchScopedRows(db.purchaseHistory, activeBranchId),
        getBranchScopedRows(db.purchaseOrders, activeBranchId),
        getBranchScopedRows(db.wasteLog, activeBranchId),
        db.stockTransfers.toArray(),
        getBranchScopedRows(db.inventorySnapshots, activeBranchId),
        getBranchScopedRows(db.stockAudits, activeBranchId),
        getBranchScopedRows(db.cart, activeBranchId),
        db.mraMappings.toArray(),
      ]);

      setDeleteProgress(15);
      setDeleteStage('Collecting records');

      const inventoryIds = new Set(inventoryItems.map((item) => String(item.id)));
      const branchTransfers = stockTransfers.filter((transfer) => {
        const fromBranchId = String(transfer.fromBranchId || '').trim();
        const toBranchId = String(transfer.toBranchId || '').trim();
        return branchCandidateSet.has(fromBranchId) || branchCandidateSet.has(toBranchId);
      });
      const branchMappings = allMappings.filter((mapping) => {
        const mappingBranchId = String(mapping.branchId || '').trim();
        return branchCandidateSet.has(mappingBranchId) || inventoryIds.has(String(mapping.inventoryItemId));
      });

      const totalRecordsToClear =
        inventoryItems.length +
        purchaseHistoryRecords.length +
        purchaseOrders.length +
        wasteRecords.length +
        branchTransfers.length +
        branchMappings.length +
        inventorySnapshots.length +
        stockAudits.length +
        cartItems.length;

      if (totalRecordsToClear === 0) {
        toast({
          title: 'Nothing to clear',
          description: 'There is no inventory data for the current branch.',
        });
        setDeleteProgress(0);
        setDeleteStage('');
        setIsDeleteAllInventoryOpen(false);
        return;
      }

      const nowIso = new Date().toISOString();
      let processedRecords = 0;
      const localWorkWeight = 65;
      const updateDeleteProgress = (stage: string, increment = 0) => {
        processedRecords += increment;
        const nextValue = Math.min(
          localWorkWeight,
          15 + Math.round((processedRecords / totalRecordsToClear) * (localWorkWeight - 15))
        );
        setDeleteStage(stage);
        setDeleteProgress((prev) => Math.max(prev, nextValue));
      };

      await db.transaction(
        'rw',
        [
          db.inventory,
          db.purchaseHistory,
          db.purchaseOrders,
          db.wasteLog,
          db.stockTransfers,
          db.mraMappings,
          db.inventorySnapshots,
          db.stockAudits,
          db.cart,
          db.auditLog,
        ],
        async () => {
          updateDeleteProgress('Queueing inventory items');
          for (const item of inventoryItems) {
            await db.inventory.update(item.id, {
              _dirty: true,
              _operation: 'delete',
              _deletedAt: nowIso,
            } as any);
            updateDeleteProgress('Queueing inventory items', 1);
          }

          updateDeleteProgress('Queueing purchase history');
          for (const record of purchaseHistoryRecords) {
            const recordId = String(record.id ?? '').trim();
            if (!recordId || typeof record.id === 'number' || /^\d+$/.test(recordId)) {
              await db.purchaseHistory.delete(record.id);
              updateDeleteProgress('Queueing purchase history', 1);
              continue;
            }

            await db.purchaseHistory.update(record.id, {
              _dirty: true,
              _operation: 'delete',
            });
            updateDeleteProgress('Queueing purchase history', 1);
          }

          updateDeleteProgress('Queueing purchase orders');
          for (const order of purchaseOrders) {
            await db.purchaseOrders.update(order.id, {
              _dirty: true,
              _operation: 'delete',
              updatedAt: nowIso,
            });
            updateDeleteProgress('Queueing purchase orders', 1);
          }

          updateDeleteProgress('Queueing waste records');
          for (const waste of wasteRecords) {
            await db.wasteLog.update(waste.id, {
              _dirty: true,
              _operation: 'delete',
            });
            updateDeleteProgress('Queueing waste records', 1);
          }

          updateDeleteProgress('Queueing stock transfers');
          for (const transfer of branchTransfers) {
            await db.stockTransfers.update(transfer.id, {
              _dirty: true,
              _operation: 'delete',
            });
            updateDeleteProgress('Queueing stock transfers', 1);
          }

          updateDeleteProgress('Queueing MRA mappings');
          for (const mapping of branchMappings) {
            await db.mraMappings.put({
              ...mapping,
              _dirty: true,
              _operation: 'delete',
              updatedAt: nowIso,
            });
            updateDeleteProgress('Queueing MRA mappings', 1);
          }

          if (inventorySnapshots.length > 0) {
            await db.inventorySnapshots.bulkDelete(inventorySnapshots.map((snapshot) => snapshot.id));
            updateDeleteProgress('Removing snapshots', inventorySnapshots.length);
          }

          if (stockAudits.length > 0) {
            await db.stockAudits.bulkDelete(stockAudits.map((audit) => audit.id));
            updateDeleteProgress('Removing stock audits', stockAudits.length);
          }

          if (cartItems.length > 0) {
            await db.cart.bulkDelete(cartItems.map((item) => item.id));
            updateDeleteProgress('Removing saved carts', cartItems.length);
          }

          await logAuditAction({
            userId: user?.uid || user?.email || 'system',
            userName: user?.displayName || user?.email || 'System',
            branchId: activeBranchId,
            actionType: 'ITEM_DELETE',
            entityType: 'InventoryItem',
            entityId: `branch:${activeBranchId}`,
            details: {
              clearedAt: nowIso,
              inventoryItems: inventoryItems.length,
              purchaseHistory: purchaseHistoryRecords.length,
              purchaseOrders: purchaseOrders.length,
              wasteRecords: wasteRecords.length,
              stockTransfers: branchTransfers.length,
              mraMappings: branchMappings.length,
              inventorySnapshots: inventorySnapshots.length,
              stockAudits: stockAudits.length,
              cartItems: cartItems.length,
            },
          });
        }
      );

      setDeleteProgress(70);
      setDeleteStage('Syncing deletions');
      setSearchTerm('');
      setActiveTab('inventory');

      const { syncService } = await import('@/lib/services/sync-service');
      await syncService.performFullSync(activeBranchId, {
        onProgress: (progress) => {
          const syncBase = 70;
          const syncSpan = 30;
          const percent = typeof progress.percent === 'number' ? progress.percent : undefined;
          let nextValue = syncBase;

          if (progress.stage === 'inventory' && percent !== undefined) {
            nextValue = syncBase + Math.round((percent / 100) * syncSpan);
          } else if (progress.stage === 'pull') {
            nextValue = syncBase + Math.round(syncSpan * 0.85);
          } else if (progress.stage === 'done') {
            nextValue = 100;
          } else if (progress.stage === 'error') {
            nextValue = syncBase + Math.round(syncSpan * 0.9);
          }

          setDeleteProgress((prev) => Math.max(prev, Math.min(100, nextValue)));
          if (progress.message) {
            setDeleteStage(progress.message);
          }
        },
      });
      setDeleteProgress(100);
      setDeleteStage('Deletion complete');
      setMraRefreshKey((current) => current + 1);
      setIsDeleteAllInventoryOpen(false);

      toast({
        title: 'Inventory data cleared',
        description: isOnline
          ? 'Current-branch inventory data was cleared locally and sync was requested.'
          : 'Current-branch inventory data was cleared locally and queued for sync when you are back online.',
      });
    } catch (error) {
      console.error('[InventoryPage] Failed to clear inventory data:', error);
      toast({
        variant: 'destructive',
        title: 'Clear failed',
        description: 'Could not clear the current branch inventory data.',
      });
      setDeleteStage('Deletion failed');
    } finally {
      setIsDeletingAllInventory(false);
    }
  };
  
  const handleFormOpenChange = (open: boolean) => {
    if (open && !canManageInventory) return;
    setAddFormOpen(open);
    if (!open) {
      setEditingItem(undefined);
    }
  };

  const handleEditItem = (item: InventoryItem) => {
    if (!canManageInventory) return;
    setEditingItem(item);
    setAddFormOpen(true);
  };

  const searchPlaceholder =
    isWarehouseSelected
      ? 'Search warehouse products...'
      : activeTab === 'purchases'
      ? 'Search supplier, product, batch, or payment...'
      : activeTab === 'waste'
        ? 'Search item, reason, or recorded by...'
        : activeTab === 'transfers'
          ? 'Search item, branch, or initiated by...'
          : activeTab === 'mra'
            ? 'Search product, MRA code, or mapping...'
            : 'Search products or scan barcode...';

  // Barcode scanner listener - search for products by barcode on inventory screen
  React.useEffect(() => {
    const handleBarcodeSearch = (e: KeyboardEvent) => {
      // Only process if we're on the inventory tab AND search field is focused
      if (isWarehouseSelected || activeTab !== 'inventory' || !isSearchFocused || e.key !== 'Enter') {
        return;
      }

      const searchValue = searchTerm.trim();

      if (!searchValue) {
        return;
      }

      console.log('[InventoryPage] Enter pressed, searching for barcode:', searchValue);
      const normalizedActiveBranchId = toBackendBranchId(activeBranchId);
      db.inventory.where('barcode').equals(searchValue).toArray().then(products => {
        const product = products.find(
          (item) =>
            toBackendBranchId(item.branchId) === normalizedActiveBranchId &&
            item._operation !== 'delete'
        );

        if (product) {
          console.log('[InventoryPage] Found product by barcode:', product.name);
          if (canManageInventory) {
            handleEditItem(product);
          }
          toast({
            title: 'Product Found',
            description: `Found: ${product.name}`,
          });
          setSearchTerm('');
        }
      }).catch(error => {
        console.error('[InventoryPage] Error searching for barcode:', error);
      });
    };

    window.addEventListener('keydown', handleBarcodeSearch, false);
    return () => {
      window.removeEventListener('keydown', handleBarcodeSearch, false);
    };
  }, [activeTab, isSearchFocused, activeBranchId, searchTerm, isWarehouseSelected, canManageInventory]);
  
  if (authLoading || !activeBranchId) {
    return (
        <div className="flex h-full items-center justify-center">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
        </div>
    )
  }

  return (
    <>
    <div className="flex flex-col gap-6">
      <div className="flex w-full flex-col items-stretch justify-between gap-4 sm:flex-row sm:items-center">
        <div className="grid gap-2">
          <h1 className="text-2xl font-bold tracking-tight">Inventory</h1>
          <p className="text-muted-foreground">
            {isWarehouseSelected
              ? 'View MRA warehouse stock and transfer it to a branch.'
              : 'Manage all your items, from raw ingredients to final products.'}
          </p>
        </div>
        <div className="flex w-full flex-col gap-2 sm:max-w-xs">
        </div>
      </div>
      
      <Card>
        <Tabs value={activeTab} onValueChange={setActiveTab}>
            <CardHeader>
                <div className="flex flex-col items-start gap-4 md:flex-row md:items-center md:flex-wrap">
                    <div className="w-full overflow-x-auto md:w-auto">
                    <TabsList className="inline-flex min-w-max">
                        {isWarehouseSelected ? (
                          <TabsTrigger value="inventory" className="whitespace-nowrap">
                              <Package className="mr-2 h-4 w-4" />
                              Warehouse Stock
                          </TabsTrigger>
                        ) : (
                        <>
                        <TabsTrigger value="inventory" className="whitespace-nowrap">
                            <Package className="mr-2 h-4 w-4" />
                            Current Stock
                            <span className="ml-1 text-xs text-muted-foreground">{inventoryCountLabel}</span>
                        </TabsTrigger>
                        {canManageInventory && (
                            <TabsTrigger value="purchases" className="whitespace-nowrap">
                                <History className="mr-2 h-4 w-4" />
                                Purchase History
                                <span className="ml-1 text-xs text-muted-foreground">{purchaseCountLabel}</span>
                            </TabsTrigger>
                        )}
                        <TabsTrigger value="transfers" className="whitespace-nowrap">
                            <Repeat className="mr-2 h-4 w-4" />
                            Transfers
                            <span className="ml-1 text-xs text-muted-foreground">{transferCountLabel}</span>
                        </TabsTrigger>
                        {canManageInventory && SHOW_WASTE_TAB && (
                            <TabsTrigger value="waste" className="whitespace-nowrap">
                                <Trash className="mr-2 h-4 w-4" />
                                Waste Log
                                <span className="ml-1 text-xs text-muted-foreground">{wasteCountLabel}</span>
                            </TabsTrigger>
                        )}
                        {canManageInventory && (
                            <TabsTrigger value="mra" className="whitespace-nowrap">
                                <Package className="mr-2 h-4 w-4" />
                                MRA Mappings
                                <span className="ml-1 text-xs text-muted-foreground">{mraCountLabel}</span>
                            </TabsTrigger>
                        )}
                        </>
                        )}
                    </TabsList>
                    </div>
                    <div className="ml-auto flex w-full flex-wrap items-center gap-2 md:w-auto md:flex-nowrap">
                        <div className="relative w-full flex-1 md:grow-0">
                          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                          <Input 
                            ref={searchInputRef}
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            placeholder={searchPlaceholder}
                            className="w-full pl-10 md:w-auto" 
                            onFocus={() => setIsSearchFocused(true)}
                            onBlur={() => setIsSearchFocused(false)}
                          />
                        </div>
                        {!isWarehouseSelected && (
                        <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                                <Button variant="outline">
                                    <Filter className="mr-2 h-4 w-4" /> Filter
                                </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end" className="w-[200px]">
                                <DropdownMenuLabel>Filter by Status</DropdownMenuLabel>
                                <DropdownMenuCheckboxItem checked>
                                In Stock
                                </DropdownMenuCheckboxItem>
                                <DropdownMenuCheckboxItem checked>
                                Low Stock
                                </DropdownMenuCheckboxItem>
                                <DropdownMenuCheckboxItem>
                                Out of Stock
                                </DropdownMenuCheckboxItem>
                            </DropdownMenuContent>
                        </DropdownMenu>
                        )}
                    </div>
                </div>
            </CardHeader>
            <TabsContent value="inventory">
                {isWarehouseSelected ? (
                  <WarehouseStockTab
                    branches={branches}
                    searchTerm={searchTerm}
                    currency={businessCurrency}
                  />
                ) : (
                <>
                  <InventoryTab
                      inventoryData={inventoryData}
                      isMobile={isMobile}
                      currentBusinessType={currentBusinessType}
                      searchTerm={searchTerm}
                      onAddItem={() => canManageInventory && setAddFormOpen(true)}
                      onEditItem={handleEditItem}
                      onImport={() => canManageInventory && setImportModalOpen(true)}
                      onTransfer={() => canTransferInventory && setTransferStockOpen(true)}
                      onSyncEisProducts={isEisEnabled ? handleSyncFromBackend : undefined}
                      isSyncingEisProducts={isSyncing}
                      readOnly={!canManageInventory}
                  />
                </>
                )}
            </TabsContent>
            {canManageInventory && (
            <TabsContent value="purchases">
                <PurchasesTab 
                    purchaseHistoryData={purchaseHistoryData}
                    isMobile={isMobile}
                    searchTerm={searchTerm}
                    onReceiveStock={() => {
                        setEditingPurchase(null);
                        setReceiveStockDefaultSource('pos_goods_receiving');
                        setReceiveStockReconciliationDefaults([]);
                        setReceiveStockOpen(true);
                    }}
                    onEditPurchase={(purchase) => {
                        setEditingPurchase(purchase);
                        setReceiveStockReconciliationDefaults([]);
                        setReceiveStockOpen(true);
                    }}
                    branchId={activeBranchId}
                    currency={businessCurrency}
                />
            </TabsContent>
            )}
            <TabsContent value="transfers">
                <TransfersTab
                    stockTransfersData={stockTransfersData}
                    isMobile={isMobile}
                    searchTerm={searchTerm}
                    onTransferStock={() => setTransferStockOpen(true)}
                    branchId={activeBranchId}
                />
            </TabsContent>
            {canManageInventory && SHOW_WASTE_TAB && (
                <TabsContent value="waste">
                     <WasteTab
                        wasteLogData={wasteLogData}
                        isMobile={isMobile}
                        searchTerm={searchTerm}
                        onRecordWaste={() => setWasteModalOpen(true)}
                        branchId={activeBranchId}
                     />
                </TabsContent>
            )}
            {canManageInventory && (
            <TabsContent value="mra">
                <MRAMappingsTab
                    inventoryData={inventoryData}
                    businessId={business?.id}
                    branchId={activeBranchId}
                    searchTerm={searchTerm}
                    refreshKey={mraRefreshKey}
                    isEisEnabled={isEisEnabled}
                />
            </TabsContent>
            )}
        </Tabs>
      </Card>
    </div>

    {/* Modals and Dialogs */}
    {canManageInventory && (
    <Dialog open={isAddFormOpen} onOpenChange={handleFormOpenChange}>
        <DialogContent className="sm:max-w-2xl max-h-[90vh] flex flex-col">
        <DialogHeader className="sticky top-0 bg-background z-10 pt-6">
            <DialogTitle>{editingItem ? 'Edit Item' : 'Add New Item to Inventory'}</DialogTitle>
            <DialogDescription>
                {editingItem ? `Update the details for ${editingItem.name}.` : 'Add an ingredient for tracking or a new sellable product with a recipe.'}
            </DialogDescription>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto -mx-6 px-6">
            <AddProductForm 
                branchId={activeBranchId}
                businessType={currentBusinessType} 
                suppliers={suppliersData || []}
                ingredients={ingredients}
                onFormSubmit={() => handleFormOpenChange(false)}
                defaultValues={editingItem}
            />
        </div>
        </DialogContent>
    </Dialog>
    )}
     {canManageInventory && (
     <Dialog
        open={isReceiveStockOpen}
        onOpenChange={(open) => {
            setReceiveStockOpen(open);
            if (!open) {
                setEditingPurchase(null);
                setReceiveStockDefaultSource('pos_goods_receiving');
                setReceiveStockReconciliationDefaults([]);
            }
        }}
    >
        <DialogContent className="sm:max-w-5xl max-h-[92vh] flex flex-col" onOpenAutoFocus={(event) => event.preventDefault()}>
            <DialogHeader className="sticky top-0 bg-background z-10 pt-6">
            <DialogTitle>{editingPurchase ? 'Edit Purchase' : 'Receive Stock'}</DialogTitle>
            <DialogDescription>
                {editingPurchase
                    ? 'Update an existing stock receipt without losing its batch purpose or offline sync behavior.'
                    : receiveStockReconciliationDefaults.length > 0
                      ? 'Products with missing local batch details are prefilled. Remove lines to split this receipt by supplier.'
                    : 'Record new inventory received from a supplier.'}
            </DialogDescription>
        </DialogHeader>
        <div className="-mx-6 px-6 pb-6">
            <ReceiveStockForm 
                branchId={activeBranchId}
                businessType={currentBusinessType} 
                inventoryItems={inventoryData || []} 
                suppliers={suppliersData || []}
                editingPurchase={editingPurchase}
                defaultEisStockReceiptSource={receiveStockDefaultSource}
                reconciliationDefaults={receiveStockReconciliationDefaults}
                onFormSubmit={() => {
                    setReceiveStockOpen(false);
                    setEditingPurchase(null);
                    setReceiveStockDefaultSource('pos_goods_receiving');
                    setReceiveStockReconciliationDefaults([]);
                }} 
            />
        </div>
        </DialogContent>
    </Dialog>
     )}
    <Dialog open={isTransferStockOpen} onOpenChange={setTransferStockOpen}>
        <DialogContent className="sm:max-w-md">
            <DialogHeader>
                <DialogTitle>Transfer Stock</DialogTitle>
                <DialogDescription>
                    Move inventory from this branch to another.
                </DialogDescription>
            </DialogHeader>
            <TransferStockForm
                branchId={activeBranchId}
                branches={branches}
                inventoryItems={inventoryData || []}
                onFormSubmit={() => setTransferStockOpen(false)}
            />
        </DialogContent>
    </Dialog>
    {canManageInventory && SHOW_WASTE_TAB && (
        <Dialog open={isWasteModalOpen} onOpenChange={setWasteModalOpen}>
            <DialogContent>
                <DialogHeader>
                    <DialogTitle>Record Inventory Waste</DialogTitle>
                    <DialogDescription>Log any items that were wasted, spoiled, or damaged. This will adjust your stock levels.</DialogDescription>
                </DialogHeader>
                <RecordWasteForm
                    branchId={activeBranchId}
                    inventoryItems={inventoryData || []}
                    onFormSubmit={() => setWasteModalOpen(false)}
                />
            </DialogContent>
        </Dialog>
    )}
    {canManageInventory && (
    <ImportModal
      isOpen={isImportModalOpen}
      onOpenChange={setImportModalOpen}
      branchId={activeBranchId}
      branches={branches}
      businessType={currentBusinessType}
    />
    )}
    <AlertDialog open={isRestoreInclusiveCostsOpen} onOpenChange={setIsRestoreInclusiveCostsOpen}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Restore full inclusive product costs?</AlertDialogTitle>
          <AlertDialogDescription>
            This temporary repair checks the current branch only. It looks for products whose latest inclusive purchase still suggests VAT was stripped from the saved cost, then restores the full purchase cost and recalculates stock value.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {(isRestoringInclusiveCosts || restoreInclusiveCostsProgress > 0) && (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>{restoreInclusiveCostsStage || 'Restoring...'}</span>
              <span>{Math.round(restoreInclusiveCostsProgress)}%</span>
            </div>
            <Progress value={restoreInclusiveCostsProgress} />
          </div>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={isRestoringInclusiveCosts}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={(event) => {
              event.preventDefault();
              void handleRestoreInclusiveCosts();
            }}
            disabled={isRestoringInclusiveCosts}
          >
            {isRestoringInclusiveCosts ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Restoring...
              </>
            ) : (
              'Run restore'
            )}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
    <AlertDialog open={isDeleteAllInventoryOpen} onOpenChange={setIsDeleteAllInventoryOpen}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Clear all inventory data?</AlertDialogTitle>
          <AlertDialogDescription>
            This removes the current branch inventory products, purchase history, purchase orders, waste logs, transfers, MRA mappings, snapshots, and stock audits. The records are hidden immediately and queued for backend deletion on sync.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {(isDeletingAllInventory || deleteProgress > 0) && (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>{deleteStage || 'Deleting...'}</span>
              <span>{Math.round(deleteProgress)}%</span>
            </div>
            <Progress value={deleteProgress} />
          </div>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={isDeletingAllInventory}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={(event) => {
              event.preventDefault();
              void handleDeleteAllInventoryData();
            }}
            disabled={isDeletingAllInventory}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {isDeletingAllInventory ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Clearing...
              </>
            ) : (
              'Delete everything'
            )}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
    </>
  );
}
