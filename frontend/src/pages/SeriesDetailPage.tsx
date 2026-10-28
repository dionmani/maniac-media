import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  Collapse,
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
import { ArrowLeft, Bookmark, BookmarkX, ChevronDown, Eraser, FileEdit, FileX, FolderInput, FolderSync, Info, Link2, Plus, RefreshCw, Search, Trash2, Zap } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { del, get, patch, post, put } from '../api/client';
import type { DeleteEpisodesResult, Episode, MediaFile, PruneStaleResult, QualityProfile, Season, Series } from '../api/types';
import { ActionsMenu, ErrorAlert, Loading, QualityChip, Section, useConfirm } from '../components/common';
import InteractiveSearchDialog, { type SearchTarget } from '../components/InteractiveSearchDialog';
import LinkDialog, { type LinkTarget } from '../components/LinkDialog';
import ManualImportDialog, { type ManualImportTarget } from '../components/ManualImportDialog';
import { CastList, Fact, MediaHeader, genreChips, requestedByChip } from '../components/MediaHeader';
import TechnicalInfoPanel, { claimedResolutionMismatch } from '../components/TechnicalInfoPanel';
import { useAuth } from '../store/auth';
import { toast } from '../store/ui';
import { palette } from '../theme';
import { bytes, episodeCode, errorMessage } from '../utils/format';

export default function SeriesDetailPage() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const can = useAuth((s) => s.can);
  const { confirm, dialog } = useConfirm();
  const [search, setSearch] = useState<SearchTarget | null>(null);
  const [adding, setAdding] = useState<'season' | 'episode' | null>(null);
  // read after `await confirm(...)`, so refs: state would still hold the value from before the dialog opened
  const deleteFiles = useRef(false);
  const searchAfter = useRef(false);
  const [removal, setRemoval] = useState<RemovalTarget | null>(null);
  const [link, setLink] = useState<LinkTarget | null>(null);
  const [importing, setImporting] = useState<ManualImportTarget | null>(null);

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

  const openLink = () => setLink({ mediaType: 'tv', mediaId: id, label: `${s.title} (${s.year})`, path: s.path });
  const linked = () => {
    refresh();
    qc.invalidateQueries({ queryKey: ['series'] });
  };

  const remove = async () => {
    deleteFiles.current = false;
    const ok = await confirm(`Delete ${s.title}?`,
      <FormControlLabel control={<Switch onChange={(e) => { deleteFiles.current = e.target.checked; }} />} label="Also move episode files to the recycle bin" />,
      { confirmLabel: 'Delete', danger: true });
    if (!ok) return;
    try {
      await del(`/series/${id}`, { delete_files: deleteFiles.current });
      qc.invalidateQueries({ queryKey: ['series'] });
      toast(`${s.title} removed`, 'success');
      navigate('/library');
    } catch (e) {
      toast(errorMessage(e), 'error');
    }
  };

  const deleteFile = async (file: MediaFile, ep: Episode) => {
    searchAfter.current = false;
    const held = file.episodes?.length ? file.episodes : [ep];
    const ok = await confirm('Delete this file?',
      <Stack spacing={1.5}>
        <Box component="code" sx={{ fontSize: '0.75rem', wordBreak: 'break-all' }}>{file.path}</Box>
        <Typography variant="body2" color="text.secondary">
          The file moves to the recycle bin; {held.length > 1 ? `${held.map((e) => episodeCode(e.season_number, e.episode_number)).join(', ')} stay` : 'the episode stays'} in
          the library without a file, so you can grab or import a replacement.
        </Typography>
        {can('search:execute') && (
          <FormControlLabel control={<Switch onChange={(e) => { searchAfter.current = e.target.checked; }} />} label="Search for a replacement afterwards" />
        )}
      </Stack>,
      { confirmLabel: 'Delete file', danger: true });
    if (!ok) return;
    try {
      const r = await del<{ message: string }>(`/media-files/${file.id}`, { search: searchAfter.current });
      toast(r.message, 'success');
      refresh();
    } catch (e) {
      toast(errorMessage(e), 'error');
    }
  };

  const currentPaths = s.seasons.flatMap((x) => x.episodes.flatMap((e) => e.files?.map((f) => f.path) ?? (e.file_path ? [e.file_path] : [])));

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
            <Chip size="small" color={s.episode_file_count >= s.aired_episode_count ? 'success' : 'warning'} label={`${s.episode_file_count} / ${s.aired_episode_count} episodes`} />
            {s.size_on_disk > 0 && <Chip size="small" label={bytes(s.size_on_disk)} variant="outlined" />}
            {s.rating ? <Chip size="small" label={`★ ${s.rating.toFixed(1)}`} /> : null}
            {requestedByChip(s.requested_by)}
            {genreChips(s.genres)}
          </>
        }
        actions={
          <ActionsMenu items={[
            { label: 'Search missing', icon: <Zap size={16} />, hidden: !can('search:execute'),
              onClick: () => run.mutate(() => post('/search/missing', undefined, { limit: 100 }).then(() => ({ message: 'Missing-episode search queued' }))) },
            { label: 'Rescan files', icon: <FolderSync size={16} />, hidden: !can('library:scan'), divider: true,
              onClick: () => run.mutate(() => post('/library/scan', { scope: 'tv', incremental: true, media_id: id }).then(() => ({ message: 'Rescan queued – refresh in a moment' }))) },
            { label: 'Import from disk…', icon: <FolderInput size={16} />, hidden: !can('files:import'),
              onClick: () => setImporting({ mediaType: 'tv', mediaId: id, title: s.title, folder: s.path ?? '', currentPaths }) },
            { label: 'Refresh metadata', icon: <RefreshCw size={16} />, hidden: !write,
              onClick: () => run.mutate(() => post(`/series/${id}/refresh`).then(() => ({ message: 'Metadata refresh queued' }))) },
            { label: 'Link…', icon: <Link2 size={16} />, hidden: !write, onClick: openLink },
            { label: 'Rename files', icon: <FileEdit size={16} />, hidden: !can('files:rename'), onClick: () => navigate(`/renamer?media_type=tv&media_id=${id}`) },
            { label: 'Add season…', icon: <Plus size={16} />, hidden: !write, divider: true, onClick: () => setAdding('season') },
            { label: 'Add episode…', icon: <Plus size={16} />, hidden: !write, onClick: () => setAdding('episode') },
            { label: 'Clean up seasons…', icon: <Eraser size={16} />, hidden: !write, onClick: pruneStale },
            { label: 'Delete series…', icon: <Trash2 size={16} />, hidden: !can('media:delete'), danger: true, divider: true, onClick: remove },
          ]} />
        }
      />

      {!s.metadata_source && write && (
        <Alert severity="info" sx={{ mb: 2 }} action={<Button color="inherit" size="small" startIcon={<Link2 size={14} />} onClick={openLink}>Link</Button>}>
          This show isn't matched to any metadata yet. If the title above is wrong, link it to the right show with its TVDB or IMDb id.
        </Alert>
      )}
      <Grid container spacing={2}>
        <Grid size={{ xs: 12, lg: 8 }}>
          <Section title="Overview" sx={{ mb: 2 }}>
            <Typography color="text.secondary" sx={{ lineHeight: 1.7 }}>{s.overview || 'No overview yet – metadata refresh runs in the background.'}</Typography>
          </Section>
          {seasons.map((season) => (
            <SeasonPanel key={season.id} season={season} series={s} write={write} canSearch={can('search:execute')}
              canDelete={can('media:delete')} onRemove={setRemoval} onDeleteFile={deleteFile} onChanged={refresh} onSearch={setSearch} defaultExpanded={season.season_number === seasons.find((x) => x.season_number > 0)?.season_number} />
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

      <LinkDialog target={link} onClose={() => setLink(null)} onLinked={linked} />
      <ManualImportDialog target={importing} onClose={() => setImporting(null)} onImported={refresh} />
      <AddDialog kind={adding} seriesId={id} onClose={() => setAdding(null)} onDone={refresh} />
      <RemoveDialog seriesId={id} target={removal} onClose={() => setRemoval(null)} onDone={refresh} />
      <InteractiveSearchDialog target={search} onClose={() => setSearch(null)} />
      {dialog}
    </>
  );
}

type RemovalTarget = { kind: 'episode'; episode: Episode } | { kind: 'season'; season: number };

function SeasonPanel({ season, series, write, canSearch, canDelete, onRemove, onDeleteFile, onChanged, onSearch, defaultExpanded }: {
  season: Season; series: Series; write: boolean; canSearch: boolean; canDelete: boolean; onRemove: (t: RemovalTarget) => void;
  onDeleteFile: (file: MediaFile, ep: Episode) => void;
  onChanged: () => void; onSearch: (t: SearchTarget) => void; defaultExpanded: boolean;
}) {
  const episodes = [...season.episodes].sort((a, b) => b.episode_number - a.episode_number);
  const have = episodes.filter((e) => e.has_file).length;
  // same rule as the series' "x / y episodes": unmonitored or unaired episodes without a file aren't wanted, so they aren't counted
  const counted = episodes.filter((e) => e.has_file || (e.monitored && e.aired)).length;
  const call =async (fn: () => Promise<unknown>, msg?: string) => {
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
          <LinearProgress variant="determinate" value={counted ? (have / counted) * 100 : 0} sx={{ flex: 1, maxWidth: 240 }} />
          <Tooltip title={counted < episodes.length ? `${episodes.length} episodes in total – unmonitored and unaired ones without a file aren't counted` : ''}>
            <Typography variant="body2" color="text.secondary">
              {counted ? `${have}/${counted}` : episodes.every((e) => !e.monitored) ? 'Unmonitored' : 'Not aired yet'}
            </Typography>
          </Tooltip>
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
          {canDelete && (
            <Tooltip title="Delete this season…">
              <IconButton size="small" onClick={(e) => { e.stopPropagation(); onRemove({ kind: 'season', season: season.season_number }); }}>
                <Trash2 size={16} color={palette.red} />
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
              <EpisodeRow key={ep.id} ep={ep} series={series} write={write} canSearch={canSearch} canDelete={canDelete} onRemove={onRemove} onDeleteFile={onDeleteFile} call={call} onSearch={onSearch} />
            ))}
          </TableBody>
        </Table>
      </AccordionDetails>
    </Accordion>
  );
}

function EpisodeRow({ ep, series, write, canSearch, canDelete, onRemove, onDeleteFile, call, onSearch }: {
  ep: Episode; series: Series; write: boolean; canSearch: boolean; canDelete: boolean; onRemove: (t: RemovalTarget) => void;
  onDeleteFile: (file: MediaFile, ep: Episode) => void; call: (fn: () => Promise<unknown>, msg?: string) => void; onSearch: (t: SearchTarget) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const aired = ep.aired;
  const primaryFile = ep.files?.find((f) => f.is_primary) ?? ep.files?.[0];
  const filePath = primaryFile?.path ?? ep.file_path ?? '';
  const otherEps = primaryFile?.episodes?.filter((e) => e.id !== ep.id);
  const multiInfo = otherEps && otherEps.length > 0
    ? `Linked episodes: ${[ep, ...otherEps].map((e) => `E${String(e.episode_number).padStart(2, '0')}`).join(', ')}`
    : null;
  const tooltipText = multiInfo ? `${filePath} · ${multiInfo}` : filePath;
  const mismatchClaimed = claimedResolutionMismatch(primaryFile?.quality ?? ep.quality, primaryFile?.video_resolution);

  const status = ep.has_file ? (
    <Tooltip title={tooltipText}>
      <Stack direction="row" spacing={0.5} alignItems="center" component="span">
        <QualityChip quality={primaryFile?.quality ?? ep.quality ?? 'On disk'} />
        {mismatchClaimed && (
          <Chip size="small" color="warning" label={`Actual: ${primaryFile?.video_resolution}`} sx={{ height: 20, fontSize: '0.68rem' }} />
        )}
        {otherEps && otherEps.length > 0 && (
          <Chip size="small" variant="outlined" sx={{ fontSize: '0.7rem', height: 20 }} label={`Multi (+E${otherEps.map((e) => e.episode_number).join(', ')})`} />
        )}
      </Stack>
    </Tooltip>
  ) : ep.status === 'downloading' ? <Chip size="small" color="secondary" label="Downloading" />
    : aired ? <Chip size="small" color="error" variant="outlined" label="Missing" />
    : <Chip size="small" variant="outlined" label="Unaired" />;
  return (
    <>
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
          {primaryFile && (
            <Tooltip title={expanded ? 'Hide technical info' : 'View technical info (ffprobe)'}>
              <IconButton size="small" onClick={() => setExpanded(!expanded)} color={expanded ? 'primary' : 'default'}>
                <Info size={14} />
              </IconButton>
            </Tooltip>
          )}
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
          {canDelete && primaryFile && (
            <Tooltip title="Delete file only (keeps the episode)…">
              <IconButton size="small" onClick={() => onDeleteFile(primaryFile, ep)}>
                <FileX size={14} />
              </IconButton>
            </Tooltip>
          )}
          {canDelete && (
            <Tooltip title="Delete episode…">
              <IconButton size="small" onClick={() => onRemove({ kind: 'episode', episode: ep })}>
                <Trash2 size={14} color={palette.red} />
              </IconButton>
            </Tooltip>
          )}
        </TableCell>
      </TableRow>
      {primaryFile && (
        <TableRow>
          <TableCell colSpan={6} sx={{ py: 0, px: 2, border: expanded ? undefined : 0 }}>
            <Collapse in={expanded} unmountOnExit>
              <Box sx={{ py: 1.5 }}>
                <TechnicalInfoPanel
                  file={primaryFile}
                  expectedRuntimeMinutes={ep.runtime_minutes ?? series.runtime_minutes}
                  canInspect={write}
                />
              </Box>
            </Collapse>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

/** Two-step delete of an episode or season: preview what it touches, then require an explicit acknowledgement before any file moves. */
function RemoveDialog({ seriesId, target, onClose, onDone }: { seriesId: number; target: RemovalTarget | null; onClose: () => void; onDone: () => void }) {
  const [preview, setPreview] = useState<DeleteEpisodesResult | null>(null);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const url = target?.kind === 'episode' ? `/series/${seriesId}/episodes/${target.episode.id}` : target ? `/series/${seriesId}/seasons/${target.season}` : '';
  const label = target?.kind === 'episode' ? `${episodeCode(target.episode.season_number, target.episode.episode_number)} – ${target.episode.title}`
    : target ? (target.season === 0 ? 'Specials' : `Season ${target.season}`) : '';

  useEffect(() => {
    setPreview(null); setDeleteFiles(false); setAck(false);
    if (!target) return;
    del<DeleteEpisodesResult>(url).then(setPreview).catch((e) => { toast(errorMessage(e), 'error'); onClose(); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  const removable = preview?.files.filter((f) => !f.shared && f.exists) ?? [];
  const shared = preview?.files.filter((f) => f.shared) ?? [];
  const needsAck = deleteFiles && removable.length > 0;

  const submit = async () => {
    setBusy(true);
    try {
      const done = await del<DeleteEpisodesResult>(url, { delete_files: deleteFiles, dry_run: false });
      toast(`Removed ${done.episodes} episode(s)${done.files_recycled ? `; ${done.files_recycled} file(s) moved to the recycle bin` : ''}`, 'success');
      onDone();
      onClose();
    } catch (e) {
      toast(errorMessage(e), 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={!!target} onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Delete {label}?</DialogTitle>
      <DialogContent>
        {!preview ? <Loading /> : (
          <Stack spacing={1.5}>
            <Typography variant="body2" color="text.secondary">
              This removes {preview.episodes} episode(s) from the library. A metadata refresh may add them back if the provider still lists them.
            </Typography>
            {removable.length > 0 && (
              <>
                <FormControlLabel label="Also move the file(s) on the server to the recycle bin"
                  control={<Checkbox checked={deleteFiles} onChange={(e) => { setDeleteFiles(e.target.checked); setAck(false); }} />} />
                <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
                  {removable.map((f) => <li key={f.path}><Box component="code" sx={{ fontSize: '0.75rem', wordBreak: 'break-all' }}>{f.path}</Box></li>)}
                </Box>
                {!deleteFiles && <Typography variant="body2" color="text.secondary">Without this the file(s) stay on disk, and a later scan may re-add them.</Typography>}
              </>
            )}
            {shared.length > 0 && (
              <Alert severity="info">
                {shared.length} file(s) also hold other episodes that are staying, so they will not be touched.
              </Alert>
            )}
            {needsAck && (
              <Alert severity="warning" icon={<Trash2 size={18} />}>
                <FormControlLabel label={`I understand ${removable.length} file(s) on the server will be moved out of the library to the recycle bin.`}
                  control={<Checkbox checked={ack} onChange={(e) => setAck(e.target.checked)} />} />
              </Alert>
            )}
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button variant="contained" color="error" onClick={submit} disabled={!preview || busy || (needsAck && !ack)}>
          {needsAck ? 'Delete episode(s) and file(s)' : 'Delete episode(s)'}
        </Button>
      </DialogActions>
    </Dialog>
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
