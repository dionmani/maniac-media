import { Box, Button, Chip, FormControlLabel, Grid, MenuItem, Switch, Table, TableBody, TableCell, TableRow, TextField, Typography } from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, FileEdit, RefreshCw, Search, Trash2, Zap } from 'lucide-react';
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { del, get, patch, post } from '../api/client';
import type { HistoryItem, Movie, Page, QualityProfile } from '../api/types';
import { ErrorAlert, Loading, QualityChip, Section, StatusChip, useConfirm } from '../components/common';
import InteractiveSearchDialog, { type SearchTarget } from '../components/InteractiveSearchDialog';
import { CastList, Fact, MediaHeader, genreChips } from '../components/MediaHeader';
import { useAuth } from '../store/auth';
import { toast } from '../store/ui';
import { bytes, errorMessage, relative } from '../utils/format';

export default function MovieDetailPage() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const can = useAuth((s) => s.can);
  const { confirm, dialog } = useConfirm();
  const [search, setSearch] = useState<SearchTarget | null>(null);
  const [deleteFiles, setDeleteFiles] = useState(false);

  const movie = useQuery({ queryKey: ['movie', id], queryFn: () => get<Movie>(`/movies/${id}`) });
  const profiles = useQuery({ queryKey: ['quality-profiles'], queryFn: () => get<QualityProfile[]>('/quality-profiles') });
  const history = useQuery({ queryKey: ['history', 'movie', id], queryFn: () => get<Page<HistoryItem>>('/history', { media_type: 'movie', media_id: id, page_size: 20 }) });

  const update = useMutation({
    mutationFn: (changes: Partial<Movie>) => patch<Movie>(`/movies/${id}`, changes),
    onSuccess: (m) => { qc.setQueryData(['movie', id], m); qc.invalidateQueries({ queryKey: ['movies'] }); },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const action = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => post<{ message?: string }>(path, body),
    onSuccess: (r) => toast(r.message ?? 'Queued', 'success'),
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  if (movie.isLoading) return <Loading />;
  if (!movie.data) return <ErrorAlert error={movie.error} />;
  const m = movie.data;

  const remove = async () => {
    const ok = await confirm(`Delete ${m.title}?`,
      <FormControlLabel control={<Switch onChange={(e) => setDeleteFiles(e.target.checked)} />} label="Also move the file to the recycle bin" />,
      { confirmLabel: 'Delete', danger: true });
    if (!ok) return;
    try {
      await del(`/movies/${id}`, { delete_files: deleteFiles });
      toast(`${m.title} removed`, 'success');
      qc.invalidateQueries({ queryKey: ['movies'] });
      navigate('/library');
    } catch (e) {
      toast(errorMessage(e), 'error');
    }
  };

  return (
    <>
      <Button startIcon={<ArrowLeft size={16} />} onClick={() => navigate(-1)} sx={{ mb: 2 }}>Back</Button>
      <MediaHeader
        title={m.title} year={m.year} backdrop={m.backdrop_path} poster={m.poster_path} tagline={m.tagline}
        overline={['Movie', m.certification, m.runtime_minutes ? `${m.runtime_minutes} min` : null, m.studio].filter(Boolean).join(' · ')}
        chips={
          <>
            {m.has_file ? <Chip color="success" size="small" label="On disk" /> : <Chip color="error" size="small" label="Missing" variant="outlined" />}
            {m.quality && <QualityChip quality={m.quality} />}
            {m.rating ? <Chip size="small" label={`★ ${m.rating.toFixed(1)}`} /> : null}
            {genreChips(m.genres)}
          </>
        }
        actions={
          <>
            {can('search:execute') && (
              <>
                <Button variant="contained" startIcon={<Zap size={16} />} disabled={action.isPending}
                  onClick={() => action.mutate({ path: '/search/automatic', body: { media_type: 'movie', media_id: id } })}>Automatic search</Button>
                <Button variant="outlined" startIcon={<Search size={16} />} onClick={() => setSearch({ mediaType: 'movie', mediaId: id, title: `${m.title} (${m.year})` })}>
                  Interactive search
                </Button>
              </>
            )}
            {can('media:write') && (
              <Button variant="outlined" startIcon={<RefreshCw size={16} />} onClick={() => action.mutate({ path: `/movies/${id}/refresh` })}>Refresh metadata</Button>
            )}
            {can('files:rename') && m.has_file && (
              <Button variant="outlined" startIcon={<FileEdit size={16} />} onClick={() => navigate(`/renamer?media_type=movie&media_id=${id}`)}>Rename</Button>
            )}
            {can('media:delete') && <Button color="error" variant="outlined" startIcon={<Trash2 size={16} />} onClick={remove}>Delete</Button>}
          </>
        }
      />

      <Grid container spacing={2}>
        <Grid size={{ xs: 12, lg: 8 }}>
          <Section title="Overview" sx={{ mb: 2 }}>
            <Typography color="text.secondary" sx={{ lineHeight: 1.7 }}>{m.overview || 'No overview available yet – metadata refresh runs in the background.'}</Typography>
            {m.crew?.length ? (
              <Typography variant="body2" sx={{ mt: 2 }}>
                {m.crew.map((c) => <span key={c.name + c.job}><b>{c.job}:</b> {c.name} &nbsp; </span>)}
              </Typography>
            ) : null}
          </Section>
          <Section title="Cast" sx={{ mb: 2 }}><CastList cast={m.cast_members} /></Section>
          <Section title="History">
            {history.data?.items.length ? (
              <Table size="small">
                <TableBody>
                  {history.data.items.map((h) => (
                    <TableRow key={h.id}>
                      <TableCell sx={{ width: 110 }}><StatusChip status={h.event_type} /></TableCell>
                      <TableCell>
                        <Typography variant="body2" fontWeight={600} sx={{ wordBreak: 'break-all' }}>{h.title}</Typography>
                        <Typography variant="caption" color="text.secondary" sx={{ wordBreak: 'break-all' }}>{h.details}</Typography>
                      </TableCell>
                      <TableCell><QualityChip quality={h.quality} /></TableCell>
                      <TableCell sx={{ whiteSpace: 'nowrap', color: 'text.secondary' }}>{relative(h.timestamp)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : <Typography color="text.secondary" variant="body2">No history yet.</Typography>}
          </Section>
        </Grid>
        <Grid size={{ xs: 12, lg: 4 }}>
          <Section title="Management">
            <FormControlLabel sx={{ mb: 2 }} label={m.monitored ? 'Monitored – searched automatically' : 'Unmonitored'}
              control={<Switch checked={m.monitored} disabled={!can('media:write')} onChange={(e) => update.mutate({ monitored: e.target.checked })} />} />
            <TextField select fullWidth size="small" label="Quality profile" value={m.quality_profile_id ?? 1} disabled={!can('media:write')} sx={{ mb: 2 }}
              onChange={(e) => update.mutate({ quality_profile_id: Number(e.target.value) })}>
              {profiles.data?.map((p) => <MenuItem key={p.id} value={p.id}>{p.name}</MenuItem>)}
            </TextField>
            <Fact label="Folder"><Box component="code" sx={{ fontSize: '0.78rem' }}>{m.path ?? m.root_folder}</Box></Fact>
            {m.file_path && <Fact label="File"><Box component="code" sx={{ fontSize: '0.78rem' }}>{m.file_path}</Box></Fact>}
            <Fact label="Size on disk">{bytes(m.file_size_bytes)}</Fact>
            <Fact label="Release dates">
              {[m.release_date && `Cinema ${m.release_date}`, m.digital_release_date && `Digital ${m.digital_release_date}`, m.physical_release_date && `Physical ${m.physical_release_date}`]
                .filter(Boolean).join(' · ') || '—'}
            </Fact>
            <Fact label="External IDs">
              {m.imdb_id && <a href={`https://www.imdb.com/title/${m.imdb_id}`} target="_blank" rel="noreferrer">IMDb {m.imdb_id}</a>}
              {m.imdb_id && m.tmdb_id && ' · '}
              {m.tmdb_id && m.metadata_source === 'tmdb' && <a href={`https://www.themoviedb.org/movie/${m.tmdb_id}`} target="_blank" rel="noreferrer">TMDB {m.tmdb_id}</a>}
            </Fact>
            <Fact label="Metadata">{m.metadata_source ?? 'not matched yet'} · refreshed {relative(m.last_metadata_refresh)}</Fact>
            <Fact label="Last searched">{relative(m.last_search_at)}</Fact>
          </Section>
        </Grid>
      </Grid>
      <InteractiveSearchDialog target={search} onClose={() => setSearch(null)} />
      {dialog}
    </>
  );
}
