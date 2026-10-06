import { Alert, Box, Button, Chip, FormControlLabel, Grid, MenuItem, Stack, Switch, TextField, Tooltip, Typography } from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Save } from 'lucide-react';
import { useEffect, useState } from 'react';
import { get, post, put } from '../../api/client';
import { ErrorAlert, Loading, Section } from '../../components/common';
import { useAuth } from '../../store/auth';
import { toast } from '../../store/ui';
import { errorMessage } from '../../utils/format';

type Settings = Record<string, unknown> & {
  media_root_movies: string; media_root_tv: string; download_folder: string; recycle_bin: string; recycle_bin_days: number;
  movie_folder_format: string; movie_format: string; series_folder_format: string; tv_format: string; rename_on_import: boolean;
  auto_import: boolean; import_mode: string; min_import_size_mb: number; watch_download_folder: boolean; download_poll_seconds: number; import_untracked_downloads: boolean;
  search_on_add: boolean; auto_retry_failed_downloads: boolean; max_download_retries: number; release_gate_catalog_days: number; metadata_language: string;
  seerr_allow_file_deletion: boolean; discover_region: string; discover_languages: string[];
  access_token_ttl_minutes: number; rate_limit_per_minute: number; metrics_require_auth: boolean;
  job_retention_days: number; history_retention_days: number; audit_retention_days: number; db_connection: string;
};

function useSettingsForm() {
  const qc = useQueryClient();
  const query = useQuery({ queryKey: ['settings'], queryFn: () => get<Settings>('/settings') });
  const [form, setForm] = useState<Settings | null>(null);
  useEffect(() => { if (query.data) setForm(query.data); }, [query.data]);
  const save = useMutation({
    mutationFn: (keys: string[]) => put<Settings>('/settings', Object.fromEntries(keys.map((k) => [k, form![k]]))),
    onSuccess: (data) => { qc.setQueryData(['settings'], { ...query.data, ...data }); toast('Settings saved', 'success'); },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const set = (k: string, v: unknown) => setForm((f) => (f ? { ...f, [k]: v } : f));
  return { query, form, set, save };
}

export function MediaManagement() {
  const { query, form, set, save } = useSettingsForm();
  const canWrite = useAuth((s) => s.can)('settings:write');
  if (query.isLoading || !form) return query.error ? <ErrorAlert error={query.error} /> : <Loading />;
  const keys = ['media_root_movies', 'media_root_tv', 'download_folder', 'recycle_bin', 'recycle_bin_days', 'auto_import', 'import_mode',
    'min_import_size_mb', 'watch_download_folder', 'download_poll_seconds', 'import_untracked_downloads', 'search_on_add', 'auto_retry_failed_downloads', 'max_download_retries', 'release_gate_catalog_days', 'metadata_language', 'seerr_allow_file_deletion'];
  const text = (k: keyof Settings, label: string, help?: string) => (
    <TextField fullWidth size="small" label={label} value={form[k] as string} helperText={help} onChange={(e) => set(k as string, e.target.value)} disabled={!canWrite} />
  );
  const toggle = (k: keyof Settings, label: string, help?: string) => (
    <Tooltip title={help ?? ''} placement="top-start">
      <FormControlLabel control={<Switch checked={!!form[k]} onChange={(e) => set(k as string, e.target.checked)} disabled={!canWrite} />} label={label} />
    </Tooltip>
  );
  return (
    <Stack spacing={2}>
      <Section title="Folders">
        <Grid container spacing={2}>
          <Grid size={{ xs: 12, md: 6 }}>{text('media_root_movies', 'Movies root folder')}</Grid>
          <Grid size={{ xs: 12, md: 6 }}>{text('media_root_tv', 'TV shows root folder')}</Grid>
          <Grid size={{ xs: 12, md: 6 }}>{text('download_folder', 'Download watch folder', 'Completed downloads land here; watched for changes in real time')}</Grid>
          <Grid size={{ xs: 12, md: 4 }}>{text('recycle_bin', 'Recycle bin', 'Replaced and deleted files are moved here, never destroyed')}</Grid>
          <Grid size={{ xs: 12, md: 2 }}>
            <TextField fullWidth size="small" type="number" label="Keep (days)" value={form.recycle_bin_days} onChange={(e) => set('recycle_bin_days', Number(e.target.value))} disabled={!canWrite} />
          </Grid>
        </Grid>
      </Section>
      <Section title="Importing">
        <Grid container spacing={2}>
          <Grid size={{ xs: 12, md: 4 }}>
            <TextField select fullWidth size="small" label="Import mode" value={form.import_mode} onChange={(e) => set('import_mode', e.target.value)} disabled={!canWrite}
              helperText="Torrents always hardlink (falls back to copy) so seeding keeps working">
              <MenuItem value="move">Move (atomic when on the same volume)</MenuItem>
              <MenuItem value="copy">Copy</MenuItem>
              <MenuItem value="hardlink">Hardlink</MenuItem>
              <MenuItem value="symlink">Symlink</MenuItem>
            </TextField>
          </Grid>
          <Grid size={{ xs: 12, md: 4 }}>
            <TextField fullWidth size="small" type="number" label="Minimum file size (MB)" value={form.min_import_size_mb} onChange={(e) => set('min_import_size_mb', Number(e.target.value))} disabled={!canWrite} />
          </Grid>
          <Grid size={{ xs: 12, md: 4 }}>
            <TextField fullWidth size="small" type="number" label="Max automatic retries" value={form.max_download_retries} onChange={(e) => set('max_download_retries', Number(e.target.value))} disabled={!canWrite} />
          </Grid>
          <Grid size={{ xs: 12, md: 4 }}>
            <TextField fullWidth size="small" type="number" label="Check active downloads every (seconds)" value={form.download_poll_seconds}
              inputProps={{ min: 5, max: 600 }} helperText="Finished downloads are still imported right away by the folder watcher"
              onChange={(e) => set('download_poll_seconds', Number(e.target.value))} disabled={!canWrite} />
          </Grid>
          <Grid size={{ xs: 12, md: 4 }}>
            <TextField fullWidth size="small" type="number" label="Back-catalogue movies after (days)" value={form.release_gate_catalog_days}
              inputProps={{ min: 0, max: 3650 }}
              helperText="Movies are only searched once out digitally or on disc. One with no such date on record counts as out this long after its first release. 0 = never"
              onChange={(e) => set('release_gate_catalog_days', Number(e.target.value))} disabled={!canWrite} />
          </Grid>
        </Grid>
        <Stack sx={{ mt: 1 }}>
          {toggle('auto_import', 'Automatically import completed downloads')}
          {toggle('watch_download_folder', 'Watch the download folder for changes (event-driven)')}
          {toggle('import_untracked_downloads', 'Import untracked downloads', 'Also import files placed in the watch folder that were not grabbed by Maniac Media, when they match a library item')}
          {toggle('auto_retry_failed_downloads', 'On failure: blocklist the release and search for another')}
          {toggle('search_on_add', 'Search for new titles as soon as they are added')}
          {toggle('seerr_allow_file_deletion', 'Let Seerr delete files when it removes a movie', 'Seerr always asks for files to be deleted when an admin removes a title. Off: only the library entry is removed. On: files are moved to the recycle bin.')}
        </Stack>
      </Section>
      {canWrite && <Box><Button variant="contained" startIcon={<Save size={16} />} onClick={() => save.mutate(keys)} disabled={save.isPending}>Save</Button></Box>}
    </Stack>
  );
}

const DISCOVER_REGIONS = ['US', 'CA', 'GB', 'IE', 'AU', 'NZ', 'DE', 'AT', 'CH', 'FR', 'BE', 'NL', 'ES', 'PT', 'IT', 'SE', 'NO', 'DK', 'FI', 'PL', 'BR', 'MX', 'AR', 'IN', 'JP', 'KR', 'ZA'];

function regionName(code: string): string {
  try {
    return new Intl.DisplayNames(undefined, { type: 'region' }).of(code) ?? code;
  } catch {
    return code;
  }
}

export function DiscoverSettings() {
  const { query, form, set, save } = useSettingsForm();
  const qc = useQueryClient();
  const canWrite = useAuth((s) => s.can)('settings:write');
  const [languages, setLanguages] = useState('');
  useEffect(() => { if (query.data) setLanguages((query.data.discover_languages ?? []).join(', ')); }, [query.data]);
  if (query.isLoading || !form) return query.error ? <ErrorAlert error={query.error} /> : <Loading />;
  const regions = DISCOVER_REGIONS.includes(form.discover_region) ? DISCOVER_REGIONS : [form.discover_region, ...DISCOVER_REGIONS];
  const editLanguages = (text: string) => {
    setLanguages(text);
    set('discover_languages', text.split(/[\s,]+/).map((l) => l.trim().toLowerCase()).filter(Boolean));
  };
  const submit = () => save.mutate(['discover_region', 'discover_languages'], { onSuccess: () => qc.invalidateQueries({ queryKey: ['discover'] }) });
  return (
    <Section title="Discover">
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        The Discover page lists new and upcoming titles from TMDB. The region decides which theatrical, digital and disc release dates and
        streaming services are shown.
      </Typography>
      <Grid container spacing={2}>
        <Grid size={{ xs: 12, md: 4 }}>
          <TextField select fullWidth size="small" label="Region" value={form.discover_region} onChange={(e) => set('discover_region', e.target.value)} disabled={!canWrite}>
            {regions.map((r) => <MenuItem key={r} value={r}>{regionName(r)} ({r})</MenuItem>)}
          </TextField>
        </Grid>
        <Grid size={{ xs: 12, md: 8 }}>
          <TextField fullWidth size="small" label="Original languages" value={languages} onChange={(e) => editLanguages(e.target.value)} disabled={!canWrite}
            placeholder="Any language" helperText="Two-letter codes, comma separated (e.g. en, es). Leave empty to include every language." />
        </Grid>
      </Grid>
      {canWrite && <Box sx={{ mt: 2 }}><Button variant="contained" startIcon={<Save size={16} />} onClick={submit} disabled={save.isPending}>Save</Button></Box>}
    </Section>
  );
}

const TOKEN_LABELS: Record<string, string> = {
  title: 'Title', year: 'Year', quality: 'Quality', resolution: 'Resolution', source: 'Source', codec: 'Codec', imdbid: 'ImdbId',
  tmdbid: 'TmdbId', tvdbid: 'TvdbId', releasegroup: 'ReleaseGroup', edition: 'Edition', originalname: 'OriginalName',
  episodetitle: 'EpisodeTitle', airdate: 'AirDate',
};

const TEMPLATES: { key: string; label: string; kind: 'movie' | 'episode' }[] = [
  { key: 'movie_folder_format', label: 'Movie folder', kind: 'movie' },
  { key: 'movie_format', label: 'Movie file', kind: 'movie' },
  { key: 'series_folder_format', label: 'Series folder', kind: 'episode' },
  { key: 'tv_format', label: 'Episode file (relative to series folder, may include season folder)', kind: 'episode' },
];

export function NamingSettings() {
  const { query, form, set, save } = useSettingsForm();
  const canWrite = useAuth((s) => s.can)('settings:write');
  if (query.isLoading || !form) return <Loading />;
  return (
    <Stack spacing={2}>
      <Alert severity="info">
        Templates use tokens such as <code>{'{Title}'}</code>, <code>{'{Year}'}</code>, <code>{'{Quality}'}</code>, <code>{'{season:02d}'}</code>.
        Empty tokens collapse their brackets. Only whitelisted tokens are accepted – templates cannot execute code or escape the library folder.
      </Alert>
      {TEMPLATES.map((t) => (
        <TemplateField key={t.key} label={t.label} kind={t.kind} value={form[t.key] as string} onChange={(v) => set(t.key, v)} disabled={!canWrite} />
      ))}
      <FormControlLabel control={<Switch checked={!!form.rename_on_import} onChange={(e) => set('rename_on_import', e.target.checked)} disabled={!canWrite} />}
        label="Rename files when importing" />
      {canWrite && (
        <Box><Button variant="contained" startIcon={<Save size={16} />} disabled={save.isPending}
          onClick={() => save.mutate([...TEMPLATES.map((t) => t.key), 'rename_on_import'])}>Save templates</Button></Box>
      )}
    </Stack>
  );
}

function TemplateField({ label, kind, value, onChange, disabled }: { label: string; kind: 'movie' | 'episode'; value: string; onChange: (v: string) => void; disabled: boolean }) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => { const t = setTimeout(() => setDebounced(value), 250); return () => clearTimeout(t); }, [value]);
  const preview = useQuery({
    queryKey: ['naming-preview', kind, debounced],
    queryFn: () => post<{ example: string | null; errors: string[]; tokens: Record<string, string> }>('/settings/naming/preview', { template: debounced, kind }),
  });
  return (
    <Section>
      <TextField fullWidth label={label} value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}
        error={!!preview.data?.errors.length} helperText={preview.data?.errors.join('; ') || (preview.data?.example ? `Example: ${preview.data.example}` : ' ')}
        InputProps={{ sx: { fontFamily: "'JetBrains Mono', monospace" } }} />
      <Stack direction="row" spacing={0.5} sx={{ mt: 1 }} flexWrap="wrap" useFlexGap>
        {Object.entries(preview.data?.tokens ?? {}).map(([k, desc]) => {
          const token = k === 'season' || k === 'episode' ? `{${k}:02d}` : `{${TOKEN_LABELS[k] ?? k}}`;
          return (
            <Tooltip key={k} title={desc}>
              <Chip size="small" variant="outlined" label={token} onClick={disabled ? undefined : () => onChange(value + token)} />
            </Tooltip>
          );
        })}
      </Stack>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>Click a token to append it.</Typography>
    </Section>
  );
}

export function SecuritySettings() {
  const { query, form, set, save } = useSettingsForm();
  if (query.isLoading || !form) return <Loading />;
  const keys = ['access_token_ttl_minutes', 'rate_limit_per_minute', 'metrics_require_auth', 'job_retention_days', 'history_retention_days', 'audit_retention_days'];
  const num = (k: keyof Settings, label: string) => (
    <TextField fullWidth size="small" type="number" label={label} value={form[k] as number} onChange={(e) => set(k as string, Number(e.target.value))} />
  );
  return (
    <Stack spacing={2}>
      <Section title="API & sessions">
        <Grid container spacing={2}>
          <Grid size={{ xs: 12, md: 4 }}>{num('access_token_ttl_minutes', 'Session lifetime (minutes)')}</Grid>
          <Grid size={{ xs: 12, md: 4 }}>{num('rate_limit_per_minute', 'API rate limit per client / minute')}</Grid>
        </Grid>
        <FormControlLabel sx={{ mt: 1 }} control={<Switch checked={!!form.metrics_require_auth} onChange={(e) => set('metrics_require_auth', e.target.checked)} />}
          label="Require a bearer token for /metrics (Prometheus)" />
      </Section>
      <Section title="Retention">
        <Grid container spacing={2}>
          <Grid size={{ xs: 12, md: 4 }}>{num('job_retention_days', 'Finished jobs (days)')}</Grid>
          <Grid size={{ xs: 12, md: 4 }}>{num('history_retention_days', 'History (days)')}</Grid>
          <Grid size={{ xs: 12, md: 4 }}>{num('audit_retention_days', 'Audit log (days, min 30)')}</Grid>
        </Grid>
      </Section>
      <Box><Button variant="contained" startIcon={<Save size={16} />} onClick={() => save.mutate(keys)}>Save</Button></Box>
    </Stack>
  );
}
