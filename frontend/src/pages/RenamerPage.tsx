import { Alert, Button, Checkbox, FormControlLabel, Stack, Table, TableBody, TableCell, TableHead, TableRow, ToggleButton, ToggleButtonGroup, Typography } from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, CheckCircle2, FileText, FlaskConical } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { get, post } from '../api/client';
import { Empty, ErrorAlert, Loading, PageHeader, QualityChip } from '../components/common';
import { useAuth } from '../store/auth';
import { toast } from '../store/ui';
import { palette } from '../theme';
import { errorMessage } from '../utils/format';

interface Preview {
  id: string; media_type: 'movie' | 'tv'; media_id: number; title: string; current_path: string; proposed_path: string; quality: string;
  folder_change: boolean; current_folder?: string; proposed_folder?: string;
}

const tail = (p: string, n = 2) => p.split(/[\\/]/).slice(-n).join('/');

export default function RenamerPage() {
  const [params] = useSearchParams();
  const can = useAuth((s) => s.can);
  const qc = useQueryClient();
  const [mediaType, setMediaType] = useState(params.get('media_type') ?? 'all');
  const mediaId = params.get('media_id') ?? undefined;
  const [folders, setFolders] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [plan, setPlan] = useState<{ from: string; to: string }[] | null>(null);

  const preview = useQuery({
    queryKey: ['rename', mediaType, folders, mediaId],
    queryFn: () => get<{ items: Preview[]; errors: string[]; truncated: boolean }>('/rename/preview', { media_type: mediaType, include_folders: folders, media_id: mediaId }),
  });
  useEffect(() => setSelected(new Set(preview.data?.items.map((i) => i.id) ?? [])), [preview.data]);
  const items = useMemo(() => preview.data?.items ?? [], [preview.data]);
  const chosen = items.filter((i) => selected.has(i.id));

  const execute = useMutation({
    mutationFn: (dry: boolean) => post<{ renamed?: number; errors?: string[]; planned?: { from: string; to: string }[]; job_id?: number; queued?: number }>(
      '/rename', { items: chosen.map((i) => ({ media_type: i.media_type, media_id: i.media_id, title: i.title })), dry_run: dry, include_folders: folders }),
    onSuccess: (r, dry) => {
      if (dry) { setPlan(r.planned ?? []); return; }
      if (r.job_id) toast(`Renaming ${r.queued} items in the background`, 'info');
      else toast(`Renamed ${r.renamed} file(s)${r.errors?.length ? `, ${r.errors.length} error(s)` : ''}`, r.errors?.length ? 'warning' : 'success');
      r.errors?.slice(0, 5).forEach((e) => toast(e, 'error'));
      setPlan(null);
      qc.invalidateQueries({ queryKey: ['rename'] });
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const toggle = (id: string) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  return (
    <>
      <PageHeader title="Renamer" icon={<FileText color={palette.purple} />} subtitle="Preview, dry-run and bulk-apply your naming templates"
        actions={can('files:rename') && (
          <>
            <Button variant="outlined" startIcon={<FlaskConical size={16} />} disabled={!chosen.length || execute.isPending} onClick={() => execute.mutate(true)}>Dry run</Button>
            <Button variant="contained" startIcon={<CheckCircle2 size={16} />} disabled={!chosen.length || execute.isPending} onClick={() => execute.mutate(false)}>
              Rename {chosen.length}
            </Button>
          </>
        )} />
      <Stack direction="row" spacing={2} sx={{ mb: 2 }} alignItems="center">
        <ToggleButtonGroup size="small" exclusive value={mediaType} onChange={(_, v) => v && setMediaType(v)}>
          <ToggleButton value="all">All</ToggleButton>
          <ToggleButton value="movie">Movies</ToggleButton>
          <ToggleButton value="tv">TV</ToggleButton>
        </ToggleButtonGroup>
        <FormControlLabel control={<Checkbox checked={folders} onChange={(e) => setFolders(e.target.checked)} />} label="Also rename movie / series folders" />
      </Stack>
      {preview.data?.errors.map((e) => <Alert key={e} severity="error" sx={{ mb: 1 }}>Template error: {e}</Alert>)}
      {preview.data?.truncated && <Alert severity="info" sx={{ mb: 1 }}>Showing the first 5,000 proposals.</Alert>}
      {plan && (
        <Alert severity="success" sx={{ mb: 2 }} onClose={() => setPlan(null)}>
          Dry run: {plan.length} file(s) would be renamed. Nothing was changed on disk.
        </Alert>
      )}
      <ErrorAlert error={preview.error} />
      {preview.isLoading ? <Loading label="Computing proposals…" /> : !items.length ? (
        <Empty icon={<CheckCircle2 size={40} />} title="Everything already matches your templates" />
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell padding="checkbox">
                <Checkbox checked={selected.size === items.length} indeterminate={selected.size > 0 && selected.size < items.length}
                  onChange={(e) => setSelected(e.target.checked ? new Set(items.map((i) => i.id)) : new Set())} />
              </TableCell>
              <TableCell>Item</TableCell>
              <TableCell>Current</TableCell>
              <TableCell />
              <TableCell>Proposed</TableCell>
              <TableCell>Quality</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {items.map((i) => (
              <TableRow key={i.id} hover onClick={() => toggle(i.id)} sx={{ cursor: 'pointer' }}>
                <TableCell padding="checkbox"><Checkbox checked={selected.has(i.id)} /></TableCell>
                <TableCell sx={{ fontWeight: 600 }}>{i.title}</TableCell>
                <TableCell><Typography variant="caption" sx={{ fontFamily: "'JetBrains Mono', monospace", color: palette.red, wordBreak: 'break-all' }}>{tail(i.current_path, i.folder_change ? 2 : 1)}</Typography></TableCell>
                <TableCell><ArrowRight size={14} color={palette.cyan} /></TableCell>
                <TableCell><Typography variant="caption" sx={{ fontFamily: "'JetBrains Mono', monospace", color: palette.cyan, wordBreak: 'break-all' }}>{tail(i.proposed_path, i.media_type === 'tv' || i.folder_change ? 2 : 1)}</Typography></TableCell>
                <TableCell><QualityChip quality={i.quality} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </>
  );
}
