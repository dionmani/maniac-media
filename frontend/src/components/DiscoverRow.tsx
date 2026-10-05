import { Alert, Box, Button, Chip, IconButton, Paper, Stack, Typography } from '@mui/material';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useRef } from 'react';
import type { DiscoverItem, DiscoverSection } from '../api/types';
import { DiscoverCard } from './DiscoverCard';

const CARD_WIDTH = 150;

type Props = {
  section: DiscoverSection;
  isNew: (item: DiscoverItem) => boolean;
  onOpen: (item: DiscoverItem) => void;
  onHide?: (item: DiscoverItem) => void;
  onSeeAll: () => void;
};

/** One Discover section as a horizontally scrolling row of posters. */
export function DiscoverRow({ section, isNew, onOpen, onHide, onSeeAll }: Props) {
  const scroller = useRef<HTMLDivElement>(null);
  const scroll = (direction: 1 | -1) => scroller.current?.scrollBy({ left: direction * scroller.current.clientWidth * 0.8, behavior: 'smooth' });
  const fresh = section.items.filter(isNew).length;
  return (
    <Paper sx={{ p: { xs: 2, md: 2.5 } }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
        <Typography variant="h6" sx={{ flex: 1, minWidth: 0 }} noWrap>
          {section.label}{' '}
          <Typography component="span" variant="body2" color="text.secondary">{section.total}</Typography>
        </Typography>
        {fresh > 0 && <Chip size="small" color="secondary" label={`${fresh} new`} />}
        <Box sx={{ display: { xs: 'none', md: 'flex' } }}>
          <IconButton size="small" aria-label="Scroll left" onClick={() => scroll(-1)}><ChevronLeft size={18} /></IconButton>
          <IconButton size="small" aria-label="Scroll right" onClick={() => scroll(1)}><ChevronRight size={18} /></IconButton>
        </Box>
        <Button size="small" onClick={onSeeAll} disabled={!section.total}>See all</Button>
      </Stack>
      {section.error ? (
        <Alert severity="warning">Couldn't load this section: {section.error}</Alert>
      ) : section.items.length === 0 ? (
        <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>Nothing here right now.</Typography>
      ) : (
        <Stack ref={scroller} direction="row" spacing={1.5} sx={{ overflowX: 'auto', pb: 1, pt: 0.75, scrollSnapType: 'x proximity', '& > *': { scrollSnapAlign: 'start' } }}>
          {section.items.map((item) => (
            <DiscoverCard key={item.tmdb_id} item={item} isNew={isNew(item)} onOpen={onOpen} onHide={onHide} width={CARD_WIDTH} />
          ))}
        </Stack>
      )}
    </Paper>
  );
}
