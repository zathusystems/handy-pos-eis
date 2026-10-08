
'use client';

import React, { useState, useMemo, useCallback } from 'react';
import { useLiveQuery } from 'dexie-react-hooks';
import { useForm, useFieldArray } from 'react-hook-form';
import { db, type InventoryItem, type TakeOrder, type Session } from '@/lib/db';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Card, CardContent, CardFooter, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { Plus, Minus, Send, ShoppingBasket, X, Trash2, Loader2 } from 'lucide-react';
import { useCurrency } from '@/hooks/use-currency';
import { toast } from '@/hooks/use-toast';
import { useAuth } from '@/hooks/use-auth';
import { Textarea } from '../ui/textarea';
import { Input } from '../ui/input';
import { authFetch } from '@/lib/auth-fetch';

type TakeOrderModalProps = {
  branchId: string;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
};

type OrderCartItem = {
    id: string;
    name: string;
    quantity: number;
    price: number;
    notes?: string;
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

export function TakeOrderModal({ branchId, isOpen, onOpenChange }: TakeOrderModalProps) {
    const { format: formatCurrency } = useCurrency();
    const { user } = useAuth();
    const [cart, setCart] = useState<OrderCartItem[]>([]);
    
    const menuItems = useLiveQuery(
        () => {
            if (!branchId) return [];
            return db.inventory
                .where('branchId')
                .equals(branchId)
                .filter(item => item.itemType === 'sellable' && item.onMenu === true)
                .toArray()
        },
        [branchId]
    ) || [];

    const activeSession = useLiveQuery(
        async () => {
            if (!branchId || !user?.uid) return undefined;

            const normalizedBranchId = normalizeBranchId(branchId);
            const currentUserId = String(user.uid);
            const currentUserEmail = String(user.email || '').trim().toLowerCase();
            const activeSessions = await db.sessions
                .where('status')
                .equals('active')
                .toArray();

            return activeSessions
                .filter((session) => {
                    if (normalizeBranchId(session.branchId) !== normalizedBranchId) {
                        return false;
                    }

                    const sessionUserId = String(session.userId || '');
                    const sessionUserEmail = String(session.userEmail || '').trim().toLowerCase();
                    return sessionUserId === currentUserId || (currentUserEmail !== '' && sessionUserEmail === currentUserEmail);
                })
                .sort((a, b) => new Date(b.startedAt).getTime() - new Date(a.startedAt).getTime())[0];
        },
        [branchId, user?.uid, user?.email],
    );

    const categories = useMemo(() => {
        const uniqueCategories = [...new Set(menuItems.map(item => item.category || 'Uncategorized'))];
        return ['All', ...uniqueCategories];
    }, [menuItems]);

    const handleAddToCart = useCallback((item: InventoryItem) => {
        setCart(prevCart => {
            const existingItemIndex = prevCart.findIndex(cartItem => cartItem.id === item.id);
            if (existingItemIndex > -1) {
                // Item already in cart, increment quantity
                const updatedCart = [...prevCart];
                updatedCart[existingItemIndex].quantity += 1;
                return updatedCart;
            }
            // New item, add to cart
            return [...prevCart, { id: item.id, name: item.name, quantity: 1, price: item.price || 0, notes: '' }];
        });
    }, []);

    const handleUpdateQuantity = (itemId: string, delta: number) => {
        setCart(prevCart => {
            const updatedCart = prevCart.map(item => {
                if (item.id === itemId) {
                    return { ...item, quantity: Math.max(0, item.quantity + delta) };
                }
                return item;
            });
            return updatedCart.filter(item => item.quantity > 0);
        });
    };
    
    const handleUpdateNotes = (itemId: string, notes: string) => {
         setCart(prevCart => prevCart.map(item => item.id === itemId ? { ...item, notes } : item));
    }

    const handleClearCart = () => setCart([]);

    const [isSubmitting, setIsSubmitting] = useState(false);
    const [showCustomerForm, setShowCustomerForm] = useState(false);
    const [customerName, setCustomerName] = useState('');
    const [customerPhone, setCustomerPhone] = useState('');
    const [customerNotes, setCustomerNotes] = useState('');
    const [tableNumber, setTableNumber] = useState('');

    const handleSendToKitchenClick = () => {
        if (cart.length === 0) {
            toast({ variant: 'destructive', title: 'Empty order', description: 'Please add items to the order.' });
            return;
        }
        setShowCustomerForm(true);
    };

    const handleSubmitOrder = async () => {
        setIsSubmitting(true);

        try {
            // Prepare the take order payload
            const payload = {
                branch_id: branchId,
                table_number: tableNumber || undefined,
                customer_name: customerName || undefined,
                customer_phone: customerPhone || undefined,
                customer_notes: customerNotes || undefined,
                items: cart.map(item => ({
                    inventory_item_id: item.id,
                    name: item.name,
                    quantity: item.quantity,
                    notes: item.notes || undefined,
                })),
            };

            // Send to backend API

            const createdOrder = await authFetch.fetch('/orders/take-orders/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });


            
            if (!createdOrder || !createdOrder.id) {
                console.error('[TakeOrderModal] Invalid response structure:', createdOrder);
                throw new Error(`Failed to create take order - invalid response: ${JSON.stringify(createdOrder)}`);
            }

            // Also save to local IndexedDB for offline support
            const takeOrder: TakeOrder = {
                id: createdOrder.id,
                orderNumber: createdOrder.order_number,
                branchId,
                status: 'Pending',
                tableNumber: tableNumber || undefined,
                customerName: createdOrder.customer_name,
                customerPhone: createdOrder.customer_phone,
                customerNotes: createdOrder.customer_notes,
                items: cart.map(item => ({
                    id: item.id,
                    inventoryItemId: item.id,
                    name: item.name,
                    quantity: item.quantity,
                    price: item.price,
                    notes: item.notes,
                    createdAt: new Date().toISOString(),
                    updatedAt: new Date().toISOString(),
                })),
                createdAt: new Date().toISOString(),
                updatedAt: new Date().toISOString(),
                orderType: 'staff',
            };

            await db.takeOrders.add(takeOrder);

            toast({
                title: 'Order Sent to Kitchen',
                description: `Take Order #${createdOrder.order_number} has been created successfully.`,
            });

            // Reset form
            handleClearCart();
            setTableNumber('');
            setCustomerName('');
            setCustomerPhone('');
            setCustomerNotes('');
            setShowCustomerForm(false);
            onOpenChange(false);
        } catch (error) {
            console.error('Failed to send order:', error);
            toast({
                variant: 'destructive',
                title: 'Failed to Send Order',
                description: error instanceof Error ? error.message : 'An error occurred while sending the order.',
            });
        } finally {
            setIsSubmitting(false);
        }
    };
    
    const subtotal = cart.reduce((acc, item) => acc + item.price * item.quantity, 0);

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[95vw] w-full h-[90vh] flex flex-col p-0">
        <DialogHeader className="p-4 sm:p-6 pb-2 sm:pb-2 shrink-0">
          <DialogTitle className="text-xl sm:text-2xl">Take a New Order</DialogTitle>
          <DialogDescription className="text-xs sm:text-sm">Select items from the menu to build the customer's order.</DialogDescription>
        </DialogHeader>
        
        {/* Mobile: Stacked layout, Desktop: Side-by-side */}
        <div className="grid grid-cols-1 lg:grid-cols-[1fr_380px] gap-0 overflow-hidden flex-1 min-h-0">
            {/* Menu Items */}
            <div className="flex flex-col h-full overflow-hidden border-r">
                <Tabs defaultValue="All" className="flex flex-col h-full overflow-hidden">
                    <TabsList className="mx-4">
                        {categories.map(category => (
                            <TabsTrigger key={category} value={category}>{category}</TabsTrigger>
                        ))}
                    </TabsList>
                    <div className="flex-1 overflow-y-auto p-4">
                        {categories.map(category => (
                            <TabsContent key={category} value={category} className="mt-0">
                                <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                                {menuItems
                                    .filter(item => category === 'All' || item.category === category)
                                    .map(item => (
                                        <Card key={item.id} className="cursor-pointer hover:shadow-md overflow-hidden transition-shadow" onClick={() => handleAddToCart(item)}>
                                            {/* Image Section */}
                                            <div className="relative flex h-32 items-center justify-center overflow-hidden bg-muted">
                                                {item.image ? (
                                                    <img
                                                        src={item.image}
                                                        alt={item.name}
                                                        className="h-full w-full object-cover"
                                                    />
                                                ) : (
                                                    <ShoppingBasket className="h-12 w-12 text-muted-foreground" />
                                                )}
                                            </div>
                                            {/* Item Info */}
                                            <CardContent className="p-3 text-center">
                                                <p className="font-semibold line-clamp-2">{item.name}</p>
                                                <p className="text-sm text-muted-foreground">{formatCurrency(item.price || 0)}</p>
                                            </CardContent>
                                        </Card>
                                    ))}
                                </div>
                            </TabsContent>
                        ))}
                    </div>
                </Tabs>
            </div>
            
            {/* Cart */}
            <div className="flex flex-col h-full bg-muted/30 min-h-0">
                <div className="p-3 sm:p-4 border-b shrink-0">
                    <h3 className="text-base sm:text-lg font-semibold flex justify-between items-center">
                        <span>Current Order</span>
                         {cart.length > 0 && (
                            <Button variant="ghost" size="sm" className="text-destructive h-7 sm:h-8 text-xs sm:text-sm" onClick={handleClearCart}>
                                <Trash2 className="mr-1 h-3 w-3 sm:h-4 sm:w-4" /> Clear
                            </Button>
                        )}
                    </h3>
                </div>

                <div className="flex-1 overflow-y-auto p-2 sm:p-4 space-y-2 min-h-0">
                {cart.length > 0 ? (
                    cart.map(item => (
                        <div key={item.id} className="bg-background p-3 rounded-lg border hover:shadow-sm transition-shadow">
                            <div className="flex items-center justify-between gap-3">
                                <div className="flex-1 min-w-0">
                                    <p className="font-medium text-sm truncate">{item.name}</p>
                                    <p className="text-xs text-muted-foreground">{formatCurrency(item.price)}</p>
                                </div>
                                <div className="flex items-center gap-1 shrink-0">
                                    <Button 
                                        size="icon" 
                                        variant="ghost" 
                                        className="h-6 w-6 text-muted-foreground hover:text-foreground" 
                                        onClick={() => handleUpdateQuantity(item.id, -1)}
                                    >
                                        <Minus className="h-3 w-3"/>
                                    </Button>
                                    <span className="w-5 text-center text-sm font-semibold">{item.quantity}</span>
                                    <Button 
                                        size="icon" 
                                        variant="ghost" 
                                        className="h-6 w-6 text-muted-foreground hover:text-foreground" 
                                        onClick={() => handleUpdateQuantity(item.id, 1)}
                                    >
                                        <Plus className="h-3 w-3"/>
                                    </Button>
                                </div>
                                <div className="text-right shrink-0">
                                    <p className="font-bold text-sm">{formatCurrency(item.price * item.quantity)}</p>
                                </div>
                            </div>
                        </div>
                    ))
                ) : (
                    <div className="flex flex-col items-center justify-center h-full text-center text-muted-foreground">
                        <ShoppingBasket className="h-16 w-16" />
                        <p className="mt-4 text-sm">Your order is empty.</p>
                        <p className="text-xs">Select items from the menu to begin.</p>
                    </div>
                )}
                </div>
                <div className="p-4 border-t mt-auto space-y-4 bg-background">
                    <div className="flex justify-between font-bold text-xl">
                        <span>Subtotal</span>
                        <span>{formatCurrency(subtotal)}</span>
                    </div>
                    <Button size="lg" className="w-full" disabled={cart.length === 0} onClick={handleSendToKitchenClick}>
                        <Send className="mr-2 h-5 w-5"/> Send to Kitchen
                    </Button>
                </div>
            </div>
        </div>

        {/* Customer Form Modal */}
        {showCustomerForm && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
            <Card className="w-full max-w-md">
              <CardHeader>
                <CardTitle>Customer Information</CardTitle>
                <CardDescription>Add customer details (optional)</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div>
                  <label className="text-sm font-medium">Table Number</label>
                  <Input
                    placeholder="Enter table number"
                    value={tableNumber}
                    onChange={(e) => setTableNumber(e.target.value)}
                    className="mt-1"
                  />
                </div>
                <div>
                  <label className="text-sm font-medium">Customer Name</label>
                  <Input
                    placeholder="Enter customer name"
                    value={customerName}
                    onChange={(e) => setCustomerName(e.target.value)}
                    className="mt-1"
                  />
                </div>
                <div>
                  <label className="text-sm font-medium">Phone Number</label>
                  <Input
                    placeholder="Enter phone number"
                    value={customerPhone}
                    onChange={(e) => setCustomerPhone(e.target.value)}
                    className="mt-1"
                  />
                </div>
                <div>
                  <label className="text-sm font-medium">Special Instructions</label>
                  <Textarea
                    placeholder="Add any special instructions..."
                    value={customerNotes}
                    onChange={(e) => setCustomerNotes(e.target.value)}
                    className="mt-1"
                  />
                </div>
              </CardContent>
              <CardFooter className="flex gap-2">
                <Button
                  variant="outline"
                  className="flex-1"
                  onClick={() => setShowCustomerForm(false)}
                  disabled={isSubmitting}
                >
                  Cancel
                </Button>
                <Button
                  className="flex-1 bg-green-600 hover:bg-green-700"
                  onClick={handleSubmitOrder}
                  disabled={isSubmitting}
                >
                  {isSubmitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  {isSubmitting ? 'Sending...' : 'Send to Kitchen'}
                </Button>
              </CardFooter>
            </Card>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
