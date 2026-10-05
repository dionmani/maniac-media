import {
  Avatar,
  Box,
  Button,
  Checkbox,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  FormControlLabel,
  Grid,
  MenuItem,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ExternalLink, EyeOff, Plus } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { get, post } from '../api/client';
import type { DiscoverItem, QualityProfile } from '../api/types';
import { useAuth } from '../store/auth';
import { toast } from '../store/ui';
import { errorMessage } from '../utils/format';
import { reasonText, shortDate } from './DiscoverCard';
import { Fact, MediaHeader } from './MediaHeader';

const MONITOR_OPTIONS = [['all', 'All episodes'], ['future', 'Future episodes'], ['missing', 'Missing episodes'], ['latest', 'Latest season'], ['first', 'First season'], ['none', 'None']];

type Props = { item: DiscoverItem | null; onClose: () => void; onHide: (item: DiscoverItem) => void };

/** Details for a Discover title, and adding it to the library. */
export function DiscoverDialog({ item, onClose, onHide }: Props) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const canWrite = useAuth((s) => s.can)('media:write');
  const profiles = useQuery({ queryKey: ['quality-profiles'], queryFn: () => get<QualityProfile[]>('/quality-profiles'), enabled: !!item && canWrite });
  const [profileId, setProfileId] = useState<number | ''>('');
  const [monitor, setMonitor] = useState('all');
  const [searchOnAdd, setSearchOnAdd] = useState(false);

  // a returning show is usually wanted from the new season on; a new show from the start
  const reason = item?.reason;
  useEffect(() => { if (reason) setMonitor(reason === 'season_premiere' ? 'latest' : 'all'); }, [item?.tmdb_id, reason]);
  useEffect(() => {
    if (profileId === '' && profiles.data?.length) setProfileId(profiles.data.find((p) => p.id === 1)?.id ?? profiles.data[0].id);
  }, [profiles.data, profileId]);

  const movie = item?.media_type === 'movie';
  const add = useMutation({
    mutationFn: (i: DiscoverItem) =>
      post<{ id: number; title: string }>(movie ? '/movies' : '/series', {
        title: i.title, year: i.year, tmdb_id: i.tmdb_id, imdb_id: i.imdb_id, tvdb_id: i.tvdb_id, tvmaze_id: i.tvmaze_id,
        quality_profile_id: profileId || 1, monitor, search_on_add: searchOnAdd,
      }),
    onSuccess: (res) => {
      toast(`Added ${res.title}`, 'success');
      qc.invalidateQueries({ queryKey: ['discover'] });
      qc.invalidateQueries({ queryKey: [movie ? 'movies' : 'series'] });
      onClose();
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  if (!item) return null;
  const libraryPath = item.library_id ? `/${movie ? 'movies' : 'series'}/${item.library_id}` : null;
  const tmdbUrl = `https://www.themoviedb.org/${movie ? 'movie' : 'tv'}/${item.tmdb_id}`;
  const dates: [string, string | null | undefined][] = movie
    ? [[item.theatrical_release_date ? 'In theaters' : 'Release', item.theatrical_release_date ?? item.release_date], ['Digital', item.digital_release_date],
      ['Blu-ray & DVD', item.physical_release_date]]
    : [['First aired', item.release_date], ...(item.reason === 'season_premiere' ? [[`Season ${item.season_number}`, item.reason_date] as [string, string | null | undefined]] : [])];

  return (
    <Dialog open onClose={add.isPending ? undefined : onClose} maxWidth="md" fullWidth>
      <DialogContent sx={{ p: 0 }}>
        <MediaHeader
          title={item.title} year={item.year} backdrop={item.backdrop_url} poster={item.poster_url} tagline={item.tagline}
          overline={[reasonText(item), item.certification, item.runtime_minutes ? `${item.runtime_minutes} min` : null, item.studio].filter(Boolean).join(' · ')}
          chips={
            <>
              {item.in_library && <Chip size="small" color="success" label="In library" />}
              {item.rating ? <Chip size="small" label={`★ ${item.rating.toFixed(1)}`} /> : null}
              {item.genres.slice(0, 4).map((g) => <Chip key={g} size="small" label={g} variant="outlined" />)}
            </>
          }
        />
        <Box sx={{ px: 3, pb: 2 }}>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{item.overview || 'No overview yet.'}</Typography>
          <Grid container spacing={2}>
            {dates.map(([label, value]) => (
              <Grid key={label} size={{ xs: 6, sm: 4 }}><Fact label={label}>{value ? shortDate(value) : 'Not announced'}</Fact></Grid>
            ))}
            {!movie && item.season_count ? <Grid size={{ xs: 6, sm: 4 }}><Fact label="Seasons">{item.season_count}</Fact></Grid> : null}
          </Grid>
          <Fact label="Where to stream">
            {item.watch_providers.length ? (
              <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mt: 0.5 }}>
                {item.watch_providers.map((p) => (
                  <Chip key={p.name} size="small" variant="outlined" label={p.name}
                    avatar={p.logo_url ? <Avatar src={p.logo_url} alt="" variant="rounded" /> : undefined} />
                ))}
              </Stack>
            ) : 'Not on a subscription service yet'}
          </Fact>
          {canWrite && !item.in_library && (
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} alignItems={{ sm: 'center' }} sx={{ mt: 2 }}>
              <TextField select size="small" label="Quality profile" value={profileId} onChange={(e) => setProfileId(Number(e.target.value))} sx={{ minWidth: 180 }}>
                {profiles.data?.map((p) => <MenuItem key={p.id} value={p.id}>{p.name}</MenuItem>)}
              </TextField>
              {!movie && (
                <TextField select size="small" label="Monitor" value={monitor} onChange={(e) => setMonitor(e.target.value)} sx={{ minWidth: 180 }}>
                  {MONITOR_OPTIONS.map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
                </TextField>
              )}
              <FormControlLabel control={<Checkbox checked={searchOnAdd} onChange={(e) => setSearchOnAdd(e.target.checked)} />} label="Start searching immediately" />
            </Stack>
          )}
        </Box>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2, flexWrap: 'wrap', gap: 1 }}>
        <Button href={tmdbUrl} target="_blank" rel="noreferrer" startIcon={<ExternalLink size={15} />} sx={{ mr: 'auto' }}>TMDB</Button>
        {canWrite && !item.in_library && (
          <Button color="inherit" startIcon={<EyeOff size={15} />} disabled={add.isPending} onClick={() => { onHide(item); onClose(); }}>Not interested</Button>
        )}
        <Button onClick={onClose} disabled={add.isPending}>Close</Button>
        {libraryPath ? (
          <Button variant="contained" onClick={() => navigate(libraryPath)}>Open in library</Button>
        ) : canWrite ? (
          <Button variant="contained" color={movie ? 'primary' : 'secondary'} startIcon={<Plus size={16} />} disabled={add.isPending || profileId === ''}
            onClick={() => add.mutate(item)}>
            {add.isPending ? 'Adding…' : 'Add to library'}
          </Button>
        ) : null}
      </DialogActions>
    </Dialog>
  );
}
