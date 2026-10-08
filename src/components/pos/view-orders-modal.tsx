'use client';

import React, { useMemo, useState } from 'react';
import { useLiveQuery } from 'dexie-react-hooks';
import { db, type TakeOrder } from '@/lib/db';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { safeLocalStorageGetItem } from '@/lib/safe-local-storage';
import { ShoppingBasket, CheckCircle2, Clock, AlertCircle, X, ChevronDown, ChevronUp, Phone, User, Utensils, Calendar, Tag } from 'lucide-react';
import { useCurrency } from '@/hooks/use-currency';
import { format, parseISO } from 'date-fns';

type ViewOrdersModalProps = {
  branchId: string;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
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

const getTimeAgo = (dateString: string): string => {
  try {
    const date = parseISO(dateString);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    
    if (diffMins < 1) return 'just now';
    if (diffMins === 1) return '1 minute ago';
    if (diffMins < 60) return `${diffMins} minutes ago`;
    
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours === 1) return '1 hour ago';
    if (diffHours < 24) return `${diffHours} hours ago`;
    
    const diffDays = Math.floor(diffHours / 24);
    if (diffDays === 1) return 'yesterday';
    if (diffDays < 7) return `${diffDays} days ago`;
    
    return format(date, 'MMM dd');
  } catch (e) {
    return 'unknown';
  }
};

export function ViewOrdersModal({ branchId, isOpen, onOpenChange }: ViewOrdersModalProps) {
  const { format: formatCurrency } = useCurrency();
  const [expandedOrderId, setExpandedOrderId] = useState<string | null>(null);
  const [, setRefresh] = useState(0);

  // Fetch all take orders for this branch
  const allOrders = useLiveQuery(
    () => {
      if (!branchId) {

        return [];
      }
      const normalizedBranchId = normalizeBranchId(branchId);
      return db.takeOrders.toArray()
        .then(orders => {
          const filteredOrders = orders.filter(
            (order) => normalizeBranchId(order.branchId) === normalizedBranchId
          );
          return filteredOrders.sort((a, b) => {
            // Sort by created date, newest first
            if (!a.createdAt || !b.createdAt) return 0;
            return new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime();
          });
        });
    },
    [branchId]
  ) || [];

  // Fetch orders from backend when modal opens
  React.useEffect(() => {
    if (isOpen && branchId) {

      const { syncService } = require('@/lib/services/sync-service');
      syncService.fetchAllTakeOrdersFromBackend(branchId);
    }
  }, [isOpen, branchId]);

  // Refresh component every minute to update "minutes ago" display
  React.useEffect(() => {
    const interval = setInterval(() => {
      setRefresh(prev => prev + 1);
    }, 60000); // Refresh every 60 seconds
    return () => clearInterval(interval);
  }, []);

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'Ready':
        return <CheckCircle2 className="h-4 w-4 text-green-600" />;
      case 'Preparing':
        return <Clock className="h-4 w-4 text-yellow-600" />;
      case 'Pending':
        return <AlertCircle className="h-4 w-4 text-blue-600" />;
      case 'Confirmed':
        return <CheckCircle2 className="h-4 w-4 text-purple-600" />;
      case 'Sent to Kitchen':
        return <Clock className="h-4 w-4 text-orange-600" />;
      case 'Completed':
        return <CheckCircle2 className="h-4 w-4 text-gray-600" />;
      case 'Cancelled':
        return <X className="h-4 w-4 text-red-600" />;
      default:
        return <ShoppingBasket className="h-4 w-4" />;
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'Ready':
        return 'bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-200 border-green-300 dark:border-green-700';
      case 'Preparing':
        return 'bg-yellow-100 dark:bg-yellow-900 text-yellow-800 dark:text-yellow-200 border-yellow-300 dark:border-yellow-700';
      case 'Pending':
        return 'bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-200 border-blue-300 dark:border-blue-700';
      case 'Confirmed':
        return 'bg-purple-100 dark:bg-purple-900 text-purple-800 dark:text-purple-200 border-purple-300 dark:border-purple-700';
      case 'Sent to Kitchen':
        return 'bg-orange-100 dark:bg-orange-900 text-orange-800 dark:text-orange-200 border-orange-300 dark:border-orange-700';
      case 'Completed':
        return 'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-200 border-gray-300 dark:border-gray-600';
      case 'Cancelled':
        return 'bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-200 border-red-300 dark:border-red-700';
      default:
        return 'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-200 border-gray-300 dark:border-gray-600';
    }
  };

  const getOrderTypeColor = (orderType: string) => {
    return orderType === 'staff' 
      ? 'bg-purple-100 dark:bg-purple-900 text-purple-800 dark:text-purple-200' 
      : 'bg-orange-100 dark:bg-orange-900 text-orange-800 dark:text-orange-200';
  };

  const statusCounts = useMemo(() => {
    const counts: { [key: string]: number } = {};
    allOrders.forEach(order => {
      counts[order.status] = (counts[order.status] || 0) + 1;
    });
    return counts;
  }, [allOrders]);

  const handleUpdateStatus = async (orderId: string, newStatus: string) => {
    try {

      const backendBranchId = branchId.replace(/^BRN-/, '');
      
      const response = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || 'https://pos3.express-travel-ticketing.online/api'}/orders/take-orders/${orderId}/update_status/`,
        {
          method: 'PATCH',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${safeLocalStorageGetItem('access_token') || ''}`
          },
          body: JSON.stringify({ status: newStatus })
        }
      );

      if (response.ok) {

        // Refresh orders from backend
        const { syncService } = require('@/lib/services/sync-service');
        syncService.fetchAllTakeOrdersFromBackend(branchId);
      } else {
        console.error(`[ViewOrdersModal] Failed to update order: ${response.status}`);
      }
    } catch (error) {
      console.error('[ViewOrdersModal] Error updating order status:', error);
    }
  };

  const OrderCard = ({ order }: { order: TakeOrder }) => {
    const total = order.items.reduce((sum, item) => {
      return sum + (item.quantity * (item.price || 0));
    }, 0);

    const isExpanded = expandedOrderId === order.id;

    return (
      <div className="border-2 border-border bg-card rounded-lg hover:shadow-md transition-all duration-200">
        {/* Header - Always Visible */}
        <button
          onClick={() => setExpandedOrderId(isExpanded ? null : order.id)}
          className="w-full p-4 text-left hover:bg-muted transition-colors"
        >
          <div className="flex items-center justify-between gap-4">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-3 mb-2 flex-wrap">
                <p className="font-bold text-lg text-gray-900 dark:text-white">Order #{order.orderNumber}</p>
                <Badge className={`${getStatusColor(order.status)} flex items-center gap-1 border`}>
                  {getStatusIcon(order.status)}
                  <span className="text-xs font-semibold">{order.status}</span>
                </Badge>
                <Badge className={`${getOrderTypeColor(order.orderType)} text-xs`}>
                  {order.orderType === 'staff' ? 'Staff' : 'Self-Service'}
                </Badge>
              </div>
              
              <div className="flex items-center gap-4 text-sm text-muted-foreground flex-wrap">
                {order.createdAt && (
                  <span className="flex items-center gap-1">
                    <Calendar className="h-3 w-3" />
                    {getTimeAgo(order.createdAt)}
                  </span>
                )}
                {order.customerName && (
                  <span className="flex items-center gap-1">
                    <User className="h-3 w-3" />
                    {order.customerName}
                  </span>
                )}
              </div>
            </div>

            <div className="flex items-center gap-4">
              <div className="text-right">
                <p className="text-lg font-bold text-green-600 dark:text-green-400">{formatCurrency(total)}</p>
                <p className="text-xs text-muted-foreground">{order.items.length} items</p>
              </div>
              <div className="text-gray-400 dark:text-gray-600">
                {isExpanded ? <ChevronUp className="h-5 w-5" /> : <ChevronDown className="h-5 w-5" />}
              </div>
            </div>
          </div>
        </button>

        {/* Expanded Details */}
        {isExpanded && (
          <div className="border-t border-border p-4 space-y-4">
            {/* Customer Information */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {order.customerName && (
                <div className="flex items-start gap-3 p-3 bg-muted rounded-lg border border-border">
                  <User className="h-4 w-4 mt-1 flex-shrink-0 text-muted-foreground" />
                  <div>
                    <p className="text-xs font-semibold text-muted-foreground uppercase">Customer Name</p>
                    <p className="font-medium text-foreground">{order.customerName}</p>
                  </div>
                </div>
              )}
              {order.customerPhone && (
                <div className="flex items-start gap-3 p-3 bg-muted rounded-lg border border-border">
                  <Phone className="h-4 w-4 mt-1 flex-shrink-0 text-muted-foreground" />
                  <div>
                    <p className="text-xs font-semibold text-muted-foreground uppercase">Phone</p>
                    <p className="font-medium text-foreground">{order.customerPhone}</p>
                  </div>
                </div>
              )}
              {order.tableNumber && (
                <div className="flex items-start gap-3 p-3 bg-muted rounded-lg border border-border">
                  <Utensils className="h-4 w-4 mt-1 flex-shrink-0 text-muted-foreground" />
                  <div>
                    <p className="text-xs font-semibold text-muted-foreground uppercase">Table Number</p>
                    <p className="font-medium text-foreground">#{order.tableNumber}</p>
                  </div>
                </div>
              )}
              <div className="flex items-start gap-3 p-3 bg-muted rounded-lg border border-border">
                <Tag className="h-4 w-4 mt-1 flex-shrink-0 text-muted-foreground" />
                <div>
                  <p className="text-xs font-semibold text-muted-foreground uppercase">Order Type</p>
                  <p className="font-medium text-foreground">{order.orderType === 'staff' ? 'Staff Created' : 'Self-Service'}</p>
                </div>
              </div>
            </div>

            {/* Completed At */}
            {order.completedAt && (
              <div className="flex items-start gap-3 p-3 bg-muted rounded-lg border border-border">
                <CheckCircle2 className="h-4 w-4 mt-1 flex-shrink-0 text-green-600 dark:text-green-400" />
                <div>
                  <p className="text-xs font-semibold text-muted-foreground uppercase">Completed At</p>
                  <p className="font-medium text-foreground text-sm">{format(parseISO(order.completedAt), 'MMM dd, yyyy HH:mm:ss')}</p>
                </div>
              </div>
            )}

            {/* Items Section */}
            <div className="bg-muted rounded-lg p-4 border border-border">
              <p className="text-xs font-bold text-foreground uppercase mb-3 tracking-wide">Order Items ({order.items.length})</p>
              <div className="space-y-2">
                {order.items.map((item, idx) => (
                  <div key={idx} className="flex justify-between items-start py-2 px-2 border-b border-border last:border-b-0 hover:bg-background rounded">
                    <div className="flex-1">
                      <p className="font-medium text-foreground">{item.name}</p>
                      <div className="flex gap-3 text-xs text-muted-foreground mt-1">
                        <span>Qty: <span className="font-semibold">{item.quantity}</span></span>
                        {item.notes && <span className="text-accent italic">Note: {item.notes}</span>}
                      </div>
                    </div>
                    <div className="text-right ml-4">
                      <p className="font-semibold text-foreground">
                        {formatCurrency(item.price * item.quantity)}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        @ {formatCurrency(item.price)} each
                      </p>
                    </div>
                  </div>
                ))}
              </div>
              
              {/* Total */}
              <div className="mt-4 pt-4 border-t border-border flex justify-between items-center bg-background p-3 rounded">
                <p className="font-bold text-foreground text-lg">Total</p>
                <p className="font-bold text-primary text-xl">{formatCurrency(total)}</p>
              </div>
            </div>

            {/* Customer Notes */}
            {order.customerNotes && (
              <div className="bg-muted border-l-4 border-accent p-4 rounded">
                <p className="text-xs font-bold text-foreground uppercase mb-2">Customer Notes</p>
                <p className="text-sm text-muted-foreground">{order.customerNotes}</p>
              </div>
            )}

            {/* Special Instructions */}
            {order.specialInstructions && (
              <div className="bg-muted border-l-4 border-accent p-4 rounded">
                <p className="text-xs font-bold text-foreground uppercase mb-2">Special Instructions</p>
                <p className="text-sm text-muted-foreground">{order.specialInstructions}</p>
              </div>
            )}

            {/* Action Buttons */}
            <div className="flex gap-2 pt-4 border-t border-border flex-wrap">
              {order.status !== 'Cancelled' && order.status !== 'Completed' && (
                <>
                  {order.status === 'Pending' && (
                    <Button
                      onClick={() => handleUpdateStatus(order.id, 'Confirmed')}
                      variant="default"
                      className="flex-1 min-w-[120px]"
                    >
                      Confirm Order
                    </Button>
                  )}
                  
                  {order.status === 'Confirmed' && (
                    <>
                      <Button
                        onClick={() => handleUpdateStatus(order.id, 'Sent to Kitchen')}
                        variant="default"
                        className="flex-1 min-w-[120px]"
                      >
                        Send to Kitchen
                      </Button>
                      <Button
                        onClick={() => handleUpdateStatus(order.id, 'Ready')}
                        variant="outline"
                        className="flex-1 min-w-[120px]"
                      >
                        Skip to Ready
                      </Button>
                    </>
                  )}
                  
                  {order.status === 'Sent to Kitchen' && (
                    <Button
                      onClick={() => handleUpdateStatus(order.id, 'Ready')}
                      variant="outline"
                      className="flex-1 min-w-[120px]"
                      disabled
                    >
                      Waiting in Kitchen ⏳
                    </Button>
                  )}
                  
                  {order.status === 'Preparing' && (
                    <Button
                      onClick={() => handleUpdateStatus(order.id, 'Ready')}
                      variant="default"
                      className="flex-1 min-w-[120px]"
                    >
                      Mark as Ready
                    </Button>
                  )}
                  
                  {order.status === 'Ready' && (
                    <Button
                      onClick={() => handleUpdateStatus(order.id, 'Completed')}
                      variant="secondary"
                      className="flex-1 min-w-[120px]"
                    >
                      Direct to POS
                    </Button>
                  )}
                  
                  <Button
                    onClick={() => handleUpdateStatus(order.id, 'Cancelled')}
                    variant="destructive"
                    className="flex-1 min-w-[100px]"
                  >
                    Cancel
                  </Button>
                </>
              )}
              
              {order.status === 'Completed' && (
                <Button
                  disabled
                  variant="secondary"
                  className="w-full"
                >
                  ✓ Completed
                </Button>
              )}
              
              {order.status === 'Cancelled' && (
                <Button
                  disabled
                  variant="destructive"
                  className="w-full"
                >
                  ✗ Cancelled
                </Button>
              )}
            </div>
          </div>
        )}
      </div>
    );
  };

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl max-h-[90vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle className="text-2xl">Orders Management</DialogTitle>
          <DialogDescription className="text-sm">
            <span className="font-semibold text-gray-700 dark:text-gray-300">Total: {allOrders.length} orders</span>
            {Object.entries(statusCounts).length > 0 && (
              <div className="mt-2 flex flex-wrap gap-2">
                {Object.entries(statusCounts).map(([status, count]) => (
                  <Badge key={status} variant="outline" className="text-xs">
                    {status}: <span className="font-bold ml-1">{count}</span>
                  </Badge>
                ))}
              </div>
            )}
          </DialogDescription>
        </DialogHeader>

        {/* Orders List */}
        <div className="flex-1 overflow-y-auto pr-4 space-y-3">
          {allOrders.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
              <ShoppingBasket className="h-16 w-16 mb-4 opacity-30" />
              <p className="text-lg font-semibold">No orders yet</p>
              <p className="text-sm">Orders will appear here when created</p>
            </div>
          ) : (
            <div className="space-y-3">
              {allOrders.map(order => (
                <OrderCard key={order.id} order={order} />
              ))}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
