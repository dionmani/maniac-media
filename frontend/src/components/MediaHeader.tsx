import { Avatar, Box, Chip, Paper, Stack, Tooltip, Typography } from '@mui/material';
import type { ReactNode } from 'react';
import type { Cast } from '../api/types';
import { palette } from '../theme';
import { Poster } from './common';

export function MediaHeader({
  title, year, backdrop, poster, overline, chips, actions, tagline,
}: {
  title: string; year?: number | null; backdrop?: string | null; poster?: string | null; overline: string; chips?: ReactNode; actions?: ReactNode; tagline?: string | null;
}) {
  const bg = backdrop && !backdrop.includes('unsplash') ? backdrop : poster && !poster.includes('unsplash') ? poster : null;
  return (
    <Paper sx={{ overflow: 'hidden', mb: 3, position: 'relative' }}>
      <Box sx={{ position: 'absolute', inset: 0, backgroundImage: bg ? `url(${bg})` : undefined, backgroundSize: 'cover', backgroundPosition: 'center 20%',
        filter: 'blur(2px)', opacity: 0.35 }} />
      <Box sx={{ position: 'absolute', inset: 0, background: `linear-gradient(90deg, ${palette.card} 20%, ${palette.card}cc 60%, transparent)` }} />
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={3} sx={{ position: 'relative', p: 3 }} alignItems={{ sm: 'flex-end' }}>
        <Poster src={poster} title={title} height={240} />
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="overline" sx={{ color: palette.cyan, fontWeight: 700, letterSpacing: 1 }}>{overline}</Typography>
          <Typography variant="h4" sx={{ lineHeight: 1.1 }}>
            {title} {year ? <Typography component="span" variant="h5" color="text.secondary">({year})</Typography> : null}
          </Typography>
          {tagline && <Typography sx={{ fontStyle: 'italic', color: 'text.secondary', mt: 0.5 }}>{tagline}</Typography>}
          <Stack direction="row" spacing={1} sx={{ mt: 1.5 }} flexWrap="wrap" useFlexGap>{chips}</Stack>
          <Stack direction="row" spacing={1} sx={{ mt: 2 }} flexWrap="wrap" useFlexGap>{actions}</Stack>
        </Box>
      </Stack>
    </Paper>
  );
}

export function CastList({ cast }: { cast?: Cast[] | null }) {
  if (!cast?.length) return <Typography color="text.secondary" variant="body2">No cast information.</Typography>;
  return (
    <Stack direction="row" spacing={2} sx={{ overflowX: 'auto', pb: 1 }}>
      {cast.slice(0, 16).map((c) => (
        <Tooltip key={c.name + c.character} title={c.character ? `as ${c.character}` : ''}>
          <Stack alignItems="center" sx={{ width: 84, flexShrink: 0 }}>
            <Avatar src={c.image ?? undefined} sx={{ width: 64, height: 64, bgcolor: '#1d2140', mb: 0.5 }}>{c.name[0]}</Avatar>
            <Typography variant="caption" fontWeight={600} textAlign="center" sx={{ lineHeight: 1.2 }}>{c.name}</Typography>
            {c.character && <Typography variant="caption" color="text.secondary" textAlign="center" noWrap sx={{ width: '100%' }}>{c.character}</Typography>}
          </Stack>
        </Tooltip>
      ))}
    </Stack>
  );
}

export function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Box sx={{ mb: 1.5 }}>
      <Typography variant="caption" color="text.disabled" fontWeight={700} sx={{ textTransform: 'uppercase', letterSpacing: 0.5 }}>{label}</Typography>
      <Typography variant="body2" component="div" sx={{ wordBreak: 'break-word' }}>{children ?? '—'}</Typography>
    </Box>
  );
}

export const genreChips = (genres?: string | null) =>
  (genres ?? '').split(',').map((g) => g.trim()).filter((g) => g && !['Cinema', 'Series', 'Feature Film'].includes(g))
    .map((g) => <Chip key={g} size="small" label={g} variant="outlined" />);
