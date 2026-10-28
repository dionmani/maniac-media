import {
  Alert,
  Button,
  Checkbox,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  MenuItem,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from '@mui/material';
import { useMutation, useQuery } from '@tanstack/react-query';
import { FolderSearch } from 'lucide-react';
import { useEffect, useState } from 'react';
import { get, post } from '../api/client';
import type { Candidate, ImportResult } from '../api/types';
import { toast } from '../store/ui';
import { bytes, errorMessage } from '../utils/format';
import { Loading, QualityChip } from './common';

export interface ManualImportTarget {
  mediaType: 'movie' | 'tv';
  mediaId: number;
  title: string;
  folder: string;
  /** Files the library already tracks for this title; listed but not importable. */
  currentPaths: string[];
}

type Row = Candidate & { selected: boolean; current: boolean; episodeText: string };

const fileName = (path: string) => path.split(/[\\/]/).pop() ?? path;
const parseEpisodes = (text: string) => text.split(/[\s,]+/).map(Number).filter((n) => Number.isInteger(n) && n > 0);

/** S01E02 / S01E02E03 / S01E02-E03 from a file name, for files the scan couldn't match to a series. */
function guessEpisode(path: string): { season: number | null; episodes: number[] } {
  const m = fileName(path).match(/S(\d{1,2})((?:[-_. ]?E\d{1,3})+)/i);
  if (!m) return { season: null, episodes: [] };
  return { season: Number(m[1]), episodes: [...m[2].matchAll(/E(\d{1,3})/gi)].map((e) => Number(e[1])) };
}

function stateChip(r: Row) {
  if (r.current) return <Chip size="small" color="primary" variant="outlined" label="Current file" />;
  if (r.state !== 'ok') return <Chip size="small" color="warning" variant="outlined" label={r.reason ?? r.state} />;
  if (r.library_state === 'new') return <Chip size="small" color="success" variant="outlined" label="New" />;
  if (r.library_state === 'upgrade') return <Chip size="small" color="success" label={`Upgrade over ${r.existing_quality ?? 'current'}`} />;
  if (r.library_state === 'duplicate') return <Chip size="small" color="warning" label={`Not an upgrade over ${r.existing_quality ?? 'current'}`} />;
  return null;
}

/** Pick files from a folder on the server and import them into one movie / series. */
export default function ManualImportDialog({ target, onClose, onImported }: {
  target: ManualImportTarget | null; onClose: () => void; onImported: () => void;
}) {
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => get<{ import_mode: string }>('/settings'), enabled: !!target });
  const [folder, setFolder] = useState('');
  const [rows, setRows] = useState<Row[]>([]);
  const [mode, setMode] = useState('');
  const [force, setForce] = useState(false);
  const [failures, setFailures] = useState<Record<string, string>>({});
  const tv = target?.mediaType === 'tv';

  useEffect(() => {
    if (settings.data && !mode) setMode(settings.data.import_mode);
  }, [settings.data, mode]);

  const scan = useMutation({
    mutationFn: (dir: string) => post<Candidate[]>('/library/manual-import/scan', { folder: dir }),
    onSuccess: (data) => {
      const current = new Set(target?.currentPaths ?? []);
      setRows(data.map((c) => {
        // whatever the scan guessed, these files are going into this title
        const ep = tv && c.media_id === target?.mediaId && c.season != null ? { season: c.season, episodes: c.episodes } : guessEpisode(c.path);
        return {
          ...c, media_type: target!.mediaType, media_id: target!.mediaId, title: target!.title,
          season: tv ? ep.season : null, episodes: tv ? ep.episodes : [], episodeText: tv ? ep.episodes.join(', ') : '',
          library_state: null, existing_quality: null, existing_size: null,
          selected: false, current: current.has(c.path),
        };
      }));
      setFailures({});
    },
    onError: (e) => { setRows([]); toast(errorMessage(e), 'error'); },
  });

  useEffect(() => {
    if (!target) return;
    setFolder(target.folder);
    setRows([]);
    setForce(false);
    setFailures({});
    if (target.folder) scan.mutate(target.folder);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  const update = (path: string, patch: Partial<Row>) => setRows((rs) => rs.map((r) => (r.path === path ? { ...r, ...patch } : r)));

  /** Re-check new / upgrade / not-an-upgrade against this title once a row is picked or its episodes change. */
  const annotate = async (r: Row) => {
    if (r.state !== 'ok' || (tv && (r.season == null || !r.episodes.length))) return;
    try {
      const c = await post<Candidate>('/library/manual-import/match',
        { path: r.path, media_type: r.media_type, media_id: r.media_id, season: r.season, episodes: r.episodes });
      update(r.path, { library_state: c.library_state, existing_quality: c.existing_quality, existing_size: c.existing_size });
    } catch { /* leave it unannotated; the import itself still validates */ }
  };

  const toggle = (r: Row) => {
    update(r.path, { selected: !r.selected });
    if (!r.selected && !r.library_state) annotate(r);
  };

  const setEpisodes = (r: Row, season: number | null, episodeText: string) => {
    update(r.path, { season, episodeText, episodes: parseEpisodes(episodeText), library_state: null });
  };

  const importable = (r: Row) => r.selected && !r.current && r.state === 'ok' && (!tv || (r.season != null && r.episodes.length > 0));
  const picked = rows.filter(importable);
  const blockedByCutoff = picked.some((r) => r.library_state === 'duplicate') && !force;

  const run = useMutation({
    mutationFn: () => post<ImportResult>('/library/manual-import', {
      mode: mode || undefined, force,
      items: picked.map((r) => ({ path: r.path, media_type: r.media_type, media_id: r.media_id, season: r.season, episodes: r.episodes, quality: r.quality })),
    }),
    onSuccess: (res) => {
      toast(`Imported ${res.imported.length} file(s)${res.failed.length ? `, ${res.failed.length} failed` : ''}`, res.failed.length ? 'warning' : 'success');
      if (res.imported.length) onImported();
      if (res.failed.length) {
        setFailures(res.failed_paths);
        setRows((rs) => rs.map((r) => (res.failed_paths[r.path] ? r : { ...r, selected: false })));
      } else {
        onClose();
      }
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  return (
    <Dialog open={!!target} onClose={run.isPending ? undefined : onClose} maxWidth="lg" fullWidth>
      <DialogTitle>Import from disk – {target?.title}</DialogTitle>
      <DialogContent>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1, mb: 2 }}>
          <TextField fullWidth size="small" label="Folder on the server" value={folder} onChange={(e) => setFolder(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && folder) scan.mutate(folder); }}
            helperText="Defaults to this title's library folder – point it at a download folder to import from there" />
          <Button variant="outlined" startIcon={<FolderSearch size={16} />} disabled={!folder || scan.isPending}
            onClick={() => scan.mutate(folder)} sx={{ alignSelf: 'flex-start', whiteSpace: 'nowrap', height: 40 }}>
            Scan folder
          </Button>
        </Stack>
        {scan.isPending ? <Loading label="Scanning…" /> : !rows.length ? (
          <Typography color="text.secondary" variant="body2">{scan.isSuccess ? 'No video files in this folder.' : 'Scan a folder to list its video files.'}</Typography>
        ) : (
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell padding="checkbox" />
                <TableCell>File</TableCell>
                {tv && <TableCell sx={{ width: 90 }}>Season</TableCell>}
                {tv && <TableCell sx={{ width: 120 }}>Episode(s)</TableCell>}
                <TableCell>Quality</TableCell>
                <TableCell>Size</TableCell>
                <TableCell>Status</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((r) => {
                const selectable = r.state === 'ok' && !r.current;
                return (
                  <TableRow key={r.path} hover selected={r.selected}>
                    <TableCell padding="checkbox">
                      <Checkbox size="small" checked={r.selected} disabled={!selectable} onChange={() => toggle(r)} />
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2" sx={{ wordBreak: 'break-all', fontFamily: 'monospace', fontSize: '0.8rem' }}>{fileName(r.path)}</Typography>
                      {failures[r.path] && <Typography variant="caption" color="error">{failures[r.path]}</Typography>}
                    </TableCell>
                    {tv && (
                      <TableCell>
                        <TextField size="small" type="number" value={r.season ?? ''} disabled={!selectable}
                          onChange={(e) => setEpisodes(r, e.target.value === '' ? null : Number(e.target.value), r.episodeText)}
                          onBlur={() => r.selected && annotate(r)} />
                      </TableCell>
                    )}
                    {tv && (
                      <TableCell>
                        <TextField size="small" placeholder="1, 2" value={r.episodeText} disabled={!selectable}
                          onChange={(e) => setEpisodes(r, r.season, e.target.value)}
                          onBlur={() => r.selected && annotate(r)} />
                      </TableCell>
                    )}
                    <TableCell><QualityChip quality={r.quality} /></TableCell>
                    <TableCell sx={{ whiteSpace: 'nowrap', color: 'text.secondary' }}>{bytes(r.size)}</TableCell>
                    <TableCell>{stateChip(r)}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ mt: 2 }} alignItems={{ sm: 'center' }}>
          <TextField select size="small" label="Import mode" value={mode} onChange={(e) => setMode(e.target.value)} sx={{ minWidth: 160 }}>
            {['move', 'copy', 'hardlink', 'symlink'].map((m) => <MenuItem key={m} value={m}>{m}</MenuItem>)}
          </TextField>
          <FormControlLabel control={<Checkbox checked={force} onChange={(e) => setForce(e.target.checked)} />}
            label="Replace the existing file even if this one isn't an upgrade" />
        </Stack>
        {blockedByCutoff && (
          <Alert severity="warning" sx={{ mt: 1 }}>
            The library already has an equal or better file for some of the selected rows. Tick “Replace the existing file” to import them anyway
            – the current file goes to the recycle bin.
          </Alert>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={run.isPending}>Cancel</Button>
        <Button variant="contained" onClick={() => run.mutate()} disabled={!picked.length || run.isPending}>
          Import {picked.length || ''} file{picked.length === 1 ? '' : 's'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
