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

export interface ApiKey {
  id: number;
  name: string;
  prefix: string;
  user_id: number;
  permissions: string[];
  allow_query_param: boolean;
  created_at: string;
  expires_at?: string | null;
  last_used_at?: string | null;
  last_used_ip?: string | null;
  revoked_at?: string | null;
}

export interface ApiKeyCreatedResponse extends ApiKey {
  api_key: string;
}

export interface ApiKeyPreset {
  name: string;
  description: string;
  permissions: string[];
  allow_query_param: boolean;
}


export interface Cast {
  name: string;
  character?: string | null;
  image?: string | null;
}

export interface MovieSummary {
  id: number;
  title: string;
  sort_title?: string | null;
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
  video_resolution?: string | null;
  hdr_format?: string | null;
  video_codec?: string | null;
  audio_codec?: string | null;
  audio_channels?: number | null;
  audio_languages?: string[] | null;
  release_date?: string | null;
  digital_release_date?: string | null;
  physical_release_date?: string | null;
  production_status?: string | null;
  /** where it stands against the server's release gate: 'released' (no file = really missing), 'in_theaters',
   *  'coming_soon', a TMDB pre-release stage ('post_production', 'in_production', 'planned', 'rumored'),
   *  'announced' or 'canceled' */
  release_state?: string | null;
  /** the gate's reason, naming the date it was based on */
  release_note?: string | null;
}

export interface MediaInfoVideo {
  codec: string;
  profile?: string | null;
  width: number;
  height: number;
  bit_depth?: number | null;
  hdr_format?: string | null;
  frame_rate?: number | null;
  bitrate?: number | null;
}

export interface MediaInfoAudio {
  codec: string;
  profile?: string | null;
  channels: number;
  channel_layout?: string | null;
  language?: string | null;
  title?: string | null;
  default?: boolean;
  commentary?: boolean;
}

export interface MediaInfoSubtitle {
  codec: string;
  language?: string | null;
  title?: string | null;
  forced?: boolean;
  hearing_impaired?: boolean;
}

export interface MediaInfoPayload {
  container?: string | null;
  duration_seconds?: number | null;
  overall_bitrate?: number | null;
  video?: MediaInfoVideo | null;
  audio?: MediaInfoAudio[];
  subtitles?: MediaInfoSubtitle[];
  chapters?: number;
}

export interface MediaFile {
  id: number;
  media_type: 'movie' | 'tv';
  movie_id?: number | null;
  series_id?: number | null;
  path: string;
  size_bytes: number;
  quality?: string | null;
  codec?: string | null;
  release_group?: string | null;
  scene_name?: string | null;
  edition?: string | null;
  is_primary: boolean;
  added_at?: string | null;
  inspected_at?: string | null;
  inspection_error?: string | null;
  container?: string | null;
  duration_seconds?: number | null;
  overall_bitrate?: number | null;
  video_codec?: string | null;
  video_width?: number | null;
  video_height?: number | null;
  video_resolution?: string | null;
  video_bit_depth?: number | null;
  hdr_format?: string | null;
  audio_codec?: string | null;
  audio_channels?: number | null;
  audio_languages?: string[] | null;
  subtitle_languages?: string[] | null;
  mediainfo?: MediaInfoPayload | null;
  custom_formats?: number[] | null;
  custom_format_score?: number | null;
  episodes?: { id: number; series_id?: number; season_number: number; episode_number: number; title: string }[];
}

export interface Movie extends MovieSummary {
  requested_by?: string[];
  quality_profile_id?: number | null;
  root_folder?: string | null;
  path?: string | null;
  file_path?: string | null;
  tmdb_id?: number | null;
  imdb_id?: string | null;
  overview?: string | null;
  tagline?: string | null;
  backdrop_path?: string | null;
  votes?: number | null;
  original_title?: string | null;
  cast_members?: Cast[] | null;
  crew?: { name: string; job: string }[] | null;
  metadata_source?: string | null;
  last_metadata_refresh?: string | null;
  last_search_at?: string | null;
  files?: MediaFile[];
}

export interface Episode {
  id: number;
  series_id: number;
  season_number: number;
  episode_number: number;
  title: string;
  overview?: string | null;
  air_date?: string | null;
  air_date_utc?: string | null;
  /** out per the server's release gate (exact air time, or the whole air date over everywhere; TBA never) */
  aired: boolean;
  monitored: boolean;
  has_file: boolean;
  file_path?: string | null;
  file_size_bytes?: number | null;
  quality?: string | null;
  status?: string | null;
  runtime_minutes?: number | null;
  series_title?: string;
  files?: MediaFile[];
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
  sort_title?: string | null;
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
  aired_episode_count: number;
  missing_count: number;
  size_on_disk: number;
  added_date?: string | null;
  /** distinct ffprobe-measured facts across the series' inspected episode files, best resolution first */
  video_resolutions: string[];
  hdr_formats: string[];
  video_codecs: string[];
  audio_codecs: string[];
}

export interface DeleteEpisodesResult {
  episodes: number;
  files: { path: string; exists: boolean; shared: boolean }[];
  files_recycled: number;
  applied: boolean;
}

export interface PruneStaleResult {
  provider: string;
  episodes: number;
  kept_with_files: number;
  seasons: { season_number: number; episodes: number; removed: boolean }[];
  applied: boolean;
}

export interface LinkResult {
  match: {
    provider: string;
    title: string;
    year?: number | null;
    overview?: string | null;
    poster_url?: string | null;
    imdb_id?: string | null;
    tmdb_id?: number | null;
    tvdb_id?: number | null;
    episode_count: number;
  };
  existing?: { id: number; title: string; year?: number | null } | null;
  /** with `existing` (movies only): what merging this entry into it would do */
  merge?: MergePlan | null;
  /** set when the entry was merged away – open this id instead */
  merged_into?: number | null;
  applied: boolean;
  renamed: number;
  rename_errors: string[];
}

export interface MergeFile {
  path: string;
  size_bytes: number;
  quality?: string | null;
  /** 'this' = the entry being linked, 'existing' = the one already in the library */
  owner: 'this' | 'existing';
  exists: boolean;
  recommended: boolean;
}

export interface MergePlan {
  files: MergeFile[];
  /** emptied into the recycle bin after the merge */
  cleanup_folder?: string | null;
}

export interface Series extends SeriesSummary {
  requested_by?: string[];
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

export type DiscoverReason = 'announced' | 'in_theaters' | 'digital' | 'physical' | 'series_premiere' | 'season_premiere';

export interface DiscoverItem {
  media_type: 'movie' | 'tv';
  provider: string;
  title: string;
  original_title?: string | null;
  year?: number | null;
  overview?: string | null;
  tagline?: string | null;
  poster_url?: string | null;
  backdrop_url?: string | null;
  genres: string[];
  runtime_minutes?: number | null;
  rating?: number | null;
  votes?: number | null;
  certification?: string | null;
  studio?: string | null;
  release_date?: string | null;
  digital_release_date?: string | null;
  physical_release_date?: string | null;
  theatrical_release_date?: string | null;
  tmdb_id: number;
  imdb_id?: string | null;
  tvdb_id?: number | null;
  tvmaze_id?: number | null;
  season_count?: number | null;
  section: string;
  reason: DiscoverReason;
  reason_date?: string | null;
  season_number?: number | null;
  watch_providers: { name: string; logo_url?: string | null }[];
  popularity?: number | null;
  in_library: boolean;
  library_id?: number | null;
  first_seen_at?: string | null;
}

export interface DiscoverSection {
  key: string;
  label: string;
  items: DiscoverItem[];
  total: number;
  error?: string;
}

export interface DiscoverResponse {
  media_type: 'movie' | 'tv';
  configured: boolean;
  region: string;
  provider?: string | null;
  sections: DiscoverSection[];
}

export interface DiscoverExclusion {
  id: number;
  media_type: 'movie' | 'tv';
  tmdb_id: number;
  title: string;
  year?: number | null;
  created_at: string;
}

export interface ReleaseFormatMatch {
  id: number;
  name: string;
  score: number;
}

export interface ScoreBreakdownItem {
  label: string;
  value: number;
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
  hdr_format?: string | null;
  audio?: string | null;
  audio_channels?: string | null;
  streaming_service?: string | null;
  approved: boolean;
  rejections: string[];
  score: number;
  custom_formats?: ReleaseFormatMatch[];
  format_score?: number;
  score_breakdown?: ScoreBreakdownItem[];
  season?: number | null;
  episodes: number[];
  /** the release/air date that cleared (or held back) this release */
  release_gate?: string | null;
  download_url_token: string;
}

export interface FixSuggestion {
  kind: string;
  label: string;
  params: Record<string, unknown>;
  safe_auto: boolean;
  changes_settings: boolean;
  bulk_applicable: boolean;
}

export interface ImportFailureAnalysis {
  reason: string;
  summary: string;
  evidence: Record<string, unknown>;
  fixes: FixSuggestion[];
  signature: string;
  confidence: number;
  analysed_at: string;
}

export interface FailureSignatureGroup {
  signature: string;
  reason: string;
  count: number;
  examples: string[];
  fix: FixSuggestion | null;
}

export interface FailuresSummary {
  total: number;
  by_reason: { reason: string; count: number }[];
  top_signatures: FailureSignatureGroup[];
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
  failure_reason?: string | null;
  failure_signature?: string | null;
  failure_analysis?: ImportFailureAnalysis | null;
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
  data?: {
    analysis?: ImportFailureAnalysis;
    formats?: string[];
    format_score?: number;
  } & Record<string, unknown> | null;
}

export interface CustomFormatSpec {
  name: string;
  kind: string;
  value: unknown;
  negate: boolean;
  required: boolean;
}

export interface CustomFormat {
  id: number;
  name: string;
  include_in_rename: boolean;
  specifications: CustomFormatSpec[];
  source?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CustomFormatTestResult {
  facts: Record<string, unknown>;
  matches: { id: number; name: string; score: number; matched_specs: string[] }[];
  formats: {
    format_id: number;
    name: string;
    matched: boolean;
    matched_specs: string[];
    score: number;
    specifications: { name: string; kind: string; required: boolean; negate: boolean; disabled: boolean; passed: boolean }[];
  }[];
  total_score: number;
  score_breakdown: ScoreBreakdownItem[];
  adhoc?: {
    matched: boolean;
    specifications: { name: string; kind: string; required: boolean; negate: boolean; disabled: boolean; passed: boolean }[];
    errors: string[];
  } | null;
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
  format_scores?: Record<string, number>;
  min_format_score?: number;
  upgrade_until_format_score?: number;
  min_format_upgrade_delta?: number;
}

/** Profile given to newly added movies / TV shows. */
export interface QualityProfileDefaults {
  movie: number;
  tv: number;
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
  failed_top_reasons: { reason: string; count: number }[];
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

export type IssueSeverity = 'info' | 'warning' | 'error';
export type IssueStatus = 'open' | 'resolved' | 'dismissed';

export interface IssueTypeSpec {
  type: string;
  label: string;
  severity: IssueSeverity;
  actions: string[];
  health_component: string | null;
}

export interface LibraryIssue {
  id: number;
  issue_type: string;
  label: string;
  media_type: 'movie' | 'tv' | null;
  media_id: number | null;
  title: string | null;
  episode_id: number | null;
  path: string | null;
  details: string | null;
  data: Record<string, unknown> | null;
  severity: IssueSeverity;
  status: IssueStatus;
  detected_at: string;
  first_detected_at: string;
  last_seen_at: string | null;
  resolved_at: string | null;
  resolution: string | null;
  actions: string[];
}

/** `data.search` on an issue: the latest automatic search started from it (or for it, when its file went missing). */
export interface IssueSearch {
  status: 'queued' | 'skipped' | 'grabbed' | 'held' | 'not_found' | 'failed';
  at: string;
  reason?: string | null;
  grabbed?: string[];
  quality?: string | null;
  candidates?: number;
  indexers?: { indexer: string; results: number; error: string | null }[];
  releases?: { title: string; indexer: string; quality: string; size_bytes: number; rejections: string[] }[];
}

export interface IssueSummary {
  by_type: Record<string, { open: number; dismissed: number; severity: IssueSeverity }>;
  by_severity: Partial<Record<IssueSeverity, number>>;
}

/** A video file found by `POST /library/manual-import/scan`, with its guessed (or chosen) library match. */
export interface Candidate {
  path: string; size: number; state: string; reason: string | null; quality: string;
  media_type: 'movie' | 'tv' | null; media_id: number | null; title: string | null; season: number | null; episodes: number[];
  library_state: 'new' | 'upgrade' | 'duplicate' | null; existing_quality: string | null; existing_size: number | null;
}

export interface ImportResult { imported: string[]; failed: string[]; failed_paths: Record<string, string>; failed_codes: Record<string, string> }
