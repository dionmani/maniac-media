import {
  Box, Button, Checkbox, Chip, Collapse, IconButton, Menu, MenuItem, Pagination, Stack, Tab, Table, TableBody, TableCell, TableHead, TableRow,
  Tabs, TextField, Tooltip, Typography,
} from '@mui/material';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CheckCheck, ChevronDown, ChevronRight, MoreVertical } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { get, post } from '../api/client';
import type { IssueSeverity, IssueStatus, IssueTypeSpec, LibraryIssue, Page } from '../api/types';
import { Empty, ErrorAlert, Loading, PageHeader, useConfirm } from '../components/common';
import { useAuth } from '../store/auth';
import { toast } from '../store/ui';
import { dateTime, errorMessage, relative } from '../utils/format';

const ACTION_LABELS: Record<string, string> = {
  dismiss: 'Dismiss', reopen: 'Reopen', rescan: 'Rescan', search: 'Search for a replacement', unmonitor: 'Unmonitor',
  delete_file: 'Move file to recycle bin', match: 'Match in Import', relabel: 'Relabel to actual resolution',
};
const SEVERITY_COLOR: Record<IssueSeverity, 'error' | 'warning' | 'info'> = { error: 'error', warning: 'warning', info: 'info' };
const BULK_ACTIONS = ['dismiss', 'relabel', 'search'];
const STATUSES: IssueStatus[] = ['open', 'dismissed', 'resolved'];
const SEVERITIES: IssueSeverity[] = ['error', 'warning', 'info'];

/** Per-type detail renderers plug in here (03 inspection, 05 duplicates); everything else gets the key/value fallback. */
const DETAIL_RENDERERS: Record<string, (issue: LibraryIssue) => ReactNode> = {
  quality_mismatch: (issue) => {
    const d = issue.data ?? {};
    return (
      <Stack spacing={1.5}>
        <Stack direction="row" spacing={2} alignItems="center">
          <Box>
            <Typography variant="caption" color="text.secondary" display="block">Claimed</Typography>
            <Chip label={String(d.claimed_quality ?? d.claimed_resolution ?? 'Unknown')} color="default" variant="outlined" />
          </Box>
          <Typography variant="body1" color="text.secondary">→</Typography>
          <Box>
            <Typography variant="caption" color="text.secondary" display="block">Actual (ffprobe)</Typography>
            <Chip
              label={`${d.actual_resolution ?? 'Unknown'}${d.actual_width && d.actual_height ? ` (${d.actual_width}×${d.actual_height})` : ''}`}
              color="warning"
            />
          </Box>
        </Stack>
        <Typography variant="body2" color="text.secondary">
          The file's stream dimensions indicate a lower resolution than claimed by the release name. Relabel it to the actual resolution so quality upgrades will trigger properly, or search for a replacement.
        </Typography>
      </Stack>
    );
  },
  runtime_mismatch: (issue) => {
    const d = issue.data ?? {};
    return (
      <Stack spacing={1.5}>
        <Stack direction="row" spacing={2} alignItems="center">
          <Box>
            <Typography variant="caption" color="text.secondary" display="block">Actual duration</Typography>
            <Chip label={`${d.actual_minutes ?? '—'} min`} color="warning" variant="outlined" />
          </Box>
          <Typography variant="body1" color="text.secondary">vs</Typography>
          <Box>
            <Typography variant="caption" color="text.secondary" display="block">Expected runtime</Typography>
            <Chip label={`${d.expected_minutes ?? '—'} min`} color="default" variant="outlined" />
          </Box>
          {d.delta_pct !== undefined && (
            <Chip size="small" label={`${d.delta_pct}% delta`} color="error" variant="outlined" />
          )}
        </Stack>
        <Typography variant="body2" color="text.secondary">
          The file duration differs from the expected metadata runtime. This may indicate a mismatched episode, a sample file, or an extended/director's cut.
        </Typography>
      </Stack>
    );
  },
};

function DetailTable({ data }: { data: Record<string, unknown> }) {
  return (
    <Table size="small">
      <TableBody>
        {Object.entries(data).map(([k, v]) => (
          <TableRow key={k}>
            <TableCell sx={{ width: 160, color: 'text.secondary' }}>{k.replace(/_/g, ' ')}</TableCell>
            <TableCell sx={{ wordBreak: 'break-all' }}>
              {typeof v === 'object' && v !== null ? <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(v, null, 1)}</pre> : String(v)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function availableActions(issue: LibraryIssue, canDelete: boolean): string[] {
  return issue.actions.filter((a) => {
    if (a === 'reopen') return issue.status !== 'open';
    if (a === 'dismiss') return issue.status === 'open';
    if (a === 'delete_file' && !canDelete) return false;
    return issue.status === 'open';
  });
}

function IssueRow({ issue, selected, onSelect, onAction, canWrite, canDelete }: {
  issue: LibraryIssue; selected: boolean; onSelect: () => void; onAction: (issue: LibraryIssue, action: string) => void; canWrite: boolean; canDelete: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const actions = availableActions(issue, canDelete);
  const link = issue.media_id ? (issue.media_type === 'movie' ? `/movies/${issue.media_id}` : `/series/${issue.media_id}`) : null;
  const render = DETAIL_RENDERERS[issue.issue_type];
  return (
    <>
      <TableRow hover>
        <TableCell padding="checkbox">{canWrite && <Checkbox size="small" checked={selected} onChange={onSelect} />}</TableCell>
        <TableCell sx={{ width: 32, px: 0 }}>
          <IconButton size="small" onClick={() => setOpen(!open)}>{open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</IconButton>
        </TableCell>
        <TableCell sx={{ whiteSpace: 'nowrap' }}>
          <Chip size="small" label={issue.severity} color={SEVERITY_COLOR[issue.severity]} variant="outlined" sx={{ mr: 1 }} />
          {issue.label}
        </TableCell>
        <TableCell>
          {link ? <Link to={link} style={{ color: 'inherit' }}>{issue.title ?? `${issue.media_type} #${issue.media_id}`}</Link> : '—'}
          <Typography variant="caption" display="block" color="text.secondary" sx={{ wordBreak: 'break-all' }}>{issue.path}</Typography>
        </TableCell>
        <TableCell>{issue.details}</TableCell>
        <TableCell sx={{ whiteSpace: 'nowrap' }}>
          <Tooltip title={dateTime(issue.first_detected_at)}><span>First seen {relative(issue.first_detected_at)}</span></Tooltip>
          <Typography variant="caption" display="block" color="text.secondary">Last seen {relative(issue.last_seen_at)}</Typography>
        </TableCell>
        <TableCell align="right">
          {canWrite && actions.length > 0 && (
            <>
              <IconButton size="small" onClick={(e) => setAnchor(e.currentTarget)}><MoreVertical size={16} /></IconButton>
              <Menu anchorEl={anchor} open={!!anchor} onClose={() => setAnchor(null)}>
                {actions.map((a) => (
                  <MenuItem key={a} onClick={() => { setAnchor(null); onAction(issue, a); }}>{ACTION_LABELS[a] ?? a}</MenuItem>
                ))}
              </Menu>
            </>
          )}
        </TableCell>
      </TableRow>
      <TableRow>
        <TableCell colSpan={7} sx={{ py: 0, border: open ? undefined : 0 }}>
          <Collapse in={open} unmountOnExit>
            <Box sx={{ py: 1.5 }}>
              {render ? render(issue) : issue.data && Object.keys(issue.data).length ? <DetailTable data={issue.data} /> : (
                <Typography variant="body2" color="text.secondary">No additional evidence recorded.</Typography>
              )}
              {issue.resolution && (
                <Typography variant="caption" color="text.secondary">Closed: {issue.resolution}{issue.resolved_at ? ` · ${dateTime(issue.resolved_at)}` : ''}</Typography>
              )}
            </Box>
          </Collapse>
        </TableCell>
      </TableRow>
    </>
  );
}

export default function IssuesPage() {
  const can = useAuth((s) => s.can);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { confirm, dialog } = useConfirm();
  const [params, setParams] = useSearchParams();
  const status = STATUSES.find((s) => s === params.get('status')) ?? 'open';
  const severity = SEVERITIES.find((s) => s === params.get('severity')) ?? '';
  const type = params.get('type') ?? '';
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Map<number, LibraryIssue>>(new Map());
  // Filters live in the URL so the dashboard can link to e.g. ?type=missing_file. However they change (a control,
  // Back, the nav link), start over on page 1 with nothing selected, so a bulk action never hits rows that are out of view.
  const filters = `${status}|${severity}|${type}`;
  const [shownFilters, setShownFilters] = useState(filters);
  if (filters !== shownFilters) {
    setShownFilters(filters);
    setPage(1);
    setSelected(new Map());
  }
  const setFilter = (key: 'status' | 'severity' | 'type', value: string) => setParams((prev) => {
    const next = new URLSearchParams(prev);
    if (value) next.set(key, value); else next.delete(key);
    return next;
  });

  const types = useQuery({ queryKey: ['issue-types'], queryFn: () => get<IssueTypeSpec[]>('/library/issue-types'), staleTime: Infinity });
  const issues = useQuery({
    queryKey: ['issues', status, severity, type, page],
    placeholderData: keepPreviousData,
    queryFn: () => get<Page<LibraryIssue>>('/library/issues', {
      status, severity: severity || undefined, issue_type: type || undefined, page, page_size: 50, sort: 'detected', order: 'desc',
    }),
  });
  const refresh = () => {
    setSelected(new Map());
    qc.invalidateQueries({ queryKey: ['issues'] });
    qc.invalidateQueries({ queryKey: ['dashboard'] });
  };
  const run = useMutation({
    mutationFn: ({ issue, action }: { issue: LibraryIssue; action: string }) => post<{ message: string }>(`/library/issues/${issue.id}/resolve`, { action }),
    onSuccess: (r) => { toast(r.message, 'success'); refresh(); },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const bulk = useMutation({
    mutationFn: (action: string) => post<{ processed: number; failed: unknown[] }>('/library/issues/bulk', { ids: [...selected.keys()], action }),
    onSuccess: (r) => {
      toast(`${r.processed} issue(s) updated${r.failed.length ? `, ${r.failed.length} failed` : ''}`, r.failed.length ? 'warning' : 'success');
      refresh();
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const onAction = async (issue: LibraryIssue, action: string) => {
    if (action === 'match') { navigate(issue.path ? `/import?folder=${encodeURIComponent(issue.path)}` : '/import'); return; }
    if (action === 'delete_file') {
      const ok = await confirm('Move file to recycle bin?', <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>{issue.path}</Typography>,
        { confirmLabel: 'Recycle', danger: true });
      if (!ok) return;
    }
    run.mutate({ issue, action });
  };

  const items = issues.data?.items ?? [];
  const canWrite = can('media:write');
  const allSelected = items.length > 0 && items.every((i) => selected.has(i.id));
  const toggle = (issue: LibraryIssue) => setSelected((s) => {
    const n = new Map(s);
    if (n.has(issue.id)) n.delete(issue.id); else n.set(issue.id, issue);
    return n;
  });
  // only what every selected issue allows, so e.g. "Relabel" isn't offered for missing files
  const bulkActions = BULK_ACTIONS.filter((a) => [...selected.values()].every((i) => i.actions.includes(a)));
  return (
    <>
      <PageHeader title="Library issues" icon={<AlertTriangle />} subtitle="Missing, duplicate, corrupt and upgradeable files found by library scans" />
      <Tabs value={status} onChange={(_, v) => setFilter('status', v === 'open' ? '' : v)} sx={{ mb: 2 }}>
        <Tab value="open" label="Open" />
        <Tab value="dismissed" label="Dismissed" />
        <Tab value="resolved" label="Resolved" />
      </Tabs>
      <Stack direction="row" spacing={2} sx={{ mb: 2 }} alignItems="center" flexWrap="wrap" useFlexGap>
        <TextField select size="small" label="Type" value={type} onChange={(e) => setFilter('type', e.target.value)} sx={{ minWidth: 200 }}>
          <MenuItem value="">All types</MenuItem>
          {(types.data ?? []).map((t) => <MenuItem key={t.type} value={t.type}>{t.label}</MenuItem>)}
          {type && !types.data?.some((t) => t.type === type) && <MenuItem value={type}>{type.replace(/_/g, ' ')}</MenuItem>}
        </TextField>
        <TextField select size="small" label="Severity" value={severity} onChange={(e) => setFilter('severity', e.target.value)} sx={{ minWidth: 140 }}>
          <MenuItem value="">All</MenuItem>
          {SEVERITIES.map((s) => <MenuItem key={s} value={s}>{s}</MenuItem>)}
        </TextField>
        {selected.size > 0 && canWrite && (
          <Stack direction="row" spacing={1} alignItems="center">
            <Typography variant="body2">{selected.size} selected</Typography>
            {status === 'open' ? bulkActions.map((a) => (
              <Button key={a} size="small" variant="outlined" disabled={bulk.isPending} onClick={() => bulk.mutate(a)}>{ACTION_LABELS[a]}</Button>
            )) : <Button size="small" variant="outlined" disabled={bulk.isPending} onClick={() => bulk.mutate('reopen')}>Reopen</Button>}
          </Stack>
        )}
      </Stack>
      <ErrorAlert error={issues.error} />
      {issues.isLoading ? <Loading /> : !items.length ? (
        <Empty icon={<CheckCheck size={40} />} title="No issues">Nothing {status} matches these filters.</Empty>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell padding="checkbox">
                {canWrite && <Checkbox size="small" checked={allSelected} onChange={() => setSelected(allSelected ? new Map() : new Map(items.map((i) => [i.id, i])))} />}
              </TableCell>
              <TableCell />
              <TableCell>Issue</TableCell>
              <TableCell>Item</TableCell>
              <TableCell>Details</TableCell>
              <TableCell>Detected</TableCell>
              <TableCell />
            </TableRow>
          </TableHead>
          <TableBody>
            {items.map((i) => (
              <IssueRow key={i.id} issue={i} canWrite={canWrite} canDelete={can('media:delete')} selected={selected.has(i.id)}
                onSelect={() => toggle(i)} onAction={onAction} />
            ))}
          </TableBody>
        </Table>
      )}
      {issues.data && issues.data.pages > 1 && (
        <Stack alignItems="center" sx={{ mt: 2 }}><Pagination count={issues.data.pages} page={page} onChange={(_, p) => setPage(p)} /></Stack>
      )}
      {dialog}
    </>
  );
}
