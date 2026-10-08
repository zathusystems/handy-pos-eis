'use client';

import React, { useState, useEffect } from 'react';
import { useLiveQuery } from 'dexie-react-hooks';
import { Clock, CheckCircle2, AlertCircle, Plus, X } from 'lucide-react';
import { db, type TakeOrder, type InventoryItem } from '@/lib/db';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  DialogClose,
} from '@/components/ui/dialog';
import { ScrollArea } from '@/components/ui/scroll-area';
import { useCurrency } from '@/hooks/use-currency';
import { syncService } from '@/lib/services/sync-service';
import type { CartItem } from '@/app/dashboard/pos/page';

interface TakeOrdersPanelProps {
  branchId: string;
  onAddToCart: (item: InventoryItem, quantity: number, price: number, notes?: string, takeOrderId?: string) => void;
  cartItems: CartItem[];
}

const normalizeBranchId = (value?: string | number | null): string => {
  const normalized = String(value ?? '').trim();
  if (!normalized) return '';

  const brnMatch = /^BRN-(\d+)$/i.exec(normalized);
  if (brnMatch) return brnMatch[1];

  const legacyMatch = /^branch-(\d+)$/i.exec(normalized);
  if (legacyMatch) return legacyMatch[1];

  return normalized;
};

const TakeOrderCard = ({
  order,
  onAddToCart,
  currencyFormatter,
}: {
  order: TakeOrder;
  onAddToCart: (order: TakeOrder) => void;
  currencyFormatter: (amount: number) => string;
}) => {
  const [isExpanded, setIsExpanded] = useState(false);

  // Handle both camelCase and snake_case field names from backend
  const customerName = (order as any).customer_name || order.customerName;
  const customerPhone = (order as any).customer_phone || order.customerPhone;
  const specialInstructions = (order as any).special_instructions || order.specialInstructions;
  const customerNotes = (order as any).customer_notes || order.customerNotes;
  const createdAt = (order as any).created_at || order.createdAt;

  const createdTime = new Date(createdAt);
  const now = new Date();
  const minutesAgo = Math.floor((now.getTime() - createdTime.getTime()) / 60000);

  return (
    <Card className="overflow-hidden border-l-4 border-l-green-600">
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between">
          <div>
            <CardTitle className="text-lg">Order #{order.orderNumber}</CardTitle>
            <p className="text-sm text-muted-foreground">{minutesAgo} min ago</p>
          </div>
          <Badge className="bg-green-100 text-green-800">
            <CheckCircle2 className="mr-1 h-3 w-3" />
            Ready
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        {/* Customer Info */}
        {(customerName || customerPhone) && (
          <div className="space-y-1 text-sm">
            {customerName && (
              <p>
                <span className="font-semibold">Customer:</span> {customerName}
              </p>
            )}
            {customerPhone && (
              <p>
                <span className="font-semibold">Phone:</span> {customerPhone}
              </p>
            )}
          </div>
        )}

        {/* Items Summary */}
        <div className="space-y-2">
          <p className="font-semibold text-sm">Items ({order.items.length}):</p>
          <div className="space-y-1">
            {order.items.slice(0, isExpanded ? undefined : 2).map((item) => (
              <div key={item.id} className="flex justify-between text-sm">
                <span>{item.name}</span>
                <span className="font-medium">x{item.quantity}</span>
              </div>
            ))}
            {!isExpanded && order.items.length > 2 && (
              <button
                onClick={() => setIsExpanded(true)}
                className="text-xs text-blue-600 hover:underline"
              >
                +{order.items.length - 2} more
              </button>
            )}
            {isExpanded && order.items.length > 2 && (
              <button
                onClick={() => setIsExpanded(false)}
                className="text-xs text-blue-600 hover:underline"
              >
                Show less
              </button>
            )}
          </div>
        </div>

        {/* Special Instructions */}
        {specialInstructions && (
          <>
            <Separator />
            <div className="rounded-lg bg-amber-50 p-2 text-sm dark:bg-amber-950/30">
              <p className="font-semibold text-amber-900 dark:text-amber-200">
                Special Instructions:
              </p>
              <p className="text-amber-800 dark:text-amber-300">
                {specialInstructions}
              </p>
            </div>
          </>
        )}

        {/* Customer Notes */}
        {customerNotes && (
          <div className="rounded-lg bg-blue-50 p-2 text-sm dark:bg-blue-950/30">
            <p className="font-semibold text-blue-900 dark:text-blue-200">Notes:</p>
            <p className="text-blue-800 dark:text-blue-300">{customerNotes}</p>
          </div>
        )}

        {/* Add to Cart Button */}
        <Button
          className="w-full bg-green-600 hover:bg-green-700"
          onClick={() => onAddToCart(order)}
        >
          <Plus className="mr-2 h-4 w-4" />
          Add to Cart
        </Button>
      </CardContent>
    </Card>
  );
};

export const TakeOrdersPanel = ({
  branchId,
  onAddToCart,
  cartItems,
}: TakeOrdersPanelProps) => {
  const { format: formatCurrency } = useCurrency();
  const [isOpen, setIsOpen] = useState(false);

  const readyTakeOrders = useLiveQuery(
    async () => {
      if (!branchId) return [];
      // Get all take orders and filter by branchId and status
      // Using toArray() instead of where() to avoid branchId type mismatch issues
      const allOrders = await db.takeOrders.toArray();
      const normalizedQueryBranchId = normalizeBranchId(branchId);

      const ordersForBranch = allOrders.filter((order) => {
        const orderBranchId = normalizeBranchId(order.branchId);
        const queryBranchId = normalizedQueryBranchId;

        return orderBranchId === queryBranchId;
      });

      
      // Ensure all take orders are marked as synced (not dirty) so they display
      // This handles take orders that were created locally but not yet synced
      for (const order of ordersForBranch) {
        if (order._dirty !== false) {

          await db.takeOrders.update(order.id, { _dirty: false, _synced_at: new Date().toISOString() });
        }
      }
      
      const ready = ordersForBranch.filter((order) => order.status === 'Ready');

      return ready;
    },
    [branchId]
  );

  // Fetch take orders from backend when component mounts or branchId changes
  useEffect(() => {
    if (branchId) {

      syncService.fetchAllTakeOrdersFromBackend(branchId);
    }
  }, [branchId]);

  const handleAddToCart = async (order: TakeOrder) => {

    
    // Add each item from the take order to the cart
    for (const item of order.items) {

      
      // First, try to use the price from the take order item (from backend)
      const takeOrderPrice = (item as any).price || 0;

      
      // Get the inventory item to get additional info
      const inventoryItem = await db.inventory.get(item.id);

      
      if (inventoryItem) {
        // Use take order price if available, otherwise use inventory price
        const price = takeOrderPrice > 0 ? takeOrderPrice : (inventoryItem.price || 0);

        onAddToCart(inventoryItem, item.quantity, price, undefined, order.id);
      } else {
        console.warn('[TakeOrdersPanel] Inventory item not found for:', item.id);
        // Search for the item by name in case the ID doesn't match
        const allInventory = await db.inventory.toArray();
        const itemByName = allInventory.find(inv => inv.name === item.name);
        
        if (itemByName) {
          // Use take order price if available, otherwise use inventory price
          const price = takeOrderPrice > 0 ? takeOrderPrice : (itemByName.price || 0);

          onAddToCart(itemByName, item.quantity, price, undefined, order.id);
        } else {
          console.warn('[TakeOrdersPanel] Inventory item not found by ID or name:', item.id, item.name);
          // Create a temporary item for cart if inventory item doesn't exist
          // Use the take order price if available
          const tempItem: InventoryItem = {
            id: item.id,
            name: item.name,
            category: 'Take Order Item',
            itemType: 'sellable',
            branchId: branchId,
            price: takeOrderPrice,
          };

          onAddToCart(tempItem, item.quantity, takeOrderPrice, undefined, order.id);
        }
      }
    }
    setIsOpen(false);
  };

  // Keep the button available even when there are no ready orders.
  const orderCount = readyTakeOrders?.length || 0;

  return (
    <Dialog open={isOpen} onOpenChange={setIsOpen}>
      <DialogTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="whitespace-nowrap"
        >
          <CheckCircle2 className="mr-2 h-4 w-4 text-green-600" />
          Ready Orders
          <Badge className="ml-2 bg-green-600 text-white">{orderCount}</Badge>
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] flex flex-col max-w-2xl">
        <DialogHeader>
          <DialogTitle>Ready Take Orders ({orderCount})</DialogTitle>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto pr-4">
          {orderCount === 0 ? (
            <div className="flex items-center justify-center h-32 text-muted-foreground">
              <p>No ready orders at this time</p>
            </div>
          ) : (
            <div className="space-y-4">
              {readyTakeOrders?.map((order) => (
                <TakeOrderCard
                  key={order.id}
                  order={order}
                  onAddToCart={handleAddToCart}
                  currencyFormatter={formatCurrency}
                />
              ))}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
};
