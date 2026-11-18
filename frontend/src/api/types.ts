export type Role =
  | "super_admin"
  | "domain_admin"
  | "restore_operator"
  | "viewer";
export type BackupType = "full" | "incremental";
export type SnapshotStatus = "complete" | "partial" | "expired";
export type JobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface RoleGrant {
  id: string;
  role: Role;
  domain_id: string | null;
}

export interface User {
  id: string;
  username: string;
  display_name: string | null;
  email: string | null;
  auth_source: "local" | "ldap" | "oidc";
  roles: RoleGrant[];
}

export interface SessionInfo {
  user: User;
  csrf_token: string;
}

export interface Health {
  status: string;
  version: string;
}

export interface Domain {
  id: string;
  name: string;
  account_count: number;
}

export interface AccountRow {
  id: string;
  email: string;
  status: string;
  last_seen_at: string | null;
  last_backup_at: string | null;
  last_backup_status: SnapshotStatus | null;
  last_backup_type: BackupType | null;
}

export interface Snapshot {
  id: string;
  seq: number;
  backup_type: BackupType;
  status: SnapshotStatus;
  taken_at: string;
  item_count: number;
  size_bytes: number;
  new_bytes: number;
}

export interface AccountDetail {
  id: string;
  email: string;
  status: string;
  domain_id: string;
  domain: string;
  last_seen_at: string | null;
  snapshot_count: number;
  last_snapshot: Snapshot | null;
}

export interface Job {
  id: string;
  kind: string;
  status: JobStatus;
  account_id: string | null;
  account_email: string | null;
  backup_type: BackupType | null;
  restore_mode?: RestoreMode | null;
  dry_run?: boolean | null;
  source_email?: string | null;
  snapshot_seq?: number | null;
  attempts: number;
  progress: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Dashboard {
  accounts: number;
  protected_24h: number;
  never_backed_up: number;
  partial_last: number;
  jobs_queued: number;
  jobs_running: number;
  jobs_failed_24h: number;
  stored_bytes: number;
  agents_active: number | null;
  last_agent_heartbeat: string | null;
}

export type RestoreMode = "new_folder" | "original";
export type ItemKind =
  | "message"
  | "contact"
  | "appointment"
  | "task"
  | "document";

export interface SnapshotFolder {
  id: number;
  parent_id: number | null;
  path: string;
  view: ItemKind;
  item_count: number;
}

export interface SnapshotItem {
  id: number;
  folder_id: number;
  kind: ItemKind;
  subject: string | null;
  sender: string | null;
  date: string | null;
  size: number;
}

export interface RestoreRequest {
  account_id: string;
  snapshot_id: string;
  target_account_id?: string;
  mode: RestoreMode;
  dry_run: boolean;
  folder_ids?: number[];
  item_ids?: number[];
}

/** `progress` of a finished restore job. */
export interface RestoreSummary {
  dry_run: boolean;
  mode: RestoreMode;
  root: string | null;
  selected: number;
  restored: number;
  present: number;
  unsupported: number;
  failed_count: number;
  folders_created: string[];
}

export type TargetType = "global" | "domain" | "account" | "accounts";

export interface RetentionRule {
  last: number;
  daily: number;
  weekly: number;
  monthly: number;
}

export interface Policy {
  id: string;
  name: string;
  target_type: TargetType;
  target_id: string | null;
  target_name: string | null;
  account_ids: string[];
  backup_type: BackupType;
  schedule: string;
  timezone: string;
  retention: RetentionRule;
  priority: number;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  created_at: string;
}

export interface PolicyInput {
  name: string;
  target_type: TargetType;
  target_id: string | null;
  account_ids?: string[];
  backup_type: BackupType;
  schedule: string;
  timezone: string;
  retention: RetentionRule;
  enabled: boolean;
}

/** Row of ``GET /api/accounts`` (all fields, see the column picker). */
export interface AccountItem {
  id: string;
  email: string;
  domain_id: string;
  domain: string;
  status: string;
  display_name: string | null;
  first_name: string | null;
  last_name: string | null;
  aliases: string[];
  cos: string | null;
  quota_bytes: number | null;
  used_bytes: number | null;
  usage_pct: number | null;
  is_admin: boolean;
  last_login_at: string | null;
  carbonio_created_at: string | null;
  last_seen_at: string | null;
  last_backup_at: string | null;
  last_backup_status: SnapshotStatus | null;
  last_backup_type: BackupType | null;
  snapshot_count: number;
  stored_bytes: number | null;
  policies: string[];
}

export type FieldType = "text" | "number" | "date" | "choice" | "boolean";

export interface AccountFacets {
  domains: { id: string; name: string }[];
  cos: string[];
  fields: Record<string, FieldType>;
}

export interface BulkBackupResult {
  queued: number;
  skipped_busy: number;
  skipped_deleted: number;
}

export interface NotificationSettings {
  enabled: boolean;
  host: string;
  port: number;
  security: "starttls" | "ssl" | "none";
  username: string;
  password_set: boolean;
  sender: string;
  recipients: string[];
  events: Record<string, boolean>;
  digest_hour: number;
  timezone: string;
  language: "fr" | "en";
  secret_key_configured: boolean;
}

export type NotificationSettingsInput = Omit<
  NotificationSettings,
  "password_set" | "secret_key_configured"
> & { password: string | null };

export interface OutboxEntry {
  id: number;
  event: string;
  subject: string;
  status: "pending" | "sent" | "failed";
  attempts: number;
  error: string | null;
  created_at: string;
  sent_at: string | null;
}
