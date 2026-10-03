import { Alert, Box, Button, Chip, FormControlLabel, Grid, MenuItem, Stack, Switch, Table, TableBody, TableCell, TableRow, TextField, Typography } from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, FileEdit, Link2, RefreshCw, Search, Trash2, Zap } from 'lucide-react';
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { del, get, patch, post } from '../api/client';
import type { HistoryItem, Movie, Page, QualityProfile } from '../api/types';
import { ErrorAlert, Loading, QualityChip, Section, StatusChip, useConfirm } from '../components/common';
import InteractiveSearchDialog, { type SearchTarget } from '../components/InteractiveSearchDialog';
import LinkDialog, { type LinkTarget } from '../components/LinkDialog';
import { CastList, Fact, MediaHeader, genreChips, requestedByChip } from '../components/MediaHeader';
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
  const [link, setLink] = useState<LinkTarget | null>(null);

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

  const openLink = () => setLink({ mediaType: 'movie', mediaId: id, label: `${m.title} (${m.year})`, path: m.file_path ?? m.path });
  const linked = () => {
    qc.invalidateQueries({ queryKey: ['movie', id] });
    qc.invalidateQueries({ queryKey: ['movies'] });
    qc.invalidateQueries({ queryKey: ['history', 'movie', id] });
  };

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
            {requestedByChip(m.requested_by)}
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
            {can('media:write') && (
              <Button variant="outlined" startIcon={<Link2 size={16} />} onClick={openLink}>Link</Button>
            )}
            {can('files:rename') && m.has_file && (
              <Button variant="outlined" startIcon={<FileEdit size={16} />} onClick={() => navigate(`/renamer?media_type=movie&media_id=${id}`)}>Rename</Button>
            )}
            {can('media:delete') && <Button color="error" variant="outlined" startIcon={<Trash2 size={16} />} onClick={remove}>Delete</Button>}
          </>
        }
      />

      {!m.metadata_source && can('media:write') && (
        <Alert severity="info" sx={{ mb: 2 }} action={<Button color="inherit" size="small" startIcon={<Link2 size={14} />} onClick={openLink}>Link</Button>}>
          This movie isn't matched to any metadata yet. If the title above is wrong, link it to the right movie with its IMDb or TMDB id.
        </Alert>
      )}
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
          {m.files && m.files.length > 0 && (
            <Section
              title={
                <Stack direction="row" spacing={1} alignItems="center">
                  <span>Files</span>
                  {m.files.length > 1 && (
                    <Chip size="small" color="secondary" label={`+${m.files.length - 1} other files`} />
                  )}
                </Stack>
              }
              sx={{ mb: 2 }}
            >
              <Table size="small">
                <TableBody>
                  {m.files.map((file) => (
                    <TableRow key={file.id}>
                      <TableCell sx={{ width: 90 }}>
                        {file.is_primary ? (
                          <Chip size="small" color="primary" label="Primary" />
                        ) : (
                          <Chip size="small" variant="outlined" label="Duplicate" />
                        )}
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" sx={{ wordBreak: 'break-all', fontFamily: 'monospace', fontSize: '0.8rem' }}>
                          {file.path}
                        </Typography>
                        {file.scene_name && file.scene_name !== file.path && (
                          <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                            Release: {file.scene_name}
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell sx={{ whiteSpace: 'nowrap', width: 120 }}>
                        <QualityChip quality={file.quality ?? 'Unknown'} />
                      </TableCell>
                      <TableCell sx={{ whiteSpace: 'nowrap', width: 100, color: 'text.secondary' }}>
                        {bytes(file.size_bytes)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Section>
          )}
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
      <LinkDialog target={link} onClose={() => setLink(null)} onLinked={linked} />
      {dialog}
    </>
  );
}
