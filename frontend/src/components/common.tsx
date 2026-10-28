import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  ListItemIcon,
  ListItemText,
  Menu,
  MenuItem,
  Paper,
  Snackbar,
  Stack,
  Typography,
} from '@mui/material';
import type { ChipProps } from '@mui/material';
import { ChevronDown } from 'lucide-react';
import { Fragment, useState, type ReactNode } from 'react';
import type { MovieSummary } from '../api/types';
import { palette } from '../theme';
import { useUi } from '../store/ui';

export function PageHeader({ title, subtitle, icon, actions }: { title: string; subtitle?: string; icon?: ReactNode; actions?: ReactNode }) {
  return (
    <Stack direction={{ xs: 'column', md: 'row' }} justifyContent="space-between" alignItems={{ md: 'center' }} spacing={2} sx={{ mb: 3 }}>
      <Box>
        <Typography variant="h4" sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
          {icon}
          {title}
        </Typography>
        {subtitle && (
          <Typography color="text.secondary" sx={{ mt: 0.5 }}>
            {subtitle}
          </Typography>
        )}
      </Box>
      {actions && (
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
          {actions}
        </Stack>
      )}
    </Stack>
  );
}

export function Section({ title, actions, children, sx }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; sx?: object }) {
  return (
    <Paper sx={{ p: 3, ...sx }}>
      {(title || actions) && (
        <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }}>
          {title && <Typography variant="h6">{title}</Typography>}
          {actions}
        </Stack>
      )}
      {children}
    </Paper>
  );
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <Stack alignItems="center" spacing={2} sx={{ py: 8, color: 'text.secondary' }}>
      <CircularProgress size={28} />
      <Typography variant="body2">{label}</Typography>
    </Stack>
  );
}

export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <Paper sx={{ p: 6, textAlign: 'center', borderStyle: 'dashed' }}>
      <Box sx={{ color: 'text.disabled', mb: 1 }}>{icon}</Box>
      <Typography variant="h6">{title}</Typography>
      {children && (
        <Typography color="text.secondary" variant="body2" sx={{ mt: 1 }}>
          {children}
        </Typography>
      )}
    </Paper>
  );
}

export function ErrorAlert({ error }: { error: unknown }) {
  if (!error) return null;
  return <Alert severity="error" sx={{ mb: 2 }}>{error instanceof Error ? error.message : String(error)}</Alert>;
}

const STATUS_COLORS: Record<string, ChipProps['color']> = {
  imported: 'success', completed: 'success', downloaded: 'success', ok: 'success', success: 'success',
  downloading: 'secondary', importing: 'secondary', running: 'secondary', queued: 'default', paused: 'default',
  warning: 'warning', import_failed: 'error', failed: 'error', error: 'error', missing: 'error', cancelled: 'default',
  grabbed: 'secondary', added: 'primary', requested: 'info', renamed: 'primary', upgraded: 'success', deleted: 'warning', download_failed: 'error',
  duplicate: 'warning', upgrade: 'success', new: 'success',
};

export function StatusChip({ status, label }: { status: string; label?: string }) {
  return <Chip size="small" label={(label ?? status).replace(/_/g, ' ')} color={STATUS_COLORS[status] ?? 'default'} variant="outlined" />;
}

// A movie without a file is only really missing once it's out (release gate); otherwise say why it isn't yet.
const RELEASE_STATES: Record<string, { label: string; color: string }> = {
  released: { label: 'Missing', color: palette.red },
  in_theaters: { label: 'In theaters', color: palette.cyan },
  coming_soon: { label: 'Coming soon', color: palette.purple },
  post_production: { label: 'Post-production', color: palette.amber },
  in_production: { label: 'In production', color: palette.amber },
  planned: { label: 'Planned', color: palette.purple },
  announced: { label: 'Announced', color: palette.purple },
  rumored: { label: 'Rumored', color: palette.dim },
  canceled: { label: 'Canceled', color: palette.dim },
};

export function releaseBadge(state?: string | null) {
  return RELEASE_STATES[state ?? 'released'] ?? RELEASE_STATES.released;
}

export function MovieStatusChip({ movie }: { movie: Pick<MovieSummary, 'has_file' | 'release_state' | 'release_note'> }) {
  if (movie.has_file) return <Chip size="small" color="success" label="On disk" />;
  const badge = releaseBadge(movie.release_state);
  return <Chip size="small" variant="outlined" label={badge.label} title={movie.release_note ?? undefined} sx={{ color: badge.color, borderColor: badge.color }} />;
}

export function QualityChip({ quality }: { quality?: string | null }) {
  if (!quality) return <Typography variant="caption" color="text.disabled">—</Typography>;
  return (
    <Chip size="small" label={quality} sx={{ bgcolor: 'rgba(168,85,247,0.15)', color: palette.purple, border: 'none' }} />
  );
}

export function StatCard({ label, value, hint, color, icon }: { label: string; value: ReactNode; hint?: ReactNode; color?: string; icon?: ReactNode }) {
  return (
    <Paper sx={{ p: 2.5, height: '100%' }}>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
        <Typography variant="body2" color="text.secondary" fontWeight={600}>
          {label}
        </Typography>
        <Box sx={{ color: color ?? palette.purple, opacity: 0.8 }}>{icon}</Box>
      </Stack>
      <Typography sx={{ fontFamily: "'Outfit'", fontSize: '2rem', fontWeight: 800, color: color ?? palette.text, mt: 1 }}>{value}</Typography>
      {hint && (
        <Typography variant="caption" color="text.secondary">
          {hint}
        </Typography>
      )}
    </Paper>
  );
}

export function useConfirm() {
  const [state, setState] = useState<{ title: string; body: ReactNode; confirmLabel: string; danger: boolean; resolve: (v: boolean) => void } | null>(null);
  const confirm = (title: string, body: ReactNode, opts: { confirmLabel?: string; danger?: boolean } = {}) =>
    new Promise<boolean>((resolve) => setState({ title, body, confirmLabel: opts.confirmLabel ?? 'Confirm', danger: opts.danger ?? false, resolve }));
  const close = (v: boolean) => {
    state?.resolve(v);
    setState(null);
  };
  const dialog = (
    <Dialog open={!!state} onClose={() => close(false)} maxWidth="xs" fullWidth>
      <DialogTitle>{state?.title}</DialogTitle>
      <DialogContent>{state?.body}</DialogContent>
      <DialogActions>
        <Button onClick={() => close(false)}>Cancel</Button>
        <Button variant="contained" color={state?.danger ? 'error' : 'primary'} onClick={() => close(true)}>
          {state?.confirmLabel}
        </Button>
      </DialogActions>
    </Dialog>
  );
  return { confirm, dialog };
}

export interface ActionItem {
  label: string;
  icon?: ReactNode;
  onClick: () => void;
  hidden?: boolean;     // permission gates: hidden items are left out
  disabled?: boolean;
  danger?: boolean;
  divider?: boolean;    // draw a separator above this item
}

/** One "Actions" dropdown for a page header, in place of a row of buttons. */
export function ActionsMenu({ items, label = 'Actions' }: { items: ActionItem[]; label?: string }) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const visible = items.filter((i) => !i.hidden);
  if (!visible.length) return null;
  return (
    <>
      <Button variant="contained" endIcon={<ChevronDown size={16} />} onClick={(e) => setAnchor(e.currentTarget)}>{label}</Button>
      <Menu anchorEl={anchor} open={!!anchor} onClose={() => setAnchor(null)}>
        {visible.map((item, i) => (
          <Fragment key={item.label}>
            {item.divider && i > 0 && <Divider />}
            <MenuItem disabled={item.disabled} onClick={() => { setAnchor(null); item.onClick(); }}
              sx={item.danger ? { color: palette.red } : undefined}>
              {item.icon && <ListItemIcon sx={{ color: 'inherit' }}>{item.icon}</ListItemIcon>}
              <ListItemText>{item.label}</ListItemText>
            </MenuItem>
          </Fragment>
        ))}
      </Menu>
    </>
  );
}

export function Toaster() {
  const toasts = useUi((s) => s.toasts);
  const dismiss = useUi((s) => s.dismiss);
  return (
    <>
      {toasts.map((t, i) => (
        <Snackbar key={t.id} open anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }} sx={{ mb: i * 7 }}>
          <Alert severity={t.severity} variant="filled" onClose={() => dismiss(t.id)} sx={{ maxWidth: 480 }}>
            {t.message}
          </Alert>
        </Snackbar>
      ))}
    </>
  );
}

export function Poster({ src, title, height = 180 }: { src?: string | null; title: string; height?: number }) {
  const [failed, setFailed] = useState(false);
  const usable = src && !failed && !src.includes('images.unsplash.com');
  return usable ? (
    <Box component="img" src={src!} alt={title} loading="lazy" onError={() => setFailed(true)}
      sx={{ width: (height * 2) / 3, height, objectFit: 'cover', borderRadius: 2, display: 'block', flexShrink: 0 }} />
  ) : (
    <Box sx={{ width: (height * 2) / 3, height, borderRadius: 2, flexShrink: 0, display: 'grid', placeItems: 'center', p: 1, textAlign: 'center',
      background: `linear-gradient(160deg, #1d2140, #0d0e19)`, color: palette.muted, fontFamily: "'Outfit'", fontWeight: 700, fontSize: height > 150 ? '1rem' : '0.7rem' }}>
      {title}
    </Box>
  );
}
