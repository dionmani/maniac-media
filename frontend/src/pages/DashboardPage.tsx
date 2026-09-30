import { Box, Chip, Grid, LinearProgress, Stack, Table, TableBody, TableCell, TableRow, Typography } from '@mui/material';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, Download, Film, HardDrive, LayoutDashboard, SearchX, Tv } from 'lucide-react';
import { Link } from 'react-router-dom';
import { get } from '../api/client';
import type { Dashboard } from '../api/types';
import { ErrorAlert, Loading, PageHeader, Poster, Section, StatCard, StatusChip } from '../components/common';
import { palette } from '../theme';
import { bytes, episodeCode, relative } from '../utils/format';

const ISSUE_LABELS: Record<string, string> = {
  missing_file: 'Missing files',
  duplicate_file: 'Duplicate files',
  upgrade_available: 'Upgrade available',
  corrupt_file: 'Corrupt files',
};

export default function DashboardPage() {
  const { data, isLoading, error } = useQuery({ queryKey: ['dashboard'], queryFn: () => get<Dashboard>('/dashboard'), refetchInterval: 60_000 });
  if (isLoading) return <Loading />;
  if (!data) return <ErrorAlert error={error} />;

  const movieCoverage = data.movies.total ? (data.movies.with_file / data.movies.total) * 100 : 0;
  const tvCoverage = data.series.episodes ? (data.series.episodes_with_file / data.series.episodes) * 100 : 0;

  return (
    <>
      <PageHeader title="Dashboard" icon={<LayoutDashboard color={palette.purple} />} subtitle="Library health, activity and storage at a glance" />
      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid size={{ xs: 6, md: 4, lg: 2 }}>
          <StatCard label="Movies" value={data.movies.total.toLocaleString()} icon={<Film size={20} />} hint={`${movieCoverage.toFixed(1)}% on disk`} />
        </Grid>
        <Grid size={{ xs: 6, md: 4, lg: 2 }}>
          <StatCard label="TV Shows" value={data.series.total.toLocaleString()} color={palette.cyan} icon={<Tv size={20} />}
            hint={`${data.series.episodes.toLocaleString()} episodes`} />
        </Grid>
        <Grid size={{ xs: 6, md: 4, lg: 2 }}>
          <StatCard label="Missing" value={data.missing_total.toLocaleString()} color={palette.red} icon={<SearchX size={20} />}
            hint={`${data.movies.missing} movies · ${data.series.episodes_missing} episodes`} />
        </Grid>
        <Grid size={{ xs: 6, md: 4, lg: 2 }}>
          <StatCard label="Active downloads" value={data.downloads.active} color={palette.amber} icon={<Download size={20} />} />
        </Grid>
        <Grid size={{ xs: 6, md: 4, lg: 2 }}>
          <StatCard label="Storage used" value={bytes(data.storage.library_bytes)} color={palette.green} icon={<HardDrive size={20} />}
            hint={`${bytes(data.storage.movies_bytes)} movies · ${bytes(data.storage.tv_bytes)} TV`} />
        </Grid>
        <Grid size={{ xs: 6, md: 4, lg: 2 }}>
          <StatCard label="Failed imports" value={data.failed_imports} color={data.failed_imports ? palette.red : palette.muted}
            icon={<AlertTriangle size={20} />} hint={`${data.failed_last_7_days} failures in 7 days`} />
        </Grid>
      </Grid>

      <Grid container spacing={2}>
        <Grid size={{ xs: 12, lg: 7 }}>
          <Section title="Recent activity" actions={<Chip size="small" component={Link} to="/activity?tab=history" clickable label="All history" />}>
            {data.recent_activity.length === 0 ? (
              <Typography color="text.secondary">Nothing has happened yet.</Typography>
            ) : (
              <Table size="small">
                <TableBody>
                  {data.recent_activity.map((h) => (
                    <TableRow key={h.id}>
                      <TableCell sx={{ width: 110 }}><StatusChip status={h.event_type} /></TableCell>
                      <TableCell>
                        <Typography variant="body2" fontWeight={600}>{h.title}</Typography>
                        <Typography variant="caption" color="text.secondary" sx={{ wordBreak: 'break-all' }}>{h.details}</Typography>
                      </TableCell>
                      <TableCell sx={{ whiteSpace: 'nowrap', color: 'text.secondary', width: 120 }}>{relative(h.timestamp)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Section>
        </Grid>
        <Grid size={{ xs: 12, lg: 5 }}>
          <Stack spacing={2}>
            <Section title="Library coverage">
              <Coverage label="Movies" value={movieCoverage} detail={`${data.movies.with_file} / ${data.movies.total}`} />
              <Coverage label="Episodes" value={tvCoverage} detail={`${data.series.episodes_with_file} / ${data.series.episodes}`} />
            </Section>
            <Section title="Disks">
              {data.storage.disks.map((d) => (
                <Coverage key={d.path} label={d.path} value={(d.used / d.total) * 100} detail={`${bytes(d.free)} free of ${bytes(d.total)}`}
                  warn={d.free < 20 * 1024 ** 3} />
              ))}
            </Section>
            {Object.keys(data.issues).length > 0 && (
              <Section title="Library issues" actions={<Chip size="small" component={Link} to="/issues" clickable label="Review" />}>
                <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                  {Object.entries(data.issues).map(([k, v]) => (
                    <Chip key={k} label={`${ISSUE_LABELS[k] ?? k}: ${v}`} color={k === 'upgrade_available' ? 'secondary' : 'warning'} variant="outlined" />
                  ))}
                </Stack>
              </Section>
            )}
            <Section title="Airing this week">
              {data.upcoming.length === 0 ? (
                <Typography color="text.secondary" variant="body2">No monitored episodes airing in the next 7 days.</Typography>
              ) : (
                <Stack spacing={1.5}>
                  {data.upcoming.map((u) => (
                    <Stack key={u.episode_id} direction="row" spacing={1.5} alignItems="center" component={Link} to={`/series/${u.series_id}`}
                      sx={{ color: 'inherit', textDecoration: 'none' }}>
                      <Poster src={u.poster} title={u.series} height={54} />
                      <Box sx={{ flex: 1, minWidth: 0 }}>
                        <Typography variant="body2" fontWeight={600} noWrap>{u.series}</Typography>
                        <Typography variant="caption" color="text.secondary" noWrap display="block">
                          {episodeCode(u.season, u.episode)} · {u.title}
                        </Typography>
                      </Box>
                      <Chip size="small" label={u.air_date} />
                    </Stack>
                  ))}
                </Stack>
              )}
            </Section>
          </Stack>
        </Grid>
      </Grid>
    </>
  );
}

function Coverage({ label, value, detail, warn }: { label: string; value: number; detail: string; warn?: boolean }) {
  return (
    <Box sx={{ mb: 2 }}>
      <Stack direction="row" justifyContent="space-between" sx={{ mb: 0.5 }}>
        <Typography variant="body2" fontWeight={600} noWrap sx={{ maxWidth: '60%' }}>{label}</Typography>
        <Typography variant="caption" color={warn ? 'error' : 'text.secondary'}>{detail}</Typography>
      </Stack>
      <LinearProgress variant="determinate" value={Math.min(100, value)} />
    </Box>
  );
}
