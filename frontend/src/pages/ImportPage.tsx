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
import { FolderInput, FolderSearch, RefreshCw, Trash2, Sparkles } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { get, post } from '../api/client';
import type { MovieSummary, Page, SeriesSummary } from '../api/types';
import { Empty, ErrorAlert, Loading, PageHeader, QualityChip, Section, StatusChip } from '../components/common';
import { toast } from '../store/ui';
import { palette } from '../theme';
import { bytes, errorMessage } from '../utils/format';

interface Candidate {
  path: string; size: number; state: string; reason: string | null; quality: string;
  media_type: 'movie' | 'tv' | null; media_id: number | null; title: string | null; season: number | null; episodes: number[];
  library_state: 'new' | 'upgrade' | 'duplicate' | null; existing_quality: string | null; existing_size: number | null;
}

type Row = Candidate & { selected: boolean };

function isImportable(r: Row): boolean {
  return r.state === 'ok' && !!r.media_id && r.library_state !== 'duplicate';
}

function isDeletable(r: Row): boolean {
  return r.state === 'corrupt' || r.library_state === 'duplicate';
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
  const [params] = useSearchParams();
  const deepLinkFolder = params.get('folder');
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => get<{ download_folder: string; import_mode: string }>('/settings') });
  const [folder, setFolder] = useState(deepLinkFolder ?? '');
  const [rows, setRows] = useState<Row[]>([]);
  const [mode, setMode] = useState('');
  useEffect(() => {
    if (settings.data && !mode) {
      setMode(settings.data.import_mode);
      if (!deepLinkFolder && !folder) setFolder(settings.data.download_folder);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.data]);

  const [failReasons, setFailReasons] = useState<Record<string, string>>({});
  const scan = useMutation({
    mutationFn: () => post<Candidate[]>('/library/manual-import/scan', { folder }),
    onSuccess: (data) => {
      setRows(data.map((c) => ({ ...c, selected: c.state === 'ok' && !!c.media_id && c.library_state !== 'duplicate' && c.library_state !== 'upgrade' })));
      // drop stale failure reasons for files that are no longer in the folder (imported, deleted, moved)
      setFailReasons((fr) => Object.fromEntries(Object.entries(fr).filter(([p]) => data.some((d) => d.path === p))));
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  useEffect(() => {
    // arrived via a MANUAL_IMPORT fix deep link ("open in manual import") – scan right away
    if (deepLinkFolder) scan.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const smartMatch = useMutation({
    mutationFn: async () => {
      const selectedIndices = rows.map((r, i) => r.selected ? i : -1).filter(i => i !== -1);
      const newRows = [...rows];
      
      for (const i of selectedIndices) {
        const r = newRows[i];
        let query = r.path.split(/[/\\]/).pop() || r.path;
        query = query.replace(/\.[^/.]+$/, '');
        query = query.replace(/\b(1080p|720p|2160p|4k|x264|x265|hevc|web-dl|bluray|remux)\b/gi, '');
        query = query.replace(/\b(19\d{2}|20\d{2})\b/g, '');
        query = query.replace(/[([].*?[)\]]/g, '');
        query = query.replace(/\bS\d{2}E\d{2}\b/gi, '');
        query = query.replace(/\bS\d{2}\b/gi, '');
        query = query.replace(/\bE\d{2}\b/gi, '');
        query = query.replace(/[._]/g, ' ').replace(/\s+/g, ' ').trim();

        if (!query) continue;

        try {
          const localMovies = await get<Page<MovieSummary>>('/movies', { search: query, page_size: 1 });
          if (localMovies.items.length > 0) {
            newRows[i] = { ...r, media_type: 'movie', media_id: localMovies.items[0].id, title: localMovies.items[0].title };
            continue;
          }

          const localTv = await get<Page<SeriesSummary>>('/series', { search: query, page_size: 1 });
          if (localTv.items.length > 0) {
            newRows[i] = { ...r, media_type: 'tv', media_id: localTv.items[0].id, title: localTv.items[0].title };
            continue;
          }

          const tmdbMovies = await get<any[]>('/movies/lookup', { query });
          if (tmdbMovies.length > 0) {
            const match = tmdbMovies[0];
            try {
              const added = await post<any>('/movies', { title: match.title, year: match.year, tmdb_id: match.tmdb_id });
              newRows[i] = { ...r, media_type: 'movie', media_id: added.id, title: added.title };
            } catch (e: any) {
              console.warn("Failed to add movie", e);
            }
            continue;
          }

          const tmdbTv = await get<any[]>('/series/lookup', { query });
          if (tmdbTv.length > 0) {
            const match = tmdbTv[0];
            try {
              // Note: POST /series might return a full object or just id depending on the backend, but based on code it returns SeriesOut
              const added = await post<any>('/series', { title: match.title, year: match.year, tmdb_id: match.tmdb_id });
              newRows[i] = { ...r, media_type: 'tv', media_id: added.id || added, title: match.title };
            } catch (e: any) {
              console.warn("Failed to add series", e);
            }
            continue;
          }
        } catch (e) {
          console.error("Smart match error for", r.path, e);
        }
      }
      return newRows;
    },
    onSuccess: (newRows) => {
      setRows(newRows);
      toast('Smart match completed', 'success');
    }
  });

  const run = useMutation({
    mutationFn: (explicit?: Row[]) => post<{ imported: string[]; failed: string[]; failed_paths: Record<string, string> }>('/library/manual-import', {
      mode, items: (explicit ?? rows.filter((r) => r.selected && isImportable(r)))
        .map((r) => ({ path: r.path, media_type: r.media_type, media_id: r.media_id, season: r.season, episodes: r.episodes, quality: r.quality })),
    }),
    onSuccess: (r) => {
      toast(`Imported ${r.imported.length} file(s)${r.failed.length ? `, ${r.failed.length} failed` : ''}`, r.failed.length ? 'warning' : 'success');
      setFailReasons((fr) => ({ ...fr, ...r.failed_paths }));
      scan.mutate();
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const match = useMutation({
    mutationFn: (r: Row) => post<Candidate>('/library/manual-import/match', { path: r.path, media_type: r.media_type, media_id: r.media_id, season: r.season, episodes: r.episodes }),
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const del = useMutation({
    mutationFn: (paths: string[]) => post<{ deleted: string[]; failed: string[] }>('/library/manual-import/delete', { paths }),
    onSuccess: (r) => {
      toast(`Deleted ${r.deleted.length} file(s)${r.failed.length ? `, ${r.failed.length} failed` : ''}`, r.failed.length ? 'warning' : 'success');
      r.failed.forEach((f) => toast(f, 'error'));
      scan.mutate();
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const update = (i: number, patch: Partial<Row>) => setRows((rs) => rs.map((r, j) => (i === j ? { ...r, ...patch } : r)));
  const importSelected = rows.filter((r) => r.selected && isImportable(r)).length;
  const deleteSelected = rows.filter((r) => r.selected && isDeletable(r)).length;
  const corruptCount = rows.filter((r) => r.state === 'corrupt').length;

  return (
    <>
      <Section sx={{ mb: 2 }}>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={2}>
          <TextField fullWidth size="small" label="Folder" value={folder} onChange={(e) => setFolder(e.target.value)} />
          <TextField select size="small" label="Mode" value={mode} onChange={(e) => setMode(e.target.value)} sx={{ minWidth: 150 }}>
            {['move', 'copy', 'hardlink', 'symlink'].map((m) => <MenuItem key={m} value={m}>{m}</MenuItem>)}
          </TextField>
          <Button variant="outlined" startIcon={<FolderSearch size={16} />} onClick={() => scan.mutate()} disabled={!folder || scan.isPending}>Scan</Button>
          <Button variant="contained" color="secondary" startIcon={<Sparkles size={16} />} onClick={() => smartMatch.mutate()} disabled={!rows.some(r => r.selected) || smartMatch.isPending}>Smart Match</Button>
          <Button variant="contained" disabled={!importSelected || run.isPending} onClick={() => run.mutate(undefined)}>Import {importSelected || ''}</Button>
          <Button variant="outlined" color="error" disabled={!deleteSelected || del.isPending}
            onClick={() => del.mutate(rows.filter((r) => r.selected && isDeletable(r)).map((r) => r.path))}>Delete {deleteSelected || ''}</Button>
          <Button variant="text" color="error" disabled={!corruptCount || del.isPending}
            onClick={() => del.mutate(rows.filter((r) => r.state === 'corrupt').map((r) => r.path))}>Delete all corrupt ({corruptCount})</Button>
        </Stack>
      </Section>
      {scan.isPending ? <Loading label="Inspecting files…" /> : rows.length === 0 ? (
        <Empty icon={<FolderSearch size={40} />} title="Scan a folder">Files are validated (partial / corrupt / sample detection), parsed and matched to your library before you import.</Empty>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell padding="checkbox" /><TableCell>File</TableCell><TableCell>State</TableCell><TableCell>Quality</TableCell>
              <TableCell sx={{ minWidth: 280 }}>Match</TableCell><TableCell>Episode(s)</TableCell><TableCell>Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((r, i) => {
              const statusLabel = r.state !== 'ok' ? r.state : r.library_state === 'duplicate' ? 'duplicate' : r.library_state === 'upgrade' ? 'upgrade' : 'ok';
              const failReason = failReasons[r.path];
              return (
                <TableRow key={r.path}>
                  <TableCell padding="checkbox">
                    <Checkbox checked={r.selected} disabled={!isImportable(r) && !isDeletable(r)} onChange={(e) => update(i, { selected: e.target.checked })} />
                  </TableCell>
                  <TableCell sx={{ maxWidth: 380 }}>
                    <Typography variant="caption" sx={{ fontFamily: "'JetBrains Mono', monospace", wordBreak: 'break-all' }}>{r.path}</Typography>
                    <Typography variant="caption" display="block" color="text.secondary">{bytes(r.size)}</Typography>
                  </TableCell>
                  <TableCell>
                    <StatusChip status={r.state !== 'ok' ? (r.state === 'partial' ? 'downloading' : 'error') : statusLabel} label={statusLabel} />
                    {r.state === 'ok' && r.library_state === 'duplicate' && (
                      <Typography variant="caption" display="block" color="text.secondary">existing: {r.existing_quality} ({bytes(r.existing_size)}) — equal or better, not an upgrade</Typography>
                    )}
                    {r.state === 'ok' && r.library_state === 'upgrade' && (
                      <Typography variant="caption" display="block" color="text.secondary">upgrade from {r.existing_quality} ({bytes(r.existing_size)})</Typography>
                    )}
                    {r.reason && <Typography variant="caption" display="block" color="text.secondary">{r.reason}</Typography>}
                    {failReason && (
                      <Typography variant="caption" display="block" color="error.main">Import failed: {failReason}</Typography>
                    )}
                  </TableCell>
                  <TableCell><QualityChip quality={r.quality} /></TableCell>
                  <TableCell>
                    <MatchPicker row={r} onChange={(p) => update(i, { ...p, selected: r.state === 'ok', library_state: null, existing_quality: null, existing_size: null })} />
                  </TableCell>
                  <TableCell>
                    {r.media_type === 'tv' && (
                      <Stack direction="row" spacing={1}>
                        <TextField size="small" type="number" label="S" value={r.season ?? ''} sx={{ width: 70 }}
                          onChange={(e) => update(i, { season: Number(e.target.value), library_state: null, existing_quality: null, existing_size: null })} />
                        <TextField size="small" label="E" value={r.episodes.join(',')} sx={{ width: 90 }}
                          onChange={(e) => update(i, {
                            episodes: e.target.value.split(',').map((x) => Number(x.trim())).filter(Boolean),
                            library_state: null, existing_quality: null, existing_size: null,
                          })} />
                      </Stack>
                    )}
                  </TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={0.5}>
                      {r.state === 'ok' && r.media_id && (
                        <Button size="small" disabled={match.isPending} onClick={() => match.mutate(r, { onSuccess: (c) => update(i, c) })}>
                          {r.library_state ? 'Re-check' : 'Match'}
                        </Button>
                      )}
                      {(r.state === 'corrupt' || r.library_state === 'duplicate') && (
                        <Button size="small" color="error" startIcon={<Trash2 size={14} />} onClick={() => del.mutate([r.path])}>Delete</Button>
                      )}
                      {r.library_state === 'upgrade' && (
                        <Button size="small" color="success" startIcon={<RefreshCw size={14} />} onClick={() => run.mutate([r])}>Replace</Button>
                      )}
                    </Stack>
                  </TableCell>
                </TableRow>
              );
            })}
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
