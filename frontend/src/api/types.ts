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
  quota_bytes?: number | null;
  used_bytes?: number | null;
  password_changed_at?: string | null;
  password_expires_at?: string | null;
  password_must_change?: boolean;
}

export interface QuotaResult {
  queued: number;
  unchanged: number;
  skipped_deleted: number;
  not_found: number;
}

export interface Job {
  bytes_done?: number | null;
  throughput_bps?: number | null;
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
  passwords_expiring?: number;
  password_alert_days?: number;
  password_filter?: string[];
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
  archived_messages?: number;
  archived_bytes?: number;
  archived_removed_bytes?: number;
  services_down?: string[];
  blacklisted?: string[];
  mail_queue_total?: number | null;
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
  full_schedule?: string | null;
  next_full_run_at?: string | null;
  created_at: string;
  account_count?: number;
  protected_bytes?: number;
  stored_bytes?: number;
  snapshot_count?: number;
  last_run?: PolicyRun | null;
}

/** One execution of a policy: the backups queued together (one per account). */
export interface PolicyRun {
  started_at: string;
  finished_at: string | null;
  backup_type: BackupType;
  trigger: "schedule" | "manual";
  status: "running" | "succeeded" | "partial" | "failed" | "cancelled";
  accounts: number;
  succeeded: number;
  failed: number;
  pending: number;
  read_bytes: number;
  new_bytes: number;
  size_bytes: number;
}

export interface AccountGroup {
  value: string | null;
  count: number;
  used_bytes: number;
  stored_bytes: number;
}

export interface SavedView {
  id: string;
  page: string;
  name: string;
  query: string;
  shared: boolean;
  owner: string;
  mine: boolean;
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
  /** Full backups on a second schedule; "" removes it. */
  full_schedule?: string | null;
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
  password_changed_at?: string | null;
  password_expires_at?: string | null;
  password_must_change?: boolean;
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
  groupable?: string[];
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
  fallback_enabled?: boolean;
  fallback_host?: string;
  fallback_port?: number;
  fallback_security?: "starttls" | "ssl" | "none";
  fallback_username?: string;
  fallback_password_set?: boolean;
  fallback_sender?: string;
  secret_key_configured: boolean;
}

export type NotificationSettingsInput = Omit<
  NotificationSettings,
  "password_set" | "secret_key_configured" | "fallback_password_set"
> & { password: string | null; fallback_password?: string | null };

export interface BlacklistSettings {
  enabled: boolean;
  ips: string[];
  zones: string[];
  interval_minutes: number;
}

export interface BlacklistResult {
  ip: string;
  zone: string;
  listed: boolean | null;
  codes: string[];
  error: string | null;
  since?: string | null;
}

export interface BlacklistStatus {
  settings: BlacklistSettings;
  checked_at: string | null;
  results: BlacklistResult[];
}

export interface TelegramSettings {
  enabled: boolean;
  token_set: boolean;
  chat_ids: string[];
  events: Record<string, boolean>;
}

export interface TelegramSettingsInput {
  enabled: boolean;
  token: string | null;
  chat_ids: string[];
  events: Record<string, boolean>;
}

export interface RestoreTestSettings {
  enabled: boolean;
  schedule: string;
  timezone: string;
  target_account_id: string | null;
  messages: number;
  cleanup: boolean;
}

export interface RestoreTestRun {
  job_id: string;
  status: JobStatus;
  source: string | null;
  seq: number | null;
  target: string;
  selected: number | null;
  restored: number | null;
  failed: number | null;
  ok: boolean | null;
  cleaned: boolean | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface RestoreTests {
  settings: RestoreTestSettings;
  target_email: string | null;
  history: RestoreTestRun[];
}

export interface OutboxEntry {
  id: number;
  channel?: "email" | "telegram";
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
  service_down_reports: number;
  mail_queue_alert: number;
  password_alert_days?: number;
}

export interface ServiceState {
  name: string;
  ok: boolean | null;
  source: "carbonio" | "port" | "mta" | "guard";
  port?: number | null;
  since?: string | null;
  detail?: string | null;
}

export interface MailTestResult {
  address: string;
  status:
    | "pending"
    | "sent"
    | "deferred"
    | "bounced"
    | "expired"
    | "error"
    | "timeout";
  relay?: string;
  detail?: string;
}

export interface MailTestReport {
  id: string;
  reason: "manual" | "after_start";
  started: string;
  done: boolean;
  ok: boolean | null;
  error: string | null;
  results: Record<string, MailTestResult>;
}

export interface AutostartReport {
  enabled: boolean;
  minutes: number;
  paused_until: string | null;
  down_since: string | null;
  note: string | null;
  last_request: { id: string; at: string; down: string[] } | null;
  last_result: {
    id: string;
    at?: string;
    ok: boolean;
    returncode?: number;
    output?: string;
    error?: string;
  } | null;
  starts_6h: number;
}

export interface ServicesView {
  agent: string;
  agent_id?: string;
  checked_at: string | null;
  stale: boolean;
  services: ServiceState[];
  down: string[];
  queue: Record<string, number> | null;
  queue_total: number | null;
  errors: string[];
  autostart?: AutostartReport | null;
  mail_test?: MailTestReport | null;
  mail_test_request?: {
    id: string;
    by: string;
    at: string;
    sent_at: string | null;
  } | null;
  certificate?: CertificateReport | null;
  cert_renew_request?: { id: string; by: string; at: string } | null;
}

export interface CertificateInfo {
  service: string;
  port: number;
  name: string;
  subject?: string | null;
  names?: string[];
  issuer?: string | null;
  not_after?: string;
  days_left?: number;
  matches?: boolean;
  self_signed?: boolean;
  error?: string;
}

export interface CertificateReport {
  name: string;
  alert_days: number;
  renew_enabled: boolean;
  renew_days: number;
  checked_at: string | null;
  days_left: number | null;
  certificates: CertificateInfo[];
  last_request: { id: string; reason: string; at: string } | null;
  last_result: {
    id: string;
    at: string;
    ok: boolean;
    renewed: boolean;
    not_after: string | null;
    error: string | null;
  } | null;
  note: string | null;
}

export type MailQueueOp = "requeue" | "hold" | "release" | "delete" | "flush";

export interface MailQueueItem {
  id: string;
  from: string;
  to: string;
  time: number | null;
  reason: string;
  origip: string;
}

export interface MailQueueDetail {
  server: string;
  queue: string;
  total: number;
  items: MailQueueItem[];
  reasons: { reason: string; count: number }[];
}

export interface MailQueueAction {
  id: string;
  op: MailQueueOp;
  queue: string | null;
  ids: string[];
  by: string;
  at: string;
  sent_at?: string | null;
  done_at?: string;
  error?: string | null;
}

export interface MailQueueAgent {
  agent_id: string;
  agent: string;
  checked_at: string | null;
  queue: Record<string, number> | null;
  queue_total: number | null;
  details: MailQueueDetail[] | null;
  pending: MailQueueAction[];
  results: (MailQueueAction & { done_at: string })[];
}

export interface MailQueueActionInput {
  agent_id: string;
  op: MailQueueOp;
  queue?: string;
  ids?: string[];
  server?: string;
  confirm?: number;
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
  services?: ServicesView[];
  metrics_token_configured: boolean;
}

export type ArchiveMode = "simulate" | "copy" | "archive";

export interface ArchivePolicyInput {
  name: string;
  enabled: boolean;
  mode: ArchiveMode;
  domain_ids: string[];
  account_ids: string[];
  exclude_account_ids: string[];
  older_than_days: number;
  exclude_folders: string[];
  keep_flagged: boolean;
  keep_unread: boolean;
  include_admins: boolean;
  max_per_run: number;
  schedule: string;
  timezone: string;
  retention_days: number;
}

export interface ArchivePolicy extends ArchivePolicyInput {
  id: string;
  effective_mode: ArchiveMode;
  simulation_valid: boolean;
  cutoff: string;
  criteria_changed_at: string;
  simulated_at: string | null;
  last_run_at: string | null;
  created_at: string;
  archived_count: number;
  archived_bytes: number;
}

export interface ArchiveRun {
  id: string;
  run: string | null;
  mode: ArchiveMode | null;
  policy: string | null;
  account_email: string | null;
  status: JobStatus;
  progress: {
    candidates?: number;
    candidate_bytes?: number;
    truncated?: boolean;
    archived?: number;
    archived_bytes?: number;
    removed_count?: number;
    removed_bytes?: number;
    error_count?: number;
    by_folder?: Record<string, [number, number]>;
  } | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface ArchiveSummary {
  messages: number;
  bytes: number;
  removed_messages: number;
  removed_bytes: number;
  accounts: number;
  last_archived_at: string | null;
  legal_hold_messages?: number;
  legal_hold_accounts?: number;
}

export interface ArchivedMessage {
  id: number;
  account_id: string;
  account_email: string;
  folder_path: string;
  subject: string | null;
  sender: string | null;
  recipients: string | null;
  item_date: string | null;
  size: number;
  has_attachments: boolean;
  archived_at: string;
  removed_at: string | null;
  legal_hold?: boolean;
}

export interface ArchivedMailbox {
  account_id: string;
  account_email: string;
  messages: number;
  bytes: number;
  removed_messages: number;
  removed_bytes: number;
  legal_hold: boolean;
  oldest: string | null;
  newest: string | null;
  last_archived_at: string | null;
}

export interface LegalHold {
  account_id: string;
  account_email: string;
  messages: number;
  bytes: number;
  last_archived_at: string | null;
  pending: boolean;
}

export interface ArchiveSearch {
  account?: string;
  q?: string;
  sender?: string;
  recipient?: string;
  folder?: string;
  date_from?: string;
  date_to?: string;
  removed?: string;
  legal_hold?: string;
  limit?: number;
  offset?: number;
}

export interface OpenedMessage {
  status: JobStatus;
  error: string | null;
  message: {
    subject: string | null;
    from: string | null;
    to: string | null;
    cc: string | null;
    date: string | null;
    text: string;
    truncated: boolean;
    html_only: boolean;
    attachments: { name: string | null; content_type: string; size: number }[];
    size: number;
  } | null;
}

export interface AgentTuningInput {
  max_concurrent_accounts: number;
  max_bandwidth_mbps: number;
  max_load_per_cpu: number;
  min_free_gb_local: number;
  min_free_percent_local: number;
  min_free_gb_network: number;
  service_monitor: boolean;
  service_ports: Record<string, number>;
  mail_monitor: boolean;
  autostart_enabled: boolean;
  autostart_minutes: number;
  mail_test_after_start: boolean;
  mail_test_internal: string;
  mail_test_external: string;
  cert_monitor: boolean;
  cert_name: string;
  cert_alert_days: number;
  cert_renew_enabled: boolean;
  cert_renew_days: number;
}

export interface AgentTuning extends AgentTuningInput {
  revision: number;
  agents: { name: string; applied: { revision: number; at: string } | null }[];
}

export interface BlockedIp {
  ip: string;
  ptr: string | null;
  domain: string | null;
  failed_mailbox: number;
  failed_smtp: number;
  suspended: number;
  last_suspended_at: string | null;
  recently_blocked: boolean;
  blocked_until?: string | null;
  first_seen: string;
  last_seen: string;
  last_seen_exact?: boolean;
  accounts: string[];
  trusted?: boolean;
  pending?: "trust" | "untrust" | null;
}

export interface IpAction {
  id: string;
  ip: string;
  action: "trust" | "untrust";
  status: "pending" | "done" | "failed";
  error: string | null;
  created_at: string;
  done_at: string | null;
}

export interface LoginFilter {
  config: {
    safe_ips: string[];
    max_failed: number | null;
    block_minutes: number | null;
    task_minutes: number | null;
    at: string | null;
  } | null;
  actions: IpAction[];
}

export interface LoginSecurity {
  period: MailPeriod;
  since: string;
  totals: {
    ips: number;
    failed: number;
    blocked: number;
    blocked_now?: number;
  };
  ips: BlockedIp[];
  reported: boolean;
  log_status?: "ok" | "denied" | "missing" | "agent_outdated" | "not_reporting";
  log_path?: string | null;
}

export interface SpamSource {
  domain: string | null;
  count: number;
  ips: { ip: string; ptr: string | null; count: number }[];
  sender_domains: string[];
  senders?: { address: string; count: number; recipients?: string[] }[];
}

export interface Blocklist {
  revision: number;
  entries: {
    value: string;
    kind: "address" | "domain";
    by: string;
    at: string;
    reason: string | null;
  }[];
  agents: {
    name: string;
    applied: boolean;
    status: {
      revision: number;
      domains: string[];
      error: string | null;
      at: string;
    } | null;
  }[];
}

export interface SpamSources {
  period: MailPeriod;
  since: string;
  sources: SpamSource[];
  sender_domains: { domain: string; count: number }[];
}

export type WidgetType =
  | "kpis"
  | "storage"
  | "manual_backup"
  | "jobs"
  | "policies"
  | "mail_top"
  | "spam_sources"
  | "blocked_ips";

export interface Widget {
  id: string;
  type: WidgetType;
  size: "full" | "half";
  title?: string | null;
  options: Record<string, unknown>;
}

export interface StandbyAgent {
  id: string;
  name: string;
  hostname: string;
  role: "primary" | "standby";
  status: string;
  last_heartbeat_at: string | null;
}

export interface StandbyOverview {
  config: { enabled: boolean; interval_minutes: number };
  agents: StandbyAgent[];
  accounts: number;
  synced: number;
  partial: number;
  failed: number;
  never: number;
  behind: number;
  pending_jobs: number;
  oldest_waiting_at: string | null;
  lag_seconds: number;
  last_applied_at: string | null;
  problems: {
    email: string;
    status: string;
    error: string | null;
    applied_at: string | null;
  }[];
}
