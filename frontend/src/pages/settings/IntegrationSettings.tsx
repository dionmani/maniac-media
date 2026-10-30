import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  Grid,
  IconButton,
  MenuItem,
  Paper,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, Pencil, Plus, Send, Trash2, XCircle } from 'lucide-react';
import { useState } from 'react';
import { del, get, post, put } from '../../api/client';
import type { PluginInfo } from '../../api/types';
import { Empty, Loading, StatusChip, useConfirm } from '../../components/common';
import { useAuth } from '../../store/auth';
import { toast } from '../../store/ui';
import { relative, errorMessage } from '../../utils/format';

type Catalogue = { plugins: Record<string, PluginInfo[]>; errors: { source: string; error: string }[]; plugin_dir: string };
const usePlugins = () => useQuery({ queryKey: ['plugins'], queryFn: () => get<Catalogue>('/plugins'), staleTime: 300_000 });

function TestResult({ result }: { result: { success: boolean; message: string } | null }) {
  if (!result) return null;
  return <Alert severity={result.success ? 'success' : 'error'} icon={result.success ? <CheckCircle2 size={18} /> : <XCircle size={18} />} sx={{ mt: 2 }}>{result.message}</Alert>;
}

// ------------------------------------------------------------------ indexers
interface Indexer {
  id?: number; name: string; implementation: string; url: string; api_path: string; api_key?: string; has_api_key?: boolean; enabled: boolean;
  priority: number; enable_rss: boolean; enable_search: boolean; rate_limit_per_minute: number; minimum_seeders: number;
  health_status?: string; health_message?: string; last_health_check?: string; failure_count?: number; disabled_until?: string | null; protocol?: string;
}

export function IndexerSettings() {
  const qc = useQueryClient();
  const canWrite = useAuth((s) => s.can)('settings:write');
  const plugins = usePlugins();
  const list = useQuery({ queryKey: ['indexers'], queryFn: () => get<Indexer[]>('/indexers') });
  const [edit, setEdit] = useState<Indexer | null>(null);
  const [test, setTest] = useState<{ success: boolean; message: string } | null>(null);
  const { confirm, dialog } = useConfirm();

  const save = useMutation({
    mutationFn: (i: Indexer) => (i.id ? put(`/indexers/${i.id}`, i) : post('/indexers', i)),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['indexers'] }); setEdit(null); toast('Indexer saved', 'success'); },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const runTest = useMutation({
    mutationFn: (i: Indexer) => post<{ success: boolean; message: string }>('/indexers/test', i, { indexer_id: i.id }),
    onSuccess: setTest,
    onError: (e) => setTest({ success: false, message: errorMessage(e) }),
  });

  if (list.isLoading || plugins.isLoading) return <Loading />;
  const impls = plugins.data?.plugins.indexer ?? [];
  const blank = (impl: string): Indexer => ({ name: impls.find((p) => p.key === impl)?.name ?? impl, implementation: impl, url: '', api_path: '/api', enabled: true, priority: 25,
    enable_rss: true, enable_search: true, rate_limit_per_minute: 30, minimum_seeders: 1 });

  return (
    <>
      <Stack direction="row" spacing={1} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
        {canWrite && impls.map((p) => (
          <Tooltip key={p.key} title={p.description}><Button variant="outlined" startIcon={<Plus size={15} />} onClick={() => { setTest(null); setEdit(blank(p.key)); }}>{p.name}</Button></Tooltip>
        ))}
      </Stack>
      {!list.data?.length ? <Empty title="No indexers yet">Add a Torznab/Newznab endpoint (Prowlarr, Jackett, NZBGeek…) or an RSS feed.</Empty> : (
        <Grid container spacing={2}>
          {list.data.map((i) => (
            <Grid key={i.id} size={{ xs: 12, md: 6, xl: 4 }}>
              <Paper sx={{ p: 2 }}>
                <Stack direction="row" justifyContent="space-between" alignItems="center">
                  <Box>
                    <Typography fontWeight={700}>{i.name}</Typography>
                    <Typography variant="caption" color="text.secondary">{i.implementation} · {i.protocol} · priority {i.priority}</Typography>
                  </Box>
                  <Stack direction="row" alignItems="center">
                    <StatusChip status={i.enabled ? i.health_status ?? 'unknown' : 'cancelled'} label={i.enabled ? i.health_status ?? 'unknown' : 'disabled'} />
                    {canWrite && <IconButton size="small" onClick={() => { setTest(null); setEdit(i); }}><Pencil size={15} /></IconButton>}
                    {canWrite && <IconButton size="small" color="error" onClick={async () => {
                      if (await confirm('Delete indexer?', i.name, { danger: true, confirmLabel: 'Delete' })) del(`/indexers/${i.id}`).then(() => qc.invalidateQueries({ queryKey: ['indexers'] }));
                    }}><Trash2 size={15} /></IconButton>}
                  </Stack>
                </Stack>
                {i.health_message && <Typography variant="caption" display="block" color={i.health_status === 'ok' ? 'text.secondary' : 'error'} sx={{ mt: 1 }}>{i.health_message} · {relative(i.last_health_check)}</Typography>}
                {i.disabled_until && <Typography variant="caption" color="warning.main" display="block">Paused after repeated failures until {new Date(i.disabled_until + 'Z').toLocaleTimeString()}</Typography>}
                <Stack direction="row" spacing={0.5} sx={{ mt: 1 }}>
                  {i.enable_search && <Chip size="small" label="Search" variant="outlined" />}
                  {i.enable_rss && <Chip size="small" label="RSS" variant="outlined" />}
                  <Chip size="small" label={`${i.rate_limit_per_minute}/min`} variant="outlined" />
                </Stack>
              </Paper>
            </Grid>
          ))}
        </Grid>
      )}
      {edit && (
        <Dialog open onClose={() => setEdit(null)} maxWidth="sm" fullWidth>
          <DialogTitle>{edit.id ? 'Edit' : 'Add'} {impls.find((p) => p.key === edit.implementation)?.name} indexer</DialogTitle>
          <DialogContent>
            <Stack spacing={2} sx={{ mt: 1 }}>
              <TextField label="Name" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
              {edit.implementation !== 'demo' && (
                <>
                  <TextField label="URL" value={edit.url} onChange={(e) => setEdit({ ...edit, url: e.target.value })}
                    helperText={impls.find((p) => p.key === edit.implementation)?.fields.find((f) => f.name === 'url')?.help} />
                  {edit.implementation !== 'rss' && <TextField label="API path" value={edit.api_path} onChange={(e) => setEdit({ ...edit, api_path: e.target.value })} />}
                  <TextField label={edit.implementation === 'rss' ? 'Cookie (optional)' : 'API key'} type="password" value={edit.api_key ?? ''}
                    placeholder={edit.has_api_key ? '•••••••• (unchanged)' : ''} onChange={(e) => setEdit({ ...edit, api_key: e.target.value })}
                    helperText="Stored encrypted (AES-256-GCM); never shown again" />
                </>
              )}
              <Stack direction="row" spacing={2}>
                <TextField type="number" label="Priority" value={edit.priority} onChange={(e) => setEdit({ ...edit, priority: Number(e.target.value) })} helperText="1 = highest" />
                <TextField type="number" label="Requests / minute" value={edit.rate_limit_per_minute} onChange={(e) => setEdit({ ...edit, rate_limit_per_minute: Number(e.target.value) })} />
                <TextField type="number" label="Min seeders" value={edit.minimum_seeders} onChange={(e) => setEdit({ ...edit, minimum_seeders: Number(e.target.value) })} />
              </Stack>
              <Stack direction="row">
                <FormControlLabel control={<Switch checked={edit.enabled} onChange={(e) => setEdit({ ...edit, enabled: e.target.checked })} />} label="Enabled" />
                <FormControlLabel control={<Switch checked={edit.enable_search} onChange={(e) => setEdit({ ...edit, enable_search: e.target.checked })} />} label="Search" />
                <FormControlLabel control={<Switch checked={edit.enable_rss} onChange={(e) => setEdit({ ...edit, enable_rss: e.target.checked })} />} label="RSS sync" />
              </Stack>
            </Stack>
            <TestResult result={test} />
          </DialogContent>
          <DialogActions>
            <Button onClick={() => runTest.mutate(edit)} disabled={runTest.isPending}>{runTest.isPending ? 'Testing…' : 'Test'}</Button>
            <Box sx={{ flex: 1 }} />
            <Button onClick={() => setEdit(null)}>Cancel</Button>
            <Button variant="contained" onClick={() => save.mutate(edit)} disabled={save.isPending}>Save</Button>
          </DialogActions>
        </Dialog>
      )}
      {dialog}
    </>
  );
}

// ------------------------------------------------------------------ download clients
interface Client {
  id?: number; name: string; client_type: string; host: string; port: number | null; use_ssl: boolean; url_base: string; username: string;
  password?: string; api_key?: string; has_password?: boolean; has_api_key?: boolean; category: string; tv_category?: string | null; enabled: boolean; priority: number;
  remove_completed: boolean; remote_path: string; local_path: string; protocol?: string; health_status?: string; health_message?: string;
}

export function ClientSettings() {
  const qc = useQueryClient();
  const canWrite = useAuth((s) => s.can)('settings:write');
  const plugins = usePlugins();
  const list = useQuery({ queryKey: ['download-clients'], queryFn: () => get<Client[]>('/download-clients') });
  const [edit, setEdit] = useState<Client | null>(null);
  const [test, setTest] = useState<{ success: boolean; message: string } | null>(null);
  const { confirm, dialog } = useConfirm();

  const save = useMutation({
    mutationFn: (c: Client) => (c.id ? put(`/download-clients/${c.id}`, c) : post('/download-clients', c)),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['download-clients'] }); setEdit(null); toast('Download client saved', 'success'); },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const runTest = useMutation({
    mutationFn: (c: Client) => post<{ success: boolean; message: string }>('/download-clients/test', c, { client_id: c.id }),
    onSuccess: setTest,
    onError: (e) => setTest({ success: false, message: errorMessage(e) }),
  });

  if (list.isLoading || plugins.isLoading) return <Loading />;
  const impls = plugins.data?.plugins.download_client ?? [];
  const spec = (type: string) => impls.find((p) => p.key === type);
  const has = (type: string, field: string) => !!spec(type)?.fields.some((f) => f.name === field);
  const blank = (p: PluginInfo): Client => ({ name: p.name, client_type: p.key, host: 'localhost', port: p.default_port ?? null, use_ssl: false,
    url_base: String(p.fields.find((f) => f.name === 'url_base')?.default ?? ''), username: String(p.fields.find((f) => f.name === 'username')?.default ?? ''),
    category: 'movies', tv_category: 'tv', enabled: true, priority: 1, remove_completed: false, remote_path: '', local_path: '' });

  return (
    <>
      <Stack direction="row" spacing={1} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
        {canWrite && impls.map((p) => (
          <Tooltip key={p.key} title={p.description}><Button variant="outlined" startIcon={<Plus size={15} />} onClick={() => { setTest(null); setEdit(blank(p)); }}>{p.name}</Button></Tooltip>
        ))}
      </Stack>
      {!list.data?.length ? <Empty title="No download clients yet">Add qBittorrent, Transmission, Deluge, SABnzbd or NZBGet.</Empty> : (
        <Grid container spacing={2}>
          {list.data.map((c) => (
            <Grid key={c.id} size={{ xs: 12, md: 6, xl: 4 }}>
              <Paper sx={{ p: 2 }}>
                <Stack direction="row" justifyContent="space-between" alignItems="center">
                  <Box>
                    <Typography fontWeight={700}>{c.name}</Typography>
                    <Typography variant="caption" color="text.secondary">{spec(c.client_type)?.name} · {c.protocol} · {c.host}{c.port ? `:${c.port}` : ''} · {c.tv_category && c.tv_category !== c.category ? `${c.category} / ${c.tv_category}` : c.category}</Typography>
                  </Box>
                  <Stack direction="row" alignItems="center">
                    <StatusChip status={c.enabled ? c.health_status ?? 'unknown' : 'cancelled'} label={c.enabled ? c.health_status ?? 'unknown' : 'disabled'} />
                    {canWrite && <IconButton size="small" onClick={() => { setTest(null); setEdit(c); }}><Pencil size={15} /></IconButton>}
                    {canWrite && <IconButton size="small" color="error" onClick={async () => {
                      if (await confirm('Delete download client?', c.name, { danger: true, confirmLabel: 'Delete' })) del(`/download-clients/${c.id}`).then(() => qc.invalidateQueries({ queryKey: ['download-clients'] }));
                    }}><Trash2 size={15} /></IconButton>}
                  </Stack>
                </Stack>
                {c.health_message && <Typography variant="caption" color={c.health_status === 'error' ? 'error' : 'text.secondary'}>{c.health_message}</Typography>}
              </Paper>
            </Grid>
          ))}
        </Grid>
      )}
      {edit && (
        <Dialog open onClose={() => setEdit(null)} maxWidth="sm" fullWidth>
          <DialogTitle>{edit.id ? 'Edit' : 'Add'} {spec(edit.client_type)?.name}</DialogTitle>
          <DialogContent>
            <Stack spacing={2} sx={{ mt: 1 }}>
              <TextField label="Name" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
              {edit.client_type === 'simulator' && <Alert severity="info">The simulator writes placeholder files into your download folder – use it only for demos and testing.</Alert>}
              {has(edit.client_type, 'host') && (
                <Stack direction="row" spacing={2}>
                  <TextField fullWidth label="Host" value={edit.host} onChange={(e) => setEdit({ ...edit, host: e.target.value })} />
                  <TextField label="Port" type="number" value={edit.port ?? ''} onChange={(e) => setEdit({ ...edit, port: Number(e.target.value) })} sx={{ width: 120 }} />
                </Stack>
              )}
              {has(edit.client_type, 'url_base') && (
                <Stack direction="row" spacing={2} alignItems="center">
                  <TextField fullWidth label="URL base" value={edit.url_base ?? ''} onChange={(e) => setEdit({ ...edit, url_base: e.target.value })} />
                  <FormControlLabel control={<Checkbox checked={edit.use_ssl} onChange={(e) => setEdit({ ...edit, use_ssl: e.target.checked })} />} label="SSL" />
                </Stack>
              )}
              {has(edit.client_type, 'username') && <TextField label="Username" value={edit.username ?? ''} onChange={(e) => setEdit({ ...edit, username: e.target.value })} />}
              {has(edit.client_type, 'password') && (
                <TextField label="Password" type="password" value={edit.password ?? ''} placeholder={edit.has_password ? '•••••••• (unchanged)' : ''}
                  onChange={(e) => setEdit({ ...edit, password: e.target.value })} helperText="Stored encrypted; never shown again" />
              )}
              {has(edit.client_type, 'api_key') && (
                <TextField label="API key" type="password" value={edit.api_key ?? ''} placeholder={edit.has_api_key ? '•••••••• (unchanged)' : ''}
                  onChange={(e) => setEdit({ ...edit, api_key: e.target.value })} />
              )}
              <Stack direction="row" spacing={2}>
                <TextField fullWidth label="Movie category / label" value={edit.category} onChange={(e) => setEdit({ ...edit, category: e.target.value })} />
                <TextField fullWidth label="TV category / label" value={edit.tv_category ?? ''} placeholder={edit.category}
                  onChange={(e) => setEdit({ ...edit, tv_category: e.target.value })} helperText="Empty: TV uses the movie category" />
                <TextField label="Priority" type="number" value={edit.priority} onChange={(e) => setEdit({ ...edit, priority: Number(e.target.value) })} sx={{ width: 120 }} />
              </Stack>
              {edit.client_type !== 'simulator' && (
                <>
                  <Typography variant="subtitle2">Remote path mapping <Typography component="span" variant="caption" color="text.secondary">(when the client sees a different path, e.g. Docker)</Typography></Typography>
                  <Stack direction="row" spacing={2}>
                    <TextField fullWidth size="small" label="Client path" placeholder="/downloads" value={edit.remote_path ?? ''} onChange={(e) => setEdit({ ...edit, remote_path: e.target.value })} />
                    <TextField fullWidth size="small" label="Local path" placeholder="M:\downloads" value={edit.local_path ?? ''} onChange={(e) => setEdit({ ...edit, local_path: e.target.value })} />
                  </Stack>
                </>
              )}
              <Stack direction="row">
                <FormControlLabel control={<Switch checked={edit.enabled} onChange={(e) => setEdit({ ...edit, enabled: e.target.checked })} />} label="Enabled" />
                <FormControlLabel control={<Switch checked={edit.remove_completed} onChange={(e) => setEdit({ ...edit, remove_completed: e.target.checked })} />} label="Remove after import" />
              </Stack>
            </Stack>
            <TestResult result={test} />
          </DialogContent>
          <DialogActions>
            <Button onClick={() => runTest.mutate(edit)} disabled={runTest.isPending}>{runTest.isPending ? 'Testing…' : 'Test'}</Button>
            <Box sx={{ flex: 1 }} />
            <Button onClick={() => setEdit(null)}>Cancel</Button>
            <Button variant="contained" onClick={() => save.mutate(edit)} disabled={save.isPending}>Save</Button>
          </DialogActions>
        </Dialog>
      )}
      {dialog}
    </>
  );
}

// ------------------------------------------------------------------ notifications
interface Channel { id?: number; name: string; implementation: string; settings: Record<string, unknown>; configured_secrets?: string[]; events: string[]; enabled: boolean }

export function NotificationSettings() {
  const qc = useQueryClient();
  const canWrite = useAuth((s) => s.can)('settings:write');
  const plugins = usePlugins();
  const list = useQuery({ queryKey: ['notifications'], queryFn: () => get<{ channels: Channel[]; events: Record<string, string> }>('/notifications') });
  const [edit, setEdit] = useState<Channel | null>(null);
  const { confirm, dialog } = useConfirm();

  const save = useMutation({
    mutationFn: (c: Channel) => (c.id ? put(`/notifications/${c.id}`, c) : post('/notifications', c)),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['notifications'] }); setEdit(null); toast('Notification channel saved', 'success'); },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  if (list.isLoading || plugins.isLoading) return <Loading />;
  const impls = plugins.data?.plugins.notification ?? [];
  const events = list.data?.events ?? {};
  const spec = edit ? impls.find((p) => p.key === edit.implementation) : undefined;

  return (
    <>
      <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
        {canWrite && impls.map((p) => (
          <Button key={p.key} variant="outlined" startIcon={<Plus size={15} />}
            onClick={() => setEdit({ name: p.name, implementation: p.key, settings: Object.fromEntries(p.fields.filter((f) => !f.secret).map((f) => [f.name, f.default ?? ''])), events: Object.keys(events), enabled: true })}>
            {p.name}
          </Button>
        ))}
      </Stack>
      {!list.data?.channels.length ? <Empty title="No notification channels">Get told when imports complete, downloads fail or health problems appear.</Empty> : (
        <Grid container spacing={2}>
          {list.data.channels.map((c) => (
            <Grid key={c.id} size={{ xs: 12, md: 6 }}>
              <Paper sx={{ p: 2 }}>
                <Stack direction="row" justifyContent="space-between" alignItems="center">
                  <Box>
                    <Typography fontWeight={700}>{c.name}</Typography>
                    <Typography variant="caption" color="text.secondary">{c.implementation} · {c.enabled ? 'enabled' : 'disabled'}</Typography>
                  </Box>
                  <Stack direction="row">
                    <Tooltip title="Send test"><IconButton size="small" onClick={() => post<{ success: boolean; message: string }>(`/notifications/${c.id}/test`).then((r) => toast(r.message, r.success ? 'success' : 'error'))}><Send size={15} /></IconButton></Tooltip>
                    {canWrite && <IconButton size="small" onClick={() => setEdit(c)}><Pencil size={15} /></IconButton>}
                    {canWrite && <IconButton size="small" color="error" onClick={async () => {
                      if (await confirm('Delete channel?', c.name, { danger: true, confirmLabel: 'Delete' })) del(`/notifications/${c.id}`).then(() => qc.invalidateQueries({ queryKey: ['notifications'] }));
                    }}><Trash2 size={15} /></IconButton>}
                  </Stack>
                </Stack>
                <Stack direction="row" spacing={0.5} sx={{ mt: 1 }} flexWrap="wrap" useFlexGap>
                  {c.events.map((e) => <Chip key={e} size="small" label={events[e] ?? e} variant="outlined" />)}
                </Stack>
              </Paper>
            </Grid>
          ))}
        </Grid>
      )}
      {edit && spec && (
        <Dialog open onClose={() => setEdit(null)} maxWidth="sm" fullWidth>
          <DialogTitle>{edit.id ? 'Edit' : 'Add'} {spec.name} channel</DialogTitle>
          <DialogContent>
            <Stack spacing={2} sx={{ mt: 1 }}>
              <TextField label="Name" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
              {spec.fields.map((f) => f.type === 'bool' ? (
                <FormControlLabel key={f.name} label={f.label} control={<Switch checked={!!edit.settings[f.name]} onChange={(e) => setEdit({ ...edit, settings: { ...edit.settings, [f.name]: e.target.checked } })} />} />
              ) : f.type === 'select' ? (
                <TextField key={f.name} select label={f.label} value={edit.settings[f.name] ?? f.default ?? ''} onChange={(e) => setEdit({ ...edit, settings: { ...edit.settings, [f.name]: e.target.value } })}>
                  {f.options.map((o) => <MenuItem key={o} value={o}>{o}</MenuItem>)}
                </TextField>
              ) : (
                <TextField key={f.name} label={f.label} type={f.secret ? 'password' : f.type === 'number' ? 'number' : 'text'} required={f.required}
                  value={(edit.settings[f.name] as string) ?? ''} helperText={f.help ?? (f.secret ? 'Stored encrypted' : undefined)}
                  placeholder={f.secret && edit.configured_secrets?.includes(f.name) ? '•••••••• (unchanged)' : ''}
                  onChange={(e) => setEdit({ ...edit, settings: { ...edit.settings, [f.name]: f.type === 'number' ? Number(e.target.value) : e.target.value } })} />
              ))}
              <Typography variant="subtitle2">Events</Typography>
              <Box>
                {Object.entries(events).map(([k, label]) => (
                  <FormControlLabel key={k} label={label} control={<Checkbox checked={edit.events.includes(k)}
                    onChange={(e) => setEdit({ ...edit, events: e.target.checked ? [...edit.events, k] : edit.events.filter((x) => x !== k) })} />} />
                ))}
              </Box>
              <FormControlLabel control={<Switch checked={edit.enabled} onChange={(e) => setEdit({ ...edit, enabled: e.target.checked })} />} label="Enabled" />
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setEdit(null)}>Cancel</Button>
            <Button variant="contained" onClick={() => save.mutate(edit)}>Save</Button>
          </DialogActions>
        </Dialog>
      )}
      {dialog}
    </>
  );
}

// ------------------------------------------------------------------ metadata providers
interface Provider extends PluginInfo { enabled: boolean; priority: number; has_api_key: boolean }

export function MetadataSettings() {
  const qc = useQueryClient();
  const canWrite = useAuth((s) => s.can)('settings:write');
  const list = useQuery({ queryKey: ['metadata-providers'], queryFn: () => get<Provider[]>('/metadata-providers') });
  const [keys, setKeys] = useState<Record<string, string>>({});
  const update = useMutation({
    mutationFn: ({ key, body }: { key: string; body: object }) => put<{ message: string }>(`/metadata-providers/${key}`, body),
    onSuccess: (r) => { toast(r.message, 'success'); qc.invalidateQueries({ queryKey: ['metadata-providers'] }); setKeys({}); },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  if (list.isLoading) return <Loading />;
  return (
    <Stack spacing={2}>
      <Alert severity="info">Providers are tried in priority order (lowest number first). TVmaze and IMDb work without keys; add a free TMDB key for the richest movie data.</Alert>
      {list.data?.map((p) => (
        <Paper key={p.key} sx={{ p: 2 }}>
          <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} alignItems={{ md: 'center' }}>
            <Box sx={{ flex: 1 }}>
              <Typography fontWeight={700}>{p.name} {p.requires_api_key && (p.has_api_key ? <Chip size="small" color="success" label="key configured" /> : <Chip size="small" color="warning" label="needs API key" />)}</Typography>
              <Typography variant="caption" color="text.secondary">{p.description} · {[p.supports_movies && 'movies', p.supports_series && 'series'].filter(Boolean).join(' & ')}</Typography>
            </Box>
            <TextField size="small" type="number" label="Priority" defaultValue={p.priority} sx={{ width: 100 }} disabled={!canWrite}
              onBlur={(e) => Number(e.target.value) !== p.priority && update.mutate({ key: p.key, body: { priority: Number(e.target.value) } })} />
            {p.fields.some((f) => f.name === 'api_key') && canWrite && (
              <Stack direction="row" spacing={1}>
                <TextField size="small" type="password" label="API key" value={keys[p.key] ?? ''} onChange={(e) => setKeys({ ...keys, [p.key]: e.target.value })}
                  placeholder={p.has_api_key ? '•••••••• (unchanged)' : ''} />
                <Button variant="outlined" disabled={!keys[p.key]} onClick={() => update.mutate({ key: p.key, body: { api_key: keys[p.key] } })}>Verify & save</Button>
                {p.has_api_key && <Button color="error" onClick={() => update.mutate({ key: p.key, body: { clear_api_key: true } })}>Clear</Button>}
              </Stack>
            )}
            <Switch checked={p.enabled} disabled={!canWrite} onChange={(e) => update.mutate({ key: p.key, body: { enabled: e.target.checked } })} />
          </Stack>
        </Paper>
      ))}
    </Stack>
  );
}
