'use client';

import type { PrinterPaperWidth } from '@/lib/services/printer-service';

type ThermalReceiptPdfOptions = {
  elementId: string;
  paperWidth: PrinterPaperWidth;
  filename: string;
};

type ThermalReceiptPdfResult = {
  filename: string;
  location: 'download' | 'tauri';
  path?: string;
};

const toSafeFilename = (value: string): string => {
  const normalized = String(value || 'thermal-receipt')
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');

  return (normalized || 'thermal-receipt').toLowerCase().endsWith('.pdf')
    ? (normalized || 'thermal-receipt')
    : `${normalized || 'thermal-receipt'}.pdf`;
};

const isTauriRuntime = (): boolean => {
  try {
    return Boolean(
      (window as any).__TAURI__ ||
      (window as any).__TAURI_INTERNALS__ ||
      navigator.userAgent.toLowerCase().includes('tauri') ||
      navigator.userAgent.toLowerCase().includes('wry')
    );
  } catch {
    return false;
  }
};

const downloadBytes = (bytes: Uint8Array, filename: string): void => {
  const blob = new Blob([new Uint8Array(bytes).buffer], { type: 'application/pdf' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
};

const saveBytesInTauri = async (
  bytes: Uint8Array,
  filename: string
): Promise<string | null> => {
  if (!isTauriRuntime()) {
    return null;
  }

  try {
    const { invoke } = await import('@tauri-apps/api/core');
    if (typeof invoke !== 'function') {
      return null;
    }

    const savedPath = await invoke<string>('save_receipt_pdf', {
      filename,
      content: Array.from(bytes),
    });
    return String(savedPath || '').trim() || null;
  } catch (error) {
    console.warn('[Receipt PDF] Native Tauri save unavailable, using download fallback:', error);
    return null;
  }
};

/**
 * Export the existing rendered thermal receipt. Capturing the rendered DOM
 * keeps PDF output aligned with the receipt sent to thermal printers.
 */
export async function exportThermalReceiptPdf(
  options: ThermalReceiptPdfOptions
): Promise<ThermalReceiptPdfResult> {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    throw new Error('Receipt PDF export is only available in the application window.');
  }

  const source = document.getElementById(options.elementId);
  if (!source) {
    throw new Error('Receipt preview is not ready. Try again.');
  }

  const widthMm = options.paperWidth === '58mm' ? 58 : 80;
  const widthPx = Math.round((widthMm / 25.4) * 96);
  const clone = source.cloneNode(true) as HTMLElement;
  const filename = toSafeFilename(options.filename);

  clone.style.display = 'block';
  clone.style.visibility = 'visible';
  clone.style.position = 'fixed';
  clone.style.left = '-100000px';
  clone.style.top = '0';
  clone.style.width = `${widthPx}px`;
  clone.style.maxWidth = 'none';
  clone.style.height = 'auto';
  clone.style.margin = '0';
  clone.style.backgroundColor = '#fff';
  clone.setAttribute('aria-hidden', 'true');
  document.body.appendChild(clone);

  try {
    if (document.fonts?.ready) {
      await document.fonts.ready;
    }
    await new Promise<void>((resolve) => {
      window.requestAnimationFrame(() => resolve());
    });

    const { default: html2canvas } = await import('html2canvas');
    const canvas = await html2canvas(clone, {
      backgroundColor: '#fff',
      scale: 2,
      useCORS: true,
      logging: false,
      width: widthPx,
      windowWidth: widthPx,
    });

    const heightMm = Math.max(20, (canvas.height / canvas.width) * widthMm);
    const { default: JsPDF } = await import('jspdf');
    const pdf = new JsPDF({
      orientation: 'portrait',
      unit: 'mm',
      format: [widthMm, heightMm],
      compress: true,
    });

    pdf.addImage(
      canvas.toDataURL('image/png'),
      'PNG',
      0,
      0,
      widthMm,
      heightMm,
      undefined,
      'FAST'
    );

    const bytes = new Uint8Array(pdf.output('arraybuffer') as ArrayBuffer);
    const nativePath = await saveBytesInTauri(bytes, filename);
    if (nativePath) {
      return { filename, location: 'tauri', path: nativePath };
    }

    downloadBytes(bytes, filename);
    return { filename, location: 'download' };
  } finally {
    clone.remove();
  }
}
