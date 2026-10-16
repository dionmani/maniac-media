import {
  Alert,
  Box,
  Button,
  Chip,
  Collapse,
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
  TableSortLabel,
  Tooltip,
  Typography,
} from '@mui/material';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Ban, CheckCircle2, ChevronDown, ChevronRight, Download, Sparkles, X } from 'lucide-react';
import { Fragment, useState } from 'react';
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
  const [sortByFormat, setSortByFormat] = useState<'none' | 'desc' | 'asc'>('none');
  const [expandedGuid, setExpandedGuid] = useState<string | null>(null);
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

  const allReleases = query.data?.releases ?? [];
  const topApprovedGuid = allReleases.find((r) => r.approved)?.guid ?? null;
  const filtered = allReleases.filter((r) => showRejected || r.approved);
  const releases = sortByFormat === 'none'
    ? filtered
    : [...filtered].sort((a, b) => {
        const fa = a.format_score ?? 0;
        const fb = b.format_score ?? 0;
        return sortByFormat === 'desc' ? fb - fa : fa - fb;
      });
  const hidden = allReleases.length - filtered.length;

  const toggleFormatSort = () => {
    setSortByFormat((prev) => (prev === 'none' ? 'desc' : prev === 'desc' ? 'asc' : 'none'));
  };

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
                  <TableCell sx={{ width: 56 }} />
                  <TableCell>Release</TableCell>
                  <TableCell>Quality</TableCell>
                  <TableCell align="right">
                    <TableSortLabel
                      active={sortByFormat !== 'none'}
                      direction={sortByFormat === 'asc' ? 'asc' : 'desc'}
                      onClick={toggleFormatSort}
                    >
                      CF Score
                    </TableSortLabel>
                  </TableCell>
                  <TableCell>Size</TableCell>
                  <TableCell>Peers</TableCell>
                  <TableCell>Age</TableCell>
                  <TableCell>Indexer</TableCell>
                  <TableCell align="right" />
                </TableRow>
              </TableHead>
              <TableBody>
                {releases.map((r) => {
                  const isExpanded = expandedGuid === r.guid;
                  const isTopApproved = r.guid === topApprovedGuid;
                  const formatScore = r.format_score ?? 0;
                  return (
                    <Fragment key={r.guid}>
                      <TableRow hover sx={{ opacity: r.approved ? 1 : 0.55, '& > td': { borderBottom: isExpanded ? 'none' : undefined } }}>
                        <TableCell sx={{ width: 56, whiteSpace: 'nowrap' }}>
                          <Stack direction="row" spacing={0.5} alignItems="center">
                            <Tooltip title="Why this rank">
                              <IconButton size="small" onClick={() => setExpandedGuid(isExpanded ? null : r.guid)}>
                                {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                              </IconButton>
                            </Tooltip>
                            {r.approved ? (
                              <CheckCircle2 size={16} color={palette.green} />
                            ) : (
                              <Tooltip title={<ul style={{ margin: 0, paddingLeft: 16 }}>{r.rejections.map((x) => <li key={x}>{x}</li>)}</ul>}>
                                <Ban size={16} color={palette.red} />
                              </Tooltip>
                            )}
                          </Stack>
                        </TableCell>
                        <TableCell sx={{ fontFamily: "'JetBrains Mono', monospace", fontSize: '0.78rem', wordBreak: 'break-all', maxWidth: 440 }}>
                          {r.title}
                          <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ mt: 0.5 }}>
                            {isTopApproved && (
                              <Chip
                                size="small"
                                icon={<Sparkles size={12} />}
                                label="Would be grabbed automatically"
                                color="primary"
                                variant="filled"
                              />
                            )}
                            {r.proper && <Chip size="small" label="PROPER" color="success" />}
                            {r.hdr && <Chip size="small" label={r.hdr_format ?? 'HDR'} color="warning" />}
                            {r.codec !== 'unknown' && <Chip size="small" label={r.codec} variant="outlined" />}
                            {(r.custom_formats ?? []).map((cf) => (
                              <Tooltip key={cf.id} title={`Format score: ${cf.score >= 0 ? `+${cf.score}` : cf.score}`}>
                                <Chip
                                  size="small"
                                  label={`${cf.name} (${cf.score >= 0 ? `+${cf.score}` : cf.score})`}
                                  color={cf.score > 0 ? 'success' : cf.score < 0 ? 'error' : 'default'}
                                  variant="outlined"
                                />
                              </Tooltip>
                            ))}
                          </Stack>
                        </TableCell>
                        <TableCell><QualityChip quality={r.quality} /></TableCell>
                        <TableCell align="right" sx={{ whiteSpace: 'nowrap', fontWeight: 600 }}>
                          <Typography
                            variant="body2"
                            fontWeight={600}
                            color={formatScore > 0 ? 'success.main' : formatScore < 0 ? 'error.main' : 'text.secondary'}
                          >
                            {formatScore > 0 ? `+${formatScore}` : formatScore}
                          </Typography>
                        </TableCell>
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
                      <TableRow>
                        <TableCell colSpan={9} sx={{ py: 0, border: 0 }}>
                          <Collapse in={isExpanded} unmountOnExit>
                            <Box sx={{ p: 1.5, my: 0.5, bgcolor: 'action.hover', borderRadius: 1 }}>
                              <Typography variant="caption" fontWeight={700} display="block" sx={{ mb: 0.5 }}>
                                Why this rank (Total score: {r.score})
                              </Typography>
                              <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                                {(r.score_breakdown ?? []).map((item) => (
                                  <Chip
                                    key={item.label}
                                    size="small"
                                    variant="outlined"
                                    color={item.value > 0 ? 'success' : item.value < 0 ? 'error' : 'default'}
                                    label={`${item.label}: ${item.value >= 0 ? `+${item.value}` : item.value}`}
                                  />
                                ))}
                              </Stack>
                              {r.rejections.length > 0 && (
                                <Box sx={{ mt: 1 }}>
                                  <Typography variant="caption" color="error" fontWeight={600}>
                                    Rejections:
                                  </Typography>
                                  <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ mt: 0.5 }}>
                                    {r.rejections.map((rej) => (
                                      <Chip key={rej} size="small" color="error" variant="outlined" label={rej} />
                                    ))}
                                  </Stack>
                                </Box>
                              )}
                            </Box>
                          </Collapse>
                        </TableCell>
                      </TableRow>
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
            {releases.length === 0 && <Typography color="text.secondary" sx={{ py: 4, textAlign: 'center' }}>No releases found.</Typography>}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
