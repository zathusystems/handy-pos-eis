'use client';

import { Download, Loader2, Share2 } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';

type ReceiptPdfExportDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  canShare: boolean;
  isBusy: boolean;
  onDownload: () => void;
  onShare: () => void;
};

export function ReceiptPdfExportDialog({
  open,
  onOpenChange,
  canShare,
  isBusy,
  onDownload,
  onShare,
}: ReceiptPdfExportDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-1rem)] max-w-sm">
        <DialogHeader>
          <DialogTitle>Export receipt PDF</DialogTitle>
          <DialogDescription>Choose where to send the receipt PDF.</DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2 sm:space-x-0">
          {canShare && (
            <Button
              type="button"
              variant="outline"
              className="w-full sm:w-auto"
              onClick={onShare}
              disabled={isBusy}
            >
              {isBusy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Share2 className="mr-2 h-4 w-4" />}
              Share
            </Button>
          )}
          <Button
            type="button"
            className="w-full sm:w-auto"
            onClick={onDownload}
            disabled={isBusy}
          >
            {isBusy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Download className="mr-2 h-4 w-4" />}
            Download
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
