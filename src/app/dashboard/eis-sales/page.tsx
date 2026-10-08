'use client';

import React, { useEffect, useMemo, useState } from 'react';
import { format, subDays } from 'date-fns';
import {
  Check,
  ExternalLink,
  FileText,
  Loader2,
  RefreshCw,
  Search,
  X,
} from 'lucide-react';
import { useLiveQuery } from 'dexie-react-hooks';

import { db, type Order } from '@/lib/db';
import { resolveEisSaleStatusLabel } from '@/lib/eis-submission';
import { authFetch } from '@/lib/auth-fetch';
import { ensureTauriDeviceIdentity, getDeviceSerial } from '@/lib/device-identity';
import { useCurrency } from '@/hooks/use-currency';
import { toast } from '@/hooks/use-toast';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import SaleDetailModal from '@/app/dashboard/sessions/modals/sale-detail-modal';
import { PaginationControls, usePaginatedItems } from '@/app/dashboard/inventory/components/pagination-controls';

type EisStatusFilter = 'all' | 'pending' | 'submitted' | 'accepted' | 'rejected' | 'missing';
type LastSubmitMode = 'online' | 'offline';

type Terminal = {
  id: string;
  status?: string;
  business?: string | number;
  terminal_id?: string;
  terminal_label?: string;
  mra_terminal_id?: string;
  device_serial?: string;
  mac_address?: string;
  branch?: string | number | { id?: string | number; pk?: string | number; branch_id?: string | number; branchId?: string | number };
  branch_id?: string | number;
  branchId?: string | number;
  pos_name?: string;
  pos_version?: string;
  os_type?: string;
  activated_at?: string | null;
  has_mra_token?: boolean;
  is_online?: boolean | null;
  last_sync_at?: string | null;
  pending_offline_invoices?: number;
  health_check?: {
    checked?: boolean;
    is_online?: boolean;
    checked_at?: string;
    server_time?: string | null;
    server_time_raw?: string | null;
    error?: string;
  } | null;
};

interface LastSubmitModeResult {
  checked?: boolean;
  dry_run?: boolean;
  matched?: boolean;
  invoiceNumber?: string | null;
  sequence?: number | null;
  order_id?: string | null;
  mra_invoice_id?: string | null;
  updated?: string[];
  completed_retries?: number;
  validation_url?: string;
  endpoint_key?: string;
  endpoint?: string;
  reason?: unknown;
  errors?: unknown;
  error?: string;
}

interface LastSubmitReconciliation {
  terminal_id?: string;
  mra_terminal_id?: string;
  checked_at?: string;
  matched?: number;
  unmatched?: Array<{ mode?: string; invoiceNumber?: string | null; reason?: unknown }>;
  results?: Partial<Record<LastSubmitMode, LastSubmitModeResult>> & Record<string, LastSubmitModeResult | undefined>;
}

interface InvoiceLookupResult {
  checked?: boolean;
  dry_run?: boolean;
  found?: boolean;
  invoice_number?: string;
  validation_url?: string;
  receipt?: any;
  response?: any;
  errors?: unknown;
  error?: string;
}

interface VoidReceiptLookupResult {
  checked?: boolean;
  dry_run?: boolean;
  items?: any[];
  page?: number;
  page_size?: number;
  total_count?: number;
  response?: any;
  errors?: unknown;
  error?: string;
}

const ACTIVE_BRANCH_STORAGE_KEY = 'handypos-active-branch';

const toTrimmedString = (value: unknown): string => String(value ?? '').trim();

const toFiniteNumber = (value: unknown, fallback = 0): number => {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const normalizeBranchId = (value?: unknown): string => {
  const normalized = String(value ?? '').trim();
  if (!normalized) return '';
  const brnMatch = /^BRN-(\d+)$/i.exec(normalized);
  if (brnMatch) return brnMatch[1];
  const legacyMatch = /^branch-(\d+)$/i.exec(normalized);
  if (legacyMatch) return legacyMatch[1];
  return normalized;
};

const getBranchIdCandidates = (branchId?: string | null): string[] => {
  const normalized = String(branchId ?? '').trim();
  if (!normalized) return [];
  const backendId = normalizeBranchId(normalized);
  const values = new Set([normalized, backendId]);
  if (/^\d+$/.test(backendId)) {
    values.add(`BRN-${backendId}`);
    values.add(`branch-${backendId}`);
  }
  return Array.from(values).filter(Boolean);
};

const extractApiList = <T,>(payload: any): T[] => {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.results)) return payload.results;
  if (Array.isArray(payload?.data)) return payload.data;
  return [];
};

const getApiBranchId = (item: any): string => {
  const rawBranch = item?.branch;
  if (rawBranch && typeof rawBranch === 'object') {
    return String(rawBranch.id ?? rawBranch.pk ?? rawBranch.branch_id ?? rawBranch.branchId ?? '').trim();
  }
  return String(rawBranch ?? item?.branch_id ?? item?.branchId ?? '').trim();
};

const getApiDeviceSerial = (item: any): string => String(item?.device_serial ?? item?.deviceSerial ?? '').trim();

const readBooleanFlag = (value: unknown): boolean | null => {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (value === 1) return true;
    if (value === 0) return false;
  }

  const normalized = String(value ?? '').trim().toLowerCase();
  if (['true', '1', 'yes', 'online', 'ok', 'success', 'successful'].includes(normalized)) return true;
  if (['false', '0', 'no', 'offline', 'down', 'failed', 'failure'].includes(normalized)) return false;
  return null;
};

const mapTerminalFromApi = (payload: any, fallback?: Terminal | null): Terminal => {
  const source = payload?.terminal && typeof payload.terminal === 'object'
    ? { ...payload.terminal, ...payload }
    : (payload || {});
  const healthCheck = source?.health_check || fallback?.health_check || null;
  const online = readBooleanFlag(source?.is_online ?? healthCheck?.is_online ?? fallback?.is_online);

  return {
    ...(fallback || {}),
    id: String(source?.id ?? fallback?.id ?? ''),
    business: source?.business ?? fallback?.business,
    status: String(source?.status ?? fallback?.status ?? ''),
    terminal_id: source?.terminal_id ? String(source.terminal_id) : fallback?.terminal_id,
    terminal_label: source?.terminal_label ? String(source.terminal_label) : fallback?.terminal_label,
    mra_terminal_id: source?.mra_terminal_id ? String(source.mra_terminal_id) : fallback?.mra_terminal_id,
    device_serial: source?.device_serial ? String(source.device_serial) : fallback?.device_serial,
    mac_address: source?.mac_address ? String(source.mac_address) : fallback?.mac_address,
    branch: source?.branch ?? fallback?.branch,
    branch_id: source?.branch_id ?? fallback?.branch_id,
    branchId: source?.branchId ?? fallback?.branchId,
    pos_name: source?.pos_name ? String(source.pos_name) : fallback?.pos_name,
    pos_version: source?.pos_version ? String(source.pos_version) : fallback?.pos_version,
    os_type: source?.os_type ? String(source.os_type) : fallback?.os_type,
    activated_at: source?.activated_at || fallback?.activated_at || null,
    has_mra_token: typeof source?.has_mra_token === 'boolean' ? source.has_mra_token : fallback?.has_mra_token,
    is_online: online,
    last_sync_at: source?.last_sync_at || fallback?.last_sync_at || null,
    pending_offline_invoices: Number(source?.pending_offline_invoices ?? fallback?.pending_offline_invoices ?? 0),
    health_check: healthCheck,
  };
};

const parseDateInput = (value: string, endOfDay = false): Date | null => {
  if (!value) return null;
  const parsed = new Date(`${value}T${endOfDay ? '23:59:59.999' : '00:00:00.000'}`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
};

const getOrderCreatedDate = (order: Order): Date | null => {
  const raw = (order as any).createdAt ?? (order as any).created_at;
  const parsed = new Date(String(raw ?? ''));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
};

const sortOrdersByMostRecent = (orders: Order[]): Order[] => {
  return [...orders].sort((a, b) => {
    const timeA = getOrderCreatedDate(a)?.getTime() ?? 0;
    const timeB = getOrderCreatedDate(b)?.getTime() ?? 0;
    if (timeB !== timeA) return timeB - timeA;
    return toFiniteNumber((b as any).orderNumber ?? (b as any).order_number) - toFiniteNumber((a as any).orderNumber ?? (a as any).order_number);
  });
};

const resolveFiscalInvoiceNumber = (order: Order | null | undefined): string => {
  const source = order as any;
  return toTrimmedString(
    source?.fiscalInvoiceNumber ??
      source?.fiscal_invoice_number ??
      source?.invoiceNumber ??
      source?.invoice_number ??
      source?.receiptNumber ??
      source?.receipt_number
  );
};

const resolveEisStatus = (order: Order | null | undefined): Order['eis_status'] | 'MISSING' => {
  const source = order as any;
  const displayStatus = resolveEisSaleStatusLabel(order, true);
  if (displayStatus === 'Fiscal Failed') return 'REJECTED';
  if (displayStatus === 'EIS Submitted') return 'SUBMITTED';
  if (displayStatus === 'EIS Pending') return 'PENDING';
  const raw = toTrimmedString(source?.eisStatus ?? source?.eis_status).toUpperCase();
  if (raw === 'PENDING' || raw === 'SUBMITTED' || raw === 'ACCEPTED' || raw === 'REJECTED') {
    return raw as Order['eis_status'];
  }
  if (resolveFiscalInvoiceNumber(order)) return 'SUBMITTED';
  if (source?._dirty) return 'PENDING';
  return 'MISSING';
};

const getStatusBadgeVariant = (status: string): 'default' | 'secondary' | 'destructive' | 'outline' => {
  if (status === 'ACCEPTED' || status === 'SUBMITTED') return 'default';
  if (status === 'PENDING') return 'secondary';
  if (status === 'REJECTED') return 'destructive';
  return 'outline';
};


const resolveOrderDiscountAmount = (order: Order): number => {
  const orderDiscount = Math.max(0, toFiniteNumber((order as any).discountAmount ?? (order as any).discount_amount));
  const items = Array.isArray((order as any).items) ? (order as any).items : [];
  const itemDiscount = items.reduce(
    (sum: number, item: any) => sum + Math.max(0, toFiniteNumber(item?.discountAmount ?? item?.discount_amount)),
    0
  );
  return Math.max(orderDiscount, itemDiscount);
};

const resolveBuyerText = (order: Order): string => {
  const source = order as any;
  return toTrimmedString(
    source.customerName ?? source.customer_name ?? source.buyerName ?? source.buyer_name ?? source.customerPhone ?? source.customer_phone ?? source.buyerTin ?? source.buyer_tin
  );
};

const resolveOrderSearchText = (order: Order): string => {
  const source = order as any;
  const createdAt = getOrderCreatedDate(order);
  const dateValues = createdAt
    ? [format(createdAt, 'yyyy-MM-dd'), format(createdAt, 'PPpp'), createdAt.toLocaleString()]
    : [];
  const items = Array.isArray(order.items)
    ? order.items.flatMap((item: any) => [item?.name, item?.inventoryItemId, item?.inventory_item_id, item?.mraProductCode, item?.mra_product_code])
    : [];

  return [
    order.id,
    source.orderNumber,
    source.order_number,
    source.sessionId,
    source.session_id,
    source.paymentMethod,
    source.payment_method,
    source.status,
    source.eisStatus,
    source.eis_status,
    resolveFiscalInvoiceNumber(order),
    source.eisUuid,
    source.eis_uuid,
    source.qrCodePayload,
    source.qr_code_payload,
    source.customerName,
    source.customer_name,
    source.customerPhone,
    source.customer_phone,
    source.customerTin,
    source.customer_tin,
    source.buyerName,
    source.buyer_name,
    source.buyerTin,
    source.buyer_tin,
    source.total,
    source.vatAmount,
    source.vat_amount,
    ...dateValues,
    ...items,
  ]
    .map(toTrimmedString)
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
};

const formatDateTime = (value: Date | null): string => {
  if (!value || Number.isNaN(value.getTime())) return 'N/A';
  return format(value, 'PPpp');
};

const formatServerTime = (value?: string | null): string => {
  if (!value) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : formatDateTime(parsed);
};

const formatReconciliationValue = (value: unknown): string => {
  if (Array.isArray(value)) return value.map(formatReconciliationValue).filter(Boolean).join(', ');
  if (value && typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value ?? '').trim();
};

const getLastSubmitStatusLabel = (result?: LastSubmitModeResult): string => {
  if (!result) return 'Not checked';
  if (result.error || formatReconciliationValue(result.errors)) return 'Error';
  if (result.dry_run) return 'Prepared';
  if (result.matched) return 'Matched';
  if (result.checked) return 'No local match';
  return 'Not checked';
};

const getLastSubmitBadgeVariant = (result?: LastSubmitModeResult): 'default' | 'secondary' | 'destructive' | 'outline' => {
  if (!result) return 'outline';
  if (result.error || formatReconciliationValue(result.errors)) return 'destructive';
  if (result.matched) return 'default';
  if (result.dry_run) return 'secondary';
  return 'outline';
};

const getLastSubmitReason = (result?: LastSubmitModeResult): string => {
  if (!result) return '';
  return result.error || formatReconciliationValue(result.errors) || formatReconciliationValue(result.reason);
};

const formatUpdatedRecords = (records?: string[]): string => {
  if (!records?.length) return 'None';
  return records.map((record) => String(record).replace(/_/g, ' ')).join(', ');
};

const getVoidReceiptNumber = (item: any): string => toTrimmedString(
  item?.invoiceNumber ?? item?.invoice_number ?? item?.receiptNumber ?? item?.receipt_number
);

const extractInvoiceNumberFromResponse = (payload: any): string => {
  const inner = payload?.data && typeof payload.data === 'object' ? payload.data : payload;
  return toTrimmedString(
    inner?.invoiceHeader?.invoiceNumber ??
      inner?.invoiceNumber ??
      inner?.invoice_number ??
      inner?.receiptNumber ??
      inner?.receipt_number ??
      payload?.invoiceHeader?.invoiceNumber ??
      payload?.invoiceNumber ??
      payload?.receiptNumber
  );
};

const normalizeLastSubmitResult = (result: any): LastSubmitModeResult | undefined => {
  if (!result || typeof result !== 'object') return undefined;
  const responsePayload = result.response || result.data || {};
  const invoiceNumber = toTrimmedString(
    result.invoiceNumber ??
      result.invoice_number ??
      result.receiptNumber ??
      result.receipt_number ??
      extractInvoiceNumberFromResponse(responsePayload)
  );
  const sequence = result.sequence ?? result.invoice_sequence ?? result.count ?? null;
  const validationUrl = toTrimmedString(
    result.validation_url ??
      result.validationURL ??
      result.validationUrl ??
      responsePayload?.validationURL ??
      responsePayload?.validationUrl ??
      responsePayload?.data?.validationURL ??
      responsePayload?.data?.validationUrl
  );

  return {
    ...result,
    invoiceNumber: invoiceNumber || null,
    sequence: sequence === null || sequence === undefined || sequence === '' ? null : Number(sequence),
    updated: Array.isArray(result.updated) ? result.updated : [],
    completed_retries: Number(result.completed_retries ?? 0),
    validation_url: validationUrl || undefined,
  };
};

const normalizeLastSubmitReconciliation = (payload: any): LastSubmitReconciliation => {
  const source = payload?.data && typeof payload.data === 'object' && !Array.isArray(payload.data)
    ? payload.data
    : (payload || {});
  const rawResults = source.results && typeof source.results === 'object' ? source.results : {};
  const normalizedResults: LastSubmitReconciliation['results'] = {};

  for (const [key, value] of Object.entries(rawResults)) {
    const normalizedKey = String(key).toLowerCase();
    normalizedResults[normalizedKey] = normalizeLastSubmitResult(value);
  }

  for (const mode of ['online', 'offline'] as LastSubmitMode[]) {
    normalizedResults[mode] = normalizedResults[mode] || normalizeLastSubmitResult(source[mode]);
  }

  return {
    ...source,
    checked_at: source.checked_at || source.checkedAt || new Date().toISOString(),
    matched: Number(source.matched ?? source.matched_count ?? 0),
    unmatched: Array.isArray(source.unmatched) ? source.unmatched : [],
    results: normalizedResults,
  };
};

export default function EisSalesAuditPage() {
  const { format: formatCurrency } = useCurrency();
  const [activeBranchId, setActiveBranchId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<EisStatusFilter>('all');
  const [fromDate, setFromDate] = useState(format(subDays(new Date(), 30), 'yyyy-MM-dd'));
  const [toDate, setToDate] = useState(format(new Date(), 'yyyy-MM-dd'));
  const [selectedOrder, setSelectedOrder] = useState<Order | null>(null);
  const [terminal, setTerminal] = useState<Terminal | null>(null);
  const [isLoadingTerminal, setIsLoadingTerminal] = useState(false);
  const [isCheckingLastSubmits, setIsCheckingLastSubmits] = useState(false);
  const [lastSubmitReconciliation, setLastSubmitReconciliation] = useState<LastSubmitReconciliation | null>(null);
  const [invoiceLookupNumber, setInvoiceLookupNumber] = useState('');
  const [isLookingUpInvoice, setIsLookingUpInvoice] = useState(false);
  const [invoiceLookupResult, setInvoiceLookupResult] = useState<InvoiceLookupResult | null>(null);
  const [voidReceiptInvoiceNumber, setVoidReceiptInvoiceNumber] = useState('');
  const [voidReceiptStatusCode, setVoidReceiptStatusCode] = useState('');
  const [voidReceiptStartDate, setVoidReceiptStartDate] = useState(format(subDays(new Date(), 30), 'yyyy-MM-dd'));
  const [voidReceiptEndDate, setVoidReceiptEndDate] = useState(format(new Date(), 'yyyy-MM-dd'));
  const [isLoadingVoidReceipts, setIsLoadingVoidReceipts] = useState(false);
  const [voidReceiptResult, setVoidReceiptResult] = useState<VoidReceiptLookupResult | null>(null);

  useEffect(() => {
    const resolveStoredActiveBranch = () => {
      const stored = localStorage.getItem(ACTIVE_BRANCH_STORAGE_KEY) || localStorage.getItem('handy-pos-active-branch');
      return stored ? String(stored) : null;
    };

    const applyActiveBranch = (branchId: string | null) => {
      setActiveBranchId((current) => {
        if (current === branchId) return current;
        setLastSubmitReconciliation(null);
        setTerminal(null);
        return branchId;
      });
    };

    const loadActiveBranch = () => applyActiveBranch(resolveStoredActiveBranch());
    const handleBranchChanged = (event: Event) => {
      const nextBranchId = String((event as CustomEvent<{ branchId?: unknown }>).detail?.branchId ?? '').trim();
      applyActiveBranch(nextBranchId || resolveStoredActiveBranch());
    };

    loadActiveBranch();
    window.addEventListener('storage', loadActiveBranch);
    window.addEventListener('branchChanged', handleBranchChanged);
    window.addEventListener('handypos-active-branch-changed', handleBranchChanged);
    return () => {
      window.removeEventListener('storage', loadActiveBranch);
      window.removeEventListener('branchChanged', handleBranchChanged);
      window.removeEventListener('handypos-active-branch-changed', handleBranchChanged);
    };
  }, []);

  const allOrders = useLiveQuery(async () => {
    const orders = await db.orders.toArray();
    const branchCandidates = getBranchIdCandidates(activeBranchId);
    const branchSet = new Set(branchCandidates.map(normalizeBranchId));

    const branchOrders = branchSet.size
      ? orders.filter((order) => branchSet.has(normalizeBranchId((order as any).branchId ?? (order as any).branch_id)))
      : orders;

    return sortOrdersByMostRecent(branchOrders);
  }, [activeBranchId]);

  useEffect(() => {
    let cancelled = false;

    const loadTerminal = async () => {
      if (!activeBranchId) {
        setTerminal(null);
        return;
      }

      setIsLoadingTerminal(true);
      try {
        await ensureTauriDeviceIdentity();
        const terminalsResponse = await authFetch.fetch<any>('/mra-eis/terminals/');
        const terminals = extractApiList<any>(terminalsResponse);
        const currentDeviceSerial = getDeviceSerial().toLowerCase();
        const branchCandidates = new Set(getBranchIdCandidates(activeBranchId).map(normalizeBranchId));
        const branchTerminals = terminals.filter((item) => branchCandidates.has(normalizeBranchId(getApiBranchId(item))));
        const matchingTerminal =
          branchTerminals.find((item) => (
            String(item?.status || '').toLowerCase() === 'active' &&
            getApiDeviceSerial(item).toLowerCase() === currentDeviceSerial
          )) ||
          branchTerminals.find((item) => getApiDeviceSerial(item).toLowerCase() === currentDeviceSerial) ||
          branchTerminals.find((item) => String(item?.status || '').toLowerCase() === 'active') ||
          branchTerminals[0];

        if (!matchingTerminal) {
          if (!cancelled) setTerminal(null);
          return;
        }

        let mappedTerminal = mapTerminalFromApi(matchingTerminal);
        try {
          const statusResponse = await authFetch.fetch<any>(`/mra-eis/terminals/${mappedTerminal.id}/status/?ping=true&startup=true`);
          mappedTerminal = mapTerminalFromApi(statusResponse, mappedTerminal);
        } catch (error) {
          console.warn('[EIS Sales] Could not refresh terminal status:', error);
        }

        if (!cancelled) {
          setTerminal(mappedTerminal);
        }
      } catch (error) {
        console.error('[EIS Sales] Could not load EIS terminal:', error);
        if (!cancelled) setTerminal(null);
      } finally {
        if (!cancelled) setIsLoadingTerminal(false);
      }
    };

    loadTerminal();
    return () => {
      cancelled = true;
    };
  }, [activeBranchId]);

  const filteredOrders = useMemo(() => {
    const orders = allOrders || [];
    const startDate = parseDateInput(fromDate);
    const endDate = parseDateInput(toDate, true);
    const query = searchQuery.trim().toLowerCase();

    return orders.filter((order) => {
      const createdAt = getOrderCreatedDate(order);
      if (startDate && createdAt && createdAt < startDate) return false;
      if (endDate && createdAt && createdAt > endDate) return false;

      const status = resolveEisStatus(order);
      if (statusFilter !== 'all' && status.toLowerCase() !== statusFilter) return false;
      if (query && !resolveOrderSearchText(order).includes(query)) return false;

      return true;
    });
  }, [allOrders, fromDate, searchQuery, statusFilter, toDate]);

  const {
    paginatedItems,
    currentPage: effectiveCurrentPage,
    setCurrentPage,
    totalItems,
    pageStartIndex,
    pageEndIndex,
    totalPages,
  } = usePaginatedItems(filteredOrders, 25);

  useEffect(() => {
    setCurrentPage(1);
  }, [fromDate, searchQuery, setCurrentPage, statusFilter, toDate]);

  const fiscalSummary = useMemo(() => {
    const base = {
      total: filteredOrders.length,
      accepted: 0,
      submitted: 0,
      pending: 0,
      rejected: 0,
      missing: 0,
      fiscalTotal: 0,
      vatTotal: 0,
    };

    for (const order of filteredOrders) {
      const status = resolveEisStatus(order);
      if (status === 'ACCEPTED') base.accepted += 1;
      else if (status === 'SUBMITTED') base.submitted += 1;
      else if (status === 'PENDING') base.pending += 1;
      else if (status === 'REJECTED') base.rejected += 1;
      else base.missing += 1;

      base.fiscalTotal += toFiniteNumber((order as any).total);
      base.vatTotal += toFiniteNumber((order as any).vatAmount ?? (order as any).vat_amount ?? (order as any).tax);
    }

    return base;
  }, [filteredOrders]);

  const terminalDisplayId = useMemo(
    () => toTrimmedString(terminal?.mra_terminal_id || terminal?.terminal_id || terminal?.id),
    [terminal]
  );

  const onCheckLastSubmits = async () => {
    if (!terminal?.id) {
      toast({
        variant: 'destructive',
        title: 'Terminal required',
        description: 'No EIS terminal is linked to this branch yet.',
      });
      return;
    }

    setIsCheckingLastSubmits(true);
    try {
      const response = await authFetch.fetch<LastSubmitReconciliation>(
        `/mra-eis/terminals/${terminal.id}/reconcile_last_transactions/`,
        {
          method: 'POST',
          body: JSON.stringify({ modes: ['online', 'offline'] }),
        }
      );
      const normalizedResponse = normalizeLastSubmitReconciliation(response);
      setLastSubmitReconciliation(normalizedResponse);
      const matched = Number(normalizedResponse?.matched || 0);
      toast({
        title: 'Last submissions checked',
        description: matched > 0
          ? `${matched} MRA transaction${matched === 1 ? '' : 's'} matched local sales.`
          : 'MRA last submission check completed.',
      });
    } catch (error: any) {
      console.error('[EIS Sales] Last submission check failed:', error);
      toast({
        variant: 'destructive',
        title: 'Last submission check failed',
        description: error?.message || 'Could not check MRA last submissions.',
      });
    } finally {
      setIsCheckingLastSubmits(false);
    }
  };

  const onLookupInvoice = async () => {
    const invoiceNumber = invoiceLookupNumber.trim();
    if (!terminal?.id) {
      toast({
        variant: 'destructive',
        title: 'Terminal required',
        description: 'No EIS terminal is linked to this branch yet.',
      });
      return;
    }
    if (!invoiceNumber) {
      toast({
        variant: 'destructive',
        title: 'Invoice number required',
        description: 'Enter the fiscal invoice number MRA should look up.',
      });
      return;
    }

    setIsLookingUpInvoice(true);
    try {
      const response = await authFetch.fetch<InvoiceLookupResult>(
        `/mra-eis/terminals/${terminal.id}/lookup_invoice/`,
        {
          method: 'POST',
          body: JSON.stringify({ invoiceNumber }),
        }
      );
      setInvoiceLookupResult(response);
      const errors = formatReconciliationValue(response?.errors);
      toast({
        title: errors ? 'MRA lookup returned a response' : 'MRA receipt lookup complete',
        description: errors || `Receipt ${response?.found ? 'found' : 'lookup finished'} for ${invoiceNumber}.`,
      });
    } catch (error: any) {
      console.error('[EIS Sales] Invoice lookup failed:', error);
      setInvoiceLookupResult({ error: error?.message || 'MRA receipt lookup failed.' });
      toast({
        variant: 'destructive',
        title: 'Receipt lookup failed',
        description: error?.message || 'Could not fetch receipt from MRA.',
      });
    } finally {
      setIsLookingUpInvoice(false);
    }
  };

  const onFetchVoidReceipts = async () => {
    if (!terminal?.id) {
      toast({
        variant: 'destructive',
        title: 'Terminal required',
        description: 'No EIS terminal is linked to this branch yet.',
      });
      return;
    }

    setIsLoadingVoidReceipts(true);
    try {
      const payload: Record<string, unknown> = {
        invoiceNumber: voidReceiptInvoiceNumber.trim(),
        startDate: voidReceiptStartDate ? `${voidReceiptStartDate}T00:00:00.000Z` : '',
        endDate: voidReceiptEndDate ? `${voidReceiptEndDate}T23:59:59.999Z` : '',
        page: 1,
        pageSize: 25,
      };
      if (voidReceiptStatusCode.trim()) {
        payload.status = Number(voidReceiptStatusCode);
      }

      const response = await authFetch.fetch<VoidReceiptLookupResult>(
        `/mra-eis/terminals/${terminal.id}/get_void_receipts/`,
        {
          method: 'POST',
          body: JSON.stringify(payload),
        }
      );
      setVoidReceiptResult(response);
      const count = Number(response?.items?.length ?? 0);
      toast({
        title: 'Cancelled receipts fetched',
        description: `${count} cancelled receipt${count === 1 ? '' : 's'} returned from MRA.`,
      });
    } catch (error: any) {
      console.error('[EIS Sales] Void receipt lookup failed:', error);
      setVoidReceiptResult({ error: error?.message || 'MRA cancelled receipt lookup failed.' });
      toast({
        variant: 'destructive',
        title: 'Cancelled receipt lookup failed',
        description: error?.message || 'Could not fetch cancelled receipts from MRA.',
      });
    } finally {
      setIsLoadingVoidReceipts(false);
    }
  };

  return (
    <div className="flex-1 space-y-6 p-4 pt-6 md:p-8">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Sales</h1>
          <p className="text-muted-foreground">Search fiscal sales across sessions and check the last MRA submissions when needed.</p>
        </div>
        <Dialog>
          <DialogTrigger asChild>
            <Button variant="outline" className="w-full sm:w-auto">
              <Search className="mr-2 h-4 w-4" />
              MRA Receipt Lookup
            </Button>
          </DialogTrigger>
          <DialogContent className="max-h-[88vh] w-[calc(100vw-1rem)] max-w-[calc(100vw-1rem)] overflow-y-auto p-4 sm:max-w-5xl sm:p-6">
            <DialogHeader className="space-y-2 text-left">
              <DialogTitle>MRA Receipt Lookup</DialogTitle>
              <DialogDescription className="text-sm leading-snug">
                Fetch official receipts and cancelled receipt requests directly from MRA for certification checks.
              </DialogDescription>
            </DialogHeader>
            <div className="grid min-w-0 gap-4 xl:grid-cols-2">
              <div className="min-w-0 space-y-3 rounded-md border p-3">
                <div>
                  <p className="text-sm font-medium">Receipt by invoice number</p>
                  <p className="text-xs text-muted-foreground">Uses MRA get-invoice-by-number.</p>
                </div>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <Input
                    value={invoiceLookupNumber}
                    onChange={(event) => setInvoiceLookupNumber(event.target.value)}
                    placeholder="Fiscal invoice number"
                    className="min-w-0 font-mono"
                  />
                  <Button onClick={onLookupInvoice} disabled={!terminal?.id || isLookingUpInvoice} className="w-full shrink-0 sm:w-auto">
                    {isLookingUpInvoice ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Search className="mr-2 h-4 w-4" />}
                    Lookup
                  </Button>
                </div>
                {invoiceLookupResult && (
                  <div className="min-w-0 rounded-md bg-muted/30 p-3 text-xs">
                    <div className="mb-2 flex flex-wrap items-center gap-2">
                      <Badge variant={invoiceLookupResult.error || formatReconciliationValue(invoiceLookupResult.errors) ? 'destructive' : invoiceLookupResult.found ? 'default' : 'outline'}>
                        {invoiceLookupResult.error ? 'Error' : invoiceLookupResult.found ? 'Found' : 'Checked'}
                      </Badge>
                      <span className="min-w-0 break-all font-mono">{invoiceLookupResult.invoice_number || invoiceLookupNumber || 'N/A'}</span>
                      {invoiceLookupResult.validation_url && (
                        <a href={invoiceLookupResult.validation_url} target="_blank" rel="noreferrer" className="inline-flex max-w-full items-center gap-1 break-all font-medium text-primary underline-offset-4 hover:underline">
                          Validation URL <ExternalLink className="h-3 w-3" />
                        </a>
                      )}
                    </div>
                    {(invoiceLookupResult.error || formatReconciliationValue(invoiceLookupResult.errors)) && (
                      <p className="mb-2 break-words text-destructive">
                        {invoiceLookupResult.error || formatReconciliationValue(invoiceLookupResult.errors)}
                      </p>
                    )}
                    {invoiceLookupResult.found && (
                      <p className="text-muted-foreground">Receipt details were returned by MRA.</p>
                    )}
                  </div>
                )}
              </div>

              <div className="min-w-0 space-y-3 rounded-md border p-3">
                <div>
                  <p className="text-sm font-medium">Cancelled receipts</p>
                  <p className="text-xs text-muted-foreground">Uses MRA get-void-receipts.</p>
                </div>
                <div className="grid min-w-0 gap-2 sm:grid-cols-2">
                  <Input
                    value={voidReceiptInvoiceNumber}
                    onChange={(event) => setVoidReceiptInvoiceNumber(event.target.value)}
                    placeholder="Invoice number optional"
                    className="min-w-0 font-mono"
                  />
                  <Input
                    value={voidReceiptStatusCode}
                    onChange={(event) => setVoidReceiptStatusCode(event.target.value.replace(/[^\d-]/g, ''))}
                    placeholder="Status code optional"
                    inputMode="numeric"
                  />
                  <Input type="date" value={voidReceiptStartDate} onChange={(event) => setVoidReceiptStartDate(event.target.value)} />
                  <Input type="date" value={voidReceiptEndDate} onChange={(event) => setVoidReceiptEndDate(event.target.value)} />
                </div>
                <Button variant="outline" onClick={onFetchVoidReceipts} disabled={!terminal?.id || isLoadingVoidReceipts} className="w-full sm:w-auto">
                  {isLoadingVoidReceipts ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
                  <span className="truncate">Fetch Cancelled Receipts</span>
                </Button>
                {voidReceiptResult && (
                  <div className="min-w-0 rounded-md bg-muted/30 p-3 text-xs">
                    <div className="mb-2 flex flex-wrap items-center gap-2">
                      <Badge variant={voidReceiptResult.error || formatReconciliationValue(voidReceiptResult.errors) ? 'destructive' : 'default'}>
                        {voidReceiptResult.error ? 'Error' : `${voidReceiptResult.items?.length ?? 0} returned`}
                      </Badge>
                      <span>Total {voidReceiptResult.total_count ?? voidReceiptResult.items?.length ?? 0}</span>
                      <span>Page {voidReceiptResult.page ?? 1}</span>
                    </div>
                    {(voidReceiptResult.error || formatReconciliationValue(voidReceiptResult.errors)) && (
                      <p className="mb-2 break-words text-destructive">
                        {voidReceiptResult.error || formatReconciliationValue(voidReceiptResult.errors)}
                      </p>
                    )}
                    {Boolean(voidReceiptResult.items?.length) ? (
                      <div className="max-h-56 max-w-full space-y-2 overflow-auto">
                        {(voidReceiptResult.items || []).map((item, index) => (
                          <div key={`${getVoidReceiptNumber(item) || 'void'}-${index}`} className="rounded border bg-background p-2">
                            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                              <span className="min-w-0 break-all font-mono">{getVoidReceiptNumber(item) || 'N/A'}</span>
                              <Badge variant="outline">{toTrimmedString(item?.status ?? item?.approvalStatus ?? item?.approval_status) || 'Status N/A'}</Badge>
                            </div>
                            <div className="mt-1 grid gap-1 text-muted-foreground sm:grid-cols-2">
                              <span>Issue: {toTrimmedString(item?.issueDate ?? item?.issue_date) || 'N/A'}</span>
                              <span>Requested: {toTrimmedString(item?.requestedOn ?? item?.requested_on) || 'N/A'}</span>
                              <span className="sm:col-span-2">Reason: {toTrimmedString(item?.requestReason ?? item?.request_reason) || 'N/A'}</span>
                              {toTrimmedString(item?.rejectedReason ?? item?.rejected_reason) && (
                                <span className="sm:col-span-2 text-destructive">Rejected: {toTrimmedString(item?.rejectedReason ?? item?.rejected_reason)}</span>
                              )}
                            </div>
                          </div>
                        ))}
                      </div>
                    ) : (
                      !voidReceiptResult.error && <p className="text-muted-foreground">No cancelled receipts returned for this filter.</p>
                    )}
                  </div>
                )}
              </div>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Fiscal Sales</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{fiscalSummary.total}</div>
            <p className="text-xs text-muted-foreground">For current filters</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Accepted / Submitted</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{fiscalSummary.accepted + fiscalSummary.submitted}</div>
            <p className="text-xs text-muted-foreground">Accepted {fiscalSummary.accepted}, submitted {fiscalSummary.submitted}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Pending / Rejected</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{fiscalSummary.pending + fiscalSummary.rejected}</div>
            <p className="text-xs text-muted-foreground">Pending {fiscalSummary.pending}, rejected {fiscalSummary.rejected}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">VAT Total</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{formatCurrency(fiscalSummary.vatTotal)}</div>
            <p className="text-xs text-muted-foreground">For listed sales</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <CardTitle className="flex items-center gap-2">
                <FileText className="h-5 w-5 text-muted-foreground" />
                Sales List
              </CardTitle>
              <CardDescription>
                Flat list across all sessions for the active branch.
                {terminalDisplayId ? ` Terminal ${terminalDisplayId}.` : isLoadingTerminal ? ' Loading EIS terminal...' : ' No EIS terminal found.'}
              </CardDescription>
              {terminal && (
                <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <Badge variant={terminal.is_online === true ? 'default' : terminal.is_online === false ? 'destructive' : 'outline'}>
                    {terminal.is_online === true ? 'MRA Online' : terminal.is_online === false ? 'MRA Offline' : isLoadingTerminal ? 'Checking MRA' : 'MRA status unavailable'}
                  </Badge>
                  {terminal.health_check?.checked_at && <span>Ping {formatDateTime(new Date(terminal.health_check.checked_at))}</span>}
                  {(terminal.health_check?.server_time || terminal.health_check?.server_time_raw) && (
                    <span>Server time {formatServerTime(terminal.health_check.server_time || terminal.health_check.server_time_raw)}</span>
                  )}
                  {terminal.pending_offline_invoices ? <span>{terminal.pending_offline_invoices} pending offline</span> : null}
                </div>
              )}
            </div>
            <Button variant="outline" onClick={onCheckLastSubmits} disabled={isCheckingLastSubmits || isLoadingTerminal || !terminal?.id} className="w-full sm:w-auto">
              {isCheckingLastSubmits ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
              Check Last Submits
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {lastSubmitReconciliation && (
            <div className="rounded-md border bg-muted/20 p-3">
              <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-sm font-medium">MRA Last Submissions</p>
                {lastSubmitReconciliation.checked_at && (
                  <Badge variant="outline">Checked {formatDateTime(new Date(lastSubmitReconciliation.checked_at))}</Badge>
                )}
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                {(['online', 'offline'] as LastSubmitMode[]).map((mode) => {
                  const result = lastSubmitReconciliation.results?.[mode];
                  const reason = getLastSubmitReason(result);
                  return (
                    <div key={mode} className="min-w-0 rounded-md border bg-background p-3">
                      <div className="flex items-center justify-between gap-2">
                        <p className="font-medium capitalize">{mode}</p>
                        <Badge variant={getLastSubmitBadgeVariant(result)}>{getLastSubmitStatusLabel(result)}</Badge>
                      </div>
                      <div className="mt-3 grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
                        <div className="min-w-0">
                          <p className="text-muted-foreground">Invoice Number</p>
                          <p className="break-all font-mono">{result?.invoiceNumber || 'N/A'}</p>
                        </div>
                        <div>
                          <p className="text-muted-foreground">Sequence</p>
                          <p className="font-medium">{result?.sequence || 'N/A'}</p>
                        </div>
                        <div>
                          <p className="text-muted-foreground">Updated</p>
                          <p className="font-medium capitalize">{formatUpdatedRecords(result?.updated)}</p>
                        </div>
                        <div>
                          <p className="text-muted-foreground">Retries Closed</p>
                          <p className="font-medium">{result?.completed_retries ?? 0}</p>
                        </div>
                      </div>
                      {reason && <p className="mt-2 break-words text-xs text-muted-foreground">{reason}</p>}
                      {result?.validation_url && (
                        <a href={result.validation_url} target="_blank" rel="noreferrer" className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-primary underline-offset-4 hover:underline">
                          Validation URL <ExternalLink className="h-3 w-3" />
                        </a>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          <div className="grid gap-3 md:grid-cols-[1fr_170px_170px_180px]">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                placeholder="Search date, invoice, order, buyer, item, status..."
                className="pl-9"
              />
            </div>
            <Input type="date" value={fromDate} onChange={(event) => setFromDate(event.target.value)} />
            <Input type="date" value={toDate} onChange={(event) => setToDate(event.target.value)} />
            <Select value={statusFilter} onValueChange={(value) => setStatusFilter(value as EisStatusFilter)}>
              <SelectTrigger>
                <SelectValue placeholder="Status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All statuses</SelectItem>
                <SelectItem value="accepted">Accepted</SelectItem>
                <SelectItem value="submitted">Submitted</SelectItem>
                <SelectItem value="pending">Pending</SelectItem>
                <SelectItem value="rejected">Rejected</SelectItem>
                <SelectItem value="missing">Missing fiscal data</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="overflow-x-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Order</TableHead>
                  <TableHead>Fiscal Invoice</TableHead>
                  <TableHead>Date</TableHead>
                  <TableHead>Buyer</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">VAT</TableHead>
                  <TableHead className="text-right">Discount</TableHead>
                  <TableHead className="text-right">Total</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {allOrders === undefined ? (
                  <TableRow>
                    <TableCell colSpan={9} className="h-24 text-center">
                      <Loader2 className="mx-auto h-5 w-5 animate-spin text-muted-foreground" />
                    </TableCell>
                  </TableRow>
                ) : filteredOrders.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={9} className="h-24 text-center text-muted-foreground">
                      No sales found for this search.
                    </TableCell>
                  </TableRow>
                ) : (
                  paginatedItems.map((order) => {
                    const status = resolveEisStatus(order);
                    const displayStatus = resolveEisSaleStatusLabel(order, true) || status;
                    const displayStatusVariant = displayStatus === 'Fiscal Failed'
                      ? 'destructive'
                      : displayStatus === 'EIS Pending'
                        ? 'secondary'
                        : displayStatus === 'EIS Submitted'
                          ? 'default'
                          : getStatusBadgeVariant(status);
                    const fiscalInvoice = resolveFiscalInvoiceNumber(order);
                    const createdAt = getOrderCreatedDate(order);
                    const qrUrl = toTrimmedString((order as any).qrCodePayload ?? (order as any).qr_code_payload);
                    const discountAmount = resolveOrderDiscountAmount(order);
                    return (
                      <TableRow key={order.id}>
                        <TableCell>
                          <div className="font-medium">#{(order as any).orderNumber ?? (order as any).order_number ?? 'N/A'}</div>
                          <div className="text-xs text-muted-foreground">{(order as any).paymentMethod ?? (order as any).payment_method ?? 'N/A'}</div>
                        </TableCell>
                        <TableCell className="max-w-[220px] break-all font-mono text-xs">{fiscalInvoice || 'N/A'}</TableCell>
                        <TableCell className="whitespace-nowrap">{formatDateTime(createdAt)}</TableCell>
                        <TableCell className="max-w-[180px] truncate">{resolveBuyerText(order) || 'Walk-in'}</TableCell>
                        <TableCell>
                          <Badge variant={displayStatusVariant} className="gap-1">
                            {displayStatus === 'Fiscal Failed' ? <X className="h-3 w-3" /> : displayStatus === 'EIS Submitted' ? <Check className="h-3 w-3" /> : null}
                            {displayStatus}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-right">{formatCurrency(toFiniteNumber((order as any).vatAmount ?? (order as any).vat_amount ?? (order as any).tax))}</TableCell>
                        <TableCell className="text-right">{discountAmount > 0 ? formatCurrency(discountAmount) : '-'}</TableCell>
                        <TableCell className="text-right font-medium">{formatCurrency(toFiniteNumber((order as any).total))}</TableCell>
                        <TableCell className="text-right">
                          <div className="flex justify-end gap-2">
                            {qrUrl.startsWith('http') && (
                              <Button asChild variant="outline" size="icon" title="Open validation URL">
                                <a href={qrUrl} target="_blank" rel="noreferrer">
                                  <ExternalLink className="h-4 w-4" />
                                </a>
                              </Button>
                            )}
                            <Button variant="outline" size="sm" onClick={() => setSelectedOrder(order)}>
                              View
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })
                )}
              </TableBody>
            </Table>
          </div>

          <PaginationControls
            currentPage={effectiveCurrentPage}
            totalItems={totalItems}
            totalPages={totalPages}
            pageStartIndex={pageStartIndex}
            pageEndIndex={pageEndIndex}
            onPageChange={setCurrentPage}
            itemLabel="sales"
          />
        </CardContent>
      </Card>

      <SaleDetailModal order={selectedOrder} isOpen={Boolean(selectedOrder)} onOpenChange={(open) => !open && setSelectedOrder(null)} />
    </div>
  );
}
