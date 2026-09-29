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
  'media.added': [['movies'], ['series'], ['dashboard']],
  'media.updated': [['movies'], ['movie'], ['series']],
  'media.deleted': [['movies'], ['series'], ['dashboard']],
  'rename.completed': [['rename'], ['movie'], ['series']],
  'scan.completed': [['movies'], ['series'], ['dashboard'], ['issues']],
  'job.updated': [['jobs'], ['tasks']],
  'health.issue': [['health']],
  'database.migration': [['database']],
};

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
    const pending = new Set<string>();
    let flush: ReturnType<typeof setTimeout> | null = null;

    const invalidate = (keys: string[][]) => {
      keys.forEach((k) => pending.add(JSON.stringify(k)));
      if (flush) return;
      // coalesce bursts (e.g. hundreds of job updates during a scan)
      flush = setTimeout(() => {
        pending.forEach((k) => qc.invalidateQueries({ queryKey: JSON.parse(k) }));
        pending.clear();
        flush = null;
      }, 400);
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
      ws?.close();
    };
  }, [token, qc, setConnected]);
}
