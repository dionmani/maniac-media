import { Alert, Box, Button, Checkbox, Chip, Dialog, DialogActions, DialogContent, DialogTitle, FormControlLabel, List, ListItemButton, ListItemText, Radio, RadioGroup, Stack, TextField, Typography } from '@mui/material';
import { GitMerge, Link2, Search } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { get, post } from '../api/client';
import type { LinkResult, MergeFile, MergePlan } from '../api/types';
import { toast } from '../store/ui';
import { bytes, errorMessage } from '../utils/format';
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
  const [keepPath, setKeepPath] = useState<string | null>(null);
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

  // preselect the file the server recommends keeping (best quality, then largest)
  useEffect(() => {
    setKeepPath(preview?.merge?.files.find((f) => f.recommended)?.path ?? null);
  }, [preview]);

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
      const done = await post<LinkResult>(url, { external_id: externalId, dry_run: false, rename, ...(merging && { merge: true, keep_path: keepPath }) });
      const name = `${done.match.title}${done.match.year ? ` (${done.match.year})` : ''}`;
      const renamed = done.renamed ? ` – ${done.renamed} rename(s) applied` : '';
      const verb = done.merged_into ? 'Merged' : 'Linked';
      toast(`${done.merged_into ? 'Merged into' : 'Linked to'} ${name}${renamed}`, 'success');
      if (done.rename_errors.length) toast(`${verb}, but renaming hit a problem: ${done.rename_errors[0]}`, 'warning');
      onClose();
      if (done.merged_into) {
        // this entry no longer exists – carry on from the one it was merged into
        navigate(`/movies/${done.merged_into}`, { replace: true });
      } else {
        onLinked();
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const m = preview?.match;
  const existing = preview?.existing;
  const plan = existing ? preview?.merge : null;
  const merging = !!plan;
  const mustPick = (plan?.files.filter((f) => f.exists).length ?? 0) > 1;
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
            <Alert severity={plan ? 'info' : 'warning'} action={
              <Button color="inherit" size="small" onClick={() => { onClose(); navigate(movie ? `/movies/${existing.id}` : `/series/${existing.id}`); }}>Open it</Button>
            }>
              {existing.title}{existing.year ? ` (${existing.year})` : ''} is already in your library.{' '}
              {plan ? 'Merge this entry into it so you end up with one movie and one file.' : 'Linking this one too would create a duplicate.'}
            </Alert>
          )}
          {plan && <MergeFiles plan={plan} keepPath={keepPath} onPick={setKeepPath} mustPick={mustPick} disabled={busy} />}
          {m && (!existing || merging) && (
            <FormControlLabel
              control={<Checkbox checked={rename} onChange={(e) => setRename(e.target.checked)} />}
              label={`Rename the folder and ${movie ? 'file' : 'episode files'} to your naming format`}
            />
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>Cancel</Button>
        <Button variant="contained" startIcon={merging ? <GitMerge size={16} /> : <Link2 size={16} />} onClick={apply}
          disabled={!m || (!!existing && !merging) || (mustPick && !keepPath) || busy}>
          {merging ? 'Merge' : 'Link'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

const fileName = (p: string) => p.split(/[\\/]/).pop() ?? p;
const folderOf = (p: string) => p.split(/[\\/]/).slice(0, -1).join('/');

/** The video files a merge ends up with; when there's more than one, the user picks the one to keep. */
function MergeFiles({ plan, keepPath, onPick, mustPick, disabled }: {
  plan: MergePlan; keepPath: string | null; onPick: (path: string) => void; mustPick: boolean; disabled: boolean;
}) {
  const present = plan.files.filter((f) => f.exists);
  const describe = (f: MergeFile) => (
    <Box sx={{ minWidth: 0 }}>
      <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap" useFlexGap>
        <Typography variant="body2" fontWeight={600} sx={{ wordBreak: 'break-all' }}>{fileName(f.path)}</Typography>
        <Chip size="small" variant="outlined" label={f.owner === 'existing' ? 'In library' : 'This entry'} />
        {f.quality && <Chip size="small" variant="outlined" label={f.quality} />}
        {f.recommended && mustPick && <Chip size="small" color="success" label="Best quality" />}
      </Stack>
      <Typography variant="caption" color="text.secondary" sx={{ wordBreak: 'break-all' }}>
        {bytes(f.size_bytes)} · {folderOf(f.path)}
      </Typography>
    </Box>
  );

  return (
    <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 1, p: 1.5 }}>
      {present.length === 0 ? (
        <Typography variant="body2" color="text.secondary">Neither entry has a video file on disk – merging just removes this duplicate.</Typography>
      ) : mustPick ? (
        <>
          <Typography variant="subtitle2" sx={{ mb: 0.5 }}>Both have a file – pick the one to keep</Typography>
          <RadioGroup value={keepPath ?? ''} onChange={(e) => onPick(e.target.value)}>
            {present.map((f) => (
              <FormControlLabel key={f.path} value={f.path} disabled={disabled} control={<Radio size="small" />} label={describe(f)}
                sx={{ alignItems: 'flex-start', mr: 0, py: 0.5, '& .MuiRadio-root': { pt: 0.25 } }} />
            ))}
          </RadioGroup>
        </>
      ) : (
        <>
          <Typography variant="subtitle2" sx={{ mb: 0.5 }}>File kept</Typography>
          {describe(present[0])}
        </>
      )}
      <Typography variant="caption" color="text.secondary" component="div" sx={{ mt: 1 }}>
        {mustPick && 'The files you don’t keep go to the recycle bin. '}
        {plan.cleanup_folder && (
          <>Anything left in <Box component="code" sx={{ wordBreak: 'break-all' }}>{plan.cleanup_folder}</Box> goes to the recycle bin too, so a rescan can't bring the duplicate back.</>
        )}
      </Typography>
    </Box>
  );
}
