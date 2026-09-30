import {
  Button,
  Checkbox,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  IconButton,
  LinearProgress,
  MenuItem,
  Pagination,
  Stack,
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
import { Activity, FolderInput, RefreshCw, RotateCcw, Trash2, Unlock } from 'lucide-react';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { del, get, post } from '../api/client';
import type { HistoryItem, Page, QueueItem } from '../api/types';
import { Empty, ErrorAlert, Loading, PageHeader, QualityChip, StatusChip } from '../components/common';
import { useAuth } from '../store/auth';
import { toast } from '../store/ui';
import { palette } from '../theme';
import { bytes, dateTime, duration, errorMessage, relative, speed } from '../utils/format';

export default function ActivityPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') ?? 'queue';
  return (
    <>
      <PageHeader title="Activity" icon={<Activity color={palette.purple} />} subtitle="Live download queue, history and blocklist" />
      <Tabs value={tab} onChange={(_, v) => setParams({ tab: v })} sx={{ mb: 2 }}>
        <Tab value="queue" label="Queue" />
        <Tab value="history" label="History" />
        <Tab value="blocklist" label="Blocklist" />
      </Tabs>
      {tab === 'queue' && <QueueTab />}
      {tab === 'history' && <HistoryTab />}
      {tab === 'blocklist' && <BlocklistTab />}
    </>
  );
}

function QueueTab() {
  const can = useAuth((s) => s.can);
  const qc = useQueryClient();
  const [showDone, setShowDone] = useState(false);
  const [removing, setRemoving] = useState<QueueItem | null>(null);
  const [opts, setOpts] = useState({ remove_from_client: true, blocklist: false, delete_data: false });
  const queue = useQuery({
    queryKey: ['queue', showDone],
    queryFn: () => get<QueueItem[]>('/queue', { include_finished: showDone }),
    refetchInterval: (q) => ((q.state.data ?? []).some((i) => ['queued', 'downloading', 'importing'].includes(i.status)) ? 5000 : 30000),
  });
  const act = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['queue'] }),
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  return (
    <>
      <Stack direction="row" spacing={1} sx={{ mb: 2 }} alignItems="center">
        <FormControlLabel control={<Checkbox checked={showDone} onChange={(e) => setShowDone(e.target.checked)} />} label="Show imported" />
        <Button size="small" startIcon={<RefreshCw size={14} />} onClick={() => act.mutate(() => post('/queue/refresh'))}>Refresh from clients</Button>
      </Stack>
      <ErrorAlert error={queue.error} />
      {queue.isLoading ? <Loading /> : !queue.data?.length ? (
        <Empty icon={<Activity size={40} />} title="Queue is empty">Grabbed releases appear here while they download and import.</Empty>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Release</TableCell>
              <TableCell>Quality</TableCell>
              <TableCell sx={{ minWidth: 200 }}>Progress</TableCell>
              <TableCell>Status</TableCell>
              <TableCell>Indexer</TableCell>
              <TableCell align="right" />
            </TableRow>
          </TableHead>
          <TableBody>
            {queue.data.map((q) => (
              <TableRow key={q.id}>
                <TableCell sx={{ maxWidth: 420 }}>
                  <Typography variant="body2" fontWeight={600}>{q.title}</Typography>
                  <Typography variant="caption" sx={{ fontFamily: "'JetBrains Mono', monospace", wordBreak: 'break-all', color: 'text.secondary' }}>{q.release_title}</Typography>
                  {q.error_message && <Typography variant="caption" color="error" display="block">{q.error_message}</Typography>}
                </TableCell>
                <TableCell><QualityChip quality={q.quality} /></TableCell>
                <TableCell>
                  <LinearProgress variant="determinate" value={q.progress ?? 0} />
                  <Typography variant="caption" color="text.secondary">
                    {(q.progress ?? 0).toFixed(0)}% of {bytes(q.size_bytes)}
                    {q.status === 'downloading' && q.download_speed_bps ? ` · ${speed(q.download_speed_bps)}` : ''}
                    {['queued', 'downloading'].includes(q.status) && q.eta_seconds ? ` · ETA ${duration(q.eta_seconds)}` : ''}
                  </Typography>
                </TableCell>
                <TableCell><StatusChip status={q.status} /></TableCell>
                <TableCell>{q.indexer_name ?? '—'}<Typography variant="caption" display="block" color="text.secondary">{q.protocol}</Typography></TableCell>
                <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                  {can('files:import') && ['completed', 'import_failed'].includes(q.status) && (
                    <Tooltip title="Retry import"><IconButton size="small" onClick={() => act.mutate(() => post(`/queue/${q.id}/import`))}><FolderInput size={15} /></IconButton></Tooltip>
                  )}
                  {can('queue:write') && q.status === 'failed' && (
                    <Tooltip title="Re-send to download client"><IconButton size="small" onClick={() => act.mutate(() => post(`/queue/${q.id}/retry`))}><RotateCcw size={15} /></IconButton></Tooltip>
                  )}
                  {can('queue:write') && (
                    <Tooltip title="Remove"><IconButton size="small" color="error" onClick={() => setRemoving(q)}><Trash2 size={15} /></IconButton></Tooltip>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <Dialog open={!!removing} onClose={() => setRemoving(null)} maxWidth="xs" fullWidth>
        <DialogTitle>Remove from queue</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: 1, wordBreak: 'break-all' }}>{removing?.release_title}</Typography>
          <FormControlLabel control={<Checkbox checked={opts.remove_from_client} onChange={(e) => setOpts({ ...opts, remove_from_client: e.target.checked })} />} label="Remove from download client" />
          <FormControlLabel control={<Checkbox checked={opts.delete_data} onChange={(e) => setOpts({ ...opts, delete_data: e.target.checked })} />} label="Delete downloaded data" />
          <FormControlLabel control={<Checkbox checked={opts.blocklist} onChange={(e) => setOpts({ ...opts, blocklist: e.target.checked })} />} label="Blocklist this release" />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRemoving(null)}>Cancel</Button>
          <Button color="error" variant="contained" onClick={() => { const id = removing!.id; setRemoving(null); act.mutate(() => del(`/queue/${id}`, opts)); }}>Remove</Button>
        </DialogActions>
      </Dialog>
    </>
  );
}

const EVENTS = ['', 'grabbed', 'downloaded', 'imported', 'upgraded', 'renamed', 'download_failed', 'import_failed', 'added', 'deleted'];

function HistoryTab() {
  const [page, setPage] = useState(1);
  const [event, setEvent] = useState('');
  const history = useQuery({
    queryKey: ['history', page, event],
    placeholderData: keepPreviousData,
    queryFn: () => get<Page<HistoryItem>>('/history', { page, page_size: 50, event_type: event || undefined }),
  });
  return (
    <>
      <TextField select size="small" label="Event" value={event} onChange={(e) => { setEvent(e.target.value); setPage(1); }} sx={{ mb: 2, minWidth: 200 }}>
        {EVENTS.map((e) => <MenuItem key={e} value={e}>{e ? e.replace('_', ' ') : 'All events'}</MenuItem>)}
      </TextField>
      <ErrorAlert error={history.error} />
      {history.isLoading ? <Loading /> : (
        <Table size="small">
          <TableHead>
            <TableRow><TableCell>When</TableCell><TableCell>Event</TableCell><TableCell>Item</TableCell><TableCell>Quality</TableCell><TableCell>Details</TableCell></TableRow>
          </TableHead>
          <TableBody>
            {history.data?.items.map((h) => (
              <TableRow key={h.id}>
                <TableCell sx={{ whiteSpace: 'nowrap' }}><Tooltip title={dateTime(h.timestamp)}><span>{relative(h.timestamp)}</span></Tooltip></TableCell>
                <TableCell><StatusChip status={h.event_type} /></TableCell>
                <TableCell sx={{ fontWeight: 600, maxWidth: 320, wordBreak: 'break-all' }}>{h.title}</TableCell>
                <TableCell><QualityChip quality={h.quality} /></TableCell>
                <TableCell sx={{ color: 'text.secondary', fontSize: '0.8rem', wordBreak: 'break-all' }}>{h.details}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {history.data && history.data.pages > 1 && <Stack alignItems="center" sx={{ mt: 2 }}><Pagination count={history.data.pages} page={page} onChange={(_, p) => setPage(p)} /></Stack>}
    </>
  );
}

interface BlockItem { id: number; media_type: string; media_id: number; release_title: string; indexer?: string; reason?: string; created_at: string }

function BlocklistTab() {
  const qc = useQueryClient();
  const can = useAuth((s) => s.can);
  const list = useQuery({ queryKey: ['blocklist'], queryFn: () => get<Page<BlockItem>>('/blocklist', { page_size: 200 }) });
  if (list.isLoading) return <Loading />;
  if (!list.data?.items.length) return <Empty title="Blocklist is empty">Failed or rejected releases are listed here and never grabbed again.</Empty>;
  return (
    <Table size="small">
      <TableHead><TableRow><TableCell>Release</TableCell><TableCell>Indexer</TableCell><TableCell>Reason</TableCell><TableCell>Added</TableCell><TableCell /></TableRow></TableHead>
      <TableBody>
        {list.data.items.map((b) => (
          <TableRow key={b.id}>
            <TableCell sx={{ fontFamily: "'JetBrains Mono', monospace", fontSize: '0.78rem', wordBreak: 'break-all' }}>{b.release_title}</TableCell>
            <TableCell>{b.indexer ?? '—'}</TableCell>
            <TableCell><Chip size="small" label={b.reason ?? '—'} variant="outlined" /></TableCell>
            <TableCell>{relative(b.created_at)}</TableCell>
            <TableCell align="right">
              {can('queue:write') && (
                <Tooltip title="Unblock"><IconButton size="small" onClick={() => del(`/blocklist/${b.id}`).then(() => qc.invalidateQueries({ queryKey: ['blocklist'] }))}><Unlock size={15} /></IconButton></Tooltip>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
