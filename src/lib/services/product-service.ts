import { db, type InventoryItem } from '@/lib/db';
import { logAuditAction } from '@/lib/audit';
import { authFetch } from '@/lib/auth-fetch';
import { syncService } from './sync-service';
import { v4 as uuidv4 } from 'uuid';

/**
 * Offline-first product CRUD service
 * Marks items as dirty for sync via Sync Service
 * Sync Service handles all offline queueing and retry logic
 */

/**
 * Create a new product (offline-first)
 * 1. Generates UUID locally for consistent ID across frontend and backend
 * 2. Saves to local database immediately with UUID and dirty flag
 * 3. Logs audit action
 * 4. Sync Service will handle pushing to backend when online
 */
export async function createProduct(
  product: Omit<InventoryItem, 'id'>,
  userId: string,
  userName: string
): Promise<InventoryItem> {
  try {
    // Generate UUID locally - this same ID will be used on backend
    const uuid = uuidv4();
    const newProduct: InventoryItem = {
      ...product,
      id: uuid,
    };

    // 1. Save to local database immediately with UUID and sync flag
    const productWithSync = {
      ...newProduct,
      _dirty: true,
      _operation: 'create' as const
    };
    await db.inventory.add(productWithSync);
    console.log('[Inventory Sync] Marked new product as dirty:', uuid);

    // 2. Log audit action locally
    await logAuditAction({
      userId,
      userName,
      branchId: product.branchId,
      actionType: 'ITEM_CREATE',
      entityType: 'InventoryItem',
      entityId: uuid,
      details: newProduct,
    });

    // 3. Trigger sync if online
    if (typeof window !== 'undefined' && navigator.onLine) {
      console.log('[Product Service] Triggering sync after product creation');
      syncService.performFullSync(product.branchId).catch(err => 
        console.error('[Product Service] Sync failed:', err)
      );
    }

    console.log(`[Product Service] Created product with UUID: ${uuid}`);
    return newProduct;
  } catch (error) {
    console.error('Failed to create product:', error);
    throw error;
  }
}

/**
 * Update an existing product (offline-first)
 * 1. Gets existing product to preserve all fields
 * 2. Merges updates with existing data
 * 3. Updates local database immediately with dirty flag
 * 4. Logs audit action
 * 5. Sync Service will handle pushing to backend when online
 */
export async function updateProduct(
  productId: string,
  updates: Partial<InventoryItem>,
  userId: string,
  userName: string,
  branchId: string
): Promise<void> {
  try {
    // 1. Get existing product to preserve all fields
    const existingProduct = await db.inventory.get(productId);
    if (!existingProduct) {
      throw new Error(`Product ${productId} not found`);
    }

    console.log('[Product Service] Updating product:', productId, 'with updates:', updates);

    // 2. Merge updates with existing data
    const mergedProduct = {
      ...existingProduct,
      ...updates,
      _dirty: true,
      _operation: 'update' as const
    };

    console.log('[Product Service] Merged product data:', mergedProduct);

    // 3. Update local database immediately with sync flag
    await db.inventory.put(mergedProduct);
    console.log('[Product Service] Successfully saved product to IndexedDB with _dirty=true and _operation=update:', productId);

    // 4. Log audit action locally
    await logAuditAction({
      userId,
      userName,
      branchId,
      actionType: 'ITEM_UPDATE',
      entityType: 'InventoryItem',
      entityId: productId,
      details: updates,
    });

    // 5. Trigger sync if online
    if (typeof window !== 'undefined' && navigator.onLine) {
      console.log('[Product Service] Triggering sync after product update');
      syncService.performFullSync(branchId).catch(err => 
        console.error('[Product Service] Sync failed:', err)
      );
    } else {
      console.log('[Product Service] Offline - product update queued for sync when online');
    }

    console.log(`[Product Service] Updated product: ${productId}`);
  } catch (error) {
    console.error('Failed to update product:', error);
    throw error;
  }
}

/**
 * Delete a product (offline-first)
 * 1. Gets existing product to preserve data for sync
 * 2. Marks as deleted with dirty flag for sync
 * 3. Logs audit action
 * 4. Sync Service will handle pushing deletion to backend when online
 */
export async function deleteProduct(
  productId: string,
  userId: string,
  userName: string,
  branchId: string
): Promise<void> {
  try {
    // 1. Get existing product to preserve data for sync
    const existingProduct = await db.inventory.get(productId);
    if (!existingProduct) {
      throw new Error(`Product ${productId} not found`);
    }

    console.log('[Product Service] Deleting product:', productId);

    // 2. Mark as deleted with dirty flag for sync (don't delete immediately)
    const deletedProduct = {
      ...existingProduct,
      _dirty: true,
      _operation: 'delete' as const,
      syncRetryBlocked: false,
      syncStatus: 'pending',
      syncError: undefined,
      syncFailedAt: undefined,
      _deletedAt: new Date().toISOString()
    };

    // Update the product with delete marker instead of deleting
    await db.inventory.put(deletedProduct);
    console.log('[Product Service] Marked product as deleted for sync:', productId);

    // 3. Log audit action locally
    await logAuditAction({
      userId,
      userName,
      branchId,
      actionType: 'ITEM_DELETE',
      entityType: 'InventoryItem',
      entityId: productId,
      details: { deletedAt: new Date().toISOString() },
    });

    // 4. Trigger sync if online
    if (typeof window !== 'undefined' && navigator.onLine) {
      console.log('[Product Service] Triggering sync after product deletion');
      try {
        await syncService.performFullSync(branchId);
        console.log('[Product Service] Backend deletion sync completed:', productId);
      } catch (err) {
        console.error('[Product Service] Sync failed:', err);
      }
    } else {
      console.log('[Product Service] Offline - product deletion queued for sync when online');
    }

    console.log(`[Product Service] Marked product for deletion: ${productId}`);
  } catch (error) {
    console.error('Failed to delete product:', error);
    throw error;
  }
}

/**
 * Get all products for a branch
 */
export async function getProductsByBranch(branchId: string): Promise<InventoryItem[]> {
  try {
    const normalized = String(branchId || '').trim();
    if (!normalized) {
      return [];
    }

    const candidates = new Set<string>([normalized]);
    const brnMatch = /^BRN-(\d+)$/i.exec(normalized);
    if (brnMatch) {
      candidates.add(brnMatch[1]);
      candidates.add(`branch-${brnMatch[1]}`);
    } else if (/^\d+$/.test(normalized)) {
      candidates.add(`BRN-${normalized}`);
      candidates.add(`branch-${normalized}`);
    }

    const candidateList = Array.from(candidates);
    if (candidateList.length === 1) {
      return await db.inventory.where({ branchId: candidateList[0] }).toArray();
    }
    return await db.inventory.where('branchId').anyOf(candidateList).toArray();
  } catch (error) {
    console.error('Failed to get products:', error);
    throw error;
  }
}

/**
 * Get a single product by ID
 */
export async function getProductById(productId: string): Promise<InventoryItem | undefined> {
  try {
    return await db.inventory.get(productId);
  } catch (error) {
    console.error('Failed to get product:', error);
    throw error;
  }
}
