import {
  Alert,
  Box,
  Button,
  Chip,
  FormControlLabel,
  Grid,
  LinearProgress,
  MenuItem,
  Paper,
  Stack,
  Step,
  StepLabel,
  Stepper,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableRow,
  TextField,
  Typography,
} from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Database, HardDriveDownload, KeyRound, ShieldAlert, ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import { get, post } from '../../api/client';
import { Loading, Section, useConfirm } from '../../components/common';
import { toast } from '../../store/ui';
import { palette } from '../../theme';
import { errorMessage } from '../../utils/format';

interface Migration { running: boolean; step: string; progress: number; message: string; target: string | null; error: string | null; backup: string | null;
  tables: Record<string, { source: number; target: number }>; finished_at: string | null }
interface DbInfo {
  engine: string; connection: string; revision: string; head: string;
  secret_store: { source: string; secure: boolean; detail: string; file: string };
  migration: Migration; drivers: Record<string, boolean>;
}

const ENGINES = [
  { value: 'postgresql', label: 'PostgreSQL', port: 5432 },
  { value: 'mysql', label: 'MySQL', port: 3306 },
  { value: 'mariadb', label: 'MariaDB', port: 3306 },
  { value: 'mssql', label: 'Microsoft SQL Server', port: 1433 },
  { value: 'sqlite', label: 'SQLite (file)', port: 0 },
];
const STEPS = ['validate', 'quiesce', 'backup', 'schema', 'copy', 'verify', 'switch', 'done'];

export default function DatabaseSettings() {
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const info = useQuery({
    queryKey: ['database'],
    queryFn: () => get<DbInfo>('/database'),
    refetchInterval: (q) => (q.state.data?.migration.running ? 1500 : false),
  });
  const [form, setForm] = useState({ db_type: 'postgresql', host: 'localhost', port: 5432, username: '', password: '', database: 'maniac_media', sqlite_path: '', overwrite: false });
  const [test, setTest] = useState<{ success: boolean; message: string } | null>(null);

  const runTest = useMutation({
    mutationFn: () => post<{ success: boolean; message: string }>('/database/test', form),
    onSuccess: setTest,
    onError: (e) => setTest({ success: false, message: errorMessage(e) }),
  });
  const migrate = useMutation({
    mutationFn: () => post('/database/migrate', form),
    onSuccess: () => { toast('Migration started', 'info'); qc.invalidateQueries({ queryKey: ['database'] }); },
    onError: (e) => toast(errorMessage(e), 'error'),
  });
  const backup = useMutation({
    mutationFn: () => post<{ path: string }>('/database/backup'),
    onSuccess: (r) => toast(`Backup written to ${r.path}`, 'success'),
    onError: (e) => toast(errorMessage(e), 'error'),
  });

  if (info.isLoading || !info.data) return <Loading />;
  const d = info.data;
  const m = d.migration;
  const engine = ENGINES.find((e) => e.value === form.db_type)!;
  const activeStep = Math.max(0, STEPS.indexOf(m.step));

  const start = async () => {
    const ok = await confirm('Migrate database?',
      <Typography variant="body2">All data will be copied to <b>{engine.label}</b>. A backup is taken first, background jobs pause, and the app keeps running on the current database if anything fails.</Typography>,
      { confirmLabel: 'Start migration' });
    if (ok) migrate.mutate();
  };

  return (
    <Stack spacing={2}>
      <Grid container spacing={2}>
        <Grid size={{ xs: 12, md: 6 }}>
          <Section title={<Stack direction="row" spacing={1} alignItems="center"><Database size={18} color={palette.purple} /><span>Active database</span></Stack>}>
            <Table size="small">
              <TableBody>
                <TableRow><TableCell>Engine</TableCell><TableCell><Chip size="small" label={d.engine} color="secondary" /></TableCell></TableRow>
                <TableRow><TableCell>Connection</TableCell><TableCell sx={{ fontFamily: "'JetBrains Mono', monospace", fontSize: '0.75rem', wordBreak: 'break-all' }}>{d.connection}</TableCell></TableRow>
                <TableRow><TableCell>Schema</TableCell><TableCell>{d.revision} {d.revision === d.head ? <Chip size="small" color="success" label="up to date" /> : <Chip size="small" color="warning" label={`head ${d.head}`} />}</TableCell></TableRow>
              </TableBody>
            </Table>
            <Button sx={{ mt: 2 }} variant="outlined" startIcon={<HardDriveDownload size={16} />} onClick={() => backup.mutate()} disabled={backup.isPending}>Back up now</Button>
          </Section>
        </Grid>
        <Grid size={{ xs: 12, md: 6 }}>
          <Section title={<Stack direction="row" spacing={1} alignItems="center"><KeyRound size={18} color={palette.purple} /><span>Credential encryption</span></Stack>}>
            <Alert severity={d.secret_store.secure ? 'success' : 'warning'} icon={d.secret_store.secure ? <ShieldCheck size={18} /> : <ShieldAlert size={18} />}>
              {d.secret_store.detail}
            </Alert>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
              Connection strings, API keys and passwords are encrypted with AES-256-GCM and stored in <code>{d.secret_store.file}</code>.
              The master key lives separately ({d.secret_store.source === 'os-keyring' ? 'OS credential store' : d.secret_store.source === 'env' ? 'MANIAC_MASTER_KEY' : 'key file'})
              and secrets are only decrypted in memory when needed.
            </Typography>
          </Section>
        </Grid>
      </Grid>

      <Section title="Migration wizard">
        {(m.running || m.step === 'done' || m.step === 'failed') && (
          <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
            <Stepper activeStep={m.step === 'done' ? STEPS.length : activeStep} alternativeLabel sx={{ mb: 2 }}>
              {STEPS.slice(0, -1).map((s) => <Step key={s}><StepLabel error={m.step === 'failed' && STEPS[activeStep] === s}>{s}</StepLabel></Step>)}
            </Stepper>
            {m.running && <LinearProgress variant="determinate" value={m.progress} sx={{ mb: 1 }} />}
            <Alert severity={m.step === 'failed' ? 'error' : m.step === 'done' ? 'success' : 'info'}>
              {m.message}{m.error ? ` – ${m.error}` : ''}{m.target ? ` (${m.target})` : ''}
            </Alert>
            {m.backup && <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>Backup: {m.backup}</Typography>}
            {Object.keys(m.tables).length > 0 && (
              <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ mt: 1 }}>
                {Object.entries(m.tables).map(([t, c]) => <Chip key={t} size="small" variant="outlined" color={c.source === c.target ? 'success' : 'error'} label={`${t}: ${c.target}/${c.source}`} />)}
              </Stack>
            )}
          </Paper>
        )}
        <Grid container spacing={2}>
          <Grid size={{ xs: 12, md: 4 }}>
            <TextField select fullWidth label="Target engine" value={form.db_type}
              onChange={(e) => { const eng = ENGINES.find((x) => x.value === e.target.value)!; setForm({ ...form, db_type: eng.value, port: eng.port }); setTest(null); }}>
              {ENGINES.map((e) => (
                <MenuItem key={e.value} value={e.value} disabled={!d.drivers[e.value]}>
                  {e.label}{!d.drivers[e.value] && ' – driver not installed'}
                </MenuItem>
              ))}
            </TextField>
          </Grid>
          {form.db_type === 'sqlite' ? (
            <Grid size={{ xs: 12, md: 8 }}>
              <TextField fullWidth label="SQLite file path" value={form.sqlite_path} onChange={(e) => setForm({ ...form, sqlite_path: e.target.value })} />
            </Grid>
          ) : (
            <>
              <Grid size={{ xs: 8, md: 5 }}><TextField fullWidth label="Host" value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} /></Grid>
              <Grid size={{ xs: 4, md: 3 }}><TextField fullWidth type="number" label="Port" value={form.port} onChange={(e) => setForm({ ...form, port: Number(e.target.value) })} /></Grid>
              <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Database" value={form.database} onChange={(e) => setForm({ ...form, database: e.target.value })} /></Grid>
              <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth label="Username" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} autoComplete="off" /></Grid>
              <Grid size={{ xs: 12, md: 4 }}><TextField fullWidth type="password" label="Password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="new-password" /></Grid>
            </>
          )}
        </Grid>
        <FormControlLabel sx={{ mt: 1 }} control={<Switch checked={form.overwrite} onChange={(e) => setForm({ ...form, overwrite: e.target.checked })} />}
          label="Overwrite data already present in the target" />
        {test && <Alert severity={test.success ? 'success' : 'error'} sx={{ mt: 2 }}>{test.message}</Alert>}
        <Box sx={{ mt: 2, display: 'flex', gap: 1 }}>
          <Button variant="outlined" onClick={() => runTest.mutate()} disabled={runTest.isPending}>{runTest.isPending ? 'Testing…' : '1. Test connection'}</Button>
          <Button variant="contained" onClick={start} disabled={!test?.success || m.running || migrate.isPending}>2. Back up & migrate</Button>
        </Box>
      </Section>
      {dialog}
    </Stack>
  );
}
