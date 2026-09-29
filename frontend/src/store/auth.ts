import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { User } from '../api/types';

interface AuthState {
  token: string | null;
  user: User | null;
  notice: string | null;
  login: (token: string, user: User) => void;
  logout: (notice?: string) => void;
  can: (permission: string) => boolean;
}

export const useAuth = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      user: null,
      notice: null,
      login: (token, user) => set({ token, user, notice: null }),
      logout: (notice) => set({ token: null, user: null, notice: notice ?? null }),
      can: (permission) => !!get().user?.permissions.includes(permission),
    }),
    {
      name: 'maniac-auth',
      // sessionStorage: the token dies with the tab, limiting exposure if the device is shared
      storage: {
        getItem: (k) => {
          const v = sessionStorage.getItem(k);
          return v ? JSON.parse(v) : null;
        },
        setItem: (k, v) => sessionStorage.setItem(k, JSON.stringify(v)),
        removeItem: (k) => sessionStorage.removeItem(k),
      },
      partialize: (s) => ({ token: s.token, user: s.user }) as AuthState,
    },
  ),
);
