

'use client';
import Dexie, { type EntityTable } from 'dexie';

export interface Business {
    id: string; // e.g., 'main-business'
    name: string;
    type: string;
    currency: string;
    tin?: string;
    email?: string;
    phone?: string;
    address?: string;
    website?: string;
}

export interface Subscription {
    id: string; // Typically matches businessId
    businessId: string;
    planId?: 'starter' | 'pro'; // Legacy field for backward compatibility
    status: 'active' | 'paused' | 'cancelled';
    trialEndDate?: string; // ISO string (legacy)
    
    // Account balance and credits
    account_balance: number;
    total_spent: number;
    
    // Pricing
    base_price_per_day: number;
    
    // Free trial
    free_trial_days: number;
    free_trial_credits_applied: boolean;
    free_trial_credits_amount: number;
    free_trial_end_date?: string; // ISO string
    
    // Feature flags
    enable_pos: boolean;
    enable_inventory: boolean;
    enable_invoicing: boolean;
    enable_online_menu: boolean;
    enable_online_ordering: boolean;
    enable_kitchen: boolean;
    enable_expense_management: boolean;
    enable_supplier_management: boolean;
    enable_purchases: boolean;
    enable_low_stock_alerts: boolean;
    enable_expiry_alerts: boolean;
    enable_customer_management: boolean;
    enable_reports: boolean;
    enable_analytics: boolean;
    enable_take_orders: boolean;
    enable_staff_management: boolean;
    enable_waste_management: boolean;
    enable_stock_transfers: boolean;
    enable_stock_audits: boolean;
    enable_tax_management: boolean;
    enable_multi_branch: boolean;
    
    // Usage limits
    enable_usage_limits: boolean;
    
    // Low balance threshold
    low_balance_threshold: number;
    low_balance_notified: boolean;
    low_balance_notified_date?: string; // ISO string
    
    // Dates
    start_date: string; // ISO string
    last_payment_date?: string; // ISO string
    last_billing_date?: string; // ISO string
    last_charge_date?: string; // ISO string
    created_at: string; // ISO string
    updated_at: string; // ISO string
}

export interface Supplier {
    id: string;
    businessId?: string; // Reference to the business this supplier belongs to
    branchId?: string;
    name: string;
    contactPerson?: string;
    createdAt?: string;
    updatedAt?: string;
    email?: string;
    phone?: string;
    address?: string;
    city?: string;
    region?: string;
    country?: string;
    
    // MRA Compliance Fields
    supplierTin?: string; // Supplier's Tax Identification Number
    mraSupplierId?: number;
    vatRegistered?: boolean; // Is supplier VAT registered?
    
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface RecipeIngredient {
    ingredientId: string;
    name: string;
    quantity: number;
    unit: string;
}

export interface InventoryItem {
    id: string;
    name:string;
    category: string;
    itemType: 'ingredient' | 'sellable';
    branchId: string; 

    // Fields for all items
    manufacturer?: string;
    supplier?: string;
    
    // Fields for ingredients
    stockUnits?: number; // This will now be a calculated sum of all batch quantities
    stock_units?: number;
    unitType?: string;
    unit_type?: string;
    reorderLevel?: number;
    cost?: number; // This will be the latest or average cost
    value?: number;
    isRecipeIngredient?: boolean;
    // Local-only flag: initial stock was created via purchase records
    initialStockViaPurchase?: boolean;
    status?: 'In Stock' | 'Low Stock' | 'Out of Stock';
    expiry?: string; // Earliest expiry date from batches
    batch?: string;
    brand?: string;
    packSize?: number;
    productCode?: string; // Unique product code for identification
    barcode?: string; // Barcode for product identification
    sku?: string; // Stock Keeping Unit

    // Fields for sellable products
    price?: number;
    recipe?: RecipeIngredient[];
    isVariablePrice?: boolean; // For items sold by weight/volume
    isFuel?: boolean; // Fuel items for fuel attendants
    isOil?: boolean; // Oil/lubricant items for separate sales reporting
    isProduced?: boolean; // For restaurant/bar: true if made in-house, false if purchased
    onMenu?: boolean;
    image?: string; // Base64 encoded image or image URL
    is_mra_ready?: boolean;
    price_locked?: boolean;
    priceLocked?: boolean;
    tax_locked?: boolean;
    taxLocked?: boolean;
    
    // Bar & Liquor specific fields
    isSoldInPortions?: boolean; // For bar/liquor: true if sold in portions (shots, tots, etc)
    portionName?: string; // Name of the portion (e.g., "shot", "tot", "glass")
    portionsPerUnit?: number; // Number of portions that make up one full unit
    
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
    _purchaseSyncPending?: boolean;
}

export type EisStockReceiptSource = 'pos_goods_receiving' | 'supplier_sale';

export interface PurchaseRecord {
    id?: string | number;
    purchaseOrderId?: string; // ID of the purchase order this item belongs to
    sessionId?: string; // Link to the session when stock was received
    referenceNumber?: string; // Supplier reference / invoice number
    vatAmount?: number; // VAT amount for the purchase
    taxRate?: number; // VAT rate (%) for this item
    taxCalculationMethod?: 'inclusive' | 'exclusive';
    taxAmount?: number; // VAT amount for this item
    productId: string;
    productName: string;
    supplierId: string;
    supplierName: string;
    supplierTin?: string;
    mraSupplierId?: number;
    supplierVatRegistered?: boolean;
    eisStockReceiptSource?: EisStockReceiptSource;
    branchId: string; 
    quantityReceived: number;
    quantityRemaining: number; // New field for FIFO
    costPerUnit: number;
    totalCost: number;
    sellingPrice?: number;
    paymentStatus: 'Paid' | 'Unpaid' | 'Partial' | 'Credit' | 'Pending';
    amountDue: number;
    batchNumber?: string;
    expiryDate?: string;
    allowQuantityDecrease?: boolean;
    receivedDate: string; // ISO string
    createdAt?: string;
    updatedAt?: string;
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface OrderItem {
    id: string;
    inventoryItemId?: string; // Reference to the original inventory item
    inventory_item_id?: string;
    name: string;
    quantity: number;
    notes?: string;
    // Per-item pricing (MRA Compliance - Immutable snapshot)
    price?: number; // Unit price at time of sale
    discountRuleId?: string;
    discount_rule_id?: string;
    discountName?: string;
    discount_name?: string;
    discountType?: 'percentage' | 'fixed' | string;
    discount_type?: 'percentage' | 'fixed' | string;
    discountValue?: number;
    discount_value?: number;
    discountAmount?: number;
    discount_amount?: number;
    // Per-item tax information (MRA Compliance - Immutable snapshot)
    tax_rate?: number; // Tax rate percentage (e.g., 16.50 for 16.5%)
    taxRate?: number; // Alias for tax_rate (camelCase)
    tax_type?: string; // Tax classification (standard/zero/exempt)
    taxType?: string; // Alias for tax_type (camelCase)
    tax_calculation_method?: 'inclusive' | 'exclusive'; // How tax is calculated for this item
    taxCalculationMethod?: 'inclusive' | 'exclusive'; // Alias for tax_calculation_method (camelCase)
    // Calculated tax amounts (Immutable snapshot for audit trail)
    subtotal?: number; // Net amount (before tax)
    tax_amount?: number; // Tax amount for this item
    taxAmount?: number; // Alias for tax_amount (camelCase)
    total?: number; // Gross amount (subtotal + tax)
    // MRA Product Mapping
    mra_product_code?: string; // MRA product code
    mraProductCode?: string; // Alias for mra_product_code (camelCase)
    vat_category?: string; // VAT category
    vatCategory?: string; // Alias for vat_category (camelCase)
}

export interface LocalInventoryConsumption {
    inventoryItemId: string;
    quantity: number;
    purchaseHistoryId?: string | number;
}

export interface Order {
    id: string;
    orderNumber: number;
    branchId: string; 
    sessionId?: string; // Link to the session
    pumpName?: string; // Fuel pump used for this order (optional)
    orderType?: 'sale' | 'return' | 'adjustment'; // Type of order
    items: OrderItem[];
    // Local-only FIFO details used to restore provisional stock when EIS rejects a sale.
    localInventoryConsumption?: LocalInventoryConsumption[];
    status: 'New' | 'Preparing' | 'Ready' | 'Completed' | 'Voided' | 'Cancelled' | 'Refunded' | 'Partially Refunded';
    subtotal: number;
    discountAmount?: number;
    discount_amount?: number;
    discountMetadata?: Record<string, any>;
    discount_metadata?: Record<string, any>;
    total: number;
    tax?: number; // Tax amount for this order (legacy field)
    tip?: number; // Tip amount for this order
    paymentMethod: 'Cash' | 'Card' | 'Mobile Money' | 'On Account' | 'Other';
    // Buyer/customer details (optional)
    customerName?: string;
    customer_name?: string;
    customerPhone?: string;
    customer_phone?: string;
    customerTin?: string;
    customer_tin?: string;
    customerEmail?: string;
    customer_email?: string;
    customerAddress?: string;
    customer_address?: string;
    customerNotes?: string;
    customer_notes?: string;
    buyerName?: string;
    buyer_name?: string;
    buyerTin?: string;
    buyer_tin?: string;
    buyerAuthorizationCode?: string;
    buyer_authorization_code?: string;
    isExport?: boolean;
    is_export?: boolean;
    isReliefSupply?: boolean;
    is_relief_supply?: boolean;
    vat5ProjectNumber?: string;
    vat5_project_number?: string;
    vat5CertificateNumber?: string;
    vat5_certificate_number?: string;
    vat5Quantity?: number;
    vat5_quantity?: number;
    eisValidationMetadata?: Record<string, any>;
    eis_validation_metadata?: Record<string, any>;
    cogs: number; // Cost of Goods Sold for this order
    // Tax snapshot (MRA compliance - NEVER calculate tax dynamically)
    // These fields preserve the exact tax rules that applied at the time of sale
    tax_rate_name?: string; // Name of the tax rate applied (e.g., 'Standard VAT')
    taxRateName?: string; // Alias for tax_rate_name (camelCase)
    tax_rate_value?: number; // VAT percentage at time of sale (e.g., 16.50)
    taxRateValue?: number; // Alias for tax_rate_value (camelCase)
    tax_type?: 'VAT_STANDARD' | 'VAT_ZERO' | 'VAT_EXEMPT'; // VAT classification at time of sale
    taxType?: 'VAT_STANDARD' | 'VAT_ZERO' | 'VAT_EXEMPT'; // Alias for tax_type (camelCase)
    vat_amount?: number; // Calculated VAT amount (for audit verification)
    vatAmount?: number; // Alias for vat_amount (camelCase)
    net_amount?: number; // Amount before VAT
    netAmount?: number; // Alias for net_amount (camelCase)
    gross_amount?: number; // Amount including VAT
    grossAmount?: number; // Alias for gross_amount (camelCase)
    // MRA EIS Fields
    fiscal_invoice_number?: string; // Fiscal invoice number from MRA
    fiscalInvoiceNumber?: string; // Alias for fiscal_invoice_number (camelCase)
    eis_uuid?: string; // EIS UUID from MRA
    eisUuid?: string; // Alias for eis_uuid (camelCase)
    eis_status?: 'PENDING' | 'SUBMITTED' | 'ACCEPTED' | 'REJECTED'; // MRA submission status
    eisStatus?: 'PENDING' | 'SUBMITTED' | 'ACCEPTED' | 'REJECTED'; // Alias for eis_status (camelCase)
    eis_sync_state?: string;
    eisSyncState?: string;
    mra_submission?: Record<string, any>;
    mraSubmission?: Record<string, any>;
    eis_submitted_at?: string; // ISO string - when submitted to MRA
    eisSubmittedAt?: string; // Alias for eis_submitted_at (camelCase)
    qr_code_payload?: string; // QR code payload from MRA
    qrCodePayload?: string; // Alias for qr_code_payload (camelCase)
    digital_signature?: string; // Digital signature from MRA
    digitalSignature?: string; // Alias for digital_signature (camelCase)
    is_fiscal_locked?: boolean; // Is this order locked (submitted to MRA)?
    isFiscalLocked?: boolean; // Alias for is_fiscal_locked (camelCase)
    createdAt: string; // ISO string
    updatedAt: string; // ISO string
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
    syncStatus?: 'pending' | 'failed' | 'synced';
    syncError?: string;
    syncRetryBlocked?: boolean;
    syncFailedAt?: string;
}

export interface Refund {
    id: string;
    branchId: string;
    orderId: string;
    orderNumber: number;
    items: OrderItem[];
    total: number;
    reason?: string;
    refundedBy: string;
    refundedAt: string;
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface TakeOrderItem {
    id: string;
    inventoryItemId: string;
    name: string;
    quantity: number;
    price: number;
    notes?: string;
    createdAt: string; // ISO string
    updatedAt: string; // ISO string
}

export interface TakeOrder {
    id: string;
    orderNumber: number;
    branchId: string;
    businessId?: string;
    status: 'Pending' | 'Confirmed' | 'Sent to Kitchen' | 'Preparing' | 'Ready' | 'Completed' | 'Cancelled';
    orderType: 'staff' | 'self_service';
    customerName?: string;
    customerPhone?: string;
    customerNotes?: string;
    tableNumber?: string;
    specialInstructions?: string;
    items: TakeOrderItem[];
    createdBy?: string;
    createdAt: string; // ISO string
    updatedAt: string; // ISO string
    completedAt?: string; // ISO string
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface StockRecord {
    itemId: string;
    name: string;
    quantity: number;
}

export interface Session {
    id: string; // e.g., `SESS-${branchId}-${timestamp}`
    branchId: string;
    userId: string; // User who started the session
    userName: string;
    userEmail: string; // User's email for comparison
    status: 'active' | 'closed';
    pumpName?: string; // Fuel pump used for this session (optional)
    openingFloat: number;
    openingStock?: StockRecord[];
    closingStock?: StockRecord[];
    closingFloat?: number;
    expectedCash: number; // openingFloat + cashSales
    actualCash?: number;
    difference?: number; // actualCash - expectedCash
    totalSales: number; // Sum of all order subtotals (tax exclusive / net sales)
    totalCashSales: number;
    totalCardSales: number;
    totalMobileMoneySales: number;
    totalOnAccountSales: number;
    totalOtherSales: number;
    totalTips: number;
    totalRefunds?: number;
    startedAt: string; // ISO String
    closedAt?: string; // ISO String
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface Staff {
    id: string;
    name: string;
    email: string;
    role: 'Admin' | 'Manager' | 'Cashier' | 'Waiter';
    branchId: string;
    assignedProductType?: string;
    isFuelAttendant?: boolean;
    password?: string; // For POS login
}

export interface StockTakeItem {
    itemId: string;
    itemName: string;
    systemStock: number;
    countedStock: number;
    discrepancy: number;
}

export interface StockTake {
    id: string; // e.g., ST-20240101-1
    branchId: string;
    createdAt: string; // ISO string
    createdBy: string; // User name
    items: StockTakeItem[];
    status: 'Pending Approval' | 'Approved' | 'Rejected';
    totalDiscrepancyValue: number;
    approvedBy?: string;
    approvedAt?: string; // ISO string
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface Expense {
    id: string; // EXP-timestamp
    branchId: string;
    title: string;
    category: 'Utilities' | 'Rent' | 'Salaries' | 'Supplies' | 'Marketing' | 'Maintenance' | 'Other';
    amount: number;
    date: string | Date; // ISO string in storage, Date in edit forms
    notes?: string;
    status: 'Pending' | 'Approved' | 'Rejected';
    createdBy: string;
    approvedBy?: string;
    approvedAt?: string;
    updatedAt?: string;
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface Customer {
    id: string; // CUST-timestamp
    branchId: string;
    name: string;
    email?: string;
    phone?: string;
    address?: string;
    createdAt: string; // ISO string
}

export interface InvoiceItem {
    id: string;
    productId: string; // Reference to InventoryItem
    name: string;
    quantity: number;
    price: number; // Price per unit at the time of invoice creation
    total: number;
    description?: string; // Optional product description
    sku?: string; // Product SKU for reference
}

export interface Invoice {
    id: string; // UUID from backend
    invoiceNumber: number;
    branchId: string;
    customerId: string;
    customerName: string;
    status: 'Draft' | 'Sent' | 'Paid' | 'Void';
    approvalStatus?: 'Pending' | 'Approved' | 'Rejected'; // Approval status for invoice review
    items: InvoiceItem[];
    subtotal: number;
    tax: number;
    total: number;
    issueDate: string; // ISO string
    dueDate: string; // ISO string
    notes?: string;
    relatedOrderId?: string; // UUID of related POS Order when invoice is marked as Paid
    approvedBy?: string; // User who approved/rejected the invoice
    approvedAt?: string; // ISO string - Timestamp of approval/rejection
    createdAt: string; // ISO string
    updatedAt?: string; // ISO string
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface TaxRate {
    id: string;
    businessId?: string; // Reference to the business this tax rate belongs to
    name: string;
    rate: number; // Stored as a percentage, e.g., 16.50 for 16.50%
    taxType: 'VAT_STANDARD' | 'VAT_ZERO' | 'VAT_EXEMPT';
    isDefault?: boolean;
    effectiveFrom?: string; // ISO date string
    effectiveTo?: string; // ISO date string, nullable
    isActive?: boolean;
    createdBy?: string; // User ID who created this tax rate
    createdByName?: string; // User's full name
    createdAt?: string; // ISO timestamp
    updatedAt?: string; // ISO timestamp
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface StockTransfer {
    id: string;
    branchId?: string;
    fromBranchId: string;
    fromBranchName: string;
    toBranchId: string;
    toBranchName: string;
    itemId: string;
    itemName: string;
    quantity: number;
    initiatedBy: string;
    createdAt: string; // ISO string
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface WasteRecord {
    id: string;
    branchId: string;
    sessionId?: string; // Link to session when waste was recorded
    itemId: string;
    itemName: string;
    batchId?: number; // Reference to PurchaseRecord batch for FIFO tracking
    quantity: number;
    unit?: string;
    cost: number;
    reason: 'Expired' | 'Damaged' | 'Spoilage' | 'Error' | 'Other';
    affectsTax?: boolean;
    notes?: string;
    approvedBy?: string;
    approvedAt?: string;
    recordedBy: string;
    recordedAt: string; // ISO string
    createdAt?: string;
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface PurchaseOrderItem {
    id: string;
    inventoryItemId: string;
    inventoryItemName?: string;
    quantityOrdered: number;
    quantityReceived: number;
    quantityRemaining: number;
    costPerUnit: number;
    batchNumber?: string;
    expiryDate?: string;
    taxRate?: number; // VAT rate (%) for this item
    taxCalculationMethod?: 'inclusive' | 'exclusive';
    taxAmount?: number; // VAT amount for this item
    
    // MRA Compliance Fields
    mraProductCode?: string; // MRA product code for this item
    mraTaxRate?: number; // MRA tax rate at time of purchase
}

export interface PurchaseOrder {
    id: string;
    orderNumber: string;
    order_number?: string;
    supplierId?: string;
    supplierName?: string;
    receivedDate?: string; // ISO string for the selected purchase date
    referenceNumber?: string; // Supplier reference / invoice number
    vatAmount?: number; // VAT amount for the purchase
    status: 'Draft' | 'Pending' | 'Approved' | 'Received' | 'Completed' | 'Cancelled';
    totalItems: number;
    totalCost: number;
    paymentStatus: 'Unpaid' | 'Partial' | 'Paid';
    amountPaid: number;
    amountDue: number;
    notes: string;
    createdBy: string;
    branchId: string;
    items: PurchaseOrderItem[];
    createdAt: string; // ISO string
    updatedAt: string; // ISO string
    
    // MRA Compliance Fields
    supplierTin?: string; // Supplier's Tax Identification Number
    mraSupplierId?: number;
    supplierVatRegistered?: boolean; // Is supplier VAT registered?
    eisStockReceiptSource?: EisStockReceiptSource;
    
    // EIS Tracking Fields
    eisInvoiceNumber?: string; // MRA EIS invoice number if this PO was invoiced
    eisSynced?: boolean; // Has this purchase been synced to MRA?
    eisSyncedAt?: string; // ISO string - When was this synced to MRA?
    
    // Approval Workflow
    approvedBy?: string; // User who approved this purchase
    approvedAt?: string; // ISO string - When was this purchase approved?
    
    // Sync fields
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export type ActionType = 
    | 'SESSION_START' | 'SESSION_END'
    | 'ITEM_CREATE' | 'ITEM_UPDATE' | 'ITEM_DELETE'
    | 'ORDER_CREATE' | 'ORDER_STATUS_UPDATE' | 'ORDER_REFUND' | 'ORDER_VOID'
    | 'STOCK_RECEIVE' | 'STOCK_RECEIVE_UPDATE' | 'STOCK_RECEIVE_DELETE' | 'STOCK_TRANSFER' | 'STOCK_WASTE' | 'STOCK_AUDIT_SUBMIT' | 'STOCK_AUDIT_APPROVE' | 'STOCK_AUDIT_REJECT'
    | 'EXPENSE_CREATE' | 'EXPENSE_APPROVE' | 'EXPENSE_REJECT'
    | 'STAFF_CREATE' | 'STAFF_UPDATE' | 'STAFF_DELETE'
    | 'SUPPLIER_CREATE' | 'SUPPLIER_UPDATE' | 'SUPPLIER_DELETE';

export interface AuditLog {
    id: string; // AUDIT-timestamp
    timestamp: string; // ISO string
    userId: string;
    userName: string;
    branchId: string;
    actionType: ActionType;
    entityType:
        | 'Session'
        | 'InventoryItem'
        | 'Order'
        | 'Purchase'
        | 'PurchaseOrder'
        | 'PurchaseRecord'
        | 'Expense'
        | 'Staff'
        | 'StockTake'
        | 'StockTransfer'
        | 'Waste'
        | 'WasteRecord'
        | 'Refund'
        | 'Supplier';
    entityId: string;
    details: Record<string, any>;
}

export interface CartItem extends InventoryItem {
    quantity: number;
    price: number;
    notes?: string;
    inventoryItemId: string;
    branchId: string;
    savedAt?: string;
    discountRuleId?: string;
    discount_rule_id?: string;
    discountName?: string;
    discount_name?: string;
    discountType?: 'percentage' | 'fixed' | string;
    discount_type?: 'percentage' | 'fixed' | string;
    discountValue?: number;
    discount_value?: number;
    discountAmount?: number;
    discount_amount?: number;
}

export interface MRAMapping {
    id: string;
    inventoryItemId: string;
    inventory_item_id?: string;
    branchId?: string;
    mraProductCode: string;
    mraProductName: string;
    mraTaxType: 'standard' | 'zero' | 'exempt';
    mra_tax_type?: 'standard' | 'zero' | 'exempt';
    mraTaxRate: number;
    mra_tax_rate?: number;
    mraUnitMeasure: string;
    taxCalculationMethod: 'inclusive' | 'exclusive';
    isProduct?: boolean;
    is_product?: boolean;
    taxType?: string;
    tax_type?: string;
    taxRate?: number;
    tax_rate?: number;
    isApproved: boolean;
    approvedAt?: string;
    mraSynced: boolean;
    isReadyForSale?: boolean;
    is_ready_for_sale?: boolean;
    isTaxpayerCompatible?: boolean;
    is_taxpayer_compatible?: boolean;
    taxpayerCompatibilityError?: string;
    taxpayer_compatibility_error?: string;
    lastSyncedAt?: string;
    createdAt: string;
    updatedAt: string;
    _dirty?: boolean;
    _operation?: 'create' | 'update' | 'delete';
    _synced_at?: string;
}

export interface InventorySnapshot {
    id: string;
    inventoryItemId: string;
    branchId: string;
    quantityBeforeSale: number;
    quantitySold: number;
    quantityAfterSale: number;
    relatedInvoiceNumber?: string;
    relatedOrderId?: string;
    productPrice: number;
    productTaxRate: number;
    productTaxType: string;
    createdAt: string;
}

export interface StockAudit {
    id: string;
    branchId: string;
    status: 'Pending' | 'Approved' | 'Rejected';
    totalDiscrepancyValue: number;
    approvalRole?: string;
    mraVisible: boolean;
    inventoryLocked: boolean;
    createdBy: string;
    createdAt: string;
    approvedBy?: string;
    approvedAt?: string;
    notes?: string;
}

export interface BusinessSettings {
    id: string;
    enableEis: boolean;
    fuelPumps?: string[];
    productTypes?: string[];
    createdAt: string;
    updatedAt: string;
}

export class HandyPosDatabase extends Dexie {
    inventory!: EntityTable<InventoryItem, 'id'>;
    suppliers!: EntityTable<Supplier, 'id'>;
    purchaseHistory!: EntityTable<PurchaseRecord, 'id'>;
    purchaseOrders!: EntityTable<PurchaseOrder, 'id'>;
    orders!: EntityTable<Order, 'id'>;
    refunds!: EntityTable<Refund, 'id'>;
    takeOrders!: EntityTable<TakeOrder, 'id'>;
    sessions!: EntityTable<Session, 'id'>;
    staff!: EntityTable<Staff, 'id'>;
    stockTakes!: EntityTable<StockTake, 'id'>;
    expenses!: EntityTable<Expense, 'id'>;
    customers!: EntityTable<Customer, 'id'>;
    invoices!: EntityTable<Invoice, 'id'>;
    taxes!: EntityTable<TaxRate, 'id'>;
    business!: EntityTable<Business, 'id'>;
    subscriptions!: EntityTable<Subscription, 'id'>;
    stockTransfers!: EntityTable<StockTransfer, 'id'>;
    wasteLog!: EntityTable<WasteRecord, 'id'>;
    auditLog!: EntityTable<AuditLog, 'id'>;
    cart!: EntityTable<CartItem, 'id'>;
    mraMappings!: EntityTable<MRAMapping, 'id'>;
    inventorySnapshots!: EntityTable<InventorySnapshot, 'id'>;
    stockAudits!: EntityTable<StockAudit, 'id'>;
    businessSettings!: EntityTable<BusinessSettings, 'id'>;


    constructor() {
        super('handypos');

        this.version(36).stores({
            inventory: 'id, name, category, itemType, supplier, status, onMenu, branchId, _dirty, [branchId+itemType], &[branchId+itemType+onMenu]',
            suppliers: 'id, name, businessId, _dirty, [businessId+name]',
            purchaseHistory: '++id, productId, supplierId, receivedDate, branchId, paymentStatus, sessionId, _dirty, &[branchId+receivedDate], [branchId+productId+receivedDate], [sessionId]',
            purchaseOrders: 'id, orderNumber, status, branchId, supplierId, createdAt, _dirty, [branchId+status], [branchId+createdAt]',
            orders: 'id, orderNumber, status, createdAt, updatedAt, branchId, sessionId, _dirty, &[branchId+status+createdAt]',
            refunds: 'id, branchId, orderId, refundedAt, _dirty',
            takeOrders: 'id, orderNumber, status, branchId, createdAt, _dirty, [branchId+status], [branchId+createdAt]',
            sessions: 'id, branchId, status, userId, startedAt, _dirty, [branchId+status], [branchId+userId+status]',
            staff: 'id, name, email, role, branchId',
            stockTakes: 'id, branchId, createdAt, status, _dirty, &[branchId+status]',
            expenses: 'id, branchId, category, date, status, _dirty, [branchId+date]',
            customers: 'id, name, branchId, email',
            invoices: 'id, invoiceNumber, branchId, customerId, status, issueDate',
            taxes: 'id, name, isDefault, businessId, _dirty, [businessId+isDefault]',
            business: 'id',
            subscriptions: 'id, businessId, planId, status',
            stockTransfers: 'id, fromBranchId, toBranchId, itemId, createdAt, _dirty',
            wasteLog: 'id, branchId, itemId, reason, recordedAt, sessionId, _dirty, &[branchId+recordedAt], [sessionId]',
            auditLog: 'id, timestamp, userId, actionType, entityType, entityId, branchId',
            cart: 'id, branchId, savedAt',
            mraMappings: 'id, inventoryItemId, isApproved, mraSynced, createdAt, _dirty',
            inventorySnapshots: 'id, inventoryItemId, branchId, createdAt',
            stockAudits: 'id, branchId, status, createdAt, _dirty',
            businessSettings: 'id',
        });
    }
}

export const db = new HandyPosDatabase();
