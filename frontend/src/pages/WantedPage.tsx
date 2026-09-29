import { Button, Chip, IconButton, Pagination, Stack, Tab, Table, TableBody, TableCell, TableHead, TableRow, Tabs, Tooltip, Typography } from '@mui/material';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCheck, Search, Zap } from 'lucide-react';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { get, post } from '../api/client';
import type { Episode, MovieSummary, Page } from '../api/types';
import { Empty, ErrorAlert, Loading, PageHeader } from '../components/common';
import { useAuth } from '../store/auth';
import { toast, useUi } from '../store/ui';
import { palette } from '../theme';
import { dateTime, episodeCode, errorMessage } from '../utils/format';

interface Issue { id: number; issue_type: string; media_type: string; media_id: number | null; path: string | null; details: string | null; detected_at: string }

export default function WantedPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') ?? 'missing';
  const mode = useUi((s) => s.mode);
  const can = useAuth((s) => s.can);
  const [page, setPage] = useState(1);
  const qc = useQueryClient();

  const missing = useQuery({
    queryKey: ['wanted', mode, page],
    enabled: tab === 'missing',
    placeholderData: keepPreviousData,
    queryFn: () => get<Page<MovieSummary | Episode>>('/wanted/missing', { media_type: mode, page, page_size: 50 }),
  });
  const issues = useQuery({
    queryKey: ['issues', tab, page],
    enabled: tab !== 'missing',
    placeholderData: keepPreviousData,
    queryFn: () => get<Page<Issue>>('/library/issues', { issue_type: tab === 'cutoff' ? 'upgrade_available' : undefined, page, page_size: 50 }),
  });
  const act = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['issues'] }),
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const search = (body: object, label: string) =>
    post('/search/automatic', body).then(() => toast(`Searching ${label}`, 'info')).catch((e) => toast(errorMessage(e), 'error'));

  const data = tab === 'missing' ? missing : issues;
  return (
    <>
      <PageHeader title="Wanted" icon={<Search color={palette.purple} />} subtitle="Monitored items without a file, upgrade candidates and library problems"
        actions={can('search:execute') && (
          <Button variant="contained" startIcon={<Zap size={16} />}
            onClick={() => post('/search/missing', undefined, { limit: 200 }).then(() => toast('Missing search queued (200 items, oldest-searched first)', 'success'))}>
            Search all missing
          </Button>
        )} />
      <Tabs value={tab} onChange={(_, v) => { setParams({ tab: v }); setPage(1); }} sx={{ mb: 2 }}>
        <Tab value="missing" label={`Missing ${mode === 'movie' ? 'movies' : 'episodes'}`} />
        <Tab value="cutoff" label="Cutoff unmet" />
        <Tab value="issues" label="All library issues" />
      </Tabs>
      <ErrorAlert error={data.error} />
      {data.isLoading ? <Loading /> : !data.data?.items.length ? (
        <Empty icon={<CheckCheck size={40} />} title="Nothing here">Everything that is monitored is accounted for.</Empty>
      ) : tab === 'missing' ? (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>{mode === 'movie' ? 'Movie' : 'Series'}</TableCell>
              {mode === 'tv' && <TableCell>Episode</TableCell>}
              <TableCell>{mode === 'movie' ? 'Year' : 'Air date'}</TableCell>
              <TableCell align="right" />
            </TableRow>
          </TableHead>
          <TableBody>
            {(missing.data?.items ?? []).map((item) => {
              if ('episode_number' in item) {
                const ep = item as Episode;
                return (
                  <TableRow key={ep.id} hover>
                    <TableCell><Link to={`/series/${ep.series_id}`} style={{ color: 'inherit' }}>{ep.series_title}</Link></TableCell>
                    <TableCell>{episodeCode(ep.season_number, ep.episode_number)} · {ep.title}</TableCell>
                    <TableCell>{ep.air_date ?? 'TBA'}</TableCell>
                    <TableCell align="right">
                      {can('search:execute') && (
                        <Tooltip title="Automatic search"><IconButton size="small" onClick={() => search({ media_type: 'tv', media_id: ep.series_id, season: ep.season_number, episodes: [ep.episode_number] }, episodeCode(ep.season_number, ep.episode_number))}><Zap size={15} /></IconButton></Tooltip>
                      )}
                    </TableCell>
                  </TableRow>
                );
              }
              const m = item as MovieSummary;
              return (
                <TableRow key={m.id} hover>
                  <TableCell><Link to={`/movies/${m.id}`} style={{ color: 'inherit', fontWeight: 600 }}>{m.title}</Link></TableCell>
                  <TableCell>{m.year}</TableCell>
                  <TableCell align="right">
                    {can('search:execute') && (
                      <Tooltip title="Automatic search"><IconButton size="small" onClick={() => search({ media_type: 'movie', media_id: m.id }, m.title)}><Zap size={15} /></IconButton></Tooltip>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Type</TableCell>
              <TableCell>Item</TableCell>
              <TableCell>Details</TableCell>
              <TableCell>Detected</TableCell>
              <TableCell align="right" />
            </TableRow>
          </TableHead>
          <TableBody>
            {(issues.data?.items ?? []).map((i) => (
              <TableRow key={i.id} hover>
                <TableCell><Chip size="small" label={i.issue_type.replace(/_/g, ' ')} color={i.issue_type === 'upgrade_available' ? 'secondary' : 'warning'} variant="outlined" /></TableCell>
                <TableCell>
                  {i.media_id ? <Link to={i.media_type === 'movie' ? `/movies/${i.media_id}` : `/series/${i.media_id}`} style={{ color: 'inherit' }}>{i.media_type} #{i.media_id}</Link> : '—'}
                  <Typography variant="caption" display="block" color="text.secondary" sx={{ wordBreak: 'break-all' }}>{i.path}</Typography>
                </TableCell>
                <TableCell>{i.details}</TableCell>
                <TableCell sx={{ whiteSpace: 'nowrap' }}>{dateTime(i.detected_at)}</TableCell>
                <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                  {i.issue_type === 'upgrade_available' && i.media_type === 'movie' && i.media_id && can('search:execute') && (
                    <Tooltip title="Search for an upgrade"><IconButton size="small" onClick={() => search({ media_type: 'movie', media_id: i.media_id }, 'upgrade')}><Zap size={15} /></IconButton></Tooltip>
                  )}
                  {can('media:write') && (
                    <Tooltip title="Dismiss"><IconButton size="small" onClick={() => act.mutate(() => post(`/library/issues/${i.id}/resolve`))}><CheckCheck size={15} /></IconButton></Tooltip>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {data.data && data.data.pages > 1 && (
        <Stack alignItems="center" sx={{ mt: 2 }}><Pagination count={data.data.pages} page={page} onChange={(_, p) => setPage(p)} /></Stack>
      )}
    </>
  );
}
