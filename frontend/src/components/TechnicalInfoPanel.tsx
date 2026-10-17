import { Alert, Box, Button, Chip, Grid, Stack, Table, TableBody, TableCell, TableHead, TableRow, Tooltip, Typography } from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { get, post } from '../api/client';
import type { MediaFile } from '../api/types';
import { toast } from '../store/ui';
import { errorMessage, relative } from '../utils/format';

const RES_RANK: Record<string, number> = { sd: 1, '720p': 2, '1080p': 3, '2160p': 4 };

/**
 * The claimed resolution when ffprobe measured a *lower* one, else null. Mirrors the backend's
 * quality_mismatch warning: higher-than-claimed is only `info` there, and an unknown measurement
 * is never a mismatch.
 */
export function claimedResolutionMismatch(quality?: string | null, actualRes?: string | null): string | null {
  if (!quality || !actualRes) return null;
  const match = quality.match(/\b(2160p|1080p|720p|480p|576p|SD)\b/i);
  if (!match) return null;
  const claimed = ['480p', '576p', 'sd'].includes(match[1].toLowerCase()) ? 'SD' : match[1].toLowerCase();
  const claimedRank = RES_RANK[claimed.toLowerCase()];
  const actualRank = RES_RANK[actualRes.toLowerCase()];
  if (!claimedRank || !actualRank) return null;
  return actualRank < claimedRank ? claimed : null;
}

export function formatChannels(channels?: number | null, layout?: string | null): string {
  if (layout && /^\d+\.\d+/.test(layout)) return layout;
  if (!channels) return '—';
  const map: Record<number, string> = { 1: '1.0', 2: '2.0', 6: '5.1', 8: '7.1' };
  return map[channels] ?? `${channels}ch`;
}

export function formatDuration(seconds?: number | null): string {
  if (!seconds || seconds <= 0) return '—';
  const totalMin = Math.round(seconds / 60);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h}h ${m}m (${totalMin} min)` : `${totalMin} min`;
}

export function formatBitrate(bps?: number | null): string {
  if (!bps || bps <= 0) return '—';
  if (bps >= 1_000_000) return `${(bps / 1_000_000).toFixed(1)} Mbps`;
  return `${Math.round(bps / 1_000)} kbps`;
}

export default function TechnicalInfoPanel({
  file,
  expectedRuntimeMinutes,
  canInspect = false,
}: {
  file: MediaFile;
  expectedRuntimeMinutes?: number | null;
  canInspect?: boolean;
}) {
  const qc = useQueryClient();
  const detail = useQuery({
    queryKey: ['media-file', file.id],
    queryFn: () => get<MediaFile>(`/media-files/${file.id}`),
    staleTime: 30_000,
  });

  const reinspect = useMutation({
    mutationFn: () => post<{ job_id: number }>(`/media-files/${file.id}/inspect`),
    onSuccess: () => {
      toast('Inspection queued', 'success');
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ['media-file', file.id] });
        if (file.movie_id) qc.invalidateQueries({ queryKey: ['movie', file.movie_id] });
        if (file.series_id) qc.invalidateQueries({ queryKey: ['series', file.series_id] });
      }, 1000);
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const mf = detail.data ?? file;
  const info = mf.mediainfo;
  const video = info?.video;
  const audioList = info?.audio ?? [];
  const subList = info?.subtitles ?? [];
  const mismatchClaimed = claimedResolutionMismatch(mf.quality, mf.video_resolution);

  return (
    <Box sx={{ p: 2, borderRadius: 1.5, bgcolor: 'background.default', border: 1, borderColor: 'divider' }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center" flexWrap="wrap" spacing={1} sx={{ mb: 1.5 }}>
        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
          <Typography variant="subtitle2" fontWeight={700}>Technical Info (ffprobe)</Typography>
          {mf.inspected_at ? (
            <Typography variant="caption" color="text.secondary">
              Inspected {relative(mf.inspected_at)}
            </Typography>
          ) : (
            <Chip size="small" variant="outlined" label="Not inspected yet" />
          )}
          {mismatchClaimed && (
            <Tooltip title={`Release quality claims ${mismatchClaimed}, but ffprobe measured ${mf.video_resolution} (${mf.video_width}×${mf.video_height})`}>
              <Chip
                size="small"
                color="warning"
                icon={<AlertTriangle size={14} />}
                label={`Actual: ${mf.video_resolution} (claimed ${mismatchClaimed})`}
              />
            </Tooltip>
          )}
        </Stack>
        {canInspect && (
          <Button
            size="small"
            variant="outlined"
            startIcon={<RefreshCw size={14} />}
            disabled={reinspect.isPending}
            onClick={() => reinspect.mutate()}
          >
            Re-inspect
          </Button>
        )}
      </Stack>

      {mf.inspection_error && (
        <Alert severity="warning" sx={{ mb: 1.5 }}>
          Inspection error: {mf.inspection_error}
        </Alert>
      )}

      {mf.inspected_at && (
        <Grid container spacing={2}>
          <Grid size={{ xs: 12, md: 6 }}>
            <Typography variant="caption" color="text.secondary" sx={{ textTransform: 'uppercase', fontWeight: 700 }}>
              Video & Container
            </Typography>
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mt: 0.75, mb: 1 }}>
              {mf.video_codec && <Chip size="small" color="primary" variant="outlined" label={mf.video_codec.toUpperCase()} />}
              {mf.video_resolution && (
                <Chip
                  size="small"
                  color={mismatchClaimed ? 'warning' : 'default'}
                  label={mf.video_width && mf.video_height ? `${mf.video_resolution} (${mf.video_width}×${mf.video_height})` : mf.video_resolution}
                />
              )}
              <Chip
                size="small"
                color={mf.hdr_format ? 'secondary' : 'default'}
                variant={mf.hdr_format ? 'filled' : 'outlined'}
                label={mf.hdr_format || 'SDR'}
              />
              {(mf.video_bit_depth || video?.bit_depth) && (
                <Chip size="small" variant="outlined" label={`${mf.video_bit_depth ?? video?.bit_depth}-bit`} />
              )}
              {video?.frame_rate && (
                <Chip size="small" variant="outlined" label={`${video.frame_rate} fps`} />
              )}
            </Stack>
            <Typography variant="body2" color="text.secondary">
              <b>Container:</b> {mf.container || '—'} · <b>Bitrate:</b> {formatBitrate(mf.overall_bitrate)}
              {info?.chapters ? ` · ${info.chapters} chapters` : ''}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              <b>Duration:</b> {formatDuration(mf.duration_seconds)}
              {expectedRuntimeMinutes ? ` (expected ${expectedRuntimeMinutes} min)` : ''}
            </Typography>
          </Grid>

          <Grid size={{ xs: 12, md: 6 }}>
            <Typography variant="caption" color="text.secondary" sx={{ textTransform: 'uppercase', fontWeight: 700 }}>
              Subtitles
            </Typography>
            <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap sx={{ mt: 0.75 }}>
              {subList.length > 0 ? (
                subList.map((s, idx) => {
                  const flags = [s.forced && 'forced', s.hearing_impaired && 'SDH'].filter(Boolean).join(', ');
                  const label = `${s.language || 'und'}${flags ? ` (${flags})` : ''}`;
                  return (
                    <Tooltip key={idx} title={[s.codec, s.title].filter(Boolean).join(' · ')}>
                      <Chip size="small" variant="outlined" label={label} />
                    </Tooltip>
                  );
                })
              ) : mf.subtitle_languages?.length ? (
                mf.subtitle_languages.map((lang) => <Chip key={lang} size="small" variant="outlined" label={lang} />)
              ) : (
                <Typography variant="body2" color="text.secondary">No embedded subtitle streams</Typography>
              )}
            </Stack>
          </Grid>

          {audioList.length > 0 && (
            <Grid size={{ xs: 12 }}>
              <Typography variant="caption" color="text.secondary" sx={{ textTransform: 'uppercase', fontWeight: 700 }}>
                Audio Tracks ({audioList.length})
              </Typography>
              <Table size="small" sx={{ mt: 0.5 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>Language</TableCell>
                    <TableCell>Codec</TableCell>
                    <TableCell>Channels</TableCell>
                    <TableCell>Title / Flags</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {audioList.map((a, idx) => (
                    <TableRow key={idx}>
                      <TableCell sx={{ fontWeight: 600 }}>{a.language || 'und'}</TableCell>
                      <TableCell>{[a.codec.toUpperCase(), a.profile].filter(Boolean).join(' ')}</TableCell>
                      <TableCell>{formatChannels(a.channels, a.channel_layout)}</TableCell>
                      <TableCell>
                        <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap" useFlexGap>
                          {a.title && <Typography variant="caption">{a.title}</Typography>}
                          {a.default && <Chip size="small" label="Default" variant="outlined" sx={{ height: 18, fontSize: '0.68rem' }} />}
                          {a.commentary && <Chip size="small" color="warning" label="Commentary" variant="outlined" sx={{ height: 18, fontSize: '0.68rem' }} />}
                        </Stack>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Grid>
          )}
        </Grid>
      )}
    </Box>
  );
}
