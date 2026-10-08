'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import Papa from 'papaparse';
import { useLiveQuery } from 'dexie-react-hooks';
import {
  MoreHorizontal,
  PlusCircle,
  Upload,
  Download,
  Loader2,
  RefreshCw,
  Edit,
  History,
  Trash2,
  ClipboardList,
  AlertCircle,
  Package,
  ShoppingBasket,
  Pill,
  Utensils,
  GlassWater,
  Apple,
  Beef,
  Sparkles,
  Eye,
} from 'lucide-react';

import { toast } from '@/hooks/use-toast';
import { db, type InventoryItem, type MRAMapping, type RecipeIngredient } from '@/lib/db';
import { type BusinessType } from '@/lib/inventory/config';
import { formatInventoryQuantity, shouldPreferWholeStockCounts } from '@/lib/quantity-format';
import { deleteProduct } from '@/lib/services/product-service';
import { useAuth } from '@/hooks/use-auth';
import { useCurrency } from '@/hooks/use-currency';
import { ProductDetailsModal } from './product-details-modal';
import { getInventoryTemplateColumnsForBusinessType } from './import-template-config';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
} from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu';
import { Separator } from '@/components/ui/separator';
import { PaginationControls, usePaginatedItems } from './pagination-controls';

const statusBadgeVariant = {
  'In Stock': 'secondary',
  'Low Stock': 'default',
  'Out of Stock': 'destructive',
  'Service': 'outline',
} as const;

const isMraServiceMapping = (mapping?: MRAMapping | null): boolean => (
    mapping?.isProduct === false || mapping?.is_product === false
);

const isServiceInventoryItem = (item: InventoryItem, mapping?: MRAMapping | null): boolean => {
    if (isMraServiceMapping(mapping)) return true;
    return String(item.category || '').toLowerCase().includes('service');
};

const toCsvBoolean = (value: boolean | undefined): string => (value ? 'true' : 'false');
const toSafeNumber = (value: unknown): number => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
};

const toOptionalCsvNumber = (value: unknown): number | '' => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : '';
};

const getVariablePriceLabel = (unitType?: string): string => {
    const unit = String(unitType || '').trim().toLowerCase();
    if (!unit) return 'Variable Price';
    if (/(^|[^a-z])l(itre|iter|iters|itres)?([^a-z]|$)/.test(unit) || unit === 'l' || unit === 'ml') {
        return 'By Volume';
    }
    if (/(kg|g|gram|grams|lb|lbs|pound|pounds|oz|ounce|ounces|ton|tons)/.test(unit)) {
        return 'By Weight';
    }
    return 'Variable Price';
};

const toTemplateExportCsvRow = (
    item: InventoryItem,
    columns: string[],
    mapping?: MRAMapping
) => {
    const itemWithOptionalTax = item as InventoryItem & {
        taxRate?: number;
        taxCalculationMethod?: 'inclusive' | 'exclusive';
        mraProductCode?: string;
        mraProductName?: string;
        mraTaxType?: string;
        mraTaxRate?: number;
        mraUnitMeasure?: string;
    };

    const sourceRow: Record<string, string | number> = {
        name: item.name || '',
        category: item.category || '',
        barcode: item.barcode || '',
        isProduced: toCsvBoolean(item.isProduced),
        currentStock: isServiceInventoryItem(item, mapping) ? '' : Number(item.stockUnits || 0),
        price: item.price ?? '',
        cost: item.cost ?? '',
        taxRate: toOptionalCsvNumber(itemWithOptionalTax.taxRate ?? mapping?.mraTaxRate),
        taxCalculationMethod: itemWithOptionalTax.taxCalculationMethod ?? mapping?.taxCalculationMethod ?? '',
        mraProductCode: itemWithOptionalTax.mraProductCode ?? mapping?.mraProductCode ?? '',
        mraProductName: itemWithOptionalTax.mraProductName ?? mapping?.mraProductName ?? '',
        mraTaxType: itemWithOptionalTax.mraTaxType ?? mapping?.mraTaxType ?? '',
        mraTaxRate: toOptionalCsvNumber(itemWithOptionalTax.mraTaxRate ?? mapping?.mraTaxRate),
        mraUnitMeasure: itemWithOptionalTax.mraUnitMeasure ?? mapping?.mraUnitMeasure ?? item.unitType ?? '',
        unitType: item.unitType || 'unit',
        reorderLevel: Number(item.reorderLevel || 0),
        supplier: item.supplier || '',
        isSoldInPortions: toCsvBoolean(item.isSoldInPortions),
        portionName: item.portionName || '',
        portionsPerUnit: item.portionsPerUnit ?? '',
        recipe: item.recipe && item.recipe.length > 0 ? JSON.stringify(item.recipe) : '',
    };

    return columns.reduce<Record<string, string | number>>((row, column) => {
        row[column] = sourceRow[column] ?? '';
        return row;
    }, {});
};

interface InventoryTabProps {
    inventoryData: InventoryItem[];
    isMobile: boolean;
    currentBusinessType: BusinessType;
    searchTerm: string;
    onAddItem: () => void;
    onEditItem: (item: InventoryItem) => void;
    onImport: () => void;
    onTransfer: () => void;
    onSyncEisProducts?: () => void;
    isSyncingEisProducts?: boolean;
    readOnly?: boolean;
}

export function InventoryTab({ 
    inventoryData, 
    isMobile,
    currentBusinessType,
    searchTerm,
    onAddItem,
    onEditItem,
    onImport,
    onTransfer,
    onSyncEisProducts,
    isSyncingEisProducts = false,
    readOnly = false
}: InventoryTabProps) {
    const { user } = useAuth();
    const { currencyCode } = useCurrency();
    
    // Get currency symbol based on currency code
    const getCurrencySymbol = () => {
        const symbols: Record<string, string> = {
            'USD': '$',
            'EUR': '€',
            'GBP': '£',
            'JPY': '¥',
            'MWK': 'MWK',
            'ZAR': 'R',
            'KES': 'KSh',
            'UGX': 'USh',
            'TZS': 'TSh',
        };
        return symbols[currencyCode] || currencyCode;
    };

    const currencySymbol = getCurrencySymbol();
    const showItemTypeBadge =
      currentBusinessType === 'Restaurant' || currentBusinessType === 'Bar & Liquor';
    const preferWholeStockCounts = React.useMemo(
        () => shouldPreferWholeStockCounts(currentBusinessType),
        [currentBusinessType]
    );
    const exportTemplateColumns = React.useMemo(
        () => getInventoryTemplateColumnsForBusinessType(currentBusinessType),
        [currentBusinessType]
    );
    const mraMappingByItemId = useLiveQuery(
        async () => {
            const mappings = await db.mraMappings.toArray();
            const byItemId = new Map<string, MRAMapping>();
            for (const mapping of mappings) {
                if (mapping._operation === 'delete') continue;
                const itemId = String(mapping.inventoryItemId || mapping.inventory_item_id || '').trim();
                if (!itemId || byItemId.has(itemId)) continue;
                byItemId.set(itemId, mapping);
            }
            return byItemId;
        },
        [],
        new Map<string, MRAMapping>()
    );
    
    // Product details modal state
    const [selectedProduct, setSelectedProduct] = useState<InventoryItem | null>(null);
    const [isDetailsModalOpen, setIsDetailsModalOpen] = useState(false);
    const normalizedSearchTerm = searchTerm.trim().toLowerCase();
    const filteredInventoryData = React.useMemo(() => {
        if (!normalizedSearchTerm) return inventoryData || [];

        return (inventoryData || []).filter((item) =>
            [
                item.name,
                item.category,
                item.status,
                item.itemType,
                item.unitType,
                item.supplier,
                item.manufacturer,
                item.brand,
                item.batch,
                item.productCode,
                item.barcode,
                item.sku,
            ].some((value) => String(value || '').toLowerCase().includes(normalizedSearchTerm))
        );
    }, [inventoryData, normalizedSearchTerm]);

    const {
        setCurrentPage,
        totalItems,
        totalPages,
        effectiveCurrentPage,
        pageStartIndex,
        pageEndIndex,
        paginatedItems: paginatedInventoryData,
    } = usePaginatedItems(filteredInventoryData);

    React.useEffect(() => {
        setCurrentPage(1);
    }, [normalizedSearchTerm, setCurrentPage]);

    const handleViewDetails = (item: InventoryItem) => {
        setSelectedProduct(item);
        setIsDetailsModalOpen(true);
    };

    const handleEditFromDetails = (item: InventoryItem) => {
        setIsDetailsModalOpen(false);
        onEditItem(item);
    };

    const handleExport = async () => {
        if (!inventoryData || inventoryData.length === 0) {
            toast({ variant: 'destructive', title: 'No data to export' });
            return;
        }

        try {
            const inventoryIds = inventoryData.map((item) => String(item.id)).filter(Boolean);
            const mappings = inventoryIds.length > 0
                ? await db.mraMappings
                    .where('inventoryItemId')
                    .anyOf(inventoryIds)
                    .toArray()
                : [];
            const mappingByItemId = new Map<string, MRAMapping>();

            mappings.forEach((mapping) => {
                if (mapping._operation === 'delete') {
                    return;
                }

                const itemId = String(mapping.inventoryItemId || '').trim();
                if (!itemId || mappingByItemId.has(itemId)) {
                    return;
                }

                mappingByItemId.set(itemId, mapping);
            });

            const rows = inventoryData.map((item) =>
                toTemplateExportCsvRow(item, exportTemplateColumns, mappingByItemId.get(String(item.id)))
            );
            const csv = Papa.unparse(rows);
            const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
            const link = document.createElement('a');
            if (link.download !== undefined) {
                const url = URL.createObjectURL(blob);
                link.setAttribute('href', url);
                link.setAttribute('download', 'inventory-export.csv');
                link.style.visibility = 'hidden';
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
            }
            toast({ title: 'Export Complete', description: `${inventoryData.length} items have been exported.` });
        } catch (error) {
            console.error('Failed to export inventory:', error);
            toast({
                variant: 'destructive',
                title: 'Export Failed',
                description: 'Could not export the inventory file. Please try again.',
            });
        }
    };

    const handleDeleteItem = async (itemId: string) => {
        if (confirm('Are you sure you want to delete this item? This action cannot be undone.')) {
            try {
                if (!user) {
                    toast({ variant: 'destructive', title: 'Not authenticated' });
                    return;
                }

                // Get the item to get branchId
                const item = inventoryData.find(i => i.id === itemId);
                if (!item) {
                    toast({ variant: 'destructive', title: 'Item not found' });
                    return;
                }

                // Use product-service which handles marking for deletion and sync queueing
                await deleteProduct(
                    itemId,
                    user.uid,
                    user.displayName || user.email || 'Unknown',
                    item.branchId
                );

                toast({
                    title: 'Item Deleted',
                    description: 'The item has been removed from your inventory and queued for sync with the backend.',
                    variant: 'destructive',
                });
            } catch (error) {
                console.error('Failed to delete item:', error);
                toast({
                    variant: 'destructive',
                    title: 'Error',
                    description: 'Failed to delete item. Please try again.',
                });
            }
        }
    };


    const renderIcon = (item: InventoryItem) => {
        // For sellable items, use business-type-specific icons
        if (item.itemType === 'sellable') {
            switch (currentBusinessType) {
            case 'Pharmacy': return <Pill className="h-6 w-6 text-muted-foreground" data-ai-hint="pharmacy medicine" />;
            case 'Restaurant': return <Utensils className="h-6 w-6 text-muted-foreground" data-ai-hint="restaurant food" />;
            case 'Bar & Liquor': return <GlassWater className="h-6 w-6 text-muted-foreground" data-ai-hint="bar liquor bottle" />;
            case 'Supermarket': return <ShoppingBasket className="h-6 w-6 text-muted-foreground" data-ai-hint="supermarket product" />;
            case 'Grocery': return <Apple className="h-6 w-6 text-muted-foreground" data-ai-hint="grocery produce" />;
            case 'Beauty Salon and Spa': return <Sparkles className="h-6 w-6 text-muted-foreground" data-ai-hint="beauty salon product" />;
            default: return <Package className="h-6 w-6 text-muted-foreground" />;
            }
        }
        // For ingredients, use business-type-specific icons
        switch (currentBusinessType) {
        case 'Pharmacy': return <Pill className="h-6 w-6 text-muted-foreground" data-ai-hint="pharmacy medicine" />;
        case 'Restaurant': return <Beef className="h-6 w-6 text-muted-foreground" data-ai-hint="restaurant ingredient" />;
        case 'Bar & Liquor': return <GlassWater className="h-6 w-6 text-muted-foreground" data-ai-hint="bar liquor bottle" />;
        case 'Supermarket': return <ShoppingBasket className="h-6 w-6 text-muted-foreground" data-ai-hint="supermarket product" />;
        case 'Grocery': return <Apple className="h-6 w-6 text-muted-foreground" data-ai-hint="grocery produce" />;
        case 'Beauty Salon and Spa': return <Sparkles className="h-6 w-6 text-muted-foreground" data-ai-hint="beauty salon product" />;
        default: return <Package className="h-6 w-6 text-muted-foreground" />;
        }
    };

    const calculateCost = (recipe: RecipeIngredient[] | undefined) => {
        if (!recipe || !inventoryData) return 0;
        return recipe.reduce((totalCost, recipeItem) => {
            const inventoryItem = inventoryData.find(i => i.id === recipeItem.ingredientId);
            if (!inventoryItem) return totalCost;
            return totalCost + toSafeNumber(inventoryItem.cost) * toSafeNumber(recipeItem.quantity);
        }, 0);
    };

    const getDisplayValue = (item: InventoryItem) => {
        if (isServiceInventoryItem(item, mraMappingByItemId?.get(String(item.id)))) return 0;

        const storedValue = toSafeNumber(item.value);
        if (storedValue > 0) return storedValue;

        const stockUnits = toSafeNumber(item.stockUnits);
        const costPerUnit = toSafeNumber(item.cost);
        return stockUnits * costPerUnit;
    };

    const renderTableHeader = () => (
        <TableRow>
            <TableHead className="w-[40px]"><Checkbox /></TableHead>
            <TableHead className="min-w-[250px]">Item</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Stock/Price</TableHead>
            <TableHead>Unit</TableHead>
            <TableHead>Supplier</TableHead>
            <TableHead className="text-right">Remaining</TableHead>
            <TableHead className="text-right">Value/Cost</TableHead>
            {!readOnly && <TableHead className="w-[50px]"><span className="sr-only">Actions</span></TableHead>}
        </TableRow>
    );

    const renderTableRow = (item: InventoryItem) => {
        const mapping = mraMappingByItemId?.get(String(item.id));
        const isService = isServiceInventoryItem(item, mapping);
        const isSellable = item.itemType === 'sellable';
        const portionLabel = item.portionName || 'portion';
        const estimatedRecipeCost = calculateCost(item.recipe);
        const cost = isSellable && estimatedRecipeCost > 0
            ? estimatedRecipeCost
            : toSafeNumber(item.cost);
        const displayValue = getDisplayValue(item);
        const formattedStockUnits = formatInventoryQuantity(item.stockUnits, {
            preferWholeNumbers: preferWholeStockCounts,
        });
        
        return (
            <TableRow key={item.id}>
                <TableCell><Checkbox /></TableCell>
                <TableCell>
                    <div className="flex items-center gap-4">
                    <div className="relative h-12 w-12 shrink-0 overflow-hidden rounded-md flex items-center justify-center bg-muted">
                        {renderIcon(item)}
                    </div>
                    <div className='grid gap-0.5'>
                        <div className="flex items-center gap-2">
                        <span className="font-medium">{item.name}</span>
                        {isService && (
                            <Badge variant="outline">Service</Badge>
                        )}
                        {item.isVariablePrice && (
                            <Badge variant="outline">{getVariablePriceLabel(item.unitType)}</Badge>
                        )}
                        </div>
                        <span className="text-xs text-muted-foreground">{item.category}</span>
                    </div>
                    </div>
                </TableCell>
                <TableCell>
                    {isService ? (
                        <Badge variant={statusBadgeVariant.Service}>Service</Badge>
                    ) : (
                        item.status && <Badge variant={statusBadgeVariant[item.status]}>{item.status === 'Low Stock' && <AlertCircle className="mr-1 h-3 w-3" />}{item.status}</Badge>
                    )}
                </TableCell>
                <TableCell className="text-right font-medium">
                    {isSellable ? `${currencySymbol}${(Number(item.price) || 0).toFixed(2)}` : formattedStockUnits}
                </TableCell>
                <TableCell className="text-muted-foreground">{isService ? 'N/A' : (item.unitType || 'N/A')}</TableCell>
                <TableCell>{item.isProduced ? 'In-house' : (item.supplier || (isSellable ? 'In-house' : 'N/A'))}</TableCell>
                <TableCell className="text-right">
                    {isService ? (
                        <span className="font-medium text-muted-foreground">Not tracked</span>
                    ) : isSellable && item.isSoldInPortions && item.portionsPerUnit ? (
                        <div className="text-sm">
                            <div className="font-semibold">{formattedStockUnits} {item.unitType}</div>
                            <div className="text-xs text-muted-foreground">
                                {Math.floor((item.stockUnits || 0) * item.portionsPerUnit)} {portionLabel}
                            </div>
                        </div>
                    ) : (
                        <div className="font-semibold">{formattedStockUnits} {item.unitType}</div>
                    )}
                </TableCell>
                <TableCell className="text-right font-semibold">
                        {isService ? 'N/A' : (isSellable ? `${currencySymbol}${toSafeNumber(cost).toFixed(2)}` : `${currencySymbol}${toSafeNumber(displayValue).toFixed(2)}`)}
                </TableCell>
                {!readOnly && (
                    <TableCell>
                        <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="icon">
                            <MoreHorizontal />
                            </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                            <DropdownMenuItem onSelect={() => handleViewDetails(item)}><Eye className="mr-2"/> View Details</DropdownMenuItem>
                            <DropdownMenuItem onSelect={() => onEditItem(item)}><Edit className="mr-2"/> Edit Item</DropdownMenuItem>
                            <DropdownMenuItem><History className="mr-2"/> View History</DropdownMenuItem>
                            <DropdownMenuSeparator />
                            <DropdownMenuItem onSelect={() => handleDeleteItem(item.id)} className="text-destructive"><Trash2 className="mr-2"/> Delete Item</DropdownMenuItem>
                        </DropdownMenuContent>
                        </DropdownMenu>
                    </TableCell>
                )}
            </TableRow>
        );
    };

    const renderMobileCard = (item: InventoryItem) => {
        const mapping = mraMappingByItemId?.get(String(item.id));
        const isService = isServiceInventoryItem(item, mapping);
        const isSellable = item.itemType === 'sellable';
        const portionLabel = item.portionName || 'portion';
        const estimatedRecipeCost = calculateCost(item.recipe);
        const cost = isSellable && estimatedRecipeCost > 0
            ? estimatedRecipeCost
            : toSafeNumber(item.cost);
        const displayValue = getDisplayValue(item);
        const formattedStockUnits = formatInventoryQuantity(item.stockUnits, {
            preferWholeNumbers: preferWholeStockCounts,
        });

        return (
            <Card key={item.id} className="mb-4">
                <CardContent className="p-4">
                    <div className="flex items-start gap-4">
                        <div className="relative h-12 w-12 shrink-0 overflow-hidden rounded-md flex items-center justify-center bg-muted">
                            {renderIcon(item)}
                        </div>
                        <div className="flex-1 grid gap-0.5">
                            <div className="flex items-center gap-2">
                            <p className="font-semibold">{item.name}</p>
                            {isService && (
                                <Badge variant="outline">Service</Badge>
                            )}
                            {item.isVariablePrice && (
                                <Badge variant="outline">{getVariablePriceLabel(item.unitType)}</Badge>
                            )}
                            </div>
                            <p className="text-sm text-muted-foreground">{item.category}</p>
                            <div className="flex items-center gap-2 mt-1">
                                {showItemTypeBadge && (
                                    <Badge variant={isSellable ? 'default' : 'outline'} className="w-fit">
                                        {item.itemType}
                                    </Badge>
                                )}
                                {isService ? (
                                    <Badge variant="outline" className="w-fit">
                                        Service
                                    </Badge>
                                ) : item.status && (
                                    <Badge variant={statusBadgeVariant[item.status]} className="w-fit">
                                        {item.status === 'Low Stock' && <AlertCircle className="mr-1 h-3 w-3" />}
                                        {item.status}
                                    </Badge>
                                )}
                            </div>
                        </div>
                        {!readOnly && (
                            <DropdownMenu>
                                <DropdownMenuTrigger asChild>
                                    <Button variant="ghost" size="icon" className="-mt-2 -mr-2">
                                        <MoreHorizontal />
                                    </Button>
                                </DropdownMenuTrigger>
                                <DropdownMenuContent align="end">
                                    <DropdownMenuItem onSelect={() => handleViewDetails(item)}><Eye className="mr-2" /> View Details</DropdownMenuItem>
                                    <DropdownMenuItem onSelect={() => onEditItem(item)}><Edit className="mr-2" /> Edit Item</DropdownMenuItem>
                                    <DropdownMenuItem><History className="mr-2" /> View History</DropdownMenuItem>
                                    <DropdownMenuSeparator />
                                    <DropdownMenuItem onSelect={() => handleDeleteItem(item.id)} className="text-destructive"><Trash2 className="mr-2" /> Delete Item</DropdownMenuItem>
                                </DropdownMenuContent>
                            </DropdownMenu>
                        )}
                    </div>
                    <Separator className="my-4" />
                    <div className="grid grid-cols-2 gap-4 text-sm">
                        {isSellable ? (
                            <>
                                <div>
                                    <p className="text-muted-foreground">{item.isVariablePrice ? 'Price/Unit' : 'Price'}</p>
                                    <p className="font-medium">{currencySymbol}{(Number(item.price) || 0).toFixed(2)}</p>
                                </div>
                                <div>
                                    <p className="text-muted-foreground">Est. Cost</p>
                                    <p className="font-medium">{isService ? 'N/A' : `${currencySymbol}${(Number(cost) || 0).toFixed(2)}`}</p>
                                </div>
                                <div>
                                    <p className="text-muted-foreground">Remaining</p>
                                    {isService ? (
                                        <p className="font-medium text-muted-foreground">Not tracked</p>
                                    ) : (
                                    <p className="font-medium">
                                        {formattedStockUnits} <span className="text-muted-foreground">{item.unitType || 'unit'}</span>
                                    </p>
                                    )}
                                    {!isService && item.isSoldInPortions && item.portionsPerUnit && (
                                        <p className="text-xs text-muted-foreground mt-1">
                                            {Math.floor((item.stockUnits || 0) * item.portionsPerUnit)} {portionLabel}
                                        </p>
                                    )}
                                </div>
                            </>
                        ) : (
                            <>
                                <div>
                                    <p className="text-muted-foreground">Stock</p>
                                    <p className="font-medium">{formattedStockUnits} <span className="text-muted-foreground">{item.unitType}</span></p>
                                </div>
                                <div>
                                    <p className="text-muted-foreground">Value</p>
                                    <p className="font-medium">{currencySymbol}{toSafeNumber(displayValue).toFixed(2)}</p>
                                </div>
                                <div>
                                    <p className="text-muted-foreground">Supplier</p>
                                    <p className="font-medium">{item.supplier}</p>
                                </div>
                                <div>
                                    <p className="text-muted-foreground">Cost/Unit</p>
                                    <p className="font-medium">{currencySymbol}{(Number(item.cost) || 0).toFixed(2)}</p>
                                </div>
                            </>
                        )}
                    </div>
                </CardContent>
            </Card>
        );
    };

    return (
         <CardContent>
            <div className="flex w-full flex-col items-stretch gap-2 mb-6 sm:flex-row">
                {!readOnly && (
                    <Button onClick={onAddItem}>
                        <PlusCircle className="mr-2 h-4 w-4" /> Add Item
                    </Button>
                )}
                {onSyncEisProducts && (
                    <Button
                        type="button"
                        variant="outline"
                        onClick={onSyncEisProducts}
                        disabled={isSyncingEisProducts}
                        className="whitespace-nowrap"
                    >
                        {isSyncingEisProducts ? (
                            <>
                                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                Syncing EIS...
                            </>
                        ) : (
                            <>
                                <RefreshCw className="mr-2 h-4 w-4" />
                                Sync EIS Products
                            </>
                        )}
                    </Button>
                )}
                {/* <Button variant="outline" onClick={onTransfer}>
                    <Repeat className="mr-2 h-4 w-4" /> Transfer Stock
                </Button> */}
                <div className="ml-auto flex items-center gap-2">
                <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                    <Button variant="outline" className='h-10 w-10 p-0'>
                        <MoreHorizontal className="h-4 w-4" />
                    </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                    {!readOnly && <DropdownMenuItem onSelect={onImport}><Upload className="mr-2" /> Import Products</DropdownMenuItem>}
                    <DropdownMenuItem onSelect={handleExport}><Download className="mr-2" /> Export Stock File</DropdownMenuItem>
                    {!readOnly && (
                        <DropdownMenuItem asChild>
                            <Link href="/dashboard/inventory/audit"><ClipboardList className="mr-2" /> Full Stock Audit</Link>
                        </DropdownMenuItem>
                    )}
                    </DropdownMenuContent>
                </DropdownMenu>
                </div>
            </div>
            {isMobile ? (
                filteredInventoryData.length > 0 ? (
                <div>
                        {paginatedInventoryData.map(renderMobileCard)}
                </div>
                ) : (
                <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
                    {normalizedSearchTerm ? `No products match "${searchTerm.trim()}".` : 'No products found.'}
                </div>
                )
            ) : (
                <div className="overflow-x-auto">
                    <Table>
                        <TableHeader>
                            {renderTableHeader()}
                        </TableHeader>
                        <TableBody>
                            {filteredInventoryData.length > 0 ? (
                                paginatedInventoryData.map(renderTableRow)
                            ) : (
                                <TableRow>
                                    <TableCell colSpan={9} className="h-24 text-center text-muted-foreground">
                                        {normalizedSearchTerm ? `No products match "${searchTerm.trim()}".` : 'No products found.'}
                                    </TableCell>
                                </TableRow>
                            )}
                        </TableBody>
                    </Table>
                </div>
            )}

            <PaginationControls
                currentPage={effectiveCurrentPage}
                totalItems={totalItems}
                totalPages={totalPages}
                pageStartIndex={pageStartIndex}
                pageEndIndex={pageEndIndex}
                onPageChange={setCurrentPage}
                itemLabel="products"
            />

            {!readOnly && (
                <ProductDetailsModal
                    product={selectedProduct}
                    isOpen={isDetailsModalOpen}
                    onOpenChange={setIsDetailsModalOpen}
                    onEdit={handleEditFromDetails}
                    currentBusinessType={currentBusinessType}
                    isServiceProduct={
                        selectedProduct
                            ? isServiceInventoryItem(selectedProduct, mraMappingByItemId?.get(String(selectedProduct.id)))
                            : false
                    }
                />
            )}
        </CardContent>
    )
}
