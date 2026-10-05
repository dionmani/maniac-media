import { Avatar, Box, Card, CardActionArea, Chip, IconButton, Stack, Tooltip, Typography } from '@mui/material';
import { Check, EyeOff } from 'lucide-react';
import { memo } from 'react';
import type { DiscoverItem } from '../api/types';
import { palette } from '../theme';
import { Poster } from './common';

/** "2026-10-14" as a local date (no UTC shift), e.g. "Oct 14", with the year only when it isn't this year. */
export function shortDate(iso?: string | null): string {
  if (!iso) return '';
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  if (!y || !m || !d) return iso;
  const date = new Date(y, m - 1, d);
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', ...(y !== new Date().getFullYear() && { year: 'numeric' }) });
}

function isPast(iso?: string | null): boolean {
  if (!iso) return false;
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  return new Date(y, m - 1, d).getTime() <= Date.now();
}

/** Why a title is in its section, e.g. "S3 premieres Oct 12"; ``compact`` fits a poster card. */
export function reasonText(item: DiscoverItem, compact = false): string {
  const date = shortDate(item.reason_date);
  const past = isPast(item.reason_date);
  switch (item.reason) {
    case 'announced':
      return date ? `Coming ${date}` : 'Release date TBA';
    case 'in_theaters':
      if (!date) return 'In theaters now';
      if (compact) return past ? `Opened ${date}` : `Opens ${date}`;
      return past ? `In theaters since ${date}` : `In theaters ${date}`;
    case 'digital':
      return past ? `${compact ? 'Digital' : 'On digital'} since ${date}` : `Digital ${date}`;
    case 'physical':
      return past ? `On disc since ${date}` : `Blu-ray & DVD ${date}`;
    case 'series_premiere':
      return past ? `Premiered ${date}` : `Premieres ${date}`;
    case 'season_premiere':
      return `S${item.season_number ?? '?'} ${past ? 'premiered' : 'premieres'} ${date}`;
    default:
      return date;
  }
}

const overlay = { position: 'absolute', width: 'auto !important', height: 'auto !important', borderRadius: '11px !important', backdropFilter: 'blur(6px)' } as const;

type Props = { item: DiscoverItem; isNew: boolean; onOpen: (item: DiscoverItem) => void; onHide?: (item: DiscoverItem) => void; width?: number };

export const DiscoverCard = memo(function DiscoverCard({ item, isNew, onOpen, onHide, width }: Props) {
  const providers = item.watch_providers.filter((p) => p.logo_url).slice(0, 3);
  return (
    <Card sx={{ position: 'relative', width: width ?? '100%', flexShrink: 0, transition: 'all .2s',
      '&:hover': { transform: 'translateY(-4px)', borderColor: palette.purple, boxShadow: `0 0 24px ${palette.purple}26` },
      '&:hover .discover-hide, & .discover-hide:focus-visible': { opacity: 1 } }}>
      <CardActionArea onClick={() => onOpen(item)}>
        <Box sx={{ position: 'relative', aspectRatio: '2 / 3', bgcolor: '#181a2e', '& > *': { width: '100% !important', height: '100% !important', borderRadius: '0 !important' } }}>
          <Poster src={item.poster_url} title={item.title} height={225} />
          {isNew && (
            <Chip size="small" label="NEW" sx={{ ...overlay, top: 8, left: 8, height: '22px !important', fontWeight: 800, letterSpacing: 0.5, bgcolor: `${palette.cyan}e6`, color: '#fff' }} />
          )}
          {item.in_library && (
            <Chip size="small" icon={<Check size={13} color="#fff" />} label="In library"
              sx={{ ...overlay, bottom: 8, right: 8, height: '22px !important', bgcolor: `${palette.green}d9`, color: '#fff' }} />
          )}
          {providers.length > 0 && (
            <Stack direction="row" spacing={0.5} sx={{ position: 'absolute', bottom: 8, left: 8, width: 'auto !important', height: 'auto !important', ...(item.in_library && { bottom: 36 }) }}>
              {providers.map((p) => (
                <Tooltip key={p.name} title={`Streaming on ${p.name}`}>
                  <Avatar src={p.logo_url ?? undefined} alt={p.name} variant="rounded" sx={{ width: 24, height: 24, border: '1px solid #0006' }} />
                </Tooltip>
              ))}
            </Stack>
          )}
        </Box>
        <Box sx={{ p: 1.25 }}>
          <Typography variant="body2" fontWeight={700} noWrap title={item.title}>{item.title}</Typography>
          <Typography variant="caption" color="text.secondary" noWrap display="block" title={reasonText(item)}>{reasonText(item, true)}</Typography>
        </Box>
      </CardActionArea>
      {onHide && !item.in_library && (
        <Tooltip title="Not interested – hide from Discover">
          <IconButton className="discover-hide" size="small" aria-label={`Hide ${item.title}`} onClick={() => onHide(item)}
            sx={{ position: 'absolute', top: 6, right: 6, bgcolor: '#000a', color: '#fff', opacity: 0, transition: 'opacity .15s',
              '&:hover': { bgcolor: '#000d' }, '@media (hover: none)': { opacity: 1 } }}>
            <EyeOff size={15} />
          </IconButton>
        </Tooltip>
      )}
    </Card>
  );
});
