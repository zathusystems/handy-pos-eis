

'use client';

import React, { useState, useMemo } from 'react';
import type { DateRange } from 'react-day-picker';
import { subDays, format, startOfDay, endOfDay, eachDayOfInterval, startOfMonth } from 'date-fns';
import {
  BarChart,
  Download,
  Calendar as CalendarIcon,
  DollarSign,
  TrendingUp,
  TrendingDown,
  Package,
  Landmark,
  Loader2,
  ShoppingCart,
  Users,
  PieChart,
  Circle,
  MoreHorizontal,
  Undo2,
  X,
  Plus,
  Check,
} from 'lucide-react';
import {
  Area,
  AreaChart,
  ResponsiveContainer,
  XAxis,
  YAxis,
  Tooltip as RechartsTooltip,
  Pie,
  Cell,
  Bar,
} from 'recharts';
import { useLiveQuery } from 'dexie-react-hooks';
import { useForm, useFieldArray } from 'react-hook-form';
import Papa from 'papaparse';

import { useReports } from '@/hooks/use-reports';
import { useAuth } from '@/hooks/use-auth';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  CardFooter
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
    DialogTrigger,
    DialogFooter,
} from '@/components/ui/dialog';
import { Calendar } from '@/components/ui/calendar';
import { cn } from '@/lib/utils';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { db, type Order, type Refund, type OrderItem } from '@/lib/db';
import {
  resolveEisSaleStatusLabel,
} from '@/lib/eis-submission';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useCurrency } from '@/hooks/use-currency';
import { Badge } from '@/components/ui/badge';
import { toast } from '@/hooks/use-toast';
import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import { Separator } from '@/components/ui/separator';
import { logAuditAction } from '@/lib/audit';
import { downloadTextFile } from '@/lib/file-download';
import { calculateZReportSummary } from '@/lib/z-report-print';
import {
  getProductReportingCategoryMeta,
  type ProductReportingCategory,
} from '@/lib/session-product-report';
import SaleDetailModal from '@/app/dashboard/sessions/modals/sale-detail-modal';

type RefundFormValues = {
  items: (OrderItem & { maxQuantity: number; price: number })[];
  reason?: string;
};

const sortOrdersByMostRecent = (orders: Order[]): Order[] => {
    return [...orders].sort((a, b) => {
        const timeA = Date.parse(String((a as any)?.createdAt ?? (a as any)?.created_at ?? ''));
        const timeB = Date.parse(String((b as any)?.createdAt ?? (b as any)?.created_at ?? ''));
        const normalizedTimeA = Number.isFinite(timeA) ? timeA : 0;
        const normalizedTimeB = Number.isFinite(timeB) ? timeB : 0;
        if (normalizedTimeB !== normalizedTimeA) {
            return normalizedTimeB - normalizedTimeA;
        }

        const orderNumberA = Number((a as any)?.orderNumber ?? (a as any)?.order_number ?? 0);
        const orderNumberB = Number((b as any)?.orderNumber ?? (b as any)?.order_number ?? 0);
        const normalizedOrderNumberA = Number.isFinite(orderNumberA) ? orderNumberA : 0;
        const normalizedOrderNumberB = Number.isFinite(orderNumberB) ? orderNumberB : 0;
        if (normalizedOrderNumberB !== normalizedOrderNumberA) {
            return normalizedOrderNumberB - normalizedOrderNumberA;
        }

        return String((b as any)?.id ?? '').localeCompare(String((a as any)?.id ?? ''));
    });
};

const toTrimmedString = (value: unknown): string => String(value ?? '').trim();

const resolveBuyerField = (...candidates: Array<unknown>): string => {
    for (const candidate of candidates) {
        const trimmed = toTrimmedString(candidate);
        if (trimmed) {
            return trimmed;
        }
    }
    return '';
};

const resolveBuyerDetails = (order: Order | null | undefined) => {
    const source = order as any;
    const name = resolveBuyerField(
        source?.customerName,
        source?.customer_name,
        source?.buyerName,
        source?.buyer_name
    );
    const phone = resolveBuyerField(
        source?.customerPhone,
        source?.customer_phone,
        source?.buyerPhone,
        source?.buyer_phone
    );
    const tin = resolveBuyerField(
        source?.customerTin,
        source?.customer_tin,
        source?.buyerTin,
        source?.buyer_tin
    );
    const email = resolveBuyerField(
        source?.customerEmail,
        source?.customer_email,
        source?.buyerEmail,
        source?.buyer_email
    );
    const address = resolveBuyerField(
        source?.customerAddress,
        source?.customer_address,
        source?.buyerAddress,
        source?.buyer_address
    );

    return {
        name,
        phone,
        tin,
        email,
        address,
    };
};

const resolveEisStatusForOrder = (
    order: Order | null | undefined,
    eisEnabled: boolean
): Order['eis_status'] | undefined => {
    const source = order as any;
    const status = toTrimmedString(source?.eisStatus ?? source?.eis_status).toUpperCase();
    if (
        status === 'PENDING' ||
        status === 'SUBMITTED' ||
        status === 'ACCEPTED' ||
        status === 'REJECTED'
    ) {
        return status;
    }

    const fiscalInvoice = toTrimmedString(
        source?.fiscalInvoiceNumber ?? source?.fiscal_invoice_number
    );
    if (fiscalInvoice) {
        return 'SUBMITTED';
    }

    if (eisEnabled && source?._dirty) {
        return 'PENDING';
    }

    return undefined;
};

const formatOptionalDateTime = (value?: string) => {
    if (!value) return 'N/A';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? 'N/A' : format(parsed, 'PPpp');
};

const resolveFiscalYearStart = (today: Date, startMonth: number) => {
    const normalizedMonth = Math.min(12, Math.max(1, Number(startMonth) || 1));
    const monthIndex = normalizedMonth - 1;
    const start = new Date(today.getFullYear(), monthIndex, 1);
    if (today < start) {
        start.setFullYear(start.getFullYear() - 1);
    }
    return start;
};

const EMPTY_EIS_SUMMARY = {
    ordersWithFiscalNumber: 0,
    pendingFiscalNumber: 0,
    eisStatusCounts: {
        pending: 0,
        submitted: 0,
        accepted: 0,
        rejected: 0,
        unknown: 0,
    },
    ordersWithQr: 0,
    ordersWithSignature: 0,
    firstFiscalInvoice: '',
    lastFiscalInvoice: '',
    firstSubmissionAt: '',
    lastSubmissionAt: '',
};

const toFiniteNumber = (value: unknown, fallback = 0): number => {
    const parsed = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
};

const formatQuantity = (value: number): string => {
    const quantity = toFiniteNumber(value, 0);
    return quantity.toFixed(Math.abs(quantity - Math.round(quantity)) < 1e-9 ? 2 : 3);
};

const ProductTypeBadge = ({ productType }: { productType: ProductReportingCategory }) => {
    const meta = getProductReportingCategoryMeta(productType);
    return (
        <Badge variant="outline" className={meta.badgeClassName}>
            {meta.shortLabel}
        </Badge>
    );
};

const normalizeOrderForReport = (order: Order) => {
    const total = toFiniteNumber(
        order.total ?? (order as any).grossAmount ?? (order as any).gross_amount,
        0
    );
    const tax = toFiniteNumber(
        order.tax ?? (order as any).vatAmount ?? (order as any).vat_amount,
        0
    );
    const subtotalCandidate = toFiniteNumber(
        order.subtotal ?? (order as any).netAmount ?? (order as any).net_amount,
        Number.NaN
    );
    const subtotal = Number.isFinite(subtotalCandidate)
        ? subtotalCandidate
        : Math.max(total - tax, 0);

    return {
        ...order,
        total,
        tax,
        subtotal,
    };
};

const RefundDialog = ({
    order,
    isOpen,
    onOpenChange
}: { order: Order; isOpen: boolean; onOpenChange: (open: boolean) => void }) => {
    const { user } = useAuth();
    const { format: formatCurrency } = useCurrency();
    const form = useForm<RefundFormValues>({
        defaultValues: {
            items: order.items.map(item => ({
                ...item,
                maxQuantity: item.quantity,
                price: order.subtotal / order.items.reduce((sum, i) => sum + i.quantity, 0), // Simplified price calculation
            })),
            reason: ''
        },
    });

    const { control, handleSubmit, watch } = form;
    const { fields } = useFieldArray({ control, name: "items" });

    const watchedItems = watch("items");
    const totalRefund = useMemo(() => {
        return watchedItems.reduce((acc, item) => acc + (item.price * item.quantity), 0);
    }, [watchedItems]);


    const onSubmit = async (data: RefundFormValues) => {
        if (!user) {
            toast({ variant: 'destructive', title: "Not authenticated."});
            return;
        }
        
        const itemsToRefund = data.items.filter(item => item.quantity > 0);
        if (itemsToRefund.length === 0) {
            toast({ variant: 'destructive', title: "No items selected for refund."});
            return;
        }

        try {
            await db.transaction('rw', db.orders, db.inventory, db.sessions, db.refunds, async () => {
                // 1. Create refund record
                const newRefund: Refund = {
                    id: `REF-${Date.now()}`,
                    branchId: order.branchId,
                    orderId: order.id,
                    orderNumber: order.orderNumber,
                    items: itemsToRefund,
                    total: totalRefund,
                    reason: data.reason,
                    refundedBy: user.uid,
                    refundedAt: new Date().toISOString(),
                };
                await db.refunds.add(newRefund);

                // 2. Update inventory (restock)
                for (const item of itemsToRefund) {
                    await db.inventory.where({ id: item.id, branchId: order.branchId }).modify(inv => {
                        inv.stockUnits = (inv.stockUnits || 0) + item.quantity;
                    });
                }

                // 3. Update order status
                const allItemsRefunded = data.items.every(item => item.quantity === item.maxQuantity);
                await db.orders.update(order.id, {
                    status: allItemsRefunded ? 'Refunded' : 'Partially Refunded'
                });

                // 4. Adjust session financials
                if (order.sessionId) {
                   await db.sessions.where({ id: order.sessionId }).modify(session => {
                        session.totalRefunds = (session.totalRefunds || 0) + totalRefund;
                        if(order.paymentMethod === 'Cash') {
                            session.expectedCash -= totalRefund;
                        }
                   });
                }
                
                await logAuditAction({
                    userId: user.uid,
                    userName: user.displayName || user.email || 'System',
                    branchId: order.branchId,
                    actionType: 'ORDER_REFUND',
                    entityType: 'Refund',
                    entityId: newRefund.id,
                    details: { orderId: order.id, total: totalRefund, items: itemsToRefund.map(i => ({id: i.id, qty: i.quantity})) },
                });
            });

            toast({ title: "Refund Processed", description: `Refund of ${formatCurrency(totalRefund)} for order #${order.orderNumber} has been recorded.`});
            onOpenChange(false);
        } catch (error) {
            console.error(error);
            toast({ variant: 'destructive', title: "Refund Failed" });
        }
    };

    return (
        <Dialog open={isOpen} onOpenChange={onOpenChange}>
            <DialogContent className="sm:max-w-2xl">
                <DialogHeader>
                    <DialogTitle>Process Refund for Order #{order.orderNumber}</DialogTitle>
                    <DialogDescription>Select items and quantities to refund. This will restock inventory.</DialogDescription>
                </DialogHeader>
                <form onSubmit={handleSubmit(onSubmit)}>
                    <div className="py-4 space-y-4">
                        <div className="space-y-2 max-h-64 overflow-y-auto">
                            {fields.map((field, index) => (
                                <div key={field.id} className="flex items-center gap-4 p-3 border rounded-md">
                                    <div className="flex-1">
                                        <p className="font-semibold">{field.name}</p>
                                        <p className="text-sm text-muted-foreground">Max: {field.maxQuantity} @ {formatCurrency(field.price)} each</p>
                                    </div>
                                    <Input
                                        type="number"
                                        className="w-24"
                                        max={field.maxQuantity}
                                        min={0}
                                        {...form.register(`items.${index}.quantity`, { valueAsNumber: true, min: 0, max: field.maxQuantity })}
                                    />
                                </div>
                            ))}
                        </div>
                         <div className="grid gap-2">
                             <Label htmlFor="reason">Reason for Refund (Optional)</Label>
                             <Input id="reason" {...form.register("reason")} />
                         </div>

                        <Separator />
                        <div className="flex justify-end text-xl font-bold">
                            Total Refund: {formatCurrency(totalRefund)}
                        </div>

                    </div>
                    <DialogFooter>
                        <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
                        <Button type="submit" variant="destructive">Confirm Refund</Button>
                    </DialogFooter>
                </form>
            </DialogContent>
        </Dialog>
    );
};


function FinancialKpiCard({ title, value, icon: Icon, isLoading }: { title: string, value: string | number, icon: React.ElementType, isLoading: boolean }) {
    
    return (
        <Card className="py-3 px-4">
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-1 p-0">
              <CardTitle className="text-xs font-medium">{title}</CardTitle>
              <Icon className="h-3 w-3 text-muted-foreground" />
            </CardHeader>
            <CardContent className="p-0 pt-2">
              {isLoading ? (
                <Skeleton className="h-6 w-3/4" />
              ) : (
                <div className="text-lg font-bold">{value}</div>
              )}
            </CardContent>
        </Card>
    );
}

export default function ReportsPage() {
    const [date, setDate] = React.useState<DateRange | undefined>({
        from: startOfDay(new Date()),
        to: endOfDay(new Date()),
    });
    const [refundingOrder, setRefundingOrder] = useState<Order | null>(null);
    const [selectedOrder, setSelectedOrder] = useState<Order | null>(null);
    const [detailModalOpen, setDetailModalOpen] = useState(false);
    const [activeBranchId, setActiveBranchId] = React.useState<string | null>(null);
    const [isEisEnabled, setIsEisEnabled] = useState(false);
    const [fiscalYearStartMonth, setFiscalYearStartMonth] = useState(1);
    const { format: formatCurrency } = useCurrency();
    const { data, loading, error } = useReports(date);

    // Get active branch from localStorage
    React.useEffect(() => {
        const branchId = localStorage.getItem('handypos-active-branch');
        if (branchId) {
            setActiveBranchId(branchId);
        }
    }, []);

    React.useEffect(() => {
        if (typeof window === 'undefined') return;
        try {
            const raw = localStorage.getItem('handypos-business-settings');
            if (!raw) return;
            const parsed = JSON.parse(raw);
            const rawEnable =
                parsed?.enableEis ??
                parsed?.enable_eis ??
                parsed?.eisEnabled ??
                parsed?.eis_enabled;
            if (rawEnable !== undefined) {
                setIsEisEnabled(rawEnable === true || rawEnable === 'true');
            }
            const rawFiscal = parsed?.fiscalYearStartMonth;
            if (rawFiscal !== undefined) {
                const parsedMonth = Number(rawFiscal);
                if (Number.isFinite(parsedMonth) && parsedMonth >= 1 && parsedMonth <= 12) {
                    setFiscalYearStartMonth(Math.trunc(parsedMonth));
                }
            }
        } catch (error) {
            console.warn('[Reports] Failed to parse business settings cache:', error);
        }
    }, []);

    const fromDate = date?.from ? startOfDay(date.from).toISOString() : new Date(0).toISOString();
    const toDateBase = date?.to || date?.from;
    const toDate = toDateBase ? endOfDay(toDateBase).toISOString() : new Date().toISOString();
    
    // Filter orders by branch and date range (includes all orders for reports)
    const allOrders = useLiveQuery(async () => {
        if (!activeBranchId) return undefined;
        const orders = await db.orders
            .where('branchId').equals(activeBranchId)
            .and(order => order.createdAt >= fromDate && order.createdAt <= toDate)
            .toArray();
        return sortOrdersByMostRecent(orders);
    }, [activeBranchId, fromDate, toDate]);

    // Normalize report orders so charts and KPIs use the same non-voided totals
    const activeOrders = useMemo(() => {
        if (!allOrders) return undefined;
        return allOrders
            .filter(order => order.status !== 'Voided' && order.status !== 'Cancelled')
            .map(normalizeOrderForReport);
    }, [allOrders]);

    const normalizedOrdersForEis = useMemo(() => {
        if (!allOrders) return undefined;
        return allOrders.map(order => {
            const resolvedStatus = resolveEisStatusForOrder(order, isEisEnabled);
            return {
                ...order,
                eis_status: resolvedStatus,
                eisStatus: resolvedStatus,
            };
        });
    }, [allOrders, isEisEnabled]);

    const eisSummary = useMemo(() => {
        if (!normalizedOrdersForEis) return EMPTY_EIS_SUMMARY;
        return calculateZReportSummary(normalizedOrdersForEis).eisSummary;
    }, [normalizedOrdersForEis]);

    const salesChartData = React.useMemo(() => {
        if (!activeOrders || !date?.from) return [];
        
        const intervalDays = eachDayOfInterval({ start: date.from, end: date.to || date.from });
        
        const dataByDay = intervalDays.map(day => {
            const dayStart = startOfDay(day);
            const dayEnd = endOfDay(day);
            const daySales = activeOrders
                .filter(order => {
                    const orderDate = new Date(order.createdAt);
                    return orderDate >= dayStart && orderDate <= dayEnd;
                })
                .reduce((sum, order) => sum + order.total, 0);

            return {
                name: format(day, 'MMM d'),
                total: daySales,
            };
        });
        return dataByDay;

    }, [activeOrders, date]);
    
    const categoryChartData = React.useMemo(() => {
        return data.salesByCategory.map((cat, index) => ({
            ...cat,
            fill: `hsl(var(--chart-${(index % 5) + 1}))`,
        }))
    }, [data.salesByCategory]);

    const orderStatusBadge: Record<Order['status'], 'default' | 'secondary' | 'destructive' | 'outline'> = {
        New: 'default',
        Preparing: 'secondary',
        Ready: 'outline',
        Completed: 'default',
        Voided: 'destructive',
        Cancelled: 'destructive',
        Refunded: 'destructive',
        'Partially Refunded': 'destructive',
    };


    const kpiMetrics = [
        { title: 'Total Sales', value: formatCurrency(data.totalSales), icon: DollarSign },
        { title: 'Gross Profit', value: formatCurrency(data.grossProfit), icon: TrendingUp },
        { title: 'Net Profit', value: formatCurrency(data.netProfit), icon: TrendingUp },
        { title: 'Total Transactions', value: data.totalTransactions.toString(), icon: ShoppingCart },
        // { title: 'Avg. Sale Value', value: formatCurrency(data.averageSaleValue), icon: BarChart },
        { title: 'Profit Margin', value: `${data.profitMargin.toFixed(2)}%`, icon: TrendingDown },
    ];

    const fiscalMetrics = [
        { title: 'Fiscal Assigned', value: `${eisSummary.ordersWithFiscalNumber}`, icon: Landmark },
        { title: 'Fiscal No Pending', value: `${eisSummary.pendingFiscalNumber}`, icon: Circle },
        { title: 'Receipt Pending', value: `${eisSummary.eisStatusCounts.pending}`, icon: Circle },
        { title: 'Fiscal Submitted', value: `${eisSummary.eisStatusCounts.submitted}`, icon: MoreHorizontal },
        { title: 'Fiscal Accepted', value: `${eisSummary.eisStatusCounts.accepted}`, icon: Check },
        { title: 'Fiscal Rejected', value: `${eisSummary.eisStatusCounts.rejected}`, icon: X },
    ];

    const handleExportReport = () => {
        if (!allOrders || allOrders.length === 0) {
            toast({
                variant: 'destructive',
                title: 'No data to export',
                description: 'There are no orders in the selected date range.',
            });
            return;
        }

        const rows = allOrders.map((order) => ({
            order_number: order.orderNumber,
            created_at: order.createdAt,
            status: order.status,
            payment_method: order.paymentMethod,
            subtotal: Number(order.subtotal ?? order.netAmount ?? order.net_amount ?? 0),
            tax: Number(order.tax ?? order.vatAmount ?? order.vat_amount ?? 0),
            total: Number(order.total ?? order.grossAmount ?? order.gross_amount ?? 0),
            cogs: Number(order.cogs ?? 0),
        }));

        const csv = Papa.unparse(rows);
        const fromLabel = date?.from ? format(date.from, 'yyyy-MM-dd') : 'from';
        const toLabel = format(date?.to || date?.from || new Date(), 'yyyy-MM-dd');
        const filename = `financial-report-${fromLabel}-to-${toLabel}.csv`;
        const downloadStarted = downloadTextFile(csv, filename);

        if (!downloadStarted) {
            toast({
                variant: 'destructive',
                title: 'Export failed',
                description: 'Could not start file download. Please try again.',
            });
            return;
        }

        toast({
            title: 'Export complete',
            description: `${rows.length} records downloaded as ${filename}.`,
        });
    };

    const handleExportMraAudit = () => {
        if (!normalizedOrdersForEis || normalizedOrdersForEis.length === 0) {
            toast({
                variant: 'destructive',
                title: 'No data to export',
                description: 'There are no receipts in the selected date range.',
            });
            return;
        }

        const rows = normalizedOrdersForEis
            .map((order) => {
                const eisStatus = resolveEisStatusForOrder(order, isEisEnabled);
                if (!eisStatus) {
                    return null;
                }

                const buyer = resolveBuyerDetails(order);
                const fiscalInvoice = toTrimmedString(
                    (order as any)?.fiscalInvoiceNumber ?? (order as any)?.fiscal_invoice_number
                );
                const itemsSummary = Array.isArray(order.items)
                    ? order.items
                          .map((item) => `${item.name} x${item.quantity} @ ${Number(item.price ?? 0).toFixed(2)}`)
                          .join(' | ')
                    : '';

                return {
                    receipt_id: order.id,
                    order_number: order.orderNumber,
                    created_at: order.createdAt,
                    status: order.status,
                    payment_method: order.paymentMethod,
                    subtotal: Number(order.subtotal ?? order.netAmount ?? order.net_amount ?? 0),
                    tax: Number(order.tax ?? order.vatAmount ?? order.vat_amount ?? 0),
                    total: Number(order.total ?? order.grossAmount ?? order.gross_amount ?? 0),
                    fiscal_invoice_number: fiscalInvoice,
                    eis_status: eisStatus,
                    eis_uuid: toTrimmedString((order as any)?.eisUuid ?? (order as any)?.eis_uuid),
                    eis_submitted_at: toTrimmedString((order as any)?.eisSubmittedAt ?? (order as any)?.eis_submitted_at),
                    qr_code_payload: toTrimmedString((order as any)?.qrCodePayload ?? (order as any)?.qr_code_payload),
                    digital_signature: toTrimmedString((order as any)?.digitalSignature ?? (order as any)?.digital_signature),
                    buyer_name: buyer.name,
                    buyer_phone: buyer.phone,
                    buyer_tin: buyer.tin,
                    buyer_email: buyer.email,
                    buyer_address: buyer.address,
                    branch_id: order.branchId,
                    session_id: order.sessionId,
                    items: itemsSummary,
                };
            })
            .filter(Boolean) as Array<Record<string, any>>;

        if (rows.length === 0) {
            toast({
                variant: 'destructive',
                title: 'No receipts to export',
                description: 'No submitted or pending receipts were found in the selected date range.',
            });
            return;
        }

        const csv = Papa.unparse(rows);
        const fromLabel = date?.from ? format(date.from, 'yyyy-MM-dd') : 'from';
        const toLabel = format(date?.to || date?.from || new Date(), 'yyyy-MM-dd');
        const filename = `mra-audit-${fromLabel}-to-${toLabel}.csv`;
        const downloadStarted = downloadTextFile(csv, filename);

        if (!downloadStarted) {
            toast({
                variant: 'destructive',
                title: 'Export failed',
                description: 'Could not start file download. Please try again.',
            });
            return;
        }

        toast({
            title: 'Export complete',
            description: `${rows.length} receipts downloaded as ${filename}.`,
        });
    };

  return (
    <>
    <div className="flex flex-col gap-6">
      <div className="flex w-full flex-col items-stretch justify-between gap-4 sm:flex-row sm:items-center">
        <div className="grid gap-2">
          <h1 className="text-2xl font-bold tracking-tight">Financial Reports</h1>
          <p className="text-muted-foreground">
            Analyze your sales, profits, and trends for the active branch.
          </p>
        </div>
        <div className="flex items-center gap-2">
            <DropdownMenu>
                <DropdownMenuTrigger asChild>
                    <Button id="date" variant="outline" className={cn('w-full sm:w-[260px] justify-start text-left font-normal', !date && 'text-muted-foreground')}>
                        <CalendarIcon className="mr-2 h-4 w-4" />
                        {date?.from ? (
                          date.to ? (
                            <>
                              {format(date.from, 'LLL dd, y')} - {format(date.to, 'LLL dd, y')}
                            </>
                          ) : (
                            format(date.from, 'LLL dd, y')
                          )
                        ) : (
                          <span>Pick a date</span>
                        )}
                    </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent className="w-auto" align="end">
                    <DropdownMenuItem onClick={() => setDate({ from: startOfDay(new Date()), to: endOfDay(new Date()) })}>
                        Today
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={() => {
                        const yesterday = subDays(new Date(), 1);
                        setDate({ from: startOfDay(yesterday), to: endOfDay(yesterday) });
                      }}
                    >
                        Yesterday
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => setDate({ from: startOfDay(subDays(new Date(), 7)), to: endOfDay(new Date()) })}>
                        Last 7 Days
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => setDate({ from: startOfDay(subDays(new Date(), 30)), to: endOfDay(new Date()) })}>
                        Last 30 Days
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={() => {
                        const today = new Date();
                        setDate({ from: startOfMonth(today), to: endOfDay(today) });
                      }}
                    >
                        This Month
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={() => {
                        const today = new Date();
                        const fiscalStart = resolveFiscalYearStart(today, fiscalYearStartMonth);
                        setDate({ from: startOfDay(fiscalStart), to: endOfDay(today) });
                      }}
                    >
                        Fiscal Year (YTD)
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuSub>
                        <DropdownMenuSubTrigger>Custom Range</DropdownMenuSubTrigger>
                        <DropdownMenuPortal>
                            <DropdownMenuSubContent className="w-auto p-0">
                                <Calendar
                                  initialFocus
                                  mode="range"
                                  defaultMonth={date?.from}
                                  selected={date}
                                  onSelect={setDate}
                                  numberOfMonths={2}
                                />
                            </DropdownMenuSubContent>
                        </DropdownMenuPortal>
                    </DropdownMenuSub>
                </DropdownMenuContent>
            </DropdownMenu>
             <Button variant="outline" onClick={handleExportReport}><Download className="mr-2 h-4 w-4" /> Export</Button>
             <Button variant="outline" onClick={handleExportMraAudit}><Landmark className="mr-2 h-4 w-4" /> MRA Audit</Button>
        </div>
      </div>
      
      <Tabs defaultValue="sales" className="w-full">
        <div className="overflow-x-auto pb-1">
          <TabsList className="h-auto w-max min-w-full gap-1 sm:grid sm:w-full sm:grid-cols-6">
              <TabsTrigger value="sales" className="shrink-0 whitespace-nowrap px-3 py-2 text-xs sm:text-sm">
                <span className="sm:hidden">Sales</span>
                <span className="hidden sm:inline">Sales Report</span>
              </TabsTrigger>
              <TabsTrigger value="orders" className="shrink-0 whitespace-nowrap px-3 py-2 text-xs sm:text-sm">
                Orders
              </TabsTrigger>
              <TabsTrigger value="products" className="shrink-0 whitespace-nowrap px-3 py-2 text-xs sm:text-sm">
                <span className="sm:hidden">Products</span>
                <span className="hidden sm:inline">Product Report</span>
              </TabsTrigger>
              <TabsTrigger value="categories" className="shrink-0 whitespace-nowrap px-3 py-2 text-xs sm:text-sm">
                <span className="sm:hidden">Categories</span>
                <span className="hidden sm:inline">Category Report</span>
              </TabsTrigger>
              <TabsTrigger value="staff" className="shrink-0 whitespace-nowrap px-3 py-2 text-xs sm:text-sm">
                <span className="sm:hidden">Staff</span>
                <span className="hidden sm:inline">Staff Report</span>
              </TabsTrigger>
              <TabsTrigger value="fiscal" className="shrink-0 whitespace-nowrap px-3 py-2 text-xs sm:text-sm">
                <span className="sm:hidden">Fiscal</span>
                <span className="hidden sm:inline">Fiscal (MRA)</span>
              </TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="sales">
            <div className="grid gap-6">
                 <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
                    {kpiMetrics.map(metric => (
                        <FinancialKpiCard
                            key={metric.title}
                            title={metric.title}
                            value={metric.value}
                            icon={metric.icon}
                            isLoading={loading}
                        />
                    ))}
                </div>

                <Card>
                    <CardHeader>
                        <CardTitle>Product Type Sales Mix</CardTitle>
                        <CardDescription>Separate sales totals for normal products, oils, and fuel in the selected period.</CardDescription>
                    </CardHeader>
                    <CardContent>
                        {loading ? (
                            <div className="grid gap-4 md:grid-cols-3">
                                {[...Array(3)].map((_, index) => (
                                    <div key={index} className="rounded-lg border p-4">
                                        <Skeleton className="h-5 w-28" />
                                        <div className="mt-4 space-y-3">
                                            <Skeleton className="h-4 w-full" />
                                            <Skeleton className="h-4 w-5/6" />
                                            <Skeleton className="h-4 w-2/3" />
                                        </div>
                                    </div>
                                ))}
                            </div>
                        ) : (
                            <div className="grid gap-4 md:grid-cols-3">
                                {data.salesByProductType.map((entry) => (
                                    <div key={entry.category} className="rounded-lg border bg-muted/20 p-4">
                                        <div className="flex items-start justify-between gap-3">
                                            <div>
                                                <p className="font-medium">{entry.label}</p>
                                                <p className="text-xs text-muted-foreground">
                                                    {entry.itemCount} product{entry.itemCount === 1 ? '' : 's'}
                                                </p>
                                            </div>
                                            <Badge variant="outline" className={entry.badgeClassName}>
                                                {entry.shortLabel}
                                            </Badge>
                                        </div>
                                        <div className="mt-4 space-y-2 text-sm">
                                            <div className="flex items-center justify-between gap-3">
                                                <span className="text-muted-foreground">Total Sales</span>
                                                <span className="font-semibold">{formatCurrency(entry.amount)}</span>
                                            </div>
                                            <div className="flex items-center justify-between gap-3">
                                                <span className="text-muted-foreground">Qty Sold</span>
                                                <span>{formatQuantity(entry.quantity)}</span>
                                            </div>
                                            <div className="flex items-center justify-between gap-3">
                                                <span className="text-muted-foreground">Distinct Products</span>
                                                <span>{entry.itemCount}</span>
                                            </div>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </CardContent>
                </Card>

                <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
                    <Card className="col-span-1 lg:col-span-3">
                        <CardHeader>
                            <CardTitle>Sales Performance</CardTitle>
                            <CardDescription>Showing sales trend for the selected period.</CardDescription>
                        </CardHeader>
                        <CardContent className="pl-2">
                             {loading ? <Skeleton className="h-[300px] w-full" /> :
                            <ResponsiveContainer width="100%" height={300}>
                            <AreaChart data={salesChartData}>
                                <defs><linearGradient id="colorTotal" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="hsl(var(--primary))" stopOpacity={0.8}/><stop offset="95%" stopColor="hsl(var(--primary))" stopOpacity={0}/></linearGradient></defs>
                                <XAxis dataKey="name" stroke="hsl(var(--muted-foreground))" fontSize={12} tickLine={false} axisLine={false}/>
                                <YAxis stroke="hsl(var(--muted-foreground))" fontSize={12} tickLine={false} axisLine={false} tickFormatter={(value) => `$${value.toLocaleString()}`}/>
                                <RechartsTooltip contentStyle={{ backgroundColor: 'hsl(var(--background))', border: '1px solid hsl(var(--border))', borderRadius: 'var(--radius)' }} formatter={(value) => formatCurrency(value as number)} />
                                <Area type="monotone" dataKey="total" stroke="hsl(var(--primary))" fillOpacity={1} fill="url(#colorTotal)" strokeWidth={2}/>
                            </AreaChart>
                            </ResponsiveContainer>
                            }
                        </CardContent>
                    </Card>

                    <Card className="col-span-1 lg:col-span-2">
                        <CardHeader>
                            <CardTitle>Profit & Loss Statement</CardTitle>
                            <CardDescription>P&L for the selected period.</CardDescription>
                        </CardHeader>
                        <CardContent>
                            {loading ? (
                                <div className="space-y-4">
                                    <Skeleton className="h-8 w-full" />
                                    <Skeleton className="h-8 w-full" />
                                    <Skeleton className="h-8 w-full" />
                                    <Skeleton className="h-8 w-full" />
                                    <Skeleton className="h-8 w-full" />
                                </div>
                            ) : (
                                <Table>
                                    <TableBody>
                                        <TableRow><TableCell>Revenue (Excl. Tax)</TableCell><TableCell className="text-right font-medium">{formatCurrency(data.totalRevenue)}</TableCell></TableRow>
                                        <TableRow><TableCell>Tax Collected</TableCell><TableCell className="text-right font-medium text-green-600">+{formatCurrency(data.totalTax)}</TableCell></TableRow>
                                        <TableRow className="bg-muted/50 font-semibold"><TableCell>Total Sales (Incl. Tax)</TableCell><TableCell className="text-right">{formatCurrency(data.totalSales)}</TableCell></TableRow>
                                        <TableRow><TableCell>Cost of Goods Sold (COGS)</TableCell><TableCell className="text-right font-medium">{`-${formatCurrency(data.totalCogs)}`}</TableCell></TableRow>
                                        <TableRow className="bg-muted/50 font-semibold"><TableCell>Gross Profit</TableCell><TableCell className="text-right">{formatCurrency(data.grossProfit)}</TableCell></TableRow>
                                        <TableRow><TableCell>Operating Expenses</TableCell><TableCell className="text-right font-medium">{`-${formatCurrency(data.totalExpenses)}`}</TableCell></TableRow>
                                        <TableRow className="bg-muted/50 font-bold text-lg"><TableCell>Net Profit</TableCell><TableCell className="text-right">{formatCurrency(data.netProfit)}</TableCell></TableRow>
                                    </TableBody>
                                </Table>
                            )}
                        </CardContent>
                    </Card>
                </div>
            </div>
        </TabsContent>
         <TabsContent value="orders">
            <Card>
                <CardHeader>
                    <CardTitle>Order History</CardTitle>
                    <CardDescription>A detailed list of all orders within the selected period.</CardDescription>
                </CardHeader>
                <CardContent>
                    <Table>
                        <TableHeader>
                            <TableRow>
                                <TableHead>Order #</TableHead>
                                <TableHead>Date</TableHead>
                                <TableHead>Status</TableHead>
                                <TableHead>Payment</TableHead>
                                <TableHead className="text-right">Total</TableHead>
                                <TableHead className="text-right">Actions</TableHead>
                            </TableRow>
                        </TableHeader>
                        <TableBody>
                            {!allOrders ? (
                                <TableRow><TableCell colSpan={6} className="text-center"><Loader2 className="animate-spin mx-auto" /></TableCell></TableRow>
                            ) : allOrders.map(order => {
                                const eisDisplayStatus = resolveEisSaleStatusLabel(order, isEisEnabled);
                                const displayStatus = eisDisplayStatus || order.status;
                                const displayStatusVariant = eisDisplayStatus === 'Fiscal Failed'
                                    ? 'destructive'
                                    : eisDisplayStatus === 'EIS Pending'
                                        ? 'outline'
                                        : eisDisplayStatus === 'EIS Submitted'
                                            ? 'default'
                                            : orderStatusBadge[order.status];

                                return (
                                <TableRow key={order.id} className={`${order.status === 'Voided' ? 'opacity-60' : ''}`}>
                                    <TableCell className="font-mono">#{order.orderNumber}</TableCell>
                                    <TableCell>{format(new Date(order.createdAt), 'PPpp')}</TableCell>
                                    <TableCell>
                                        <div className="flex flex-col items-start gap-1">
                                            <Badge variant={displayStatusVariant}>{displayStatus}</Badge>
                                        </div>
                                    </TableCell>
                                    <TableCell><Badge variant="outline">{order.paymentMethod}</Badge></TableCell>
                                    <TableCell className="text-right font-semibold">{formatCurrency(order.total)}</TableCell>
                                    <TableCell className="text-right">
                                        <DropdownMenu>
                                            <DropdownMenuTrigger asChild>
                                                <Button variant="ghost" size="icon"><MoreHorizontal /></Button>
                                            </DropdownMenuTrigger>
                                            <DropdownMenuContent align="end">
                                                <DropdownMenuItem onClick={() => { setSelectedOrder(order); setDetailModalOpen(true); }}>
                                                    View Details
                                                </DropdownMenuItem>
                                                <DropdownMenuItem onClick={() => setRefundingOrder(order)} disabled={order.status === 'Refunded' || order.status === 'Voided'}>
                                                    <Undo2 className="mr-2" /> Refund
                                                </DropdownMenuItem>
                                            </DropdownMenuContent>
                                        </DropdownMenu>
                                    </TableCell>
                                </TableRow>
                                );
                            })}
                        </TableBody>
                    </Table>
                </CardContent>
            </Card>
        </TabsContent>
        <TabsContent value="products">
            <div className="grid gap-6">
                <Card>
                    <CardHeader>
                        <CardTitle>Top Selling Products</CardTitle>
                        <CardDescription>Your best performing products by revenue for the selected period.</CardDescription>
                    </CardHeader>
                    <CardContent>
                        <Table>
                            <TableHeader>
                                <TableRow>
                                    <TableHead>Product</TableHead>
                                    <TableHead>Type</TableHead>
                                    <TableHead className="text-right">Quantity Sold</TableHead>
                                    <TableHead className="text-right">Revenue</TableHead>
                                    <TableHead className="text-right">Total Sales</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {loading ? (
                                    [...Array(5)].map((_, i) => (
                                        <TableRow key={i}>
                                            <TableCell><Skeleton className="h-5 w-3/4" /></TableCell>
                                            <TableCell><Skeleton className="h-5 w-16" /></TableCell>
                                            <TableCell><Skeleton className="ml-auto h-5 w-14" /></TableCell>
                                            <TableCell><Skeleton className="ml-auto h-5 w-20" /></TableCell>
                                            <TableCell><Skeleton className="ml-auto h-5 w-20" /></TableCell>
                                        </TableRow>
                                    ))
                                ) : data.topProducts.length === 0 ? (
                                    <TableRow>
                                        <TableCell colSpan={5} className="text-center text-muted-foreground">
                                            No product sales for this period.
                                        </TableCell>
                                    </TableRow>
                                ) : data.topProducts.map(product => (
                                    <TableRow key={product.name}>
                                        <TableCell className="font-medium">{product.name}</TableCell>
                                        <TableCell><ProductTypeBadge productType={product.productType} /></TableCell>
                                        <TableCell className="text-right">{formatQuantity(product.quantity)}</TableCell>
                                        <TableCell className="text-right font-semibold">{formatCurrency(product.revenue)}</TableCell>
                                        <TableCell className="text-right font-semibold text-green-600">{formatCurrency(product.totalSales)}</TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                    </CardContent>
                </Card>

                <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
                    <Card>
                        <CardHeader>
                            <CardTitle>Fast-Moving Products</CardTitle>
                            <CardDescription>Products with the highest quantity sold in the selected period.</CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead>Product</TableHead>
                                        <TableHead>Type</TableHead>
                                        <TableHead className="text-right">Qty Sold</TableHead>
                                        <TableHead className="text-right">Avg/Day</TableHead>
                                        <TableHead className="text-right">Remaining</TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {loading ? (
                                        [...Array(5)].map((_, i) => (
                                            <TableRow key={i}>
                                                <TableCell><Skeleton className="h-5 w-3/4" /></TableCell>
                                                <TableCell><Skeleton className="h-5 w-16" /></TableCell>
                                                <TableCell><Skeleton className="ml-auto h-5 w-12" /></TableCell>
                                                <TableCell><Skeleton className="ml-auto h-5 w-12" /></TableCell>
                                                <TableCell><Skeleton className="ml-auto h-5 w-16" /></TableCell>
                                            </TableRow>
                                        ))
                                    ) : data.fastMovingProducts.length === 0 ? (
                                        <TableRow>
                                            <TableCell colSpan={5} className="text-center text-muted-foreground">
                                                No product movement for this period.
                                            </TableCell>
                                        </TableRow>
                                    ) : data.fastMovingProducts.map((product, index) => (
                                        <TableRow key={`fast-${product.name}-${index}`}>
                                            <TableCell className="font-medium">{product.name}</TableCell>
                                            <TableCell><ProductTypeBadge productType={product.productType} /></TableCell>
                                            <TableCell className="text-right">{formatQuantity(product.quantity)}</TableCell>
                                            <TableCell className="text-right">{formatQuantity(product.averagePerDay)}</TableCell>
                                            <TableCell className="text-right">
                                                {formatQuantity(product.currentStock)} {product.unitType}
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle>Slow-Moving Products</CardTitle>
                            <CardDescription>Products with the least movement in the selected period.</CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead>Product</TableHead>
                                        <TableHead>Type</TableHead>
                                        <TableHead className="text-right">Qty Sold</TableHead>
                                        <TableHead className="text-right">Avg/Day</TableHead>
                                        <TableHead className="text-right">Remaining</TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {loading ? (
                                        [...Array(5)].map((_, i) => (
                                            <TableRow key={i}>
                                                <TableCell><Skeleton className="h-5 w-3/4" /></TableCell>
                                                <TableCell><Skeleton className="h-5 w-16" /></TableCell>
                                                <TableCell><Skeleton className="ml-auto h-5 w-12" /></TableCell>
                                                <TableCell><Skeleton className="ml-auto h-5 w-12" /></TableCell>
                                                <TableCell><Skeleton className="ml-auto h-5 w-16" /></TableCell>
                                            </TableRow>
                                        ))
                                    ) : data.slowMovingProducts.length === 0 ? (
                                        <TableRow>
                                            <TableCell colSpan={5} className="text-center text-muted-foreground">
                                                No products available for movement analysis.
                                            </TableCell>
                                        </TableRow>
                                    ) : data.slowMovingProducts.map((product, index) => (
                                        <TableRow key={`slow-${product.name}-${index}`}>
                                            <TableCell className="font-medium">{product.name}</TableCell>
                                            <TableCell><ProductTypeBadge productType={product.productType} /></TableCell>
                                            <TableCell className="text-right">{formatQuantity(product.quantity)}</TableCell>
                                            <TableCell className="text-right">{formatQuantity(product.averagePerDay)}</TableCell>
                                            <TableCell className="text-right">
                                                {formatQuantity(product.currentStock)} {product.unitType}
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </CardContent>
                    </Card>
                </div>
            </div>
        </TabsContent>
        <TabsContent value="categories">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <Card>
                    <CardHeader><CardTitle>Sales by Category</CardTitle><CardDescription>Revenue breakdown by product category.</CardDescription></CardHeader>
                    <CardContent>
                         <ResponsiveContainer width="100%" height={300}>
                            <PieChart>
                                <Pie data={categoryChartData} dataKey="revenue" nameKey="name" cx="50%" cy="50%" outerRadius={100} label={(entry) => entry.name}>
                                    {categoryChartData.map((entry, index) => (<Cell key={`cell-${index}`} fill={entry.fill} />))}
                                </Pie>
                                <RechartsTooltip formatter={(value) => formatCurrency(value as number)} />
                            </PieChart>
                        </ResponsiveContainer>
                    </CardContent>
                </Card>
                <Card>
                    <CardHeader><CardTitle>Category Revenue</CardTitle><CardDescription>Table view of revenue per category.</CardDescription></CardHeader>
                    <CardContent>
                         <Table>
                            <TableHeader><TableRow><TableHead>Category</TableHead><TableHead className="text-right">Revenue</TableHead><TableHead className="text-right">Total Sales</TableHead></TableRow></TableHeader>
                            <TableBody>
                                {loading ? (
                                     [...Array(5)].map((_, i) => <TableRow key={i}><TableCell><Skeleton className="h-5 w-3/4" /></TableCell><TableCell><Skeleton className="h-5 w-1/4 ml-auto" /></TableCell><TableCell><Skeleton className="h-5 w-1/4 ml-auto" /></TableCell></TableRow>)
                                ) : data.salesByCategory.map(cat => (
                                    <TableRow key={cat.name}><TableCell className="font-medium">{cat.name}</TableCell><TableCell className="text-right font-semibold">{formatCurrency(cat.revenue)}</TableCell><TableCell className="text-right font-semibold text-green-600">{formatCurrency(cat.totalSales)}</TableCell></TableRow>
                                ))}
                            </TableBody>
                        </Table>
                    </CardContent>
                </Card>
            </div>
        </TabsContent>
        <TabsContent value="staff">
             <Card>
                <CardHeader>
                    <CardTitle>Sales by Staff</CardTitle>
                    <CardDescription>Performance overview for each staff member in the selected period.</CardDescription>
                </CardHeader>
                <CardContent>
                    <Table>
                        <TableHeader><TableRow><TableHead>Staff Member</TableHead><TableHead className="text-right">Transactions</TableHead><TableHead className="text-right">Revenue</TableHead><TableHead className="text-right">Total Sales</TableHead></TableRow></TableHeader>
                        <TableBody>
                             {loading ? (
                                [...Array(3)].map((_, i) => <TableRow key={i}><TableCell><Skeleton className="h-5 w-1/2" /></TableCell><TableCell><Skeleton className="h-5 w-1/4 ml-auto" /></TableCell><TableCell><Skeleton className="h-5 w-1/4 ml-auto" /></TableCell><TableCell><Skeleton className="h-5 w-1/4 ml-auto" /></TableCell></TableRow>)
                            ) : data.salesByStaff.map(staff => (
                                <TableRow key={staff.name}><TableCell className="font-medium">{staff.name}</TableCell><TableCell className="text-right">{staff.transactions}</TableCell><TableCell className="text-right font-semibold">{formatCurrency(staff.revenue)}</TableCell><TableCell className="text-right font-semibold text-green-600">{formatCurrency(staff.totalSales)}</TableCell></TableRow>
                            ))}
                        </TableBody>
                    </Table>
                </CardContent>
            </Card>
        </TabsContent>
        <TabsContent value="fiscal">
            <div className="grid gap-6">
                <Card>
                    <CardHeader>
                        <CardTitle>Fiscal (MRA) Summary</CardTitle>
                        <CardDescription>Fiscal receipt and EIS submission overview for the selected date range.</CardDescription>
                    </CardHeader>
                    <CardContent>
                        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
                            {fiscalMetrics.map(metric => (
                                <FinancialKpiCard
                                    key={metric.title}
                                    title={metric.title}
                                    value={metric.value}
                                    icon={metric.icon}
                                    isLoading={loading}
                                />
                            ))}
                        </div>
                    </CardContent>
                </Card>
                <Card>
                    <CardHeader>
                        <CardTitle>Receipt Compliance</CardTitle>
                        <CardDescription>Key fiscal markers for audit readiness.</CardDescription>
                    </CardHeader>
                    <CardContent>
                        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                            <div className="space-y-1">
                                <p className="text-xs text-muted-foreground">First Fiscal Invoice</p>
                                <p className="font-medium">{eisSummary.firstFiscalInvoice || 'N/A'}</p>
                            </div>
                            <div className="space-y-1">
                                <p className="text-xs text-muted-foreground">Last Fiscal Invoice</p>
                                <p className="font-medium">{eisSummary.lastFiscalInvoice || 'N/A'}</p>
                            </div>
                            <div className="space-y-1">
                                <p className="text-xs text-muted-foreground">First Submission</p>
                                <p className="font-medium">{formatOptionalDateTime(eisSummary.firstSubmissionAt)}</p>
                            </div>
                            <div className="space-y-1">
                                <p className="text-xs text-muted-foreground">Last Submission</p>
                                <p className="font-medium">{formatOptionalDateTime(eisSummary.lastSubmissionAt)}</p>
                            </div>
                            <div className="space-y-1">
                                <p className="text-xs text-muted-foreground">Receipts with QR</p>
                                <p className="font-medium">{eisSummary.ordersWithQr}</p>
                            </div>
                            <div className="space-y-1">
                                <p className="text-xs text-muted-foreground">Receipts with Signature</p>
                                <p className="font-medium">{eisSummary.ordersWithSignature}</p>
                            </div>
                        </div>
                    </CardContent>
                </Card>
            </div>
        </TabsContent>
      </Tabs>
    </div>
    {refundingOrder && (
        <RefundDialog
            order={refundingOrder}
            isOpen={!!refundingOrder}
            onOpenChange={(open) => !open && setRefundingOrder(null)}
        />
    )}
    </>
  );
}
