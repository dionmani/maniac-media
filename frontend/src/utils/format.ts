export function bytes(n?: number | null): string {
  if (!n) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function speed(bps?: number | null): string {
  return bps ? `${bytes(bps)}/s` : '—';
}

export function duration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ${seconds % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function relative(iso?: string | null): string {
  if (!iso) return '—';
  const date = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z');
  const diff = (Date.now() - date.getTime()) / 1000;
  const abs = Math.abs(diff);
  const fmt = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  if (abs < 60) return fmt.format(-Math.round(diff), 'second');
  if (abs < 3600) return fmt.format(-Math.round(diff / 60), 'minute');
  if (abs < 86400) return fmt.format(-Math.round(diff / 3600), 'hour');
  if (abs < 86400 * 30) return fmt.format(-Math.round(diff / 86400), 'day');
  return date.toLocaleDateString();
}

export function dateTime(iso?: string | null): string {
  if (!iso) return '—';
  const date = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z');
  return date.toLocaleString();
}

export const pad = (n: number) => String(n).padStart(2, '0');
export const episodeCode = (s: number, e: number) => `S${pad(s)}E${pad(e)}`;

export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
