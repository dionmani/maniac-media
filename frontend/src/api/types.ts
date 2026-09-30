export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface User {
  id: number;
  username: string;
  role: 'admin' | 'power_user' | 'read_only';
  is_active: boolean;
  last_login?: string | null;
  created_at?: string | null;
  permissions: string[];
}

export interface Cast {
  name: string;
  character?: string | null;
  image?: string | null;
}

export interface MovieSummary {
  id: number;
  title: string;
  year: number;
  monitored: boolean;
  has_file: boolean;
  quality?: string | null;
  poster_path?: string | null;
  studio?: string | null;
  status?: string | null;
  rating?: number | null;
  runtime_minutes?: number | null;
  file_size_bytes?: number | null;
  genres?: string | null;
  certification?: string | null;
  added_date?: string | null;
}

export interface Movie extends MovieSummary {
  quality_profile_id?: number | null;
  root_folder?: string | null;
  path?: string | null;
  file_path?: string | null;
  tmdb_id?: number | null;
  imdb_id?: string | null;
  overview?: string | null;
  tagline?: string | null;
  backdrop_path?: string | null;
  release_date?: string | null;
  digital_release_date?: string | null;
  physical_release_date?: string | null;
  votes?: number | null;
  original_title?: string | null;
  cast_members?: Cast[] | null;
  crew?: { name: string; job: string }[] | null;
  metadata_source?: string | null;
  last_metadata_refresh?: string | null;
  last_search_at?: string | null;
}

export interface Episode {
  id: number;
  series_id: number;
  season_number: number;
  episode_number: number;
  title: string;
  overview?: string | null;
  air_date?: string | null;
  monitored: boolean;
  has_file: boolean;
  file_path?: string | null;
  file_size_bytes?: number | null;
  quality?: string | null;
  status?: string | null;
  runtime_minutes?: number | null;
  series_title?: string;
}

export interface Season {
  id: number;
  season_number: number;
  monitored: boolean;
  episodes: Episode[];
}

export interface SeriesSummary {
  id: number;
  title: string;
  year: number;
  status?: string | null;
  monitored: boolean;
  network?: string | null;
  poster_path?: string | null;
  total_seasons?: number | null;
  rating?: number | null;
  genres?: string | null;
  episode_count: number;
  episode_file_count: number;
  missing_count: number;
  size_on_disk: number;
  added_date?: string | null;
}

export interface PruneStaleResult {
  provider: string;
  episodes: number;
  kept_with_files: number;
  seasons: { season_number: number; episodes: number; removed: boolean }[];
  applied: boolean;
}

export interface Series extends SeriesSummary {
  quality_profile_id?: number | null;
  root_folder?: string | null;
  path?: string | null;
  tvdb_id?: number | null;
  tmdb_id?: number | null;
  tvmaze_id?: number | null;
  imdb_id?: string | null;
  overview?: string | null;
  backdrop_path?: string | null;
  first_aired?: string | null;
  runtime_minutes?: number | null;
  certification?: string | null;
  cast_members?: Cast[] | null;
  series_type?: string | null;
  monitor_new_seasons?: boolean | null;
  metadata_source?: string | null;
  seasons: Season[];
}

export interface LookupResult {
  media_type: 'movie' | 'tv';
  provider: string;
  title: string;
  year?: number | null;
  overview?: string | null;
  poster_url?: string | null;
  genres: string[];
  studio?: string | null;
  rating?: number | null;
  tmdb_id?: number | null;
  imdb_id?: string | null;
  tvdb_id?: number | null;
  tvmaze_id?: number | null;
  season_count?: number | null;
  cast: Cast[];
}

export interface Release {
  guid: string;
  title: string;
  indexer: string;
  protocol: 'torrent' | 'usenet';
  size_bytes: number;
  seeders?: number | null;
  leechers?: number | null;
  age_days?: number | null;
  info_url?: string | null;
  quality: string;
  codec: string;
  proper: boolean;
  hdr: boolean;
  approved: boolean;
  rejections: string[];
  score: number;
  season?: number | null;
  episodes: number[];
  download_url_token: string;
}

export interface QueueItem {
  id: number;
  title: string;
  media_type: 'movie' | 'tv';
  media_id: number;
  season_num?: number | null;
  ep_num?: number | null;
  episode_numbers?: number[] | null;
  indexer_name?: string | null;
  release_title: string;
  quality?: string | null;
  size_bytes?: number | null;
  download_speed_bps?: number | null;
  eta_seconds?: number | null;
  client_position?: number | null;
  progress?: number | null;
  status: string;
  protocol?: string | null;
  download_path?: string | null;
  error_message?: string | null;
  retry_count?: number | null;
  added_at?: string | null;
}

export interface HistoryItem {
  id: number;
  media_type: string;
  media_id?: number | null;
  title: string;
  event_type: string;
  quality?: string | null;
  details?: string | null;
  timestamp?: string | null;
}

export interface QualityProfile {
  id: number;
  name: string;
  items: string[];
  cutoff?: string | null;
  upgrade_allowed: boolean;
  preferred_codecs: string[];
  rejected_codecs: string[];
  min_size_mb: number;
  max_size_mb: number;
  prefer_proper: boolean;
}

export interface FieldSpec {
  name: string;
  label: string;
  type: 'text' | 'password' | 'number' | 'bool' | 'select' | 'list';
  required: boolean;
  default: unknown;
  secret: boolean;
  help?: string | null;
  options: string[];
}

export interface PluginInfo {
  kind: string;
  key: string;
  name: string;
  description: string;
  builtin: boolean;
  fields: FieldSpec[];
  protocol?: string;
  default_port?: number;
  supports_movies?: boolean;
  supports_series?: boolean;
  requires_api_key?: boolean;
}

export interface Dashboard {
  movies: { total: number; with_file: number; missing: number };
  series: { total: number; episodes: number; episodes_with_file: number; episodes_missing: number };
  missing_total: number;
  downloads: { active: number };
  failed_imports: number;
  failed_last_7_days: number;
  storage: { library_bytes: number; movies_bytes: number; tv_bytes: number; disks: { path: string; total: number; used: number; free: number }[] };
  issues: Record<string, number>;
  upcoming: { episode_id: number; series_id: number; series: string; season: number; episode: number; title: string; air_date: string; poster?: string }[];
  recent_activity: HistoryItem[];
}

export interface Job {
  id: number;
  name: string;
  title: string;
  status: string;
  trigger?: string;
  attempts: number;
  progress?: number | null;
  message?: string | null;
  error?: string | null;
  result?: unknown;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface LiveEvent {
  type: string;
  payload: Record<string, unknown>;
  timestamp?: string;
}
