'use client';

import { useEffect } from 'react';
import { isTauriApp } from '@/lib/tauri-init';

const FONTS_TIMEOUT_MS = 2000;
const EXTRA_SETTLE_MS = 150;

function waitForWindowLoad(): Promise<void> {
  if (document.readyState === 'complete') {
    return Promise.resolve();
  }

  return new Promise((resolve) => {
    window.addEventListener('load', () => resolve(), { once: true });
  });
}

function waitForPaintFrames(frameCount: number = 2): Promise<void> {
  return new Promise((resolve) => {
    const nextFrame = (remaining: number) => {
      if (remaining <= 0) {
        resolve();
        return;
      }

      requestAnimationFrame(() => nextFrame(remaining - 1));
    };

    nextFrame(frameCount);
  });
}

async function waitForFontsWithTimeout(timeoutMs: number): Promise<void> {
  if (!('fonts' in document)) {
    return;
  }

  const fontsReady = (document as Document & { fonts?: { ready?: Promise<unknown> } }).fonts?.ready;
  if (!fontsReady) {
    return;
  }

  await Promise.race([
    fontsReady.then(() => undefined).catch(() => undefined),
    new Promise<void>((resolve) => setTimeout(resolve, timeoutMs)),
  ]);
}

export function TauriReadySignal() {
  useEffect(() => {
    let cancelled = false;

    const emitReady = async () => {
      const tauriApp = isTauriApp();
      if (!tauriApp) {
        return;
      }

      const isAndroid = /android/i.test(navigator.userAgent || '');
      if (isAndroid) {
        document.documentElement.setAttribute('data-tauri-android', 'true');
      } else {
        document.documentElement.removeAttribute('data-tauri-android');
      }

      const path = window.location.pathname.toLowerCase();
      if (path === '/splash' || path === '/splash/') {
        // Only the main window should close the splash.
        return;
      }

      await waitForWindowLoad();
      await waitForPaintFrames(2);
      await waitForFontsWithTimeout(FONTS_TIMEOUT_MS);
      await new Promise((resolve) => setTimeout(resolve, EXTRA_SETTLE_MS));

      if (cancelled) {
        return;
      }

      document.documentElement.setAttribute('data-tauri-app-ready', 'true');

      try {
        const { emit } = await import('@tauri-apps/api/event');
        await emit('frontend-ready', { timestamp: Date.now() });

      } catch (error) {
        console.warn('[Tauri Splash] Failed to emit frontend-ready:', error);
      }
    };

    void emitReady();

    return () => {
      cancelled = true;
    };
  }, []);

  return null;
}
