import {
  Alert,
  Autocomplete,
  Button,
  Checkbox,
  Chip,
  MenuItem,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  Typography,
} from '@mui/material';
import { useMutation, useQuery } from '@tanstack/react-query';
import { FolderInput, FolderSearch } from 'lucide-react';
import { useEffect, useState } from 'react';
import { get, post } from '../api/client';
import type { MovieSummary, Page, SeriesSummary } from '../api/types';
import { Empty, ErrorAlert, Loading, PageHeader, QualityChip, Section, StatusChip } from '../components/common';
import { toast } from '../store/ui';
import { palette } from '../theme';
import { bytes, errorMessage } from '../utils/format';

interface Candidate {
  path: string; size: number; state: string; reason: string | null; quality: string;
  media_type: 'movie' | 'tv' | null; media_id: number | null; title: string | null; season: number | null; episodes: number[];
}

export default function ImportPage() {
  const [tab, setTab] = useState<'manual' | 'watch'>('manual');
  return (
    <>
      <PageHeader title="Import" icon={<FolderInput color={palette.purple} />} subtitle="Manually import files and inspect the download watch folder" />
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab value="manual" label="Manual import" />
        <Tab value="watch" label="Download folder monitor" />
      </Tabs>
      {tab === 'manual' ? <ManualImport /> : <WatchFolder />}
    </>
  );
}

function ManualImport() {
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => get<{ download_folder: string; import_mode: string }>('/settings') });
  const [folder, setFolder] = useState('');
  const [rows, setRows] = useState<(Candidate & { selected: boolean })[]>([]);
  const [mode, setMode] = useState('');
  useEffect(() => {
    if (settings.data && !folder) {
      setFolder(settings.data.download_folder);
      setMode(settings.data.import_mode);
    }
  }, [settings.data, folder]);

  const scan = useMutation({
    mutationFn: () => post<Candidate[]>('/library/manual-import/scan', { folder }),
    onSuccess: (data) => setRows(data.map((c) => ({ ...c, selected: c.state === 'ok' && !!c.media_id }))),
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const run = useMutation({
    mutationFn: () => post<{ imported: string[]; failed: string[] }>('/library/manual-import', {
      mode, items: rows.filter((r) => r.selected && r.media_id).map((r) => ({ path: r.path, media_type: r.media_type, media_id: r.media_id, season: r.season, episodes: r.episodes, quality: r.quality })),
    }),
    onSuccess: (r) => {
      toast(`Imported ${r.imported.length} file(s)${r.failed.length ? `, ${r.failed.length} failed` : ''}`, r.failed.length ? 'warning' : 'success');
      r.failed.forEach((f) => toast(f, 'error'));
      scan.mutate();
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const update = (i: number, patch: Partial<Candidate & { selected: boolean }>) => setRows((rs) => rs.map((r, j) => (i === j ? { ...r, ...patch } : r)));
  const selected = rows.filter((r) => r.selected && r.media_id).length;

  return (
    <>
      <Section sx={{ mb: 2 }}>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={2}>
          <TextField fullWidth size="small" label="Folder" value={folder} onChange={(e) => setFolder(e.target.value)} />
          <TextField select size="small" label="Mode" value={mode} onChange={(e) => setMode(e.target.value)} sx={{ minWidth: 150 }}>
            {['move', 'copy', 'hardlink', 'symlink'].map((m) => <MenuItem key={m} value={m}>{m}</MenuItem>)}
          </TextField>
          <Button variant="outlined" startIcon={<FolderSearch size={16} />} onClick={() => scan.mutate()} disabled={!folder || scan.isPending}>Scan</Button>
          <Button variant="contained" disabled={!selected || run.isPending} onClick={() => run.mutate()}>Import {selected || ''}</Button>
        </Stack>
      </Section>
      {scan.isPending ? <Loading label="Inspecting files…" /> : rows.length === 0 ? (
        <Empty icon={<FolderSearch size={40} />} title="Scan a folder">Files are validated (partial / corrupt / sample detection), parsed and matched to your library before you import.</Empty>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow><TableCell padding="checkbox" /><TableCell>File</TableCell><TableCell>State</TableCell><TableCell>Quality</TableCell><TableCell sx={{ minWidth: 280 }}>Match</TableCell><TableCell>Episode(s)</TableCell></TableRow>
          </TableHead>
          <TableBody>
            {rows.map((r, i) => (
              <TableRow key={r.path}>
                <TableCell padding="checkbox"><Checkbox checked={r.selected} disabled={r.state !== 'ok'} onChange={(e) => update(i, { selected: e.target.checked })} /></TableCell>
                <TableCell sx={{ maxWidth: 380 }}>
                  <Typography variant="caption" sx={{ fontFamily: "'JetBrains Mono', monospace", wordBreak: 'break-all' }}>{r.path}</Typography>
                  <Typography variant="caption" display="block" color="text.secondary">{bytes(r.size)}</Typography>
                </TableCell>
                <TableCell><StatusChip status={r.state === 'ok' ? 'ok' : r.state === 'partial' ? 'downloading' : 'error'} label={r.state} />{r.reason && <Typography variant="caption" display="block" color="text.secondary">{r.reason}</Typography>}</TableCell>
                <TableCell><QualityChip quality={r.quality} /></TableCell>
                <TableCell><MatchPicker row={r} onChange={(p) => update(i, { ...p, selected: r.state === 'ok' })} /></TableCell>
                <TableCell>
                  {r.media_type === 'tv' && (
                    <Stack direction="row" spacing={1}>
                      <TextField size="small" type="number" label="S" value={r.season ?? ''} sx={{ width: 70 }} onChange={(e) => update(i, { season: Number(e.target.value) })} />
                      <TextField size="small" label="E" value={r.episodes.join(',')} sx={{ width: 90 }}
                        onChange={(e) => update(i, { episodes: e.target.value.split(',').map((x) => Number(x.trim())).filter(Boolean) })} />
                    </Stack>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </>
  );
}

function MatchPicker({ row, onChange }: { row: Candidate; onChange: (p: Partial<Candidate>) => void }) {
  const [input, setInput] = useState('');
  const [type, setType] = useState<'movie' | 'tv'>(row.media_type ?? 'movie');
  const options = useQuery({
    queryKey: ['match-options', type, input],
    enabled: input.length > 1,
    queryFn: () => get<Page<MovieSummary | SeriesSummary>>(type === 'movie' ? '/movies' : '/series', { search: input, page_size: 15 }),
  });
  return (
    <Stack direction="row" spacing={1}>
      <TextField select size="small" value={type} onChange={(e) => setType(e.target.value as 'movie' | 'tv')} sx={{ width: 90 }}>
        <MenuItem value="movie">Movie</MenuItem>
        <MenuItem value="tv">TV</MenuItem>
      </TextField>
      <Autocomplete size="small" sx={{ flex: 1 }} options={options.data?.items ?? []} filterOptions={(x) => x}
        getOptionLabel={(o) => (typeof o === 'string' ? o : `${o.title} (${o.year})`)}
        value={null} inputValue={input || row.title || ''} onInputChange={(_, v, reason) => reason === 'input' && setInput(v)}
        onChange={(_, v) => v && typeof v !== 'string' && onChange({ media_type: type, media_id: v.id, title: `${v.title} (${v.year})` })}
        renderInput={(p) => <TextField {...p} placeholder="Search library…" />}
        noOptionsText={input.length > 1 ? 'No matches' : 'Type to search'} />
    </Stack>
  );
}

function WatchFolder() {
  const report = useQuery({
    queryKey: ['download-folder'],
    queryFn: () => get<{ completed: number; partial: number; corrupt: number; imported: number; untracked: Candidate[] }>('/library/download-folder'),
  });
  if (report.isLoading) return <Loading label="Inspecting download folder…" />;
  if (!report.data) return <ErrorAlert error={report.error} />;
  const r = report.data;
  return (
    <>
      <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
        <Chip color="success" label={`${r.completed} completed`} />
        <Chip color="secondary" label={`${r.partial} in progress`} />
        <Chip color="error" label={`${r.corrupt} corrupt/failed`} />
        <Chip label={`${r.imported} auto-imported`} />
      </Stack>
      <Alert severity="info" sx={{ mb: 2 }}>
        Downloads grabbed by Maniac Media import automatically. Files placed in the folder by hand are only listed here unless
        “Import untracked downloads” is enabled in Settings → Media management. Use Manual import to bring them in.
      </Alert>
      <Table size="small">
        <TableHead><TableRow><TableCell>Untracked file</TableCell><TableCell>Quality</TableCell><TableCell>Library match</TableCell><TableCell>Size</TableCell></TableRow></TableHead>
        <TableBody>
          {r.untracked.map((c) => (
            <TableRow key={c.path}>
              <TableCell sx={{ fontFamily: "'JetBrains Mono', monospace", fontSize: '0.75rem', wordBreak: 'break-all' }}>{c.path}</TableCell>
              <TableCell><QualityChip quality={c.quality} /></TableCell>
              <TableCell>{c.title ? <Chip size="small" color="success" variant="outlined" label={c.title + (c.season ? ` S${c.season}E${c.episodes.join(',')}` : '')} /> : <Chip size="small" label="No match" />}</TableCell>
              <TableCell>{bytes(c.size)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </>
  );
}
