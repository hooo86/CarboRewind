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
  is_active?: boolean;
  locked_until?: string | null;
  last_login_at?: string | null;
  totp_enabled?: boolean;
  failed_logins?: number;
  created_at?: string | null;
  /** /api/auth/me only. */
  totp_enrollment_required?: boolean;
  recovery_codes_left?: number;
}

export interface SessionInfo {
  user: User;
  csrf_token: string;
  totp_enrollment_required?: boolean;
}

export interface TotpSetup {
  secret: string;
  uri: string;
  qr_svg: string;
}

export type TwoFactorPolicy = "none" | "admins" | "all";

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
  storage?: StorageReport[];
  mail_used_bytes?: number;
  mail_quota_bytes?: number;
  mailboxes_nearly_full?: number;
  mailbox_alert_percent?: number;
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
  profile?: Record<string, string>;
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

export interface StorageReport {
  agent: string;
  path: string;
  kind: "local" | "network";
  fstype: string | null;
  source: string | null;
  total_bytes?: number | null;
  free_bytes?: number | null;
  error?: string | null;
  reported_at: string;
  mail_store?: Omit<
    StorageReport,
    "agent" | "reported_at" | "replica" | "apply" | "mail_store"
  > | null;
  replica?: Omit<
    StorageReport,
    "agent" | "reported_at" | "replica" | "apply"
  > | null;
  apply?: {
    revision?: number | null;
    ok?: boolean | null;
    error?: string | null;
    at?: string | null;
    changed?: string[] | null;
  } | null;
}

export interface ShareSettings {
  share: string;
  username: string;
  domain: string;
  seal: boolean;
  password_set: boolean;
}

export interface ShareInput {
  share: string;
  username: string;
  password?: string | null;
  domain: string;
  seal: boolean;
}

export interface StorageSettings {
  revision: number;
  primary_kind: "local" | "smb" | null;
  primary_smb: ShareSettings | null;
  migrate: boolean;
  copy_02: ShareSettings | null;
  agents: Partial<StorageReport>[];
  applied: boolean;
  last_replication: {
    status: JobStatus;
    created_at: string;
    finished_at: string | null;
    progress: Record<string, unknown> | null;
    error: string | null;
  } | null;
  secret_key_configured: boolean;
}

export interface StorageSettingsInput {
  primary: { kind: "local" | "smb"; smb?: ShareInput | null; migrate: boolean };
  copy_02: ShareInput | null;
}

// ------------------------------------------------------------------ migrations

export type ImapSecurity = "ssl" | "starttls" | "none";
export type MigrationAuth = "password" | "master_plain" | "master_login";

export interface MigrationSource {
  id: string;
  name: string;
  host: string;
  port: number;
  security: ImapSecurity;
  verify_tls: boolean;
  auth_mode: MigrationAuth;
  master_username: string | null;
  master_password_set: boolean;
  master_separator: string;
  batches: number;
}

export interface MigrationSourceInput {
  name: string;
  host: string;
  port: number;
  security: ImapSecurity;
  verify_tls: boolean;
  auth_mode: MigrationAuth;
  master_username?: string | null;
  master_password?: string | null;
  master_separator?: string;
}

export interface MigrationOptions {
  root?: string | null;
  exclude_folders?: string[];
  since?: string | null;
  before?: string | null;
}

export interface MigrationBatch {
  id: string;
  name: string;
  source_id: string;
  source_name: string;
  auth_mode: MigrationAuth;
  options: MigrationOptions;
  created_at: string;
  counts: Record<string, number>;
}

export type MigrationStatus =
  | "new"
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface MigrationStats {
  copied?: number;
  duplicates?: number;
  failed?: number;
  bytes?: number;
  total_copied?: number;
  total_bytes?: number;
  cancelled?: boolean;
  finished_at?: string;
  plan?: { messages: number; total: number; folders: number; at: string };
}

export interface MigrationMailbox {
  id: string;
  source_email: string;
  source_login: string | null;
  password_set: boolean;
  target_account_id: string | null;
  target_email: string | null;
  target_status: string | null;
  match: "exact" | "alias" | "local_part" | "manual" | null;
  root: string | null;
  status: MigrationStatus;
  dry_run: boolean | null;
  progress: Record<string, unknown> | null;
  stats: MigrationStats | null;
  error: string | null;
  last_run_at: string | null;
}

export interface MigrationBatchDetail extends MigrationBatch {
  mailboxes: MigrationMailbox[];
}

export interface MigrationRowInput {
  source_email: string;
  source_login?: string | null;
  password?: string | null;
  target_email?: string | null;
  root?: string | null;
}

export interface ImportReport {
  added: number;
  updated: number;
  matched: number;
  unmatched: { line: number; source: string; candidates: string[] }[];
  errors: { line: number; message: string }[];
}

export interface StartResult {
  queued: number;
  skipped: { id: string; source: string; reason: string }[];
}

// ------------------------------------------------------------------ mail traffic

export type MailPeriod = "1h" | "24h" | "7d" | "30d";

export interface MailLimits {
  enabled: boolean;
  sent_messages_hour: number;
  sent_recipients_hour: number;
  sent_messages_day: number;
  sent_recipients_day: number;
  spam_sent_day: number;
  action: "alert" | "lock";
  exempt: string[];
}

export interface MailAlert {
  id: string;
  account_id: string | null;
  email: string;
  metric: string;
  value: number;
  threshold: number;
  window_start: string;
  action: string;
  created_at: string;
  acknowledged_at: string | null;
  account_status: string | null;
}

export interface MailOverview {
  period: MailPeriod;
  since: string;
  totals: Record<
    | "sent_messages"
    | "sent_recipients"
    | "spam_sent"
    | "spam_received"
    | "spam_senders",
    number
  >;
  top_senders: {
    email: string;
    account_id: string | null;
    status: string | null;
    messages: number;
    recipients: number;
    spam_sent: number;
  }[];
  spam_recipients: { email: string; count: number }[];
  spam_senders: {
    sender: string;
    count: number;
    ips: string[];
    last: string;
  }[];
  hourly: {
    at: string;
    sent_messages: number;
    sent_recipients: number;
    spam_received: number;
    spam_sent: number;
  }[];
  alerts: MailAlert[];
  pending_actions: {
    id: string;
    email: string;
    action: "lock" | "unlock";
    created_at: string;
  }[];
  limits: MailLimits;
  agents: { agent: string; log?: string | null; reported_at?: string }[];
}

// ------------------------------------------------------------------ monitoring

export interface MonitoringSettings {
  stale_hours: number;
  replica_max_lag_hours: number;
  integrity_enabled: boolean;
  integrity_cron: string;
  integrity_sample: number;
  integrity_timezone: string;
  mailbox_alert_percent: number;
  mailbox_alert_gb: number;
}

export interface StorageSeries {
  agent: string;
  target: "primary" | "replica" | "server";
  total_bytes: number;
  free_bytes: number;
  used_bytes: number;
  days_until_full: number | null;
  growth_per_day: number | null;
  points: { at: string; used_bytes: number }[];
}

export interface RiskCategory {
  count: number;
  filters: string[];
  accounts: { id: string; email: string; last_backup_at: string | null }[];
}

export interface IntegrityCheck {
  job_id: string;
  status: JobStatus;
  created_at: string;
  finished_at: string | null;
  sample: number | null;
  progress: {
    checked?: number;
    corrupted_count?: number;
    manifests_checked?: number;
    manifests_corrupted?: string[];
  } | null;
  error: string | null;
}

export interface MonitoringOverview {
  settings: MonitoringSettings;
  storage: StorageSeries[];
  at_risk: Record<string, RiskCategory>;
  replica: {
    last_success_at: string | null;
    behind: boolean;
    lag_seconds: number;
    max_lag_hours: number;
    late: boolean;
    last_status: string | null;
    last_error: string | null;
  } | null;
  integrity: { primary: IntegrityCheck | null; replica: IntegrityCheck | null };
  next_integrity_at: string | null;
  jobs_failed_24h: Record<string, number>;
  agents: {
    name: string;
    version: string | null;
    last_heartbeat_at: string | null;
    cert_not_after: string | null;
    mail_reported_at: string | null;
  }[];
  metrics_token_configured: boolean;
}
