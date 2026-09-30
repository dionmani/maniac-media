import {
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
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
import { KeyRound, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { del, get, patch, post } from '../../api/client';
import type { Page, User } from '../../api/types';
import { Loading, Section, useConfirm } from '../../components/common';
import { useAuth } from '../../store/auth';
import { toast } from '../../store/ui';
import { errorMessage, relative } from '../../utils/format';

const ROLES = [
  { value: 'admin', label: 'Admin', help: 'Everything, including users, database and security settings' },
  { value: 'power_user', label: 'Power user', help: 'Manage media, search, import and rename – no settings' },
  { value: 'read_only', label: 'Read only', help: 'Browse the library and activity' },
];

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
    <Section title="Your account">
      <Typography sx={{ mb: 2 }}>Signed in as <b>{user?.username}</b> ({user?.role.replace('_', ' ')})</Typography>
      <Stack spacing={2} sx={{ maxWidth: 400 }}>
        <TextField type="password" label="Current password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" />
        <TextField type="password" label="New password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />
        <Button variant="contained" disabled={!current || !next || change.isPending} onClick={() => change.mutate()}>Change password</Button>
      </Stack>
      <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 2 }}>Changing your password signs you out on every device.</Typography>
    </Section>
  );
}
