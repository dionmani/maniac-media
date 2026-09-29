import { Box, Tab, Tabs } from '@mui/material';
import { Settings } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { PageHeader } from '../components/common';
import { useAuth } from '../store/auth';
import { palette } from '../theme';
import DatabaseSettings from './settings/DatabaseSettings';
import { MediaManagement, NamingSettings, SecuritySettings } from './settings/GeneralSettings';
import { ClientSettings, IndexerSettings, MetadataSettings, NotificationSettings } from './settings/IntegrationSettings';
import QualitySettings from './settings/QualitySettings';
import { AccountSettings, UserSettings } from './settings/UserSettings';

const TABS = [
  { key: 'media', label: 'Media management', perm: 'settings:read', el: <MediaManagement /> },
  { key: 'naming', label: 'Naming', perm: 'settings:read', el: <NamingSettings /> },
  { key: 'quality', label: 'Quality profiles', perm: 'settings:read', el: <QualitySettings /> },
  { key: 'indexers', label: 'Indexers', perm: 'settings:read', el: <IndexerSettings /> },
  { key: 'clients', label: 'Download clients', perm: 'settings:read', el: <ClientSettings /> },
  { key: 'metadata', label: 'Metadata', perm: 'settings:read', el: <MetadataSettings /> },
  { key: 'notifications', label: 'Notifications', perm: 'settings:read', el: <NotificationSettings /> },
  { key: 'database', label: 'Database', perm: 'database:manage', el: <DatabaseSettings /> },
  { key: 'security', label: 'Security', perm: 'settings:write', el: <SecuritySettings /> },
  { key: 'users', label: 'Users', perm: 'users:manage', el: <UserSettings /> },
  { key: 'account', label: 'Account', perm: 'media:read', el: <AccountSettings /> },
];

export default function SettingsPage() {
  const can = useAuth((s) => s.can);
  const [params, setParams] = useSearchParams();
  const visible = TABS.filter((t) => can(t.perm));
  const tab = visible.find((t) => t.key === params.get('tab')) ?? visible[0];
  return (
    <>
      <PageHeader title="Settings" icon={<Settings color={palette.cyan} />} subtitle="Library layout, naming, quality, integrations, database and security" />
      <Tabs value={tab.key} onChange={(_, v) => setParams({ tab: v })} variant="scrollable" scrollButtons="auto" sx={{ mb: 3 }}>
        {visible.map((t) => <Tab key={t.key} value={t.key} label={t.label} />)}
      </Tabs>
      <Box key={tab.key}>{tab.el}</Box>
    </>
  );
}
