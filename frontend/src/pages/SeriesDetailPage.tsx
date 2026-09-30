import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  Grid,
  IconButton,
  LinearProgress,
  MenuItem,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Bookmark, BookmarkX, ChevronDown, Eraser, FileEdit, Plus, RefreshCw, Search, Trash2, Zap } from 'lucide-react';
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { del, get, patch, post, put } from '../api/client';
import type { Episode, PruneStaleResult, QualityProfile, Season, Series } from '../api/types';
import { ErrorAlert, Loading, QualityChip, Section, useConfirm } from '../components/common';
import InteractiveSearchDialog, { type SearchTarget } from '../components/InteractiveSearchDialog';
import { CastList, Fact, MediaHeader, genreChips } from '../components/MediaHeader';
import { useAuth } from '../store/auth';
import { toast } from '../store/ui';
import { palette } from '../theme';
import { bytes, episodeCode, errorMessage } from '../utils/format';

const today = new Date().toISOString().slice(0, 10);

export default function SeriesDetailPage() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const can = useAuth((s) => s.can);
  const { confirm, dialog } = useConfirm();
  const [search, setSearch] = useState<SearchTarget | null>(null);
  const [adding, setAdding] = useState<'season' | 'episode' | null>(null);
  const [deleteFiles, setDeleteFiles] = useState(false);

  const series = useQuery({ queryKey: ['series', id], queryFn: () => get<Series>(`/series/${id}`) });
  const profiles = useQuery({ queryKey: ['quality-profiles'], queryFn: () => get<QualityProfile[]>('/quality-profiles') });
  const refresh = () => qc.invalidateQueries({ queryKey: ['series', id] });

  const run = useMutation({
    mutationFn: async (fn: () => Promise<unknown>) => fn(),
    onSuccess: (r) => { refresh(); const msg = (r as { message?: string })?.message; if (msg) toast(msg, 'success'); },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  if (series.isLoading) return <Loading />;
  if (!series.data) return <ErrorAlert error={series.error} />;
  const s = series.data;
  const write = can('media:write');
  const seasons = [...s.seasons].sort((a, b) => b.season_number - a.season_number);

  const remove = async () => {
    const ok = await confirm(`Delete ${s.title}?`,
      <FormControlLabel control={<Switch onChange={(e) => setDeleteFiles(e.target.checked)} />} label="Also move episode files to the recycle bin" />,
      { confirmLabel: 'Delete', danger: true });
    if (!ok) return;
    try {
      await del(`/series/${id}`, { delete_files: deleteFiles });
      qc.invalidateQueries({ queryKey: ['series'] });
      toast(`${s.title} removed`, 'success');
      navigate('/library');
    } catch (e) {
      toast(errorMessage(e), 'error');
    }
  };

  const pruneStale = async () => {
    try {
      const preview = await post<PruneStaleResult>(`/series/${id}/prune-stale`, undefined, { dry_run: true });
      if (!preview.seasons.length) {
        toast(`Nothing to clean up – every season matches ${preview.provider} metadata`, 'info');
        return;
      }
      const ok = await confirm('Clean up stale seasons?',
        <Stack spacing={1.5}>
          <Typography variant="body2" color="text.secondary">
            These file-less entries aren't in the current {preview.provider} metadata and would be removed. Files on disk are never touched.
          </Typography>
          <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
            {preview.seasons.map((x) => (
              <li key={x.season_number}>
                <Typography variant="body2">
                  {x.season_number === 0 ? 'Specials' : `Season ${x.season_number}`}: {x.removed ? `whole season (${x.episodes} episodes)` : `${x.episodes} episode(s)`}
                </Typography>
              </li>
            ))}
          </Box>
          {preview.kept_with_files > 0 && (
            <Typography variant="body2" color="text.secondary">{preview.kept_with_files} unlisted episode(s) with files will be kept.</Typography>
          )}
        </Stack>,
        { confirmLabel: `Remove ${preview.episodes} episode(s)`, danger: true });
      if (!ok) return;
      const done = await post<PruneStaleResult>(`/series/${id}/prune-stale`, undefined, { dry_run: false });
      refresh();
      toast(`Removed ${done.episodes} stale episode(s)`, 'success');
    } catch (e) {
      toast(errorMessage(e), 'error');
    }
  };

  return (
    <>
      <Button startIcon={<ArrowLeft size={16} />} onClick={() => navigate(-1)} sx={{ mb: 2 }}>Back</Button>
      <MediaHeader
        title={s.title} year={s.year} backdrop={s.backdrop_path} poster={s.poster_path}
        overline={['TV Series', s.network, s.certification, s.runtime_minutes ? `${s.runtime_minutes} min` : null, s.status].filter(Boolean).join(' · ')}
        chips={
          <>
            <Chip size="small" color={s.episode_file_count >= s.episode_count ? 'success' : 'warning'} label={`${s.episode_file_count} / ${s.episode_count} episodes`} />
            {s.size_on_disk > 0 && <Chip size="small" label={bytes(s.size_on_disk)} variant="outlined" />}
            {s.rating ? <Chip size="small" label={`★ ${s.rating.toFixed(1)}`} /> : null}
            {genreChips(s.genres)}
          </>
        }
        actions={
          <>
            {can('search:execute') && (
              <Button variant="contained" color="secondary" startIcon={<Zap size={16} />}
                onClick={() => run.mutate(() => post('/search/missing', undefined, { limit: 100 }).then(() => ({ message: 'Missing-episode search queued' })))}>
                Search missing
              </Button>
            )}
            {write && <Button variant="outlined" startIcon={<RefreshCw size={16} />} onClick={() => run.mutate(() => post(`/series/${id}/refresh`).then(() => ({ message: 'Metadata refresh queued' })))}>Refresh metadata</Button>}
            {can('files:rename') && <Button variant="outlined" startIcon={<FileEdit size={16} />} onClick={() => navigate(`/renamer?media_type=tv&media_id=${id}`)}>Rename files</Button>}
            {write && <Button variant="outlined" startIcon={<Plus size={16} />} onClick={() => setAdding('season')}>Add season</Button>}
            {write && <Button variant="outlined" startIcon={<Plus size={16} />} onClick={() => setAdding('episode')}>Add episode</Button>}
            {write && <Button variant="outlined" startIcon={<Eraser size={16} />} onClick={pruneStale}>Clean up seasons</Button>}
            {can('media:delete') && <Button color="error" variant="outlined" startIcon={<Trash2 size={16} />} onClick={remove}>Delete</Button>}
          </>
        }
      />

      <Grid container spacing={2}>
        <Grid size={{ xs: 12, lg: 8 }}>
          <Section title="Overview" sx={{ mb: 2 }}>
            <Typography color="text.secondary" sx={{ lineHeight: 1.7 }}>{s.overview || 'No overview yet – metadata refresh runs in the background.'}</Typography>
          </Section>
          {seasons.map((season) => (
            <SeasonPanel key={season.id} season={season} series={s} write={write} canSearch={can('search:execute')}
              onChanged={refresh} onSearch={setSearch} defaultExpanded={season.season_number === seasons.find((x) => x.season_number > 0)?.season_number} />
          ))}
          <Section title="Cast" sx={{ mt: 2 }}><CastList cast={s.cast_members} /></Section>
        </Grid>
        <Grid size={{ xs: 12, lg: 4 }}>
          <Section title="Management">
            <FormControlLabel sx={{ mb: 1 }} label={s.monitored ? 'Series monitored' : 'Series unmonitored'}
              control={<Switch checked={s.monitored} disabled={!write} onChange={(_, monitored) => run.mutate(() => patch(`/series/${id}`, { monitored }))} />} />
            <FormControlLabel sx={{ mb: 2 }} label="Monitor new seasons"
              control={<Switch checked={s.monitor_new_seasons !== false} disabled={!write} onChange={(_, monitor_new_seasons) => run.mutate(() => patch(`/series/${id}`, { monitor_new_seasons }))} />} />
            <TextField select fullWidth size="small" label="Quality profile" value={s.quality_profile_id ?? 1} disabled={!write} sx={{ mb: 2 }}
              onChange={(e) => { const quality_profile_id = Number(e.target.value); run.mutate(() => patch(`/series/${id}`, { quality_profile_id })); }}>
              {profiles.data?.map((p) => <MenuItem key={p.id} value={p.id}>{p.name}</MenuItem>)}
            </TextField>
            <TextField select fullWidth size="small" label="Apply monitoring preset" value="" disabled={!write} sx={{ mb: 2 }}
              onChange={(e) => { const preset = e.target.value; run.mutate(() => put(`/series/${id}/monitoring`, undefined, { preset })); }}>
              {[['all', 'All episodes'], ['future', 'Future episodes'], ['missing', 'Missing episodes'], ['latest', 'Latest season'], ['first', 'First season'], ['none', 'None']]
                .map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
            </TextField>
            <Fact label="Folder"><Box component="code" sx={{ fontSize: '0.78rem' }}>{s.path ?? s.root_folder}</Box></Fact>
            <Fact label="First aired">{s.first_aired}</Fact>
            <Fact label="External IDs">
              {[s.imdb_id && `IMDb ${s.imdb_id}`, s.tvdb_id && `TVDB ${s.tvdb_id}`, s.tvmaze_id && `TVmaze ${s.tvmaze_id}`].filter(Boolean).join(' · ') || '—'}
            </Fact>
            <Fact label="Metadata source">{s.metadata_source ?? 'not matched yet'}</Fact>
          </Section>
        </Grid>
      </Grid>

      <AddDialog kind={adding} seriesId={id} onClose={() => setAdding(null)} onDone={refresh} />
      <InteractiveSearchDialog target={search} onClose={() => setSearch(null)} />
      {dialog}
    </>
  );
}

function SeasonPanel({ season, series, write, canSearch, onChanged, onSearch, defaultExpanded }: {
  season: Season; series: Series; write: boolean; canSearch: boolean; onChanged: () => void; onSearch: (t: SearchTarget) => void; defaultExpanded: boolean;
}) {
  const episodes = [...season.episodes].sort((a, b) => b.episode_number - a.episode_number);
  const have = episodes.filter((e) => e.has_file).length;
  const call = async (fn: () => Promise<unknown>, msg?: string) => {
    try {
      await fn();
      if (msg) toast(msg, 'success');
      onChanged();
    } catch (e) {
      toast(errorMessage(e), 'error');
    }
  };
  const label = season.season_number === 0 ? 'Specials' : `Season ${season.season_number}`;

  return (
    <Accordion defaultExpanded={defaultExpanded} disableGutters sx={{ mb: 1, '&:before': { display: 'none' } }}>
      <AccordionSummary expandIcon={<ChevronDown size={18} />}>
        <Stack direction="row" alignItems="center" spacing={2} sx={{ width: '100%', pr: 2 }}>
          <Tooltip title={season.monitored ? 'Monitored – click to unmonitor' : 'Unmonitored – click to monitor'}>
            <IconButton size="small" disabled={!write} onClick={(e) => { e.stopPropagation(); call(() => put(`/series/${series.id}/seasons/${season.season_number}/monitor`, { monitored: !season.monitored })); }}>
              {season.monitored ? <Bookmark size={16} color={palette.cyan} /> : <BookmarkX size={16} color={palette.dim} />}
            </IconButton>
          </Tooltip>
          <Typography fontWeight={700} sx={{ minWidth: 90 }}>{label}</Typography>
          <LinearProgress variant="determinate" value={episodes.length ? (have / episodes.length) * 100 : 0} sx={{ flex: 1, maxWidth: 240 }} />
          <Typography variant="body2" color="text.secondary">{have}/{episodes.length}</Typography>
          <Box sx={{ flex: 1 }} />
          {canSearch && (
            <Tooltip title="Search this season (pack preferred)">
              <IconButton size="small" onClick={(e) => { e.stopPropagation(); call(() => post('/search/automatic', { media_type: 'tv', media_id: series.id, season: season.season_number }), `Searching ${label}`); }}>
                <Zap size={16} />
              </IconButton>
            </Tooltip>
          )}
          {canSearch && (
            <Tooltip title="Interactive season search">
              <IconButton size="small" onClick={(e) => { e.stopPropagation(); onSearch({ mediaType: 'tv', mediaId: series.id, title: series.title, season: season.season_number }); }}>
                <Search size={16} />
              </IconButton>
            </Tooltip>
          )}
        </Stack>
      </AccordionSummary>
      <AccordionDetails sx={{ p: 0 }}>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell />
              <TableCell>#</TableCell>
              <TableCell>Title</TableCell>
              <TableCell>Air date</TableCell>
              <TableCell>Status</TableCell>
              <TableCell align="right" />
            </TableRow>
          </TableHead>
          <TableBody>
            {episodes.map((ep) => (
              <EpisodeRow key={ep.id} ep={ep} series={series} write={write} canSearch={canSearch} call={call} onSearch={onSearch} />
            ))}
          </TableBody>
        </Table>
      </AccordionDetails>
    </Accordion>
  );
}

function EpisodeRow({ ep, series, write, canSearch, call, onSearch }: {
  ep: Episode; series: Series; write: boolean; canSearch: boolean; call: (fn: () => Promise<unknown>, msg?: string) => void; onSearch: (t: SearchTarget) => void;
}) {
  const aired = !ep.air_date || ep.air_date <= today;
  const primaryFile = ep.files?.find((f) => f.is_primary) ?? ep.files?.[0];
  const filePath = primaryFile?.path ?? ep.file_path ?? '';
  const otherEps = primaryFile?.episodes?.filter((e) => e.id !== ep.id);
  const multiInfo = otherEps && otherEps.length > 0
    ? `Linked episodes: ${[ep, ...otherEps].map((e) => `E${String(e.episode_number).padStart(2, '0')}`).join(', ')}`
    : null;
  const tooltipText = multiInfo ? `${filePath} · ${multiInfo}` : filePath;

  const status = ep.has_file ? (
    <Tooltip title={tooltipText}>
      <span>
        <QualityChip quality={primaryFile?.quality ?? ep.quality ?? 'On disk'} />
        {otherEps && otherEps.length > 0 && (
          <Chip size="small" variant="outlined" sx={{ ml: 0.75, fontSize: '0.7rem', height: 20 }} label={`Multi (+E${otherEps.map((e) => e.episode_number).join(', ')})`} />
        )}
      </span>
    </Tooltip>
  ) : ep.status === 'downloading' ? <Chip size="small" color="secondary" label="Downloading" />
    : aired ? <Chip size="small" color="error" variant="outlined" label="Missing" />
    : <Chip size="small" variant="outlined" label="Unaired" />;
  return (
    <TableRow hover>
      <TableCell sx={{ width: 40 }}>
        <IconButton size="small" disabled={!write} onClick={() => call(() => put('/episodes/monitor', { episode_ids: [ep.id], monitored: !ep.monitored }))}>
          {ep.monitored ? <Bookmark size={14} color={palette.cyan} /> : <BookmarkX size={14} color={palette.dim} />}
        </IconButton>
      </TableCell>
      <TableCell sx={{ width: 50, fontWeight: 700 }}>{ep.episode_number}</TableCell>
      <TableCell>
        <Tooltip title={ep.overview ?? ''} placement="top-start"><span>{ep.title}</span></Tooltip>
      </TableCell>
      <TableCell sx={{ whiteSpace: 'nowrap', color: 'text.secondary' }}>{ep.air_date ?? 'TBA'}</TableCell>
      <TableCell>{status}</TableCell>
      <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
        {canSearch && aired && (
          <>
            <Tooltip title="Automatic search">
              <IconButton size="small" onClick={() => call(() => post('/search/automatic', { media_type: 'tv', media_id: series.id, season: ep.season_number, episodes: [ep.episode_number] }),
                `Searching ${episodeCode(ep.season_number, ep.episode_number)}`)}>
                <Zap size={14} />
              </IconButton>
            </Tooltip>
            <Tooltip title="Interactive search">
              <IconButton size="small" onClick={() => onSearch({ mediaType: 'tv', mediaId: series.id, title: series.title, season: ep.season_number, episode: ep.episode_number })}>
                <Search size={14} />
              </IconButton>
            </Tooltip>
          </>
        )}
      </TableCell>
    </TableRow>
  );
}

function AddDialog({ kind, seriesId, onClose, onDone }: { kind: 'season' | 'episode' | null; seriesId: number; onClose: () => void; onDone: () => void }) {
  const [season, setSeason] = useState(1);
  const [episode, setEpisode] = useState(1);
  const [count, setCount] = useState(10);
  const [title, setTitle] = useState('');
  const [airDate, setAirDate] = useState('');
  const submit = async () => {
    try {
      if (kind === 'season') await post(`/series/${seriesId}/seasons`, { season_number: season, episode_count: count });
      else await post(`/series/${seriesId}/episodes`, { season_number: season, episode_number: episode, title, air_date: airDate || null });
      toast(kind === 'season' ? `Season ${season} added` : `Episode added`, 'success');
      onDone();
      onClose();
    } catch (e) {
      toast(errorMessage(e), 'error');
    }
  };
  return (
    <Dialog open={!!kind} onClose={onClose} maxWidth="xs" fullWidth>
      <DialogTitle>{kind === 'season' ? 'Add season' : 'Add episode'}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField type="number" label="Season number" value={season} onChange={(e) => setSeason(Number(e.target.value))} />
          {kind === 'season' ? (
            <TextField type="number" label="Number of episodes" value={count} onChange={(e) => setCount(Number(e.target.value))} helperText="Placeholder episodes; metadata refresh will fill titles" />
          ) : (
            <>
              <TextField type="number" label="Episode number" value={episode} onChange={(e) => setEpisode(Number(e.target.value))} />
              <TextField label="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
              <TextField type="date" label="Air date" value={airDate} onChange={(e) => setAirDate(e.target.value)} InputLabelProps={{ shrink: true }} />
            </>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" onClick={submit}>Add</Button>
      </DialogActions>
    </Dialog>
  );
}
