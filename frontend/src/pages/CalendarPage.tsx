import { Box, Button, Chip, Paper, Stack, Tooltip, Typography } from '@mui/material';
import { useQuery } from '@tanstack/react-query';
import { CalendarClock, ChevronLeft, ChevronRight } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { get } from '../api/client';
import type { Episode, MovieSummary } from '../api/types';
import { ErrorAlert, Loading, PageHeader } from '../components/common';
import { palette } from '../theme';
import { episodeCode } from '../utils/format';

const iso = (d: Date) => d.toISOString().slice(0, 10);

const STATUS = {
  downloaded: { color: palette.green, label: 'Downloaded', hint: 'Episode is on disk' },
  missing: { color: palette.cyan, label: 'Monitored', hint: 'Monitored, not downloaded yet' },
  unmonitored: { color: palette.dim, label: 'Unmonitored', hint: 'Not monitored, will not be grabbed' },
  movie: { color: palette.purple, label: 'Movie', hint: 'Movie release (cinema or digital)' },
} as const;

const episodeStatus = (e: Episode) =>
  e.has_file ? STATUS.downloaded : e.monitored ? STATUS.missing : STATUS.unmonitored;

function Legend() {
  return (
    <Stack direction="row" spacing={2} useFlexGap flexWrap="wrap" sx={{ mb: 2 }}>
      {Object.values(STATUS).map((s) => (
        <Tooltip key={s.label} title={s.hint}>
          <Stack direction="row" spacing={0.75} alignItems="center">
            <Box sx={{ width: 3, height: 14, borderRadius: 1, bgcolor: s.color }} />
            <Typography variant="caption" color="text.secondary">{s.label}</Typography>
          </Stack>
        </Tooltip>
      ))}
    </Stack>
  );
}

export default function CalendarPage() {
  const [offset, setOffset] = useState(0);
  const start = useMemo(() => {
    const d = new Date();
    d.setDate(d.getDate() - d.getDay() + offset * 7);
    return d;
  }, [offset]);
  const days = Array.from({ length: 14 }, (_, i) => { const d = new Date(start); d.setDate(d.getDate() + i); return iso(d); });
  const { data, isLoading, error } = useQuery({
    queryKey: ['calendar', days[0]],
    queryFn: () => get<{ episodes: (Episode & { series_title: string })[]; movies: MovieSummary[] }>(
      '/calendar', { start: days[0], end: days[13] }),
  });
  const today = iso(new Date());

  return (
    <>
      <PageHeader title="Calendar" icon={<CalendarClock color={palette.purple} />} subtitle="Upcoming episodes and movie releases"
        actions={
          <>
            <Button variant="outlined" onClick={() => setOffset((o) => o - 1)}><ChevronLeft size={16} /></Button>
            <Button variant="outlined" onClick={() => setOffset(0)}>Today</Button>
            <Button variant="outlined" onClick={() => setOffset((o) => o + 1)}><ChevronRight size={16} /></Button>
          </>
        } />
      <ErrorAlert error={error} />
      <Legend />
      {isLoading ? <Loading /> : (
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'repeat(2, minmax(0, 1fr))', lg: 'repeat(7, minmax(0, 1fr))' }, gap: 1.5 }}>
          {days.map((day) => {
            const eps = data?.episodes.filter((e) => e.air_date === day) ?? [];
            const movies = data?.movies.filter((m) => m.release_date === day || m.digital_release_date === day) ?? [];
            return (
              <Paper key={day} sx={{ p: 1.5, minHeight: 140, borderColor: day === today ? palette.purple : undefined }}>
                <Typography variant="caption" fontWeight={700} color={day === today ? 'primary' : 'text.secondary'}>
                  {new Date(day + 'T00:00').toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })}
                </Typography>
                <Stack spacing={0.75} sx={{ mt: 1 }}>
                  {eps.map((e) => {
                    const s = episodeStatus(e);
                    return (
                      <Tooltip key={e.id} title={s.hint} placement="top" enterDelay={400}>
                        <Box component={Link} to={`/series/${e.series_id}`}
                          sx={{ display: 'block', p: 0.75, borderRadius: 1.5, textDecoration: 'none', color: 'inherit', borderLeft: `3px solid ${s.color}`, bgcolor: '#ffffff08' }}>
                          <Typography variant="caption" fontWeight={700} display="block" noWrap>{e.series_title}</Typography>
                          <Typography variant="caption" color="text.secondary" display="block" noWrap>{episodeCode(e.season_number, e.episode_number)} {e.title}</Typography>
                        </Box>
                      </Tooltip>
                    );
                  })}
                  {movies.map((m) => (
                    <Tooltip key={m.id} title={STATUS.movie.hint} placement="top" enterDelay={400}>
                      <Box component={Link} to={`/movies/${m.id}`}
                        sx={{ display: 'block', p: 0.75, borderRadius: 1.5, textDecoration: 'none', color: 'inherit', borderLeft: `3px solid ${STATUS.movie.color}`, bgcolor: '#ffffff08' }}>
                        <Typography variant="caption" fontWeight={700} display="block" noWrap>{m.title}</Typography>
                        <Chip size="small" label={m.digital_release_date === day ? 'Digital' : 'Cinema'} sx={{ height: 18, fontSize: '0.65rem' }} />
                      </Box>
                    </Tooltip>
                  ))}
                </Stack>
              </Paper>
            );
          })}
        </Box>
      )}
    </>
  );
}
