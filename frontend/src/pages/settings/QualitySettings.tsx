import {
  Box,
  Button,
  Checkbox,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  Grid,
  IconButton,
  List,
  ListItem,
  MenuItem,
  Paper,
  Stack,
  Switch,
  TextField,
  Typography,
} from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, ArrowUp, Pencil, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { del, get, post, put } from '../../api/client';
import type { QualityProfile } from '../../api/types';
import { Loading, useConfirm } from '../../components/common';
import { useAuth } from '../../store/auth';
import { toast } from '../../store/ui';
import { palette } from '../../theme';
import { errorMessage } from '../../utils/format';

interface QualityDef { name: string; resolution: string; source: string; weight: number }
const CODECS = ['H265', 'AV1', 'H264', 'XviD'];

export default function QualitySettings() {
  const qc = useQueryClient();
  const canWrite = useAuth((s) => s.can)('settings:write');
  const { confirm, dialog } = useConfirm();
  const profiles = useQuery({ queryKey: ['quality-profiles'], queryFn: () => get<QualityProfile[]>('/quality-profiles') });
  const qualities = useQuery({ queryKey: ['qualities'], queryFn: () => get<QualityDef[]>('/qualities') });
  const [editing, setEditing] = useState<Partial<QualityProfile> | null>(null);

  const save = useMutation({
    mutationFn: (p: Partial<QualityProfile>) => (p.id ? put(`/quality-profiles/${p.id}`, p) : post('/quality-profiles', p)),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['quality-profiles'] }); setEditing(null); toast('Profile saved', 'success'); },
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  if (profiles.isLoading || qualities.isLoading) return <Loading />;
  return (
    <>
      <Stack direction="row" justifyContent="space-between" sx={{ mb: 2 }}>
        <Typography color="text.secondary">Profiles rank allowed qualities (top = best). Releases below the cutoff keep being upgraded; codec preferences break ties.</Typography>
        {canWrite && <Button variant="contained" startIcon={<Plus size={16} />}
          onClick={() => setEditing({ name: 'New profile', items: ['Bluray-1080p', 'WEBDL-1080p'], cutoff: 'Bluray-1080p', upgrade_allowed: true, preferred_codecs: ['H265', 'H264'], rejected_codecs: [], min_size_mb: 0, max_size_mb: 0, prefer_proper: true })}>
          Add profile</Button>}
      </Stack>
      <Grid container spacing={2}>
        {profiles.data?.map((p) => (
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
              <Typography variant="caption" color="text.secondary">Cutoff: {p.cutoff} · Upgrades {p.upgrade_allowed ? 'on' : 'off'}</Typography>
              <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ mt: 1 }}>
                {p.items.map((q) => <Chip key={q} size="small" label={q} color={q === p.cutoff ? 'primary' : 'default'} variant={q === p.cutoff ? 'filled' : 'outlined'} />)}
              </Stack>
              {p.preferred_codecs.length > 0 && <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>Codecs: {p.preferred_codecs.join(' > ')}</Typography>}
            </Paper>
          </Grid>
        ))}
      </Grid>
      {editing && <ProfileEditor profile={editing} qualities={qualities.data ?? []} onClose={() => setEditing(null)} onSave={(p) => save.mutate(p)} />}
      {dialog}
    </>
  );
}

function ProfileEditor({ profile, qualities, onClose, onSave }: { profile: Partial<QualityProfile>; qualities: QualityDef[]; onClose: () => void; onSave: (p: Partial<QualityProfile>) => void }) {
  const [p, setP] = useState<Partial<QualityProfile>>(profile);
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
          </Grid>
        </Grid>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" onClick={() => onSave(p)}>Save</Button>
      </DialogActions>
    </Dialog>
  );
}
