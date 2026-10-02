import {
  Box,
  Button,
  Card,
  CardActionArea,
  Checkbox,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  InputAdornment,
  LinearProgress,
  MenuItem,
  Pagination,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from '@mui/material';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bookmark, BookmarkX, Film, LayoutGrid, List, ListChecks, Pencil, Plus, RefreshCw, Search, Tv, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { get, post } from '../api/client';
import type { MovieSummary, Page, QualityProfile, SeriesSummary } from '../api/types';
import { Empty, ErrorAlert, Loading, PageHeader, Poster, QualityChip } from '../components/common';
import { useAuth } from '../store/auth';
import { toast, useUi } from '../store/ui';
import { palette } from '../theme';
import { bytes, errorMessage } from '../utils/format';

type Item = MovieSummary | SeriesSummary;
const isSeries = (i: Item): i is SeriesSummary => 'episode_count' in i;

const FILTERS = {
  movie: [
    { value: 'all', label: 'All' },
    { value: 'has_file', label: 'On disk' },
    { value: 'missing', label: 'Missing' },
    { value: 'monitored', label: 'Monitored' },
    { value: 'unmonitored', label: 'Unmonitored' },
  ],
  tv: [
    { value: 'all', label: 'All' },
    { value: 'continuing', label: 'Continuing' },
    { value: 'ended', label: 'Ended' },
    { value: 'monitored', label: 'Monitored' },
    { value: 'unmonitored', label: 'Unmonitored' },
  ],
};
const SORTS = {
  movie: [['title', 'Title'], ['year', 'Year'], ['added', 'Date added'], ['size', 'Size'], ['rating', 'Rating'], ['runtime', 'Runtime']],
  tv: [['title', 'Title'], ['year', 'Year'], ['added', 'Date added'], ['rating', 'Rating']],
};

export default function LibraryPage() {
  const { mode, libraryView, setLibraryView } = useUi();
  const can = useAuth((s) => s.can);
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const [debounced, setDebounced] = useState('');
  const [filter, setFilter] = useState('all');
  const [sort, setSort] = useState('title');
  const [order, setOrder] = useState<'asc' | 'desc'>('asc');
  const [page, setPage] = useState(1);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [editing, setEditing] = useState(false);
  const pageSize = libraryView === 'posters' ? 60 : 100;

  useEffect(() => {
    const t = setTimeout(() => setDebounced(search), 300);
    return () => clearTimeout(t);
  }, [search]);
  useEffect(() => {
    setPage(1);
    setFilter('all');
    setSort('title');
    setSelecting(false);
    setSelected(new Set());
  }, [mode]);
  useEffect(() => setPage(1), [debounced, filter, sort, order]);

  const params: Record<string, string | number | boolean | undefined> = { page, page_size: pageSize, sort, order, search: debounced || undefined };
  if (filter === 'has_file') params.has_file = true;
  if (filter === 'missing') params.has_file = false;
  if (filter === 'monitored') params.monitored = true;
  if (filter === 'unmonitored') params.monitored = false;
  if (filter === 'continuing' || filter === 'ended') params.status = filter;

  const query = useQuery({
    queryKey: [mode === 'movie' ? 'movies' : 'series', params],
    queryFn: () => get<Page<Item>>(mode === 'movie' ? '/movies' : '/series', params),
    placeholderData: keepPreviousData,
  });

  const scan = useMutation({
    mutationFn: () => post<{ message: string }>('/library/scan', { scope: mode === 'movie' ? 'movies' : 'tv', incremental: true }),
    onSuccess: (r) => toast(r.message, 'success'),
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const toggle = (id: number) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const stopSelecting = () => { setSelecting(false); setSelected(new Set()); };
  const pageIds = query.data?.items.map((i) => i.id) ?? [];
  const allOnPage = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
  const togglePage = () => setSelected((prev) => {
    const next = new Set(prev);
    pageIds.forEach((id) => (allOnPage ? next.delete(id) : next.add(id)));
    return next;
  });
  const open = (i: Item) => (selecting ? toggle(i.id) : navigate(isSeries(i) ? `/series/${i.id}` : `/movies/${i.id}`));
  const Icon = mode === 'movie' ? Film : Tv;

  return (
    <>
      <PageHeader
        title={mode === 'movie' ? 'Movies' : 'TV Shows'}
        icon={<Icon color={mode === 'movie' ? palette.purple : palette.cyan} />}
        subtitle={query.data ? `${query.data.total.toLocaleString()} ${mode === 'movie' ? 'movies' : 'series'} in your library` : undefined}
        actions={
          <>
            {can('library:scan') && (
              <Button variant="outlined" startIcon={<RefreshCw size={16} />} onClick={() => scan.mutate()} disabled={scan.isPending}>
                Scan folder
              </Button>
            )}
            {can('media:write') && (
              <Button variant={selecting ? 'contained' : 'outlined'} color="inherit" startIcon={<ListChecks size={16} />} onClick={() => (selecting ? stopSelecting() : setSelecting(true))}>
                {selecting ? 'Done' : 'Select'}
              </Button>
            )}
            {can('media:write') && (
              <Button variant="contained" color={mode === 'movie' ? 'primary' : 'secondary'} startIcon={<Plus size={16} />} onClick={() => navigate('/add')}>
                Add {mode === 'movie' ? 'movie' : 'series'}
              </Button>
            )}
          </>
        }
      />

      <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} sx={{ mb: 3 }} alignItems={{ md: 'center' }}>
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ flex: 1 }}>
          {FILTERS[mode].map((f) => (
            <Chip key={f.value} label={f.label} onClick={() => setFilter(f.value)} color={filter === f.value ? 'secondary' : 'default'}
              variant={filter === f.value ? 'filled' : 'outlined'} />
          ))}
        </Stack>
        <TextField select size="small" label="Sort" value={sort} onChange={(e) => setSort(e.target.value)} sx={{ minWidth: 140 }}>
          {SORTS[mode].map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
        </TextField>
        <ToggleButtonGroup size="small" exclusive value={order} onChange={(_, v) => v && setOrder(v)}>
          <ToggleButton value="asc">Asc</ToggleButton>
          <ToggleButton value="desc">Desc</ToggleButton>
        </ToggleButtonGroup>
        <TextField size="small" placeholder="Filter by title…" value={search} onChange={(e) => setSearch(e.target.value)} sx={{ minWidth: 240 }}
          InputProps={{ startAdornment: <InputAdornment position="start"><Search size={16} /></InputAdornment> }} />
        <ToggleButtonGroup size="small" exclusive value={libraryView} onChange={(_, v) => v && setLibraryView(v)}>
          <ToggleButton value="posters"><Tooltip title="Posters"><LayoutGrid size={16} /></Tooltip></ToggleButton>
          <ToggleButton value="table"><Tooltip title="Table"><List size={16} /></Tooltip></ToggleButton>
        </ToggleButtonGroup>
      </Stack>

      <ErrorAlert error={query.error} />
      {query.isLoading ? (
        <Loading />
      ) : !query.data?.items.length ? (
        <Empty icon={<Icon size={48} />} title={`No ${mode === 'movie' ? 'movies' : 'series'} found`}>
          Scan your {mode === 'movie' ? 'movie' : 'TV'} folder or add titles from the metadata providers.
        </Empty>
      ) : libraryView === 'posters' ? (
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 2, opacity: query.isFetching ? 0.7 : 1 }}>
          {query.data.items.map((i) => <PosterCard key={i.id} item={i} onClick={() => open(i)} selecting={selecting} selected={selected.has(i.id)} />)}
        </Box>
      ) : (
        <Table size="small" sx={{ opacity: query.isFetching ? 0.7 : 1 }}>
          <TableHead>
            <TableRow>
              <TableCell sx={{ width: 32 }}>
                {selecting && <Checkbox size="small" checked={allOnPage} indeterminate={!allOnPage && pageIds.some((id) => selected.has(id))} onChange={togglePage} />}
              </TableCell>
              <TableCell>Title</TableCell>
              <TableCell>Year</TableCell>
              <TableCell>{mode === 'movie' ? 'Quality' : 'Network'}</TableCell>
              <TableCell>{mode === 'movie' ? 'Size' : 'Episodes'}</TableCell>
              <TableCell>Status</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {query.data.items.map((i) => (
              <TableRow key={i.id} hover selected={selected.has(i.id)} onClick={() => open(i)} sx={{ cursor: 'pointer' }}>
                <TableCell sx={{ width: 32 }}>
                  {selecting ? <Checkbox size="small" checked={selected.has(i.id)} />
                    : i.monitored ? <Bookmark size={15} color={palette.cyan} /> : <BookmarkX size={15} color={palette.dim} />}
                </TableCell>
                <TableCell sx={{ fontWeight: 600 }}>{i.title}</TableCell>
                <TableCell>{i.year || '—'}</TableCell>
                <TableCell>{isSeries(i) ? i.network ?? '—' : <QualityChip quality={i.quality} />}</TableCell>
                <TableCell>
                  {isSeries(i) ? (
                    <Stack direction="row" spacing={1} alignItems="center">
                      <LinearProgress variant="determinate" value={i.aired_episode_count ? (i.episode_file_count / i.aired_episode_count) * 100 : 0} sx={{ width: 90 }} />
                      <Typography variant="caption">{i.episode_file_count}/{i.aired_episode_count}</Typography>
                    </Stack>
                  ) : bytes(i.file_size_bytes)}
                </TableCell>
                <TableCell>
                  {isSeries(i) ? <Chip size="small" label={i.status ?? 'continuing'} variant="outlined" />
                    : i.has_file ? <Chip size="small" color="success" label="On disk" /> : <Chip size="small" color="error" label="Missing" variant="outlined" />}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {query.data && query.data.pages > 1 && (
        <Stack alignItems="center" sx={{ mt: 3 }}>
          <Pagination count={query.data.pages} page={page} onChange={(_, p) => { setPage(p); window.scrollTo({ top: 0 }); }} color="primary" />
        </Stack>
      )}

      {selecting && (
        <Stack direction="row" spacing={1.5} alignItems="center" sx={{ position: 'sticky', bottom: 16, mt: 3, p: 1.5, borderRadius: 2, bgcolor: 'background.paper', border: 1, borderColor: 'divider', boxShadow: 6, zIndex: 5 }}>
          <Typography fontWeight={700}>{selected.size} selected</Typography>
          <Button size="small" onClick={togglePage}>{allOnPage ? 'Unselect page' : 'Select page'}</Button>
          <Button size="small" onClick={() => setSelected(new Set())} disabled={!selected.size}>Clear</Button>
          <Box sx={{ flex: 1 }} />
          <Button variant="contained" startIcon={<Pencil size={16} />} disabled={!selected.size} onClick={() => setEditing(true)}>Edit selected</Button>
          <Tooltip title="Exit select mode"><Button size="small" color="inherit" onClick={stopSelecting}><X size={16} /></Button></Tooltip>
        </Stack>
      )}
      <BulkEditDialog open={editing} mode={mode} ids={[...selected]} onClose={() => setEditing(false)} onDone={stopSelecting} />
    </>
  );
}

function BulkEditDialog({ open, mode, ids, onClose, onDone }: { open: boolean; mode: 'movie' | 'tv'; ids: number[]; onClose: () => void; onDone: () => void }) {
  const qc = useQueryClient();
  const profiles = useQuery({ queryKey: ['quality-profiles'], queryFn: () => get<QualityProfile[]>('/quality-profiles'), enabled: open });
  const [profile, setProfile] = useState('');
  const [monitored, setMonitored] = useState('');
  const [newSeasons, setNewSeasons] = useState('');
  const apply = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = { ids };
      if (profile) body.quality_profile_id = Number(profile);
      if (monitored) body.monitored = monitored === 'yes';
      if (newSeasons) body.monitor_new_seasons = newSeasons === 'yes';
      return post<{ updated: number }>(mode === 'movie' ? '/movies/bulk' : '/series/bulk', body);
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: [mode === 'movie' ? 'movies' : 'series'] });
      toast(`Updated ${r.updated} ${mode === 'movie' ? 'movie(s)' : 'series'}`, 'success');
      setProfile(''); setMonitored(''); setNewSeasons('');
      onClose();
      onDone();
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const nothing = !profile && !monitored && !newSeasons;
  return (
    <Dialog open={open} onClose={apply.isPending ? undefined : onClose} maxWidth="xs" fullWidth>
      <DialogTitle>Edit {ids.length} {mode === 'movie' ? 'movie(s)' : 'series'}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField select size="small" label="Quality profile" value={profile} onChange={(e) => setProfile(e.target.value)}>
            <MenuItem value="">No change</MenuItem>
            {profiles.data?.map((p) => <MenuItem key={p.id} value={String(p.id)}>{p.name}</MenuItem>)}
          </TextField>
          <TextField select size="small" label="Monitored" value={monitored} onChange={(e) => setMonitored(e.target.value)}>
            <MenuItem value="">No change</MenuItem>
            <MenuItem value="yes">Monitored</MenuItem>
            <MenuItem value="no">Unmonitored</MenuItem>
          </TextField>
          {mode === 'tv' && (
            <TextField select size="small" label="Monitor new seasons" value={newSeasons} onChange={(e) => setNewSeasons(e.target.value)}>
              <MenuItem value="">No change</MenuItem>
              <MenuItem value="yes">Yes</MenuItem>
              <MenuItem value="no">No</MenuItem>
            </TextField>
          )}
          <Typography variant="caption" color="text.secondary">Only the fields you change are applied; everything else is left as it is.</Typography>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={apply.isPending}>Cancel</Button>
        <Button variant="contained" onClick={() => apply.mutate()} disabled={nothing || apply.isPending}>Apply to {ids.length}</Button>
      </DialogActions>
    </Dialog>
  );
}

function PosterCard({ item, onClick, selecting, selected }: { item: Item; onClick: () => void; selecting: boolean; selected: boolean }) {
  const series = isSeries(item);
  const progress = series ? (item.aired_episode_count ? (item.episode_file_count / item.aired_episode_count) * 100 : 0) : item.has_file ? 100 : 0;
  const badge = series
    ? { label: `${item.episode_file_count}/${item.aired_episode_count}`, color: progress >= 100 ? palette.green : progress > 0 ? palette.amber : palette.red }
    : item.has_file ? { label: item.quality ?? 'On disk', color: palette.green } : { label: 'Missing', color: palette.red };
  return (
    <Card sx={{ transition: 'all .2s', ...(selected && { borderColor: palette.cyan, boxShadow: `0 0 0 2px ${palette.cyan}` }), '&:hover': { transform: 'translateY(-4px)', borderColor: palette.purple, boxShadow: `0 0 24px ${palette.purple}26` } }}>
      <CardActionArea onClick={onClick}>
        <Box sx={{ position: 'relative', aspectRatio: '2 / 3', bgcolor: '#181a2e', '& > *': { width: '100% !important', height: '100% !important', borderRadius: '0 !important' } }}>
          <Poster src={item.poster_path} title={item.title} height={240} />
          <Chip size="small" label={badge.label} sx={{ position: 'absolute', top: 8, right: 8, width: 'auto !important', height: '22px !important',
            bgcolor: `${badge.color}d9`, color: '#fff', backdropFilter: 'blur(6px)', borderRadius: '11px !important' }} />
          {selecting && (
            <Checkbox checked={selected} size="small" sx={{ position: 'absolute', top: 36, left: 4, width: 'auto !important', height: 'auto !important', color: '#fff', bgcolor: '#000a', p: 0.25, borderRadius: '6px !important' }} />
          )}
          {!item.monitored && (
            <Chip size="small" label="Unmonitored" sx={{ position: 'absolute', top: 8, left: 8, width: 'auto !important', height: '22px !important', bgcolor: '#000a', borderRadius: '11px !important' }} />
          )}
        </Box>
        <LinearProgress variant="determinate" value={progress} sx={{ borderRadius: 0, height: 3 }} />
        <Box sx={{ p: 1.25 }}>
          <Typography variant="body2" fontWeight={700} noWrap title={item.title}>{item.title}</Typography>
          <Typography variant="caption" color="text.secondary" noWrap display="block">
            {item.year || '—'} · {series ? item.network ?? 'TV' : item.runtime_minutes ? `${item.runtime_minutes} min` : 'Movie'}
          </Typography>
        </Box>
      </CardActionArea>
    </Card>
  );
}
