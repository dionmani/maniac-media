import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import Layout from './components/Layout';
import { Loading, Toaster } from './components/common';
import { useLiveEvents } from './hooks/useLiveEvents';
import LoginPage from './pages/LoginPage';
import { useAuth } from './store/auth';

const Dashboard = lazy(() => import('./pages/DashboardPage'));
const Library = lazy(() => import('./pages/LibraryPage'));
const MovieDetail = lazy(() => import('./pages/MovieDetailPage'));
const SeriesDetail = lazy(() => import('./pages/SeriesDetailPage'));
const AddMedia = lazy(() => import('./pages/AddMediaPage'));
const Wanted = lazy(() => import('./pages/WantedPage'));
const Issues = lazy(() => import('./pages/IssuesPage'));
const Calendar = lazy(() => import('./pages/CalendarPage'));
const Activity = lazy(() => import('./pages/ActivityPage'));
const Import = lazy(() => import('./pages/ImportPage'));
const Renamer = lazy(() => import('./pages/RenamerPage'));
const Settings = lazy(() => import('./pages/SettingsPage'));
const System = lazy(() => import('./pages/SystemPage'));
const Help = lazy(() => import('./pages/HelpPage'));

function Authenticated() {
  useLiveEvents();
  return (
    <Layout>
      <Suspense fallback={<Loading />}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/library" element={<Library />} />
          <Route path="/movies/:id" element={<MovieDetail />} />
          <Route path="/series/:id" element={<SeriesDetail />} />
          <Route path="/add" element={<AddMedia />} />
          <Route path="/wanted" element={<Wanted />} />
          <Route path="/issues" element={<Issues />} />
          <Route path="/calendar" element={<Calendar />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/import" element={<Import />} />
          <Route path="/renamer" element={<Renamer />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/system" element={<System />} />
          <Route path="/help" element={<Help />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </Layout>
  );
}

export default function App() {
  const token = useAuth((s) => s.token);
  return (
    <>
      {token ? <Authenticated /> : <LoginPage />}
      <Toaster />
    </>
  );
}
