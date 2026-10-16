import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControlLabel,
  Grid,
  IconButton,
  List,
  ListItem,
  MenuItem,
  Paper,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, ArrowUp, CheckCircle2, Download, FlaskConical, Pencil, Plus, Trash2, Upload, XCircle } from 'lucide-react';
import { useEffect, useState } from 'react';
import { del, get, post, put } from '../../api/client';
import type { CustomFormat, CustomFormatSpec, CustomFormatTestResult, QualityProfile } from '../../api/types';
import { Loading, useConfirm } from '../../components/common';
import { useAuth } from '../../store/auth';
import { toast } from '../../store/ui';
import { palette } from '../../theme';
import { errorMessage } from '../../utils/format';

interface QualityDef { name: string; resolution: string; source: string; weight: number }
const CODECS = ['H265', 'AV1', 'H264', 'XviD'];
const SOURCES = ['Remux', 'Bluray', 'WEB-DL', 'WEBRip', 'HDTV', 'DVD'];
const RESOLUTIONS = ['2160p', '1080p', '720p', 'SD'];
const QUALITY_MODIFIERS = ['remux', 'proper', 'repack', 'hdr'];
const PROTOCOLS = ['torrent', 'usenet'];

const SPEC_KINDS: { value: string; label: string }[] = [
  { value: 'release_title', label: 'Release Title (Regex)' },
  { value: 'release_group', label: 'Release Group (Regex)' },
  { value: 'source', label: 'Source' },
  { value: 'resolution', label: 'Resolution' },
  { value: 'codec', label: 'Codec' },
  { value: 'quality_modifier', label: 'Quality Modifier' },
  { value: 'size', label: 'Size (GB)' },
  { value: 'protocol', label: 'Protocol' },
  { value: 'indexer', label: 'Indexer ID' },
];

export default function QualitySettings() {
  const qc = useQueryClient();
  const canWrite = useAuth((s) => s.can)('settings:write');
  const { confirm, dialog } = useConfirm();
  const profiles = useQuery({ queryKey: ['quality-profiles'], queryFn: () => get<QualityProfile[]>('/quality-profiles') });
  const qualities = useQuery({ queryKey: ['qualities'], queryFn: () => get<QualityDef[]>('/qualities') });
  const formats = useQuery({ queryKey: ['custom-formats'], queryFn: () => get<CustomFormat[]>('/custom-formats') });

  const [editing, setEditing] = useState<Partial<QualityProfile> | null>(null);
  const [editingFormat, setEditingFormat] = useState<Partial<CustomFormat> | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [testerOpen, setTesterOpen] = useState(false);

  const save = useMutation({
    mutationFn: (p: Partial<QualityProfile>) => (p.id ? put(`/quality-profiles/${p.id}`, p) : post('/quality-profiles', p)),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['quality-profiles'] }); setEditing(null); toast('Profile saved', 'success'); },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const saveFormat = useMutation({
    mutationFn: (cf: Partial<CustomFormat>) => (cf.id ? put(`/custom-formats/${cf.id}`, cf) : post('/custom-formats', cf)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['custom-formats'] });
      setEditingFormat(null);
      toast('Custom format saved', 'success');
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const handleExport = async () => {
    try {
      const exported = await get<unknown[]>('/custom-formats/export');
      const blob = new Blob([JSON.stringify(exported, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'custom-formats.json';
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast(errorMessage(e), 'error');
    }
  };

  if (profiles.isLoading || qualities.isLoading || formats.isLoading) return <Loading />;
  const customFormats = formats.data ?? [];
  const profileList = profiles.data ?? [];

  return (
    <>
      <Stack direction="row" justifyContent="space-between" sx={{ mb: 2 }}>
        <Typography color="text.secondary">Profiles rank allowed qualities (top = best). Releases below the cutoff keep being upgraded; custom format scores and codec preferences break ties.</Typography>
        {canWrite && (
          <Button
            variant="contained"
            startIcon={<Plus size={16} />}
            onClick={() =>
              setEditing({
                name: 'New profile',
                items: ['Bluray-1080p', 'WEBDL-1080p'],
                cutoff: 'Bluray-1080p',
                upgrade_allowed: true,
                preferred_codecs: ['H265', 'H264'],
                rejected_codecs: [],
                min_size_mb: 0,
                max_size_mb: 0,
                prefer_proper: true,
                format_scores: {},
                min_format_score: 0,
                upgrade_until_format_score: 10000,
                min_format_upgrade_delta: 1,
              })
            }
          >
            Add profile
          </Button>
        )}
      </Stack>
      <Grid container spacing={2}>
        {profileList.map((p) => (
          <Grid key={p.id} size={{ xs: 12, md: 6, lg: 4 }}>
            <Paper sx={{ p: 2, height: '100%' }}>
              <Stack direction="row" justifyContent="space-between" alignItems="center">
                <Typography variant="h6">{p.name}</Typography>
                {canWrite && (
                  <Box>
                    <IconButton size="small" onClick={() => setEditing(p)}><Pencil size={15} /></IconButton>
                    <IconButton size="small" color="error" onClick={async () => {
                      if (await confirm('Delete profile?', `Delete "${p.name}"? Profiles in use cannot be deleted.`, { danger: true, confirmLabel: 'Delete' })) {
                        del(`/quality-profiles/${p.id}`).then(() => qc.invalidateQueries({ queryKey: ['quality-profiles'] })).catch((e) => toast(errorMessage(e), 'error'));
                      }
                    }}><Trash2 size={15} /></IconButton>
                  </Box>
                )}
              </Stack>
              <Typography variant="caption" color="text.secondary">
                Cutoff: {p.cutoff} · Upgrades {p.upgrade_allowed ? 'on' : 'off'}
                {(p.min_format_score ?? 0) !== 0 ? ` · Min CF: ${p.min_format_score}` : ''}
              </Typography>
              <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ mt: 1 }}>
                {p.items.map((q) => <Chip key={q} size="small" label={q} color={q === p.cutoff ? 'primary' : 'default'} variant={q === p.cutoff ? 'filled' : 'outlined'} />)}
              </Stack>
              {p.preferred_codecs.length > 0 && <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>Codecs: {p.preferred_codecs.join(' > ')}</Typography>}
            </Paper>
          </Grid>
        ))}
      </Grid>

      <Divider sx={{ my: 4 }} />

      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
        <Box>
          <Typography variant="h6">Custom formats</Typography>
          <Typography variant="body2" color="text.secondary">
            Fine-grained release matching rules (HDR/DV, audio codecs, release groups, streaming services, TRaSH Guides JSON).
          </Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          <Button variant="outlined" size="small" startIcon={<FlaskConical size={15} />} onClick={() => setTesterOpen(true)}>
            Test release
          </Button>
          {customFormats.length > 0 && (
            <Button variant="outlined" size="small" startIcon={<Download size={15} />} onClick={handleExport}>
              Export
            </Button>
          )}
          {canWrite && (
            <>
              <Button variant="outlined" size="small" startIcon={<Upload size={15} />} onClick={() => setImportOpen(true)}>
                Import JSON
              </Button>
              <Button
                variant="contained"
                size="small"
                startIcon={<Plus size={15} />}
                onClick={() =>
                  setEditingFormat({
                    name: '',
                    include_in_rename: false,
                    specifications: [{ name: 'Rule 1', kind: 'release_title', value: '', negate: false, required: true }],
                  })
                }
              >
                Add format
              </Button>
            </>
          )}
        </Stack>
      </Stack>

      {customFormats.length === 0 ? (
        <Paper sx={{ p: 3, textAlign: 'center' }}>
          <Typography color="text.secondary">
            No custom formats defined yet. Add one manually or import a TRaSH Guides / Radarr / Sonarr JSON definition.
          </Typography>
        </Paper>
      ) : (
        <Grid container spacing={2}>
          {customFormats.map((cf) => {
            const scoredIn = profileList
              .map((prof) => ({ name: prof.name, score: prof.format_scores?.[String(cf.id)] ?? 0 }))
              .filter((x) => x.score !== 0);
            return (
              <Grid key={cf.id} size={{ xs: 12, md: 6, lg: 4 }}>
                <Paper sx={{ p: 2, height: '100%' }}>
                  <Stack direction="row" justifyContent="space-between" alignItems="center">
                    <Stack direction="row" spacing={1} alignItems="center">
                      <Typography variant="subtitle1" fontWeight={600}>{cf.name}</Typography>
                      {cf.include_in_rename && <Chip size="small" label="Rename" variant="outlined" />}
                    </Stack>
                    {canWrite && (
                      <Box>
                        <IconButton size="small" onClick={() => setEditingFormat(cf)}><Pencil size={15} /></IconButton>
                        <IconButton
                          size="small"
                          color="error"
                          onClick={async () => {
                            if (await confirm('Delete custom format?', `Delete "${cf.name}"? It will also be removed from all quality profiles.`, { danger: true, confirmLabel: 'Delete' })) {
                              del(`/custom-formats/${cf.id}`)
                                .then(() => {
                                  qc.invalidateQueries({ queryKey: ['custom-formats'] });
                                  qc.invalidateQueries({ queryKey: ['quality-profiles'] });
                                })
                                .catch((e) => toast(errorMessage(e), 'error'));
                            }
                          }}
                        >
                          <Trash2 size={15} />
                        </IconButton>
                      </Box>
                    )}
                  </Stack>
                  <Typography variant="caption" color="text.secondary" display="block">
                    {cf.specifications.length} specification{cf.specifications.length === 1 ? '' : 's'}
                  </Typography>
                  <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ mt: 1 }}>
                    {cf.specifications.map((s, idx) => (
                      <Chip
                        key={`${s.name}-${idx}`}
                        size="small"
                        variant="outlined"
                        label={`${s.negate ? '!' : ''}${s.name} (${s.kind})`}
                      />
                    ))}
                  </Stack>
                  {scoredIn.length > 0 && (
                    <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ mt: 1 }}>
                      {scoredIn.map((entry) => (
                        <Chip
                          key={entry.name}
                          size="small"
                          color={entry.score > 0 ? 'success' : 'error'}
                          label={`${entry.name}: ${entry.score > 0 ? `+${entry.score}` : entry.score}`}
                        />
                      ))}
                    </Stack>
                  )}
                </Paper>
              </Grid>
            );
          })}
        </Grid>
      )}

      {editing && (
        <ProfileEditor
          profile={editing}
          qualities={qualities.data ?? []}
          customFormats={customFormats}
          onClose={() => setEditing(null)}
          onSave={(p) => save.mutate(p)}
        />
      )}
      {editingFormat && (
        <CustomFormatEditor
          format={editingFormat}
          onClose={() => setEditingFormat(null)}
          onSave={(cf) => saveFormat.mutate(cf)}
        />
      )}
      {importOpen && (
        <CustomFormatImportDialog
          onClose={() => setImportOpen(false)}
          onImported={() => {
            qc.invalidateQueries({ queryKey: ['custom-formats'] });
            setImportOpen(false);
          }}
        />
      )}
      {testerOpen && (
        <CustomFormatTesterDialog
          profiles={profileList}
          onClose={() => setTesterOpen(false)}
        />
      )}
      {dialog}
    </>
  );
}

function ProfileEditor({
  profile,
  qualities,
  customFormats,
  onClose,
  onSave,
}: {
  profile: Partial<QualityProfile>;
  qualities: QualityDef[];
  customFormats: CustomFormat[];
  onClose: () => void;
  onSave: (p: Partial<QualityProfile>) => void;
}) {
  const [p, setP] = useState<Partial<QualityProfile>>({
    format_scores: {},
    min_format_score: 0,
    upgrade_until_format_score: 10000,
    min_format_upgrade_delta: 1,
    ...profile,
  });
  const items = p.items ?? [];
  const unused = [...qualities].sort((a, b) => b.weight - a.weight).filter((q) => !items.includes(q.name));
  const move = (i: number, d: number) => {
    const next = [...items];
    [next[i], next[i + d]] = [next[i + d], next[i]];
    setP({ ...p, items: next });
  };
  const toggleCodec = (list: 'preferred_codecs' | 'rejected_codecs', c: string) => {
    const cur = p[list] ?? [];
    setP({ ...p, [list]: cur.includes(c) ? cur.filter((x) => x !== c) : [...cur, c] });
  };
  const setFormatScore = (id: number, score: number) => {
    const next = { ...(p.format_scores ?? {}) };
    if (score === 0) {
      delete next[String(id)];
    } else {
      next[String(id)] = score;
    }
    setP({ ...p, format_scores: next });
  };

  return (
    <Dialog open onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>{p.id ? 'Edit' : 'New'} quality profile</DialogTitle>
      <DialogContent>
        <Grid container spacing={3} sx={{ mt: 0 }}>
          <Grid size={{ xs: 12, md: 6 }}>
            <TextField fullWidth size="small" label="Name" value={p.name} onChange={(e) => setP({ ...p, name: e.target.value })} sx={{ mb: 2 }} />
            <Typography variant="subtitle2" sx={{ mb: 1 }}>Allowed qualities (best first)</Typography>
            <List dense sx={{ border: `1px solid ${palette.border}`, borderRadius: 2 }}>
              {items.map((q, i) => (
                <ListItem key={q} secondaryAction={
                  <>
                    <IconButton size="small" disabled={i === 0} onClick={() => move(i, -1)}><ArrowUp size={14} /></IconButton>
                    <IconButton size="small" disabled={i === items.length - 1} onClick={() => move(i, 1)}><ArrowDown size={14} /></IconButton>
                    <IconButton size="small" onClick={() => setP({ ...p, items: items.filter((x) => x !== q), cutoff: p.cutoff === q ? items[0] : p.cutoff })}><Trash2 size={14} /></IconButton>
                  </>
                }>
                  <Typography variant="body2" fontWeight={q === p.cutoff ? 700 : 400} color={q === p.cutoff ? 'primary' : undefined}>{i + 1}. {q}</Typography>
                </ListItem>
              ))}
            </List>
            <TextField select fullWidth size="small" label="Add quality" value="" sx={{ mt: 1 }} onChange={(e) => setP({ ...p, items: [...items, e.target.value] })}>
              {unused.map((q) => <MenuItem key={q.name} value={q.name}>{q.name}</MenuItem>)}
            </TextField>
          </Grid>
          <Grid size={{ xs: 12, md: 6 }}>
            <TextField select fullWidth size="small" label="Upgrade until (cutoff)" value={p.cutoff ?? ''} onChange={(e) => setP({ ...p, cutoff: e.target.value })} sx={{ mb: 2 }}>
              {items.map((q) => <MenuItem key={q} value={q}>{q}</MenuItem>)}
            </TextField>
            <FormControlLabel control={<Switch checked={!!p.upgrade_allowed} onChange={(e) => setP({ ...p, upgrade_allowed: e.target.checked })} />} label="Allow upgrades" />
            <FormControlLabel control={<Switch checked={!!p.prefer_proper} onChange={(e) => setP({ ...p, prefer_proper: e.target.checked })} />} label="Prefer PROPER / REPACK" />
            <Typography variant="subtitle2" sx={{ mt: 2 }}>Preferred codecs (in click order)</Typography>
            <Stack direction="row" spacing={1} sx={{ my: 1 }}>
              {CODECS.map((c) => {
                const idx = (p.preferred_codecs ?? []).indexOf(c);
                return <Chip key={c} label={idx >= 0 ? `${idx + 1}. ${c}` : c} color={idx >= 0 ? 'secondary' : 'default'} onClick={() => toggleCodec('preferred_codecs', c)} />;
              })}
            </Stack>
            <Typography variant="subtitle2">Rejected codecs</Typography>
            <Stack direction="row" spacing={1} sx={{ my: 1 }}>
              {CODECS.map((c) => (
                <FormControlLabel key={c} control={<Checkbox size="small" checked={(p.rejected_codecs ?? []).includes(c)} onChange={() => toggleCodec('rejected_codecs', c)} />} label={c} />
              ))}
            </Stack>
            <Stack direction="row" spacing={2} sx={{ mt: 1 }}>
              <TextField size="small" type="number" label="Min size (MB)" value={p.min_size_mb} onChange={(e) => setP({ ...p, min_size_mb: Number(e.target.value) })} helperText="0 = no limit" />
              <TextField size="small" type="number" label="Max size (MB)" value={p.max_size_mb} onChange={(e) => setP({ ...p, max_size_mb: Number(e.target.value) })} helperText="per episode / movie" />
            </Stack>
            <Typography variant="subtitle2" sx={{ mt: 2, mb: 1 }}>Custom format thresholds</Typography>
            <Stack direction="row" spacing={1.5}>
              <TextField
                size="small"
                type="number"
                label="Min format score"
                value={p.min_format_score ?? 0}
                onChange={(e) => setP({ ...p, min_format_score: Number(e.target.value) })}
              />
              <TextField
                size="small"
                type="number"
                label="Upgrade until score"
                value={p.upgrade_until_format_score ?? 10000}
                onChange={(e) => setP({ ...p, upgrade_until_format_score: Number(e.target.value) })}
              />
              <TextField
                size="small"
                type="number"
                label="Min upgrade delta"
                value={p.min_format_upgrade_delta ?? 1}
                onChange={(e) => setP({ ...p, min_format_upgrade_delta: Math.max(1, Number(e.target.value)) })}
              />
            </Stack>
          </Grid>

          {customFormats.length > 0 && (
            <Grid size={{ xs: 12 }}>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>Custom format scores</Typography>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Format</TableCell>
                    <TableCell width={160}>Score</TableCell>
                    <TableCell>Presets</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {customFormats.map((cf) => {
                    const curScore = p.format_scores?.[String(cf.id)] ?? 0;
                    return (
                      <TableRow key={cf.id}>
                        <TableCell>{cf.name}</TableCell>
                        <TableCell>
                          <TextField
                            size="small"
                            type="number"
                            value={curScore}
                            onChange={(e) => setFormatScore(cf.id, Number(e.target.value))}
                          />
                        </TableCell>
                        <TableCell>
                          <Stack direction="row" spacing={0.5}>
                            <Button size="small" color="error" variant="outlined" onClick={() => setFormatScore(cf.id, -10000)}>
                              Reject (-10000)
                            </Button>
                            <Button size="small" variant="outlined" onClick={() => setFormatScore(cf.id, 0)}>
                              Neutral (0)
                            </Button>
                            <Button size="small" color="success" variant="outlined" onClick={() => setFormatScore(cf.id, 100)}>
                              Prefer (+100)
                            </Button>
                          </Stack>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </Grid>
          )}
        </Grid>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" onClick={() => onSave(p)}>Save</Button>
      </DialogActions>
    </Dialog>
  );
}

function CustomFormatEditor({
  format,
  onClose,
  onSave,
}: {
  format: Partial<CustomFormat>;
  onClose: () => void;
  onSave: (cf: Partial<CustomFormat>) => void;
}) {
  const [cf, setCf] = useState<Partial<CustomFormat>>(format);
  const [sampleTitle, setSampleTitle] = useState('Movie.Name.2024.2160p.WEB-DL.DDP5.1.DV.HDR10.H.265-FLUX');
  const [testResult, setTestResult] = useState<CustomFormatTestResult | null>(null);
  const specs = cf.specifications ?? [];

  useEffect(() => {
    if (!sampleTitle.trim() || specs.length === 0) {
      setTestResult(null);
      return;
    }
    const timer = setTimeout(() => {
      post<CustomFormatTestResult>('/custom-formats/test', {
        title: sampleTitle,
        specifications: specs,
      })
        .then(setTestResult)
        .catch(() => setTestResult(null));
    }, 300);
    return () => clearTimeout(timer);
  }, [sampleTitle, specs]);

  const updateSpec = (idx: number, patch: Partial<CustomFormatSpec>) => {
    const next = specs.map((s, i) => (i === idx ? { ...s, ...patch } : s));
    setCf({ ...cf, specifications: next });
  };

  const handleKindChange = (idx: number, kind: string) => {
    let defaultVal: unknown = '';
    if (kind === 'source') defaultVal = 'WEB-DL';
    else if (kind === 'resolution') defaultVal = '1080p';
    else if (kind === 'codec') defaultVal = 'H265';
    else if (kind === 'quality_modifier') defaultVal = 'remux';
    else if (kind === 'protocol') defaultVal = 'torrent';
    else if (kind === 'indexer') defaultVal = 1;
    else if (kind === 'size') defaultVal = { min_gb: 0, max_gb: 25 };
    updateSpec(idx, { kind, value: defaultVal });
  };

  return (
    <Dialog open onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>{cf.id ? 'Edit' : 'New'} custom format</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <Stack direction="row" spacing={2} alignItems="center">
            <TextField
              fullWidth
              size="small"
              label="Format name"
              value={cf.name ?? ''}
              onChange={(e) => setCf({ ...cf, name: e.target.value })}
            />
            <FormControlLabel
              control={
                <Switch
                  checked={!!cf.include_in_rename}
                  onChange={(e) => setCf({ ...cf, include_in_rename: e.target.checked })}
                />
              }
              label="Include in rename ({CustomFormats})"
              sx={{ whiteSpace: 'nowrap' }}
            />
          </Stack>

          <Stack direction="row" justifyContent="space-between" alignItems="center">
            <Typography variant="subtitle2">Specifications</Typography>
            <Button
              size="small"
              startIcon={<Plus size={14} />}
              onClick={() =>
                setCf({
                  ...cf,
                  specifications: [
                    ...specs,
                    { name: `Rule ${specs.length + 1}`, kind: 'release_title', value: '', negate: false, required: false },
                  ],
                })
              }
            >
              Add specification
            </Button>
          </Stack>

          {specs.map((s, idx) => {
            const specEval = testResult?.adhoc?.specifications?.[idx];
            const sizeVal = (typeof s.value === 'object' && s.value !== null ? s.value : { min_gb: 0, max_gb: 0 }) as {
              min_gb?: number;
              max_gb?: number;
            };
            return (
              <Paper key={idx} variant="outlined" sx={{ p: 1.5 }}>
                <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap>
                  <TextField
                    size="small"
                    label="Spec name"
                    value={s.name}
                    onChange={(e) => updateSpec(idx, { name: e.target.value })}
                    sx={{ width: 170 }}
                  />
                  <TextField
                    select
                    size="small"
                    label="Kind"
                    value={s.kind}
                    onChange={(e) => handleKindChange(idx, e.target.value)}
                    sx={{ width: 190 }}
                  >
                    {SPEC_KINDS.map((k) => (
                      <MenuItem key={k.value} value={k.value}>{k.label}</MenuItem>
                    ))}
                  </TextField>

                  {s.kind === 'source' ? (
                    <TextField
                      select
                      size="small"
                      label="Source"
                      value={String(s.value || 'WEB-DL')}
                      onChange={(e) => updateSpec(idx, { value: e.target.value })}
                      sx={{ minWidth: 150, flex: 1 }}
                    >
                      {SOURCES.map((v) => <MenuItem key={v} value={v}>{v}</MenuItem>)}
                    </TextField>
                  ) : s.kind === 'resolution' ? (
                    <TextField
                      select
                      size="small"
                      label="Resolution"
                      value={String(s.value || '1080p')}
                      onChange={(e) => updateSpec(idx, { value: e.target.value })}
                      sx={{ minWidth: 150, flex: 1 }}
                    >
                      {RESOLUTIONS.map((v) => <MenuItem key={v} value={v}>{v}</MenuItem>)}
                    </TextField>
                  ) : s.kind === 'codec' ? (
                    <TextField
                      select
                      size="small"
                      label="Codec"
                      value={String(s.value || 'H265')}
                      onChange={(e) => updateSpec(idx, { value: e.target.value })}
                      sx={{ minWidth: 150, flex: 1 }}
                    >
                      {CODECS.map((v) => <MenuItem key={v} value={v}>{v}</MenuItem>)}
                    </TextField>
                  ) : s.kind === 'quality_modifier' ? (
                    <TextField
                      select
                      size="small"
                      label="Modifier"
                      value={String(s.value || 'remux')}
                      onChange={(e) => updateSpec(idx, { value: e.target.value })}
                      sx={{ minWidth: 150, flex: 1 }}
                    >
                      {QUALITY_MODIFIERS.map((v) => <MenuItem key={v} value={v}>{v}</MenuItem>)}
                    </TextField>
                  ) : s.kind === 'protocol' ? (
                    <TextField
                      select
                      size="small"
                      label="Protocol"
                      value={String(s.value || 'torrent')}
                      onChange={(e) => updateSpec(idx, { value: e.target.value })}
                      sx={{ minWidth: 150, flex: 1 }}
                    >
                      {PROTOCOLS.map((v) => <MenuItem key={v} value={v}>{v}</MenuItem>)}
                    </TextField>
                  ) : s.kind === 'size' ? (
                    <>
                      <TextField
                        size="small"
                        type="number"
                        label="Min GB"
                        value={sizeVal.min_gb ?? 0}
                        onChange={(e) => updateSpec(idx, { value: { ...sizeVal, min_gb: Number(e.target.value) } })}
                        sx={{ width: 100 }}
                      />
                      <TextField
                        size="small"
                        type="number"
                        label="Max GB"
                        value={sizeVal.max_gb ?? 0}
                        onChange={(e) => updateSpec(idx, { value: { ...sizeVal, max_gb: Number(e.target.value) } })}
                        sx={{ width: 100 }}
                      />
                    </>
                  ) : s.kind === 'indexer' ? (
                    <TextField
                      size="small"
                      type="number"
                      label="Indexer ID"
                      value={Number(s.value || 1)}
                      onChange={(e) => updateSpec(idx, { value: Number(e.target.value) })}
                      sx={{ width: 120 }}
                    />
                  ) : (
                    <TextField
                      size="small"
                      label="Regex pattern"
                      value={String(s.value ?? '')}
                      onChange={(e) => updateSpec(idx, { value: e.target.value })}
                      sx={{ minWidth: 200, flex: 1, fontFamily: "'JetBrains Mono', monospace" }}
                    />
                  )}

                  <FormControlLabel
                    control={<Checkbox size="small" checked={!!s.required} onChange={(e) => updateSpec(idx, { required: e.target.checked })} />}
                    label="Required"
                  />
                  <FormControlLabel
                    control={<Checkbox size="small" checked={!!s.negate} onChange={(e) => updateSpec(idx, { negate: e.target.checked })} />}
                    label="Negate"
                  />
                  {specEval && (
                    <Tooltip title={specEval.passed ? 'Specification passed' : 'Specification did not match'}>
                      <span>
                        {specEval.passed ? <CheckCircle2 size={16} color={palette.green} /> : <XCircle size={16} color={palette.red} />}
                      </span>
                    </Tooltip>
                  )}
                  <IconButton
                    size="small"
                    color="error"
                    disabled={specs.length <= 1}
                    onClick={() => setCf({ ...cf, specifications: specs.filter((_, i) => i !== idx) })}
                  >
                    <Trash2 size={15} />
                  </IconButton>
                </Stack>
              </Paper>
            );
          })}

          <Divider />
          <Box>
            <Typography variant="subtitle2" sx={{ mb: 1 }}>Live specification tester</Typography>
            <TextField
              fullWidth
              size="small"
              label="Sample release title"
              value={sampleTitle}
              onChange={(e) => setSampleTitle(e.target.value)}
            />
            {testResult?.adhoc?.errors && testResult.adhoc.errors.length > 0 && (
              <Alert severity="error" sx={{ mt: 1 }}>
                {testResult.adhoc.errors.join('; ')}
              </Alert>
            )}
            {testResult?.adhoc && (!testResult.adhoc.errors || testResult.adhoc.errors.length === 0) && (
              <Alert severity={testResult.adhoc.matched ? 'success' : 'info'} sx={{ mt: 1 }}>
                {testResult.adhoc.matched ? 'Format MATCHES sample release' : 'Format does NOT match sample release'}
              </Alert>
            )}
          </Box>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" onClick={() => onSave(cf)}>Save</Button>
      </DialogActions>
    </Dialog>
  );
}

function CustomFormatImportDialog({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const [jsonText, setJsonText] = useState('');
  const [url, setUrl] = useState('');

  const doImport = useMutation({
    mutationFn: () => {
      if (url.trim()) {
        return post<{ imported: string[]; updated: string[]; warnings: string[] }>('/custom-formats/import', { url: url.trim() });
      }
      return post<{ imported: string[]; updated: string[]; warnings: string[] }>('/custom-formats/import', { json: jsonText });
    },
    onSuccess: (res) => {
      const count = (res.imported?.length ?? 0) + (res.updated?.length ?? 0);
      toast(`Imported/updated ${count} custom format(s)`, 'success');
      if (res.warnings?.length) {
        toast(res.warnings.join('; '), 'info');
      }
      onImported();
    },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Import Custom Format JSON (TRaSH / Radarr / Sonarr)</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField
            fullWidth
            size="small"
            label="Import from URL (optional)"
            placeholder="https://raw.githubusercontent.com/TRaSH-Guides/..."
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <TextField
            fullWidth
            multiline
            minRows={8}
            maxRows={16}
            label="Or paste Radarr / Sonarr / TRaSH Custom Format JSON"
            value={jsonText}
            onChange={(e) => setJsonText(e.target.value)}
            disabled={!!url.trim()}
            sx={{ fontFamily: "'JetBrains Mono', monospace" }}
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="contained"
          disabled={doImport.isPending || (!jsonText.trim() && !url.trim())}
          onClick={() => doImport.mutate()}
        >
          Import
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function CustomFormatTesterDialog({ profiles, onClose }: { profiles: QualityProfile[]; onClose: () => void }) {
  const [title, setTitle] = useState('Dune.Part.Two.2024.2160p.AMZN.WEB-DL.DDP5.1.Atmos.DV.HDR10.H.265-FLUX');
  const [sizeGb, setSizeGb] = useState(15);
  const [profileId, setProfileId] = useState<number>(profiles[0]?.id ?? 1);

  const test = useMutation({
    mutationFn: () =>
      post<CustomFormatTestResult>('/custom-formats/test', {
        title,
        size_bytes: Math.round(sizeGb * 1024 * 1024 * 1024),
        profile_id: profileId,
      }),
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  useEffect(() => {
    if (title.trim()) {
      test.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Dialog open onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>Custom Format Release Tester</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <Stack direction="row" spacing={1.5}>
            <TextField
              fullWidth
              size="small"
              label="Release title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
            <TextField
              size="small"
              type="number"
              label="Size (GB)"
              value={sizeGb}
              onChange={(e) => setSizeGb(Number(e.target.value))}
              sx={{ width: 110 }}
            />
            <TextField
              select
              size="small"
              label="Profile"
              value={profileId}
              onChange={(e) => setProfileId(Number(e.target.value))}
              sx={{ minWidth: 160 }}
            >
              {profiles.map((p) => (
                <MenuItem key={p.id} value={p.id}>{p.name}</MenuItem>
              ))}
            </TextField>
            <Button variant="contained" onClick={() => test.mutate()} disabled={!title.trim()}>
              Test
            </Button>
          </Stack>

          {test.data && (
            <>
              <Paper variant="outlined" sx={{ p: 1.5 }}>
                <Typography variant="caption" fontWeight={700} display="block" sx={{ mb: 0.5 }}>
                  Parsed release facts
                </Typography>
                <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                  {Object.entries(test.data.facts)
                    .filter(([k, v]) => k !== 'title' && v !== null && v !== false && v !== '')
                    .map(([k, v]) => (
                      <Chip key={k} size="small" variant="outlined" label={`${k}: ${String(v)}`} />
                    ))}
                </Stack>
              </Paper>

              <Alert severity={test.data.total_score >= 0 ? 'success' : 'warning'}>
                Total custom format score: <strong>{test.data.total_score >= 0 ? `+${test.data.total_score}` : test.data.total_score}</strong>
                {' '}({test.data.matches.length} matched format{test.data.matches.length === 1 ? '' : 's'})
              </Alert>

              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Format</TableCell>
                    <TableCell>Status</TableCell>
                    <TableCell>Matched Specs</TableCell>
                    <TableCell align="right">Profile Score</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {test.data.formats.map((f) => (
                    <TableRow key={f.format_id}>
                      <TableCell sx={{ fontWeight: f.matched ? 600 : 400 }}>{f.name}</TableCell>
                      <TableCell>
                        <Chip
                          size="small"
                          color={f.matched ? 'success' : 'default'}
                          label={f.matched ? 'Matched' : 'No match'}
                        />
                      </TableCell>
                      <TableCell>{f.matched_specs.join(', ') || '—'}</TableCell>
                      <TableCell align="right">
                        {f.matched ? (f.score >= 0 ? `+${f.score}` : f.score) : '—'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
