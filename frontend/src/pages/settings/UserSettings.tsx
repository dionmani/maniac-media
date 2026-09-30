import {
  Alert,
  Button,
  Checkbox,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  FormGroup,
  IconButton,
  InputAdornment,
  MenuItem,
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
import { Check, Copy, KeyRound, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { del, get, patch, post } from '../../api/client';
import type { ApiKey, ApiKeyCreatedResponse, ApiKeyPreset, Page, User } from '../../api/types';
import { Loading, Section, useConfirm } from '../../components/common';
import { useAuth } from '../../store/auth';
import { toast } from '../../store/ui';
import { errorMessage, relative } from '../../utils/format';

const ROLES = [
  { value: 'admin', label: 'Admin', help: 'Everything, including users, database and security settings' },
  { value: 'power_user', label: 'Power user', help: 'Manage media, search, import and rename – no settings' },
  { value: 'read_only', label: 'Read only', help: 'Browse the library and activity' },
];

const ALL_PERMISSIONS = [
  { value: 'media:read', label: 'Media Read' },
  { value: 'media:write', label: 'Media Write' },
  { value: 'media:delete', label: 'Media Delete' },
  { value: 'search:execute', label: 'Search' },
  { value: 'queue:read', label: 'Queue Read' },
  { value: 'queue:write', label: 'Queue Write' },
  { value: 'library:scan', label: 'Library Scan' },
  { value: 'files:rename', label: 'Files Rename' },
  { value: 'files:import', label: 'Files Import' },
  { value: 'settings:read', label: 'Settings Read' },
  { value: 'settings:write', label: 'Settings Write' },
  { value: 'database:manage', label: 'Database Manage' },
  { value: 'users:manage', label: 'Users Manage' },
  { value: 'system:read', label: 'System Read' },
  { value: 'system:admin', label: 'System Admin' },
  { value: 'audit:read', label: 'Audit Read' },
];

export function ApiKeySettings() {
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const apiKeys = useQuery({ queryKey: ['api-keys'], queryFn: () => get<ApiKey[]>('/api-keys') });
  const presets = useQuery({ queryKey: ['api-key-presets'], queryFn: () => get<ApiKeyPreset[]>('/api-keys/presets') });

  const [creating, setCreating] = useState(false);
  const [selectedPreset, setSelectedPreset] = useState<string>('custom');
  const [form, setForm] = useState({
    name: '',
    permissions: [] as string[],
    expires_in_days: '' as string | number,
    allow_query_param: false,
  });

  const [newKeyRevealed, setNewKeyRevealed] = useState<ApiKeyCreatedResponse | null>(null);
  const [copied, setCopied] = useState(false);

  const call = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['api-keys'] }),
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  const handlePresetChange = (presetName: string) => {
    setSelectedPreset(presetName);
    if (presetName === 'custom') return;
    const p = presets.data?.find((x) => x.name === presetName);
    if (p) {
      setForm((prev) => ({
        ...prev,
        permissions: [...p.permissions],
        allow_query_param: p.allow_query_param,
      }));
    }
  };

  const togglePermission = (perm: string) => {
    setSelectedPreset('custom');
    setForm((prev) => ({
      ...prev,
      permissions: prev.permissions.includes(perm)
        ? prev.permissions.filter((x) => x !== perm)
        : [...prev.permissions, perm],
    }));
  };

  const handleCreate = async () => {
    try {
      const payload = {
        name: form.name,
        permissions: form.permissions,
        expires_in_days: form.expires_in_days ? Number(form.expires_in_days) : null,
        allow_query_param: form.allow_query_param,
      };
      const result = await post<ApiKeyCreatedResponse>('/api-keys', payload);
      setCreating(false);
      setForm({ name: '', permissions: [], expires_in_days: '', allow_query_param: false });
      setSelectedPreset('custom');
      qc.invalidateQueries({ queryKey: ['api-keys'] });
      setNewKeyRevealed(result);
    } catch (e) {
      toast(errorMessage(e), 'error');
    }
  };

  if (apiKeys.isLoading) return <Loading />;

  return (
    <Section
      title="API keys"
      actions={
        <Button variant="contained" startIcon={<Plus size={16} />} onClick={() => setCreating(true)}>
          Create API key
        </Button>
      }
    >
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Name</TableCell>
            <TableCell>Prefix</TableCell>
            <TableCell>Permissions</TableCell>
            <TableCell>Query Param</TableCell>
            <TableCell>Created</TableCell>
            <TableCell>Last Used</TableCell>
            <TableCell>Expires</TableCell>
            <TableCell>Status</TableCell>
            <TableCell align="right" />
          </TableRow>
        </TableHead>
        <TableBody>
          {apiKeys.data?.length === 0 ? (
            <TableRow>
              <TableCell colSpan={9} align="center">
                <Typography color="text.secondary" variant="body2">
                  No API keys generated yet.
                </Typography>
              </TableCell>
            </TableRow>
          ) : (
            apiKeys.data?.map((k) => {
              const isRevoked = !!k.revoked_at;
              const isExpired = !!k.expires_at && new Date(k.expires_at) < new Date();
              return (
                <TableRow key={k.id} sx={{ opacity: isRevoked ? 0.6 : 1 }}>
                  <TableCell sx={{ fontWeight: 600 }}>{k.name}</TableCell>
                  <TableCell>
                    <code>{k.prefix}…</code>
                  </TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap">
                      {k.permissions.map((p) => (
                        <Chip key={p} label={p} size="small" variant="outlined" />
                      ))}
                    </Stack>
                  </TableCell>
                  <TableCell>{k.allow_query_param ? 'Yes' : 'No'}</TableCell>
                  <TableCell>{relative(k.created_at)}</TableCell>
                  <TableCell>{k.last_used_at ? `${relative(k.last_used_at)}${k.last_used_ip ? ` (${k.last_used_ip})` : ''}` : 'Never'}</TableCell>
                  <TableCell>{k.expires_at ? relative(k.expires_at) : 'Never'}</TableCell>
                  <TableCell>
                    {isRevoked ? (
                      <Chip label="Revoked" size="small" color="error" />
                    ) : isExpired ? (
                      <Chip label="Expired" size="small" color="warning" />
                    ) : (
                      <Chip label="Active" size="small" color="success" />
                    )}
                  </TableCell>
                  <TableCell align="right">
                    {!isRevoked ? (
                      <Button
                        size="small"
                        color="warning"
                        onClick={async () => {
                          if (await confirm('Revoke API key?', k.name, { danger: true, confirmLabel: 'Revoke' })) {
                            call.mutate(() => post(`/api-keys/${k.id}/revoke`));
                          }
                        }}
                      >
                        Revoke
                      </Button>
                    ) : (
                      <Tooltip title="Delete key">
                        <IconButton
                          size="small"
                          color="error"
                          onClick={async () => {
                            if (await confirm('Delete API key permanently?', k.name, { danger: true, confirmLabel: 'Delete' })) {
                              call.mutate(() => del(`/api-keys/${k.id}`));
                            }
                          }}
                        >
                          <Trash2 size={15} />
                        </IconButton>
                      </Tooltip>
                    )}
                  </TableCell>
                </TableRow>
              );
            })
          )}
        </TableBody>
      </Table>

      <Dialog open={creating} onClose={() => setCreating(false)} maxWidth="sm" fullWidth>
        <DialogTitle>Create API Key</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="Name"
              placeholder="e.g. Overseerr, Home Assistant"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              fullWidth
            />

            <TextField
              select
              label="Preset"
              value={selectedPreset}
              onChange={(e) => handlePresetChange(e.target.value)}
              fullWidth
            >
              <MenuItem value="custom">Custom</MenuItem>
              {presets.data?.map((p) => (
                <MenuItem key={p.name} value={p.name}>
                  {p.name} — {p.description}
                </MenuItem>
              ))}
            </TextField>

            <TextField
              label="Expires in (days)"
              type="number"
              placeholder="Leave blank for no expiration"
              value={form.expires_in_days}
              onChange={(e) => setForm({ ...form, expires_in_days: e.target.value })}
              fullWidth
            />

            <FormControlLabel
              control={
                <Checkbox
                  checked={form.allow_query_param}
                  onChange={(e) => setForm({ ...form, allow_query_param: e.target.checked })}
                />
              }
              label="Allow key in URL query parameter (?apikey=...)"
            />

            <Typography variant="subtitle2" sx={{ mt: 1 }}>
              Permissions
            </Typography>
            <FormGroup row sx={{ gap: 1 }}>
              {ALL_PERMISSIONS.map((p) => (
                <FormControlLabel
                  key={p.value}
                  control={
                    <Checkbox
                      size="small"
                      checked={form.permissions.includes(p.value)}
                      onChange={() => togglePermission(p.value)}
                    />
                  }
                  label={p.label}
                />
              ))}
            </FormGroup>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreating(false)}>Cancel</Button>
          <Button variant="contained" disabled={!form.name || form.permissions.length === 0} onClick={handleCreate}>
            Generate Key
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={!!newKeyRevealed} onClose={() => setNewKeyRevealed(null)} maxWidth="xs" fullWidth>
        <DialogTitle>API Key Created</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <Alert severity="warning">
              Save this key now! It will <strong>never</strong> be shown again.
            </Alert>
            <TextField
              fullWidth
              value={newKeyRevealed?.api_key || ''}
              InputProps={{
                readOnly: true,
                endAdornment: (
                  <InputAdornment position="end">
                    <IconButton
                      onClick={() => {
                        if (newKeyRevealed?.api_key) {
                          navigator.clipboard.writeText(newKeyRevealed.api_key);
                          setCopied(true);
                          setTimeout(() => setCopied(false), 2000);
                        }
                      }}
                    >
                      {copied ? <Check size={18} /> : <Copy size={18} />}
                    </IconButton>
                  </InputAdornment>
                ),
              }}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button variant="contained" onClick={() => setNewKeyRevealed(null)}>
            Done
          </Button>
        </DialogActions>
      </Dialog>

      {dialog}
    </Section>
  );
}

export function UserSettings() {
  const qc = useQueryClient();
  const me = useAuth((s) => s.user);
  const { confirm, dialog } = useConfirm();
  const users = useQuery({ queryKey: ['users'], queryFn: () => get<Page<User>>('/users', { page_size: 200 }) });
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ username: '', password: '', role: 'read_only' });
  const [reset, setReset] = useState<User | null>(null);
  const [newPassword, setNewPassword] = useState('');

  const call = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['users'] }),
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  if (users.isLoading) return <Loading />;
  return (
    <Stack spacing={4}>
      <Section title="Users & roles" actions={<Button variant="contained" startIcon={<Plus size={16} />} onClick={() => setCreating(true)}>Add user</Button>}>
        <Table size="small">
          <TableHead><TableRow><TableCell>User</TableCell><TableCell>Role</TableCell><TableCell>Active</TableCell><TableCell>Last login</TableCell><TableCell /></TableRow></TableHead>
          <TableBody>
            {users.data?.items.map((u) => (
              <TableRow key={u.id}>
                <TableCell sx={{ fontWeight: 600 }}>{u.username} {u.id === me?.id && <Chip size="small" label="you" />}</TableCell>
                <TableCell>
                  <TextField select size="small" value={u.role} disabled={u.id === me?.id} onChange={(e) => { const role = e.target.value; call.mutate(() => patch(`/users/${u.id}`, { role })); }}>
                    {ROLES.map((r) => <MenuItem key={r.value} value={r.value}>{r.label}</MenuItem>)}
                  </TextField>
                </TableCell>
                <TableCell><Switch checked={u.is_active} disabled={u.id === me?.id} onChange={(_, is_active) => call.mutate(() => patch(`/users/${u.id}`, { is_active }))} /></TableCell>
                <TableCell>{relative(u.last_login)}</TableCell>
                <TableCell align="right">
                  <Tooltip title="Reset password"><IconButton size="small" onClick={() => setReset(u)}><KeyRound size={15} /></IconButton></Tooltip>
                  {u.id !== me?.id && (
                    <Tooltip title="Delete"><IconButton size="small" color="error" onClick={async () => {
                      if (await confirm('Delete user?', u.username, { danger: true, confirmLabel: 'Delete' })) call.mutate(() => del(`/users/${u.id}`));
                    }}><Trash2 size={15} /></IconButton></Tooltip>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <Stack sx={{ mt: 2 }} spacing={0.5}>
          {ROLES.map((r) => <Typography key={r.value} variant="caption" color="text.secondary"><b>{r.label}:</b> {r.help}</Typography>)}
        </Stack>

        <Dialog open={creating} onClose={() => setCreating(false)} maxWidth="xs" fullWidth>
          <DialogTitle>Add user</DialogTitle>
          <DialogContent>
            <Stack spacing={2} sx={{ mt: 1 }}>
              <TextField label="Username" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
              <TextField label="Password" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="new-password"
                helperText="≥ 10 characters, 3 of: lower, upper, digits, symbols" />
              <TextField select label="Role" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
                {ROLES.map((r) => <MenuItem key={r.value} value={r.value}>{r.label}</MenuItem>)}
              </TextField>
            </Stack>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setCreating(false)}>Cancel</Button>
            <Button variant="contained" onClick={() => call.mutate(() => post('/users', form).then(() => { setCreating(false); setForm({ username: '', password: '', role: 'read_only' }); }))}>Create</Button>
          </DialogActions>
        </Dialog>
        <Dialog open={!!reset} onClose={() => setReset(null)} maxWidth="xs" fullWidth>
          <DialogTitle>Reset password for {reset?.username}</DialogTitle>
          <DialogContent>
            <TextField fullWidth sx={{ mt: 1 }} type="password" label="New password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} autoComplete="new-password"
              helperText="All of this user's sessions will be signed out" />
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setReset(null)}>Cancel</Button>
            <Button variant="contained" onClick={() => call.mutate(() => patch(`/users/${reset!.id}`, { password: newPassword }).then(() => { setReset(null); setNewPassword(''); toast('Password reset', 'success'); }))}>Reset</Button>
          </DialogActions>
        </Dialog>
        {dialog}
      </Section>
      <ApiKeySettings />
    </Stack>
  );
}

export function AccountSettings() {
  const { user, logout } = useAuth();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const change = useMutation({
    mutationFn: () => post<{ message: string }>('/auth/password', { current_password: current, new_password: next }),
    onSuccess: (r) => logout(r.message),
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  return (
    <Stack spacing={4}>
      <Section title="Your account">
        <Typography sx={{ mb: 2 }}>Signed in as <b>{user?.username}</b> ({user?.role.replace('_', ' ')})</Typography>
        <Stack spacing={2} sx={{ maxWidth: 400 }}>
          <TextField type="password" label="Current password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" />
          <TextField type="password" label="New password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />
          <Button variant="contained" disabled={!current || !next || change.isPending} onClick={() => change.mutate()}>Change password</Button>
        </Stack>
        <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 2 }}>Changing your password signs you out on every device.</Typography>
      </Section>
      <ApiKeySettings />
    </Stack>
  );
}

