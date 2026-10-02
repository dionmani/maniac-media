import { Accordion, AccordionDetails, AccordionSummary, Chip, InputAdornment, Stack, TextField, Typography } from '@mui/material';
import { ChevronDown, HelpCircle, Search } from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { Empty, PageHeader } from '../components/common';

type Topic = 'Missing & counts' | 'Seasons & metadata' | 'Files & deleting' | 'Scanning';

interface Entry {
  q: string;
  topic: Topic;
  keywords: string;
  a: ReactNode;
}

// Add new questions here – this page is the app's running FAQ. Keep answers about what the app does and where to click.
const ENTRIES: Entry[] = [
  {
    q: 'Unaired episodes are showing as missing – are they?',
    topic: 'Missing & counts',
    keywords: 'unaired future air date missing wanted count progress',
    a: (
      <>
        No. An episode only counts as missing once its air date has passed. Episodes with a future air date show an <b>Unaired</b> chip and are left out
        of the missing count, the Wanted list, searches and the "x / y episodes" progress on the library and series pages. Episodes with <b>no</b> air
        date (TBA) are treated as aired, so they do count.
      </>
    ),
  },
  {
    q: 'The dashboard says I am missing thousands of episodes. Is that real?',
    topic: 'Missing & counts',
    keywords: 'dashboard missing total episodes inflated duplicate seasons',
    a: (
      <>
        Usually it is inflated by shows that have a second, wrong set of seasons (see the next question). The dashboard counts monitored episodes that have
        aired and have no file. Open <Link to="/wanted">Wanted</Link> to see the actual list, and use <b>Clean up seasons</b> on any show whose seasons
        look doubled. The dashboard also counts episodes in unmonitored seasons, which Wanted does not, so the two can differ slightly.
      </>
    ),
  },
  {
    q: 'A show has the same seasons twice (for example 1966–1971 and 1–12) with hundreds of missing files',
    topic: 'Seasons & metadata',
    keywords: 'duplicate seasons year numbering tmdb tvdb clean up stale',
    a: (
      <>
        Metadata providers number some shows differently (by year on one, season 1–12 on another). A refresh adds episodes but never removes any, so the
        other numbering is left behind as file-less rows. Open the show and click <b>Clean up seasons</b>: it previews the file-less seasons the current
        metadata no longer lists and removes them after you confirm. Files on disk are never touched by this.
      </>
    ),
  },
  {
    q: '"Clean up seasons" says there is nothing to clean up, but an old season is still there',
    topic: 'Seasons & metadata',
    keywords: 'clean up seasons nothing stale has file remnant',
    a: (
      <>
        Clean up only removes seasons and episodes that have <b>no file</b>. If the app still believes an episode has a file – for example you deleted the
        file by hand – the row is kept. Click <b>Rescan files</b> on the show first so the app notices the file is gone, then run Clean up seasons again.
        Or delete the episode directly with its trash icon.
      </>
    ),
  },
  {
    q: 'I deleted a file from the disk but the episode still shows as downloaded',
    topic: 'Scanning',
    keywords: 'deleted file disk still shows rescan scan missing',
    a: (
      <>
        The app only notices changes on disk when it scans. Use <b>Rescan files</b> on the show page (just that show), or <b>Scan folder</b> on the TV
        Shows page (everything). The episode then flips to Missing and a "Missing file" entry appears on <Link to="/issues">Issues</Link>.
      </>
    ),
  },
  {
    q: 'Refresh metadata did nothing',
    topic: 'Seasons & metadata',
    keywords: 'refresh metadata nothing change remove',
    a: (
      <>
        Refresh metadata updates titles, air dates and adds new episodes. It never removes episodes or changes whether a file is attached, so it cannot fix
        stale rows or files deleted by hand. Use <b>Clean up seasons</b>, <b>Rescan files</b> or the delete buttons for those.
      </>
    ),
  },
  {
    q: 'How do I remove a single episode or a whole season?',
    topic: 'Files & deleting',
    keywords: 'delete episode season trash remove recycle bin file',
    a: (
      <>
        Use the trash icon on the season header or on an episode row (needs delete permission). A dialog first shows what will be removed. If a file
        exists on the server you can choose to move it to the recycle bin; that needs an explicit tick-box acknowledgement before the button is enabled.
        Without the option the file stays on disk and a later scan may re-add it. A file that also holds other, remaining episodes (a multi-episode file) is
        never touched.
      </>
    ),
  },
  {
    q: 'Where do deleted files go – can I get one back?',
    topic: 'Files & deleting',
    keywords: 'recycle bin restore recover deleted files',
    a: (
      <>
        Files are moved to the recycle bin folder (dated sub-folders) rather than erased, and cleaned up automatically after the retention period set in
        Settings. To restore one, move it back from that folder by hand and run <b>Rescan files</b>.
      </>
    ),
  },
  {
    q: 'What is the difference between "Scan folder" and "Rescan files"?',
    topic: 'Scanning',
    keywords: 'scan folder rescan files library difference',
    a: (
      <>
        <b>Scan folder</b> (TV Shows / Movies page) checks the whole library root for new files and missing files. <b>Rescan files</b> (series page) does the
        same for just that one show, so it is quick.
      </>
    ),
  },
];

const TOPICS: Topic[] = ['Missing & counts', 'Seasons & metadata', 'Files & deleting', 'Scanning'];

export default function HelpPage() {
  const [query, setQuery] = useState('');
  const [topic, setTopic] = useState<Topic | null>(null);

  const shown = useMemo(() => {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    return ENTRIES.filter((e) => (!topic || e.topic === topic) && words.every((w) => `${e.q} ${e.keywords}`.toLowerCase().includes(w)));
  }, [query, topic]);

  return (
    <>
      <PageHeader title="Help & Q&A" icon={<HelpCircle />} subtitle="Answers to common questions about counts, seasons, scans and deleting" />
      <Stack spacing={2} sx={{ mb: 3 }}>
        <TextField
          size="small" placeholder="Search questions…" value={query} onChange={(e) => setQuery(e.target.value)}
          slotProps={{ input: { startAdornment: <InputAdornment position="start"><Search size={16} /></InputAdornment> } }}
        />
        <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
          {TOPICS.map((t) => (
            <Chip key={t} label={t} color={topic === t ? 'primary' : 'default'} variant={topic === t ? 'filled' : 'outlined'} onClick={() => setTopic(topic === t ? null : t)} />
          ))}
        </Stack>
      </Stack>
      {shown.length === 0 ? (
        <Empty icon={<HelpCircle size={40} />} title="No matching questions">Try fewer words, or clear the topic filter.</Empty>
      ) : (
        shown.map((e) => (
          <Accordion key={e.q} disableGutters sx={{ mb: 1, '&:before': { display: 'none' } }}>
            <AccordionSummary expandIcon={<ChevronDown size={18} />}>
              <Stack spacing={0.5}>
                <Typography fontWeight={700}>{e.q}</Typography>
                <Typography variant="caption" color="text.secondary">{e.topic}</Typography>
              </Stack>
            </AccordionSummary>
            <AccordionDetails>
              <Typography color="text.secondary" sx={{ lineHeight: 1.7 }}>{e.a}</Typography>
            </AccordionDetails>
          </Accordion>
        ))
      )}
    </>
  );
}
