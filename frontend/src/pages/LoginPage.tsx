import { Alert, Box, Button, Paper, Stack, TextField, Typography } from '@mui/material';
import { useQuery } from '@tanstack/react-query';
import { ShieldCheck } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { get, post } from '../api/client';
import type { User } from '../api/types';
import { useAuth } from '../store/auth';
import { palette } from '../theme';

export default function LoginPage() {
  const { login, notice } = useAuth();
  const status = useQuery({ queryKey: ['auth-status'], queryFn: () => get<{ setup_required: boolean }>('/auth/status') });
  const setup = status.data?.setup_required;
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (setup && password !== confirm) {
      setError('Passwords do not match');
      return;
    }
    setBusy(true);
    try {
      const res = await post<{ access_token: string; user: User }>(setup ? '/auth/setup' : '/auth/login', { username, password });
      login(res.access_token, res.user);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box sx={{ minHeight: '100vh', display: 'grid', placeItems: 'center', p: 2,
      background: `radial-gradient(circle at 20% 20%, ${palette.purple}22, transparent 40%), radial-gradient(circle at 80% 80%, ${palette.cyan}1a, transparent 40%)` }}>
      <Paper component="form" onSubmit={submit} sx={{ p: 4, width: '100%', maxWidth: 420 }}>
        <Stack alignItems="center" spacing={1} sx={{ mb: 3 }}>
          <Box component="img" src="/logo.png" alt="Maniac Media" sx={{ width: 96, height: 96,
            filter: `drop-shadow(0 0 12px ${palette.purple}66)` }} />
          <Typography variant="h5">Maniac Media</Typography>
          <Typography color="text.secondary" variant="body2" textAlign="center">
            {setup ? 'Welcome! Create the administrator account to finish setup.' : 'Sign in to continue'}
          </Typography>
        </Stack>
        {notice && !error && <Alert severity="info" sx={{ mb: 2 }}>{notice}</Alert>}
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        <Stack spacing={2}>
          <TextField label="Username" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoComplete="username" required />
          <TextField label="Password" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete={setup ? 'new-password' : 'current-password'} required
            helperText={setup ? 'At least 10 characters, mixing 3 of: lower, upper, digits, symbols' : undefined} />
          {setup && (
            <TextField label="Confirm password" type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" required />
          )}
          <Button type="submit" variant="contained" size="large" disabled={busy || status.isLoading} startIcon={setup ? <ShieldCheck size={18} /> : undefined}>
            {busy ? 'Please wait…' : setup ? 'Create administrator' : 'Sign in'}
          </Button>
        </Stack>
      </Paper>
    </Box>
  );
}
