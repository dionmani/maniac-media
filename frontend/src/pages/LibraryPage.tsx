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
import { memo, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
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
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(search), 300);
    return () => clearTimeout(t);
  }, [search]);
  useEffect(() => {
    setFilter('all');
    setSort('title');
    setSelecting(false);
    setSelected(new Set());
  }, [mode]);

  const params: Record<string, string | number | boolean | undefined> = { sort, order, search: debounced || undefined };
  if (filter === 'has_file') params.has_file = true;
  if (filter === 'missing') params.has_file = false;
  if (filter === 'monitored') params.monitored = true;
  if (filter === 'unmonitored') params.monitored = false;
  if (filter === 'continuing' || filter === 'ended') params.status = filter;

  const query = useQuery({
    queryKey: [mode === 'movie' ? 'movies' : 'series', 'all', params],
    queryFn: () => fetchAll(mode === 'movie' ? '/movies' : '/series', params),
    placeholderData: keepPreviousData,
  });
  const items = useMemo(() => query.data ?? [], [query.data]);

  const scan = useMutation({
    mutationFn: () => post<{ message: string }>('/library/scan', { scope: mode === 'movie' ? 'movies' : 'tv', incremental: true }),
    onSuccess: (r) => toast(r.message, 'success'),
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const toggle = useCallback((id: number) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  }), []);
  const stopSelecting = () => { setSelecting(false); setSelected(new Set()); };
  const allSelected = items.length > 0 && items.every((i) => selected.has(i.id));
  const selectAll = () => setSelected((prev) => new Set([...prev, ...items.map((i) => i.id)]));
  const unselectAll = () => setSelected(new Set());
  const open = useCallback(
    (i: Item) => (selecting ? toggle(i.id) : navigate(isSeries(i) ? `/series/${i.id}` : `/movies/${i.id}`)),
    [selecting, toggle, navigate],
  );
  const Icon = mode === 'movie' ? Film : Tv;

  // The first item of each letter is a jump target; only meaningful while the list is sorted by title.
  const anchors = useMemo(() => {
    const byLetter = new Map<string, number>();
    if (sort === 'title') items.forEach((i) => { const l = jumpLetter(i); if (!byLetter.has(l)) byLetter.set(l, i.id); });
    return byLetter;
  }, [items, sort]);
  const anchorOf = useMemo(() => new Map([...anchors].map(([l, id]) => [id, l])), [anchors]);
  const showJump = anchors.size > 1;

  // Track the sticky header's height so jump targets land just below it and the jump bar sits beneath it.
  const headerRef = useRef<HTMLDivElement>(null);
  const [headerHeight, setHeaderHeight] = useState(0);
  useEffect(() => {
    const el = headerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setHeaderHeight(el.offsetHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <Box style={{ '--lib-header-h': `${headerHeight}px` } as CSSProperties}>
      {/* Header + filters stay pinned (desktop) while the library scrolls underneath. On phones the
          filter bar wraps into a tall column, so it scrolls away normally there. */}
      <Box ref={headerRef} sx={{
        position: { xs: 'static', md: 'sticky' }, top: 0, zIndex: 7, bgcolor: 'background.default',
        mx: { xs: -2, md: -4 }, px: { xs: 2, md: 4 }, mt: { xs: -8, md: -4 }, pt: { xs: 8, md: 4 }, pb: 2, mb: 3,
        borderBottom: 1, borderColor: 'divider',
      }}>
        <PageHeader
          title={mode === 'movie' ? 'Movies' : 'TV Shows'}
          icon={<Icon color={mode === 'movie' ? palette.purple : palette.cyan} />}
          subtitle={query.data ? `${items.length.toLocaleString()} ${mode === 'movie' ? 'movies' : 'series'} in your library` : undefined}
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

        <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} alignItems={{ md: 'center' }}>
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
      </Box>

      <ErrorAlert error={query.error} />
      <Box sx={{ pr: showJump ? { xs: 3, md: 4 } : 0 }}>
        {query.isLoading ? (
          <Loading />
        ) : !items.length ? (
          <Empty icon={<Icon size={48} />} title={`No ${mode === 'movie' ? 'movies' : 'series'} found`}>
            Scan your {mode === 'movie' ? 'movie' : 'TV'} folder or add titles from the metadata providers.
          </Empty>
        ) : libraryView === 'posters' ? (
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 2, opacity: query.isFetching ? 0.7 : 1 }}>
            {items.map((i) => <PosterCard key={i.id} item={i} onOpen={open} selecting={selecting} selected={selected.has(i.id)} anchor={anchorOf.get(i.id)} />)}
          </Box>
        ) : (
          <Table size="small" sx={{ opacity: query.isFetching ? 0.7 : 1 }}>
            <TableHead>
              <TableRow>
                <TableCell sx={{ width: 32 }}>
                  {selecting && <Checkbox size="small" checked={allSelected} indeterminate={!allSelected && selected.size > 0} onChange={allSelected ? unselectAll : selectAll} />}
                </TableCell>
                <TableCell>Title</TableCell>
                <TableCell>Year</TableCell>
                <TableCell>{mode === 'movie' ? 'Quality' : 'Network'}</TableCell>
                <TableCell>{mode === 'movie' ? 'Size' : 'Episodes'}</TableCell>
                <TableCell>Status</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {items.map((i) => <LibraryRow key={i.id} item={i} onOpen={open} selecting={selecting} selected={selected.has(i.id)} anchor={anchorOf.get(i.id)} />)}
            </TableBody>
          </Table>
        )}
      </Box>

      {showJump && <JumpBar available={anchors} />}

      {selecting && (
        <Stack direction="row" spacing={1.5} alignItems="center" sx={{ position: 'sticky', bottom: 16, mt: 3, p: 1.5, borderRadius: 2, bgcolor: 'background.paper', border: 1, borderColor: 'divider', boxShadow: 6, zIndex: 5 }}>
          <Typography fontWeight={700}>{selected.size} selected</Typography>
          <Button size="small" onClick={selectAll} disabled={allSelected}>Select all ({items.length.toLocaleString()})</Button>
          <Button size="small" onClick={unselectAll} disabled={!selected.size}>Unselect all</Button>
          <Box sx={{ flex: 1 }} />
          <Button variant="contained" startIcon={<Pencil size={16} />} disabled={!selected.size} onClick={() => setEditing(true)}>Edit selected</Button>
          <Tooltip title="Exit select mode"><Button size="small" color="inherit" onClick={stopSelecting}><X size={16} /></Button></Tooltip>
        </Stack>
      )}
      <BulkEditDialog open={editing} mode={mode} ids={[...selected]} onClose={() => setEditing(false)} onDone={stopSelecting} />
    </Box>
  );
}

const FETCH_PAGE_SIZE = 500; // the API's per-request cap

/** Loads every page of a library listing so the whole library renders as one scrollable list. */
async function fetchAll(path: string, params: Record<string, string | number | boolean | undefined>): Promise<Item[]> {
  const first = await get<Page<Item>>(path, { ...params, page: 1, page_size: FETCH_PAGE_SIZE });
  const rest = await Promise.all(
    Array.from({ length: first.pages - 1 }, (_, n) => get<Page<Item>>(path, { ...params, page: n + 2, page_size: FETCH_PAGE_SIZE })),
  );
  // Dedupe in case the library changed between page requests.
  const seen = new Set<number>();
  return [first, ...rest].flatMap((p) => p.items).filter((i) => !seen.has(i.id) && !!seen.add(i.id));
}

const LETTERS = ['#', ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'];

/** Jump-bar bucket for an item, matching the server's title sort (sort_title moves a leading "The"/"A"/"An" to the end). */
function jumpLetter(i: Item): string {
  const c = (i.sort_title || i.title).trim().charAt(0).toUpperCase();
  return c >= 'A' && c <= 'Z' ? c : '#';
}

// Phones: clear the floating menu button. Desktop: clear the sticky header.
const SCROLL_MARGIN = { xs: 72, md: 'calc(var(--lib-header-h) + 16px)' };

const anchorId = (letter: string) => `library-jump-${letter === '#' ? 'num' : letter}`;

function JumpBar({ available }: { available: Map<string, number> }) {
  const jump = (letter: string) => document.getElementById(anchorId(letter))?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  return (
    <Stack component="nav" aria-label="Jump to letter" sx={{
      position: 'fixed', right: { xs: 2, md: 8 }, transform: 'translateY(-50%)', zIndex: 6, py: 0.75, px: 0.25,
      // Centred in the space below the sticky header on desktop; centred in the viewport on phones.
      top: { xs: '50%', md: 'calc((100vh + var(--lib-header-h)) / 2)' },
      maxHeight: { xs: 'calc(100vh - 32px)', md: 'calc(100vh - var(--lib-header-h) - 32px)' },
      borderRadius: 3, bgcolor: 'background.paper', border: 1, borderColor: 'divider', boxShadow: 4, overflowY: 'auto',
    }}>
      {LETTERS.map((l) => {
        const enabled = available.has(l);
        return (
          <Box key={l} component="button" type="button" disabled={!enabled} onClick={() => jump(l)} sx={{
            all: 'unset', cursor: enabled ? 'pointer' : 'default', textAlign: 'center', width: { xs: 18, md: 22 }, lineHeight: { xs: '15px', md: '18px' },
            fontSize: { xs: 10, md: 11 }, fontWeight: 700, borderRadius: 1, color: enabled ? 'text.primary' : 'text.disabled',
            '&:hover:enabled': { bgcolor: palette.purple, color: '#fff' }, '&:focus-visible': { outline: `2px solid ${palette.cyan}` },
          }}>
            {l}
          </Box>
        );
      })}
    </Stack>
  );
}

type CardProps = { item: Item; onOpen: (i: Item) => void; selecting: boolean; selected: boolean; anchor?: string };

// Memoized so toggling one checkbox in a large library doesn't re-render every row/card.
const LibraryRow = memo(function LibraryRow({ item: i, onOpen, selecting, selected, anchor }: CardProps) {
  return (
    <TableRow id={anchor ? anchorId(anchor) : undefined} hover selected={selected} onClick={() => onOpen(i)} sx={{ cursor: 'pointer', scrollMarginTop: SCROLL_MARGIN }}>
      <TableCell sx={{ width: 32 }}>
        {selecting ? <Checkbox size="small" checked={selected} />
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
  );
});

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

const PosterCard = memo(function PosterCard({ item, onOpen, selecting, selected, anchor }: CardProps) {
  const series = isSeries(item);
  const progress = series ? (item.aired_episode_count ? (item.episode_file_count / item.aired_episode_count) * 100 : 0) : item.has_file ? 100 : 0;
  const badge = series
    ? { label: `${item.episode_file_count}/${item.aired_episode_count}`, color: progress >= 100 ? palette.green : progress > 0 ? palette.amber : palette.red }
    : item.has_file ? { label: item.quality ?? 'On disk', color: palette.green } : { label: 'Missing', color: palette.red };
  return (
    // content-visibility lets the browser skip layout/paint for off-screen cards when the whole library is rendered.
    <Card id={anchor ? anchorId(anchor) : undefined} sx={{ contentVisibility: 'auto', containIntrinsicSize: 'auto 320px', scrollMarginTop: SCROLL_MARGIN, transition: 'all .2s', ...(selected && { borderColor: palette.cyan, boxShadow: `0 0 0 2px ${palette.cyan}` }), '&:hover': { transform: 'translateY(-4px)', borderColor: palette.purple, boxShadow: `0 0 24px ${palette.purple}26` } }}>
      <CardActionArea onClick={() => onOpen(item)}>
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
});
