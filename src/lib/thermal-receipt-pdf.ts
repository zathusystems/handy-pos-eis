'use client';

import type { PrinterPaperWidth } from '@/lib/services/printer-service';

type ThermalReceiptPdfOptions = {
  elementId: string;
  paperWidth: PrinterPaperWidth;
  filename: string;
};

export type ThermalReceiptPdfDocument = {
  filename: string;
  bytes: Uint8Array;
};

export type ThermalReceiptPdfResult = {
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

const isAndroidRuntime = (): boolean => {
  try {
    return /android/i.test(navigator.userAgent);
  } catch {
    return false;
  }
};

const createPdfBlob = (bytes: Uint8Array): Blob => (
  new Blob([new Uint8Array(bytes).buffer], { type: 'application/pdf' })
);

const downloadBytes = (bytes: Uint8Array, filename: string): void => {
  const url = URL.createObjectURL(createPdfBlob(bytes));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = 'noopener';
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
};

const invokeTauri = async <T>(command: string, args: Record<string, unknown>): Promise<T> => {
  const { invoke } = await import('@tauri-apps/api/core');
  if (typeof invoke !== 'function') {
    throw new Error('The native export service is unavailable.');
  }
  return invoke<T>(command, args);
};

export const canShareThermalReceiptPdf = (): boolean => {
  try {
    return Boolean(
      (isTauriRuntime() && isAndroidRuntime()) ||
      typeof navigator.share === 'function'
    );
  } catch {
    return false;
  }
};

export async function downloadThermalReceiptPdf(
  document: ThermalReceiptPdfDocument
): Promise<ThermalReceiptPdfResult> {
  if (isTauriRuntime()) {
    const savedPath = await invokeTauri<string>('save_receipt_pdf', {
      filename: document.filename,
      content: Array.from(document.bytes),
    });
    const path = String(savedPath || '').trim();
    if (!path) {
      throw new Error('The PDF was not saved.');
    }
    return { filename: document.filename, location: 'tauri', path };
  }

  downloadBytes(document.bytes, document.filename);
  return { filename: document.filename, location: 'download' };
}

export async function shareThermalReceiptPdf(
  document: ThermalReceiptPdfDocument
): Promise<void> {
  if (isTauriRuntime() && isAndroidRuntime()) {
    await invokeTauri<void>('share_receipt_pdf', {
      filename: document.filename,
      content: Array.from(document.bytes),
    });
    return;
  }

  if (typeof navigator.share !== 'function') {
    throw new Error('Sharing is not supported on this device.');
  }

  const file = new File([new Uint8Array(document.bytes)], document.filename, {
    type: 'application/pdf',
  });
  if (typeof navigator.canShare === 'function' && !navigator.canShare({ files: [file] })) {
    throw new Error('This device cannot share PDF files.');
  }

  await navigator.share({
    files: [file],
    title: 'HandyPOS receipt',
  });
}

/** Generate a PDF from the rendered thermal receipt without choosing a destination. */
export async function generateThermalReceiptPdf(
  options: ThermalReceiptPdfOptions
): Promise<ThermalReceiptPdfDocument> {
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

    return {
      filename,
      bytes: new Uint8Array(pdf.output('arraybuffer') as ArrayBuffer),
    };
  } finally {
    clone.remove();
  }
}

/** Backwards-compatible one-click export used by non-action-menu callers. */
export async function exportThermalReceiptPdf(
  options: ThermalReceiptPdfOptions
): Promise<ThermalReceiptPdfResult> {
  const document = await generateThermalReceiptPdf(options);
  return downloadThermalReceiptPdf(document);
}
