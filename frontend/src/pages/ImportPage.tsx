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
import { useEffect, useRef, useState } from 'react';
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

interface Suggestion {
  source: 'library' | 'tmdb'; media_type: 'movie' | 'tv'; media_id: number | null; tmdb_id: number | null;
  title: string; year: number | null; overview: string | null; score: number;
}
interface Identified { path: string; query: string; year: number | null; kind: string; season: number | null; episodes: number[]; candidates: Suggestion[] }
/** A TMDb hit that isn't in the library yet; it is added when the import runs. */
interface ImportResult { imported: string[]; failed: string[]; failed_paths: Record<string, string>; failed_codes: Record<string, string> }
interface PendingAdd { title: string; year: number | null; tmdb_id: number }

type Row = Candidate & { selected: boolean; query?: string; suggestions?: Suggestion[]; pending?: PendingAdd | null; haveIt?: boolean };

const suggestionKey = (s: Suggestion) => `${s.media_type}:${s.media_id ?? `t${s.tmdb_id}`}`;
const suggestionLabel = (s: Suggestion) => `${s.title}${s.year ? ` (${s.year})` : ''}`;
const rowKey = (r: Row) => `${r.media_type}:${r.media_id ?? `t${r.pending?.tmdb_id}`}`;

function isImportable(r: Row): boolean {
  return r.state === 'ok' && (!!r.media_id || !!r.pending) && r.library_state !== 'duplicate';
}

/** Import rejected it because the library already has an equal-or-better copy (or the identical file). */
const ALREADY_HAVE = ['duplicate', 'not_an_upgrade'];

function isDeletable(r: Row): boolean {
  // corrupt files, duplicates, extras/samples (never importable) and anything the library already has
  return r.state === 'corrupt' || r.state === 'sample' || r.library_state === 'duplicate' || !!r.haveIt;
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
  const haveIt = useRef(new Set<string>());   // paths the library already has; survives the post-import rescan
  const scan = useMutation({
    mutationFn: () => post<Candidate[]>('/library/manual-import/scan', { folder }),
    onSuccess: (data) => {
      setRows(data.map((c) => ({
        ...c, haveIt: haveIt.current.has(c.path),
        selected: c.state === 'ok' && !!c.media_id && c.library_state !== 'duplicate' && c.library_state !== 'upgrade' && !haveIt.current.has(c.path),
      })));
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
  const matchApi = (r: Pick<Row, 'path' | 'media_type' | 'media_id' | 'season' | 'episodes'>) =>
    post<Candidate>('/library/manual-import/match', { path: r.path, media_type: r.media_type, media_id: r.media_id, season: r.season, episodes: r.episodes });
  /** Point a row at a suggestion; TMDb-only ones are queued for adding when the import runs. */
  const applyPatch = (r: Row, sug: Suggestion): Row => ({
    ...r, media_type: sug.media_type, media_id: sug.media_id, title: suggestionLabel(sug),
    pending: sug.media_id ? null : { title: sug.title, year: sug.year, tmdb_id: sug.tmdb_id! },
    library_state: null, existing_quality: null, existing_size: null,
  });
  const smartMatch = useMutation({
    mutationFn: async () => {
      const picked = rows.filter((r) => r.selected);
      const found = await post<Identified[]>('/library/manual-import/identify', { paths: picked.map((r) => r.path) });
      const byPath = new Map(found.map((f) => [f.path, f]));
      const next = await Promise.all(rows.map(async (r) => {
        const f = r.selected ? byPath.get(r.path) : undefined;
        if (!f) return r;
        let row: Row = { ...r, query: f.query, suggestions: f.candidates };
        const top = f.candidates[0];
        if (!top) return row;
        row = applyPatch(row, top);
        if (top.media_type === 'tv') {
          row = { ...row, season: row.season ?? f.season, episodes: row.episodes.length ? row.episodes : f.episodes };
        }
        if (top.media_id) {
          try { row = { ...row, ...(await matchApi(row)) }; } catch { /* keep the unannotated row; the Match button retries */ }
        }
        return row;
      }));
      return { next, matched: next.filter((r) => r.selected && (r.media_id || r.pending)).length, total: picked.length };
    },
    onSuccess: ({ next, matched, total }) => {
      setRows(next);
      toast(`Matched ${matched} of ${total} file(s)`, matched === total ? 'success' : 'warning');
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const run = useMutation({
    mutationFn: async (explicit?: Row[]) => {
      const list = explicit ?? rows.filter((r) => r.selected && isImportable(r));
      const created = new Map<string, Promise<number>>();   // one add per TMDb id, shared by every episode of a series
      const addFailed: Record<string, string> = {};
      const items = [];
      for (const r of list) {
        let id = r.media_id;
        if (!id && r.pending) {
          const key = `${r.media_type}:${r.pending.tmdb_id}`;
          if (!created.has(key)) {
            created.set(key, post<{ id: number }>(r.media_type === 'movie' ? '/movies' : '/series', r.pending).then((m) => m.id));
          }
          try { id = await created.get(key)!; } catch (e) { addFailed[r.path] = `Could not add "${r.pending.title}" to the library: ${errorMessage(e)}`; continue; }
        }
        items.push({ path: r.path, media_type: r.media_type, media_id: id, season: r.season, episodes: r.episodes, quality: r.quality });
      }
      const res = items.length
        ? await post<ImportResult>('/library/manual-import', { mode, items })
        : { imported: [], failed: [], failed_paths: {}, failed_codes: {} };
      return { ...res, failed: [...res.failed, ...Object.keys(addFailed)], failed_paths: { ...res.failed_paths, ...addFailed } };
    },
    onSuccess: (r) => {
      toast(`Imported ${r.imported.length} file(s)${r.failed.length ? `, ${r.failed.length} failed` : ''}`, r.failed.length ? 'warning' : 'success');
      const dupes = Object.entries(r.failed_codes ?? {}).filter(([, code]) => ALREADY_HAVE.includes(code)).map(([path]) => path);
      dupes.forEach((p) => haveIt.current.add(p));
      setFailReasons((fr) => ({ ...fr, ...Object.fromEntries(Object.entries(r.failed_paths).filter(([p]) => !haveIt.current.has(p))) }));
      if (dupes.length) toast(`${dupes.length} file(s) already in your library — select them and Delete to clean up`, 'info');
      scan.mutate();
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const match = useMutation({
    mutationFn: (r: Row) => matchApi(r),
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
  const allSelected = rows.length > 0 && rows.every((r) => r.selected);
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
          <Button variant="contained" color="secondary" startIcon={<Sparkles size={16} />} onClick={() => smartMatch.mutate()} disabled={!rows.some((r) => r.selected) || smartMatch.isPending}>Smart Match{rows.some((r) => r.selected) ? ` (${rows.filter((r) => r.selected).length})` : ''}</Button>
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
              <TableCell padding="checkbox">
                <Checkbox checked={allSelected} indeterminate={rows.some((r) => r.selected) && !allSelected}
                  onChange={(e) => setRows((rs) => rs.map((r) => ({ ...r, selected: e.target.checked })))} />
              </TableCell><TableCell>File</TableCell><TableCell>State</TableCell><TableCell>Quality</TableCell>
              <TableCell sx={{ minWidth: 280 }}>Match</TableCell><TableCell>Episode(s)</TableCell><TableCell>Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((r, i) => {
              const statusLabel = r.state !== 'ok' ? (r.state === 'sample' ? 'extra' : r.state) : r.haveIt ? 'in library' : r.library_state === 'duplicate' ? 'duplicate' : r.library_state === 'upgrade' ? 'upgrade' : 'ok';
              const failReason = failReasons[r.path];
              return (
                <TableRow key={r.path}>
                  <TableCell padding="checkbox">
                    <Checkbox checked={r.selected} onChange={(e) => update(i, { selected: e.target.checked })} />
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
                    {r.haveIt && <Typography variant="caption" display="block" color="text.secondary">Library already has this (equal or better) — safe to delete the download</Typography>}
                    {r.state === 'sample' && <Typography variant="caption" display="block" color="text.secondary">Sample / bonus material — not importable</Typography>}
                    {r.reason && <Typography variant="caption" display="block" color="text.secondary">{r.reason}</Typography>}
                    {failReason && (
                      <Typography variant="caption" display="block" color="error.main">Import failed: {failReason}</Typography>
                    )}
                  </TableCell>
                  <TableCell><QualityChip quality={r.quality} /></TableCell>
                  <TableCell>
                    {r.suggestions && (
                      r.suggestions.length ? (
                        <TextField select size="small" fullWidth sx={{ mb: 1 }} label={`Suggestions for "${r.query}"`}
                          value={r.suggestions.some((x) => suggestionKey(x) === rowKey(r)) ? rowKey(r) : ''}
                          onChange={async (e) => {
                            const sug = r.suggestions!.find((x) => suggestionKey(x) === e.target.value);
                            if (!sug) return;
                            let row = applyPatch(r, sug);
                            if (sug.media_id) { try { row = { ...row, ...(await matchApi(row)) }; } catch { /* annotate on demand */ } }
                            update(i, row);
                          }}>
                          {r.suggestions.map((sug) => (
                            <MenuItem key={suggestionKey(sug)} value={suggestionKey(sug)}>
                              {suggestionLabel(sug)} · {sug.media_type === 'movie' ? 'Movie' : 'TV'} · {sug.source === 'library' ? 'in library' : 'TMDb'}
                            </MenuItem>
                          ))}
                        </TextField>
                      ) : <Typography variant="caption" color="warning.main" display="block" sx={{ mb: 1 }}>No match found for "{r.query}" — search manually below</Typography>
                    )}
                    <MatchPicker row={r} onChange={(p) => update(i, { ...p, pending: null, library_state: null, existing_quality: null, existing_size: null })} />
                    {r.pending && <Chip size="small" color="info" variant="outlined" sx={{ mt: 0.5 }} label="From TMDb — added to library on import" />}
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
                      {r.state === 'ok' && r.media_id && !r.haveIt && (
                        <Button size="small" disabled={match.isPending} onClick={() => match.mutate(r, { onSuccess: (c) => update(i, c) })}>
                          {r.library_state ? 'Re-check' : 'Match'}
                        </Button>
                      )}
                      {isDeletable(r) && (
                        <Button size="small" color="error" startIcon={<Trash2 size={14} />} onClick={() => del.mutate([r.path])}>Delete</Button>
                      )}
                      {r.library_state === 'upgrade' && !r.haveIt && (
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
