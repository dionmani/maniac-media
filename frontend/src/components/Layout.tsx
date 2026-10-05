import {
  Avatar,
  Box,
  Chip,
  Divider,
  Drawer,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Menu,
  MenuItem,
  Stack,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
  useMediaQuery,
} from '@mui/material';
import {
  Activity,
  AlertTriangle,
  CalendarClock,
  Compass,
  FileText,
  Film,
  FolderInput,
  HelpCircle,
  LayoutDashboard,
  LogOut,
  Menu as MenuIcon,
  PlusCircle,
  Search,
  Settings,
  ShieldCheck,
  Tv,
} from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { post } from '../api/client';
import { useAuth } from '../store/auth';
import { useUi } from '../store/ui';
import { palette, theme } from '../theme';

const WIDTH = 248;

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, perm: 'media:read' },
  { to: '/add', label: 'Add New', icon: PlusCircle, perm: 'media:write' },
  { to: '/discover', label: 'Discover', icon: Compass, perm: 'media:read' },
  { to: '/wanted', label: 'Wanted', icon: Search, perm: 'media:read' },
  { to: '/issues', label: 'Issues', icon: AlertTriangle, perm: 'media:read' },
  { to: '/calendar', label: 'Calendar', icon: CalendarClock, perm: 'media:read' },
  { to: '/activity', label: 'Activity', icon: Activity, perm: 'queue:read' },
  { to: '/import', label: 'Import', icon: FolderInput, perm: 'files:import' },
  { to: '/renamer', label: 'Renamer', icon: FileText, perm: 'files:rename' },
  { to: '/settings', label: 'Settings', icon: Settings, perm: 'settings:read' },
  { to: '/system', label: 'System', icon: ShieldCheck, perm: 'system:read' },
  { to: '/help', label: 'Help & Q&A', icon: HelpCircle, perm: 'media:read' },
];

export default function Layout({ children }: { children: ReactNode }) {
  const mobile = useMediaQuery(theme.breakpoints.down('md'));
  const [open, setOpen] = useState(false);
  const { mode, setMode, connected } = useUi();
  const { user, can, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [menu, setMenu] = useState<HTMLElement | null>(null);
  const [version, setVersion] = useState<{ version: string; build: string } | null>(null);

  useEffect(() => {
    fetch('/api/health').then((r) => r.json()).then(setVersion).catch(() => undefined);
  }, []);

  const drawer = (
    <Stack sx={{ height: '100%', p: 2 }}>
      <Stack direction="row" spacing={1.5} alignItems="center" sx={{ px: 1, mb: 3 }}>
        <Box component="img" src="/logo.png" alt="Maniac Media" sx={{ width: 44, height: 44, flexShrink: 0,
          filter: `drop-shadow(0 0 8px ${palette.purple}66)` }} />
        <Box>
          <Typography sx={{ fontFamily: "'Outfit'", fontWeight: 900, fontSize: '1.2rem', letterSpacing: -0.5,
            background: `linear-gradient(to right, #fff, ${palette.purple})`, WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
            MANIAC MEDIA
          </Typography>
          <Typography variant="caption" sx={{ color: palette.cyan, fontWeight: 700, letterSpacing: 1 }}>
            {version ? `v${version.version}` : 'v…'} · AUTOMATION
          </Typography>
          {version?.build && (
            <Typography variant="caption" sx={{ display: 'block', color: 'text.secondary', fontSize: '0.65rem', lineHeight: 1.2 }}>
              build {version.build}
            </Typography>
          )}
        </Box>
      </Stack>

      {/* The mode toggle doubles as the Library link: clicking either side switches mode and opens that library. */}
      <ToggleButtonGroup exclusive fullWidth size="small" value={mode} sx={{ mb: 2 }}>
        {(['movie', 'tv'] as const).map((m) => (
          <ToggleButton key={m} value={m} sx={{ gap: 1 }}
            onClick={() => { setMode(m); setOpen(false); if (can('media:read')) navigate('/library'); }}>
            {m === 'movie' ? <><Film size={16} /> Movies</> : <><Tv size={16} /> TV Shows</>}
          </ToggleButton>
        ))}
      </ToggleButtonGroup>

      <List sx={{ flex: 1 }} dense>
        {NAV.filter((n) => can(n.perm)).map((n) => {
          const Icon = n.icon;
          const active = n.to === '/' ? location.pathname === '/' : location.pathname.startsWith(n.to);
          return (
            <ListItemButton key={n.to} component={NavLink} to={n.to} onClick={() => setOpen(false)} selected={active}
              sx={{ borderRadius: 2, mb: 0.5, border: '1px solid transparent',
                '&.Mui-selected': { background: `linear-gradient(90deg, ${palette.purple}26, ${palette.cyan}0d)`, borderColor: `${palette.purple}4d` } }}>
              <ListItemIcon sx={{ minWidth: 36, color: active ? palette.purple : 'text.secondary' }}>
                <Icon size={19} />
              </ListItemIcon>
              <ListItemText primary={n.label} primaryTypographyProps={{ fontWeight: 500 }} />
            </ListItemButton>
          );
        })}
      </List>

      <Divider sx={{ mb: 1.5 }} />
      <Stack direction="row" alignItems="center" spacing={1} sx={{ px: 1 }}>
        <Avatar sx={{ width: 32, height: 32, bgcolor: palette.purple, fontSize: 14 }}>{user?.username[0]?.toUpperCase()}</Avatar>
        <Box sx={{ flex: 1, minWidth: 0 }} onClick={(e) => setMenu(e.currentTarget)} role="button" style={{ cursor: 'pointer' }}>
          <Typography variant="body2" fontWeight={600} noWrap>{user?.username}</Typography>
          <Typography variant="caption" color="text.secondary">{user?.role.replace('_', ' ')}</Typography>
        </Box>
        <Tooltip title={connected ? 'Live updates connected' : 'Live updates disconnected – retrying'}>
          <Box sx={{ width: 9, height: 9, borderRadius: '50%', bgcolor: connected ? palette.green : palette.red, boxShadow: connected ? `0 0 8px ${palette.green}` : 'none' }} />
        </Tooltip>
        <Tooltip title="Sign out">
          <IconButton size="small" onClick={() => logout()}>
            <LogOut size={16} />
          </IconButton>
        </Tooltip>
      </Stack>
      <Menu anchorEl={menu} open={!!menu} onClose={() => setMenu(null)}>
        <MenuItem onClick={() => { setMenu(null); navigate('/settings?tab=account'); }}>Account & password</MenuItem>
        <MenuItem onClick={async () => { setMenu(null); await post('/auth/logout').catch(() => undefined); logout(); }}>
          Sign out everywhere
        </MenuItem>
      </Menu>
    </Stack>
  );

  return (
    <Box sx={{ display: 'flex', minHeight: '100vh' }}>
      {mobile ? (
        <>
          <Drawer open={open} onClose={() => setOpen(false)} PaperProps={{ sx: { width: WIDTH, border: 'none' } }}>{drawer}</Drawer>
          <IconButton onClick={() => setOpen(true)} sx={{ position: 'fixed', top: 12, left: 12, zIndex: 20, bgcolor: palette.card }}>
            <MenuIcon size={20} />
          </IconButton>
        </>
      ) : (
        <Drawer variant="permanent" PaperProps={{ sx: { width: WIDTH, borderTop: 0, borderBottom: 0, borderLeft: 0 } }} sx={{ width: WIDTH, flexShrink: 0 }}>
          {drawer}
        </Drawer>
      )}
      <Box component="main" sx={{ flex: 1, minWidth: 0, p: { xs: 2, md: 4 }, pt: { xs: 8, md: 4 }, maxWidth: 1680 }}>
        {!connected && (
          <Chip size="small" color="warning" variant="outlined" label="Reconnecting live updates…" sx={{ position: 'fixed', top: 12, right: 16, zIndex: 10 }} />
        )}
        {children}
      </Box>
    </Box>
  );
}
