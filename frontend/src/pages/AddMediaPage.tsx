import {
  Box,
  Button,
  Checkbox,
  Chip,
  FormControlLabel,
  InputAdornment,
  MenuItem,
  Paper,
  Stack,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Film, Plus, PlusCircle, Search, Tv } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { get, post } from '../api/client';
import type { LookupResult, QualityProfile } from '../api/types';
import { Empty, ErrorAlert, Loading, PageHeader, Poster } from '../components/common';
import { toast, useUi } from '../store/ui';
import { palette } from '../theme';
import { errorMessage } from '../utils/format';

export default function AddMediaPage() {
  const { mode, setMode } = useUi();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [term, setTerm] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [profileId, setProfileId] = useState(1);
  const [monitor, setMonitor] = useState('all');
  const [searchOnAdd, setSearchOnAdd] = useState(false);

  const profiles = useQuery({ queryKey: ['quality-profiles'], queryFn: () => get<QualityProfile[]>('/quality-profiles') });
  const results = useQuery({
    queryKey: ['lookup', mode, submitted],
    enabled: submitted.length > 0,
    queryFn: () => get<LookupResult[]>('/lookup', { term: submitted, media_type: mode }),
  });
  const add = useMutation({
    mutationFn: (r: LookupResult) =>
      post<{ id: number; title: string }>(mode === 'movie' ? '/movies' : '/series', {
        title: r.title, year: r.year, tmdb_id: r.tmdb_id, imdb_id: r.imdb_id, tvdb_id: r.tvdb_id, tvmaze_id: r.tvmaze_id,
        quality_profile_id: profileId, monitor, search_on_add: searchOnAdd,
      }),
    onSuccess: (res) => {
      toast(`Added ${res.title}`, 'success');
      qc.invalidateQueries({ queryKey: [mode === 'movie' ? 'movies' : 'series'] });
      navigate(mode === 'movie' ? `/movies/${res.id}` : `/series/${res.id}`);
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(term.trim());
  };

  return (
    <>
      <PageHeader title="Add New" icon={<PlusCircle color={palette.purple} />} subtitle="Search metadata providers and add titles to your library" />
      <Paper component="form" onSubmit={submit} sx={{ p: 2, mb: 3 }}>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} alignItems={{ md: 'center' }}>
          <ToggleButtonGroup exclusive size="small" value={mode} onChange={(_, v) => v && setMode(v)} sx={{ flexShrink: 0, whiteSpace: 'nowrap' }}>
            <ToggleButton value="movie" sx={{ gap: 1 }}><Film size={16} /> Movies</ToggleButton>
            <ToggleButton value="tv" sx={{ gap: 1 }}><Tv size={16} /> TV Shows</ToggleButton>
          </ToggleButtonGroup>
          <TextField fullWidth size="small" autoFocus value={term} onChange={(e) => setTerm(e.target.value)}
            placeholder={mode === 'movie' ? 'e.g. Dune Part Two, tt15239678' : 'e.g. Severance, The Expanse'}
            InputProps={{ startAdornment: <InputAdornment position="start"><Search size={16} /></InputAdornment> }} />
          <Button type="submit" variant="contained" disabled={!term.trim()}>Search</Button>
        </Stack>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} sx={{ mt: 2 }} alignItems={{ md: 'center' }}>
          <TextField select size="small" label="Quality profile" value={profileId} onChange={(e) => setProfileId(Number(e.target.value))} sx={{ minWidth: 200 }}>
            {profiles.data?.map((p) => <MenuItem key={p.id} value={p.id}>{p.name}</MenuItem>)}
          </TextField>
          {mode === 'tv' && (
            <TextField select size="small" label="Monitor" value={monitor} onChange={(e) => setMonitor(e.target.value)} sx={{ minWidth: 200 }}>
              {[['all', 'All episodes'], ['future', 'Future episodes'], ['missing', 'Missing episodes'], ['latest', 'Latest season'], ['first', 'First season'], ['none', 'None']]
                .map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
            </TextField>
          )}
          <FormControlLabel control={<Checkbox checked={searchOnAdd} onChange={(e) => setSearchOnAdd(e.target.checked)} />} label="Start searching immediately" />
        </Stack>
      </Paper>

      <ErrorAlert error={results.error} />
      {results.isFetching ? (
        <Loading label="Searching metadata providers…" />
      ) : submitted && results.data?.length === 0 ? (
        <Empty icon={<Search size={40} />} title="No matches">Try a different spelling, the original title, or an IMDb id.</Empty>
      ) : (
        <Stack spacing={1.5}>
          {results.data?.map((r, i) => (
            <Paper key={`${r.provider}-${r.imdb_id ?? r.tmdb_id ?? r.tvmaze_id ?? i}`} sx={{ p: 2 }}>
              <Stack direction="row" spacing={2}>
                <Poster src={r.poster_url} title={r.title} height={130} />
                <Box sx={{ flex: 1, minWidth: 0 }}>
                  <Typography variant="h6">
                    {r.title} {r.year ? <Typography component="span" color="text.secondary">({r.year})</Typography> : null}
                  </Typography>
                  <Stack direction="row" spacing={1} sx={{ my: 1 }} flexWrap="wrap" useFlexGap>
                    <Chip size="small" label={r.provider.toUpperCase()} variant="outlined" />
                    {r.studio && <Chip size="small" label={r.studio} />}
                    {r.season_count ? <Chip size="small" label={`${r.season_count} seasons`} /> : null}
                    {r.rating ? <Chip size="small" label={`★ ${r.rating.toFixed(1)}`} /> : null}
                    {r.genres.slice(0, 3).map((g) => <Chip key={g} size="small" label={g} variant="outlined" />)}
                  </Stack>
                  <Typography variant="body2" color="text.secondary" sx={{ display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                    {r.overview || (r.cast.length ? `Starring ${r.cast.map((c) => c.name).join(', ')}` : '')}
                  </Typography>
                </Box>
                <Box>
                  <Button variant="contained" color={mode === 'movie' ? 'primary' : 'secondary'} startIcon={<Plus size={16} />}
                    disabled={add.isPending} onClick={() => add.mutate(r)}>Add</Button>
                </Box>
              </Stack>
            </Paper>
          ))}
        </Stack>
      )}
    </>
  );
}
