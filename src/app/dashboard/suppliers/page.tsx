

'use client';

import React, { useMemo, useState } from 'react';
import { useLiveQuery } from 'dexie-react-hooks';
import { useForm } from 'react-hook-form';
import { MoreHorizontal, PlusCircle, Edit, Trash2, Package, History, DollarSign, Loader2, Printer, Download, RefreshCw } from 'lucide-react';

import { db, type Supplier, type InventoryItem, type PurchaseRecord } from '@/lib/db';
import { useCurrency } from '@/hooks/use-currency';
import { useAuth } from '@/hooks/use-auth';
import { createSupplier, updateSupplier, deleteSupplier } from '@/lib/services/supplier-service';
import { syncService } from '@/lib/services/sync-service';
import { logAuditAction } from '@/lib/audit';
import { authFetch } from '@/lib/auth-fetch';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  CardFooter,
} from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  DialogFooter,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { toast } from '@/hooks/use-toast';
import { Badge } from '@/components/ui/badge';
import { generatePurchaseInvoicePDF } from '@/lib/purchase-invoice-pdf';


const resolveStoredBusinessId = (): string => {
    if (typeof window === 'undefined') return '';

    const storageKeys = ['handy-pos-business', 'handypos-business'];
    for (const key of storageKeys) {
        const raw = localStorage.getItem(key);
        if (!raw) continue;
        try {
            const parsed = JSON.parse(raw);
            if (parsed?.id) {
                return String(parsed.id);
            }
        } catch (error) {
            console.warn(`[SuppliersPage] Failed to parse ${key}:`, error);
        }
    }

    const fallbackBusinessId = localStorage.getItem('handypos-business-id');
    return fallbackBusinessId ? String(fallbackBusinessId) : '';
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

const readBooleanFlag = (value: unknown): boolean | null => {
    if (typeof value === 'boolean') return value;
    if (typeof value === 'number') return value !== 0;
    if (typeof value === 'string') {
        const normalized = value.trim().toLowerCase();
        if (['true', '1', 'yes', 'on'].includes(normalized)) return true;
        if (['false', '0', 'no', 'off'].includes(normalized)) return false;
    }
    return null;
};

const readJsonStorage = (key: string): any => {
    if (typeof window === 'undefined') return null;
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    try {
        return JSON.parse(raw);
    } catch {
        return null;
    }
};

const resolveCachedEisEnabled = (business?: any): boolean => {
    const storedBusiness =
        readJsonStorage('handy-pos-business') ||
        readJsonStorage('handypos-business') ||
        {};
    const storedSettings = readJsonStorage('handypos-business-settings') || {};
    const candidates = [
        business?.enable_eis,
        business?.enableEis,
        business?.eis_enabled,
        business?.eisEnabled,
        storedBusiness?.enable_eis,
        storedBusiness?.enableEis,
        storedBusiness?.eis_enabled,
        storedBusiness?.eisEnabled,
        storedSettings?.enable_eis,
        storedSettings?.enableEis,
        storedSettings?.eis_enabled,
        storedSettings?.eisEnabled,
    ];

    for (const value of candidates) {
        const parsed = readBooleanFlag(value);
        if (parsed !== null) return parsed;
    }
    return false;
};

const parseDateCandidate = (...values: unknown[]): Date | null => {
    for (const value of values) {
        if (value instanceof Date) {
            if (!Number.isNaN(value.getTime())) return value;
            continue;
        }

        if (typeof value === 'number' && Number.isFinite(value)) {
            const ms = value < 1_000_000_000_000 ? value * 1000 : value;
            const parsed = new Date(ms);
            if (!Number.isNaN(parsed.getTime())) return parsed;
            continue;
        }

        if (typeof value !== 'string') continue;

        const trimmed = value.trim();
        if (!trimmed) continue;
        if (trimmed.toLowerCase() === 'null' || trimmed.toLowerCase() === 'undefined') continue;

        if (/^\d+$/.test(trimmed)) {
            const numericValue = Number(trimmed);
            if (Number.isFinite(numericValue)) {
                const ms = numericValue < 1_000_000_000_000 ? numericValue * 1000 : numericValue;
                const parsed = new Date(ms);
                if (!Number.isNaN(parsed.getTime())) return parsed;
            }
        }

        const parsed = new Date(trimmed);
        if (!Number.isNaN(parsed.getTime())) return parsed;
    }

    return null;
};

const formatDisplayDate = (...values: unknown[]): string => {
    const parsed = parseDateCandidate(...values);
    return parsed ? parsed.toLocaleDateString() : '-';
};

const dateSortValue = (...values: unknown[]): number => {
    const parsed = parseDateCandidate(...values);
    return parsed ? parsed.getTime() : 0;
};

const normalizeTaxRate = (value: unknown): number => {
    const parsed = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
};

const resolveTaxMethod = (value: unknown): 'inclusive' | 'exclusive' => {
    return value === 'inclusive' ? 'inclusive' : 'exclusive';
};

const resolveRecordVat = (record: PurchaseRecord): number => {
    const taxRate = normalizeTaxRate(record.taxRate);
    const method = resolveTaxMethod(record.taxCalculationMethod);
    const base = Number(record.totalCost || 0);
    if (!Number.isFinite(base) || base <= 0 || taxRate <= 0) {
        return typeof record.taxAmount === 'number' && Number.isFinite(record.taxAmount) ? record.taxAmount : 0;
    }
    if (method === 'inclusive') {
        return base - base / (1 + taxRate / 100);
    }
    return base * (taxRate / 100);
};

const resolveRecordGross = (record: PurchaseRecord, vatAmount: number): number => {
    const base = Number(record.totalCost || 0);
    if (!Number.isFinite(base)) return 0;
    const method = resolveTaxMethod(record.taxCalculationMethod);
    return method === 'exclusive' ? base + (vatAmount || 0) : base;
};

type SupplierPurchaseGroup = {
    groupId: string;
    supplierName: string;
    displayDate: string;
    dateSortValue: number;
    createdDateDisplay: string;
    createdDateSortValue: number;
    paymentStatus: string;
    totalCost: number;
    totalQuantity: number;
    amountDue: number;
    totalVat: number;
    totalWithVat: number;
    referenceNumber?: string;
    vatAmount?: number;
    items: PurchaseRecord[];
};


const SupplierForm = ({
    onFormSubmit,
    defaultValues,
    activeBranchId,
    activeBusinessId,
    isEisEnabled,
}: {
    onFormSubmit: () => void,
    defaultValues?: Supplier,
    activeBranchId: string,
    activeBusinessId?: string,
    isEisEnabled?: boolean,
}) => {
    const { user, business } = useAuth();
    const { register, handleSubmit, reset, formState: { errors } } = useForm({ defaultValues });

    const onSubmit = async (data: {
        name: string;
        contactPerson?: string;
        email?: string;
        phone?: string;
        address?: string;
        city?: string;
        region?: string;
        country?: string;
        supplierTin?: string;
        mraSupplierId?: string | number;
        vatRegistered?: boolean;
    }) => {
        if (isEisEnabled) {
            toast({
                variant: 'destructive',
                title: 'Suppliers are EIS managed',
                description: 'Use Sync EIS Suppliers.',
            });
            return;
        }

        if (!user) {
            toast({ variant: 'destructive', title: 'User not found' });
            return;
        }

        try {
            const parsedMraSupplierId = Number(data.mraSupplierId);
            const mraSupplierId = Number.isFinite(parsedMraSupplierId) && parsedMraSupplierId > 0
                ? parsedMraSupplierId
                : undefined;
            const businessId = String(
                activeBusinessId ||
                user.businessId ||
                business?.id ||
                resolveStoredBusinessId() ||
                ''
            ).trim();

            if (!businessId) {
                toast({
                    variant: 'destructive',
                    title: 'Business context missing',
                    description: 'Please refresh and try again.',
                });
                return;
            }

            if (defaultValues) {
                await updateSupplier(
                    defaultValues.id,
                    {
                        ...data,
                        mraSupplierId,
                    },
                    user.uid,
                    user.displayName || user.email || 'Unknown',
                    activeBranchId
                );
                toast({ title: 'Supplier Updated' });
            } else {
                await createSupplier(
                    { 
                        name: data.name, 
                        email: data.email, 
                        phone: data.phone, 
                        address: data.address, 
                        city: data.city,
                        contactPerson: data.contactPerson,
                        region: data.region,
                        country: data.country,
                        // MRA EIS Compliance Fields
                        supplierTin: data.supplierTin,
                        mraSupplierId,
                        vatRegistered: data.vatRegistered
                    },
                    user.uid,
                    user.displayName || user.email || 'Unknown',
                    activeBranchId,
                    businessId
                );
                toast({ title: 'Supplier Added' });
            }
            reset();
            onFormSubmit();
        } catch (error) {
            console.error('Failed to save supplier:', error);
            toast({ variant: 'destructive', title: 'Error saving supplier' });
        }
    };

    return (
        <form onSubmit={handleSubmit(onSubmit)} className="grid gap-4 py-4">
            <div>
                <Label htmlFor="name">Supplier Name*</Label>
                <Input id="name" {...register("name", { required: "Supplier name is required" })} />
                {errors.name && <p className="text-destructive text-sm mt-1">{errors.name.message as string}</p>}
            </div>
            <div>
                <Label htmlFor="contactPerson">Contact Person*</Label>
                <Input id="contactPerson" {...register("contactPerson", { required: "Contact person is required" })} />
                {errors.contactPerson && <p className="text-destructive text-sm mt-1">{errors.contactPerson.message as string}</p>}
            </div>
            <div>
                <Label htmlFor="email">Contact Email</Label>
                <Input id="email" type="email" {...register("email")} />
            </div>
            <div>
                <Label htmlFor="phone">Contact Phone*</Label>
                <Input id="phone" {...register("phone", { required: "Contact phone is required" })} />
                {errors.phone && <p className="text-destructive text-sm mt-1">{errors.phone.message as string}</p>}
            </div>
            <div>
                <Label htmlFor="address">Address*</Label>
                <Input id="address" {...register("address", { required: "Address is required" })} />
                {errors.address && <p className="text-destructive text-sm mt-1">{errors.address.message as string}</p>}
            </div>
            <div>
                <Label htmlFor="city">City / Place of Business*</Label>
                <Input id="city" {...register("city", { required: "City/place is required" })} />
                {errors.city && <p className="text-destructive text-sm mt-1">{errors.city.message as string}</p>}
            </div>
            <div>
                <Label htmlFor="region">Region / State</Label>
                <Input id="region" {...register("region")} />
            </div>
            <div>
                <Label htmlFor="country">Country*</Label>
                <Input id="country" {...register("country", { required: "Country is required" })} />
                {errors.country && <p className="text-destructive text-sm mt-1">{errors.country.message as string}</p>}
            </div>
            
            {/* MRA EIS Compliance Fields */}
            <div className="border-t pt-4 mt-4">
                <h3 className="font-semibold text-sm mb-3">Supplier Identity</h3>
	                <div>
	                    <Label htmlFor="supplierTin">Supplier TIN</Label>
	                    <Input 
	                        id="supplierTin" 
	                        placeholder="e.g., 1234567890"
	                        {...register("supplierTin")} 
	                    />
	                </div>
            </div>
            
            <DialogFooter>
                <Button type="submit">{defaultValues ? 'Save Changes' : 'Add Supplier'}</Button>
            </DialogFooter>
        </form>
    );
};


const SupplierListRow = ({
    supplier,
    onView,
    onEdit,
    onDelete,
    activeBranchId,
    isEisEnabled,
}: {
    supplier: Supplier,
    onView: () => void,
    onEdit: () => void,
    onDelete: () => void,
    activeBranchId: string,
    isEisEnabled: boolean,
}) => {
    const { format: formatCurrency } = useCurrency();
    
    // Get purchase history for this supplier
    const purchaseHistory = useLiveQuery(
        async () => {
            const normalizedActiveBranchId = normalizeBranchId(activeBranchId);
            const allRecords = await db.purchaseHistory.where('supplierId').equals(supplier.id).toArray();
            return allRecords.filter(
                (record) => normalizeBranchId(record.branchId) === normalizedActiveBranchId
            );
        },
        [supplier.id, activeBranchId]
    );
    
    // ✅ Calculate balance due from unpaid orders (paymentStatus === 'Unpaid')
    const balanceDue = purchaseHistory?.filter(p => p.paymentStatus === 'Unpaid').reduce((acc, p) => acc + p.amountDue, 0) || 0;
    const totalPurchases = purchaseHistory?.reduce((acc, p) => acc + p.totalCost, 0) || 0;
    
    return (
        <TableRow>
            <TableCell>
                <button onClick={onView} className="font-medium text-primary hover:underline">
                    {supplier.name}
                </button>
            </TableCell>
            <TableCell>
                <div className="text-sm">
                    {supplier.contactPerson && <p>{supplier.contactPerson}</p>}
                    {supplier.phone && <p>{supplier.phone}</p>}
                    {supplier.email && <p className="text-muted-foreground text-xs">{supplier.email}</p>}
                </div>
            </TableCell>
            <TableCell>{[supplier.city, supplier.region, supplier.country].filter(Boolean).join(', ') || '-'}</TableCell>
            <TableCell className="text-right">
                <Badge variant={balanceDue > 0 ? 'destructive' : 'secondary'}>
                    {formatCurrency(balanceDue)}
                </Badge>
            </TableCell>
            <TableCell className="text-right font-semibold">{formatCurrency(totalPurchases)}</TableCell>
            <TableCell className="text-right">
                <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="icon">
                            <MoreHorizontal className="h-4 w-4" />
                        </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                        <DropdownMenuItem onClick={onView}>
                            <History className="mr-2 h-4 w-4" /> View Details
                        </DropdownMenuItem>
                        {isEisEnabled ? (
                            <DropdownMenuItem disabled>
                                <Badge variant="outline">EIS managed</Badge>
                            </DropdownMenuItem>
                        ) : (
                            <>
                                <DropdownMenuItem onClick={onEdit}>
                                    <Edit className="mr-2 h-4 w-4" /> Edit
                                </DropdownMenuItem>
                                <DropdownMenuItem className="text-destructive" onClick={onDelete}>
                                    <Trash2 className="mr-2 h-4 w-4" /> Delete
                                </DropdownMenuItem>
                            </>
                        )}
                    </DropdownMenuContent>
                </DropdownMenu>
            </TableCell>
        </TableRow>
    );
};


const SupplierDetailDialog = ({ supplier, isOpen, onOpenChange, activeBranchId }: { supplier: Supplier, isOpen: boolean, onOpenChange: (open: boolean) => void, activeBranchId: string }) => {
    const [isPaying, setIsPaying] = useState(false);
    const [isSyncingDetails, setIsSyncingDetails] = useState(false);
    const [isExporting, setIsExporting] = useState(false);
    const [isPurchaseDetailOpen, setIsPurchaseDetailOpen] = useState(false);
    const [selectedPurchase, setSelectedPurchase] = useState<SupplierPurchaseGroup | null>(null);
    const { user, business } = useAuth();
    const { format: formatCurrency } = useCurrency();
    
    // Note: Purchase history is stored locally in purchaseHistory table
    // Backend doesn't have a dedicated purchase-history endpoint
    // Data is synced via the main sync service
    
    const purchaseHistory = useLiveQuery(
        async () => {
            const normalizedActiveBranchId = normalizeBranchId(activeBranchId);
            const allRecords = await db.purchaseHistory.where('supplierId').equals(supplier.id).toArray();
            return allRecords
                .filter((record) => normalizeBranchId(record.branchId) === normalizedActiveBranchId)
                .sort(
                    (a, b) =>
                        dateSortValue(
                            b.receivedDate,
                            (b as any).createdAt,
                            (b as any).updatedAt
                        ) -
                        dateSortValue(
                            a.receivedDate,
                            (a as any).createdAt,
                            (a as any).updatedAt
                        )
                );
        },
        [supplier.id, activeBranchId]
    );

    const associatedProducts = useLiveQuery(
        async () => {
            const normalizedActiveBranchId = normalizeBranchId(activeBranchId);
            const allProducts = await db.inventory.where('supplier').equals(supplier.name).toArray();
            return allProducts.filter(
                (product) => normalizeBranchId(product.branchId) === normalizedActiveBranchId
            );
        },
        [supplier.name, activeBranchId]
    );

    const businessProfile = useLiveQuery(
        async () => {
            if (!business?.id) return undefined;
            return db.business.get(business.id);
        },
        [business?.id]
    );
    
    const amountOwed = purchaseHistory?.filter(p => p.paymentStatus === 'Unpaid').reduce((acc, p) => acc + p.amountDue, 0) || 0;
    const purchaseGroups = useMemo<SupplierPurchaseGroup[]>(() => {
        if (!purchaseHistory) return [];
        const groups: Record<string, SupplierPurchaseGroup> = {};

        const resolveGroupStatus = (statuses: string[]): string => {
            if (statuses.includes('Unpaid')) return 'Unpaid';
            if (statuses.includes('Pending')) return 'Pending';
            if (statuses.includes('Partial')) return 'Partial';
            if (statuses.includes('Credit')) return 'Credit';
            return 'Paid';
        };

        purchaseHistory.forEach((record) => {
            const groupId = record.purchaseOrderId || `${record.receivedDate}-${record.supplierId}`;
            const sortValue = dateSortValue(
                record.receivedDate,
                (record as any).createdAt,
                (record as any).updatedAt
            );
            if (!groups[groupId]) {
                groups[groupId] = {
                    groupId,
                    supplierName: supplier.name,
                    displayDate: formatDisplayDate(
                        record.receivedDate,
                        (record as any).createdAt,
                        (record as any).updatedAt
                    ),
                    dateSortValue: sortValue,
                    createdDateDisplay: formatDisplayDate((record as any).createdAt),
                    createdDateSortValue: dateSortValue((record as any).createdAt),
                    paymentStatus: record.paymentStatus,
                    totalCost: 0,
                    totalQuantity: 0,
                    amountDue: 0,
                    totalVat: 0,
                    totalWithVat: 0,
                    referenceNumber: record.referenceNumber,
                    vatAmount: record.vatAmount,
                    items: [],
                };
            }

            const itemVat = resolveRecordVat(record);
            const itemGross = resolveRecordGross(record, itemVat);
            groups[groupId].items.push(record);
            groups[groupId].totalCost += record.totalCost || 0;
            groups[groupId].totalVat += itemVat;
            groups[groupId].totalWithVat += itemGross;
            groups[groupId].totalQuantity += record.quantityReceived || 0;
            groups[groupId].amountDue += record.amountDue || 0;
            groups[groupId].paymentStatus = resolveGroupStatus([
                groups[groupId].paymentStatus,
                record.paymentStatus,
            ].filter(Boolean));

            if (!groups[groupId].referenceNumber && record.referenceNumber) {
                groups[groupId].referenceNumber = record.referenceNumber;
            }
            if (groups[groupId].vatAmount === undefined && record.vatAmount !== undefined) {
                groups[groupId].vatAmount = record.vatAmount;
            }

            if (sortValue > groups[groupId].dateSortValue) {
                groups[groupId].dateSortValue = sortValue;
                groups[groupId].displayDate = formatDisplayDate(
                    record.receivedDate,
                    (record as any).createdAt,
                    (record as any).updatedAt
                );
            }

            const createdSortValue = dateSortValue((record as any).createdAt);
            if (
                createdSortValue > 0 &&
                (groups[groupId].createdDateSortValue === 0 || createdSortValue < groups[groupId].createdDateSortValue)
            ) {
                groups[groupId].createdDateSortValue = createdSortValue;
                groups[groupId].createdDateDisplay = formatDisplayDate((record as any).createdAt);
            }
        });

        return Object.values(groups).sort((a, b) => b.dateSortValue - a.dateSortValue);
    }, [purchaseHistory]);

    const handleViewPurchaseDetails = (group: SupplierPurchaseGroup) => {
        setSelectedPurchase(group);
        setIsPurchaseDetailOpen(true);
    };

    const handlePurchaseDetailOpenChange = (open: boolean) => {
        setIsPurchaseDetailOpen(open);
        if (!open) {
            setSelectedPurchase(null);
        }
    };

    const buildPurchaseDetailHtml = (group: SupplierPurchaseGroup) => {
        const escapeHtml = (value: string) =>
            value
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');

        const vatTotal =
            typeof group.totalVat === 'number' && group.totalVat > 0
                ? group.totalVat
                : group.vatAmount ?? 0;
        const subtotal = group.totalWithVat - vatTotal;
        const itemsRows = group.items
            .map((item) => {
                const itemVat = resolveRecordVat(item);
                const vatMethod = resolveTaxMethod(item.taxCalculationMethod);
                const vatLabel = vatMethod === 'inclusive' ? 'Incl' : 'Excl';
                return `
                <tr>
                    <td>${escapeHtml(item.productName || '')}</td>
                    <td class="right">${item.quantityReceived}</td>
                    <td class="right">${formatCurrency(item.costPerUnit || 0)}</td>
                    <td class="right">${formatCurrency(item.totalCost || 0)}</td>
                    <td class="right">${formatCurrency(itemVat || 0)} (${vatLabel})</td>
                    <td>${escapeHtml(item.batchNumber || 'N/A')}</td>
                    <td>${escapeHtml(formatDisplayDate(item.expiryDate))}</td>
                </tr>
            `;
            })
            .join('');

        return `
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8" />
                <title>Purchase Details</title>
                <style>
                    * { box-sizing: border-box; }
                    body { font-family: Arial, sans-serif; color: #111; margin: 24px; }
                    h1 { font-size: 20px; margin-bottom: 4px; }
                    .subheading { font-size: 12px; color: #555; margin-bottom: 16px; }
                    .purchase-header { display: grid; grid-template-columns: repeat(6, minmax(140px, 1fr)); gap: 12px; margin-bottom: 12px; font-size: 12px; }
                    .label { font-size: 10px; text-transform: uppercase; color: #777; }
                    .value { font-weight: 600; }
                    table { width: 100%; border-collapse: collapse; font-size: 11px; }
                    th, td { padding: 6px 8px; border-bottom: 1px solid #eee; }
                    th { text-align: left; background: #f5f5f5; }
                    .right { text-align: right; }
                </style>
            </head>
            <body>
                <h1>Purchase Details</h1>
                <div class="subheading">${escapeHtml(supplier.name)} · ${escapeHtml(group.displayDate)}</div>
                <div class="purchase-header">
                    <div>
                        <div class="label">Reference</div>
                        <div class="value">${escapeHtml(group.referenceNumber || 'N/A')}</div>
                    </div>
                    <div>
                        <div class="label">Payment Status</div>
                        <div class="value">${escapeHtml(group.paymentStatus)}</div>
                    </div>
                    <div>
                        <div class="label">Subtotal (Excl VAT)</div>
                        <div class="value">${formatCurrency(subtotal)}</div>
                    </div>
                    <div>
                        <div class="label">VAT</div>
                        <div class="value">${formatCurrency(vatTotal)}</div>
                    </div>
                    <div>
                        <div class="label">Total (Incl VAT)</div>
                        <div class="value">${formatCurrency(group.totalWithVat)}</div>
                    </div>
                    <div>
                        <div class="label">Amount Due</div>
                        <div class="value">${formatCurrency(group.amountDue)}</div>
                    </div>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Product</th>
                            <th class="right">Qty</th>
                            <th class="right">Cost/Unit</th>
                            <th class="right">Total</th>
                            <th class="right">VAT (Incl/Excl)</th>
                            <th>Batch</th>
                            <th>Expiry</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${itemsRows || '<tr><td colspan="7">No items available.</td></tr>'}
                    </tbody>
                </table>
            </body>
            </html>
        `;
    };

    const handlePrintPurchaseDetails = () => {
        if (!selectedPurchase) {
            toast({ title: 'Select a purchase', description: 'Choose a purchase to print.' });
            return;
        }

        const html = buildPurchaseDetailHtml(selectedPurchase);
        const printWindow = window.open('', '_blank', 'width=900,height=700');
        if (!printWindow) {
            toast({ variant: 'destructive', title: 'Popup blocked', description: 'Allow popups to print.' });
            return;
        }

        printWindow.document.write(html);
        printWindow.document.close();
        printWindow.focus();
        printWindow.print();
    };

    const handleDownloadPurchasePdf = async () => {
        if (!selectedPurchase) {
            toast({ title: 'Select a purchase', description: 'Choose a purchase to export.' });
            return;
        }

        setIsExporting(true);
        try {
            const vatTotal =
                typeof selectedPurchase.totalVat === 'number' && selectedPurchase.totalVat > 0
                    ? selectedPurchase.totalVat
                    : selectedPurchase.vatAmount ?? 0;

            await generatePurchaseInvoicePDF({
                purchase: {
                    ...selectedPurchase,
                    totalVat: vatTotal,
                },
                business: {
                    name: businessProfile?.name || business?.name || 'Business',
                    address: businessProfile?.address,
                    phone: businessProfile?.phone,
                    email: businessProfile?.email,
                    tin: businessProfile?.tin,
                },
                currencyCode: businessProfile?.currency || business?.currency || 'USD',
            });
            toast({ title: 'Invoice downloaded', description: 'Purchase invoice PDF was downloaded successfully.' });
        } catch (error) {
            console.error('[Supplier Detail] Failed to export purchase invoice PDF:', error);
            toast({ variant: 'destructive', title: 'Download failed', description: 'Could not generate the purchase invoice PDF. Please try again.' });
        } finally {
            setIsExporting(false);
        }
    };

    const handleRecordPayment = async () => {
        setIsPaying(true);
        try {
            const unpaidPurchases = purchaseHistory?.filter(p => p.paymentStatus === 'Unpaid') || [];
            if (unpaidPurchases.length) {
                // Update purchase history records
                await db.purchaseHistory.bulkUpdate(unpaidPurchases.map(record => ({
                    key: record.id!,
                    changes: { 
                        paymentStatus: 'Paid', 
                        amountDue: 0,
                        _dirty: true,
                        _operation: 'update' as const
                    }
                })));

                // Log audit action
                if (user) {
                    await logAuditAction({
                        userId: user.uid,
                        userName: user.displayName || user.email || 'Unknown',
                        branchId: localStorage.getItem('handypos-active-branch') || 'default',
                        actionType: 'STOCK_RECEIVE',
                        entityType: 'Purchase',
                        entityId: supplier.id,
                        details: { 
                            supplierId: supplier.id,
                            supplierName: supplier.name,
                            recordsUpdated: unpaidPurchases.length,
                            totalAmountPaid: amountOwed
                        },
                    });
                }

                // Trigger sync
                const branchId = localStorage.getItem('handypos-active-branch') || 'default';
                syncService.performFullSync(branchId).catch(err => 
                    console.error('[Supplier Detail] Sync failed:', err)
                );
            }
            toast({ title: 'Payment Recorded', description: `Cleared outstanding balance for ${supplier.name}.`});
        } catch(e) {
            console.error(e);
            toast({ variant: 'destructive', title: 'Failed to record payment' });
        } finally {
            setIsPaying(false);
        }
    };


    return (
        <Dialog open={isOpen} onOpenChange={onOpenChange}>
            <DialogContent className="max-h-[calc(100vh-2rem)] max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-4xl">
                <DialogHeader>
                    <DialogTitle>{supplier.name}</DialogTitle>
                    <DialogDescription>
                        {supplier.email || supplier.phone
                            ? [supplier.email, supplier.phone].filter(Boolean).join(' · ')
                            : 'Supplier details and purchase history.'}
                    </DialogDescription>
                </DialogHeader>
                <Tabs defaultValue="account" className="min-w-0">
                    <div className="-mx-1 overflow-x-auto px-1 pb-1">
                        <TabsList className="inline-flex h-auto min-w-max gap-1 sm:grid sm:w-full sm:min-w-0 sm:grid-cols-4">
                            <TabsTrigger value="account" className="shrink-0 whitespace-nowrap px-3 py-2 text-xs sm:text-sm">Account</TabsTrigger>
                            <TabsTrigger value="history" className="shrink-0 whitespace-nowrap px-3 py-2 text-xs sm:text-sm">Purchases</TabsTrigger>
                            <TabsTrigger value="products" className="shrink-0 whitespace-nowrap px-3 py-2 text-xs sm:text-sm">Products</TabsTrigger>
                            <TabsTrigger value="details" className="shrink-0 whitespace-nowrap px-3 py-2 text-xs sm:text-sm">Details</TabsTrigger>
                        </TabsList>
                    </div>
                     <TabsContent value="account">
                        <Card>
                            <CardHeader>
                                <CardTitle>Account Balance</CardTitle>
                                <CardDescription>Manage outstanding payments to {supplier.name}.</CardDescription>
                            </CardHeader>
                            <CardContent>
                                <div className="flex flex-col items-center justify-center space-y-4 rounded-lg bg-muted/50 p-8">
                                    <p className="text-sm font-medium text-muted-foreground">Total Amount Owed</p>
                                    <p className="text-4xl font-bold tracking-tight">{formatCurrency(amountOwed)}</p>
                                    <Button onClick={handleRecordPayment} disabled={amountOwed === 0 || isPaying}>
                                        {isPaying ? <Loader2 className="mr-2 animate-spin" /> : <DollarSign className="mr-2" />}
                                        Record Full Payment
                                    </Button>
                                </div>
                            </CardContent>
                        </Card>
                    </TabsContent>
                    <TabsContent value="history">
                        <Card>
                            <CardHeader>
                                <div>
                                    <CardTitle>Purchase History</CardTitle>
                                    <CardDescription>Purchase summary for {supplier.name}.</CardDescription>
                                </div>
                            </CardHeader>
                            <CardContent>
                                {purchaseGroups.length === 0 ? (
                                    <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
                                        No purchase history available for this supplier.
                                    </div>
                                ) : (
                                    <div className="max-h-[500px] overflow-y-auto">
                                        <div className="overflow-x-auto">
                                            <Table>
                                                <TableHeader>
                                                    <TableRow>
                                                        <TableHead>Purchase Date</TableHead>
                                                        <TableHead>Created Date</TableHead>
                                                        <TableHead>Reference</TableHead>
                                                        <TableHead>Payment</TableHead>
                                                        <TableHead className="text-right">Subtotal (Excl VAT)</TableHead>
                                                        <TableHead className="text-right">VAT</TableHead>
                                                        <TableHead className="text-right">Total (Incl VAT)</TableHead>
                                                        <TableHead className="text-right">Amount Due</TableHead>
                                                        <TableHead className="text-right">Action</TableHead>
                                                    </TableRow>
                                                </TableHeader>
                                                <TableBody>
                                                    {purchaseGroups.map((group) => {
                                                        const vatTotal =
                                                            typeof group.totalVat === 'number' && group.totalVat > 0
                                                                ? group.totalVat
                                                                : group.vatAmount ?? 0;
                                                        const subtotal = group.totalWithVat - vatTotal;

                                                        return (
                                                            <TableRow key={group.groupId}>
                                                                <TableCell>{group.displayDate}</TableCell>
                                                                <TableCell>{group.createdDateDisplay}</TableCell>
                                                                <TableCell>{group.referenceNumber || 'N/A'}</TableCell>
                                                                <TableCell>
                                                                    <Badge
                                                                        variant={
                                                                            group.paymentStatus === 'Paid'
                                                                                ? 'secondary'
                                                                                : group.paymentStatus === 'Unpaid'
                                                                                ? 'destructive'
                                                                                : 'outline'
                                                                        }
                                                                    >
                                                                        {group.paymentStatus}
                                                                    </Badge>
                                                                </TableCell>
                                                                <TableCell className="text-right">{formatCurrency(subtotal)}</TableCell>
                                                                <TableCell className="text-right">{formatCurrency(vatTotal)}</TableCell>
                                                                <TableCell className="text-right">{formatCurrency(group.totalWithVat)}</TableCell>
                                                                <TableCell className="text-right">{formatCurrency(group.amountDue)}</TableCell>
                                                                <TableCell className="text-right">
                                                                    <Button variant="ghost" size="sm" onClick={() => handleViewPurchaseDetails(group)}>
                                                                        Details
                                                                    </Button>
                                                                </TableCell>
                                                            </TableRow>
                                                        );
                                                    })}
                                                </TableBody>
                                            </Table>
                                        </div>
                                    </div>
                                )}
                            </CardContent>
                        </Card>
                    </TabsContent>
                    <TabsContent value="products">
                        <Card>
                            <CardHeader>
                                <CardTitle>Associated Products</CardTitle>
                                <CardDescription>Products sourced from {supplier.name}.</CardDescription>
                            </CardHeader>
                             <CardContent>
                                <div className="overflow-x-auto max-h-[400px]">
                                    <Table>
                                        <TableHeader>
                                            <TableRow>
                                                <TableHead>Product</TableHead>
                                                <TableHead>Manufacturer</TableHead>
                                                <TableHead>Category</TableHead>
                                                <TableHead className="text-right">Current Stock</TableHead>
                                            </TableRow>
                                        </TableHeader>
                                        <TableBody>
                                            {associatedProducts?.map((product) => (
                                                <TableRow key={product.id}>
                                                    <TableCell className="font-medium">{product.name}</TableCell>
                                                    <TableCell>{product.manufacturer}</TableCell>
                                                    <TableCell>{product.category}</TableCell>
                                                    <TableCell className="text-right">{product.stockUnits} {product.unitType}</TableCell>
                                                </TableRow>
                                            ))}
                                        </TableBody>
                                    </Table>
                                </div>
                            </CardContent>
                        </Card>
                    </TabsContent>
                    <TabsContent value="details">
                         <Card>
                            <CardHeader><CardTitle>Supplier Information</CardTitle></CardHeader>
                            <CardContent>
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                    <div>
                                        <p className="text-sm font-medium text-muted-foreground">Supplier Name</p>
                                        <p className="text-lg font-semibold mt-1">{supplier.name}</p>
                                    </div>
                                    <div>
                                        <p className="text-sm font-medium text-muted-foreground">Contact Person</p>
                                        <p className="text-lg mt-1">{supplier.contactPerson || 'Not provided'}</p>
                                    </div>
                                    <div>
                                        <p className="text-sm font-medium text-muted-foreground">Contact Email</p>
                                        <p className="text-lg mt-1">{supplier.email || 'Not provided'}</p>
                                    </div>
                                    <div>
                                        <p className="text-sm font-medium text-muted-foreground">Contact Phone</p>
                                        <p className="text-lg mt-1">{supplier.phone || 'Not provided'}</p>
                                    </div>
                                    <div>
                                        <p className="text-sm font-medium text-muted-foreground">City / Place of Business</p>
                                        <p className="text-lg mt-1">{supplier.city || 'Not provided'}</p>
                                    </div>
                                    <div>
                                        <p className="text-sm font-medium text-muted-foreground">Region / State</p>
                                        <p className="text-lg mt-1">{supplier.region || 'Not provided'}</p>
                                    </div>
                                    <div>
                                        <p className="text-sm font-medium text-muted-foreground">Country</p>
                                        <p className="text-lg mt-1">{supplier.country || 'Not provided'}</p>
                                    </div>
                                    <div className="md:col-span-2">
                                        <p className="text-sm font-medium text-muted-foreground">Address</p>
                                        <p className="text-lg mt-1">{supplier.address || 'Not provided'}</p>
                                    </div>
                                    
                                    {/* MRA EIS Compliance Fields */}
                                    <div className="md:col-span-2 border-t pt-6 mt-6">
                                        <h3 className="font-semibold text-sm mb-4">MRA EIS Compliance Information</h3>
                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                            <div>
                                                <p className="text-sm font-medium text-muted-foreground">Supplier TIN</p>
                                                <p className="text-lg mt-1 font-mono">
                                                    {supplier.supplierTin ? (
                                                        <Badge variant="outline">{supplier.supplierTin}</Badge>
                                                    ) : (
                                                        <span className="text-muted-foreground">Not provided</span>
                                                    )}
                                                </p>
                                            </div>
	                                            <div>
	                                                <p className="text-sm font-medium text-muted-foreground">VAT Registration Status</p>
	                                                <p className="text-lg mt-1">
	                                                    {supplier.vatRegistered ? (
	                                                        <Badge variant="secondary">VAT Registered</Badge>
	                                                    ) : (
	                                                        <Badge variant="outline">Not VAT Registered</Badge>
	                                                    )}
	                                                </p>
	                                            </div>
	                                            <div>
	                                                <p className="text-sm font-medium text-muted-foreground">EIS Supplier ID</p>
	                                                <p className="text-lg mt-1 font-mono">
	                                                    {supplier.mraSupplierId ? (
	                                                        <Badge variant="outline">{supplier.mraSupplierId}</Badge>
	                                                    ) : (
	                                                        <span className="text-muted-foreground">Not provided</span>
	                                                    )}
	                                                </p>
	                                            </div>
	                                        </div>
	                                    </div>
                                </div>
                            </CardContent>
                        </Card>
                    </TabsContent>
                </Tabs>
                <Dialog open={isPurchaseDetailOpen} onOpenChange={handlePurchaseDetailOpenChange}>
                    <DialogContent className="sm:max-w-4xl max-h-[90vh] flex flex-col">
                        <DialogHeader className="space-y-3">
                            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                                <div>
                                    <DialogTitle>Purchase Details</DialogTitle>
                                    <DialogDescription>
                                        {selectedPurchase ? `${supplier.name} · ${selectedPurchase.displayDate}` : ''}
                                    </DialogDescription>
                                </div>
                                <div className="flex flex-wrap gap-2">
                                    <Button variant="outline" onClick={handlePrintPurchaseDetails} disabled={!selectedPurchase}>
                                        <Printer className="mr-2 h-4 w-4" /> Print
                                    </Button>
                                    <Button
                                        variant="outline"
                                        onClick={handleDownloadPurchasePdf}
                                        disabled={!selectedPurchase || isExporting}
                                    >
                                        {isExporting ? (
                                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                        ) : (
                                            <Download className="mr-2 h-4 w-4" />
                                        )}
                                        Download Invoice PDF
                                    </Button>
                                </div>
                            </div>
                        </DialogHeader>

                        {selectedPurchase && (() => {
                            const vatTotal =
                                typeof selectedPurchase.totalVat === 'number' && selectedPurchase.totalVat > 0
                                    ? selectedPurchase.totalVat
                                    : selectedPurchase.vatAmount ?? 0;
                            const subtotal = selectedPurchase.totalWithVat - vatTotal;

                            return (
                                <div className="flex-1 overflow-y-auto -mx-6 px-6">
                                    <div className="grid grid-cols-2 gap-4 mb-6 rounded-lg bg-muted p-4 sm:grid-cols-4">
                                        <div>
                                            <p className="text-xs text-muted-foreground">Purchase Date</p>
                                            <p className="font-semibold">{selectedPurchase.displayDate}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted-foreground">Created Date</p>
                                            <p className="font-semibold">{selectedPurchase.createdDateDisplay}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted-foreground">Reference</p>
                                            <p className="font-semibold">{selectedPurchase.referenceNumber || 'N/A'}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted-foreground">Payment Status</p>
                                            <Badge
                                                variant={
                                                    selectedPurchase.paymentStatus === 'Paid'
                                                        ? 'secondary'
                                                        : selectedPurchase.paymentStatus === 'Unpaid'
                                                        ? 'destructive'
                                                        : 'outline'
                                                }
                                            >
                                                {selectedPurchase.paymentStatus}
                                            </Badge>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted-foreground">Subtotal (Excl VAT)</p>
                                            <p className="font-semibold">{formatCurrency(subtotal)}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted-foreground">VAT</p>
                                            <p className="font-semibold">{formatCurrency(vatTotal)}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted-foreground">Total (Incl VAT)</p>
                                            <p className="font-semibold">{formatCurrency(selectedPurchase.totalWithVat)}</p>
                                        </div>
                                        <div>
                                            <p className="text-xs text-muted-foreground">Amount Due</p>
                                            <p className="font-semibold">{formatCurrency(selectedPurchase.amountDue)}</p>
                                        </div>
                                    </div>

                                    <div className="mb-4">
                                        <h3 className="font-semibold mb-3">Items in this Purchase</h3>
                                        <div className="overflow-x-auto">
                                            <Table>
                                                <TableHeader>
                                                    <TableRow>
                                                        <TableHead>Product</TableHead>
                                                        <TableHead className="text-right">Qty</TableHead>
                                                        <TableHead className="text-right">Cost/Unit</TableHead>
                                                        <TableHead className="text-right">Total</TableHead>
                                                        <TableHead className="text-right">VAT (Incl/Excl)</TableHead>
                                                        <TableHead>Batch</TableHead>
                                                        <TableHead>Expiry</TableHead>
                                                    </TableRow>
                                                </TableHeader>
                                                <TableBody>
                                                    {selectedPurchase.items.map((record) => (
                                                        <TableRow key={record.id}>
                                                            <TableCell className="font-medium">{record.productName}</TableCell>
                                                            <TableCell className="text-right">{record.quantityReceived}</TableCell>
                                                            <TableCell className="text-right">{formatCurrency(record.costPerUnit)}</TableCell>
                                                            <TableCell className="text-right">{formatCurrency(record.totalCost)}</TableCell>
                                                            <TableCell className="text-right">
                                                                {formatCurrency(resolveRecordVat(record))}{' '}
                                                                ({resolveTaxMethod(record.taxCalculationMethod) === 'inclusive' ? 'Incl' : 'Excl'})
                                                            </TableCell>
                                                            <TableCell>{record.batchNumber || 'N/A'}</TableCell>
                                                            <TableCell>{formatDisplayDate(record.expiryDate)}</TableCell>
                                                        </TableRow>
                                                    ))}
                                                </TableBody>
                                            </Table>
                                        </div>
                                    </div>
                                </div>
                            );
                        })()}
                    </DialogContent>
                </Dialog>
            </DialogContent>
        </Dialog>
    );
};


export default function SuppliersPage() {
  const { user, business } = useAuth();
  const [isAddFormOpen, setAddFormOpen] = useState(false);
  const [editingSupplier, setEditingSupplier] = useState<Supplier | undefined>(undefined);
  const [viewingSupplier, setViewingSupplier] = useState<Supplier | undefined>(undefined);
  const [activeBranchId, setActiveBranchId] = useState<string>('default');
  const [businessId, setBusinessId] = useState<string>('');
  const [isSyncing, setIsSyncing] = useState(false);
  const [isMraSupplierSyncing, setIsMraSupplierSyncing] = useState(false);
  const isEisEnabled = useMemo(() => resolveCachedEisEnabled(business), [business]);

  // Get active branch ID and business ID from auth context
  React.useEffect(() => {
    if (typeof window !== 'undefined') {
      const branchId = localStorage.getItem('handypos-active-branch') || 'default';
      setActiveBranchId(branchId);
      
      const resolvedBusinessId =
        String(user?.businessId || business?.id || resolveStoredBusinessId() || '').trim();

      if (resolvedBusinessId) {
        console.log('[SuppliersPage] Mount - resolved businessId:', resolvedBusinessId);
        setBusinessId(resolvedBusinessId);
        console.log('[SuppliersPage] Initialized with business:', resolvedBusinessId, 'branch:', branchId);
      } else {
        console.warn('[SuppliersPage] No businessId found in auth context/storage');
      }
    }
  }, [user, business]);

  // Fetch suppliers from backend and sync with local database
  React.useEffect(() => {
    const syncSuppliersFromBackend = async () => {
      if (!businessId) {
        console.log('[SuppliersPage] No business ID, skipping backend sync');
        return;
      }

      setIsSyncing(true);
      try {
        // Repair legacy local records that were created without business context.
        const orphanSuppliers = await db.suppliers
          .toArray()
          .then(items => items.filter(s => !s.businessId || String(s.businessId).trim() === ''));
        if (orphanSuppliers.length > 0) {
          console.log('[SuppliersPage] Repairing orphan suppliers with missing businessId:', orphanSuppliers.length);
          for (const supplier of orphanSuppliers) {
            await db.suppliers.update(supplier.id, { businessId });
          }
        }

        const url = `/inventory/suppliers/?business_id=${businessId}`;
        console.log('[SuppliersPage] ===== STARTING SUPPLIER FETCH =====');
        console.log('[SuppliersPage] Business ID:', businessId);
        console.log('[SuppliersPage] Full URL:', url);
        console.log('[SuppliersPage] Timestamp:', new Date().toISOString());
        
        // Fetch suppliers directly from backend API with business_id parameter
        console.log('[SuppliersPage] About to call authFetch.fetch()...');
        const backendSuppliers = await authFetch.fetch<any>(url);
        console.log('[SuppliersPage] ===== FETCH COMPLETED =====');
        console.log('[SuppliersPage] Backend suppliers response:', backendSuppliers);
        console.log('[SuppliersPage] Response type:', typeof backendSuppliers);
        console.log('[SuppliersPage] Response keys:', backendSuppliers ? Object.keys(backendSuppliers) : 'null/undefined');
        
        // Check if response is null or undefined
        if (!backendSuppliers) {
          console.warn('[SuppliersPage] Backend returned null/undefined response');
          setIsSyncing(false);
          return;
        }
        
        // Handle different response formats
        let suppliersData: any[] = [];
        if (Array.isArray(backendSuppliers)) {
          suppliersData = backendSuppliers;
        } else if (backendSuppliers?.results && Array.isArray(backendSuppliers.results)) {
          suppliersData = backendSuppliers.results;
        } else if (backendSuppliers) {
          console.warn('[SuppliersPage] Unexpected backend response format:', backendSuppliers);
          suppliersData = [];
        }

        console.log('[SuppliersPage] Processing', suppliersData.length, 'suppliers from backend');

        // Get current local suppliers for this business
        const localSuppliers = await db.suppliers
          .toArray()
          .then(items => items.filter(s => String(s.businessId || '').trim() === businessId));
        const localSupplierIds = new Set(localSuppliers.map(s => s.id));
        const backendSupplierIds = new Set(suppliersData.map(s => s.id));

        // Update or create suppliers from backend
        for (const backendSupplier of suppliersData) {
          const mappedSupplier: Supplier = {
            id: backendSupplier.id,
            businessId: businessId,
            name: backendSupplier.name,
            contactPerson: backendSupplier.contact_person || backendSupplier.contactPerson || '',
            email: backendSupplier.email || '',
            phone: backendSupplier.phone || '',
            address: backendSupplier.address || '',
            city: backendSupplier.city || '',
            region: backendSupplier.region || backendSupplier.region_state || backendSupplier.regionState || backendSupplier.state || '',
            country: backendSupplier.country || '',
	            // MRA EIS Compliance Fields
	            supplierTin: backendSupplier.supplier_tin || '',
	            mraSupplierId: backendSupplier.mra_supplier_id || undefined,
	            vatRegistered: backendSupplier.vat_registered || false,
          };

          try {
            // Use put to insert or update (upsert)
            await db.suppliers.put(mappedSupplier);
            console.log('[SuppliersPage] Upserted supplier:', backendSupplier.id);
          } catch (error) {
            console.error('[SuppliersPage] Error upserting supplier:', backendSupplier.id, error);
          }
        }

        // Delete suppliers that no longer exist on backend
        for (const localSupplierId of localSupplierIds) {
          if (!backendSupplierIds.has(localSupplierId)) {
            const localSupplier = localSuppliers.find(s => s.id === localSupplierId);
            // Keep unsynced local records to avoid dropping newly created suppliers.
            if (localSupplier?._dirty) {
              console.log('[SuppliersPage] Keeping dirty local supplier not yet on backend:', localSupplierId);
              continue;
            }
            await db.suppliers.delete(localSupplierId);
            console.log('[SuppliersPage] Deleted supplier:', localSupplierId);
          }
        }

        console.log('[SuppliersPage] Suppliers sync completed successfully');
      } catch (error) {
        console.error('[SuppliersPage] Error syncing suppliers from backend:', error);
        // Silently fail - will use local data
      } finally {
        setIsSyncing(false);
      }
    };

    syncSuppliersFromBackend();
  }, [businessId]);

  // Listen for branch changes from header (custom event)
  React.useEffect(() => {
    const handleBranchChange = (e: Event) => {
      const customEvent = e as CustomEvent;
      const branchId = customEvent.detail?.branchId;
      if (branchId) {
        console.log('[SuppliersPage] Branch changed to:', branchId);
        setActiveBranchId(branchId);
      }
    };

    window.addEventListener('branchChanged', handleBranchChange);
    return () => window.removeEventListener('branchChanged', handleBranchChange);
  }, []);

  // Query suppliers filtered by current business
  const suppliers = useLiveQuery(() => {
    if (!businessId) {
      console.log('[SuppliersPage] No business ID, returning empty suppliers');
      return Promise.resolve([]);
    }
    console.log('[SuppliersPage] Querying suppliers for business:', businessId);
    return db.suppliers.toArray().then(all => {
      const data = all.filter(s => String(s.businessId || '').trim() === businessId);
      console.log('[SuppliersPage] Query result:', data.length, 'suppliers for business', businessId);
      return data;
    });
  }, [businessId]);

  const handleEdit = (supplier: Supplier) => {
    if (isEisEnabled) {
      toast({ title: 'Suppliers are EIS managed', description: 'Use Sync EIS Suppliers.' });
      return;
    }
    setEditingSupplier(supplier);
    setAddFormOpen(true);
  };
  
  const handleDelete = async (id: string) => {
    if (isEisEnabled) {
      toast({ title: 'Suppliers are EIS managed', description: 'Use Sync EIS Suppliers.' });
      return;
    }

    if (confirm('Are you sure you want to delete this supplier? This action cannot be undone.')) {
        if (user) {
          await deleteSupplier(
            id,
            user.uid,
            user.displayName || user.email || 'Unknown',
            activeBranchId
          );
        }
    }
  };


  const handleSyncMraSuppliers = async () => {
    if (!businessId) {
      toast({
        variant: 'destructive',
        title: 'Business context missing',
        description: 'Please refresh and try again.',
      });
      return;
    }

    setIsMraSupplierSyncing(true);
    try {
      const params = new URLSearchParams({ business_id: businessId });
      const response = await authFetch.fetch<any>(`/inventory/suppliers/sync-from-mra/?${params.toString()}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
        timeoutMs: 30000,
      });

      const syncedSuppliers = Array.isArray(response?.suppliers) ? response.suppliers : [];
      for (const backendSupplier of syncedSuppliers) {
        const mappedSupplier: Supplier = {
          id: backendSupplier.id,
          businessId,
          name: backendSupplier.name,
          contactPerson: backendSupplier.contact_person || backendSupplier.contactPerson || '',
          email: backendSupplier.email || '',
          phone: backendSupplier.phone || '',
          address: backendSupplier.address || '',
          city: backendSupplier.city || '',
          region: backendSupplier.region || backendSupplier.region_state || backendSupplier.regionState || backendSupplier.state || '',
          country: backendSupplier.country || '',
          supplierTin: backendSupplier.supplier_tin || '',
          mraSupplierId: backendSupplier.mra_supplier_id || undefined,
          vatRegistered: backendSupplier.vat_registered || false,
        };
        await db.suppliers.put(mappedSupplier);
      }

      toast({
        title: 'EIS suppliers synced',
        description: `${response?.fetched ?? syncedSuppliers.length} fetched, ${response?.created ?? 0} created, ${response?.updated ?? 0} updated.`,
      });
    } catch (error: any) {
      console.error('[SuppliersPage] Failed to sync MRA suppliers:', error);
      toast({
        variant: 'destructive',
        title: 'EIS supplier sync failed',
        description: error?.message || 'Could not fetch suppliers from MRA.',
      });
    } finally {
      setIsMraSupplierSyncing(false);
    }
  };
  
  const handleAddFormOpenChange = (open: boolean) => {
    setAddFormOpen(open);
    if (!open) {
        setEditingSupplier(undefined);
    }
  }
  
  const handleDetailViewOpenChange = (open: boolean) => {
      if (!open) {
          setViewingSupplier(undefined);
      }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Suppliers</h1>
          <p className="text-muted-foreground">
            {isEisEnabled
              ? 'Suppliers are managed by MRA EIS.'
              : 'Manage your product suppliers and their information.'}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:justify-end">
          {isEisEnabled && <Badge variant="outline">EIS managed</Badge>}
          <Button
            type="button"
            variant="outline"
            onClick={handleSyncMraSuppliers}
            disabled={!businessId || isSyncing || isMraSupplierSyncing}
          >
            {isMraSupplierSyncing ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <RefreshCw className="mr-2 h-4 w-4" />
            )}
            Sync EIS Suppliers
          </Button>
          {!isEisEnabled && (
            <Dialog open={isAddFormOpen} onOpenChange={handleAddFormOpenChange}>
              <DialogTrigger asChild>
                  <Button>
                      <PlusCircle className="mr-2 h-4 w-4" /> Add Supplier
                  </Button>
              </DialogTrigger>
              <DialogContent className="sm:max-w-[425px] max-h-[90vh] flex flex-col">
                  <DialogHeader>
                      <DialogTitle>{editingSupplier ? 'Edit Supplier' : 'Add New Supplier'}</DialogTitle>
                      <DialogDescription>
                          {editingSupplier ? 'Update the details for this supplier.' : 'Add a new supplier to your list.'}
                      </DialogDescription>
                  </DialogHeader>
                  <div className="overflow-y-auto flex-1 pr-4">
                      <SupplierForm
                          onFormSubmit={() => handleAddFormOpenChange(false)}
                          defaultValues={editingSupplier}
                          activeBranchId={activeBranchId}
                          activeBusinessId={businessId}
                          isEisEnabled={isEisEnabled}
                      />
                  </div>
              </DialogContent>
            </Dialog>
          )}
        </div>
      </div>
      
      <Card>
        <CardHeader>
          <CardTitle>Supplier List</CardTitle>
          <CardDescription>A list of all suppliers in your system. Click a name to see details.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Supplier Name</TableHead>
                <TableHead>Contact</TableHead>
                <TableHead>Place</TableHead>
                <TableHead className="text-right">Balance Due</TableHead>
                <TableHead className="text-right">Total Purchases</TableHead>
                <TableHead className="w-[50px] text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {suppliers?.map((supplier) => (
                <SupplierListRow 
                  key={supplier.id} 
                  supplier={supplier}
                  onView={() => setViewingSupplier(supplier)}
                  onEdit={() => handleEdit(supplier)}
                  onDelete={() => handleDelete(supplier.id)}
                  activeBranchId={activeBranchId}
                  isEisEnabled={isEisEnabled}
                />
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

        {viewingSupplier && (
            <SupplierDetailDialog 
                supplier={viewingSupplier} 
                isOpen={!!viewingSupplier}
                onOpenChange={handleDetailViewOpenChange}
                activeBranchId={activeBranchId}
            />
        )}
    </div>
  );
}
