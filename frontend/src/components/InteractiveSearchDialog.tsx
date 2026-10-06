import {
  Alert,
  Box,
  Button,
  Chip,
  Dialog,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  IconButton,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from '@mui/material';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Ban, CheckCircle2, Download, X } from 'lucide-react';
import { useState } from 'react';
import { get, post } from '../api/client';
import type { Release } from '../api/types';
import { toast } from '../store/ui';
import { palette } from '../theme';
import { bytes, episodeCode, errorMessage } from '../utils/format';
import { Loading, QualityChip } from './common';

export interface SearchTarget {
  mediaType: 'movie' | 'tv';
  mediaId: number;
  title: string;
  season?: number;
  episode?: number;
}

export default function InteractiveSearchDialog({ target, onClose }: { target: SearchTarget | null; onClose: () => void }) {
  const [showRejected, setShowRejected] = useState(false);
  const query = useQuery({
    queryKey: ['releases', target],
    enabled: !!target,
    staleTime: 0,
    queryFn: () =>
      get<{ releases: Release[]; indexers: { indexer: string; results: number; error: string | null }[]; held?: string | null }>('/releases', {
        media_type: target!.mediaType, media_id: target!.mediaId, season: target!.season, episode: target!.episode,
      }),
  });
  const grab = useMutation({
    mutationFn: (r: Release) =>
      post<{ message: string }>('/releases/grab', {
        token: r.download_url_token, media_type: target!.mediaType, media_id: target!.mediaId,
        season: target!.season ?? r.season ?? null, episodes: target!.episode ? [target!.episode] : r.episodes,
      }),
    onSuccess: (res) => {
      toast(res.message, 'success');
      onClose();
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const releases = (query.data?.releases ?? []).filter((r) => showRejected || r.approved);
  const hidden = (query.data?.releases.length ?? 0) - releases.length;

  return (
    <Dialog open={!!target} onClose={onClose} maxWidth="lg" fullWidth>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <Box sx={{ flex: 1 }}>
          Interactive search
          <Typography variant="body2" color="text.secondary">
            {target?.title}
            {target?.season !== undefined && (target.episode ? ` · ${episodeCode(target.season, target.episode)}` : ` · Season ${target.season}`)}
          </Typography>
        </Box>
        <FormControlLabel control={<Switch checked={showRejected} onChange={(e) => setShowRejected(e.target.checked)} />} label="Show rejected" />
        <IconButton onClick={onClose}><X size={18} /></IconButton>
      </DialogTitle>
      <DialogContent>
        {query.isLoading && <Loading label="Querying indexers…" />}
        {query.error && <Alert severity="error">{errorMessage(query.error)}</Alert>}
        {query.data && (
          <>
            <Stack direction="row" spacing={1} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
              {query.data.indexers.length === 0 && <Alert severity="warning" sx={{ width: '100%' }}>No indexers are enabled. Add one under Settings → Indexers.</Alert>}
              {query.data.indexers.map((i) => (
                <Chip key={i.indexer} size="small" color={i.error ? 'error' : 'default'} variant="outlined"
                  label={i.error ? `${i.indexer}: ${i.error}` : `${i.indexer}: ${i.results}`} />
              ))}
              {hidden > 0 && <Chip size="small" label={`${hidden} rejected hidden`} />}
            </Stack>
            {query.data.held && (
              <Alert severity="info" sx={{ mb: 2 }}>
                {query.data.held}. Automatic search waits for this date; grabbing a release now overrides that.
              </Alert>
            )}
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell />
                  <TableCell>Release</TableCell>
                  <TableCell>Quality</TableCell>
                  <TableCell>Size</TableCell>
                  <TableCell>Peers</TableCell>
                  <TableCell>Age</TableCell>
                  <TableCell>Indexer</TableCell>
                  <TableCell align="right" />
                </TableRow>
              </TableHead>
              <TableBody>
                {releases.map((r) => (
                  <TableRow key={r.guid} sx={{ opacity: r.approved ? 1 : 0.55 }}>
                    <TableCell sx={{ width: 32 }}>
                      {r.approved ? (
                        <CheckCircle2 size={16} color={palette.green} />
                      ) : (
                        <Tooltip title={<ul style={{ margin: 0, paddingLeft: 16 }}>{r.rejections.map((x) => <li key={x}>{x}</li>)}</ul>}>
                          <Ban size={16} color={palette.red} />
                        </Tooltip>
                      )}
                    </TableCell>
                    <TableCell sx={{ fontFamily: "'JetBrains Mono', monospace", fontSize: '0.78rem', wordBreak: 'break-all', maxWidth: 440 }}>
                      {r.title}
                      <Stack direction="row" spacing={0.5} sx={{ mt: 0.5 }}>
                        {r.proper && <Chip size="small" label="PROPER" color="success" />}
                        {r.hdr && <Chip size="small" label="HDR" color="warning" />}
                        {r.codec !== 'unknown' && <Chip size="small" label={r.codec} variant="outlined" />}
                      </Stack>
                    </TableCell>
                    <TableCell><QualityChip quality={r.quality} /></TableCell>
                    <TableCell sx={{ whiteSpace: 'nowrap' }}>{bytes(r.size_bytes)}</TableCell>
                    <TableCell>
                      {r.protocol === 'torrent' ? (
                        <Typography variant="body2" color={(r.seeders ?? 0) > 0 ? 'success.main' : 'error.main'}>↑{r.seeders ?? '?'} ↓{r.leechers ?? '?'}</Typography>
                      ) : (
                        <Chip size="small" label="usenet" />
                      )}
                    </TableCell>
                    <TableCell>{r.age_days !== null && r.age_days !== undefined ? `${r.age_days}d` : '—'}</TableCell>
                    <TableCell>{r.indexer}</TableCell>
                    <TableCell align="right">
                      <Tooltip title={r.approved ? 'Send to download client' : 'Override the rejection and grab anyway'}>
                        <span>
                          <Button size="small" variant={r.approved ? 'contained' : 'outlined'} color={r.approved ? 'primary' : 'warning'}
                            startIcon={<Download size={14} />} disabled={grab.isPending} onClick={() => grab.mutate(r)}>
                            Grab
                          </Button>
                        </span>
                      </Tooltip>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {releases.length === 0 && <Typography color="text.secondary" sx={{ py: 4, textAlign: 'center' }}>No releases found.</Typography>}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
