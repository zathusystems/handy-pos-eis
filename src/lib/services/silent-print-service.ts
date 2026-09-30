'use client';

import {
  normalizePrinterPaperWidth,
  type PrinterPaperWidth,
} from './printer-service';

/**
 * Silent Print Service
 * Handles silent printing by sending directly to printer without dialog
 * Uses system-level printing when available
 */

export interface SilentPrintOptions {
  printerName?: string;
  printerId?: string;
  copies?: number;
  paperSize?: PrinterPaperWidth;
  printerPaperSize?: PrinterPaperWidth;
  timeout?: number;
}

class SilentPrintService {
  private lastError: string | null = null;

  getLastError(): string | null {
    return this.lastError;
  }

  private setLastError(error: unknown, fallback: string): string {
    const message = error instanceof Error
      ? error.message
      : typeof error === 'string'
        ? error
        : String(error || fallback);
    this.lastError = message || fallback;
    return this.lastError;
  }

  /**
   * Print silently using system print command (if available)
   * This works best with Tauri or Electron apps
   */
  async printSilentlyViaSystem(
    htmlContent: string,
    options: SilentPrintOptions = {}
  ): Promise<boolean> {
    try {
      this.lastError = null;
      const {
        printerName = 'default',
        printerId,
        copies = 1,
        paperSize = '80mm',
        timeout = 5000
      } = options;
      const resolvedPaperSize = normalizePrinterPaperWidth(paperSize);
      const resolvedPrinterPaperSize = resolvedPaperSize;

      console.log('[SilentPrint] Attempting system print to:', printerName);

      // Prefer native Tauri printing when available (more reliable than UA detection).
      const tauriInvoke = await this.getTauriInvoke();
      if (tauriInvoke) {
        const resolvedPrinterId = await this.resolvePrinterId(printerId, printerName, tauriInvoke);
        if (resolvedPrinterId) {
          const tauriResult = await this.printViaTauri(
            htmlContent,
            resolvedPrinterId,
            copies,
            resolvedPaperSize,
            resolvedPrinterPaperSize
          );

          if (!tauriResult && this.isWindowsEnvironment()) {
            console.warn('[SilentPrint] Tauri print failed on Windows, trying browser fallback.');
            return await this.printViaIframe(htmlContent, copies, resolvedPaperSize);
          }

          return tauriResult;
        }

        // No usable native printer ID: fallback to browser print path.
        this.setLastError(
          `Could not resolve printer "${printerName}" to a native printer ID.`,
          'Could not resolve printer.'
        );
        console.warn('[SilentPrint] Could not resolve a native printer ID, falling back to browser print.');
        return this.isWindowsEnvironment()
          ? await this.printViaIframe(htmlContent, copies, resolvedPaperSize)
          : await this.printViaAutoSubmit(htmlContent, copies, resolvedPaperSize);
      }

      // Check if running in Electron environment
      if (this.isElectronAvailable()) {
        return await this.printViaElectron(htmlContent, printerName, copies, resolvedPaperSize);
      }

      // Fallback: Use browser's print API with auto-submit
      return await this.printViaAutoSubmit(htmlContent, copies, resolvedPaperSize);
    } catch (error) {
      this.setLastError(error, 'System print failed.');
      console.error('[SilentPrint] Error in system print:', error);
      return false;
    }
  }

  /**
   * Check if Tauri is available
   */
  private isTauriAvailable(): boolean {
    try {
      const hasGlobalInvoke = typeof (window as any).__TAURI__?.invoke === 'function';
      const hasInternalInvoke = typeof (window as any).__TAURI_INTERNALS__?.invoke === 'function';
      const userAgent = navigator.userAgent.toLowerCase();
      const isTauriWebview = userAgent.includes('tauri') || userAgent.includes('wry');
      return hasGlobalInvoke || hasInternalInvoke || isTauriWebview;
    } catch {
      return false;
    }
  }

  /**
   * Check if Electron is available
   */
  private isElectronAvailable(): boolean {
    try {
      return typeof (window as any).electron !== 'undefined' || 
             typeof (window as any).require !== 'undefined';
    } catch {
      return false;
    }
  }

  private isWindowsEnvironment(): boolean {
    try {
      const userAgent = navigator.userAgent.toLowerCase();
      return userAgent.includes('windows');
    } catch {
      return false;
    }
  }

  /**
   * Print via Tauri (desktop app)
   */
  private async printViaTauri(
    htmlContent: string,
    printerId: string,
    copies: number,
    paperSize: PrinterPaperWidth,
    printerPaperSize: PrinterPaperWidth
  ): Promise<boolean> {
    try {
      const invoke = await this.getTauriInvoke();
      if (!invoke) {
        throw new Error('Tauri invoke API not available');
      }
      
      console.log('[SilentPrint] Using Tauri for silent printing');

      // Use Tauri command args with the default camelCase naming.
      const result = await invoke('print_receipt', {
        html: htmlContent,
        printerId,
        copies,
        paperSize,
        printerPaperWidth: printerPaperSize,
      });

      console.log('[SilentPrint] Tauri print result:', result);
      if (result === true) {
        return true;
      }

      if (typeof result === 'string') {
        const normalizedResult = result.trim().toLowerCase();
        if (
          normalizedResult === 'success' ||
          normalizedResult.startsWith('printed to ') ||
          normalizedResult.startsWith('printed.')
        ) {
          return true;
        }

        console.warn('[SilentPrint] Tauri print reported failure:', result);
        this.setLastError(result, 'Tauri print failed.');
      }

      if (!this.lastError) {
        this.setLastError('Tauri print failed.', 'Tauri print failed.');
      }
      return false;
    } catch (error) {
      this.setLastError(error, 'Tauri print failed.');
      console.error('[SilentPrint] Tauri print error:', error);
      return false;
    }
  }

  private isNativePrinterId(value?: string): boolean {
    if (typeof value !== 'string') {
      return false;
    }

    const normalized = value.trim();
    if (!normalized) {
      return false;
    }

    const lower = normalized.toLowerCase();
    const isWindows = this.isWindowsEnvironment();

    if (/^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$/.test(normalized)) {
      return !isWindows; // Raw USB VID:PID (not supported on Windows backend)
    }

    if (lower.startsWith('cups:')) {
      return !isWindows; // CUPS queues are non-Windows
    }

    if (lower.startsWith('bt:')) {
      return !isWindows; // Direct BT IDs are non-Windows
    }

    if (lower.startsWith('win:')) {
      return isWindows; // Windows spooler queue
    }

    return false;
  }

  private async getTauriInvoke(): Promise<((command: string, args?: Record<string, unknown>) => Promise<any>) | null> {
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      if (typeof invoke === 'function') {
        return invoke as (command: string, args?: Record<string, unknown>) => Promise<any>;
      }
    } catch {
      // Ignore and try global fallbacks.
    }

    try {
      const tauriInvoke = (window as any).__TAURI__?.invoke;
      if (typeof tauriInvoke === 'function') {
        return tauriInvoke.bind((window as any).__TAURI__);
      }

      const internalInvoke = (window as any).__TAURI_INTERNALS__?.invoke;
      if (typeof internalInvoke === 'function') {
        return internalInvoke.bind((window as any).__TAURI_INTERNALS__);
      }
    } catch {
      // Ignore global access errors.
    }

    return null;
  }

  private async resolvePrinterId(
    printerId?: string,
    printerName?: string,
    invokeOverride?: (command: string, args?: Record<string, unknown>) => Promise<any>
  ): Promise<string | null> {
    if (this.isNativePrinterId(printerId)) {
      return printerId!.trim();
    }

    if (this.isNativePrinterId(printerName)) {
      return printerName!.trim();
    }

    const invoke = invokeOverride ?? (await this.getTauriInvoke());
    if (!invoke) {
      return null;
    }

    try {
      const printers = await invoke('get_printers');
      if (!Array.isArray(printers)) {
        return null;
      }

      if (printerName) {
        const normalizedTargetName = printerName.trim().toLowerCase();
        const match = printers.find((printer: any) => {
          const name = String(printer?.name || '').trim().toLowerCase();
          const id = String(printer?.id || '').trim();
          return name === normalizedTargetName && this.isNativePrinterId(id);
        });

        if (match) {
          return String(match.id).trim();
        }
      }

      const defaultPrinter = printers.find((printer: any) => printer?.is_default || printer?.isDefault);
      if (defaultPrinter) {
        const id = String(defaultPrinter?.id || '').trim();
        if (this.isNativePrinterId(id)) {
          return id;
        }
      }

      const nativePrinterIds = printers
        .map((printer: any) => String(printer?.id || '').trim())
        .filter((id: string) => this.isNativePrinterId(id));
      if (nativePrinterIds.length === 1) {
        return nativePrinterIds[0];
      }

      return null;
    } catch (error) {
      this.setLastError(error, 'Failed to resolve printer.');
      console.error('[SilentPrint] Failed to resolve printer ID from discovery:', error);
      return null;
    }
  }

  /**
   * Print via Electron (desktop app)
   */
  private async printViaElectron(
    htmlContent: string,
    printerName: string,
    copies: number,
    paperSize: PrinterPaperWidth
  ): Promise<boolean> {
    try {
      const electron = (window as any).electron || (window as any).require('electron');
      
      console.log('[SilentPrint] Using Electron for silent printing');

      // Use Electron's print API
      const result = await electron.ipcRenderer.invoke('print-receipt', {
        html: htmlContent,
        printer: printerName,
        copies: copies,
        paperSize,
      });

      console.log('[SilentPrint] Electron print result:', result);
      return result === true || result === 'success';
    } catch (error) {
      console.error('[SilentPrint] Electron print error:', error);
      return false;
    }
  }

  /**
   * Print via browser with auto-submit (best effort)
   * This creates a print window and attempts to auto-submit
   */
  private async printViaAutoSubmit(
    htmlContent: string,
    copies: number,
    paperSize: PrinterPaperWidth
  ): Promise<boolean> {
    try {
      console.log('[SilentPrint] Using browser auto-submit method');

      const printWidth = normalizePrinterPaperWidth(paperSize);

      for (let i = 0; i < copies; i++) {
        const printWindow = window.open('', '', 'width=800,height=600');
        
        if (!printWindow) {
          console.warn('[SilentPrint] Could not open print window, falling back to iframe printing');
          return await this.printViaIframe(htmlContent, copies, paperSize);
        }

        // Write content to print window
        printWindow.document.write(`
          <!DOCTYPE html>
          <html>
            <head>
              <meta charset="UTF-8">
              <meta name="viewport" content="width=device-width, initial-scale=1.0">
              <style>
                * {
                  margin: 0;
                  padding: 0;
                  box-sizing: border-box;
                }
                body {
                  font-family: 'Courier New', monospace;
                  font-size: 12px;
                  width: ${printWidth};
                  margin: 0;
                  padding: 0;
                }
                @media print {
                  body {
                    margin: 0 !important;
                    padding: 0 !important;
                  }
                  @page {
                    size: ${printWidth} auto;
                    margin: 0;
                  }
                }
              </style>
            </head>
            <body>
              ${i > 0 ? `<div style="text-align: center; margin-bottom: 8px; padding-bottom: 8px; border-bottom: 1px dashed; font-weight: bold;">*** COPY #${i + 1} ***</div>` : ''}
              ${htmlContent}
              <script>
                // Auto-print after content loads
                window.addEventListener('load', function() {
                  setTimeout(function() {
                    window.print();
                    // Close after print dialog appears
                    setTimeout(function() {
                      window.close();
                    }, 500);
                  }, 100);
                });
              </script>
            </body>
          </html>
        `);
        printWindow.document.close();

        // Wait between copies
        if (i < copies - 1) {
          await new Promise(resolve => setTimeout(resolve, 2000));
        }
      }

      console.log('[SilentPrint] Auto-submit print completed');
      return true;
    } catch (error) {
      console.error('[SilentPrint] Auto-submit print error:', error);
      return false;
    }
  }

  private async printViaIframe(
    htmlContent: string,
    copies: number,
    paperSize: PrinterPaperWidth
  ): Promise<boolean> {
    try {
      console.log('[SilentPrint] Using iframe print fallback');

      const printWidth = normalizePrinterPaperWidth(paperSize);

      for (let i = 0; i < copies; i++) {
        const iframe = document.createElement('iframe');
        iframe.style.display = 'none';
        iframe.style.visibility = 'hidden';
        iframe.style.position = 'absolute';
        iframe.style.left = '-9999px';
        iframe.style.width = printWidth;
        iframe.style.height = 'auto';
        document.body.appendChild(iframe);

        const iframeDoc = iframe.contentDocument || iframe.contentWindow?.document;
        if (!iframeDoc) {
          throw new Error('Could not access iframe document');
        }

        iframeDoc.write(`
          <!DOCTYPE html>
          <html>
            <head>
              <meta charset="UTF-8">
              <meta name="viewport" content="width=device-width, initial-scale=1.0">
              <style>
                * {
                  margin: 0;
                  padding: 0;
                  box-sizing: border-box;
                }
                body {
                  font-family: 'Courier New', monospace;
                  font-size: 12px;
                  width: ${printWidth};
                  margin: 0;
                  padding: 0;
                }
                @media print {
                  body {
                    margin: 0 !important;
                    padding: 0 !important;
                  }
                  @page {
                    size: ${printWidth} auto;
                    margin: 0;
                  }
                }
              </style>
            </head>
            <body>
              ${i > 0 ? `<div style="text-align: center; margin-bottom: 8px; padding-bottom: 8px; border-bottom: 1px dashed; font-weight: bold;">*** COPY #${i + 1} ***</div>` : ''}
              ${htmlContent}
            </body>
          </html>
        `);
        iframeDoc.close();

        await new Promise(resolve => setTimeout(resolve, 300));

        iframe.contentWindow?.print();

        await new Promise(resolve => setTimeout(resolve, 1000));

        try {
          document.body.removeChild(iframe);
        } catch (error) {
          console.warn('[SilentPrint] Failed to remove print iframe:', error);
        }
      }

      return true;
    } catch (error) {
      console.error('[SilentPrint] Iframe print error:', error);
      return false;
    }
  }

  /**
   * Enable auto-print mode (stores preference)
   */
  enableAutoPrint(): void {
    localStorage.setItem('handy-pos-auto-print-enabled', 'true');
    console.log('[SilentPrint] Auto-print mode enabled');
  }

  /**
   * Disable auto-print mode
   */
  disableAutoPrint(): void {
    localStorage.removeItem('handy-pos-auto-print-enabled');
    console.log('[SilentPrint] Auto-print mode disabled');
  }

  /**
   * Check if auto-print is enabled
   */
  isAutoPrintEnabled(): boolean {
    return localStorage.getItem('handy-pos-auto-print-enabled') === 'true';
  }

  /**
   * Get available print methods
   */
  getAvailableMethods(): string[] {
    const methods: string[] = [];

    if (this.isTauriAvailable()) {
      methods.push('tauri');
    }

    if (this.isElectronAvailable()) {
      methods.push('electron');
    }

    methods.push('browser');

    return methods;
  }
}

export const silentPrintService = new SilentPrintService();
