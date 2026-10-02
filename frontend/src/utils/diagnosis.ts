// Import failure reason vocabulary, mirroring backend/domain/diagnosis.py's ImportFailureReason.
export const REASON_LABELS: Record<string, string> = {
  path_not_found: 'Path not found',
  remote_path_mapping: 'Path mapping',
  permission_denied: 'Permission denied',
  disk_full: 'Disk full',
  cross_device_failed: 'Cross-device move failed',
  path_too_long: 'Path too long',
  destination_exists: 'Destination exists',
  still_downloading: 'Still downloading',
  no_video_files: 'No video files',
  archive_not_extracted: 'Archive not extracted',
  sample_only: 'Sample only',
  corrupt_file: 'Corrupt file',
  too_small: 'File too small',
  unsupported_format: 'Unsupported format',
  unparseable_episode: 'Unparseable episode',
  not_matched: 'Not matched',
  media_deleted: 'Media deleted',
  quality_not_allowed: 'Quality not allowed',
  not_an_upgrade: 'Not an upgrade',
  duplicate: 'Duplicate',
  naming_template_error: 'Naming template error',
  unknown: 'Unknown',
};

export type ReasonCategory = 'setup' | 'release' | 'library' | 'other';

const CATEGORY_MAP: Record<string, ReasonCategory> = {
  path_not_found: 'setup',
  remote_path_mapping: 'setup',
  permission_denied: 'setup',
  disk_full: 'setup',
  cross_device_failed: 'setup',
  path_too_long: 'setup',
  destination_exists: 'setup',
  still_downloading: 'release',
  no_video_files: 'release',
  archive_not_extracted: 'release',
  sample_only: 'release',
  corrupt_file: 'release',
  too_small: 'release',
  unsupported_format: 'release',
  unparseable_episode: 'library',
  not_matched: 'library',
  media_deleted: 'library',
  quality_not_allowed: 'library',
  not_an_upgrade: 'library',
  duplicate: 'library',
  naming_template_error: 'library',
};

export function reasonCategory(reason: string): ReasonCategory {
  return CATEGORY_MAP[reason] ?? 'other';
}

export function reasonLabel(reason: string): string {
  return REASON_LABELS[reason] ?? reason.replace(/_/g, ' ');
}

export const CATEGORY_COLORS: Record<ReasonCategory, string> = {
  setup: '#f59e0b',
  release: '#a855f7',
  library: '#38bdf8',
  other: '#94a3b8',
};
