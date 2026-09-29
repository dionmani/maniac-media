import { alpha, createTheme } from '@mui/material/styles';

export const palette = {
  bg: '#07080e',
  card: '#111322',
  cardHover: '#181b30',
  border: 'rgba(255,255,255,0.08)',
  purple: '#a855f7',
  cyan: '#06b6d4',
  green: '#10b981',
  amber: '#f59e0b',
  red: '#ef4444',
  text: '#f3f4f6',
  muted: '#9ca3af',
  dim: '#6b7280',
};

export const theme = createTheme({
  palette: {
    mode: 'dark',
    primary: { main: palette.purple },
    secondary: { main: palette.cyan },
    success: { main: palette.green },
    warning: { main: palette.amber },
    error: { main: palette.red },
    background: { default: palette.bg, paper: palette.card },
    text: { primary: palette.text, secondary: palette.muted },
    divider: palette.border,
  },
  shape: { borderRadius: 12 },
  typography: {
    fontFamily: "'Inter', system-ui, sans-serif",
    h1: { fontFamily: "'Outfit', sans-serif", fontWeight: 800 },
    h2: { fontFamily: "'Outfit', sans-serif", fontWeight: 800 },
    h3: { fontFamily: "'Outfit', sans-serif", fontWeight: 800 },
    h4: { fontFamily: "'Outfit', sans-serif", fontWeight: 800, fontSize: '1.75rem' },
    h5: { fontFamily: "'Outfit', sans-serif", fontWeight: 700 },
    h6: { fontFamily: "'Outfit', sans-serif", fontWeight: 700 },
    button: { textTransform: 'none', fontWeight: 600 },
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        body: { backgroundColor: palette.bg },
        '*::-webkit-scrollbar': { width: 8, height: 8 },
        '*::-webkit-scrollbar-thumb': { background: '#1f233a', borderRadius: 4 },
        '*::-webkit-scrollbar-thumb:hover': { background: palette.purple },
      },
    },
    MuiPaper: {
      styleOverrides: { root: { backgroundImage: 'none', border: `1px solid ${palette.border}` } },
    },
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        containedPrimary: { background: `linear-gradient(135deg, ${palette.purple}, #7c3aed)` },
        containedSecondary: { background: `linear-gradient(135deg, ${palette.cyan}, #0891b2)`, color: '#fff' },
      },
    },
    MuiTableCell: {
      styleOverrides: {
        root: { borderColor: palette.border },
        head: { color: palette.muted, fontSize: '0.75rem', textTransform: 'uppercase', letterSpacing: 0.5, fontWeight: 700 },
      },
    },
    MuiChip: { styleOverrides: { root: { fontWeight: 600 } } },
    MuiDialog: { styleOverrides: { paper: { backgroundColor: palette.card } } },
    MuiTooltip: { defaultProps: { arrow: true } },
    MuiLinearProgress: {
      styleOverrides: {
        root: { borderRadius: 4, height: 6, backgroundColor: alpha('#fff', 0.08) },
        bar: { background: `linear-gradient(90deg, ${palette.purple}, ${palette.cyan})` },
      },
    },
  },
});
