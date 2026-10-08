'use client';

import { db, type Order } from '@/lib/db';

const SALES_STORAGE_KEY = 'handypos-sales';
const PENDING_SALES_KEY = 'handypos-pending-sales';
const LAST_REJECTED_SALE_KEY = 'handypos-last-rejected-sale';

export interface SaleRecord extends Order {
  syncedAt?: string;
  syncError?: string;
}

interface MarkSaleFailedOptions {
  retryBlocked?: boolean;
}

/**
 * Save a completed sale to localStorage with branch information
 */
export function saveSaleToLocalStorage(order: Order, branchId?: string): void {
  try {
    if (typeof window === 'undefined') return;

    const sales = getSalesFromLocalStorage();
    const saleRecord: SaleRecord = {
      ...order,
      branchId: branchId || order.branchId, // Ensure branch is included
      syncedAt: undefined,
      syncError: undefined,
      syncStatus: 'pending',
      syncRetryBlocked: false,
    };

    sales.push(saleRecord);
    localStorage.setItem(SALES_STORAGE_KEY, JSON.stringify(sales));
    console.log('[Sales Service] Saved sale to localStorage:', order.id, 'Branch:', branchId);
  } catch (error) {
    console.error('[Sales Service] Failed to save sale to localStorage:', error);
  }
}

/**
 * Get all sales from localStorage
 */
export function getSalesFromLocalStorage(): SaleRecord[] {
  try {
    if (typeof window === 'undefined') return [];

    const sales = localStorage.getItem(SALES_STORAGE_KEY);
    return sales ? JSON.parse(sales) : [];
  } catch (error) {
    console.error('[Sales Service] Failed to get sales from localStorage:', error);
    return [];
  }
}

/**
 * Add a pending sale (waiting to sync to backend)
 */
export function addPendingSale(order: Order): void {
  try {
    if (typeof window === 'undefined') return;

    const pending = getPendingSales();
    pending.push({
      ...order,
      syncedAt: undefined,
      syncError: undefined,
      syncStatus: 'pending',
      syncRetryBlocked: false,
    });

    localStorage.setItem(PENDING_SALES_KEY, JSON.stringify(pending));
    console.log('[Sales Service] Added pending sale:', order.id);
  } catch (error) {
    console.error('[Sales Service] Failed to add pending sale:', error);
  }
}

/**
 * Remove a provisional sale after a confirmed EIS rejection.
 *
 * Network failures remain pending for retry. This path is only for a sale
 * that MRA explicitly rejected, so its local order, stock movement, session
 * totals, audit row, and localStorage records must all disappear together.
 */
export async function removeRejectedSaleLocally(
  orderId: string,
  reason = 'MRA rejected the fiscal sale.'
): Promise<void> {
  try {
    const existingOrder = await db.orders.get(orderId);
    if (existingOrder) {
      await db.transaction(
        'rw',
        db.inventory,
        db.orders,
        db.sessions,
        db.purchaseHistory,
        async () => {
          const order = await db.orders.get(orderId);
          if (!order) return;

          const recordedConsumption = Array.isArray(order.localInventoryConsumption)
            ? order.localInventoryConsumption
            : [];
          const consumption: NonNullable<Order['localInventoryConsumption']> = recordedConsumption.length > 0
            ? recordedConsumption
            : (order.items || [])
                .map((item) => ({
                  inventoryItemId: String(item.inventoryItemId || item.inventory_item_id || ''),
                  quantity: Number(item.quantity || 0),
                }))
                .filter((item) => item.inventoryItemId && Number.isFinite(item.quantity) && item.quantity > 0);

          for (const entry of consumption) {
            const quantity = Number(entry.quantity || 0);
            if (!Number.isFinite(quantity) || quantity <= 0) continue;

            if (entry.purchaseHistoryId !== undefined && entry.purchaseHistoryId !== null) {
              const batch = await db.purchaseHistory.get(entry.purchaseHistoryId as any);
              if (batch) {
                await db.purchaseHistory.update(batch.id!, {
                  quantityRemaining: Number(batch.quantityRemaining || 0) + quantity,
                  _dirty: false,
                  _operation: undefined,
                });
              }
            }

            const inventoryItem = await db.inventory.get(String(entry.inventoryItemId));
            if (inventoryItem) {
              const stockUnits = Number(inventoryItem.stockUnits || 0) + quantity;
              const reorderLevel = Number(inventoryItem.reorderLevel || 0);
              await db.inventory.update(inventoryItem.id, {
                stockUnits,
                status: stockUnits <= 0
                  ? 'Out of Stock'
                  : stockUnits <= reorderLevel
                    ? 'Low Stock'
                    : 'In Stock',
                _dirty: false,
                _operation: undefined,
              });
            }
          }

          if (order.sessionId) {
            const session = await db.sessions.get(order.sessionId);
            if (session) {
              const sessionUpdate: Record<string, any> = {
                totalSales: Math.max(0, Number(session.totalSales || 0) - Number(order.subtotal || 0)),
                _dirty: true,
                _operation: 'update',
              };
              const saleAmount = Number(order.total || 0);
              const paymentFields: Record<string, string> = {
                Cash: 'totalCashSales',
                Card: 'totalCardSales',
                'Mobile Money': 'totalMobileMoneySales',
                'On Account': 'totalOnAccountSales',
                Other: 'totalOtherSales',
              };
              const paymentField = paymentFields[order.paymentMethod];
              if (paymentField) {
                sessionUpdate[paymentField] = Math.max(
                  0,
                  Number((session as any)[paymentField] || 0) - saleAmount
                );
              }
              if (order.paymentMethod === 'Cash') {
                sessionUpdate.expectedCash = Math.max(
                  0,
                  Number(session.expectedCash || 0) - saleAmount
                );
              }
              await db.sessions.update(session.id, sessionUpdate);
            }
          }

          await db.orders.delete(orderId);
        }
      );
      await db.auditLog.where('entityId').equals(orderId).delete();
    }

    if (typeof window !== 'undefined') {
      try {
        localStorage.setItem(
          LAST_REJECTED_SALE_KEY,
          JSON.stringify({
            orderId,
            reason,
            recordedAt: new Date().toISOString(),
          })
        );
      } catch (storageError) {
        console.warn('[Sales Service] Failed to retain EIS rejection reason:', storageError);
      }
    }

    removeSaleFromLocalStorage(orderId);
    console.info('[Sales Service] Removed EIS-rejected provisional sale:', orderId, reason);
  } catch (error) {
    console.error('[Sales Service] Failed to remove EIS-rejected sale:', orderId, error);
    throw error;
  }
}

/** Read the most recent rejection reason for a sale removed after an explicit MRA rejection. */
export function getLastRejectedSaleReason(orderId: string): string {
  try {
    if (typeof window === 'undefined') return '';

    const raw = localStorage.getItem(LAST_REJECTED_SALE_KEY);
    if (!raw) return '';

    const record = JSON.parse(raw) as { orderId?: string; reason?: string };
    return record.orderId === orderId ? String(record.reason || '').trim() : '';
  } catch (error) {
    console.warn('[Sales Service] Failed to read EIS rejection reason:', error);
    return '';
  }
}

/** Remove a sale from both localStorage history and the retry queue. */
export function removeSaleFromLocalStorage(orderId: string): void {
  try {
    if (typeof window === 'undefined') return;

    const sales = getSalesFromLocalStorage().filter((sale) => sale.id !== orderId);
    localStorage.setItem(SALES_STORAGE_KEY, JSON.stringify(sales));

    const pending = getPendingSales().filter((sale) => sale.id !== orderId);
    localStorage.setItem(PENDING_SALES_KEY, JSON.stringify(pending));
  } catch (error) {
    console.error('[Sales Service] Failed to remove sale from localStorage:', error);
  }
}

/**
 * Get all pending sales
 */
export function getPendingSales(): SaleRecord[] {
  try {
    if (typeof window === 'undefined') return [];

    const pending = localStorage.getItem(PENDING_SALES_KEY);
    return pending ? JSON.parse(pending) : [];
  } catch (error) {
    console.error('[Sales Service] Failed to get pending sales:', error);
    return [];
  }
}

/**
 * Mark a sale as synced
 */
export function markSaleAsSynced(orderId: string): void {
  try {
    if (typeof window === 'undefined') return;

    const pending = getPendingSales();
    const updated = pending.filter(sale => sale.id !== orderId);
    localStorage.setItem(PENDING_SALES_KEY, JSON.stringify(updated));

    const sales = getSalesFromLocalStorage();
    const saleIndex = sales.findIndex(s => s.id === orderId);
    if (saleIndex !== -1) {
      sales[saleIndex].syncedAt = new Date().toISOString();
      sales[saleIndex].syncError = undefined;
      sales[saleIndex].syncStatus = 'synced';
      sales[saleIndex].syncRetryBlocked = false;
      localStorage.setItem(SALES_STORAGE_KEY, JSON.stringify(sales));
    }

    void db.orders.update(orderId, {
      syncStatus: 'synced',
      syncError: undefined,
      syncRetryBlocked: false,
      syncFailedAt: undefined,
    });

    console.log('[Sales Service] Marked sale as synced:', orderId);
  } catch (error) {
    console.error('[Sales Service] Failed to mark sale as synced:', error);
  }
}

/**
 * Mark a sale as failed to sync
 */
export function markSaleAsFailed(orderId: string, error: string, options: MarkSaleFailedOptions = {}): void {
  try {
    if (typeof window === 'undefined') return;

    const failedAt = new Date().toISOString();
    const sales = getSalesFromLocalStorage();
    const saleIndex = sales.findIndex(s => s.id === orderId);
    if (saleIndex !== -1) {
      sales[saleIndex].syncError = error;
      sales[saleIndex].syncStatus = 'failed';
      sales[saleIndex].syncRetryBlocked = Boolean(options.retryBlocked);
      sales[saleIndex].syncFailedAt = failedAt;
      localStorage.setItem(SALES_STORAGE_KEY, JSON.stringify(sales));
    }

    const pending = getPendingSales();
    const pendingIndex = pending.findIndex(s => s.id === orderId);
    if (pendingIndex !== -1) {
      pending[pendingIndex].syncError = error;
      pending[pendingIndex].syncStatus = 'failed';
      pending[pendingIndex].syncRetryBlocked = Boolean(options.retryBlocked);
      pending[pendingIndex].syncFailedAt = failedAt;
      localStorage.setItem(PENDING_SALES_KEY, JSON.stringify(pending));
    }

    void db.orders.update(orderId, {
      syncStatus: 'failed',
      syncError: error,
      syncRetryBlocked: Boolean(options.retryBlocked),
      syncFailedAt: failedAt,
    });

    console.log('[Sales Service] Marked sale as failed:', orderId, error);
  } catch (error) {
    console.error('[Sales Service] Failed to mark sale as failed:', error);
  }
}

/**
 * Get sales summary for a date range
 */
export function getSalesSummary(startDate?: Date, endDate?: Date): {
  totalSales: number;
  totalOrders: number;
  totalTips: number;
  byPaymentMethod: Record<string, number>;
} {
  try {
    const sales = getSalesFromLocalStorage();
    
    let filtered = sales;
    if (startDate || endDate) {
      filtered = sales.filter(sale => {
        const saleDate = new Date(sale.createdAt);
        if (startDate && saleDate < startDate) return false;
        if (endDate && saleDate > endDate) return false;
        return true;
      });
    }

    const summary = {
      totalSales: filtered.reduce((sum, sale) => sum + sale.total, 0),
      totalOrders: filtered.length,
      totalTips: filtered.reduce((sum, sale) => sum + (sale.tip || 0), 0),
      byPaymentMethod: {} as Record<string, number>,
    };

    filtered.forEach(sale => {
      const method = sale.paymentMethod || 'Unknown';
      summary.byPaymentMethod[method] = (summary.byPaymentMethod[method] || 0) + sale.total;
    });

    return summary;
  } catch (error) {
    console.error('[Sales Service] Failed to get sales summary:', error);
    return {
      totalSales: 0,
      totalOrders: 0,
      totalTips: 0,
      byPaymentMethod: {},
    };
  }
}

/**
 * Clear all sales from localStorage (use with caution)
 */
export function clearSalesHistory(): void {
  try {
    if (typeof window === 'undefined') return;

    localStorage.removeItem(SALES_STORAGE_KEY);
    localStorage.removeItem(PENDING_SALES_KEY);
    console.log('[Sales Service] Cleared all sales history');
  } catch (error) {
    console.error('[Sales Service] Failed to clear sales history:', error);
  }
}
