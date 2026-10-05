import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  List,
  ListItem,
  ListItemText,
  Stack,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Compass, EyeOff, Film, RefreshCw, Tv, Undo2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link as RouterLink, useSearchParams } from 'react-router-dom';
import { del, get, post } from '../api/client';
import type { DiscoverExclusion, DiscoverItem, DiscoverResponse } from '../api/types';
import { Empty, ErrorAlert, Loading, PageHeader } from '../components/common';
import { DiscoverCard } from '../components/DiscoverCard';
import { DiscoverDialog } from '../components/DiscoverDialog';
import { DiscoverRow } from '../components/DiscoverRow';
import { useAuth } from '../store/auth';
import { toast, useUi, type Mode } from '../store/ui';
import { palette } from '../theme';
import { errorMessage } from '../utils/format';

const ROW_LIMIT = 20;

// "New" = first seen by the server after the newest title shown on the previous visit. Both sides of the
// comparison are server timestamps, so a skewed browser clock can't mark everything new (or nothing).
const markKey = (mode: Mode) => `maniac-discover-seen-${mode}`;
const previousMarks: Partial<Record<Mode, string | null>> = {};

function previousMark(mode: Mode): string | null {
  if (!(mode in previousMarks)) {
    try {
      previousMarks[mode] = localStorage.getItem(markKey(mode));
    } catch {
      previousMarks[mode] = null; // storage unavailable: no badges rather than everything badged
    }
  }
  return previousMarks[mode] ?? null;
}

function saveMark(mode: Mode, items: DiscoverItem[]) {
  const newest = items.reduce<string | null>((max, i) => (i.first_seen_at && (!max || Date.parse(i.first_seen_at) > Date.parse(max)) ? i.first_seen_at : max), null);
  if (!newest) return;
  try {
    const stored = localStorage.getItem(markKey(mode));
    if (!stored || Date.parse(newest) > Date.parse(stored)) localStorage.setItem(markKey(mode), newest);
  } catch {
    /* storage unavailable */
  }
}

export default function DiscoverPage() {
  const { mode, setMode } = useUi();
  const can = useAuth((s) => s.can);
  const canWrite = can('media:write');
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const sectionKey = params.get('section');
  const [open, setOpen] = useState<DiscoverItem | null>(null);
  const [hiddenOpen, setHiddenOpen] = useState(false);
  const [sort, setSort] = useState<'popular' | 'date'>('popular');

  const query = useQuery({
    queryKey: ['discover', mode, sectionKey ?? 'overview'],
    queryFn: () => get<DiscoverResponse>(sectionKey ? `/discover/${mode}/${sectionKey}` : `/discover/${mode}`, sectionKey ? undefined : { limit: ROW_LIMIT }),
    staleTime: 5 * 60_000,
  });

  const since = previousMark(mode);
  const isNew = useCallback((i: DiscoverItem) => !!(since && i.first_seen_at && Date.parse(i.first_seen_at) > Date.parse(since)), [since]);
  useEffect(() => {
    if (query.data) saveMark(mode, query.data.sections.flatMap((s) => s.items));
  }, [query.data, mode]);

  const refresh = useMutation({
    mutationFn: () => post(`/discover/${mode}/refresh`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['discover', mode] }),
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const hide = useMutation({
    mutationFn: (i: DiscoverItem) => post('/discover/exclusions', { media_type: i.media_type, tmdb_id: i.tmdb_id, title: i.title, year: i.year }),
    onMutate: (i) => {
      qc.setQueriesData<DiscoverResponse>({ queryKey: ['discover', mode] }, (old) =>
        old && { ...old, sections: old.sections.map((s) => ({ ...s, items: s.items.filter((x) => x.tmdb_id !== i.tmdb_id), total: s.total - (s.items.some((x) => x.tmdb_id === i.tmdb_id) ? 1 : 0) })) });
    },
    onSuccess: (_, i) => toast(`Hid ${i.title}. Restore it from "Hidden titles".`, 'info'),
    onError: (e) => toast(errorMessage(e), 'error'),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['discover', mode] });
      qc.invalidateQueries({ queryKey: ['discover-exclusions'] });
    },
  });
  const onHide = canWrite ? hide.mutate : undefined;

  const data = query.data;
  const section = sectionKey ? data?.sections[0] : undefined;
  const sorted = useMemo(() => {
    const items = section?.items ?? [];
    if (sort === 'popular') return items;
    return [...items].sort((a, b) => (a.reason_date ?? '9999').localeCompare(b.reason_date ?? '9999'));
  }, [section, sort]);

  const actions = (
    <>
      <ToggleButtonGroup exclusive size="small" value={mode} sx={{ whiteSpace: 'nowrap' }}
        onChange={(_, v) => { if (v) { setMode(v); setParams({}); } }}>
        <ToggleButton value="movie" sx={{ gap: 1 }}><Film size={16} /> Movies</ToggleButton>
        <ToggleButton value="tv" sx={{ gap: 1 }}><Tv size={16} /> TV Shows</ToggleButton>
      </ToggleButtonGroup>
      {canWrite && <Button variant="outlined" startIcon={<EyeOff size={16} />} onClick={() => setHiddenOpen(true)}>Hidden titles</Button>}
      {canWrite && (
        <Button variant="outlined" startIcon={<RefreshCw size={16} />} disabled={refresh.isPending || query.isFetching} onClick={() => refresh.mutate()}>Refresh</Button>
      )}
    </>
  );

  return (
    <>
      <PageHeader title="Discover" icon={<Compass color={palette.purple} />} actions={actions}
        subtitle={mode === 'movie' ? 'Newly announced films, what’s in theaters, and what’s coming home to stream or buy' : 'Brand-new shows and returning shows starting a new season'} />

      <ErrorAlert error={query.error} />
      {query.isLoading ? (
        <Loading label="Finding new titles…" />
      ) : data && !data.configured ? (
        <Empty icon={<Compass size={40} />} title="Discover needs a TMDB API key">
          Add a free TMDB key under Settings → Metadata to see new and upcoming titles.{' '}
          {can('settings:read') && <Button component={RouterLink} to="/settings?tab=metadata" size="small">Open settings</Button>}
        </Empty>
      ) : sectionKey && section ? (
        <>
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
            <IconButton aria-label="Back to Discover" onClick={() => setParams({})}><ArrowLeft size={18} /></IconButton>
            <Typography variant="h6" sx={{ flex: 1 }}>{section.label} <Typography component="span" color="text.secondary">{section.total}</Typography></Typography>
            <ToggleButtonGroup exclusive size="small" value={sort} onChange={(_, v) => v && setSort(v)}>
              <ToggleButton value="popular">Popular</ToggleButton>
              <ToggleButton value="date">Date</ToggleButton>
            </ToggleButtonGroup>
          </Stack>
          {section.error && <ErrorAlert error={section.error} />}
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 2 }}>
            {sorted.map((item) => <DiscoverCard key={item.tmdb_id} item={item} isNew={isNew(item)} onOpen={setOpen} onHide={onHide} />)}
          </Box>
        </>
      ) : data ? (
        <Stack spacing={2}>
          {data.sections.map((s) => (
            <DiscoverRow key={s.key} section={s} isNew={isNew} onOpen={setOpen} onHide={onHide} onSeeAll={() => setParams({ section: s.key })} />
          ))}
        </Stack>
      ) : null}

      {data?.configured && (
        <Typography variant="caption" color="text.disabled" component="p" sx={{ mt: 3 }}>
          Release dates and streaming services for {data.region}. Data from TMDB; streaming availability by JustWatch.
          This product uses the TMDB API but is not endorsed or certified by TMDB.
        </Typography>
      )}

      <DiscoverDialog item={open} onClose={() => setOpen(null)} onHide={(i) => hide.mutate(i)} />
      <HiddenTitlesDialog open={hiddenOpen} mode={mode} onClose={() => setHiddenOpen(false)} />
    </>
  );
}

function HiddenTitlesDialog({ open, mode, onClose }: { open: boolean; mode: Mode; onClose: () => void }) {
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ['discover-exclusions', mode],
    queryFn: () => get<DiscoverExclusion[]>('/discover/exclusions', { media_type: mode }),
    enabled: open,
  });
  const restore = useMutation({
    mutationFn: (x: DiscoverExclusion) => del(`/discover/exclusions/${x.media_type}/${x.tmdb_id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discover-exclusions'] });
      qc.invalidateQueries({ queryKey: ['discover', mode] });
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  return (
    <Dialog open={open} onClose={onClose} maxWidth="xs" fullWidth>
      <DialogTitle>Hidden {mode === 'movie' ? 'movies' : 'shows'}</DialogTitle>
      <DialogContent dividers>
        {list.isLoading ? <Loading /> : list.data?.length ? (
          <List dense disablePadding>
            {list.data.map((x) => (
              <ListItem key={x.id} disableGutters secondaryAction={
                <Tooltip title="Show in Discover again">
                  <IconButton edge="end" aria-label={`Restore ${x.title}`} disabled={restore.isPending} onClick={() => restore.mutate(x)}><Undo2 size={16} /></IconButton>
                </Tooltip>
              }>
                <ListItemText primary={x.title} secondary={x.year ?? undefined} />
              </ListItem>
            ))}
          </List>
        ) : (
          <Typography variant="body2" color="text.secondary">Nothing hidden. Use “Not interested” on a title to hide it from Discover.</Typography>
        )}
      </DialogContent>
      <DialogActions><Button onClick={onClose}>Close</Button></DialogActions>
    </Dialog>
  );
}
