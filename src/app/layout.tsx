import type { Metadata, Viewport } from 'next';
import './globals.css';
import { Toaster } from '@/components/ui/toaster';
import { AuthProvider } from '@/hooks/use-auth';
import { ThemeProvider } from '@/contexts/theme-context';
import { TauriReadySignal } from '@/components/tauri-ready-signal';
import { AndroidModalBackGuard } from '@/components/android-modal-back-guard';

export const metadata: Metadata = {
  title: 'HandyPOS - POS and Inventory System',
  description: 'Modern POS and Inventory Management System for HandyPOS with offline-first capabilities.',
  manifest: '/manifest.json',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  viewportFit: 'cover',
  interactiveWidget: 'resizes-content',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <meta name="theme-color" content="#0066FF" />
        <style
          dangerouslySetInnerHTML={{
            __html: `
              html[data-tauri-android='true']:not([data-tauri-app-ready='true']) body {
                opacity: 0 !important;
                background: #1f4fd7 !important;
              }
            `,
          }}
        />
        <script
          dangerouslySetInnerHTML={{
            __html: `
              (function () {
                var ua = navigator.userAgent || '';
                var isAndroid = /android/i.test(ua);
                var isTauri = /tauri|wry/i.test(ua) || !!window.__TAURI__ || !!window.__TAURI_INTERNALS__;
                if (!isAndroid || !isTauri) return;
                var root = document.documentElement;
                root.setAttribute('data-tauri-android', 'true');
                setTimeout(function () {
                  if (!root.hasAttribute('data-tauri-app-ready')) {
                    root.setAttribute('data-tauri-app-ready', 'true');
                  }
                }, 5000);
              })();
            `,
          }}
        />
      </head>
      <body className="antialiased" suppressHydrationWarning>
        <ThemeProvider>
          <AuthProvider>
            {children}
            <Toaster />
            <TauriReadySignal />
            <AndroidModalBackGuard />
          </AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
