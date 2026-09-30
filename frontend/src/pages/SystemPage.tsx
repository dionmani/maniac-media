import {
  Alert,
  Box,
  Chip,
  Grid,
  IconButton,
  LinearProgress,
  MenuItem,
  Pagination,
  Stack,
  Switch,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Ban, CheckCircle2, Play, ShieldCheck, TriangleAlert, XCircle } from 'lucide-react';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { get, post, put } from '../api/client';
import type { Job, Page, PluginInfo } from '../api/types';
import { ErrorAlert, Loading, PageHeader, Section, StatusChip } from '../components/common';
import { useAuth } from '../store/auth';
import { toast } from '../store/ui';
import { palette } from '../theme';
import { dateTime, duration, errorMessage, relative } from '../utils/format';

export default function SystemPage() {
  const can = useAuth((s) => s.can);
  const [params, setParams] = useSearchParams();
  const tabs = [
    ['status', 'Status & health', true], ['tasks', 'Tasks', true], ['jobs', 'Jobs', true],
    ['audit', 'Audit log', can('audit:read')], ['logs', 'Logs', can('system:admin')], ['plugins', 'Plugins', true],
  ].filter((t) => t[2]) as [string, string, boolean][];
  const tab = params.get('tab') ?? 'status';
  return (
    <>
      <PageHeader title="System" icon={<ShieldCheck color={palette.purple} />} subtitle="Health checks, background work, audit trail and diagnostics" />
      <Tabs value={tab} onChange={(_, v) => setParams({ tab: v })} variant="scrollable" sx={{ mb: 3 }}>
        {tabs.map(([k, l]) => <Tab key={k} value={k} label={l} />)}
      </Tabs>
      {tab === 'status' && <StatusTab />}
      {tab === 'tasks' && <TasksTab />}
      {tab === 'jobs' && <JobsTab />}
      {tab === 'audit' && <AuditTab />}
      {tab === 'logs' && <LogsTab />}
      {tab === 'plugins' && <PluginsTab />}
    </>
  );
}

interface Check { source: string; status: 'ok' | 'warning' | 'error'; message: string }

function StatusTab() {
  const status = useQuery({ queryKey: ['status'], queryFn: () => get<Record<string, unknown> & { download_watcher: { active: boolean; path: string } }>('/system/status') });
  const health = useQuery({ queryKey: ['health'], queryFn: () => get<Check[]>('/system/health'), refetchInterval: 60000 });
  const icon = { ok: <CheckCircle2 size={16} color={palette.green} />, warning: <TriangleAlert size={16} color={palette.amber} />, error: <XCircle size={16} color={palette.red} /> };
  return (
    <Grid container spacing={2}>
      <Grid size={{ xs: 12, lg: 7 }}>
        <Section title="Health">
          {health.isLoading ? <Loading /> : (
            <Table size="small">
              <TableBody>
                {health.data?.map((c) => (
                  <TableRow key={c.source + c.message}>
                    <TableCell sx={{ width: 30 }}>{icon[c.status]}</TableCell>
                    <TableCell sx={{ fontWeight: 600, whiteSpace: 'nowrap' }}>{c.source}</TableCell>
                    <TableCell sx={{ color: c.status === 'ok' ? 'text.secondary' : undefined }}>{c.message}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Section>
      </Grid>
      <Grid size={{ xs: 12, lg: 5 }}>
        <Section title="About">
          {status.data && (
            <Table size="small">
              <TableBody>
                {[['Version', status.data.version], ['Python', status.data.python], ['Platform', status.data.platform], ['Database', status.data.database],
                  ['Data folder', status.data.data_dir], ['Uptime', duration(status.data.uptime_seconds as number)],
                  ['Download watcher', status.data.download_watcher.active ? `watching ${status.data.download_watcher.path}` : 'inactive (polling)']]
                  .map(([k, v]) => (
                    <TableRow key={String(k)}><TableCell sx={{ color: 'text.secondary' }}>{String(k)}</TableCell><TableCell sx={{ wordBreak: 'break-all' }}>{String(v)}</TableCell></TableRow>
                  ))}
              </TableBody>
            </Table>
          )}
          <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
            API docs: <a href="/api/docs" target="_blank" rel="noreferrer">/api/docs</a> · Prometheus: <a href="/metrics" target="_blank" rel="noreferrer">/metrics</a>
          </Typography>
        </Section>
      </Grid>
    </Grid>
  );
}

interface Task { name: string; title: string; interval_minutes: number; enabled: boolean; last_run: string | null; last_duration_ms: number | null; last_status: string | null; next_run: string | null }

function TasksTab() {
  const qc = useQueryClient();
  const can = useAuth((s) => s.can);
  const tasks = useQuery({ queryKey: ['tasks'], queryFn: () => get<Task[]>('/system/tasks'), refetchInterval: 15000 });
  const call = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  if (tasks.isLoading) return <Loading />;
  return (
    <Table size="small">
      <TableHead>
        <TableRow><TableCell>Task</TableCell><TableCell>Every</TableCell><TableCell>Enabled</TableCell><TableCell>Last run</TableCell><TableCell>Took</TableCell><TableCell>Next run</TableCell><TableCell /></TableRow>
      </TableHead>
      <TableBody>
        {tasks.data?.map((t) => (
          <TableRow key={t.name}>
            <TableCell sx={{ fontWeight: 600 }}>{t.title}</TableCell>
            <TableCell>
              <TextField size="small" type="number" defaultValue={t.interval_minutes} disabled={!can('system:admin')} sx={{ width: 110 }}
                InputProps={{ endAdornment: <Typography variant="caption">min</Typography> }}
                onBlur={(e) => { const interval_minutes = Number(e.target.value); if (interval_minutes !== t.interval_minutes) call.mutate(() => put(`/system/tasks/${t.name}`, { interval_minutes })); }} />
            </TableCell>
            <TableCell><Switch checked={t.enabled} disabled={!can('system:admin')} onChange={(_, enabled) => call.mutate(() => put(`/system/tasks/${t.name}`, { enabled }))} /></TableCell>
            <TableCell>{relative(t.last_run)} {t.last_status && <StatusChip status={t.last_status} />}</TableCell>
            <TableCell>{t.last_duration_ms !== null ? `${(t.last_duration_ms / 1000).toFixed(1)}s` : '—'}</TableCell>
            <TableCell>{t.enabled && t.next_run ? dateTime(t.next_run) : '—'}</TableCell>
            <TableCell align="right">
              {can('library:scan') && (
                <Tooltip title="Run now"><IconButton size="small" onClick={() => call.mutate(() => post(`/system/tasks/${t.name}/run`).then(() => toast(`${t.title} queued`, 'info')))}><Play size={15} /></IconButton></Tooltip>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function JobsTab() {
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState('');
  const can = useAuth((s) => s.can);
  const jobs = useQuery({
    queryKey: ['jobs', page, status],
    placeholderData: keepPreviousData,
    queryFn: () => get<Page<Job>>('/system/jobs', { page, page_size: 50, status: status || undefined }),
    refetchInterval: 5000,
  });
  return (
    <>
      <TextField select size="small" label="Status" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} sx={{ mb: 2, minWidth: 180 }}>
        {['', 'queued', 'running', 'completed', 'failed', 'cancelled'].map((s) => <MenuItem key={s} value={s}>{s || 'All'}</MenuItem>)}
      </TextField>
      <ErrorAlert error={jobs.error} />
      <Table size="small">
        <TableHead><TableRow><TableCell>#</TableCell><TableCell>Job</TableCell><TableCell>Status</TableCell><TableCell>Progress</TableCell><TableCell>Trigger</TableCell><TableCell>Created</TableCell><TableCell>Result</TableCell><TableCell /></TableRow></TableHead>
        <TableBody>
          {jobs.data?.items.map((j) => (
            <TableRow key={j.id}>
              <TableCell>{j.id}</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>{j.title}</TableCell>
              <TableCell><StatusChip status={j.status} />{j.attempts > 1 && <Typography variant="caption" color="text.secondary"> ×{j.attempts}</Typography>}</TableCell>
              <TableCell sx={{ minWidth: 140 }}>
                {j.status === 'running' ? <><LinearProgress variant="determinate" value={j.progress ?? 0} /><Typography variant="caption" color="text.secondary">{j.message}</Typography></> : null}
              </TableCell>
              <TableCell>{j.trigger}</TableCell>
              <TableCell sx={{ whiteSpace: 'nowrap' }}>{relative(j.created_at)}</TableCell>
              <TableCell sx={{ maxWidth: 360 }}>
                {j.error ? <Typography variant="caption" color="error">{j.error}</Typography> : j.result ? (
                  <Tooltip title={<pre style={{ margin: 0, maxHeight: 300, overflow: 'auto' }}>{JSON.stringify(j.result, null, 2).slice(0, 3000)}</pre>}>
                    <Typography variant="caption" color="text.secondary" noWrap display="block">{JSON.stringify(j.result).slice(0, 80)}</Typography>
                  </Tooltip>
                ) : null}
              </TableCell>
              <TableCell>
                {j.status === 'queued' && can('system:admin') && (
                  <Tooltip title="Cancel"><IconButton size="small" onClick={() => post(`/system/jobs/${j.id}/cancel`).then(() => jobs.refetch())}><Ban size={14} /></IconButton></Tooltip>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {jobs.data && jobs.data.pages > 1 && <Stack alignItems="center" sx={{ mt: 2 }}><Pagination count={jobs.data.pages} page={page} onChange={(_, p) => setPage(p)} /></Stack>}
    </>
  );
}

interface Audit { id: number; timestamp: string; username: string | null; category: string; action: string; entity_type: string | null; entity_id: string | null;
  severity: string; success: boolean; ip_address: string | null; details: Record<string, unknown> | null }

function AuditTab() {
  const [page, setPage] = useState(1);
  const [category, setCategory] = useState('');
  const audit = useQuery({
    queryKey: ['audit', page, category],
    placeholderData: keepPreviousData,
    queryFn: () => get<Page<Audit>>('/system/audit', { page, page_size: 50, category: category || undefined }),
  });
  return (
    <>
      <TextField select size="small" label="Category" value={category} onChange={(e) => { setCategory(e.target.value); setPage(1); }} sx={{ mb: 2, minWidth: 180 }}>
        {['', 'security', 'media', 'import', 'rename', 'delete', 'settings', 'system'].map((c) => <MenuItem key={c} value={c}>{c || 'All'}</MenuItem>)}
      </TextField>
      <Table size="small">
        <TableHead><TableRow><TableCell>When</TableCell><TableCell>User</TableCell><TableCell>Category</TableCell><TableCell>Action</TableCell><TableCell>Target</TableCell><TableCell>Details</TableCell></TableRow></TableHead>
        <TableBody>
          {audit.data?.items.map((a) => (
            <TableRow key={a.id} sx={{ bgcolor: !a.success ? `${palette.red}14` : undefined }}>
              <TableCell sx={{ whiteSpace: 'nowrap' }}><Tooltip title={dateTime(a.timestamp)}><span>{relative(a.timestamp)}</span></Tooltip></TableCell>
              <TableCell>{a.username ?? 'system'}<Typography variant="caption" display="block" color="text.secondary">{a.ip_address}</Typography></TableCell>
              <TableCell><Chip size="small" label={a.category} variant="outlined" color={a.category === 'security' ? 'warning' : 'default'} /></TableCell>
              <TableCell sx={{ fontWeight: 600 }}>{a.action}{!a.success && <Chip size="small" color="error" label="failed" sx={{ ml: 1 }} />}</TableCell>
              <TableCell>{a.entity_type ? `${a.entity_type} ${a.entity_id ?? ''}` : '—'}</TableCell>
              <TableCell sx={{ maxWidth: 380 }}>
                <Typography variant="caption" color="text.secondary" sx={{ fontFamily: "'JetBrains Mono', monospace", wordBreak: 'break-all' }}>
                  {a.details ? JSON.stringify(a.details).slice(0, 240) : ''}
                </Typography>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {audit.data && audit.data.pages > 1 && <Stack alignItems="center" sx={{ mt: 2 }}><Pagination count={audit.data.pages} page={page} onChange={(_, p) => setPage(p)} /></Stack>}
    </>
  );
}

interface LogLine { ts: string; level: string; logger: string; msg: string; request_id?: string }

function LogsTab() {
  const [level, setLevel] = useState('');
  const logs = useQuery({ queryKey: ['logs', level], queryFn: () => get<LogLine[]>('/system/logs', { lines: 500, level: level || undefined }), refetchInterval: 10000 });
  const color: Record<string, string> = { ERROR: palette.red, WARNING: palette.amber, INFO: palette.cyan, DEBUG: palette.dim };
  return (
    <>
      <TextField select size="small" label="Level" value={level} onChange={(e) => setLevel(e.target.value)} sx={{ mb: 2, minWidth: 160 }}>
        {['', 'ERROR', 'WARNING', 'INFO'].map((l) => <MenuItem key={l} value={l}>{l || 'All'}</MenuItem>)}
      </TextField>
      <Alert severity="info" sx={{ mb: 2 }}>Structured JSON logs (data/logs/maniac.log). Credentials are redacted before anything is written.</Alert>
      <Box sx={{ fontFamily: "'JetBrains Mono', monospace", fontSize: '0.75rem', bgcolor: '#0a0b14', p: 2, borderRadius: 2, maxHeight: '65vh', overflow: 'auto' }}>
        {logs.data?.map((l, i) => (
          <div key={i}>
            <span style={{ color: palette.dim }}>{l.ts.slice(0, 19).replace('T', ' ')}</span>{' '}
            <span style={{ color: color[l.level] ?? palette.text }}>{l.level.padEnd(7)}</span>{' '}
            <span style={{ color: palette.purple }}>{l.logger}</span> {l.msg}
          </div>
        ))}
      </Box>
    </>
  );
}

function PluginsTab() {
  const data = useQuery({ queryKey: ['plugins'], queryFn: () => get<{ plugins: Record<string, PluginInfo[]>; errors: { source: string; error: string }[]; plugin_dir: string }>('/plugins') });
  if (data.isLoading || !data.data) return <Loading />;
  return (
    <Stack spacing={2}>
      <Alert severity="info">Drop a Python module into <code>{data.data.plugin_dir}</code> (or install a package exposing the <code>maniac_media.plugins</code> entry point) and restart – no core changes needed.</Alert>
      {data.data.errors.map((e) => <Alert key={e.source} severity="error">{e.source}: {e.error}</Alert>)}
      {Object.entries(data.data.plugins).map(([kind, list]) => (
        <Section key={kind} title={kind.replace('_', ' ')}>
          <Grid container spacing={1}>
            {list.map((p) => (
              <Grid key={p.key} size={{ xs: 12, md: 6, lg: 4 }}>
                <Box sx={{ p: 1.5, border: `1px solid ${palette.border}`, borderRadius: 2 }}>
                  <Typography fontWeight={700}>{p.name} {!p.builtin && <Chip size="small" color="secondary" label="external" />}</Typography>
                  <Typography variant="caption" color="text.secondary">{p.description}</Typography>
                </Box>
              </Grid>
            ))}
          </Grid>
        </Section>
      ))}
    </Stack>
  );
}
