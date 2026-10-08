'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { format } from 'date-fns';
import {
  MoreHorizontal,
  AlertTriangle,
  FileText,
  Loader2,
  Printer,
  FilePlus2,
  FileMinus2,
  Ban,
  Download,
} from 'lucide-react';

import { db, type Order, type Business } from '@/lib/db';
import { resolveEisSubmissionFailureReason } from '@/lib/eis-submission';
import { useCurrency } from '@/hooks/use-currency';
import { useAuth } from '@/hooks/use-auth';
import { authFetch } from '@/lib/auth-fetch';
import { useToast } from '@/hooks/use-toast';
import { getOfflineBusinessProfile } from '@/lib/business-profile';
import {
  PRINTER_CONFIG_UPDATED_EVENT,
  normalizePrinterPaperWidth,
  type PrinterPaperWidth,
  type PrinterSettings,
} from '@/lib/services/printer-service';
import { getNextReceiptCopyNumber, markReceiptPrinted } from '@/lib/services/receipt-copy-service';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Receipt } from '@/components/pos/receipt';
import { exportThermalReceiptPdf } from '@/lib/thermal-receipt-pdf';
import { VoidModal } from './void-modal';
import { CreditNoteModal } from './credit-note-modal';
import { DebitNoteModal } from './debit-note-modal';

interface FiscalCorrectionBase {
  id: string;
  eis_status?: string | null;
  eis_sync_state?: string | null;
  eis_submitted_at?: string | null;
  qr_code_payload?: string | null;
  digital_signature?: string | null;
  created_by_name?: string;
  created_at: string;
}

interface CreditNote extends FiscalCorrectionBase {
  credit_note_number: string;
  fiscal_credit_number?: string | null;
  reason: string;
  description: string;
  credit_amount: number | string;
  vat_amount: number | string;
  total_credit: number | string;
}

interface DebitNote extends FiscalCorrectionBase {
  debit_note_number: string;
  fiscal_debit_number?: string | null;
  description: string;
  additional_amount: number | string;
  vat_amount: number | string;
  total_debit: number | string;
}

interface VoidTransaction extends FiscalCorrectionBase {
  void_number: string;
  fiscal_void_number?: string | null;
  void_reason: string;
  reason_description: string;
  voided_amount: number | string;
  voided_vat: number | string;
  refund_method: string;
  refund_amount: number | string;
  refund_processed: boolean;
  refund_processed_at?: string | null;
}

const toFiniteNumber = (value: unknown, fallback = 0): number => {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const normalizeItemTaxType = (value: unknown): 'standard' | 'zero' | 'exempt' => {
  const normalized = String(value ?? '').trim().toLowerCase();
  if (normalized.includes('zero')) return 'zero';
  if (normalized.includes('exempt')) return 'exempt';
  return 'standard';
};

const normalizeTaxMethod = (value: unknown): 'inclusive' | 'exclusive' => {
  return String(value ?? '').trim().toLowerCase() === 'exclusive' ? 'exclusive' : 'inclusive';
};

const resolveDiscountAmount = (value: unknown): number => Math.max(0, toFiniteNumber(value, 0));

const formatDiscountLabel = (item: any): string => {
  const name = toTrimmedString(item?.discount_name ?? item?.discountName) || 'Discount';
  const type = toTrimmedString(item?.discount_type ?? item?.discountType).toLowerCase();
  const rawValue = toFiniteNumber(item?.discount_value ?? item?.discountValue, 0);
  if (rawValue <= 0) return name;
  if (type === 'percentage') return `${name} (${rawValue}%)`;
  if (type === 'fixed') return `${name} (fixed)`;
  return name;
};

const toTrimmedString = (value: unknown): string => {
  if (value === undefined || value === null) {
    return '';
  }
  const trimmed = String(value).trim();
  return trimmed;
};

const normalizeFiscalStatus = (value: unknown, fallback = ''): string => {
  const normalized = toTrimmedString(value).toUpperCase();
  return normalized || fallback;
};

const formatFiscalReceiptStatus = (status: string): string => {
  switch (status) {
    case 'SUBMITTED':
      return 'Fiscal receipt submitted';
    case 'ACCEPTED':
    case 'SUCCESS':
      return 'Fiscal receipt accepted';
    case 'PENDING':
      return 'Fiscal receipt pending';
    case 'REJECTED':
    case 'FAILED':
      return 'Fiscal receipt rejected';
    default:
      return status ? `Fiscal receipt ${status.toLowerCase()}` : 'Fiscal receipt not submitted';
  }
};

const formatCorrectionStatus = (status: string, kind: string): string => {
  const normalizedKind = kind.toLowerCase();
  const documentLabel = normalizedKind.includes('void')
    ? 'Cancellation'
    : normalizedKind.includes('credit')
      ? 'Credit note'
      : normalizedKind.includes('debit')
        ? 'Debit note'
        : 'Correction';

  switch (status) {
    case 'SUBMITTED':
      return `${documentLabel} submitted`;
    case 'ACCEPTED':
    case 'SUCCESS':
      return `${documentLabel} accepted`;
    case 'PENDING':
      return `${documentLabel} pending`;
    case 'REJECTED':
    case 'FAILED':
      return `${documentLabel} rejected`;
    default:
      return status ? `${documentLabel} ${status.toLowerCase()}` : `${documentLabel} not submitted`;
  }
};

const correctionStatusLabel = (kind: string): string => {
  const normalizedKind = kind.toLowerCase();
  if (normalizedKind.includes('void')) return 'Cancellation status';
  if (normalizedKind.includes('credit')) return 'Credit note status';
  if (normalizedKind.includes('debit')) return 'Debit note status';
  return 'Correction status';
};

const resolveOrderItemInventoryId = (item: any): string => {
  const candidates = [
    item?.inventoryItemId,
    item?.inventory_item_id,
    item?.inventoryItem,
    item?.inventory_item,
  ];

  for (const candidate of candidates) {
    const normalized = toTrimmedString(candidate);
    if (normalized) {
      return normalized;
    }
  }

  const rawLineId = toTrimmedString(item?.id);
  if (!rawLineId) {
    return '';
  }
  return rawLineId.split('::cart::')[0] || rawLineId;
};

const normalizeUnitLabel = (value: unknown): string => {
  return toTrimmedString(value);
};

const formatUnitLabel = (unit: string, quantity: number): string => {
  const trimmed = normalizeUnitLabel(unit);
  if (!trimmed) {
    return '';
  }
  if (trimmed.toLowerCase() === 'unit') {
    return quantity === 1 ? 'unit' : 'units';
  }
  return trimmed;
};

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
  const customer = source?.customer ?? {};
  const buyer = source?.buyer ?? {};
  const name = resolveBuyerField(
    source?.customerName,
    source?.customer_name,
    source?.buyerName,
    source?.buyer_name,
    customer?.name,
    customer?.fullName,
    buyer?.name,
    buyer?.fullName
  );
  const phone = resolveBuyerField(
    source?.customerPhone,
    source?.customer_phone,
    source?.buyerPhone,
    source?.buyer_phone,
    customer?.phone,
    customer?.phoneNumber,
    buyer?.phone,
    buyer?.phoneNumber
  );
  const tin = resolveBuyerField(
    source?.customerTin,
    source?.customer_tin,
    source?.buyerTin,
    source?.buyer_tin,
    customer?.tin,
    customer?.taxPin,
    customer?.tax_pin,
    buyer?.tin,
    buyer?.taxPin,
    buyer?.tax_pin
  );

  return {
    name: name || '',
    phone: phone || '',
    tin: tin || '',
  };
};

export default function SaleDetailModal({ order, isOpen, onOpenChange }: { order: Order | null; isOpen: boolean; onOpenChange: (open: boolean) => void }) {
  const { format: formatCurrency } = useCurrency();
  const { user } = useAuth();
  const { toast } = useToast();
  const [voidOpen, setVoidOpen] = useState(false);
  const [creditOpen, setCreditOpen] = useState(false);
  const [debitOpen, setDebitOpen] = useState(false);
  const [voidTransaction, setVoidTransaction] = useState<VoidTransaction | null>(null);
  const [creditNotes, setCreditNotes] = useState<CreditNote[]>([]);
  const [debitNotes, setDebitNotes] = useState<DebitNote[]>([]);
  const [loadingCorrections, setLoadingCorrections] = useState(false);
  const [isPrinting, setIsPrinting] = useState(false);
  const [isExportingReceiptPdf, setIsExportingReceiptPdf] = useState(false);
  const [businessSettings, setBusinessSettings] = useState<Business | null>(null);
  const [inventoryUnitById, setInventoryUnitById] = useState<Record<string, string>>({});
  const [receiptPaperWidth, setReceiptPaperWidth] = useState<PrinterPaperWidth>('80mm');
  const [receiptCopyNumber, setReceiptCopyNumber] = useState(1);
  const [receiptDisplaySettings, setReceiptDisplaySettings] = useState({
    showHeader: true,
    showFooter: true,
    showQRCode: true,
    showItemDetails: true,
    showTaxBreakdown: true,
  });

  const applyPrinterSettingsToReceipt = useCallback(
    (
      settings?: Partial<PrinterSettings> | null,
      fallbackPaperWidth: PrinterPaperWidth = '80mm'
    ): PrinterPaperWidth => {
      const resolvedPaperWidth = normalizePrinterPaperWidth(
        settings?.receiptPaperWidth,
        fallbackPaperWidth
      );

      setReceiptPaperWidth(resolvedPaperWidth);
      setReceiptDisplaySettings({
        showHeader: settings?.printHeader ?? true,
        showFooter: settings?.printFooter ?? true,
        showQRCode: settings?.printQRCode ?? true,
        showItemDetails: settings?.printItemDetails ?? true,
        showTaxBreakdown: settings?.printTaxBreakdown ?? true,
      });

      return resolvedPaperWidth;
    },
    []
  );

  const refreshPrinterSettings = useCallback(async () => {
    const activeBranchId = localStorage.getItem('handypos-active-branch') || 'main';
    const { printerService } = await import('@/lib/services/printer-service');
    const [printerSettings, defaultPrinter] = await Promise.all([
      printerService.getPrinterSettings(activeBranchId),
      printerService.getDefaultPrinter(activeBranchId),
    ]);

    applyPrinterSettingsToReceipt(
      printerSettings,
      normalizePrinterPaperWidth(defaultPrinter?.paperWidth)
    );
  }, [applyPrinterSettingsToReceipt]);

  const refreshCorrectionRecords = useCallback(async () => {
    if (!order) {
      setVoidTransaction(null);
      setCreditNotes([]);
      setDebitNotes([]);
      return;
    }

    setLoadingCorrections(true);
    setVoidTransaction(null);
    setCreditNotes([]);
    setDebitNotes([]);
    try {
      const [voids, credits, debits] = await Promise.all([
        authFetch.fetch<VoidTransaction[]>(`/sessions/void-transactions/by_order/?order_id=${order.id}`),
        authFetch.fetch<CreditNote[]>(`/sessions/credit-notes/by_order/?order_id=${order.id}`),
        authFetch.fetch<DebitNote[]>(`/sessions/debit-notes/by_order/?order_id=${order.id}`),
      ]);

      setVoidTransaction(Array.isArray(voids) && voids.length > 0 ? voids[0] : null);
      setCreditNotes(Array.isArray(credits) ? credits : []);
      setDebitNotes(Array.isArray(debits) ? debits : []);
    } catch (error) {
      console.error('Error fetching EIS correction records:', error);
      setVoidTransaction(null);
      setCreditNotes([]);
      setDebitNotes([]);
    } finally {
      setLoadingCorrections(false);
    }
  }, [order]);

  // Fetch business settings and EIS correction details
  useEffect(() => {
    let isMounted = true;

    if (isOpen) {
      // Fetch business settings
      getOfflineBusinessProfile().then((business) => {
        if (isMounted) {
          setBusinessSettings(business || null);
        }
      });

      const activeBranchId = localStorage.getItem('handypos-active-branch') || 'main';
      import('@/lib/services/printer-service')
        .then(async ({ printerService }) => {
          const [printerSettings, defaultPrinter] = await Promise.all([
            printerService.getPrinterSettings(activeBranchId),
            printerService.getDefaultPrinter(activeBranchId),
          ]);

          if (isMounted) {
            applyPrinterSettingsToReceipt(
              printerSettings,
              normalizePrinterPaperWidth(defaultPrinter?.paperWidth)
            );
          }
        })
        .catch((error) => {
          console.warn('Error loading printer settings:', error);
        });

      void refreshCorrectionRecords();
    }

    return () => {
      isMounted = false;
    };
  }, [order, isOpen, applyPrinterSettingsToReceipt, refreshCorrectionRecords]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const handlePrinterUpdate = (event: Event) => {
      const customEvent = event as CustomEvent<{ branchId?: string }>;
      const activeBranchId = localStorage.getItem('handypos-active-branch') || 'main';
      const updatedBranchId = String(customEvent.detail?.branchId || '').trim();
      if (updatedBranchId && updatedBranchId !== activeBranchId) {
        return;
      }

      void refreshPrinterSettings().catch((error) => {
        console.warn('Error refreshing printer settings after update:', error);
      });
    };

    window.addEventListener(PRINTER_CONFIG_UPDATED_EVENT, handlePrinterUpdate);
    return () => window.removeEventListener(PRINTER_CONFIG_UPDATED_EVENT, handlePrinterUpdate);
  }, [isOpen, refreshPrinterSettings]);

  useEffect(() => {
    let isMounted = true;

    if (!isOpen || !order) {
      setInventoryUnitById({});
      return () => {
        isMounted = false;
      };
    }

    const inventoryIds = Array.from(
      new Set(
        order.items
          .map((item) => resolveOrderItemInventoryId(item))
          .filter((id) => Boolean(id))
      )
    );

    if (inventoryIds.length === 0) {
      setInventoryUnitById({});
      return () => {
        isMounted = false;
      };
    }

    db.inventory
      .bulkGet(inventoryIds)
      .then((items) => {
        if (!isMounted) {
          return;
        }
        const nextMap: Record<string, string> = {};
        items.forEach((inventoryItem, index) => {
          const id = inventoryIds[index];
          if (!inventoryItem || !id) {
            return;
          }
          const unitLabel =
            normalizeUnitLabel((inventoryItem as any).unitType) ||
            normalizeUnitLabel((inventoryItem as any).unit_type) ||
            normalizeUnitLabel((inventoryItem as any).unit);
          if (unitLabel) {
            nextMap[id] = unitLabel;
          }
        });
        setInventoryUnitById(nextMap);
      })
      .catch((error) => {
        console.warn('Error loading inventory units:', error);
        if (isMounted) {
          setInventoryUnitById({});
        }
      });

    return () => {
      isMounted = false;
    };
  }, [isOpen, order]);

  if (!order) return null;

  const buyerDetails = resolveBuyerDetails(order);
  const buyerNameDisplay = buyerDetails.name || 'Walk-in Customer';
  const buyerPhoneDisplay = buyerDetails.phone || 'N/A';
  const buyerTinDisplay = buyerDetails.tin || 'N/A';
  const fiscalInvoiceNumber = toTrimmedString(
    (order as any).fiscalInvoiceNumber ?? (order as any).fiscal_invoice_number
  );
  const eisStatusDisplay = normalizeFiscalStatus((order as any).eisStatus ?? (order as any).eis_status);
  const fiscalReceiptStatusText = formatFiscalReceiptStatus(eisStatusDisplay);
  const eisFailureReason = resolveEisSubmissionFailureReason(order);
  const orderSyncStatus = toTrimmedString((order as any).syncStatus ?? (order as any).sync_status).toLowerCase();

  const isVoided = order.status === 'Voided' || order.status === 'Cancelled' || Boolean(voidTransaction);
  const isFiscalLocked = Boolean((order as any).is_fiscal_locked ?? (order as any).isFiscalLocked);
  const isAdminUser = String(user?.role || '').toLowerCase() === 'admin';
  const isEisCorrectionReady =
    !eisStatusDisplay || ['SUBMITTED', 'ACCEPTED'].includes(eisStatusDisplay);
  const correctionActionDisabled = isFiscalLocked && !isEisCorrectionReady;
  const correctionDisabledTitle =
    correctionActionDisabled ? 'Original EIS sale must be submitted or accepted before correction.' : undefined;
  const normalizedItems = order.items.map((item) => {
    const itemTaxRate = toFiniteNumber(item.tax_rate ?? item.taxRate, 0);
    const itemTaxType = normalizeItemTaxType(item.tax_type ?? item.taxType);
    const itemTaxMethod = normalizeTaxMethod(
      item.tax_calculation_method ?? item.taxCalculationMethod
    );
    const itemPrice = toFiniteNumber(item.price, 0);
    const itemQuantity = toFiniteNumber(item.quantity, 1);
    const itemSubtotal = toFiniteNumber(item.subtotal, itemPrice * itemQuantity);
    const itemDiscountAmount = resolveDiscountAmount(item.discount_amount ?? item.discountAmount);
    const itemDiscountLabel = itemDiscountAmount > 0 ? formatDiscountLabel(item) : '';
    const itemTaxAmount = toFiniteNumber(item.tax_amount ?? item.taxAmount, 0);
    const itemTotal = toFiniteNumber(item.total, itemSubtotal + itemTaxAmount);
    const directUnit =
      normalizeUnitLabel((item as any).unitType) ||
      normalizeUnitLabel((item as any).unit_type) ||
      normalizeUnitLabel((item as any).unit) ||
      normalizeUnitLabel((item as any).mraUnitMeasure) ||
      normalizeUnitLabel((item as any).mra_unit_measure);
    const resolvedInventoryId = resolveOrderItemInventoryId(item);
    const inventoryUnit = resolvedInventoryId ? inventoryUnitById[resolvedInventoryId] : '';
    const unitLabel = formatUnitLabel(
      directUnit || inventoryUnit || 'unit',
      itemQuantity
    );

    return {
      ...item,
      itemTaxRate,
      itemTaxType,
      itemTaxMethod,
      itemPrice,
      itemQuantity,
      itemSubtotal,
      itemDiscountAmount,
      itemDiscountLabel,
      itemTaxAmount,
      itemTotal,
      unitLabel,
    };
  });
  const orderDiscountAmount = resolveDiscountAmount(
    (order as any).discount_amount ?? (order as any).discountAmount
  );
  const itemDiscountTotal = normalizedItems.reduce((sum, item) => sum + item.itemDiscountAmount, 0);
  const totalDiscountAmount = Math.max(orderDiscountAmount, itemDiscountTotal);

  const voidEisStatus = normalizeFiscalStatus(
    voidTransaction?.eis_status || voidTransaction?.eis_sync_state || ''
  );
  const voidEisIsConfirmed = ['SUBMITTED', 'ACCEPTED', 'SUCCESS'].includes(voidEisStatus);
  const voidEisIsFailed = ['REJECTED', 'FAILED'].includes(voidEisStatus);
  const voidEisFiscalNumber = toTrimmedString(voidTransaction?.fiscal_void_number);
  const voidEisStatusText = voidTransaction
    ? formatCorrectionStatus(voidEisStatus || 'PENDING', 'Void Sale')
    : 'Cancellation not recorded';

  const correctionRows = [
    ...creditNotes.map((note) => ({
      id: note.id,
      kind: 'Credit Note',
      number: note.credit_note_number,
      fiscalNumber: note.fiscal_credit_number,
      status: note.eis_status,
      syncState: note.eis_sync_state,
      amount: note.total_credit,
      createdAt: note.created_at,
      description: note.reason.replace(/_/g, ' '),
      tone: 'text-emerald-700 dark:text-emerald-300',
    })),
    ...debitNotes.map((note) => ({
      id: note.id,
      kind: 'Debit Note',
      number: note.debit_note_number,
      fiscalNumber: note.fiscal_debit_number,
      status: note.eis_status,
      syncState: note.eis_sync_state,
      amount: note.total_debit,
      createdAt: note.created_at,
      description: 'Additional charge',
      tone: 'text-orange-700 dark:text-orange-300',
    })),
    ...(voidTransaction
      ? [
          {
            id: voidTransaction.id,
            kind: 'Void Sale',
            number: voidTransaction.void_number,
            fiscalNumber: voidTransaction.fiscal_void_number,
            status: voidTransaction.eis_status,
            syncState: voidTransaction.eis_sync_state,
            amount: voidTransaction.voided_amount,
            createdAt: voidTransaction.created_at,
            description: voidTransaction.void_reason.replace(/_/g, ' '),
            tone: 'text-destructive',
          },
        ]
      : []),
  ].sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());

  const handlePrintReceipt = async () => {
    try {
      setIsPrinting(true);
      const { printerService } = await import('@/lib/services/printer-service');
      const { silentPrintService } = await import('@/lib/services/silent-print-service');
      const orderId = String(order?.id ?? '').trim();

      const activeBranchId = localStorage.getItem('handypos-active-branch') || 'main';
      const [settings, defaultPrinter] = await Promise.all([
        printerService.getPrinterSettings(activeBranchId),
        printerService.getDefaultPrinter(activeBranchId),
      ]);
      const selectedPaperWidth = applyPrinterSettingsToReceipt(
        settings,
        normalizePrinterPaperWidth(defaultPrinter?.paperWidth)
      );
      
      if (!defaultPrinter) {
        console.error('No default printer configured');
        toast({
          variant: 'destructive',
          title: 'No Printer Configured',
          description: 'Please configure a printer in Settings → Printers before printing.',
        });
        setIsPrinting(false);
        return;
      }

      const configuredCopies = Number.isFinite(Number(settings.printCopies))
        ? Number(settings.printCopies)
        : 1;
      const copiesToPrint = Math.max(1, Math.floor(configuredCopies));
      const startingCopyNumber = getNextReceiptCopyNumber(orderId);
      const isBluetoothPrinter =
        defaultPrinter.connectionType === 'bluetooth' ||
        String(defaultPrinter.id || '').toLowerCase().startsWith('bt:');
      const printAttemptTimeoutMs = isBluetoothPrinter ? 60000 : 20000;

      toast({
        title: 'Printing...',
        description: `Sending ${copiesToPrint} receipt${copiesToPrint > 1 ? 's' : ''} to ${defaultPrinter.name}`,
      });

      // Try silent printing first (works with Tauri/Electron or auto-submit)
      const availableMethods = silentPrintService.getAvailableMethods();
      console.log('[Print] Available print methods:', availableMethods);

      let printedCopies = 0;
      let failedResult: { timedOut: boolean } | null = null;

      for (let copyIndex = 0; copyIndex < copiesToPrint; copyIndex += 1) {
        const currentCopyNumber = startingCopyNumber + copyIndex;
        setReceiptCopyNumber(currentCopyNumber);

        // Wait for receipt to re-render with updated ORIGINAL/COPY marker.
        await new Promise((resolve) => setTimeout(resolve, 100));

        const receiptElement = document.getElementById('receipt-printable-area');
        const printContents = receiptElement?.outerHTML || receiptElement?.innerHTML;

        if (!printContents || printContents.trim().length === 0) {
          console.error('Receipt content not found or empty');
          failedResult = { timedOut: false };
          break;
        }

        const printAttempt = Promise.race([
          silentPrintService.printSilentlyViaSystem(printContents, {
            printerName: defaultPrinter.name,
            printerId: defaultPrinter.id,
            copies: 1,
            paperSize: selectedPaperWidth,
            printerPaperSize: selectedPaperWidth,
          }).then((success) => ({ success, timedOut: false })),
          new Promise<{ success: false; timedOut: true }>((resolve) =>
            setTimeout(() => resolve({ success: false, timedOut: true }), printAttemptTimeoutMs)
          ),
        ]);

        const result = await printAttempt;
        if (!result.success) {
          failedResult = { timedOut: result.timedOut };
          break;
        }

        printedCopies += 1;
      }

      if (printedCopies > 0) {
        markReceiptPrinted(orderId, printedCopies);
      }

      const isCompleteSuccess = printedCopies === copiesToPrint && failedResult === null;
      if (isCompleteSuccess) {
        const printedTypeLabel = startingCopyNumber > 1 ? 'Receipt copy printed' : 'Original receipt printed';
        toast({
          title: 'Print Successful',
          description: copiesToPrint > 1
            ? `${printedCopies} receipts sent to ${defaultPrinter.name}`
            : `${printedTypeLabel} to ${defaultPrinter.name}`,
        });
      } else {
        console.warn('Print failed');
        const failedDescription = failedResult?.timedOut
          ? 'Printer timed out.'
          : 'Print failed.';
        toast({
          variant: 'destructive',
          title: failedResult?.timedOut ? 'Print Timed Out' : 'Print Failed',
          description: printedCopies > 0
            ? `${printedCopies} printed. ${failedDescription}`
            : failedDescription,
        });
      }
    } catch (error) {
      console.error('Error printing receipt:', error);
      toast({
        variant: 'destructive',
        title: 'Print Error',
        description: error instanceof Error ? error.message : 'An unknown error occurred',
      });
    } finally {
      setIsPrinting(false);
    }
  };

  const handleExportReceiptPdf = async () => {
    if (!order) {
      return;
    }

    try {
      setIsExportingReceiptPdf(true);
      setReceiptCopyNumber(1);
      await new Promise((resolve) => setTimeout(resolve, 120));
      const orderNumber = String(
        (order as any).fiscalInvoiceNumber ??
        (order as any).fiscal_invoice_number ??
        (order as any).orderNumber ??
        (order as any).order_number ??
        'receipt'
      ).trim();
      const result = await exportThermalReceiptPdf({
        elementId: 'receipt-printable-area',
        paperWidth: receiptPaperWidth,
        filename: `handypos-receipt-${orderNumber}`,
      });
      toast({
        title: 'Receipt PDF Exported',
        description: result.location === 'tauri'
          ? `Saved to ${result.path}`
          : `${result.filename} downloaded.`,
      });
    } catch (error) {
      console.error('[Receipt PDF] Export failed:', error);
      toast({
        variant: 'destructive',
        title: 'PDF Export Failed',
        description: 'Could not export the thermal receipt PDF.',
      });
    } finally {
      setIsExportingReceiptPdf(false);
    }
  };

  return (
    <>
      <Dialog open={isOpen} onOpenChange={onOpenChange}>
        <DialogContent className="flex max-h-[95vh] max-h-[95dvh] w-[calc(100vw-0.75rem)] max-w-4xl flex-col overflow-hidden p-4 sm:w-[95vw] sm:p-6">
          <DialogHeader className="gap-3 pr-8 text-left sm:flex-row sm:items-start sm:justify-between">
            <div className="flex-1">
              <DialogTitle>Order #{order.orderNumber} Details</DialogTitle>
              <DialogDescription>
                {format(new Date(order.createdAt), 'PPpp')}
              </DialogDescription>
            </div>
            <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:flex-nowrap sm:justify-end">
              <Button 
                variant="outline" 
                size="sm" 
                className="h-8 flex-1 sm:h-9 sm:flex-none"
                onClick={handlePrintReceipt}
                disabled={isPrinting}
              >
                <Printer className="mr-2 h-4 w-4" />
                {isPrinting ? 'Printing...' : 'Print'}
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-8 flex-1 sm:h-9 sm:flex-none"
                onClick={handleExportReceiptPdf}
                disabled={isExportingReceiptPdf}
              >
                <Download className="mr-2 h-4 w-4" />
                {isExportingReceiptPdf ? 'Exporting...' : 'PDF'}
              </Button>
              {!isVoided && isAdminUser && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" size="sm" className="h-8 w-8 shrink-0 p-0">
                      <MoreHorizontal className="h-4 w-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-56">
                    <DropdownMenuItem
                      onClick={() => setCreditOpen(true)}
                      disabled={correctionActionDisabled}
                      title={correctionDisabledTitle}
                    >
                      <FileMinus2 className="mr-2 h-4 w-4" />
                      <span>Create Credit Note</span>
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={() => setDebitOpen(true)}
                      disabled={correctionActionDisabled}
                      title={correctionDisabledTitle}
                    >
                      <FilePlus2 className="mr-2 h-4 w-4" />
                      <span>Create Debit Note</span>
                    </DropdownMenuItem>
                    <DropdownMenuItem 
                      onClick={() => setVoidOpen(true)} 
                      disabled={correctionActionDisabled}
                      title={correctionDisabledTitle}
                      className="text-red-600"
                    >
                      <Ban className="mr-2 h-4 w-4" />
                      <span>Void Sale</span>
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </div>
          </DialogHeader>

          <div className="space-y-6 overflow-y-auto pb-1">
            {/* Status Badges */}
            {isVoided && (
              <div className={voidEisIsFailed ? 'rounded-lg border border-destructive/30 bg-destructive/10 p-3' : voidEisIsConfirmed ? 'rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3' : 'rounded-lg border border-amber-500/30 bg-amber-500/10 p-3'}>
                <div className="flex items-start gap-2">
                  <AlertTriangle className={voidEisIsFailed ? 'mt-0.5 h-4 w-4 text-destructive' : voidEisIsConfirmed ? 'mt-0.5 h-4 w-4 text-emerald-600 dark:text-emerald-400' : 'mt-0.5 h-4 w-4 text-amber-600 dark:text-amber-400'} />
                  <div className="min-w-0 text-sm">
                    <p className={voidEisIsFailed ? 'font-semibold text-destructive' : voidEisIsConfirmed ? 'font-semibold text-emerald-700 dark:text-emerald-300' : 'font-semibold text-amber-700 dark:text-amber-300'}>
                      Local sale is voided. {voidEisStatusText}
                    </p>
                    {voidTransaction ? (
                      <div className="mt-1 space-y-0.5 text-xs text-muted-foreground">
                        <p>Void record: #{voidTransaction.void_number}</p>
                        <p>Original receipt: {fiscalInvoiceNumber || 'N/A'}</p>
                        {voidEisFiscalNumber && <p className="break-all">Cancellation fiscal number: {voidEisFiscalNumber}</p>}
                        {!voidEisIsConfirmed && !voidEisIsFailed && (
                          <p>The cancellation may not show until the pending retry is accepted.</p>
                        )}
                        {voidEisIsFailed && (
                          <p>Retry correction.</p>
                        )}
                        {voidEisIsConfirmed && (
                          <p>Cancellation submitted separately.</p>
                        )}
                      </div>
                    ) : (
                      <p className="mt-1 text-xs text-muted-foreground">No linked void record was loaded yet.</p>
                    )}
                  </div>
                </div>
              </div>
            )}

            {isFiscalLocked && (
              <div className="bg-blue-500/10 dark:bg-blue-500/20 border border-blue-500/30 p-3 rounded-lg flex items-center gap-2">
                <FileText className="h-4 w-4 text-blue-600 dark:text-blue-400" />
                <span className="text-sm font-medium text-blue-700 dark:text-blue-300">Original legal receipt is locked after submission</span>
              </div>
            )}

            {/* Items Table with Tax Details */}
            <div>
              <h3 className="font-semibold mb-3">Items & Tax Details</h3>
              <div className="space-y-3 sm:hidden">
                {normalizedItems.map((item, idx) => (
                  <Card key={`mobile-item-${idx}`} className="border">
                    <CardContent className="space-y-3 p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="font-medium break-words">{item.name}</p>
                          <p className="text-xs text-muted-foreground">
                            Qty: {item.itemQuantity} {item.unitLabel}
                          </p>
                          {item.notes && (
                            <p className="mt-1 text-xs text-muted-foreground break-words">{item.notes}</p>
                          )}
                        </div>
                        <p className="text-sm font-bold whitespace-nowrap">{formatCurrency(item.itemTotal)}</p>
                      </div>

                      <div className="grid grid-cols-2 gap-2 text-xs">
                        <div className="rounded-md bg-muted p-2">
                          <p className="text-muted-foreground">Unit Price</p>
                          <p className="font-medium">{formatCurrency(item.itemPrice)}</p>
                        </div>
                        <div className="rounded-md bg-muted p-2">
                          <p className="text-muted-foreground">Subtotal</p>
                          <p className="font-medium">{formatCurrency(item.itemSubtotal)}</p>
                        </div>
                      </div>

                      {item.itemDiscountAmount > 0 && (
                        <div className="flex items-center justify-between rounded-md bg-amber-50 p-2 text-xs text-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
                          <span>{item.itemDiscountLabel}</span>
                          <span className="font-semibold">{formatCurrency(item.itemDiscountAmount)}</span>
                        </div>
                      )}

                      <div className="flex flex-wrap items-center gap-1.5 text-xs">
                        <span className="rounded-full bg-blue-100 px-2 py-1 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
                          {item.itemTaxRate}%
                        </span>
                        <span className="rounded-full bg-muted px-2 py-1">
                          {item.itemTaxType === 'standard'
                            ? 'Standard'
                            : item.itemTaxType === 'zero'
                              ? 'Zero'
                              : 'Exempt'}
                        </span>
                        <span className="rounded-full bg-blue-100 px-2 py-1 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
                          {item.itemTaxMethod === 'exclusive' ? 'Exclusive' : 'Inclusive'}
                        </span>
                      </div>

                      <div className="flex items-center justify-between border-t pt-2 text-sm">
                        <span className="text-muted-foreground">Item Tax</span>
                        <span className="font-semibold text-green-600 dark:text-green-400">
                          {formatCurrency(item.itemTaxAmount)}
                        </span>
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>

              <div className="hidden sm:block">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Item Name & Qty</TableHead>
                      <TableHead className="text-right">Unit Price</TableHead>
                      <TableHead className="text-right">Subtotal</TableHead>
                      <TableHead className="text-right">Discount</TableHead>
                      <TableHead className="text-right">Tax Details</TableHead>
                      <TableHead className="text-right">Item Tax</TableHead>
                      <TableHead className="text-right">Item Total</TableHead>
                    </TableRow>
                  </TableHeader>
                    <TableBody>
                    {normalizedItems.map((item, idx) => {
                      return (
                        <TableRow key={idx}>
                          <TableCell className="font-medium">
                            <div>
                              <p>{item.name}</p>
                              <p className="text-xs text-muted-foreground">
                                Qty: {item.itemQuantity} {item.unitLabel}
                              </p>
                              {item.notes && <p className="text-xs text-muted-foreground mt-1">{item.notes}</p>}
                            </div>
                          </TableCell>
                          <TableCell className="text-right">{formatCurrency(item.itemPrice)}</TableCell>
                          <TableCell className="text-right">{formatCurrency(item.itemSubtotal)}</TableCell>
                          <TableCell className="text-right">
                            {item.itemDiscountAmount > 0 ? (
                              <div className="space-y-1">
                                <div className="font-medium text-amber-700 dark:text-amber-300">{formatCurrency(item.itemDiscountAmount)}</div>
                                <div className="text-xs text-muted-foreground">{item.itemDiscountLabel}</div>
                              </div>
                            ) : (
                              <span className="text-muted-foreground">-</span>
                            )}
                          </TableCell>
                          <TableCell className="text-right">
                            <div className="space-y-1">
                              <div className="text-blue-600 dark:text-blue-400 font-medium">{item.itemTaxRate}%</div>
                              <div className="text-xs px-2 py-1 rounded-full bg-muted inline-block">
                                {item.itemTaxType === 'standard' ? 'Standard' : item.itemTaxType === 'zero' ? 'Zero' : 'Exempt'}
                              </div>
                              <div className="text-xs px-2 py-1 rounded-full bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 inline-block ml-1">
                                {item.itemTaxMethod === 'exclusive' ? 'Exclusive' : 'Inclusive'}
                              </div>
                            </div>
                          </TableCell>
                          <TableCell className="text-right font-medium text-green-600 dark:text-green-400">{formatCurrency(item.itemTaxAmount)}</TableCell>
                          <TableCell className="text-right font-bold">{formatCurrency(item.itemTotal)}</TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            </div>

            {/* Payment & Status Info */}
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <p className="text-sm text-muted-foreground">Payment Method</p>
                <p className="font-semibold">{order.paymentMethod}</p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Status</p>
                <p className="font-semibold capitalize">{order.status}</p>
              </div>
            </div>

            {/* Buyer Details */}
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Buyer Details</CardTitle>
              </CardHeader>
              <CardContent className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div>
                  <p className="text-sm text-muted-foreground">Name</p>
                  <p className="font-semibold">{buyerNameDisplay}</p>
                </div>
                <div>
                  <p className="text-sm text-muted-foreground">Phone</p>
                  <p className="font-semibold">{buyerPhoneDisplay}</p>
                </div>
                <div>
                  <p className="text-sm text-muted-foreground">TIN</p>
                  <p className="font-semibold">{buyerTinDisplay}</p>
                </div>
              </CardContent>
            </Card>

            {/* Tax Breakdown */}
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Tax Breakdown</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Subtotal:</span>
                  <span className="font-medium">{formatCurrency(order.subtotal)}</span>
                </div>
                {totalDiscountAmount > 0 && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Total discount:</span>
                    <span className="font-medium text-amber-700 dark:text-amber-300">{formatCurrency(totalDiscountAmount)}</span>
                  </div>
                )}
                
                <div className="border-t pt-3 space-y-2">
                  {/* Group items by tax rate and calculation method */}
                  {(() => {
                    const taxBreakdown: Record<string, { count: number; taxAmount: number; method: string }> = {};
                    
                    order.items.forEach((item) => {
                      const taxRate = toFiniteNumber(item.tax_rate ?? item.taxRate, 0);
                      const taxAmount = toFiniteNumber(item.tax_amount ?? item.taxAmount, 0);
                      const taxMethod = normalizeTaxMethod(
                        item.tax_calculation_method ?? item.taxCalculationMethod
                      );
                      const rateKey = `${taxRate}-${taxMethod}`;
                      
                      if (!taxBreakdown[rateKey]) {
                        taxBreakdown[rateKey] = { count: 0, taxAmount: 0, method: taxMethod };
                      }
                      taxBreakdown[rateKey].count += 1;
                      taxBreakdown[rateKey].taxAmount += taxAmount;
                    });
                    
                    return Object.entries(taxBreakdown)
                      .sort(([keyA], [keyB]) => {
                        const rateA = parseFloat(keyA.split('-')[0]);
                        const rateB = parseFloat(keyB.split('-')[0]);
                        return rateB - rateA;
                      })
                      .map(([key, data]) => {
                        const rate = key.split('-')[0];
                        const methodLabel = data.method === 'exclusive' ? 'Exclusive' : 'Inclusive';
                        return (
                          <div key={key} className="flex items-start justify-between gap-3 text-sm">
                            <div className="min-w-0 text-muted-foreground">
                              <div>
                                Tax @ {parseFloat(rate).toFixed(2)}% ({data.count} item{data.count !== 1 ? 's' : ''})
                              </div>
                              <div className="text-xs text-muted-foreground/70 ml-2">
                                {methodLabel}
                              </div>
                            </div>
                            <span className="font-medium text-blue-600 dark:text-blue-400">
                              {formatCurrency(data.taxAmount)}
                            </span>
                          </div>
                        );
                      });
                  })()}
                  
                  <div className="flex justify-between pt-2 border-t">
                    <span className="text-muted-foreground font-medium">Total VAT:</span>
                    <span className="font-bold text-blue-600 dark:text-blue-400">{formatCurrency(order.vatAmount || order.tax || 0)}</span>
                  </div>
                </div>

                <div className="border-t pt-3">
                  <div className="flex justify-between text-lg font-bold">
                    <span>Total Amount:</span>
                    <span>{formatCurrency(order.total)}</span>
                  </div>
                </div>

              </CardContent>
            </Card>

            {/* COGS */}
            {order.cogs > 0 && (
              <div className="bg-muted p-3 rounded-md">
                <p className="text-sm text-muted-foreground">Cost of Goods Sold (COGS)</p>
                <p className="font-semibold">{formatCurrency(order.cogs)}</p>
              </div>
            )}

            {/* MRA EIS Status */}
            {(eisStatusDisplay || eisFailureReason || orderSyncStatus === 'failed') && (
              <div className="bg-blue-500/10 dark:bg-blue-500/20 p-3 rounded-md border border-blue-500/30">
                <p className="text-sm text-muted-foreground">Fiscal Receipt Submission</p>
                <p className={`font-semibold ${eisFailureReason ? 'text-red-700 dark:text-red-300' : 'text-blue-700 dark:text-blue-300'}`}>
                  {eisStatusDisplay ? fiscalReceiptStatusText : 'Fiscal receipt submission failed'}
                </p>
                {fiscalInvoiceNumber && (
                  <p className="text-xs text-muted-foreground mt-1">Original fiscal receipt: {fiscalInvoiceNumber}</p>
                )}
                {eisFailureReason && (
                  <p className="text-xs text-red-700 dark:text-red-300 mt-1">
                    Reason: {eisFailureReason}
                  </p>
                )}
                {eisStatusDisplay === 'PENDING' && (
                  <p className="text-xs text-blue-700/80 dark:text-blue-300 mt-1">
                    Original sale receipt is pending sync to EIS. It is separate from any cancellation or correction status.
                  </p>
                )}
              </div>
            )}

            {/* Receipt Preview */}
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Receipt Preview</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="overflow-x-auto rounded-md border bg-muted/30 p-3">
                  <div className="flex min-w-max justify-center">
                    <Receipt
                      order={order}
                      business={businessSettings}
                      currencyFormatter={formatCurrency}
                      paperWidth={receiptPaperWidth}
                      showHeader={receiptDisplaySettings.showHeader}
                      showFooter={receiptDisplaySettings.showFooter}
                      showQRCode={receiptDisplaySettings.showQRCode}
                      showItemDetails={receiptDisplaySettings.showItemDetails}
                      showTaxBreakdown={receiptDisplaySettings.showTaxBreakdown}
                      copyNumber={1}
                      elementId={`receipt-preview-${order.id}`}
                      enablePrintStyles={false}
                    />
                  </div>
                </div>
              </CardContent>
            </Card>

            {(loadingCorrections || correctionRows.length > 0) && (
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Corrections</CardTitle>
                  <CardDescription>Fiscal credit, debit, and void documents linked to this sale.</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  {loadingCorrections ? (
                    <div className="flex items-center justify-center py-4">
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      <span className="text-sm text-muted-foreground">Loading correction records...</span>
                    </div>
                  ) : (
                    correctionRows.map((row) => {
                      const rawStatus = normalizeFiscalStatus(row.status || row.syncState || 'PENDING', 'PENDING');
                      const statusText = formatCorrectionStatus(rawStatus, row.kind);
                      const statusLabel = correctionStatusLabel(row.kind);
                      const fiscalNumber = toTrimmedString(row.fiscalNumber) || 'Pending fiscal number';
                      return (
                        <div
                          key={`${row.kind}-${row.id}`}
                          className="rounded-md border p-3"
                        >
                          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                            <div className="min-w-0">
                              <p className={`text-sm font-semibold ${row.tone}`}>
                                {row.kind} #{row.number}
                              </p>
                              <p className="text-xs capitalize text-muted-foreground">{row.description}</p>
                            </div>
                            <div className="text-left sm:text-right">
                              <p className="text-sm font-semibold">{formatCurrency(toFiniteNumber(row.amount))}</p>
                              <p className="text-xs text-muted-foreground">{format(new Date(row.createdAt), 'PPp')}</p>
                            </div>
                          </div>
                          <div className="mt-3 grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
                            <div>
                              <span className="text-muted-foreground">Correction fiscal no: </span>
                              <span className="font-medium">{fiscalNumber}</span>
                            </div>
                            <div>
                              <span className="text-muted-foreground">{statusLabel}: </span>
                              <span className="font-medium">{statusText}</span>
                            </div>
                          </div>
                        </div>
                      );
                    })
                  )}
                </CardContent>
              </Card>
            )}

            {/* Void Transaction Details */}
            {isVoided && (
              <Card className="border-destructive/30 bg-destructive/5">
                <CardHeader>
                  <CardTitle className="text-base text-destructive">Void Transaction Details</CardTitle>
                </CardHeader>
                <CardContent className="space-y-4">
                  {loadingCorrections ? (
                    <div className="flex items-center justify-center py-4">
                      <Loader2 className="h-4 w-4 animate-spin mr-2" />
                      <span className="text-sm text-muted-foreground">Loading void details...</span>
                    </div>
                  ) : voidTransaction ? (
                    <>
                      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                        <div>
                          <p className="text-xs text-muted-foreground">Void Number</p>
                          <p className="font-semibold text-destructive">{voidTransaction.void_number}</p>
                        </div>
                        <div>
                          <p className="text-xs text-muted-foreground">Void Reason</p>
                          <p className="font-semibold text-destructive capitalize">{voidTransaction.void_reason.replace(/_/g, ' ')}</p>
                        </div>
                      </div>

                      <div>
                        <p className="text-xs text-muted-foreground">Reason Description</p>
                        <p className="text-sm text-destructive">{voidTransaction.reason_description}</p>
                      </div>

                      <div className="grid grid-cols-1 gap-4 border-t border-destructive/30 pt-3 sm:grid-cols-2">
                        <div>
                          <p className="text-xs text-muted-foreground">Cancellation Fiscal Number</p>
                          <p className="font-semibold text-destructive">
                            {toTrimmedString(voidTransaction.fiscal_void_number) || 'Pending fiscal number'}
                          </p>
                        </div>
                        <div>
                          <p className="text-xs text-muted-foreground">Cancellation Status</p>
                          <p className="font-semibold text-destructive">
                            {formatCorrectionStatus(normalizeFiscalStatus(voidTransaction.eis_status || voidTransaction.eis_sync_state || 'PENDING', 'PENDING'), 'Void Sale')}
                          </p>
                        </div>
                      </div>

                      <div className="border-t border-destructive/30 pt-3 space-y-2">
                        <div className="flex justify-between">
                          <span className="text-xs text-muted-foreground">Voided Amount:</span>
                          <span className="font-medium text-destructive">{formatCurrency(toFiniteNumber(voidTransaction.voided_amount))}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-xs text-muted-foreground">Voided VAT:</span>
                          <span className="font-medium text-destructive">{formatCurrency(toFiniteNumber(voidTransaction.voided_vat))}</span>
                        </div>
                      </div>

                      <div className="border-t border-destructive/30 pt-3 space-y-2">
                        <div className="flex justify-between">
                          <span className="text-xs text-muted-foreground">Refund Method:</span>
                          <span className="font-medium text-destructive capitalize">{voidTransaction.refund_method}</span>
                        </div>
                        {toFiniteNumber(voidTransaction.refund_amount) > 0 && (
                          <div className="flex justify-between">
                            <span className="text-xs text-muted-foreground">Refund Amount:</span>
                            <span className="font-medium text-destructive">{formatCurrency(toFiniteNumber(voidTransaction.refund_amount))}</span>
                          </div>
                        )}
                        <div className="flex justify-between">
                          <span className="text-xs text-muted-foreground">Refund Processed:</span>
                          <span className="font-medium text-destructive">{voidTransaction.refund_processed ? 'Yes' : 'No'}</span>
                        </div>
                      </div>

                      <div className="border-t border-destructive/30 pt-3 space-y-1">
                        <p className="text-xs text-muted-foreground">Voided By: {voidTransaction.created_by_name || 'Unknown'}</p>
                        <p className="text-xs text-muted-foreground">Voided At: {format(new Date(voidTransaction.created_at), 'PPpp')}</p>
                      </div>
                    </>
                  ) : (
                    <p className="text-sm text-muted-foreground">No void transaction details found</p>
                  )}
                </CardContent>
              </Card>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Hidden Receipt for Printing */}
      <div className="hidden">
        <Receipt 
          order={order} 
          business={businessSettings} 
          currencyFormatter={formatCurrency}
          paperWidth={receiptPaperWidth}
          showHeader={receiptDisplaySettings.showHeader}
          showFooter={receiptDisplaySettings.showFooter}
          showQRCode={receiptDisplaySettings.showQRCode}
          showItemDetails={receiptDisplaySettings.showItemDetails}
          showTaxBreakdown={receiptDisplaySettings.showTaxBreakdown}
          copyNumber={receiptCopyNumber}
        />
      </div>

      <VoidModal
        order={order}
        isOpen={voidOpen}
        onOpenChange={setVoidOpen}
        canVoid={isAdminUser}
        onVoidCreated={(updatedOrder) => {
          // Update the order with the new status from the response
          if (order && updatedOrder) {
            Object.assign(order, updatedOrder);
          }
          setVoidOpen(false);
          void refreshCorrectionRecords();
        }}
      />
      <CreditNoteModal
        order={order}
        isOpen={creditOpen}
        onOpenChange={setCreditOpen}
        onCreditNoteCreated={() => {
          void refreshCorrectionRecords();
        }}
      />
      <DebitNoteModal
        order={order}
        isOpen={debitOpen}
        onOpenChange={setDebitOpen}
        onDebitNoteCreated={() => {
          void refreshCorrectionRecords();
        }}
      />
    </>
  );
}
