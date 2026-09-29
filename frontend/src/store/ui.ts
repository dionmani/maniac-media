import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type Mode = 'movie' | 'tv';

export interface Toast {
  id: number;
  message: string;
  severity: 'success' | 'info' | 'warning' | 'error';
}

interface UiState {
  mode: Mode;
  setMode: (mode: Mode) => void;
  libraryView: 'posters' | 'table';
  setLibraryView: (v: 'posters' | 'table') => void;
  connected: boolean;
  setConnected: (c: boolean) => void;
  toasts: Toast[];
  toast: (message: string, severity?: Toast['severity']) => void;
  dismiss: (id: number) => void;
}

let seq = 0;

export const useUi = create<UiState>()(
  persist(
    (set) => ({
      mode: 'movie',
      setMode: (mode) => set({ mode }),
      libraryView: 'posters',
      setLibraryView: (libraryView) => set({ libraryView }),
      connected: false,
      setConnected: (connected) => set({ connected }),
      toasts: [],
      toast: (message, severity = 'info') => {
        const id = ++seq;
        set((s) => ({ toasts: [...s.toasts.slice(-3), { id, message, severity }] }));
        setTimeout(() => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), 5000);
      },
      dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
    }),
    { name: 'maniac-ui', partialize: (s) => ({ mode: s.mode, libraryView: s.libraryView }) as UiState },
  ),
);

export const toast = (message: string, severity?: Toast['severity']) => useUi.getState().toast(message, severity);
