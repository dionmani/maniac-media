import { Alert, Box, Button, Checkbox, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel, List, ListItemButton, ListItemText, Stack, TextField, Typography } from '@mui/material';
import { Link2, Search } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { get, post } from '../api/client';
import type { LinkResult } from '../api/types';
import { toast } from '../store/ui';
import { errorMessage } from '../utils/format';
import { Poster } from './common';

export interface LinkTarget {
  mediaType: 'movie' | 'tv';
  mediaId: number;
  /** what the library currently calls it, e.g. "www Torrenting com - American Pie (1999)" */
  label: string;
  /** a file/folder on disk, to remind the user what they're identifying */
  path?: string | null;
}

interface SearchHit { title: string; year: number | null; tmdb_id: number | null; overview: string | null }

/** "www UIndex org - You Only Live Twice (1967)" -> { query: "You Only Live Twice", year: 1967 } */
function searchSeed(label: string): { query: string; year: number | null } {
  const year = label.match(/\((\d{4})\)\s*$/)?.[1];
  const query = label.replace(/\((\d{4})\)\s*$/, '').replace(/^\s*www[\s.]+\S+[\s.]+[a-z]{2,4}\s*-\s*/i, '').trim();
  return { query, year: year ? Number(year) : null };
}

/** "Link to…": identify an on-disk movie/series by IMDb / TMDB / TVDB id, preview the match, then apply it. */
export default function LinkDialog({ target, onClose, onLinked }: { target: LinkTarget | null; onClose: () => void; onLinked: () => void }) {
  const navigate = useNavigate();
  const [externalId, setExternalId] = useState('');
  const [preview, setPreview] = useState<LinkResult | null>(null);
  const [rename, setRename] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const movie = target?.mediaType === 'movie';
  const url = target ? `/${movie ? 'movies' : 'series'}/${target.mediaId}/link` : '';

  useEffect(() => {
    setExternalId(''); setPreview(null); setRename(true); setError(null); setHits(null);
    setQuery(target ? searchSeed(target.label).query : '');
  }, [target]);

  const search = async () => {
    if (!target || !query.trim()) return;
    setSearching(true); setError(null); setHits(null);
    try {
      const year = movie ? searchSeed(target.label).year : null;
      const found = await get<SearchHit[]>(`/${movie ? 'movies' : 'series'}/lookup`, { query: query.trim(), ...(year ? { year } : {}) });
      // a year-filtered search that finds nothing is retried without the year (the library's year can be wrong too)
      setHits(found.length || !year ? found : await get<SearchHit[]>('/movies/lookup', { query: query.trim() }));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setSearching(false);
    }
  };

  const pick = async (hit: SearchHit) => {
    if (!hit.tmdb_id) return;
    const id = `tmdb:${hit.tmdb_id}`;   // explicit prefix: a bare number means TVDB for series
    setExternalId(id); setBusy(true); setError(null); setPreview(null);
    try {
      setPreview(await post<LinkResult>(url, { external_id: id, dry_run: true }));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const lookup = async () => {
    setBusy(true); setError(null); setPreview(null);
    try {
      setPreview(await post<LinkResult>(url, { external_id: externalId, dry_run: true }));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    setBusy(true);
    try {
      const done = await post<LinkResult>(url, { external_id: externalId, dry_run: false, rename });
      const name = `${done.match.title}${done.match.year ? ` (${done.match.year})` : ''}`;
      toast(`Linked to ${name}${done.renamed ? ` – ${done.renamed} rename(s) applied` : ''}`, 'success');
      if (done.rename_errors.length) toast(`Linked, but renaming hit a problem: ${done.rename_errors[0]}`, 'warning');
      onLinked();
      onClose();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const m = preview?.match;
  const existing = preview?.existing;
  const ids = m ? [m.imdb_id && `IMDb ${m.imdb_id}`, m.tmdb_id && `TMDB ${m.tmdb_id}`, m.tvdb_id && `TVDB ${m.tvdb_id}`].filter(Boolean).join(' · ') : '';

  return (
    <Dialog open={!!target} onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Link to {movie ? 'a movie' : 'a TV show'}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          <Box>
            <Typography variant="body2" color="text.secondary">Currently</Typography>
            <Typography variant="body2" fontWeight={600} sx={{ wordBreak: 'break-word' }}>{target?.label}</Typography>
            {target?.path && <Box component="code" sx={{ fontSize: '0.75rem', wordBreak: 'break-all', color: 'text.secondary' }}>{target.path}</Box>}
          </Box>
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <TextField
              autoFocus fullWidth size="small" value={query} disabled={busy} label="Search TMDb by title"
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && query.trim() && !busy && !searching) { e.preventDefault(); search(); } }}
            />
            <Button variant="outlined" startIcon={<Search size={16} />} onClick={search} disabled={!query.trim() || busy || searching}
              sx={{ whiteSpace: 'nowrap', height: 40 }}>Search</Button>
          </Stack>
          {hits && (hits.length === 0 ? (
            <Typography variant="body2" color="text.secondary">No results for "{query}" — try fewer words, or enter an id below.</Typography>
          ) : (
            <List dense disablePadding sx={{ maxHeight: 220, overflow: 'auto', border: 1, borderColor: 'divider', borderRadius: 1 }}>
              {hits.filter((h) => h.tmdb_id).map((h) => (
                <ListItemButton key={h.tmdb_id} selected={externalId === `tmdb:${h.tmdb_id}`} disabled={busy} onClick={() => pick(h)}>
                  <ListItemText primary={`${h.title}${h.year ? ` (${h.year})` : ''}`} secondary={h.overview}
                    secondaryTypographyProps={{ noWrap: true }} />
                </ListItemButton>
              ))}
            </List>
          ))}
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <TextField
              fullWidth size="small" value={externalId} disabled={busy}
              label={movie ? 'IMDb or TMDB id' : 'TVDB, IMDb or TMDB id'}
              placeholder={movie ? 'tt0163651' : '73244 or tt0386676'}
              helperText={movie
                ? 'An IMDb id (tt…), a TMDB number, or paste the IMDb / TMDB page link'
                : 'A TVDB number, an IMDb id (tt…), or paste the TVDB / IMDb / TMDB page link'}
              onChange={(e) => { setExternalId(e.target.value); setPreview(null); setError(null); }}
              onKeyDown={(e) => { if (e.key === 'Enter' && externalId.trim() && !busy) { e.preventDefault(); lookup(); } }}
            />
            <Button variant="outlined" onClick={lookup} disabled={!externalId.trim() || busy} sx={{ whiteSpace: 'nowrap', height: 40 }}>Look up</Button>
          </Stack>
          {error && <Alert severity="error">{error}</Alert>}
          {m && (
            <Stack direction="row" spacing={2}>
              <Poster src={m.poster_url} title={m.title} height={150} />
              <Box sx={{ minWidth: 0 }}>
                <Typography variant="h6" sx={{ lineHeight: 1.2 }}>{m.title}{m.year ? ` (${m.year})` : ''}</Typography>
                <Typography variant="caption" color="text.secondary">
                  {[ids, `via ${m.provider}`, !movie && m.episode_count ? `${m.episode_count} episodes` : null].filter(Boolean).join(' · ')}
                </Typography>
                {m.overview && (
                  <Typography variant="body2" color="text.secondary"
                    sx={{ mt: 1, display: '-webkit-box', WebkitLineClamp: 4, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                    {m.overview}
                  </Typography>
                )}
              </Box>
            </Stack>
          )}
          {existing && (
            <Alert severity="warning" action={
              <Button color="inherit" size="small" onClick={() => { onClose(); navigate(movie ? `/movies/${existing.id}` : `/series/${existing.id}`); }}>Open it</Button>
            }>
              {existing.title}{existing.year ? ` (${existing.year})` : ''} is already in your library. Linking this one too would create a duplicate.
            </Alert>
          )}
          {m && !existing && (
            <FormControlLabel
              control={<Checkbox checked={rename} onChange={(e) => setRename(e.target.checked)} />}
              label={`Rename the folder and ${movie ? 'file' : 'episode files'} to your naming format`}
            />
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button variant="contained" startIcon={<Link2 size={16} />} onClick={apply} disabled={!m || !!existing || busy}>
          Link
        </Button>
      </DialogActions>
    </Dialog>
  );
}
