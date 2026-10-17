import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import type { LiveEvent } from '../api/types';
import { useAuth } from '../store/auth';
import { toast, useUi } from '../store/ui';

/** Which cached queries each server event invalidates. */
const INVALIDATES: Record<string, string[][]> = {
  'queue.changed': [['queue'], ['dashboard']],
  'release.grabbed': [['queue'], ['history']],
  'download.completed': [['queue'], ['dashboard']],
  'download.failed': [['queue'], ['dashboard'], ['history']],
  'import.completed': [['queue'], ['movies'], ['movie'], ['series'], ['dashboard'], ['history'], ['wanted']],
  'import.failed': [['queue'], ['dashboard'], ['history']],
  'media.added': [['movies'], ['series'], ['dashboard'], ['discover']],
  'media.updated': [['movies'], ['movie'], ['series']],
  'media.deleted': [['movies'], ['series'], ['dashboard'], ['discover']],
  'rename.completed': [['rename'], ['movie'], ['series']],
  'scan.completed': [['movies'], ['series'], ['dashboard'], ['issues']],
  'library.issues_changed': [['issues'], ['dashboard']],
  'media.inspected': [['movie'], ['series'], ['media-file'], ['movies']],
  'job.updated': [['jobs'], ['tasks']],
  'health.issue': [['health']],
  'database.migration': [['database']],
};

/** Query families that are costly to refetch; their live invalidations are throttled harder. */
const HEAVY = new Set(['movies', 'series', 'dashboard']);

export function useLiveEvents() {
  const token = useAuth((s) => s.token);
  const qc = useQueryClient();
  const setConnected = useUi((s) => s.setConnected);

  useEffect(() => {
    if (!token) return;
    let ws: WebSocket | null = null;
    let retry = 1000;
    let stopped = false;
    let ping: ReturnType<typeof setInterval>;
    // Coalesce bursts (e.g. hundreds of job updates during a scan). The whole-library lists and the
    // dashboard are expensive to reload (the library page fetches every page), so during a scan or
    // metadata refresh they refresh at most every few seconds instead of on every event.
    const makeBatch = (delay: number) => {
      const pending = new Set<string>();
      let timer: ReturnType<typeof setTimeout> | null = null;
      return {
        add(key: string) {
          pending.add(key);
          if (timer) return;
          timer = setTimeout(() => {
            pending.forEach((k) => qc.invalidateQueries({ queryKey: JSON.parse(k) }));
            pending.clear();
            timer = null;
          }, delay);
        },
        cancel() {
          if (timer) clearTimeout(timer);
        },
      };
    };
    const fast = makeBatch(400);
    const slow = makeBatch(3000);

    const invalidate = (keys: string[][]) => {
      keys.forEach((k) => (HEAVY.has(k[0]) ? slow : fast).add(JSON.stringify(k)));
    };

    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/api/v1/ws?token=${encodeURIComponent(token)}`);
      ws.onopen = () => {
        retry = 1000;
        setConnected(true);
        ping = setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send('ping'), 25000);
      };
      ws.onmessage = (msg) => {
        const event: LiveEvent = JSON.parse(msg.data);
        const keys = INVALIDATES[event.type];
        if (keys) invalidate(keys);
        const p = event.payload ?? {};
        if (event.type === 'import.completed') toast(`Imported: ${p.title}`, 'success');
        if (event.type === 'import.failed') toast(`Import failed: ${p.reason}`, 'error');
        if (event.type === 'download.failed') toast(`Download failed: ${p.reason}`, 'error');
        if (event.type === 'release.grabbed') toast(`Grabbed ${p.title}`, 'info');
        if (event.type === 'health.issue') toast(String(p.message), 'warning');
      };
      ws.onclose = (e) => {
        setConnected(false);
        clearInterval(ping);
        if (e.code === 4401) {
          useAuth.getState().logout('Your session expired. Please sign in again.');
          return;
        }
        if (!stopped) setTimeout(connect, (retry = Math.min(retry * 2, 30000)));
      };
    };
    connect();
    return () => {
      stopped = true;
      clearInterval(ping);
      fast.cancel();
      slow.cancel();
      ws?.close();
    };
  }, [token, qc, setConnected]);
}
