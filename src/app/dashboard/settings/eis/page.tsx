'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useState, useEffect, useCallback, useMemo, useRef, type ChangeEvent } from 'react';
import { useForm, FormProvider } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import * as z from 'zod';
import { 
  Zap, AlertCircle, CheckCircle2, Clock, RefreshCw, Loader2, 
  Eye, EyeOff, ChevronDown, ChevronUp, AlertTriangle,
  Terminal, Settings, Package, FileText, Download, Upload, Trash2
} from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { toast } from '@/hooks/use-toast';
import { useAuth } from '@/hooks/use-auth';
import { authFetch } from '@/lib/auth-fetch';
import { db, type InventoryItem } from '@/lib/db';
import {
  DEFAULT_DEVICE_MAC_ADDRESS,
  DEVICE_IDENTITY_CHANGED_EVENT,
  ensureTauriDeviceIdentity,
  ensureTauriDeviceMacAddress,
  getDetectedOS,
  getDeviceMacAddress,
  getDeviceSerial,
  normalizeDeviceMacAddress,
} from '@/lib/device-identity';
import { downloadBlobFile, downloadTextFile } from '@/lib/file-download';
import {
  formatMraProductSyncDiagnostics,
  syncInventoryFromBackend,
} from '@/lib/services/inventory-sync';

// Schemas
const eisSetupSchema = z.object({
  // Enable/Disable
  enableEis: z.boolean().default(false),
  
  // Taxpayer Information
  tin: z.string().optional(),
  vatRegistrationNumber: z.string().optional(),
  vatRegistered: z.boolean().default(false),
  mraTaxpayerType: z.enum(['VAT', 'NON_VAT']).default('NON_VAT'),
  
  // MRA Enrollment
  mraEnrolled: z.boolean().default(false),
  
  // Environment
  eisEnvironment: z.enum(['TEST', 'PROD']).default('TEST'),
  
  // Safety
  blockSalesIfEisDown: z.boolean().default(true),
  blockSalesIfTaxMappingMissing: z.boolean().default(true),
});

const terminalActivationSchema = z.object({
  activeBranch: z.string().min(1, 'Please select a branch.'),
  tac_code: z.string().min(1, 'Terminal Activation Code is required.'),
  pos_name: z.string().min(1, 'POS name is required.'),
  pos_version: z.string().min(1, 'POS version is required.'),
  os_type: z.string().min(1, 'OS type is required.'),
  device_serial: z.string().min(1, 'Device serial is required.'),
  mac_address: z.string().optional(),
});

type EISSetupFormValues = z.infer<typeof eisSetupSchema>;
type TerminalActivationFormValues = z.infer<typeof terminalActivationSchema>;
type InitialStockExportMode = 'all' | 'first_20' | 'custom';

const EIS_TERMINAL_ACTIVATION_CHANGED_EVENT = 'handypos-eis-terminal-activation-changed';
const ACTIVATION_RELOAD_DELAY_MS = 900;

const INITIAL_STOCK_UPLOAD_HEADERS = [
  'BarCode',
  'ProductName',
  'ProductDescription',
  'QuantityInStock',
  'UnitPrice',
  'CostPrice',
  'SellingPrice',
  'ReorderLevel',
  'OverQuantityStockLevel',
] as const;

type InitialStockHeader = typeof INITIAL_STOCK_UPLOAD_HEADERS[number];
type InitialStockRow = Record<InitialStockHeader, string>;
type InitialStockApiProduct = {
  BarCode: string;
  ProductName: string;
  ProductDescription: string;
  QuantityInStock: number;
  UnitPrice: number;
  CostPrice: number;
  SellingPrice: number;
  ReorderLevel: number;
  OverQuantityStockLevel: number;
};
type InitialStockSubmissionPreview = {
  tin: string;
  branchName: string;
  totalAvailable: number;
  generatedBarcodeCount: number;
  mraMappedCount: number;
  isLastBatch: boolean;
  products: InitialStockApiProduct[];
};
type MRAHsCode = {
  code?: string;
  description?: string;
  taxRateId?: string;
};
type MRAUnitOfMeasure = {
  unitOfMeasure?: string;
  unitOfMeasureDescription?: string;
};
type MRAProductCreateForm = {
  barcode: string;
  hsCode: string;
  name: string;
  description: string;
  uom: string;
};

const XLSX_MIME_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const MRA_INITIAL_INVENTORY_TEMPLATE_SHEET = 'TAXPAYERINVENTORY';
const MRA_INITIAL_INVENTORY_NOTE = `1. Please fill in the details of your products and raw materials according to the example provided.

2. If your products or raw materials do not have barcodes , log in to the EIS Taxpayer Portal to search for your product. Refer to the instructios below:
If the product is not found,
    . Create it on the portal  (inventory management -> Products -> add product).
    . A barcode will be generated from there.

3. Remove the  two sample products as they are just examples
4. Add your real products.`;

const toBackendBranchId = (id: string): string => {
  const normalized = String(id || '').trim();
  if (!normalized) return normalized;

  const brnMatch = /^BRN-(\d+)$/i.exec(normalized);
  if (brnMatch) return brnMatch[1];

  const legacyBranchMatch = /^branch-(\d+)$/i.exec(normalized);
  if (legacyBranchMatch) return legacyBranchMatch[1];

  return normalized;
};

const getBranchIdCandidates = (branchId: string): string[] => {
  const normalized = String(branchId || '').trim();
  if (!normalized) return [];

  const backendId = toBackendBranchId(normalized);
  const candidates = new Set<string>([normalized, backendId]);

  if (/^\d+$/.test(backendId)) {
    candidates.add(`BRN-${backendId}`);
    candidates.add(`branch-${backendId}`);
  }

  return Array.from(candidates).filter(Boolean);
};

const getInventoryItemsForBranch = async (branchId: string): Promise<InventoryItem[]> => {
  const branchCandidates = getBranchIdCandidates(branchId);
  if (branchCandidates.length === 0) return [];
  if (branchCandidates.length === 1) {
    return db.inventory.where('branchId').equals(branchCandidates[0]).toArray();
  }
  return db.inventory.where('branchId').anyOf(branchCandidates).toArray();
};

const toFiniteNumber = (value: unknown, fallback = 0): number => {
  if (typeof value === 'number') return Number.isFinite(value) ? value : fallback;
  const parsed = Number.parseFloat(String(value ?? '').replace(/,/g, ''));
  return Number.isFinite(parsed) ? parsed : fallback;
};

const formatFlexibleNumber = (value: unknown): string => {
  const numberValue = toFiniteNumber(value);
  if (Number.isInteger(numberValue)) return String(numberValue);
  return numberValue.toFixed(3).replace(/\.?0+$/, '');
};

const formatMoney = (value: unknown): string => toFiniteNumber(value).toFixed(2);

const xmlEscape = (value: unknown): string => {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
};

const buildInlineStringCellXml = (cellReference: string, value: unknown, style?: number): string => {
  const styleAttribute = typeof style === 'number' ? ` s="${style}"` : '';
  return `<c r="${cellReference}"${styleAttribute} t="inlineStr"><is><t xml:space="preserve">${xmlEscape(value)}</t></is></c>`;
};

const buildNumberCellXml = (cellReference: string, value: unknown, style?: number): string => {
  const styleAttribute = typeof style === 'number' ? ` s="${style}"` : '';
  return `<c r="${cellReference}"${styleAttribute}><v>${toFiniteNumber(value)}</v></c>`;
};

const excelDateSerialFromDate = (date = new Date()): number => {
  const utcMidnight = Date.UTC(date.getFullYear(), date.getMonth(), date.getDate());
  return Math.floor(utcMidnight / 86400000) + 25569;
};

const buildInitialStockWorksheetXml = (rows: InitialStockRow[]): string => {
  const headerCells = [
    ['B2', 'Bar Code'],
    ['C2', 'Product Name'],
    ['D2', 'Product Description'],
    ['E2', 'Unit Price'],
    ['F2', 'Quantity in Stock'],
    ['G2', 'Cost Price'],
    ['H2', 'Date Bought'],
  ].map(([cell, label]) => buildInlineStringCellXml(cell, label)).join('');

  const dateBoughtSerial = excelDateSerialFromDate();
  const dataRows = rows.map((row, rowIndex) => {
    const rowNumber = rowIndex + 3;
    const totalCost = toFiniteNumber(row.CostPrice || row.UnitPrice) * toFiniteNumber(row.QuantityInStock);
    const cells = [
      buildInlineStringCellXml(`B${rowNumber}`, row.BarCode),
      buildInlineStringCellXml(`C${rowNumber}`, row.ProductName),
      buildInlineStringCellXml(`D${rowNumber}`, row.ProductDescription),
      buildNumberCellXml(`E${rowNumber}`, row.UnitPrice),
      buildNumberCellXml(`F${rowNumber}`, row.QuantityInStock),
      buildNumberCellXml(`G${rowNumber}`, totalCost),
      buildNumberCellXml(`H${rowNumber}`, dateBoughtSerial),
      rowIndex === 0 ? buildInlineStringCellXml(`K${rowNumber}`, MRA_INITIAL_INVENTORY_NOTE) : '',
    ].join('');
    return `<row r="${rowNumber}">${cells}</row>`;
  }).join('');

  const headerRow = `<row r="2">${headerCells}${buildInlineStringCellXml('K2', 'NOTE')}</row>`;
  const noteRow = rows.length === 0
    ? `<row r="3">${buildInlineStringCellXml('K3', MRA_INITIAL_INVENTORY_NOTE)}</row>`
    : '';

  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="A1:X1048576"/>
  <sheetViews><sheetView workbookViewId="0"/></sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <cols>
    <col min="2" max="2" width="18" customWidth="1"/>
    <col min="3" max="3" width="24" customWidth="1"/>
    <col min="4" max="4" width="48" customWidth="1"/>
    <col min="5" max="7" width="14" customWidth="1"/>
    <col min="8" max="8" width="16" customWidth="1"/>
    <col min="11" max="19" width="13" customWidth="1"/>
  </cols>
  <sheetData>
    ${headerRow}
    ${dataRows}
    ${noteRow}
    <row r="100000">${buildInlineStringCellXml('X100000', 'Coolmbo')}</row>
    <row r="1048576">${buildInlineStringCellXml('A1048576', 'Malawi123!')}</row>
  </sheetData>
</worksheet>`;
};

const CRC32_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let current = index;
    for (let bit = 0; bit < 8; bit += 1) {
      current = current & 1 ? 0xedb88320 ^ (current >>> 1) : current >>> 1;
    }
    table[index] = current >>> 0;
  }
  return table;
})();

const calculateCrc32 = (data: Uint8Array): number => {
  let crc = 0xffffffff;
  for (let index = 0; index < data.length; index += 1) {
    crc = CRC32_TABLE[(crc ^ data[index]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
};

const concatUint8Arrays = (chunks: Uint8Array[]): Uint8Array => {
  const totalLength = chunks.reduce((total, chunk) => total + chunk.length, 0);
  const output = new Uint8Array(totalLength);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.length;
  }
  return output;
};

const getDosTimestamp = (date = new Date()): { time: number; date: number } => {
  const year = Math.max(1980, date.getFullYear());
  const dosTime = (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2);
  const dosDate = ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate();
  return { time: dosTime, date: dosDate };
};

const createZipArchive = (entries: Array<{ name: string; content: string }>): Uint8Array => {
  const encoder = new TextEncoder();
  const { time, date } = getDosTimestamp();
  const localParts: Uint8Array[] = [];
  const centralParts: Uint8Array[] = [];
  let offset = 0;

  for (const entry of entries) {
    const nameBytes = encoder.encode(entry.name);
    const data = encoder.encode(entry.content);
    const crc = calculateCrc32(data);

    const localHeader = new Uint8Array(30 + nameBytes.length);
    const localView = new DataView(localHeader.buffer);
    localView.setUint32(0, 0x04034b50, true);
    localView.setUint16(4, 20, true);
    localView.setUint16(6, 0, true);
    localView.setUint16(8, 0, true);
    localView.setUint16(10, time, true);
    localView.setUint16(12, date, true);
    localView.setUint32(14, crc, true);
    localView.setUint32(18, data.length, true);
    localView.setUint32(22, data.length, true);
    localView.setUint16(26, nameBytes.length, true);
    localView.setUint16(28, 0, true);
    localHeader.set(nameBytes, 30);
    localParts.push(localHeader, data);

    const centralHeader = new Uint8Array(46 + nameBytes.length);
    const centralView = new DataView(centralHeader.buffer);
    centralView.setUint32(0, 0x02014b50, true);
    centralView.setUint16(4, 20, true);
    centralView.setUint16(6, 20, true);
    centralView.setUint16(8, 0, true);
    centralView.setUint16(10, 0, true);
    centralView.setUint16(12, time, true);
    centralView.setUint16(14, date, true);
    centralView.setUint32(16, crc, true);
    centralView.setUint32(20, data.length, true);
    centralView.setUint32(24, data.length, true);
    centralView.setUint16(28, nameBytes.length, true);
    centralView.setUint16(30, 0, true);
    centralView.setUint16(32, 0, true);
    centralView.setUint16(34, 0, true);
    centralView.setUint16(36, 0, true);
    centralView.setUint32(38, 0, true);
    centralView.setUint32(42, offset, true);
    centralHeader.set(nameBytes, 46);
    centralParts.push(centralHeader);

    offset += localHeader.length + data.length;
  }

  const centralDirectory = concatUint8Arrays(centralParts);
  const endRecord = new Uint8Array(22);
  const endView = new DataView(endRecord.buffer);
  endView.setUint32(0, 0x06054b50, true);
  endView.setUint16(4, 0, true);
  endView.setUint16(6, 0, true);
  endView.setUint16(8, entries.length, true);
  endView.setUint16(10, entries.length, true);
  endView.setUint32(12, centralDirectory.length, true);
  endView.setUint32(16, offset, true);
  endView.setUint16(20, 0, true);

  return concatUint8Arrays([...localParts, centralDirectory, endRecord]);
};

const buildInitialStockWorkbookBlob = (rows: InitialStockRow[]): Blob => {
  const createdAt = new Date().toISOString();
  const entries = [
    {
      name: '[Content_Types].xml',
      content: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>`,
    },
    {
      name: '_rels/.rels',
      content: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>`,
    },
    {
      name: 'docProps/app.xml',
      content: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>HandyPOS</Application>
</Properties>`,
    },
    {
      name: 'docProps/core.xml',
      content: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>HandyPOS</dc:creator>
  <cp:lastModifiedBy>HandyPOS</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">${createdAt}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">${createdAt}</dcterms:modified>
</cp:coreProperties>`,
    },
    {
      name: 'xl/workbook.xml',
      content: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="${MRA_INITIAL_INVENTORY_TEMPLATE_SHEET}" sheetId="2" r:id="rId1"/>
  </sheets>
</workbook>`,
    },
    {
      name: 'xl/_rels/workbook.xml.rels',
      content: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>`,
    },
    {
      name: 'xl/worksheets/sheet1.xml',
      content: buildInitialStockWorksheetXml(rows),
    },
  ];

  const archive = createZipArchive(entries);
  const archiveBuffer = new ArrayBuffer(archive.byteLength);
  new Uint8Array(archiveBuffer).set(archive);
  return new Blob([archiveBuffer], { type: XLSX_MIME_TYPE });
};

const sanitizeFilenamePart = (value: string): string => {
  return String(value || 'branch')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'branch';
};

const resolveProductDescription = (item: InventoryItem): string => {
  const rawDescription = String((item as any).description || '').trim();
  if (rawDescription) return rawDescription;

  const details = [
    item.brand,
    item.manufacturer,
    item.category,
    item.unitType || item.unit_type,
  ].map((value) => String(value || '').trim()).filter(Boolean);

  return details.length > 0 ? details.join(' | ') : item.name;
};

const hasExplicitInitialStockBarcode = (item: InventoryItem): boolean => {
  return [item.barcode, item.productCode, item.sku].some((value) => isMraBarcode(value));
};

const hashInitialStockBarcodeSeed = (seed: string): number => {
  let hash = 2166136261;
  for (let index = 0; index < seed.length; index += 1) {
    hash ^= seed.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
};

const isMraBarcode = (value: unknown): boolean => {
  return /^\d{10,15}$/.test(String(value || '').trim());
};

const calculateEan13CheckDigit = (firstTwelveDigits: string): string => {
  const sum = firstTwelveDigits
    .split('')
    .reduce((total, digit, index) => total + Number(digit) * (index % 2 === 0 ? 1 : 3), 0);
  return String((10 - (sum % 10)) % 10);
};

const buildGeneratedMraBarcode = (seed: string): string => {
  const hashValue = hashInitialStockBarcodeSeed(seed || 'PRODUCT');
  const base = `29${String(hashValue).padStart(10, '0').slice(-10)}`;
  return `${base}${calculateEan13CheckDigit(base)}`;
};

const resolveInitialStockBarcode = (item: InventoryItem): string => {
  const existingCode = [item.barcode, item.productCode, item.sku]
    .map((value) => String(value || '').trim())
    .find(isMraBarcode);
  if (existingCode) return existingCode;

  const seed = [
    item.id,
    item.branchId,
    item.name,
    item.category,
    item.itemType,
  ].map((value) => String(value || '').trim()).filter(Boolean).join('|');

  return buildGeneratedMraBarcode(seed);
};

const mapItemToInitialStockRow = (item: InventoryItem): InitialStockRow => {
  const quantityInStock = toFiniteNumber(item.stockUnits ?? item.stock_units, 0);
  const reorderLevel = toFiniteNumber(item.reorderLevel, 0);
  const unitPrice = toFiniteNumber((item as any).unitPrice ?? item.price ?? item.cost, 0);
  const costPrice = toFiniteNumber(item.cost ?? unitPrice, 0);
  const sellingPrice = toFiniteNumber(item.price ?? unitPrice, 0);
  const overQuantityStockLevel = toFiniteNumber(
    (item as any).overQuantityStockLevel ?? (item as any).overstockLevel,
    Math.max(quantityInStock, reorderLevel)
  );

  return {
    BarCode: resolveInitialStockBarcode(item),
    ProductName: String(item.name || '').trim(),
    ProductDescription: resolveProductDescription(item),
    QuantityInStock: formatFlexibleNumber(quantityInStock),
    UnitPrice: formatMoney(unitPrice),
    CostPrice: formatMoney(costPrice),
    SellingPrice: formatMoney(sellingPrice),
    ReorderLevel: formatFlexibleNumber(reorderLevel),
    OverQuantityStockLevel: formatFlexibleNumber(overQuantityStockLevel),
  };
};

const resolveInitialStockExportLimit = (
  mode: InitialStockExportMode,
  customLimit: string
): number | null => {
  if (mode === 'all') return null;
  if (mode === 'first_20') return 20;

  const parsedLimit = Number.parseInt(String(customLimit || '').trim(), 10);
  return Number.isFinite(parsedLimit) && parsedLimit > 0 ? parsedLimit : 0;
};

const mapInitialStockRowToApiProduct = (row: InitialStockRow): InitialStockApiProduct => ({
  BarCode: row.BarCode,
  ProductName: row.ProductName,
  ProductDescription: row.ProductDescription,
  QuantityInStock: toFiniteNumber(row.QuantityInStock),
  UnitPrice: toFiniteNumber(row.UnitPrice),
  CostPrice: toFiniteNumber(row.CostPrice),
  SellingPrice: toFiniteNumber(row.SellingPrice),
  ReorderLevel: toFiniteNumber(row.ReorderLevel),
  OverQuantityStockLevel: toFiniteNumber(row.OverQuantityStockLevel),
});

const extractInitialStockProductsFromJson = (payload: any): any[] => {
  if (Array.isArray(payload)) return payload;

  const candidates = [
    payload?.Products,
    payload?.products,
    payload?.items,
    payload?.data?.Products,
    payload?.data?.products,
    payload?.data?.items,
    payload?.response?.data?.Products,
    payload?.response?.data?.products,
  ];

  const products = candidates.find(Array.isArray);
  if (!products) {
    throw new Error('No Products array found in the selected JSON file.');
  }

  return products;
};

const extractMraResponseData = (response: any): any[] => {
  if (Array.isArray(response)) return response;
  if (Array.isArray(response?.data)) return response.data;
  if (Array.isArray(response?.response?.data)) return response.response.data;
  if (Array.isArray(response?.results)) return response.results;
  return [];
};

interface TerminalActivationResult {
  state?: string;
  dry_run?: boolean;
  dry_run_reason?: string;
  endpoint?: string;
  status_code?: number | null;
  confirmation_state?: string;
  error?: string;
  created_at?: string;
}

interface TerminalBlockingStatus {
  is_blocked?: boolean | null;
  is_unblocked?: boolean | null;
  blocking_reason?: string | null;
  blocked_at?: string | null;
  checked_at?: string | null;
  source?: string | null;
}

interface TerminalHealthCheck {
  checked?: boolean;
  dry_run?: boolean;
  is_online?: boolean;
  previous_online?: boolean;
  endpoint?: string | null;
  endpoint_key?: string | null;
  status_code?: number | null;
  response?: unknown;
  error?: string;
  checked_at?: string;
  server_time?: string | null;
  server_time_raw?: string | null;
  server_time_source?: string | null;
}

interface Terminal {
  id: string;
  business?: string;
  branch?: string;
  terminal_id: string;
  device_serial?: string;
  mac_address?: string;
  status: 'pending_activation' | 'active' | 'suspended' | 'deactivated';
  is_online: boolean;
  online_invoice_counter: number;
  offline_invoice_counter: number;
  pending_offline_invoices?: number;
  pos_name: string;
  pos_version: string;
  os_type: string;
  activated_at?: string;
  last_sync_at?: string;
  token_expires_at?: string;
  has_mra_token?: boolean;
  activation_result?: TerminalActivationResult | null;
  blocking_status?: TerminalBlockingStatus | null;
  health_check?: TerminalHealthCheck | null;
}

type LastSubmitMode = 'online' | 'offline';

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
  terminal_counter_updated?: boolean;
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

interface Branch {
  id: string;
  name: string;
  address?: string;
}

const REQUIRED_CONFIG_TYPES = ['tax_rules', 'receipt_format', 'product_codes'] as const;
type RequiredConfigType = typeof REQUIRED_CONFIG_TYPES[number];

interface ConfigurationStatus {
  synced: boolean;
  version: string | null;
}

interface SyncedConfiguration {
  id: string;
  config_type: string;
  config_version?: string | null;
  config_data?: unknown;
  effective_from?: string | null;
  effective_to?: string | null;
  fetched_from_mra_at?: string | null;
  is_active?: boolean;
  created_at?: string | null;
}

type ConfigurationStatusMap = Record<RequiredConfigType, ConfigurationStatus>;

const CONFIG_LABELS: Record<RequiredConfigType, string> = {
  tax_rules: 'Tax Rules',
  receipt_format: 'Receipt Format',
  product_codes: 'Product Codes',
};

const ALL_CONFIG_LABELS: Record<string, string> = {
  tax_rules: 'Tax Rules',
  receipt_format: 'Receipt Format',
  product_codes: 'Product Codes',
  system_settings: 'System Settings',
  global_configuration: 'Global Configuration',
  terminal_configuration: 'Terminal Configuration',
  taxpayer_configuration: 'Taxpayer Configuration',
  terminal_site_products: 'Terminal Site Products',
};

const createDefaultConfigurationStatus = (): ConfigurationStatusMap => ({
  tax_rules: { synced: false, version: null },
  receipt_format: { synced: false, version: null },
  product_codes: { synced: false, version: null },
});

const isRequiredConfigType = (value: string): value is RequiredConfigType => {
  return REQUIRED_CONFIG_TYPES.includes(value as RequiredConfigType);
};

const getConfigurationLabel = (configType: string): string => {
  if (ALL_CONFIG_LABELS[configType]) return ALL_CONFIG_LABELS[configType];
  return String(configType || 'Configuration')
    .split('_')
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
};

const getConfigDataObject = (config: SyncedConfiguration): Record<string, any> => {
  return config.config_data && typeof config.config_data === 'object' && !Array.isArray(config.config_data)
    ? config.config_data as Record<string, any>
    : {};
};

const getConfigDataItems = (config: SyncedConfiguration): unknown[] => {
  const data = config.config_data as any;
  if (Array.isArray(data)) return data;
  const candidates = [data?.items, data?.products, data?.terminalSiteProducts, data?.taxrates, data?.taxRates];
  const found = candidates.find(Array.isArray);
  return found || [];
};

const summarizeConfiguration = (config: SyncedConfiguration): Array<{ label: string; value: string }> => {
  const data = getConfigDataObject(config);
  const items = getConfigDataItems(config);
  const terminalSite = data.terminalSite || data.site || {};
  const offlineLimit = data.offlineLimit || data.offlineLimits || data.OfflineLimit || {};
  const taxRates = Array.isArray(data.taxrates)
    ? data.taxrates
    : Array.isArray(data.taxRates)
      ? data.taxRates
      : [];

  const values: Array<{ label: string; value: string }> = [];
  const add = (label: string, value: unknown) => {
    const normalized = String(value ?? '').trim();
    if (normalized) values.push({ label, value: normalized });
  };

  add('Version', data.versionNo ?? data.version ?? data.configVersion ?? config.config_version);
  add('Site ID', terminalSite.siteId ?? data.siteId);
  add('TIN', data.tin ?? data.TIN ?? data.taxpayerTin ?? data.taxpayerTIN);
  add('Taxpayer', data.taxpayerName ?? data.name ?? data.businessName);
  if (taxRates.length > 0) add('Tax rates', taxRates.length);
  if (items.length > 0) add('Items', items.length);
  add('Offline age hours', offlineLimit.maxTransactionAgeInHours ?? offlineLimit.maxOfflineTransactionAgeInHours);
  add('Offline amount limit', offlineLimit.maxCummulativeAmount ?? offlineLimit.maxCumulativeAmount);

  return values;
};

const extractApiList = <T,>(response: any): T[] => {
  if (Array.isArray(response)) return response as T[];
  if (Array.isArray(response?.results)) return response.results as T[];
  if (Array.isArray(response?.data)) return response.data as T[];
  return [];
};

const getApiBranchId = (item: any): string => {
  const rawBranch = item?.branch;
  if (rawBranch && typeof rawBranch === 'object') {
    return String(rawBranch.id ?? rawBranch.pk ?? rawBranch.branch_id ?? '');
  }
  return String(rawBranch ?? item?.branch_id ?? item?.branchId ?? '');
};

const formatReconciliationValue = (value: unknown): string => {
  if (Array.isArray(value)) {
    return value.map(formatReconciliationValue).filter(Boolean).join(', ');
  }
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
  return (
    result.error ||
    formatReconciliationValue(result.errors) ||
    formatReconciliationValue(result.reason)
  );
};

const formatUpdatedRecords = (records?: string[]): string => {
  if (!records?.length) return 'None';
  return records.map((record) => String(record).replace(/_/g, ' ')).join(', ');
};

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

const extractInvoiceNumberFromResponse = (payload: any): string => {
  const inner = payload?.data && typeof payload.data === 'object' ? payload.data : payload;
  return String(
    inner?.invoiceHeader?.invoiceNumber ??
      inner?.invoiceNumber ??
      inner?.invoice_number ??
      inner?.receiptNumber ??
      inner?.receipt_number ??
      payload?.invoiceHeader?.invoiceNumber ??
      payload?.invoiceNumber ??
      payload?.receiptNumber ??
      ''
  ).trim();
};

const normalizeLastSubmitResult = (result: any): LastSubmitModeResult | undefined => {
  if (!result || typeof result !== 'object') return undefined;
  const responsePayload = result.response || result.data || {};
  const invoiceNumber = String(
    result.invoiceNumber ??
      result.invoice_number ??
      result.receiptNumber ??
      result.receipt_number ??
      extractInvoiceNumberFromResponse(responsePayload) ??
      ''
  ).trim();
  const sequence = result.sequence ?? result.invoice_sequence ?? result.count ?? null;
  const validationUrl = String(
    result.validation_url ??
      result.validationURL ??
      result.validationUrl ??
      responsePayload?.validationURL ??
      responsePayload?.validationUrl ??
      responsePayload?.data?.validationURL ??
      responsePayload?.data?.validationUrl ??
      ''
  ).trim();

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
    normalizedResults[String(key).toLowerCase()] = normalizeLastSubmitResult(value);
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

const CONFIGURED_POS_NAME = (
  process.env.NEXT_PUBLIC_MRA_EIS_POS_NAME ||
  process.env.NEXT_PUBLIC_POS_NAME ||
  process.env.NEXT_PUBLIC_APP_NAME ||
  'HandyPOS'
).trim() || 'HandyPOS';

const normalizeMacAddress = (value?: string | null): string => {
  return normalizeDeviceMacAddress(value) || DEFAULT_DEVICE_MAC_ADDRESS;
};

export default function EISSettingsPage() {
  const ACTIVE_BRANCH_STORAGE_KEY = 'handypos-active-branch';
  const { business, loading: isAuthLoading } = useAuth();
  const searchParams = useSearchParams();
  const activationRequired = searchParams.get('activationRequired') === '1';
  const requestedBranchId = searchParams.get('branch') || '';
  const [branches, setBranches] = useState<Branch[]>([]);
  const [terminal, setTerminal] = useState<Terminal | null>(null);
  const [isLoadingEisSettings, setIsLoadingEisSettings] = useState(true);
  const [isLoadingBranches, setIsLoadingBranches] = useState(true);
  const [isTerminalDataReady, setIsTerminalDataReady] = useState(false);
  const [isInitialConfigurationReady, setIsInitialConfigurationReady] = useState(false);
  const [isActivatingTerminal, setIsActivatingTerminal] = useState(false);
  const [isLoadingTerminal, setIsLoadingTerminal] = useState(false);
  const [isRefreshingTerminalStatus, setIsRefreshingTerminalStatus] = useState(false);
  const [isRefreshingTerminalToken, setIsRefreshingTerminalToken] = useState(false);
  const [isResettingTerminalActivation, setIsResettingTerminalActivation] = useState(false);
  const [isCheckingTerminalBlockStatus, setIsCheckingTerminalBlockStatus] = useState(false);
  const [isCheckingLastSubmits, setIsCheckingLastSubmits] = useState(false);
  const [lastSubmitReconciliation, setLastSubmitReconciliation] = useState<LastSubmitReconciliation | null>(null);
  const [isActivationModalOpen, setIsActivationModalOpen] = useState(false);
  const [isSyncingConfigurations, setIsSyncingConfigurations] = useState(false);
  const [isExportingInitialStock, setIsExportingInitialStock] = useState(false);
  const [isPreparingInitialStockPreview, setIsPreparingInitialStockPreview] = useState(false);
  const [isSubmittingInitialStock, setIsSubmittingInitialStock] = useState(false);
  const [isImportingInitialStock, setIsImportingInitialStock] = useState(false);
  const [isPullingApprovedProducts, setIsPullingApprovedProducts] = useState(false);
  const [isCreateProductModalOpen, setIsCreateProductModalOpen] = useState(false);
  const [isLoadingProductLookups, setIsLoadingProductLookups] = useState(false);
  const [isSubmittingMraProduct, setIsSubmittingMraProduct] = useState(false);
  const [mraHsCodes, setMraHsCodes] = useState<MRAHsCode[]>([]);
  const [mraUnitsOfMeasure, setMraUnitsOfMeasure] = useState<MRAUnitOfMeasure[]>([]);
  const [mraProductForm, setMraProductForm] = useState<MRAProductCreateForm>({
    barcode: '',
    hsCode: '',
    name: '',
    description: '',
    uom: '',
  });
  const [initialStockExportProgress, setInitialStockExportProgress] = useState(0);
  const [initialStockExportStage, setInitialStockExportStage] = useState('');
  const [initialStockExportMode, setInitialStockExportMode] = useState<InitialStockExportMode>('all');
  const [initialStockCustomLimit, setInitialStockCustomLimit] = useState('20');
  const [initialStockIsLastBatch, setInitialStockIsLastBatch] = useState(false);
  const [initialStockSubmissionPreview, setInitialStockSubmissionPreview] = useState<InitialStockSubmissionPreview | null>(null);
  const [isInitialStockPreviewOpen, setIsInitialStockPreviewOpen] = useState(false);
  const [configurationStatus, setConfigurationStatus] = useState<ConfigurationStatusMap>(createDefaultConfigurationStatus);
  const [syncedConfigurations, setSyncedConfigurations] = useState<SyncedConfiguration[]>([]);
  const [isLoadingConfigurationStatus, setIsLoadingConfigurationStatus] = useState(false);
  const [showTacPassword, setShowTacPassword] = useState(false);
  const [deviceIdentityRefreshKey, setDeviceIdentityRefreshKey] = useState(0);
  const terminalLoadRequestRef = useRef(0);
  const configurationContextKeyRef = useRef('');
  const configurationLoadRequestRef = useRef(0);
  const initialStockImportFileRef = useRef<HTMLInputElement | null>(null);
  const [expandedSections, setExpandedSections] = useState<Record<string, boolean>>({
    setup: true,
    terminal: true,
    configuration: true,
    products: true,
  });

  useEffect(() => {
    if (!activationRequired) {
      return;
    }

    setExpandedSections((previous) => ({
      ...previous,
      setup: true,
      terminal: true,
    }));
    setIsActivationModalOpen(true);

    toast({
      variant: 'destructive',
      title: 'Activation required',
      description: 'Activate this device first.',
    });
  }, [activationRequired]);

  const getTerminalStorageKey = (businessId: string, branchId: string): string => {
    return `handypos-terminal:${businessId}:${branchId}`;
  };

  const mapTerminalFromApi = (payload: any, fallback?: Terminal | null): Terminal => {
    const source = payload?.terminal && typeof payload.terminal === 'object'
      ? { ...payload.terminal, ...payload }
      : (payload || {});
    const healthCheck = source?.health_check || fallback?.health_check || null;
    const parsedOnline = readBooleanFlag(source?.is_online ?? healthCheck?.is_online ?? fallback?.is_online);

    return {
      ...(fallback || {}),
      id: String(source?.id ?? fallback?.id ?? ''),
      business: source?.business ? String(source.business) : fallback?.business,
      branch: source?.branch ? String(source.branch) : fallback?.branch,
      terminal_id: String(source?.terminal_id ?? fallback?.terminal_id ?? ''),
      device_serial: source?.device_serial ? String(source.device_serial) : fallback?.device_serial,
      mac_address: source?.mac_address ? String(source.mac_address) : fallback?.mac_address,
      status: (source?.status || fallback?.status || 'pending_activation') as Terminal['status'],
      is_online: parsedOnline === null ? Boolean(fallback?.is_online) : parsedOnline,
      online_invoice_counter: Number(source?.online_invoice_counter ?? fallback?.online_invoice_counter ?? 0),
      offline_invoice_counter: Number(source?.offline_invoice_counter ?? fallback?.offline_invoice_counter ?? 0),
      pending_offline_invoices: Number(source?.pending_offline_invoices ?? fallback?.pending_offline_invoices ?? 0),
      pos_name: String(source?.pos_name ?? fallback?.pos_name ?? ''),
      pos_version: String(source?.pos_version ?? fallback?.pos_version ?? ''),
      os_type: String(source?.os_type ?? fallback?.os_type ?? ''),
      activated_at: source?.activated_at || fallback?.activated_at || undefined,
      last_sync_at: source?.last_sync_at || fallback?.last_sync_at || undefined,
      token_expires_at: source?.token_expires_at || fallback?.token_expires_at || undefined,
      has_mra_token: Boolean(source?.has_mra_token ?? fallback?.has_mra_token),
      activation_result: source?.activation_result || fallback?.activation_result || null,
      blocking_status: source?.blocking_status || fallback?.blocking_status || null,
      health_check: healthCheck,
    };
  };

  const eisForm = useForm<EISSetupFormValues>({
    resolver: zodResolver(eisSetupSchema),
    defaultValues: {
      enableEis: false,
      tin: '',
      vatRegistrationNumber: '',
      vatRegistered: false,
      mraTaxpayerType: 'NON_VAT',
      mraEnrolled: false,
      eisEnvironment: 'TEST',
      blockSalesIfEisDown: true,
      blockSalesIfTaxMappingMissing: true,
    },
  });

  const terminalForm = useForm<TerminalActivationFormValues>({
    resolver: zodResolver(terminalActivationSchema),
    defaultValues: {
      activeBranch: '',
      pos_name: CONFIGURED_POS_NAME,
      pos_version: '1.0.0',
      os_type: getDetectedOS(),
      device_serial: getDeviceSerial(),
      mac_address: getDeviceMacAddress(),
    },
  });

  const activeBranchId = terminalForm.watch('activeBranch');
  const isEisEnabled = eisForm.watch('enableEis');
  const activationUiEnabled = isEisEnabled || activationRequired;
  const tinValue = eisForm.watch('tin');
  const currentDeviceSerial = useMemo(
    () => (typeof window === 'undefined' ? '' : getDeviceSerial()),
    [deviceIdentityRefreshKey]
  );
  const terminalDeviceMismatch = Boolean(
    terminal?.status === 'active' &&
    terminal.device_serial &&
    currentDeviceSerial &&
    terminal.device_serial.toLowerCase() !== currentDeviceSerial.toLowerCase()
  );
  const terminalHasBeenActivated = terminal?.status === 'active';
  const terminalIsActive = terminalHasBeenActivated && !terminalDeviceMismatch;
  const terminalIsBlocked = terminal?.status === 'suspended' || terminal?.blocking_status?.is_blocked === true;
  const terminalHasToken = Boolean(terminal?.has_mra_token || terminal?.token_expires_at);
  const showActivationForm = !terminal || terminal.status !== 'active' || terminalDeviceMismatch;
  const terminalStatusCardClassName = terminalDeviceMismatch
    ? 'p-4 rounded-lg border border-destructive/40 bg-destructive/10 space-y-3'
    : 'p-4 rounded-lg border border-emerald-500/30 bg-emerald-500/10 space-y-3';

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }

    const refreshDeviceIdentity = (identity?: { deviceSerial?: string; macAddress?: string }) => {
      const resolvedDeviceSerial = identity?.deviceSerial || getDeviceSerial();
      const resolvedMacAddress = normalizeMacAddress(identity?.macAddress || getDeviceMacAddress());
      if (resolvedDeviceSerial) {
        terminalForm.setValue('device_serial', resolvedDeviceSerial, { shouldValidate: true });
      }
      terminalForm.setValue('mac_address', resolvedMacAddress, { shouldValidate: true });
      setDeviceIdentityRefreshKey((value) => value + 1);
    };

    void Promise.all([
      ensureTauriDeviceIdentity(),
      ensureTauriDeviceMacAddress(),
    ]).then(([deviceSerial, macAddress]) => {
      refreshDeviceIdentity({ deviceSerial, macAddress });
    });

    const handleDeviceIdentityChanged = (event: Event) => {
      const customEvent = event as CustomEvent<{ deviceSerial?: string; macAddress?: string }>;
      refreshDeviceIdentity(customEvent.detail);
    };

    window.addEventListener(DEVICE_IDENTITY_CHANGED_EVENT, handleDeviceIdentityChanged);
    return () => {
      window.removeEventListener(DEVICE_IDENTITY_CHANGED_EVENT, handleDeviceIdentityChanged);
    };
  }, [terminalForm]);

  const persistBusinessSettingsCache = useCallback((updates: Record<string, unknown>) => {
    if (typeof window === 'undefined') return;
    let existing: Record<string, unknown> = {};
    try {
      const raw = localStorage.getItem('handypos-business-settings');
      if (raw) {
        existing = JSON.parse(raw);
      }
    } catch (error) {
      console.warn('[EIS Settings] Failed to parse cached business settings:', error);
      existing = {};
    }

    const next = { ...existing, ...updates };
    if (business?.id) {
      next.businessId = String(business.id);
    }
    localStorage.setItem('handypos-business-settings', JSON.stringify(next));
    window.dispatchEvent(new Event('handypos-business-settings-changed'));
  }, [business?.id]);

  const persistTerminalCache = useCallback((branchId: string, value: Terminal | null) => {
    if (!business?.id || !branchId || typeof window === 'undefined') return;
    const cacheKey = getTerminalStorageKey(String(business.id), String(branchId));
    if (value) {
      localStorage.setItem(cacheKey, JSON.stringify(value));
    } else {
      localStorage.removeItem(cacheKey);
    }
  }, [business?.id]);

  const loadTerminalForBranch = useCallback(async (branchId: string) => {
    const requestId = ++terminalLoadRequestRef.current;
    const isCurrentRequest = () => terminalLoadRequestRef.current === requestId;
    setIsTerminalDataReady(false);

    if (!business?.id || !branchId) {
      if (isCurrentRequest()) {
        setTerminal(null);
        setIsLoadingTerminal(false);
        setIsTerminalDataReady(true);
      }
      return;
    }

    setIsLoadingTerminal(true);
    setLastSubmitReconciliation(null);
    try {
      await ensureTauriDeviceIdentity();
      const response = await authFetch.fetch<any>('/mra-eis/terminals/');
      const terminals = extractApiList<any>(response);
      const branchCandidates = new Set(getBranchIdCandidates(branchId).map(toBackendBranchId));
      const branchTerminals = terminals.filter((item) => branchCandidates.has(toBackendBranchId(getApiBranchId(item))));
      const deviceSerial = getDeviceSerial();
      const selected = (
        branchTerminals.find((item) => String(item?.device_serial || '').toLowerCase() === deviceSerial.toLowerCase()) ||
        branchTerminals.find((item) => String(item?.status || '').toLowerCase() === 'active') ||
        branchTerminals[0] ||
        null
      );

      if (!isCurrentRequest()) return;

      if (selected) {
        let mapped = mapTerminalFromApi(selected);
        setTerminal(mapped);
        persistTerminalCache(branchId, mapped);
        try {
          const statusResponse = await authFetch.fetch<any>(`/mra-eis/terminals/${mapped.id}/status/?ping=true`);
          if (!isCurrentRequest()) return;
          mapped = mapTerminalFromApi(statusResponse, mapped);
          setTerminal(mapped);
          persistTerminalCache(branchId, mapped);
        } catch (statusError) {
          console.warn('[EIS Settings] Failed to refresh terminal ping status:', statusError);
        }
      } else {
        if (!isCurrentRequest()) return;
        setTerminal(null);
        persistTerminalCache(branchId, null);
      }
    } catch (error) {
      if (!isCurrentRequest()) return;
      console.error('Error loading terminal from backend:', error);
      const cacheKey = getTerminalStorageKey(String(business.id), String(branchId));
      const cachedTerminal = localStorage.getItem(cacheKey);
      if (cachedTerminal) {
        try {
          setTerminal(JSON.parse(cachedTerminal));
        } catch {
          setTerminal(null);
        }
      } else {
        setTerminal(null);
      }
    } finally {
      if (isCurrentRequest()) {
        setIsLoadingTerminal(false);
        setIsTerminalDataReady(true);
      }
    }
  }, [business?.id, persistTerminalCache]);

  const loadConfigurationStatus = useCallback(async () => {
    if (!business?.id) {
      setConfigurationStatus(createDefaultConfigurationStatus());
      setSyncedConfigurations([]);
      return;
    }

    setIsLoadingConfigurationStatus(true);
    try {
      const response = await authFetch.fetch<any>(`/mra-eis/configurations/?business_id=${business.id}`);
      const configList = Array.isArray(response)
        ? response
        : Array.isArray(response?.results)
          ? response.results
          : [];
      const visibleConfigs = configList.map((item: any) => ({
        id: String(item?.id || `${item?.config_type || 'config'}-${item?.config_version || ''}`),
        config_type: String(item?.config_type || ''),
        config_version: item?.config_version ? String(item.config_version) : null,
        config_data: item?.config_data ?? {},
        effective_from: item?.effective_from || null,
        effective_to: item?.effective_to || null,
        fetched_from_mra_at: item?.fetched_from_mra_at || null,
        is_active: item?.is_active !== false,
        created_at: item?.created_at || null,
      })).sort((a: SyncedConfiguration, b: SyncedConfiguration) => {
        const typeCompare = getConfigurationLabel(a.config_type).localeCompare(getConfigurationLabel(b.config_type));
        if (typeCompare !== 0) return typeCompare;
        return String(b.effective_from || b.created_at || '').localeCompare(String(a.effective_from || a.created_at || ''));
      });

      const nextStatus = createDefaultConfigurationStatus();
      for (const item of configList) {
        const configType = String(item?.config_type || '');
        if (!isRequiredConfigType(configType)) continue;
        if (nextStatus[configType].synced) continue;
        nextStatus[configType] = {
          synced: item?.is_active !== false,
          version: item?.config_version ? String(item.config_version) : null,
        };
      }

      setSyncedConfigurations(visibleConfigs);
      setConfigurationStatus(nextStatus);
    } catch (error) {
      console.error('Error loading configuration status:', error);
      setConfigurationStatus(createDefaultConfigurationStatus());
      setSyncedConfigurations([]);
    } finally {
      setIsLoadingConfigurationStatus(false);
    }
  }, [business?.id]);

  // Load business settings
  useEffect(() => {
    let cancelled = false;

    if (!business?.id) {
      setIsLoadingEisSettings(isAuthLoading);
      return () => {
        cancelled = true;
      };
    }

    setIsLoadingEisSettings(true);
    const loadSettings = async () => {
      try {
        const backendBusiness = await authFetch.fetch<any>(`/business/businesses/${business.id}/`);
          
          if (backendBusiness) {
            const rawEnableEis =
              backendBusiness.enable_eis ??
              backendBusiness.enableEis ??
              backendBusiness.eis_enabled ??
              backendBusiness.eisEnabled;
            const enableEisValue = readBooleanFlag(rawEnableEis) ?? activationRequired;
            const vatRegisteredValue = backendBusiness.vat_registered === true || backendBusiness.vat_registered === 'true';
            const mraEnrolledValue = backendBusiness.mra_enrolled === true || backendBusiness.mra_enrolled === 'true';
            const blockSalesValue = backendBusiness.block_sales_if_eis_down !== false && backendBusiness.block_sales_if_eis_down !== 'false';
            const rawBlockTaxMapping = backendBusiness.block_sales_if_tax_mapping_missing ?? backendBusiness.blockSalesIfTaxMappingMissing;
            const blockTaxMappingValue = rawBlockTaxMapping === undefined
              ? enableEisValue
              : rawBlockTaxMapping !== false && rawBlockTaxMapping !== 'false';
            
            eisForm.reset({
              enableEis: enableEisValue,
              tin: backendBusiness.tin || '',
              vatRegistrationNumber: backendBusiness.vat_registration_number || '',
              vatRegistered: vatRegisteredValue,
              mraTaxpayerType: backendBusiness.mra_taxpayer_type || 'NON_VAT',
              mraEnrolled: mraEnrolledValue,
              eisEnvironment: backendBusiness.eis_environment || 'TEST',
              blockSalesIfEisDown: blockSalesValue,
              blockSalesIfTaxMappingMissing: blockTaxMappingValue,
            });
          }
      } catch (error) {
        console.error('Error loading EIS settings:', error);
        if (activationRequired) {
          eisForm.setValue('enableEis', true, { shouldValidate: true });
        }
      } finally {
        if (!cancelled) setIsLoadingEisSettings(false);
      }
    };

    void loadSettings();
    return () => {
      cancelled = true;
    };
  }, [activationRequired, business?.id, eisForm, isAuthLoading]);

  // Load branches
  useEffect(() => {
    let cancelled = false;

    if (!business?.id) {
      setIsLoadingBranches(isAuthLoading);
      return () => {
        cancelled = true;
      };
    }

    setIsLoadingBranches(true);
    const loadBranches = async () => {
      try {
        const response = await authFetch.fetch<any>(`/business/businesses/${business.id}/`);
          if (response?.branches && Array.isArray(response.branches)) {
            const mappedBranches: Branch[] = response.branches.map((branch: any) => ({
              id: String(branch.id),
              name: branch.name || 'Branch',
              address: branch.address || '',
            }));
            if (cancelled) return;
            setBranches(mappedBranches);

            const currentBranch = terminalForm.getValues('activeBranch');
            const storedBranch = localStorage.getItem(ACTIVE_BRANCH_STORAGE_KEY) || '';
            const hasCurrent = mappedBranches.some((branch) => branch.id === currentBranch);
            const hasRequested = mappedBranches.some((branch) => branch.id === requestedBranchId);
            const hasStored = mappedBranches.some((branch) => branch.id === storedBranch);

            const nextBranch = hasCurrent
              ? currentBranch
              : hasRequested
                ? requestedBranchId
                : hasStored
                  ? storedBranch
                  : mappedBranches[0]?.id || '';

            if (nextBranch) {
              terminalForm.setValue('activeBranch', nextBranch, { shouldValidate: true });
            }
        } else {
          if (cancelled) return;
          setBranches([]);
          terminalForm.setValue('activeBranch', '');
        }
      } catch (error) {
        console.error('Error loading branches:', error);
      } finally {
        if (!cancelled) setIsLoadingBranches(false);
      }
    };

    void loadBranches();
    return () => {
      cancelled = true;
    };
  }, [business?.id, requestedBranchId, terminalForm, ACTIVE_BRANCH_STORAGE_KEY, isAuthLoading]);

  useEffect(() => {
    if (!business?.id || !activeBranchId) {
      setTerminal(null);
      setLastSubmitReconciliation(null);
      return;
    }
    localStorage.setItem(ACTIVE_BRANCH_STORAGE_KEY, activeBranchId);
    loadTerminalForBranch(activeBranchId);
  }, [business?.id, activeBranchId, loadTerminalForBranch, ACTIVE_BRANCH_STORAGE_KEY]);

  useEffect(() => {
    if (typeof window === 'undefined') return;

    const resetBranchScopedState = () => {
      setTerminal(null);
      setIsTerminalDataReady(false);
      setLastSubmitReconciliation(null);
      setConfigurationStatus(createDefaultConfigurationStatus());
      setIsInitialConfigurationReady(false);
      configurationContextKeyRef.current = '';
      setSyncedConfigurations([]);
      setInitialStockSubmissionPreview(null);
      setIsInitialStockPreviewOpen(false);
      setIsActivationModalOpen(false);
      setMraHsCodes([]);
      setMraUnitsOfMeasure([]);
    };

    const applyBranchChange = (rawBranchId?: unknown) => {
      const nextBranchId = String(
        rawBranchId ?? localStorage.getItem(ACTIVE_BRANCH_STORAGE_KEY) ?? ''
      ).trim();
      if (!nextBranchId || nextBranchId === terminalForm.getValues('activeBranch')) {
        return;
      }

      resetBranchScopedState();
      terminalForm.setValue('activeBranch', nextBranchId, { shouldValidate: true });
    };

    const handleBranchChanged = (event: Event) => {
      applyBranchChange((event as CustomEvent).detail?.branchId);
    };

    const handleStorageChanged = (event: StorageEvent) => {
      if (event.key === ACTIVE_BRANCH_STORAGE_KEY) {
        applyBranchChange(event.newValue);
      }
    };

    window.addEventListener('branchChanged', handleBranchChanged);
    window.addEventListener('handypos-active-branch-changed', handleBranchChanged);
    window.addEventListener('storage', handleStorageChanged);

    return () => {
      window.removeEventListener('branchChanged', handleBranchChanged);
      window.removeEventListener('handypos-active-branch-changed', handleBranchChanged);
      window.removeEventListener('storage', handleStorageChanged);
    };
  }, [ACTIVE_BRANCH_STORAGE_KEY, terminalForm]);
  useEffect(() => {
    const configurationContextKey = `${business?.id || ''}:${activeBranchId || ''}:${activationUiEnabled ? 'enabled' : 'disabled'}`;

    if (!business?.id || !activationUiEnabled) {
      setConfigurationStatus(createDefaultConfigurationStatus());
      setSyncedConfigurations([]);
      setIsInitialConfigurationReady(true);
      configurationContextKeyRef.current = configurationContextKey;
      return;
    }

    const requestId = ++configurationLoadRequestRef.current;
    const shouldGatePage = configurationContextKeyRef.current !== configurationContextKey;
    if (shouldGatePage) setIsInitialConfigurationReady(false);

    void loadConfigurationStatus().finally(() => {
      if (configurationLoadRequestRef.current !== requestId) return;
      if (shouldGatePage) {
        configurationContextKeyRef.current = configurationContextKey;
        setIsInitialConfigurationReady(true);
      }
    });
  }, [activationUiEnabled, activeBranchId, business?.id, terminal?.id, terminal?.status, loadConfigurationStatus]);

  const onEISSetupSubmit = async (data: EISSetupFormValues) => {
    if (!business?.id) {
      toast({
        variant: 'destructive',
        title: 'Error',
        description: 'No business selected.',
      });
      return;
    }

    try {
      // Fetch current business data to include required fields
      const currentBusiness = await authFetch.fetch<any>(`/business/businesses/${business.id}/`);
      
      const backendPayload = {
        // Include required fields from current business
        name: currentBusiness?.name || '',
        business_type: currentBusiness?.business_type || 'generic',
        email: currentBusiness?.email || '',
        phone: currentBusiness?.phone || '',
        address: currentBusiness?.address || '',
        website: currentBusiness?.website || '',
        // MRA EIS Fields
        enable_eis: data.enableEis,
        tin: data.tin || '',
        vat_registration_number: data.vatRegistrationNumber || '',
        vat_registered: data.vatRegistered,
        mra_taxpayer_type: data.mraTaxpayerType,
        mra_enrolled: data.mraEnrolled,
        eis_environment: data.eisEnvironment,
        block_sales_if_eis_down: data.blockSalesIfEisDown,
        block_sales_if_tax_mapping_missing: data.blockSalesIfTaxMappingMissing,
      };



      const response = await authFetch.fetch(`/business/businesses/${business.id}/`, {
        method: 'PUT',
        body: JSON.stringify(backendPayload),
      });

      if (response) {

        persistBusinessSettingsCache({
          enableEis: data.enableEis,
          eisEnvironment: data.eisEnvironment,
          blockSalesIfEisDown: data.blockSalesIfEisDown,
          blockSalesIfTaxMappingMissing: data.blockSalesIfTaxMappingMissing,
          tin: data.tin || '',
          vatRegistered: data.vatRegistered,
          mraTaxpayerType: data.mraTaxpayerType,
        });
        toast({
          title: 'Settings saved!',
          description: 'Your EIS settings have been updated.',
        });
      }
    } catch (error) {
      console.error('Error saving EIS settings:', error);
      toast({
        variant: 'destructive',
        title: 'Error',
        description: error instanceof Error ? error.message : 'Failed to save settings.',
      });
    }
  };

  const onTerminalActivation = async (data: TerminalActivationFormValues) => {
    if (!business?.id) {
      toast({
        variant: 'destructive',
        title: 'Error',
        description: 'Business not selected.',
      });
      return;
    }

    const branchId = activeBranchId || data.activeBranch;
    if (!branchId) {
      toast({
        variant: 'destructive',
        title: 'Select branch',
        description: 'Select a branch before activating this device.',
      });
      return;
    }

    const deviceSerial = data.device_serial || getDeviceSerial();
    const macAddress = normalizeMacAddress(data.mac_address);
    const osType = data.os_type || getDetectedOS();
    const posVersion = data.pos_version || '1.0.0';

    setIsActivatingTerminal(true);
    try {
      const response = await authFetch.fetch<any>(
        `/mra-eis/terminals/activate/?business_id=${business.id}&branch_id=${branchId}`,
        {
          method: 'POST',
          body: JSON.stringify({
            tac_code: data.tac_code,
            pos_name: CONFIGURED_POS_NAME,
            pos_version: posVersion,
            os_type: osType,
            device_serial: deviceSerial,
            mac_address: macAddress,
          }),
        }
      );

      if (response?.id) {
        const terminalData = mapTerminalFromApi(response);
        const activationResult = terminalData.activation_result;
        const activationWasDryRun = Boolean(activationResult?.dry_run);
        const activationError = activationResult?.error;
        const shouldReloadAfterActivation = terminalData.status === 'active' && !activationWasDryRun && !activationError;
        const activationToastTitle = activationWasDryRun
          ? 'Activation prepared'
          : activationError
            ? 'Activation failed'
            : terminalData.status === 'active'
              ? 'Terminal activated'
              : 'Terminal registered';
        const activationToastDescription = activationWasDryRun
          ? 'Not sent to MRA.'
          : activationError
            ? 'Check the TAC.'
            : terminalData.status === 'active'
              ? 'Reloading app.'
              : 'Activation submitted.';
        setTerminal(terminalData);
        setLastSubmitReconciliation(null);
        persistTerminalCache(branchId, terminalData);
        setIsActivationModalOpen(false);
        if (typeof window !== 'undefined') {
          window.dispatchEvent(new Event(EIS_TERMINAL_ACTIVATION_CHANGED_EVENT));
        }

        toast({
          title: activationToastTitle,
          description: activationToastDescription,
        });

        terminalForm.reset({
          activeBranch: branchId,
          tac_code: '',
          pos_name: CONFIGURED_POS_NAME,
          pos_version: posVersion,
          os_type: osType,
          device_serial: deviceSerial,
          mac_address: macAddress,
        });

        if (shouldReloadAfterActivation && typeof window !== 'undefined') {
          window.setTimeout(() => {
            window.location.reload();
          }, ACTIVATION_RELOAD_DELAY_MS);
        }
      }
    } catch (error: any) {
      console.error('Terminal activation error:', error);
      toast({
        variant: 'destructive',
        title: 'Activation failed',
        description: 'Check the TAC.',
      });
    } finally {
      setIsActivatingTerminal(false);
    }
  };

  const onRefreshTerminalStatus = useCallback(async (options?: { silent?: boolean }) => {
    if (!terminal?.id) return;

    setIsRefreshingTerminalStatus(true);
    try {
      const statusResponse = await authFetch.fetch<any>(`/mra-eis/terminals/${terminal.id}/status/?ping=true`);
      const refreshedTerminal = mapTerminalFromApi(statusResponse, terminal);

      setTerminal(refreshedTerminal);
      if (activeBranchId) {
        persistTerminalCache(activeBranchId, refreshedTerminal);
      }
      if (refreshedTerminal.status === 'active') {
        await loadConfigurationStatus();
      }

      if (!options?.silent) {
        const serverTime = formatMraServerTime(
          refreshedTerminal.health_check?.server_time || refreshedTerminal.health_check?.server_time_raw
        );
        toast({
          title: refreshedTerminal.is_online ? 'MRA server is up' : 'MRA server is offline',
          description: serverTime
            ? `Server time ${serverTime}.`
            : refreshedTerminal.health_check?.checked
              ? `MRA is ${refreshedTerminal.is_online ? 'online' : 'offline'}.`
              : `Terminal is ${refreshedTerminal.status.replace('_', ' ')}.`,
        });
      }
    } catch (error: any) {
      console.error('Refresh terminal status error:', error);
      if (!options?.silent) {
        toast({
          variant: 'destructive',
          title: 'Failed to refresh status',
          description: error?.message || 'Could not fetch terminal status.',
        });
      }
    } finally {
      setIsRefreshingTerminalStatus(false);
    }
  }, [terminal, activeBranchId, persistTerminalCache, loadConfigurationStatus, toast]);

  const onCheckTerminalBlockStatus = async () => {
    if (!terminal?.id) {
      toast({
        variant: 'destructive',
        title: 'Activation required',
        description: 'Activate this device first.',
      });
      return;
    }

    setIsCheckingTerminalBlockStatus(true);
    try {
      const response = await authFetch.fetch<any>(`/mra-eis/terminals/${terminal.id}/check_blocking_status/`, {
        method: 'POST',
      });

      const refreshedTerminal = response?.terminal
        ? mapTerminalFromApi(response.terminal)
        : {
            ...terminal,
            status: (response?.status || terminal.status) as Terminal['status'],
            blocking_status: response?.blocking_status || terminal.blocking_status || null,
          };

      setTerminal(refreshedTerminal);
      if (activeBranchId) {
        persistTerminalCache(activeBranchId, refreshedTerminal);
      }

      toast({
        title: response?.is_blocked ? 'Terminal blocked' : 'Block status checked',
        description: response?.is_blocked
          ? 'Contact MRA.'
          : 'Not blocked.',
        variant: response?.is_blocked ? 'destructive' : undefined,
      });
    } catch (error: any) {
      console.error('Check terminal block status error:', error);
      toast({
        variant: 'destructive',
        title: 'Check failed',
        description: 'Try again.',
      });
    } finally {
      setIsCheckingTerminalBlockStatus(false);
    }
  };

  const onCheckLastSubmits = async () => {
    if (!terminal?.id) {
      toast({
        variant: 'destructive',
        title: 'Activation required',
        description: 'Activate this device first.',
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
      const resultValues = Object.values(normalizedResponse?.results || {});
      const errors = resultValues.filter((result) => result?.error || formatReconciliationValue(result?.errors)).length;
      const unmatched = Array.isArray(normalizedResponse?.unmatched) ? normalizedResponse.unmatched.length : 0;

      toast({
        title: 'Last submissions checked',
        description: matched > 0
          ? `${matched} MRA transaction${matched === 1 ? '' : 's'} matched local records.`
          : errors > 0
            ? 'MRA last submission check returned an error.'
            : unmatched > 0
              ? 'MRA returned last submissions with no local match.'
              : 'MRA last submission check completed.',
      });

      await onRefreshTerminalStatus({ silent: true });
    } catch (error: any) {
      console.error('Check last MRA submissions error:', error);
      toast({
        variant: 'destructive',
        title: 'Last submission check failed',
        description: error?.message || 'Could not check MRA last submissions.',
      });
    } finally {
      setIsCheckingLastSubmits(false);
    }
  };

  const formatTimestamp = (value?: string) => {
    if (!value) return 'N/A';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return 'N/A';
    return parsed.toLocaleString();
  };

  const formatMraServerTime = (value?: string | null) => {
    if (!value) return '';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
  };

  useEffect(() => {
    if (!terminal?.id || terminal.status !== 'active') {
      return;
    }

    void onRefreshTerminalStatus({ silent: true });
  }, [terminal?.id, terminal?.status]);

  useEffect(() => {
    if (!terminal?.id || terminal.status !== 'active') {
      return;
    }

    const intervalId = window.setInterval(() => {
      if (document.hidden) return;
      void onRefreshTerminalStatus({ silent: true });
    }, 30000);

    return () => window.clearInterval(intervalId);
  }, [terminal?.id, terminal?.status, onRefreshTerminalStatus]);

  const onRefreshTerminalToken = async () => {
    if (!terminal?.id) return;

    setIsRefreshingTerminalToken(true);
    try {
      const response = await authFetch.fetch<any>(`/mra-eis/terminals/${terminal.id}/refresh_token/`, {
        method: 'POST',
      });
      const refreshedTerminal = mapTerminalFromApi(response);
      setTerminal(refreshedTerminal);
      if (activeBranchId) {
        persistTerminalCache(activeBranchId, refreshedTerminal);
      }
      toast({
        title: 'Token refreshed',
        description: 'MRA terminal token has been refreshed.',
      });
    } catch (error: any) {
      console.error('Refresh terminal token error:', error);
      toast({
        variant: 'destructive',
        title: 'Token refresh failed',
        description: error?.message || 'Could not refresh MRA token.',
      });
    } finally {
      setIsRefreshingTerminalToken(false);
    }
  };

  const onResetFailedActivation = async () => {
    if (!terminal?.id) return;

    const confirmed = window.confirm(
      'Remove this local failed activation so you can retry terminal activation? Use a fresh TAC if MRA already consumed the previous one.'
    );
    if (!confirmed) return;

    setIsResettingTerminalActivation(true);
    try {
      await authFetch.fetch<any>(`/mra-eis/terminals/${terminal.id}/reset_activation/`, {
        method: 'POST',
      });

      if (activeBranchId) {
        persistTerminalCache(activeBranchId, null);
      }
      setTerminal(null);
      setLastSubmitReconciliation(null);
      terminalForm.setValue('tac_code', '');

      toast({
        title: 'Failed activation reset',
        description: 'The local terminal record was removed. Enter a TAC to activate again.',
      });
    } catch (error: any) {
      console.error('Reset terminal activation error:', error);
      toast({
        variant: 'destructive',
        title: 'Reset failed',
        description: 'Try again.',
      });
    } finally {
      setIsResettingTerminalActivation(false);
    }
  };

  const onSyncConfigurations = async () => {
    if (!business?.id) return;
    if (!terminal) {
      toast({
        variant: 'destructive',
        title: 'Activation required',
        description: 'Activate this device first.',
      });
      return;
    }

    setIsSyncingConfigurations(true);
    try {
      const params = new URLSearchParams({
        business_id: String(business.id),
        terminal_id: String(terminal.id),
      });
      const response = await authFetch.fetch<any>(`/mra-eis/configurations/sync_from_mra/?${params.toString()}`, {
        method: 'POST',
        body: JSON.stringify({
          config_types: [
            'global_configuration',
            'terminal_configuration',
            'taxpayer_configuration',
            'tax_rules',
            'receipt_format',
            'product_codes',
            'system_settings',
          ],
          sync_products: true,
        }),
      });

      const productSyncStatus = response?.product_sync?.synced
        ? ' Product catalog synced.'
        : '';
      toast({
        title: 'New MRA configs downloaded',
        description: `MRA config sync status: ${String(response?.status || 'success')}.${productSyncStatus}`,
      });

      await onRefreshTerminalStatus();
      await loadConfigurationStatus();
    } catch (error: any) {
      console.error('Sync configuration error:', error);
      toast({
        variant: 'destructive',
        title: 'Configuration sync failed',
        description: error?.message || 'Could not sync configurations from MRA.',
      });
    } finally {
      setIsSyncingConfigurations(false);
    }
  };

  const onDownloadSyncedConfigurations = () => {
    if (syncedConfigurations.length === 0) {
      toast({
        variant: 'destructive',
        title: 'No configurations',
        description: 'There are no synced MRA configurations to download.',
      });
      return;
    }

    const datePart = new Date().toISOString().slice(0, 10);
    const filename = `mra-eis-configurations-${sanitizeFilenamePart(String(business?.id || 'business'))}-${datePart}.json`;
    const downloadStarted = downloadTextFile(
      JSON.stringify(
        {
          exportedAt: new Date().toISOString(),
          businessId: business?.id || null,
          configurations: syncedConfigurations,
        },
        null,
        2
      ),
      filename,
      'application/json;charset=utf-8;'
    );

    if (!downloadStarted) {
      toast({
        variant: 'destructive',
        title: 'Download blocked',
        description: 'The browser blocked the configuration download.',
      });
      return;
    }

    toast({
      title: 'Configurations downloaded',
      description: `${syncedConfigurations.length} synced configuration record${syncedConfigurations.length === 1 ? '' : 's'} downloaded.`,
    });
  };

  const onExportInitialStockUpload = async () => {
    if (!activeBranchId) {
      toast({
        variant: 'destructive',
        title: 'Select a branch',
        description: 'Choose the branch whose opening stock should be exported for MRA.',
      });
      return;
    }

    const exportLimit = resolveInitialStockExportLimit(initialStockExportMode, initialStockCustomLimit);
    if (exportLimit === 0) {
      toast({
        variant: 'destructive',
        title: 'Enter product count',
        description: 'Use a custom product count greater than zero.',
      });
      return;
    }

    const updateProgress = async (stage: string, progress: number) => {
      setInitialStockExportStage(stage);
      setInitialStockExportProgress(Math.max(0, Math.min(100, Math.round(progress))));
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
    };

    setIsExportingInitialStock(true);
    setInitialStockExportProgress(0);
    setInitialStockExportStage('Preparing export...');
    try {
      await updateProgress('Reading selected branch inventory...', 10);
      const branchName = branches.find((branch) => branch.id === activeBranchId)?.name || activeBranchId;
      const inventoryItems = await getInventoryItemsForBranch(activeBranchId);
      await updateProgress('Filtering products...', 30);
      const exportableItems = inventoryItems
        .filter((item) => item._operation !== 'delete')
        .filter((item) => String(item.name || '').trim().length > 0)
        .sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
      const selectedItems = exportLimit === null ? exportableItems : exportableItems.slice(0, exportLimit);

      if (selectedItems.length === 0) {
        setInitialStockExportProgress(0);
        setInitialStockExportStage('');
        toast({
          variant: 'destructive',
          title: 'No products to export',
          description: 'No inventory products were found for the selected branch.',
        });
        return;
      }

      await updateProgress('Preparing MRA initial stock rows...', 55);
      const generatedBarcodeCount = selectedItems.filter((item) => !hasExplicitInitialStockBarcode(item)).length;
      const rows = selectedItems.map(mapItemToInitialStockRow);
      await updateProgress('Building Excel workbook...', 78);
      const workbook = buildInitialStockWorkbookBlob(rows);
      await updateProgress('Starting download...', 92);
      const datePart = new Date().toISOString().slice(0, 10);
      const limitLabel = exportLimit === null ? 'all' : `${selectedItems.length}`;
      const filename = `mra-initial-stock-${sanitizeFilenamePart(branchName)}-${limitLabel}-${datePart}.xlsx`;
      const downloadStarted = downloadBlobFile(workbook, filename);

      if (!downloadStarted) {
        throw new Error('The browser blocked the Excel download.');
      }

      await updateProgress('Excel file ready.', 100);
      toast({
        title: 'Initial stock Excel exported',
        description: `${selectedItems.length} of ${exportableItems.length} product${exportableItems.length === 1 ? '' : 's'} exported for ${branchName}.${generatedBarcodeCount ? ` ${generatedBarcodeCount} row${generatedBarcodeCount === 1 ? '' : 's'} used generated numeric barcode values.` : ''}`,
      });
      window.setTimeout(() => {
        setInitialStockExportProgress(0);
        setInitialStockExportStage('');
      }, 1500);
    } catch (error: any) {
      console.error('Initial stock export error:', error);
      setInitialStockExportProgress(0);
      setInitialStockExportStage('');
      toast({
        variant: 'destructive',
        title: 'Export failed',
        description: error?.message || 'Could not export the initial stock Excel file.',
      });
    } finally {
      setIsExportingInitialStock(false);
    }
  };

  const importInitialStockProductsToPos = async (
    products: any[],
    options: { markAsMraSynced?: boolean } = {}
  ) => {
    if (!terminal?.id || !terminalIsActive) {
      throw new Error('Activate this device first.');
    }

    if (!activeBranchId) {
      throw new Error('Select the branch whose initial stock should be imported.');
    }

    const response = await authFetch.fetch<any>(
      `/mra-eis/terminals/${terminal.id}/import_initial_inventory/`,
      {
        method: 'POST',
        body: JSON.stringify({
          TIN: String(tinValue || '').trim(),
          Products: products,
          markAsMraSynced: options.markAsMraSynced ?? false,
        }),
      }
    );

    const syncResult = await syncInventoryFromBackend(activeBranchId);
    return { response, syncResult };
  };

  const onImportInitialStockJson = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;

    setIsImportingInitialStock(true);
    try {
      const text = await file.text();
      const payload = JSON.parse(text);
      const products = extractInitialStockProductsFromJson(payload);

      if (products.length === 0) {
        throw new Error('The selected JSON file has no products.');
      }

      const { response, syncResult } = await importInitialStockProductsToPos(products, {
        markAsMraSynced: false,
      });

      toast({
        title: 'Initial stock imported',
        description: `${response?.created ?? 0} created, ${response?.updated ?? 0} updated. Sale-ready mappings: ${response?.mappings_sale_ready ?? 0}; pending MRA approval/sync: ${response?.mappings_pending ?? 0}. Local sync pulled ${syncResult.synced} inventory record${syncResult.synced === 1 ? '' : 's'}.`,
      });
    } catch (error: any) {
      console.error('Initial stock import error:', error);
      toast({
        variant: 'destructive',
        title: 'Import failed',
        description: error?.message || 'Could not import the initial stock JSON.',
      });
    } finally {
      setIsImportingInitialStock(false);
    }
  };

  const onPullApprovedProductsFromMra = async () => {
    if (!terminal?.id || !terminalIsActive) {
      toast({
        variant: 'destructive',
        title: 'Activation required',
        description: 'Activate this device first.',
      });
      return;
    }

    if (!activeBranchId) {
      toast({
        variant: 'destructive',
        title: 'Select a branch',
        description: 'Select a branch first.',
      });
      return;
    }

    setIsPullingApprovedProducts(true);
    try {
      const response = await authFetch.fetch<any>(
        `/mra-eis/terminals/${terminal.id}/pull_approved_products/`,
        {
          method: 'POST',
          body: JSON.stringify({ refreshFromMra: true }),
        }
      );
      const syncResult = await syncInventoryFromBackend(activeBranchId, {
        authoritativeMraStock: true,
        mraMappings: Array.isArray(response?.mra_mappings)
          ? response.mra_mappings
          : undefined,
      });

      toast({
        title: 'MRA products synced',
        description: `${response?.created ?? 0} created, ${response?.updated ?? 0} updated.`,
      });
      const syncDiagnostics = formatMraProductSyncDiagnostics({
        skippedProducts: response?.skipped_invalid_products ?? response?.skippedInvalidProducts,
        taxpayerIncompatible: response?.taxpayer_incompatible ?? response?.taxpayerIncompatible,
        notImportedCount: Math.max(
          0,
          Number(response?.product_count ?? 0) - Number(response?.imported_product_count ?? 0)
        ),
      });
      if (syncDiagnostics) {
        toast({
          variant: 'destructive',
          title: 'Some EIS products need attention',
          description: syncDiagnostics,
        });
      }
    } catch (error: any) {
      console.error('Pull approved MRA products error:', error);
      toast({
        variant: 'destructive',
        title: 'Product pull failed',
        description: error?.message || 'Could not pull approved products from MRA.',
      });
    } finally {
      setIsPullingApprovedProducts(false);
    }
  };

  const updateMraProductForm = (field: keyof MRAProductCreateForm, value: string) => {
    setMraProductForm((previous) => ({ ...previous, [field]: value }));
  };

  const loadMraProductCreationLookups = async () => {
    if (!terminal?.id || !terminalIsActive) {
      toast({
        variant: 'destructive',
        title: 'Activation required',
        description: 'Activate this device first.',
      });
      return;
    }

    setIsLoadingProductLookups(true);
    try {
      const [hsCodesResponse, unitsResponse] = await Promise.all([
        authFetch.fetch<any>(`/mra-eis/terminals/${terminal.id}/hs_codes/`),
        authFetch.fetch<any>(`/mra-eis/terminals/${terminal.id}/units_of_measure/`),
      ]);
      setMraHsCodes(extractMraResponseData(hsCodesResponse));
      setMraUnitsOfMeasure(extractMraResponseData(unitsResponse));
    } catch (error: any) {
      console.error('MRA product lookup load error:', error);
      toast({
        variant: 'destructive',
        title: 'Setup failed',
        description: 'Try again.',
      });
    } finally {
      setIsLoadingProductLookups(false);
    }
  };

  const openCreateMraProductModal = async () => {
    if (!terminal?.id || !terminalIsActive) {
      toast({
        variant: 'destructive',
        title: 'Activation required',
        description: 'Activate this device first.',
      });
      return;
    }
    setIsCreateProductModalOpen(true);
    if (mraHsCodes.length === 0 || mraUnitsOfMeasure.length === 0) {
      await loadMraProductCreationLookups();
    }
  };

  const onSubmitMraProduct = async () => {
    if (!terminal?.id || !terminalIsActive) {
      toast({
        variant: 'destructive',
        title: 'Activation required',
        description: 'Activate this device first.',
      });
      return;
    }

    const payload = {
      barcode: mraProductForm.barcode.trim() || null,
      hsCode: mraProductForm.hsCode.trim(),
      name: mraProductForm.name.trim(),
      description: mraProductForm.description.trim(),
      uom: mraProductForm.uom.trim(),
    };
    const missing = [
      !payload.hsCode && 'HS code',
      !payload.name && 'name',
      !payload.description && 'description',
      !payload.uom && 'unit of measure',
    ].filter(Boolean);
    if (missing.length > 0) {
      toast({
        variant: 'destructive',
        title: 'Complete product details',
        description: `Missing ${missing.join(', ')}.`,
      });
      return;
    }

    setIsSubmittingMraProduct(true);
    try {
      const response = await authFetch.fetch<any>(
        `/mra-eis/terminals/${terminal.id}/add_product/`,
        {
          method: 'POST',
          body: JSON.stringify(payload),
        }
      );
      const data = response?.data || response?.response?.data || {};
      const productCode = data?.barcode || data?.productId || payload.barcode || payload.name;
      toast({
        title: response?.dry_run ? 'MRA product prepared' : 'MRA product submitted',
        description: `${productCode} sent to MRA.`,
      });
      setMraProductForm({ barcode: '', hsCode: '', name: '', description: '', uom: '' });
      setIsCreateProductModalOpen(false);
    } catch (error: any) {
      console.error('MRA product creation error:', error);
      toast({
        variant: 'destructive',
        title: 'MRA product failed',
        description: 'Try again.',
      });
    } finally {
      setIsSubmittingMraProduct(false);
    }
  };

  const onSubmitInitialStockToMra = async () => {
    if (!terminal?.id || !terminalIsActive) {
      toast({
        variant: 'destructive',
        title: 'Activation required',
        description: 'Activate this device first.',
      });
      return;
    }

    if (!activeBranchId) {
      toast({
        variant: 'destructive',
        title: 'Select a branch',
        description: 'Choose the branch whose opening stock should be submitted to MRA.',
      });
      return;
    }

    const tin = String(tinValue || '').trim();
    if (!tin) {
      toast({
        variant: 'destructive',
        title: 'TIN required',
        description: 'Add and save the taxpayer TIN in EIS Setup before submitting.',
      });
      return;
    }

    const exportLimit = resolveInitialStockExportLimit(initialStockExportMode, initialStockCustomLimit);
    if (exportLimit === 0) {
      toast({
        variant: 'destructive',
        title: 'Enter product count',
        description: 'Use a custom product count greater than zero.',
      });
      return;
    }

    setIsPreparingInitialStockPreview(true);
    try {
      const inventoryItems = await getInventoryItemsForBranch(activeBranchId);
      const exportableItems = inventoryItems
        .filter((item) => item._operation !== 'delete')
        .filter((item) => String(item.name || '').trim().length > 0)
        .sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
      const selectedItems = exportLimit === null ? exportableItems : exportableItems.slice(0, exportLimit);

      if (selectedItems.length === 0) {
        toast({
          variant: 'destructive',
          title: 'No products to submit',
          description: 'No inventory products were found for the selected branch.',
        });
        return;
      }

      const rows = selectedItems.map(mapItemToInitialStockRow);
      const branchName = branches.find((branch) => branch.id === activeBranchId)?.name || activeBranchId;
      const generatedBarcodeCount = selectedItems.filter((item) => !hasExplicitInitialStockBarcode(item)).length;
      const mraMappedCount = selectedItems.filter((item) => {
        const mapping = (item as any).mra_mapping || (item as any).mraMapping;
        return Boolean(
          (item as any).is_mra_ready ||
            (item as any).isMraReady ||
            mapping?.mra_product_code ||
            mapping?.mraProductCode ||
            mapping?.mra_synced ||
            mapping?.mraSynced
        );
      }).length;

      setInitialStockSubmissionPreview({
        tin,
        branchName,
        totalAvailable: exportableItems.length,
        generatedBarcodeCount,
        mraMappedCount,
        isLastBatch: initialStockIsLastBatch,
        products: rows.map(mapInitialStockRowToApiProduct),
      });
      setIsInitialStockPreviewOpen(true);
    } catch (error: any) {
      console.error('Initial stock preview error:', error);
      toast({
        variant: 'destructive',
        title: 'Preview failed',
        description: error?.message || 'Could not prepare the initial stock preview.',
      });
    } finally {
      setIsPreparingInitialStockPreview(false);
    }
  };

  const onConfirmInitialStockSubmission = async () => {
    if (!terminal?.id || !terminalIsActive || !initialStockSubmissionPreview) {
      toast({
        variant: 'destructive',
        title: 'Review required',
        description: 'Review products first.',
      });
      return;
    }

    const payload = {
      TIN: initialStockSubmissionPreview.tin,
      IsLastBatch: initialStockSubmissionPreview.isLastBatch,
      Products: initialStockSubmissionPreview.products,
    };

    setIsSubmittingInitialStock(true);
    try {
      const response = await authFetch.fetch<any>(
        `/mra-eis/terminals/${terminal.id}/submit_initial_inventory/`,
        {
          method: 'POST',
          body: JSON.stringify(payload),
        }
      );

      const responseData = response?.data || response?.response?.data || {};
      const mappedItems = responseData?.mappedItems;
      const unmappedItems = responseData?.unmappedItems;
      const mappingSummary = mappedItems !== undefined || unmappedItems !== undefined
        ? ` Mapped: ${mappedItems ?? 0}, unmapped: ${unmappedItems ?? 0}.`
        : '';
      const batchCount = Number(response?.batch_count || response?.batchCount || 1);
      const batchSize = Number(response?.batch_size || response?.batchSize || 0);
      const batchSummary = batchCount > 1
        ? ` Submitted automatically in ${batchCount} batches${batchSize > 0 ? ` of up to ${batchSize}` : ''}.`
        : '';
      let localImportSummary = '';

      if (!response?.dry_run) {
        try {
          const { response: importResponse } = await importInitialStockProductsToPos(payload.Products, {
            markAsMraSynced: false,
          });
          localImportSummary = ` Local POS inventory imported: ${importResponse?.created ?? 0} created, ${importResponse?.updated ?? 0} updated; sale-ready mappings: ${importResponse?.mappings_sale_ready ?? 0}.`;
        } catch (importError) {
          console.warn('Initial stock submitted but local POS import failed:', importError);
          localImportSummary = ' MRA accepted the upload, but local POS import needs to be run manually.';
        }
      }

      toast({
        title: response?.dry_run ? 'Initial stock prepared' : 'Initial stock submitted',
        description: `${payload.Products.length} product${payload.Products.length === 1 ? '' : 's'} ${response?.dry_run ? 'prepared for MRA' : 'sent to MRA'}.${batchSummary}${payload.IsLastBatch ? ' Final batch marked on the last MRA batch only.' : ' More batches can follow.'}${mappingSummary}${localImportSummary}`,
      });
      setIsInitialStockPreviewOpen(false);
    } catch (error: any) {
      console.error('Initial stock MRA submission error:', error);
      toast({
        variant: 'destructive',
        title: 'Submission failed',
        description: error?.message || 'Could not submit initial stock to MRA.',
      });
    } finally {
      setIsSubmittingInitialStock(false);
    }
  };

  const toggleSection = (section: string) => {
    setExpandedSections(prev => ({
      ...prev,
      [section]: !prev[section]
    }));
  };


  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'active':
        return <Badge variant="default">Active</Badge>;
      case 'pending_activation':
        return <Badge variant="secondary">Pending</Badge>;
      case 'suspended':
        return <Badge variant="destructive">Suspended</Badge>;
      case 'deactivated':
        return <Badge variant="outline">Deactivated</Badge>;
      default:
        return <Badge variant="outline">{status}</Badge>;
    }
  };

  const initialStockPreviewRows = initialStockSubmissionPreview?.products.slice(0, 50) || [];

  const isPageLoading =
    isAuthLoading ||
    isLoadingEisSettings ||
    isLoadingBranches ||
    (activationUiEnabled && branches.length > 0 && (
      !activeBranchId ||
      !isTerminalDataReady ||
      isLoadingTerminal ||
      !isInitialConfigurationReady
    ));

  if (isPageLoading) {
    return (
      <div className="flex min-h-[360px] items-center justify-center">
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <Loader2 className="h-5 w-5 animate-spin" />
          Loading EIS settings...
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-3xl font-bold flex items-center gap-2">
          <Zap className="h-8 w-8 text-primary" />
          MRA EIS Integration
        </h1>
        <p className="text-muted-foreground mt-2">
          Configure your Malawi Revenue Authority Electronic Invoicing System compliance in one place.
        </p>
      </div>

      {activationRequired && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0" />
            <div className="space-y-1">
              <p className="font-semibold">Activation required</p>
              <p>Activate this device first.</p>
            </div>
          </div>
        </div>
      )}

      <Dialog
        open={isInitialStockPreviewOpen}
        onOpenChange={(open) => {
          if (isSubmittingInitialStock) return;
          setIsInitialStockPreviewOpen(open);
          if (!open) setInitialStockSubmissionPreview(null);
        }}
      >
        <DialogContent className="max-h-[88vh] overflow-y-auto sm:max-w-5xl">
          <DialogHeader>
            <DialogTitle>Review Initial Stock Submission</DialogTitle>
            <DialogDescription>
              Review products and quantities.
            </DialogDescription>
          </DialogHeader>

          {initialStockSubmissionPreview && (
            <div className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div className="rounded-lg border p-3">
                  <p className="text-xs text-muted-foreground">Branch</p>
                  <p className="font-medium">{initialStockSubmissionPreview.branchName}</p>
                </div>
                <div className="rounded-lg border p-3">
                  <p className="text-xs text-muted-foreground">TIN</p>
                  <p className="font-mono font-medium">{initialStockSubmissionPreview.tin}</p>
                </div>
                <div className="rounded-lg border p-3">
                  <p className="text-xs text-muted-foreground">Products selected</p>
                  <p className="font-medium">
                    {initialStockSubmissionPreview.products.length} of {initialStockSubmissionPreview.totalAvailable}
                  </p>
                </div>
                <div className="rounded-lg border p-3">
                  <p className="text-xs text-muted-foreground">Final batch</p>
                  <p className="font-medium">{initialStockSubmissionPreview.isLastBatch ? 'Yes' : 'No'}</p>
                </div>
              </div>

              <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
                <p className="font-medium">Important for certification</p>
                <p className="mt-1">
                  This uses the current POS inventory as opening stock. Products pulled from MRA can appear here, but the
                  submitted quantity is the POS quantity shown below. For supplier stock that should transfer through EIS,
                  use the purchase/B2B receiving flow instead of initial inventory.
                </p>
                {initialStockSubmissionPreview.isLastBatch && (
                  <p className="mt-2 font-medium">
                    This preview is marked as the final initial inventory batch. MRA treats initial inventory as a one-time opening stock upload.
                  </p>
                )}
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-lg border p-3 text-sm">
                  <p className="text-xs text-muted-foreground">Products with existing MRA mapping</p>
                  <p className="font-medium">{initialStockSubmissionPreview.mraMappedCount}</p>
                </div>
                <div className="rounded-lg border p-3 text-sm">
                  <p className="text-xs text-muted-foreground">Rows using generated barcodes</p>
                  <p className="font-medium">{initialStockSubmissionPreview.generatedBarcodeCount}</p>
                </div>
              </div>

              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full min-w-[860px] text-sm">
                  <thead className="bg-muted/60 text-left">
                    <tr>
                      <th className="px-3 py-2 font-medium">Barcode</th>
                      <th className="px-3 py-2 font-medium">Product</th>
                      <th className="px-3 py-2 text-right font-medium">Qty</th>
                      <th className="px-3 py-2 text-right font-medium">Unit Price</th>
                      <th className="px-3 py-2 text-right font-medium">Cost</th>
                      <th className="px-3 py-2 text-right font-medium">Selling</th>
                      <th className="px-3 py-2 text-right font-medium">Reorder</th>
                    </tr>
                  </thead>
                  <tbody>
                    {initialStockPreviewRows.map((product, index) => (
                      <tr key={`${product.BarCode}-${index}`} className="border-t">
                        <td className="px-3 py-2 font-mono text-xs">{product.BarCode}</td>
                        <td className="px-3 py-2">
                          <div className="font-medium">{product.ProductName}</div>
                          <div className="max-w-[320px] truncate text-xs text-muted-foreground">
                            {product.ProductDescription}
                          </div>
                        </td>
                        <td className="px-3 py-2 text-right">{formatFlexibleNumber(product.QuantityInStock)}</td>
                        <td className="px-3 py-2 text-right">{formatMoney(product.UnitPrice)}</td>
                        <td className="px-3 py-2 text-right">{formatMoney(product.CostPrice)}</td>
                        <td className="px-3 py-2 text-right">{formatMoney(product.SellingPrice)}</td>
                        <td className="px-3 py-2 text-right">{formatFlexibleNumber(product.ReorderLevel)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {initialStockSubmissionPreview.products.length > initialStockPreviewRows.length && (
                <p className="text-xs text-muted-foreground">
                  Showing first {initialStockPreviewRows.length} of {initialStockSubmissionPreview.products.length} products.
                </p>
              )}

              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                <Button
                  type="button"
                  variant="outline"
                  disabled={isSubmittingInitialStock}
                  onClick={() => {
                    setIsInitialStockPreviewOpen(false);
                    setInitialStockSubmissionPreview(null);
                  }}
                >
                  Cancel
                </Button>
                <Button type="button" disabled={isSubmittingInitialStock} onClick={onConfirmInitialStockSubmission}>
                  {isSubmittingInitialStock ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Submitting to MRA...
                    </>
                  ) : (
                    <>
                      <Upload className="mr-2 h-4 w-4" />
                      Confirm Submit to MRA
                    </>
                  )}
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={isCreateProductModalOpen}
        onOpenChange={(open) => {
          if (isSubmittingMraProduct) return;
          setIsCreateProductModalOpen(open);
        }}
      >
        <DialogContent className="max-h-[88vh] overflow-y-auto sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Create Product in MRA EIS</DialogTitle>
            <DialogDescription>
              Submit a product to MRA.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
              <p className="font-medium">Approval/use note</p>
              <p className="mt-1">
                Swagger does not expose a separate approval flag here. Treat the product as usable only after MRA returns it from the approved terminal-site products sync.
              </p>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-2">
                <label className="text-sm font-medium">Barcode</label>
                <Input
                  value={mraProductForm.barcode}
                  onChange={(event) => updateMraProductForm('barcode', event.target.value)}
                  placeholder="Optional, minimum 4 characters"
                  className="font-mono"
                />
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium">HS Code</label>
                <Select
                  value={mraProductForm.hsCode || undefined}
                  onValueChange={(value) => updateMraProductForm('hsCode', value)}
                  disabled={isLoadingProductLookups}
                >
                  <SelectTrigger>
                    <SelectValue placeholder={isLoadingProductLookups ? 'Loading HS codes...' : 'Select HS code'} />
                  </SelectTrigger>
                  <SelectContent>
                    {mraHsCodes.map((code) => {
                      const value = String(code.code || '').trim();
                      if (!value) return null;
                      return (
                        <SelectItem key={value} value={value}>
                          {value}{code.description ? ` - ${code.description}` : ''}{code.taxRateId ? ` (${code.taxRateId})` : ''}
                        </SelectItem>
                      );
                    })}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium">Name</label>
                <Input
                  value={mraProductForm.name}
                  onChange={(event) => updateMraProductForm('name', event.target.value)}
                  placeholder="Product name"
                />
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium">Unit of Measure</label>
                <Select
                  value={mraProductForm.uom || undefined}
                  onValueChange={(value) => updateMraProductForm('uom', value)}
                  disabled={isLoadingProductLookups}
                >
                  <SelectTrigger>
                    <SelectValue placeholder={isLoadingProductLookups ? 'Loading units...' : 'Select unit'} />
                  </SelectTrigger>
                  <SelectContent>
                    {mraUnitsOfMeasure.map((unit) => {
                      const value = String(unit.unitOfMeasure || '').trim();
                      if (!value) return null;
                      return (
                        <SelectItem key={value} value={value}>
                          {value}{unit.unitOfMeasureDescription ? ` - ${unit.unitOfMeasureDescription}` : ''}
                        </SelectItem>
                      );
                    })}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2 sm:col-span-2">
                <label className="text-sm font-medium">Description</label>
                <Input
                  value={mraProductForm.description}
                  onChange={(event) => updateMraProductForm('description', event.target.value)}
                  placeholder="Product description"
                />
              </div>
            </div>

            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <Button
                type="button"
                variant="outline"
                disabled={isSubmittingMraProduct}
                onClick={() => setIsCreateProductModalOpen(false)}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={isLoadingProductLookups || isSubmittingMraProduct}
                onClick={loadMraProductCreationLookups}
              >
                {isLoadingProductLookups ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
                Refresh Lookups
              </Button>
              <Button type="button" disabled={isSubmittingMraProduct || isLoadingProductLookups} onClick={onSubmitMraProduct}>
                {isSubmittingMraProduct ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Submitting...
                  </>
                ) : (
                  <>
                    <Upload className="mr-2 h-4 w-4" />
                    Submit to MRA
                  </>
                )}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Tabs defaultValue={activationRequired ? 'terminal' : 'setup'} className="space-y-4">
        <TabsList className={`grid h-auto w-full gap-2 ${activationUiEnabled ? 'grid-cols-2 sm:grid-cols-4' : 'grid-cols-1'}`}>
          <TabsTrigger value="setup" className="gap-2">
            <Settings className="h-4 w-4" />
            Setup
          </TabsTrigger>
          {activationUiEnabled && (
            <>
              <TabsTrigger value="terminal" className="gap-2">
                <Terminal className="h-4 w-4" />
                Activation
              </TabsTrigger>
              <TabsTrigger value="configuration" className="gap-2">
                <Settings className="h-4 w-4" />
                Configs
              </TabsTrigger>
              <TabsTrigger value="products" className="gap-2">
                <Package className="h-4 w-4" />
                Products
              </TabsTrigger>
            </>
          )}
        </TabsList>

        <TabsContent value="setup" className="space-y-4">
          {/* Section 1: EIS Setup */}
          <Card>
        <CardHeader 
          className="cursor-pointer hover:bg-muted/50"
          onClick={() => toggleSection('setup')}
        >
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Settings className="h-5 w-5 text-primary" />
              <div>
                <CardTitle>1. EIS Setup</CardTitle>
                <CardDescription>Enable and configure MRA EIS integration</CardDescription>
              </div>
            </div>
            {expandedSections.setup ? <ChevronUp /> : <ChevronDown />}
          </div>
        </CardHeader>

        {expandedSections.setup && (
          <CardContent>
            <FormProvider {...eisForm}>
              <form onSubmit={eisForm.handleSubmit(onEISSetupSubmit)} className="space-y-6">
                {/* Enable/Disable */}
                <div className="p-4 rounded-lg border border-border">
                  <FormField
                    control={eisForm.control}
                    name="enableEis"
                    render={({ field }) => {
                      const isEisEnabled = field.value || activationRequired;
                      
                      if (isEisEnabled) {
                        // Once enabled, show locked status instead of checkbox
                        return (
                          <FormItem>
                            <div className="p-4 rounded-lg border border-border flex items-start gap-3">
                              <CheckCircle2 className="h-6 w-6 text-foreground mt-0.5 flex-shrink-0" />
                              <div className="flex-1">
                                <p className="font-semibold text-sm">MRA EIS is Enabled & Locked</p>
                                <p className="text-xs text-muted-foreground mt-1">
                                  Your business is now enrolled in the MRA Electronic Invoicing System. This setting cannot be changed to ensure compliance with MRA requirements and maintain audit trail integrity.
                                </p>
                                <p className="text-xs text-muted-foreground mt-2 font-medium flex items-center gap-1">
                                  <CheckCircle2 className="h-3 w-3" />
                                  Automatic invoice submission to MRA is active
                                </p>
                              </div>
                            </div>
                          </FormItem>
                        );
                      }
                      
                      // Before enabled, show checkbox
                      return (
                        <FormItem className="flex items-center space-x-3">
                          <FormControl>
                            <input
                              type="checkbox"
                              checked={field.value}
                              onChange={field.onChange}
                              className="h-5 w-5 rounded border-input cursor-pointer"
                            />
                          </FormControl>
                          <div>
                            <FormLabel className="mb-0 font-semibold">Enable MRA EIS Integration</FormLabel>
                            <p className="text-sm text-muted-foreground mt-1">
                              Check to enable automatic invoice submission to MRA. This cannot be undone.
                            </p>
                          </div>
                        </FormItem>
                      );
                    }}
                  />
                </div>

                {/* Taxpayer Information - Only if enabled */}
                {activationUiEnabled && (
                  <div className="p-4 rounded-lg border border-border space-y-4">
                    <h3 className="font-semibold text-sm flex items-center gap-2">
                      <FileText className="h-4 w-4" />
                      Taxpayer Information
                    </h3>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <FormField
                        control={eisForm.control}
                        name="tin"
                        render={({ field }) => (
                          <FormItem>
                            <FormLabel className="text-sm">TIN (Taxpayer ID)</FormLabel>
                            <FormControl>
                              <Input
                                placeholder="e.g., 123456789"
                                disabled={terminalHasBeenActivated}
                                {...field}
                              />
                            </FormControl>
                            <FormMessage />
                          </FormItem>
                        )}
                      />
                      <FormField
                        control={eisForm.control}
                        name="eisEnvironment"
                        render={({ field }) => (
                          <FormItem>
                            <FormLabel className="text-sm">Environment</FormLabel>
                            <Select
                              value={field.value}
                              onValueChange={field.onChange}
                              disabled={terminalHasBeenActivated}
                            >
                              <FormControl>
                                <SelectTrigger className="text-sm">
                                  <SelectValue />
                                </SelectTrigger>
                              </FormControl>
                              <SelectContent>
                                <SelectItem value="TEST">Test/Sandbox</SelectItem>
                                <SelectItem value="PROD">Production</SelectItem>
                              </SelectContent>
                            </Select>
                            <FormMessage />
                          </FormItem>
                        )}
                      />
                    </div>
                  </div>
                )}

                {activationUiEnabled && (
                  <details className="rounded-lg border border-border bg-muted/20 p-4">
                    <summary className="cursor-pointer text-sm font-semibold">Advanced POS rules</summary>
                    <div className="mt-4 space-y-3">
                      <FormField
                        control={eisForm.control}
                        name="blockSalesIfEisDown"
                        render={({ field }) => (
                          <FormItem className="flex items-center space-x-2 p-3 rounded border border-border bg-background">
                            <FormControl>
                              <input
                                type="checkbox"
                                checked={field.value}
                                onChange={field.onChange}
                                className="h-4 w-4 rounded border-input"
                              />
                            </FormControl>
                            <div>
                              <FormLabel className="mb-0 text-sm font-medium">Block sales if EIS is unavailable</FormLabel>
                              <p className="text-xs text-muted-foreground">Prevents fiscal sales when MRA cannot be reached.</p>
                            </div>
                          </FormItem>
                        )}
                      />
                      <FormField
                        control={eisForm.control}
                        name="blockSalesIfTaxMappingMissing"
                        render={({ field }) => (
                          <FormItem className="flex items-center space-x-2 p-3 rounded border border-border bg-background">
                            <FormControl>
                              <input
                                type="checkbox"
                                checked={field.value}
                                onChange={field.onChange}
                                className="h-4 w-4 rounded border-input"
                              />
                            </FormControl>
                            <div>
                              <FormLabel className="mb-0 text-sm font-medium">Block products without MRA mapping</FormLabel>
                              <p className="text-xs text-muted-foreground">Requires approved MRA product mappings before sale.</p>
                            </div>
                          </FormItem>
                        )}
                      />
                    </div>
                  </details>
                )}

                {!activationUiEnabled && (
                  <div className="p-3 rounded border border-border text-sm text-muted-foreground">
                    Enable MRA EIS above to configure tax compliance settings.
                  </div>
                )}

                <Button type="submit" className="w-full">Save EIS Settings</Button>
              </form>
            </FormProvider>
          </CardContent>
        )}
          </Card>
        </TabsContent>

        {/* Section 2: Terminal Activation */}
        {activationUiEnabled && (
          <TabsContent value="terminal" className="space-y-4">
            <Card>
          <CardHeader 
            className="cursor-pointer hover:bg-muted/50"
            onClick={() => toggleSection('terminal')}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Terminal className="h-5 w-5 text-emerald-600 dark:text-emerald-400" />
                <div>
                  <CardTitle>2. Terminal Activation</CardTitle>
                  <CardDescription>Activate your POS terminal with MRA</CardDescription>
                </div>
              </div>
              {expandedSections.terminal ? <ChevronUp /> : <ChevronDown />}
            </div>
          </CardHeader>

          {expandedSections.terminal && (
            <CardContent className="space-y-6">
              {activeBranchId && (
                <div className="p-3 bg-muted/40 rounded-lg border border-border text-sm">
                  <span className="text-muted-foreground">Selected branch:</span>{' '}
                  <span className="font-medium">
                    {branches.find((branch) => branch.id === activeBranchId)?.name || activeBranchId}
                  </span>
                </div>
              )}

              {isLoadingTerminal && (
                <div className="p-4 rounded-lg border border-border flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Loading terminal status...
                </div>
              )}

              {/* Terminal Status */}
              {terminal ? (
                <div className={terminalStatusCardClassName}>
                  <div className="flex items-center justify-between">
                    <div>
                      <h3 className="font-semibold text-sm">
                        {terminalDeviceMismatch ? 'Current Device Status' : 'Terminal Status'}
                      </h3>
                      {terminalDeviceMismatch && (
                        <p className="text-xs text-muted-foreground mt-1">
                          Another device is already activated for this branch.
                        </p>
                      )}
                    </div>
                    {terminalDeviceMismatch ? (
                      <Badge variant="destructive">Not Activated</Badge>
                    ) : (
                      getStatusBadge(terminal.status)
                    )}
                  </div>
                  <div className="grid grid-cols-2 sm:grid-cols-6 gap-3 text-sm">
                    <div>
                      <p className="text-muted-foreground">{terminalDeviceMismatch ? 'Other Terminal ID' : 'Terminal ID'}</p>
                      <p className="font-mono text-xs">{terminal.terminal_id}</p>
                    </div>
                    <div>
                      <p className="text-muted-foreground">{terminalDeviceMismatch ? 'Other POS Name' : 'POS Name'}</p>
                      <p className="font-medium">{terminal.pos_name}</p>
                    </div>
                    <div>
                      <p className="text-muted-foreground">Online Status</p>
                      <p className="font-medium">{terminal.is_online ? '🟢 Online' : '🔴 Offline'}</p>
                      {terminal.health_check?.checked_at && (
                        <p className="text-xs text-muted-foreground">
                          MRA ping {formatTimestamp(terminal.health_check.checked_at)}
                        </p>
                      )}
                      {(terminal.health_check?.server_time || terminal.health_check?.server_time_raw) && (
                        <p className="text-xs text-muted-foreground">
                          Server time {formatMraServerTime(terminal.health_check.server_time || terminal.health_check.server_time_raw)}
                        </p>
                      )}
                    </div>
                    <div>
                      <p className="text-muted-foreground">Invoices</p>
                      <p className="font-medium">{terminal.online_invoice_counter + terminal.offline_invoice_counter}</p>
                    </div>
                    <div>
                      <p className="text-muted-foreground">Pending Offline</p>
                      <p className="font-medium">{terminal.pending_offline_invoices ?? 0}</p>
                    </div>
                    <div>
                      <p className="text-muted-foreground">Last Sync</p>
                      <p className="font-medium text-xs">{formatTimestamp(terminal.last_sync_at)}</p>
                    </div>
                  </div>

	                  {terminal.activated_at && (
	                    <p className="text-xs text-muted-foreground">
	                      Activated: {new Date(terminal.activated_at).toLocaleString()}
	                    </p>
	                  )}

	                  <div className="grid grid-cols-1 gap-2 rounded-md border bg-background/70 p-3 text-xs sm:grid-cols-2">
	                    <div>
	                      <p className="text-muted-foreground">Activated device serial</p>
	                      <p className="break-all font-mono">{terminal.device_serial || 'Not recorded'}</p>
	                    </div>
	                    <div>
	                      <p className="text-muted-foreground">This device serial</p>
	                      <p className="break-all font-mono">{currentDeviceSerial || 'Unavailable'}</p>
	                    </div>
	                  </div>

	                  {terminalDeviceMismatch && (
	                    <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
	                      <div className="flex items-start gap-2">
	                        <AlertTriangle className="h-4 w-4 mt-0.5 flex-shrink-0" />
	                        <div>
                          <p className="font-semibold">Activation required</p>
                          <p className="text-xs mt-1">Activate this device first.</p>
	                        </div>
	                      </div>
	                    </div>
	                  )}

	                  {terminal.activation_result?.dry_run && (
                    <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-900 dark:text-amber-200">
                      <div className="flex items-start gap-2">
                        <AlertTriangle className="h-4 w-4 mt-0.5 flex-shrink-0" />
                        <div>
                          <p className="font-semibold">Activation was prepared only</p>
                          <p className="text-xs mt-1">Not sent to MRA.</p>
                        </div>
                      </div>
                    </div>
                  )}

                  {terminal.activation_result?.error && !terminal.activation_result?.dry_run && (
                    <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                      <div className="flex items-start gap-2">
                        <AlertTriangle className="h-4 w-4 mt-0.5 flex-shrink-0" />
                        <div>
                          <p className="font-semibold">Last activation error</p>
                          <p className="text-xs mt-1">{terminal.activation_result.error}</p>
                        </div>
                      </div>
                    </div>
                  )}

                  {terminal && !terminalHasToken && !terminal.activation_result?.dry_run && (
                    <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-900 dark:text-amber-200">
                      <div className="flex items-start gap-2">
                        <AlertTriangle className="h-4 w-4 mt-0.5 flex-shrink-0" />
                        <div>
                          <p className="font-semibold">Terminal token missing</p>
                          <p className="text-xs mt-1">
                            Refresh token is unavailable until MRA activation returns a terminal JWT.
                          </p>
                        </div>
                      </div>
                    </div>
                  )}

                  {terminalIsBlocked && (
                    <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                      <div className="flex items-start gap-2">
                        <AlertTriangle className="h-4 w-4 mt-0.5 flex-shrink-0" />
                        <div className="min-w-0">
                          <p className="font-semibold">MRA terminal blocked</p>
                          <p className="text-xs mt-1 break-words">
                            {terminal.blocking_status?.blocking_reason || 'Check MRA block status to fetch the official blocking reason.'}
                          </p>
                          {(terminal.blocking_status?.blocked_at || terminal.blocking_status?.checked_at) && (
                            <p className="text-xs mt-1 opacity-80">
                              {terminal.blocking_status?.blocked_at
                                ? `Blocked ${formatTimestamp(terminal.blocking_status.blocked_at)}`
                                : `Checked ${formatTimestamp(terminal.blocking_status?.checked_at || undefined)}`}
                            </p>
                          )}
                        </div>
                      </div>
                    </div>
                  )}

                  {!terminalDeviceMismatch && (
                    <>
                  <div className="flex flex-wrap gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          void onRefreshTerminalStatus();
                        }}
                        disabled={isRefreshingTerminalStatus}
                      >
                        {isRefreshingTerminalStatus ? (
                          <>
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            Refreshing...
                          </>
                        ) : (
                          <>
                            <RefreshCw className="mr-2 h-4 w-4" />
                            Refresh Status
                          </>
                        )}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={onRefreshTerminalToken}
                        disabled={isRefreshingTerminalToken || !terminalHasToken}
                      >
                        {isRefreshingTerminalToken ? (
                          <>
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            Refreshing Token...
                          </>
                        ) : (
                          <>
                            <Clock className="mr-2 h-4 w-4" />
                            Refresh Token
                          </>
                        )}
                      </Button>
                      <Button
                        type="button"
                        variant={terminalIsBlocked ? 'destructive' : 'outline'}
                        size="sm"
                        onClick={onCheckTerminalBlockStatus}
                        disabled={isCheckingTerminalBlockStatus || !terminalHasToken}
                      >
                        {isCheckingTerminalBlockStatus ? (
                          <>
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            Checking Block...
                          </>
                        ) : (
                          <>
                            <AlertTriangle className="mr-2 h-4 w-4" />
                            Check Block Status
                          </>
                        )}
                      </Button>
                      {terminal.status !== 'active' && (
                        <Button
                          type="button"
                          variant="destructive"
                          size="sm"
                          onClick={onResetFailedActivation}
                          disabled={isResettingTerminalActivation}
                        >
                          {isResettingTerminalActivation ? (
                            <>
                              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                              Resetting...
                            </>
                          ) : (
                            <>
                              <Trash2 className="mr-2 h-4 w-4" />
                              Reset Failed Activation
                            </>
                          )}
                        </Button>
                      )}
                    </div>

                    <div className="rounded-lg border border-border bg-background/70 p-3 space-y-3">
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                      <div className="flex min-w-0 items-center gap-2">
                        <FileText className="h-4 w-4 text-muted-foreground" />
                        <div className="min-w-0">
                          <p className="font-semibold text-sm">MRA Last Submissions</p>
                          {lastSubmitReconciliation?.checked_at && (
                            <p className="text-xs text-muted-foreground">
                              Checked {formatTimestamp(lastSubmitReconciliation.checked_at)}
                            </p>
                          )}
                        </div>
                      </div>
                      <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
                        <Button asChild type="button" variant="outline" size="sm" className="w-full sm:w-auto">
                          <Link href="/dashboard/eis-sales">
                            <FileText className="mr-2 h-4 w-4" />
                            View Sales List
                          </Link>
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="w-full sm:w-auto"
                          onClick={onCheckLastSubmits}
                          disabled={isCheckingLastSubmits || terminal.status !== 'active'}
                        >
                        {isCheckingLastSubmits ? (
                          <>
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            Checking...
                          </>
                        ) : (
                          <>
                            <RefreshCw className="mr-2 h-4 w-4" />
                            Check Last Submits
                          </>
                        )}
                        </Button>
                      </div>
                    </div>

                    {lastSubmitReconciliation ? (
                      <div className="grid gap-3 md:grid-cols-2">
                        {(['online', 'offline'] as LastSubmitMode[]).map((mode) => {
                          const result = lastSubmitReconciliation.results?.[mode];
                          const reason = getLastSubmitReason(result);
                          return (
                            <div key={mode} className="rounded-md border border-border bg-muted/30 p-3 space-y-2 min-w-0">
                              <div className="flex items-center justify-between gap-2">
                                <p className="font-medium text-sm capitalize">{mode}</p>
                                <Badge variant={getLastSubmitBadgeVariant(result)}>
                                  {getLastSubmitStatusLabel(result)}
                                </Badge>
                              </div>
                              <div className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
                                <div className="min-w-0">
                                  <p className="text-muted-foreground">Invoice Number</p>
                                  <p className="font-mono break-all">{result?.invoiceNumber || 'N/A'}</p>
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
                              {reason && (
                                <p className="text-xs text-muted-foreground break-words">{reason}</p>
                              )}
                              {result?.validation_url && (
                                <a
                                  href={result.validation_url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="text-xs font-medium text-primary underline-offset-4 hover:underline break-all"
                                >
                                  Validation URL
                                </a>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <div className="grid gap-3 md:grid-cols-2">
                        {(['online', 'offline'] as LastSubmitMode[]).map((mode) => (
                          <div key={mode} className="rounded-md border border-dashed border-border bg-muted/20 p-3">
                            <div className="flex items-center justify-between gap-2">
                              <p className="font-medium text-sm capitalize">{mode}</p>
                              <Badge variant="outline">Not checked</Badge>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                    </div>
                    </>
                  )}
                </div>
              ) : (
                <div className="p-4 rounded-lg border border-amber-500/30 bg-amber-500/10 flex items-start gap-3">
                  <AlertTriangle className="h-5 w-5 text-amber-600 dark:text-amber-400 mt-0.5 flex-shrink-0" />
                  <div>
                    <p className="font-semibold text-sm text-amber-900 dark:text-amber-300">No terminal found</p>
                    <p className="text-sm text-amber-800 dark:text-amber-400 mt-1">
                      Activate this device first.
                    </p>
                  </div>
                </div>
              )}

              {showActivationForm && (
                <div className="rounded-lg border border-sky-500/30 bg-sky-500/10 p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <p className="text-sm font-semibold text-sky-950 dark:text-sky-200">
                        {terminal && !terminalDeviceMismatch ? 'Re-activation required' : 'Activation required'}
                      </p>
                      <p className="mt-1 text-sm text-sky-900 dark:text-sky-300">
                        Enter the TAC from MRA.
                      </p>
                    </div>
                    <Button
                      type="button"
                      className="w-full sm:w-auto"
                      onClick={() => {
                        terminalForm.setValue('pos_name', CONFIGURED_POS_NAME);
                        terminalForm.setValue('os_type', getDetectedOS());
                        terminalForm.setValue('device_serial', getDeviceSerial(), { shouldValidate: true });
                        terminalForm.setValue('mac_address', normalizeMacAddress(getDeviceMacAddress()), { shouldValidate: true });
                        setIsActivationModalOpen(true);
                      }}
                      disabled={branches.length === 0}
                    >
                      <Zap className="mr-2 h-4 w-4" />
                      Activate This Device
                    </Button>
                  </div>
                </div>
              )}

              <Dialog
                open={showActivationForm && isActivationModalOpen}
                onOpenChange={(open) => {
                  if (!isActivatingTerminal) {
                    setIsActivationModalOpen(open);
                  }
                }}
              >
                <DialogContent className="sm:max-w-2xl">
                  <DialogHeader>
                    <DialogTitle>
                      {terminal && !terminalDeviceMismatch ? 'Re-activate EIS Terminal' : 'Activate EIS Terminal'}
                    </DialogTitle>
                    <DialogDescription>
                      Use the TAC issued by MRA for this installed desktop device.
                    </DialogDescription>
                  </DialogHeader>

                  <FormProvider {...terminalForm}>
                    <form onSubmit={terminalForm.handleSubmit(onTerminalActivation)} className="space-y-4">
                      <FormField
                        control={terminalForm.control}
                        name="tac_code"
                        render={({ field }) => (
                          <FormItem>
                            <FormLabel>Terminal Activation Code (TAC)</FormLabel>
                            <div className="flex gap-2">
                              <FormControl>
                                <Input
                                  placeholder="Enter your TAC from MRA"
                                  type={showTacPassword ? 'text' : 'password'}
                                  {...field}
                                />
                              </FormControl>
                              <Button
                                type="button"
                                variant="outline"
                                size="icon"
                                onClick={() => setShowTacPassword(!showTacPassword)}
                              >
                                {showTacPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                              </Button>
                            </div>
                            <p className="text-xs text-muted-foreground mt-1">
                              Your unique activation code from Malawi Revenue Authority
                            </p>
                            <FormMessage />
                          </FormItem>
                        )}
                      />

                      <Button
                        type="submit"
                        className="w-full"
                        disabled={isActivatingTerminal || branches.length === 0 || !activeBranchId}
                      >
                        {isActivatingTerminal ? (
                          <>
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            Activating...
                          </>
                        ) : (
                          <>
                            <Zap className="mr-2 h-4 w-4" />
                            Activate Terminal
                          </>
                        )}
                      </Button>
                    </form>
                  </FormProvider>
                </DialogContent>
              </Dialog>
            </CardContent>
          )}
            </Card>
          </TabsContent>
        )}

        {/* Section 3: Configuration Management */}
        {activationUiEnabled && (
          <TabsContent value="configuration" className="space-y-4">
            <Card>
          <CardHeader 
            className="cursor-pointer hover:bg-muted/50"
            onClick={() => toggleSection('configuration')}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Settings className="h-5 w-5 text-violet-600 dark:text-violet-400" />
                <div>
                  <CardTitle>3. Configuration Management</CardTitle>
                  <CardDescription>MRA configurations and settings</CardDescription>
                </div>
              </div>
              {expandedSections.configuration ? <ChevronUp /> : <ChevronDown />}
            </div>
          </CardHeader>

          {expandedSections.configuration && (
            <CardContent className="space-y-4">
              <div className="p-4 rounded-lg border border-emerald-500/30 bg-emerald-500/10">
                <p className="text-sm text-emerald-900 dark:text-emerald-300">
                  <strong>ℹ️ Info:</strong> MRA configurations are automatically fetched and stored when your terminal is active.
                </p>
              </div>

              <div className="space-y-2">
                {REQUIRED_CONFIG_TYPES.map((type) => (
                  <div key={type} className="p-3 border rounded-lg flex items-center justify-between">
                    <div>
                      <p className="font-medium text-sm">{CONFIG_LABELS[type]}</p>
                      <p className="text-xs text-muted-foreground">
                        {configurationStatus[type].synced
                          ? `v${configurationStatus[type].version || '-'}`
                          : 'Not synced yet'}
                      </p>
                    </div>
                    <Badge variant={configurationStatus[type].synced ? 'default' : 'outline'}>
                      {configurationStatus[type].synced ? 'Synced' : 'Not Synced'}
                    </Badge>
                  </div>
                ))}
              </div>

              <details className="rounded-lg border border-border bg-muted/20 p-4">
                <summary className="cursor-pointer text-sm font-semibold">Advanced configuration data</summary>
                <div className="mt-4 space-y-3">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <p className="text-sm font-medium">Synced configurations</p>
                    <p className="text-xs text-muted-foreground">
                      {syncedConfigurations.length > 0
                        ? `${syncedConfigurations.length} active configuration record${syncedConfigurations.length === 1 ? '' : 's'} available`
                        : 'No active configuration records found'}
                    </p>
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="gap-2"
                    disabled={syncedConfigurations.length === 0}
                    onClick={onDownloadSyncedConfigurations}
                  >
                    <Download className="h-4 w-4" />
                    Download records
                  </Button>
                </div>

                {syncedConfigurations.length === 0 && !isLoadingConfigurationStatus ? (
                  <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                    No synced MRA configurations available.
                  </div>
                ) : (
                  syncedConfigurations.map((config) => {
                    const summary = summarizeConfiguration(config);
                    return (
                      <div key={config.id} className="rounded-lg border bg-background p-4">
                        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                          <div className="min-w-0">
                            <p className="text-sm font-semibold">{getConfigurationLabel(config.config_type)}</p>
                            <p className="break-all text-xs text-muted-foreground">{config.config_type}</p>
                          </div>
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge variant={config.is_active === false ? 'outline' : 'default'}>
                              {config.is_active === false ? 'Inactive' : 'Active'}
                            </Badge>
                            <Badge variant="outline">v{config.config_version || '-'}</Badge>
                          </div>
                        </div>

                        <div className="mt-3 grid grid-cols-1 gap-2 text-xs sm:grid-cols-3">
                          <div>
                            <span className="text-muted-foreground">Fetched: </span>
                            <span className="font-medium">{formatTimestamp(config.fetched_from_mra_at || config.created_at || undefined)}</span>
                          </div>
                          <div>
                            <span className="text-muted-foreground">Effective: </span>
                            <span className="font-medium">{formatTimestamp(config.effective_from || undefined)}</span>
                          </div>
                          <div>
                            <span className="text-muted-foreground">Expires: </span>
                            <span className="font-medium">{formatTimestamp(config.effective_to || undefined)}</span>
                          </div>
                        </div>

                        {summary.length > 0 && (
                          <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
                            {summary.map((item) => (
                              <div key={`${config.id}-${item.label}`} className="rounded-md bg-muted p-2">
                                <p className="text-xs text-muted-foreground">{item.label}</p>
                                <p className="break-words text-sm font-medium">{item.value}</p>
                              </div>
                            ))}
                          </div>
                        )}

                        {summary.length === 0 && (
                          <p className="mt-3 rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
                            Configuration downloaded.
                          </p>
                        )}
                      </div>
                    );
                  })
                )}
                </div>
              </details>

              {isLoadingConfigurationStatus && (
                <div className="p-3 rounded-lg border border-border text-sm text-muted-foreground inline-flex items-center gap-2">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Refreshing configuration status...
                </div>
              )}

              <Button
                variant="outline"
                className="w-full"
                disabled={!terminalIsActive || isSyncingConfigurations || isLoadingConfigurationStatus}
                onClick={onSyncConfigurations}
              >
                {isSyncingConfigurations ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Syncing Configurations...
                  </>
                ) : (
                  <>
                    <RefreshCw className="mr-2 h-4 w-4" />
                    Sync Configurations {!terminalIsActive && '(Requires Active Terminal)'}
                  </>
                )}
              </Button>
            </CardContent>
          )}
            </Card>
          </TabsContent>
        )}

        {/* Section 4: Product Mapping */}
        {activationUiEnabled && (
          <TabsContent value="products" className="space-y-4">
            <Card>
          <CardHeader 
            className="cursor-pointer hover:bg-muted/50"
            onClick={() => toggleSection('products')}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Package className="h-5 w-5 text-orange-600 dark:text-orange-400" />
                <div>
                  <CardTitle>4. Product Mapping</CardTitle>
                  <CardDescription>Map products to MRA codes</CardDescription>
                </div>
              </div>
              {expandedSections.products ? <ChevronUp /> : <ChevronDown />}
            </div>
          </CardHeader>

          {expandedSections.products && (
            <CardContent className="space-y-4">
              <div className="p-4 rounded-lg border border-violet-500/30 bg-violet-500/10">
                <p className="text-sm text-violet-900 dark:text-violet-300">
                  <strong>📦 Info:</strong> MRA portal approval is the source of truth. Local mappings mirror approved products for POS reporting and sale validation.
                </p>
              </div>

              <div className="p-4 rounded-lg border border-border bg-background space-y-3">
                <div>
                  <p className="font-medium text-sm">Approved MRA Products</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    Pull products and services already mapped and approved in the MRA EIS portal into POS inventory.
                  </p>
                </div>

                <Button
                  type="button"
                  className="w-full"
                  disabled={!activeBranchId || !terminalIsActive || isPullingApprovedProducts || isExportingInitialStock || isSubmittingInitialStock || isImportingInitialStock || isSubmittingMraProduct}
                  onClick={onPullApprovedProductsFromMra}
                >
                  {isPullingApprovedProducts ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Pulling from MRA...
                    </>
                  ) : (
                    <>
                      <RefreshCw className="mr-2 h-4 w-4" />
                      Pull Approved Products from MRA
                    </>
                  )}
                </Button>

                <Button
                  type="button"
                  variant="outline"
                  className="w-full"
                  disabled={!activeBranchId || !terminalIsActive || isPullingApprovedProducts || isExportingInitialStock || isSubmittingInitialStock || isImportingInitialStock || isSubmittingMraProduct}
                  onClick={openCreateMraProductModal}
                >
                  {isSubmittingMraProduct ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Submitting product...
                    </>
                  ) : (
                    <>
                      <Upload className="mr-2 h-4 w-4" />
                      Create Product in MRA
                    </>
                  )}
                </Button>
              </div>

              <details className="rounded-lg border border-border bg-muted/20 p-4">
                <summary className="cursor-pointer text-sm font-semibold">Advanced stock tools</summary>
                <div className="mt-4 space-y-3">
                <div>
                  <p className="font-medium text-sm">Initial Stock Upload</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    Export products for MRA upload, submit them, or import an already uploaded JSON back into POS inventory. API submission is split into safe MRA batches automatically.
                  </p>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div className="space-y-2">
                    <label className="text-sm font-medium">Products to include</label>
                    <Select
                      value={initialStockExportMode}
                      onValueChange={(value) => setInitialStockExportMode(value as InitialStockExportMode)}
                      disabled={isExportingInitialStock || isPreparingInitialStockPreview || isSubmittingInitialStock || isImportingInitialStock || isPullingApprovedProducts}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="all">All products</SelectItem>
                        <SelectItem value="first_20">First 20 products</SelectItem>
                        <SelectItem value="custom">Custom count</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>

                  {initialStockExportMode === 'custom' && (
                    <div className="space-y-2">
                      <label className="text-sm font-medium">Custom count</label>
                      <Input
                        type="number"
                        min="1"
                        step="1"
                        inputMode="numeric"
                        value={initialStockCustomLimit}
                        disabled={isExportingInitialStock || isPreparingInitialStockPreview || isSubmittingInitialStock || isImportingInitialStock || isPullingApprovedProducts}
                        onChange={(event) => setInitialStockCustomLimit(event.target.value)}
                      />
                    </div>
                  )}
                </div>

                <div className="p-3 rounded-lg border border-border space-y-3">
                  <label className="flex items-start gap-3 text-sm">
                    <input
                      type="checkbox"
                      checked={initialStockIsLastBatch}
                      disabled={isPreparingInitialStockPreview || isSubmittingInitialStock || isImportingInitialStock || isPullingApprovedProducts}
                      onChange={(event) => setInitialStockIsLastBatch(event.target.checked)}
                      className="mt-1 h-4 w-4 rounded border-input"
                    />
                    <span>
                      <span className="font-medium block">Mark this submission as the final batch</span>
                      <span className="text-xs text-muted-foreground">
                        Leave unchecked for partial uploads. If this is checked, only the last automatic MRA batch is marked final.
                      </span>
                    </span>
                  </label>
                </div>

                <input
                  ref={initialStockImportFileRef}
                  type="file"
                  accept=".json,application/json"
                  className="hidden"
                  onChange={onImportInitialStockJson}
                />

                <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    className="w-full"
                    disabled={!activeBranchId || isExportingInitialStock || isPreparingInitialStockPreview || isSubmittingInitialStock || isImportingInitialStock || isPullingApprovedProducts}
                    onClick={onExportInitialStockUpload}
                  >
                    {isExportingInitialStock ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        Exporting Excel...
                      </>
                    ) : (
                      <>
                        <Download className="mr-2 h-4 w-4" />
                        Export Excel Data
                      </>
                    )}
                  </Button>

                  <Button
                    type="button"
                    variant="outline"
                    className="w-full"
                    disabled={!activeBranchId || !terminalIsActive || isExportingInitialStock || isPreparingInitialStockPreview || isSubmittingInitialStock || isImportingInitialStock || isPullingApprovedProducts}
                    onClick={() => initialStockImportFileRef.current?.click()}
                  >
                    {isImportingInitialStock ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        Importing...
                      </>
                    ) : (
                      <>
                        <Upload className="mr-2 h-4 w-4" />
                        Import Uploaded JSON
                      </>
                    )}
                  </Button>
                </div>

                <Button
                  type="button"
                  className="w-full"
                  disabled={!activeBranchId || !terminalIsActive || isExportingInitialStock || isPreparingInitialStockPreview || isSubmittingInitialStock || isImportingInitialStock || isPullingApprovedProducts}
                  onClick={onSubmitInitialStockToMra}
                >
                  {isPreparingInitialStockPreview ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Preparing preview...
                    </>
                  ) : (
                    <>
                      <Eye className="mr-2 h-4 w-4" />
                      Review Selected Products {!terminalIsActive && '(Requires Active Terminal)'}
                    </>
                  )}
                </Button>

                {(isExportingInitialStock || initialStockExportProgress > 0) && (
                  <div className="space-y-2 rounded-lg border border-border p-3">
                    <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
                      <span>{initialStockExportStage || 'Preparing export...'}</span>
                      <span>{Math.round(initialStockExportProgress)}%</span>
                    </div>
                    <Progress value={initialStockExportProgress} />
                  </div>
                )}
                </div>
              </details>

              <div className="text-center py-8 text-muted-foreground">
                <Package className="h-12 w-12 mx-auto mb-3 opacity-50" />
                <p className="text-sm">Product mapping is managed from Inventory.</p>
              </div>

              <Button asChild variant="outline" className="w-full">
                <Link href="/dashboard/inventory?tab=mra">
                  <Package className="mr-2 h-4 w-4" />
                  Open MRA Product Mappings
                </Link>
              </Button>
            </CardContent>
          )}
            </Card>
          </TabsContent>
        )}
      </Tabs>

      {/* Info Box */}
      {!activationUiEnabled && (
        <Card className="border-sky-500/30 bg-sky-500/10">
          <CardContent className="pt-6">
            <div className="flex items-start gap-3">
              <AlertCircle className="h-5 w-5 text-sky-600 dark:text-sky-400 mt-0.5 flex-shrink-0" />
              <div>
                <p className="font-semibold text-sm text-sky-900 dark:text-sky-300">Get Started with MRA EIS</p>
                <p className="text-sm text-sky-800 dark:text-sky-400 mt-1">
                  Enable MRA EIS integration above to configure your business for tax compliance and automatic invoice submission to the Malawi Revenue Authority.
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
