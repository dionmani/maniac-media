import {
  Box,
  Button,
  Card,
  CardActionArea,
  Chip,
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
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query';
import { Bookmark, BookmarkX, Film, LayoutGrid, List, Plus, RefreshCw, Search, Tv } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { get, post } from '../api/client';
import type { MovieSummary, Page, SeriesSummary } from '../api/types';
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
  const pageSize = libraryView === 'posters' ? 60 : 100;

  useEffect(() => {
    const t = setTimeout(() => setDebounced(search), 300);
    return () => clearTimeout(t);
  }, [search]);
  useEffect(() => {
    setPage(1);
    setFilter('all');
    setSort('title');
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

  const open = (i: Item) => navigate(isSeries(i) ? `/series/${i.id}` : `/movies/${i.id}`);
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
          {query.data.items.map((i) => <PosterCard key={i.id} item={i} onClick={() => open(i)} />)}
        </Box>
      ) : (
        <Table size="small" sx={{ opacity: query.isFetching ? 0.7 : 1 }}>
          <TableHead>
            <TableRow>
              <TableCell />
              <TableCell>Title</TableCell>
              <TableCell>Year</TableCell>
              <TableCell>{mode === 'movie' ? 'Quality' : 'Network'}</TableCell>
              <TableCell>{mode === 'movie' ? 'Size' : 'Episodes'}</TableCell>
              <TableCell>Status</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {query.data.items.map((i) => (
              <TableRow key={i.id} hover onClick={() => open(i)} sx={{ cursor: 'pointer' }}>
                <TableCell sx={{ width: 32 }}>{i.monitored ? <Bookmark size={15} color={palette.cyan} /> : <BookmarkX size={15} color={palette.dim} />}</TableCell>
                <TableCell sx={{ fontWeight: 600 }}>{i.title}</TableCell>
                <TableCell>{i.year || '—'}</TableCell>
                <TableCell>{isSeries(i) ? i.network ?? '—' : <QualityChip quality={i.quality} />}</TableCell>
                <TableCell>
                  {isSeries(i) ? (
                    <Stack direction="row" spacing={1} alignItems="center">
                      <LinearProgress variant="determinate" value={i.episode_count ? (i.episode_file_count / i.episode_count) * 100 : 0} sx={{ width: 90 }} />
                      <Typography variant="caption">{i.episode_file_count}/{i.episode_count}</Typography>
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
    </>
  );
}

function PosterCard({ item, onClick }: { item: Item; onClick: () => void }) {
  const series = isSeries(item);
  const progress = series ? (item.episode_count ? (item.episode_file_count / item.episode_count) * 100 : 0) : item.has_file ? 100 : 0;
  const badge = series
    ? { label: `${item.episode_file_count}/${item.episode_count}`, color: progress >= 100 ? palette.green : progress > 0 ? palette.amber : palette.red }
    : item.has_file ? { label: item.quality ?? 'On disk', color: palette.green } : { label: 'Missing', color: palette.red };
  return (
    <Card sx={{ transition: 'all .2s', '&:hover': { transform: 'translateY(-4px)', borderColor: palette.purple, boxShadow: `0 0 24px ${palette.purple}26` } }}>
      <CardActionArea onClick={onClick}>
        <Box sx={{ position: 'relative', aspectRatio: '2 / 3', bgcolor: '#181a2e', '& > *': { width: '100% !important', height: '100% !important', borderRadius: '0 !important' } }}>
          <Poster src={item.poster_path} title={item.title} height={240} />
          <Chip size="small" label={badge.label} sx={{ position: 'absolute', top: 8, right: 8, width: 'auto !important', height: '22px !important',
            bgcolor: `${badge.color}d9`, color: '#fff', backdropFilter: 'blur(6px)', borderRadius: '11px !important' }} />
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
