'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Barcode, Camera, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';

type DetectorResult = {
  rawValue?: string | null;
};

type DetectorInstance = {
  detect: (source: ImageBitmapSource) => Promise<DetectorResult[]>;
};

type DetectorConstructor = {
  new (options?: { formats?: string[] }): DetectorInstance;
  getSupportedFormats?: () => Promise<string[]>;
};

export type BarcodeDetectionOutcome = {
  accepted: boolean;
  productName?: string;
  message?: string;
};

type ScanHistoryEntry = {
  barcode: string;
  productName: string;
  count: number;
  lastScannedAt: string;
};

interface CameraBarcodeScannerModalProps {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  onBarcodeDetected: (barcode: string) => Promise<BarcodeDetectionOutcome | boolean> | BarcodeDetectionOutcome | boolean;
}

const PREFERRED_FORMATS = [
  'code_128',
  'ean_13',
  'ean_8',
  'upc_a',
  'upc_e',
  'qr_code',
];
const SAME_BARCODE_COOLDOWN_MS = 1200;

function normalizeDetectionOutcome(result: BarcodeDetectionOutcome | boolean): BarcodeDetectionOutcome {
  if (typeof result === 'boolean') {
    return { accepted: result };
  }
  return {
    accepted: Boolean(result.accepted),
    productName: result.productName,
    message: result.message,
  };
}

function getCameraErrorMessage(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === 'NotAllowedError') {
      return 'Camera permission was denied. Allow camera access and try again.';
    }
    if (error.name === 'NotFoundError') {
      return 'No camera was found on this device.';
    }
    if (error.name === 'NotReadableError') {
      return 'Camera is already in use by another app.';
    }
    if (error.name === 'OverconstrainedError') {
      return 'Unable to start the camera with the selected settings.';
    }
  }

  return error instanceof Error
    ? error.message
    : 'Unable to start camera scanner. Please try again.';
}

export function CameraBarcodeScannerModal({
  isOpen,
  onOpenChange,
  onBarcodeDetected,
}: CameraBarcodeScannerModalProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const detectorRef = useRef<DetectorInstance | null>(null);
  const scanFrameRef = useRef<number | null>(null);
  const scanLockRef = useRef(false);
  const handlingBarcodeRef = useRef(false);
  const lastScanTsRef = useRef(0);
  const lastAcceptedBarcodeRef = useRef('');
  const lastAcceptedAtRef = useRef(0);

  const [manualBarcode, setManualBarcode] = useState('');
  const [lastDetectedBarcode, setLastDetectedBarcode] = useState('');
  const [scanHistory, setScanHistory] = useState<ScanHistoryEntry[]>([]);
  const [status, setStatus] = useState<'starting' | 'scanning' | 'fallback' | 'error'>('starting');
  const [errorMessage, setErrorMessage] = useState('');
  const [torchAvailable, setTorchAvailable] = useState(false);
  const [torchEnabled, setTorchEnabled] = useState(false);
  const [isTorchUpdating, setIsTorchUpdating] = useState(false);

  const stopScanner = useCallback(() => {
    if (scanFrameRef.current !== null) {
      cancelAnimationFrame(scanFrameRef.current);
      scanFrameRef.current = null;
    }

    detectorRef.current = null;
    scanLockRef.current = false;
    lastScanTsRef.current = 0;
    lastAcceptedBarcodeRef.current = '';
    lastAcceptedAtRef.current = 0;

    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }

    if (videoRef.current) {
      videoRef.current.srcObject = null;
    }

    setTorchAvailable(false);
    setTorchEnabled(false);
    setIsTorchUpdating(false);
  }, []);

  const triggerVibrationFeedback = useCallback(() => {
    if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
      navigator.vibrate([40, 20, 60]);
    }
  }, []);

  const addToScanHistory = useCallback((barcode: string, productName?: string) => {
    const trimmedBarcode = barcode.trim();
    if (!trimmedBarcode) {
      return;
    }

    const nowIso = new Date().toISOString();
    const fallbackName = `Barcode ${trimmedBarcode}`;

    setScanHistory((previous) => {
      const existingIndex = previous.findIndex((entry) => entry.barcode === trimmedBarcode);
      if (existingIndex >= 0) {
        const next = [...previous];
        const existing = next[existingIndex];
        next[existingIndex] = {
          ...existing,
          productName: productName?.trim() || existing.productName || fallbackName,
          count: existing.count + 1,
          lastScannedAt: nowIso,
        };
        return next.sort((a, b) => b.lastScannedAt.localeCompare(a.lastScannedAt));
      }

      return [
        {
          barcode: trimmedBarcode,
          productName: productName?.trim() || fallbackName,
          count: 1,
          lastScannedAt: nowIso,
        },
        ...previous,
      ].slice(0, 20);
    });
  }, []);

  const applyTorchConstraint = useCallback(async (enabled: boolean): Promise<boolean> => {
    if (!streamRef.current) {
      return false;
    }

    const [videoTrack] = streamRef.current.getVideoTracks();
    if (!videoTrack || typeof videoTrack.applyConstraints !== 'function') {
      return false;
    }

    try {
      await videoTrack.applyConstraints({
        advanced: [{ torch: enabled } as MediaTrackConstraintSet],
      });
      setTorchEnabled(enabled);
      return true;
    } catch {
      try {
        await videoTrack.applyConstraints({ torch: enabled } as MediaTrackConstraints);
        setTorchEnabled(enabled);
        return true;
      } catch (error) {
        console.warn('[Camera Scanner] Failed to update torch state:', error);
        return false;
      }
    }
  }, []);

  const createDetector = useCallback(async (): Promise<DetectorInstance | null> => {
    const detectorCtor = (window as Window & { BarcodeDetector?: DetectorConstructor }).BarcodeDetector;
    if (!detectorCtor) {
      return null;
    }

    try {
      const getFormats = detectorCtor.getSupportedFormats;
      if (typeof getFormats !== 'function') {
        return new detectorCtor();
      }

      const supportedFormats = await getFormats();
      const selectedFormats = PREFERRED_FORMATS.filter((format) => supportedFormats.includes(format));
      if (selectedFormats.length > 0) {
        return new detectorCtor({ formats: selectedFormats });
      }

      return new detectorCtor();
    } catch (error) {
      console.warn('[Camera Scanner] Failed to initialize BarcodeDetector:', error);
      return null;
    }
  }, []);

  const handleBarcode = useCallback(async (barcode: string): Promise<BarcodeDetectionOutcome> => {
    const trimmed = barcode.trim();
    if (!trimmed || handlingBarcodeRef.current) {
      return { accepted: false };
    }

    const now = Date.now();
    if (
      trimmed === lastAcceptedBarcodeRef.current
      && now - lastAcceptedAtRef.current < SAME_BARCODE_COOLDOWN_MS
    ) {
      return { accepted: false };
    }

    handlingBarcodeRef.current = true;
    setLastDetectedBarcode(trimmed);
    try {
      const result = await Promise.resolve(onBarcodeDetected(trimmed));
      const outcome = normalizeDetectionOutcome(result);

      if (outcome.accepted) {
        lastAcceptedBarcodeRef.current = trimmed;
        lastAcceptedAtRef.current = now;
        addToScanHistory(trimmed, outcome.productName);
        triggerVibrationFeedback();
        setErrorMessage('');
      } else if (outcome.message) {
        setErrorMessage(outcome.message);
      }

      return outcome;
    } finally {
      handlingBarcodeRef.current = false;
    }
  }, [addToScanHistory, onBarcodeDetected, triggerVibrationFeedback]);

  useEffect(() => {
    if (!isOpen) {
      setManualBarcode('');
      setLastDetectedBarcode('');
      setScanHistory([]);
      setErrorMessage('');
      stopScanner();
      return;
    }

    let active = true;
    setStatus('starting');
    setErrorMessage('');

    const scanLoop = async () => {
      if (!active || !detectorRef.current || !videoRef.current) {
        return;
      }

      if (scanLockRef.current) {
        scanFrameRef.current = requestAnimationFrame(() => void scanLoop());
        return;
      }

      const video = videoRef.current;
      if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
        scanFrameRef.current = requestAnimationFrame(() => void scanLoop());
        return;
      }

      const now = performance.now();
      if (now - lastScanTsRef.current < 120) {
        scanFrameRef.current = requestAnimationFrame(() => void scanLoop());
        return;
      }

      lastScanTsRef.current = now;
      scanLockRef.current = true;

      try {
        const results = await detectorRef.current.detect(video as unknown as ImageBitmapSource);
        const detectedValue = results.find((entry) => typeof entry.rawValue === 'string' && entry.rawValue.trim())?.rawValue;

        if (detectedValue) {
          await handleBarcode(detectedValue);
        }
      } catch (error) {

      } finally {
        scanLockRef.current = false;
      }

      if (active) {
        scanFrameRef.current = requestAnimationFrame(() => void scanLoop());
      }
    };

    const startScanner = async () => {
      if (!navigator.mediaDevices?.getUserMedia) {
        setStatus('error');
        setErrorMessage('Camera access is not supported on this device.');
        return;
      }

      const detector = await createDetector();
      if (!active) return;

      detectorRef.current = detector;

      if (!detector) {
        setStatus('fallback');
        setErrorMessage('Automatic camera decoding is unavailable. Enter barcode manually.');
      }

      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: {
            facingMode: { ideal: 'environment' },
            width: { ideal: 1280 },
            height: { ideal: 720 },
          },
          audio: false,
        });

        if (!active) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }

        streamRef.current = stream;

        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          videoRef.current.setAttribute('playsinline', 'true');
          await videoRef.current.play().catch(() => undefined);
        }

        const [videoTrack] = stream.getVideoTracks();
        if (videoTrack && typeof videoTrack.getCapabilities === 'function') {
          const capabilities = videoTrack.getCapabilities() as MediaTrackCapabilities & { torch?: boolean };
          setTorchAvailable(Boolean(capabilities?.torch));
        } else {
          setTorchAvailable(false);
        }

        if (detector) {
          setStatus('scanning');
          scanFrameRef.current = requestAnimationFrame(() => void scanLoop());
        }
      } catch (error) {
        setStatus('error');
        setErrorMessage(getCameraErrorMessage(error));
      }
    };

    void startScanner();

    return () => {
      active = false;
      stopScanner();
    };
  }, [createDetector, handleBarcode, isOpen, stopScanner]);

  const submitManualBarcode = useCallback(async () => {
    const barcode = manualBarcode.trim();
    if (!barcode) return;

    await handleBarcode(barcode);
    setManualBarcode('');
  }, [handleBarcode, manualBarcode]);

  const handleTorchToggle = useCallback(async () => {
    if (!torchAvailable || isTorchUpdating) {
      return;
    }

    setIsTorchUpdating(true);
    const success = await applyTorchConstraint(!torchEnabled);
    if (!success) {
      setErrorMessage('Unable to toggle flashlight on this camera.');
    } else if (errorMessage === 'Unable to toggle flashlight on this camera.') {
      setErrorMessage('');
    }
    setIsTorchUpdating(false);
  }, [applyTorchConstraint, errorMessage, isTorchUpdating, torchAvailable, torchEnabled]);

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
          <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Camera className="h-5 w-5" />
            Scan Barcode
          </DialogTitle>
          <DialogDescription>
            Point the camera at a barcode to add products continuously. The scanner stays open for multiple scans.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="relative overflow-hidden rounded-lg border bg-black">
            <video
              ref={videoRef}
              autoPlay
              muted
              playsInline
              className="h-64 w-full object-cover"
            />
            {status === 'starting' && (
              <div className="absolute inset-0 flex items-center justify-center bg-black/60 text-white">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Starting camera...
              </div>
            )}
          </div>

          {torchAvailable && (
            <Button
              type="button"
              variant="outline"
              onClick={() => void handleTorchToggle()}
              disabled={isTorchUpdating}
            >
              {isTorchUpdating
                ? 'Updating Flashlight...'
                : torchEnabled
                  ? 'Turn Flashlight Off'
                  : 'Turn Flashlight On'}
            </Button>
          )}

          {status === 'scanning' && (
            <p className="text-sm text-muted-foreground">
              Keep barcode inside camera view until it is detected. Move to the next item after each vibration.
            </p>
          )}

          {errorMessage && (
            <p className={`text-sm ${status === 'error' ? 'text-destructive' : 'text-muted-foreground'}`}>
              {errorMessage}
            </p>
          )}

          <div className="space-y-2">
            <label className="text-sm font-medium">Manual Barcode Entry</label>
            <div className="flex items-center gap-2">
              <Input
                value={manualBarcode}
                onChange={(event) => setManualBarcode(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    void submitManualBarcode();
                  }
                }}
                placeholder="Type barcode and press Enter"
                className="font-mono"
              />
              <Button
                type="button"
                variant="outline"
                onClick={() => void submitManualBarcode()}
                disabled={!manualBarcode.trim()}
              >
                <Barcode className="mr-2 h-4 w-4" />
                Add
              </Button>
            </div>
          </div>

          {lastDetectedBarcode && (
            <div className="rounded-md border bg-muted/40 p-2">
              <p className="text-xs text-muted-foreground">Last detected barcode</p>
              <p className="font-mono text-sm">{lastDetectedBarcode}</p>
            </div>
          )}

          <div className="rounded-md border bg-muted/30 p-3">
            <p className="text-xs font-medium text-muted-foreground">
              Scanned Products ({scanHistory.reduce((total, entry) => total + entry.count, 0)})
            </p>
            {scanHistory.length === 0 ? (
              <p className="mt-2 text-sm text-muted-foreground">
                No products scanned yet.
              </p>
            ) : (
              <div className="mt-2 max-h-40 space-y-2 overflow-y-auto pr-1">
                {scanHistory.map((entry) => (
                  <div key={entry.barcode} className="flex items-start justify-between gap-3 rounded border bg-background p-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{entry.productName}</p>
                      <p className="truncate font-mono text-xs text-muted-foreground">{entry.barcode}</p>
                    </div>
                    <span className="rounded bg-primary/10 px-2 py-0.5 text-xs font-semibold text-primary">
                      x{entry.count}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
