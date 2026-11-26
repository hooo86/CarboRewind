import { request } from "./client";
import type {
  IpAction,
  LoginFilter,
  LoginSecurity,
  SpamSources,
  Blocklist,
  Widget,
  AccountGroup,
  PolicyRun,
  SavedView,
  ArchivedMailbox,
  RestoreTests,
  RestoreTestSettings,
  TelegramSettings,
  TelegramSettingsInput,
  BlacklistSettings,
  BlacklistStatus,
  MailQueueActionInput,
  MailQueueAgent,
  AgentTuning,
  AgentTuningInput,
  ArchivedMessage,
  ArchiveMode,
  ArchivePolicy,
  ArchivePolicyInput,
  ArchiveRun,
  ArchiveSearch,
  ArchiveSummary,
  LegalHold,
  OpenedMessage,
  AccountDetail,
  QuotaResult,
  StandbyOverview,
  AccountFacets,
  AccountItem,
  BulkBackupResult,
  AccountRow,
  BackupType,
  Dashboard,
  Domain,
  Health,
  ImportReport,
  Job,
  JobStatus,
  MailLimits,
  MonitoringOverview,
  MonitoringSettings,
  MailOverview,
  MailPeriod,
  MigrationBatch,
  MigrationBatchDetail,
  MigrationMailbox,
  MigrationOptions,
  MigrationRowInput,
  MigrationSource,
  MigrationSourceInput,
  NotificationSettings,
  NotificationSettingsInput,
  OutboxEntry,
  Page,
  Policy,
  PolicyInput,
  RestoreRequest,
  Role,
  SessionInfo,
  Snapshot,
  SnapshotFolder,
  SnapshotItem,
  StartResult,
  StorageSettings,
  StorageSettingsInput,
  TotpSetup,
  TwoFactorPolicy,
  User,
} from "./types";

export const api = {
  health: () => request<Health>("GET", "/healthz"),
  login: (username: string, password: string, otp?: string) =>
    request<SessionInfo>(
      "POST",
      "/api/auth/login",
      otp ? { username, password, otp } : { username, password },
    ),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<undefined>("POST", "/api/auth/password", {
      current_password: currentPassword,
      new_password: newPassword,
    }),
  totpSetup: () => request<TotpSetup>("POST", "/api/auth/2fa/setup"),
  totpEnable: (code: string) =>
    request<{ recovery_codes: string[] }>("POST", "/api/auth/2fa/enable", {
      code,
    }),
  totpDisable: (code: string) =>
    request<undefined>("POST", "/api/auth/2fa/disable", { code }),
  totpRecoveryCodes: (code: string) =>
    request<{ recovery_codes: string[] }>(
      "POST",
      "/api/auth/2fa/recovery-codes",
      { code },
    ),
  users: () =>
    request<Page<User>>("GET", "/api/users", undefined, { limit: 200 }),
  createUser: (body: {
    username: string;
    auth_source: "local" | "ldap";
    password?: string | null;
    display_name?: string | null;
    email?: string | null;
  }) => request<User>("POST", "/api/users", body),
  updateUser: (
    id: string,
    body: {
      is_active?: boolean;
      display_name?: string | null;
      email?: string | null;
      unlock?: boolean;
      password?: string;
      reset_2fa?: boolean;
    },
  ) => request<User>("PATCH", `/api/users/${id}`, body),
  deleteUser: (id: string) => request<undefined>("DELETE", `/api/users/${id}`),
  grantRole: (id: string, role: Role, domainId: string | null) =>
    request<User>("POST", `/api/users/${id}/roles`, {
      role,
      domain_id: domainId,
    }),
  revokeRole: (id: string, assignmentId: string) =>
    request<User>("DELETE", `/api/users/${id}/roles/${assignmentId}`),
  securityPolicy: () =>
    request<{ require_2fa: TwoFactorPolicy }>("GET", "/api/settings/security"),
  saveSecurityPolicy: (requireTwoFactor: TwoFactorPolicy) =>
    request<{ require_2fa: TwoFactorPolicy }>("PUT", "/api/settings/security", {
      require_2fa: requireTwoFactor,
    }),
  logout: () => request<undefined>("POST", "/api/auth/logout"),
  me: () => request<User>("GET", "/api/auth/me"),
  dashboard: () => request<Dashboard>("GET", "/api/dashboard"),
  domains: () =>
    request<Page<Domain>>("GET", "/api/domains", undefined, { limit: 200 }),
  accounts: (domainId: string, q?: string, offset = 0) =>
    request<Page<AccountRow>>(
      "GET",
      `/api/domains/${domainId}/accounts`,
      undefined,
      {
        q,
        limit: 50,
        offset,
      },
    ),
  accountList: (params: {
    q?: string;
    filters: string[];
    sort: string;
    order: "asc" | "desc";
    limit: number;
    offset: number;
  }) =>
    request<Page<AccountItem>>("GET", "/api/accounts", undefined, {
      q: params.q,
      f: params.filters,
      sort: params.sort,
      order: params.order,
      limit: params.limit,
      offset: params.offset,
    }),
  accountFacets: () => request<AccountFacets>("GET", "/api/accounts/facets"),
  accountGroups: (params: { groupBy: string; q?: string; filters: string[] }) =>
    request<AccountGroup[]>("GET", "/api/accounts/groups", undefined, {
      group_by: params.groupBy,
      q: params.q,
      f: params.filters,
    }),
  views: (page: string) =>
    request<SavedView[]>("GET", "/api/views", undefined, { page }),
  saveView: (body: {
    page: string;
    name: string;
    query: string;
    shared: boolean;
  }) => request<SavedView>("POST", "/api/views", body),
  deleteView: (id: string) => request<undefined>("DELETE", `/api/views/${id}`),
  policyRuns: (id: string, offset = 0) =>
    request<Page<PolicyRun>>("GET", `/api/policies/${id}/runs`, undefined, {
      limit: 20,
      offset,
    }),
  bulkBackup: (accountIds: string[], backupType: BackupType) =>
    request<BulkBackupResult>("POST", "/api/backups/bulk", {
      account_ids: accountIds,
      backup_type: backupType,
    }),
  standby: () => request<StandbyOverview>("GET", "/api/standby"),
  saveStandby: (enabled: boolean, intervalMinutes: number) =>
    request<{ enabled: boolean; interval_minutes: number }>(
      "PUT",
      "/api/standby/settings",
      { enabled, interval_minutes: intervalMinutes },
    ),
  setAgentRole: (agentId: string, role: "primary" | "standby") =>
    request<{ id: string; role: string }>(
      "PUT",
      `/api/standby/agents/${agentId}/role`,
      { role },
    ),
  revokeAgent: (agentId: string) =>
    request<unknown>("POST", `/api/agents/${agentId}/revoke`),
  standbySyncNow: () =>
    request<{ queued: number }>("POST", "/api/standby/sync-now"),
  promoteStandby: (agentId: string, confirm: string) =>
    request<{ promoted: string; demoted: string[] }>(
      "POST",
      "/api/standby/promote",
      { agent_id: agentId, confirm },
    ),
  setQuota: (accountIds: string[], quotaBytes: number, reason?: string) =>
    request<QuotaResult>("POST", "/api/mail/accounts/quota", {
      account_ids: accountIds,
      quota_bytes: quotaBytes,
      reason: reason || null,
    }),
  account: (id: string) => request<AccountDetail>("GET", `/api/accounts/${id}`),
  snapshots: (id: string) =>
    request<Page<Snapshot>>("GET", `/api/accounts/${id}/snapshots`, undefined, {
      limit: 100,
    }),
  jobs: (
    filters: {
      status?: JobStatus;
      account_id?: string;
      kind?: string;
      q?: string;
      sort?: string;
      order?: "asc" | "desc";
      limit?: number;
      offset?: number;
    } = {},
  ) =>
    request<Page<Job>>("GET", "/api/jobs", undefined, {
      limit: 50,
      ...filters,
    }),
  job: (id: string) => request<Job>("GET", `/api/jobs/${id}`),
  snapshotFolders: (accountId: string, snapshotId: string) =>
    request<SnapshotFolder[]>(
      "GET",
      `/api/accounts/${accountId}/snapshots/${snapshotId}/folders`,
    ),
  snapshotItems: (
    accountId: string,
    snapshotId: string,
    filters: { folder_id?: number; q?: string; offset?: number } = {},
  ) =>
    request<Page<SnapshotItem>>(
      "GET",
      `/api/accounts/${accountId}/snapshots/${snapshotId}/items`,
      undefined,
      { limit: 50, ...filters },
    ),
  startRestore: (body: RestoreRequest) =>
    request<Job>("POST", "/api/restores", body),
  policies: () => request<Policy[]>("GET", "/api/policies"),
  createPolicy: (body: PolicyInput) =>
    request<Policy>("POST", "/api/policies", body),
  updatePolicy: (id: string, body: Partial<PolicyInput>) =>
    request<Policy>("PATCH", `/api/policies/${id}`, body),
  deletePolicy: (id: string) =>
    request<undefined>("DELETE", `/api/policies/${id}`),
  notificationSettings: () =>
    request<NotificationSettings>("GET", "/api/settings/notifications"),
  saveNotificationSettings: (body: NotificationSettingsInput) =>
    request<NotificationSettings>("PUT", "/api/settings/notifications", body),
  testNotification: (to?: string, server: "main" | "fallback" = "main") =>
    request<{ ok: boolean; error: string | null }>(
      "POST",
      "/api/settings/notifications/test",
      to ? { to, server } : { server },
    ),
  notificationOutbox: () =>
    request<OutboxEntry[]>("GET", "/api/settings/notifications/outbox"),
  migrationSources: () =>
    request<MigrationSource[]>("GET", "/api/migrations/sources"),
  createMigrationSource: (body: MigrationSourceInput) =>
    request<MigrationSource>("POST", "/api/migrations/sources", body),
  updateMigrationSource: (id: string, body: MigrationSourceInput) =>
    request<MigrationSource>("PUT", `/api/migrations/sources/${id}`, body),
  deleteMigrationSource: (id: string) =>
    request<undefined>("DELETE", `/api/migrations/sources/${id}`),
  migrationBatches: () =>
    request<MigrationBatch[]>("GET", "/api/migrations/batches"),
  migrationBatch: (id: string) =>
    request<MigrationBatchDetail>("GET", `/api/migrations/batches/${id}`),
  saveMigrationBatch: (
    id: string | null,
    body: { name: string; source_id: string; options: MigrationOptions },
  ) =>
    id
      ? request<MigrationBatch>("PUT", `/api/migrations/batches/${id}`, body)
      : request<MigrationBatch>("POST", "/api/migrations/batches", body),
  deleteMigrationBatch: (id: string) =>
    request<undefined>("DELETE", `/api/migrations/batches/${id}`),
  importMigrationFile: (id: string, file: Blob, targetDomain?: string) =>
    request<ImportReport>(
      "POST",
      `/api/migrations/batches/${id}/import`,
      file,
      { target_domain: targetDomain },
    ),
  addMigrationMailboxes: (
    id: string,
    rows: MigrationRowInput[],
    targetDomain?: string,
  ) =>
    request<ImportReport>("POST", `/api/migrations/batches/${id}/mailboxes`, {
      rows,
      target_domain: targetDomain || null,
    }),
  rematchMigration: (id: string, targetDomain?: string, all = false) =>
    request<ImportReport>("POST", `/api/migrations/batches/${id}/match`, {
      target_domain: targetDomain || null,
      all,
    }),
  updateMigrationMailbox: (
    id: string,
    body: {
      target_email?: string | null;
      clear_target?: boolean;
      source_login?: string | null;
      password?: string | null;
      root?: string | null;
    },
  ) =>
    request<MigrationMailbox>("PATCH", `/api/migrations/mailboxes/${id}`, body),
  deleteMigrationMailbox: (id: string) =>
    request<undefined>("DELETE", `/api/migrations/mailboxes/${id}`),
  startMigration: (id: string, mailboxIds: string[] | null, dryRun: boolean) =>
    request<StartResult>("POST", `/api/migrations/batches/${id}/start`, {
      mailbox_ids: mailboxIds,
      dry_run: dryRun,
    }),
  cancelMigration: (id: string) =>
    request<{ cancelled: number }>(
      "POST",
      `/api/migrations/batches/${id}/cancel`,
    ),
  forgetMigrationPasswords: (id: string) =>
    request<undefined>(
      "POST",
      `/api/migrations/batches/${id}/forget-passwords`,
    ),
  archivePolicies: () =>
    request<ArchivePolicy[]>("GET", "/api/archives/policies"),
  createArchivePolicy: (body: ArchivePolicyInput) =>
    request<ArchivePolicy>("POST", "/api/archives/policies", body),
  updateArchivePolicy: (id: string, body: Partial<ArchivePolicyInput>) =>
    request<ArchivePolicy>("PATCH", `/api/archives/policies/${id}`, body),
  deleteArchivePolicy: (id: string) =>
    request<undefined>("DELETE", `/api/archives/policies/${id}`),
  runArchivePolicy: (id: string, simulate: boolean) =>
    request<{ run: string; mode: ArchiveMode; jobs: number }>(
      "POST",
      `/api/archives/policies/${id}/run`,
      { simulate },
    ),
  archiveRuns: (policyId?: string, limit = 50) =>
    request<Page<ArchiveRun>>("GET", "/api/archives/runs", undefined, {
      policy_id: policyId,
      limit,
    }),
  archiveSummary: () => request<ArchiveSummary>("GET", "/api/archives/summary"),
  archivedMessages: (search: ArchiveSearch) =>
    request<Page<ArchivedMessage>>("GET", "/api/archives/messages", undefined, {
      ...search,
    }),
  openArchivedMessage: (id: number) =>
    request<{ job_id: string }>("POST", `/api/archives/messages/${id}/open`),
  openedMessage: (jobId: string) =>
    request<OpenedMessage>("GET", `/api/archives/open/${jobId}`),
  emlUrl: (jobId: string) => `/api/archives/open/${jobId}/eml`,
  restoreArchived: (
    ids: number[],
    mode: "new_folder" | "original",
    targetAccountId?: string,
  ) =>
    request<Job[]>("POST", "/api/archives/restore", {
      ids,
      mode,
      target_account_id: targetAccountId,
    }),
  archivedMailboxes: (q: string, offset = 0) =>
    request<Page<ArchivedMailbox>>(
      "GET",
      "/api/archives/mailboxes",
      undefined,
      {
        q: q || undefined,
        offset,
        limit: 50,
      },
    ),
  restoreArchivedMailbox: (
    accountId: string,
    body: {
      mode: "original" | "new_folder";
      only_removed: boolean;
      target_account_id?: string;
    },
  ) =>
    request<Job[]>("POST", `/api/archives/accounts/${accountId}/restore`, body),
  legalHolds: () => request<LegalHold[]>("GET", "/api/archives/legal-holds"),
  startLegalHold: (
    accountId: string,
    note: string,
    options: {
      remove_from_mailbox?: boolean;
      older_than_days?: number;
      confirm?: string;
    } = {},
  ) =>
    request<Job>("POST", `/api/archives/accounts/${accountId}/legal-hold`, {
      note,
      ...options,
    }),
  deleteMailbox: (accountId: string, confirm: string, reason?: string) =>
    request<{ id: string | null; action: string; queued: boolean }>(
      "POST",
      `/api/mail/accounts/${accountId}/delete`,
      { confirm, reason: reason || null },
    ),
  releaseLegalHold: (accountId: string) =>
    request<undefined>(
      "DELETE",
      `/api/archives/accounts/${accountId}/legal-hold`,
    ),
  mailLimits: () => request<MailLimits>("GET", "/api/mail/limits"),
  agentTuning: () => request<AgentTuning>("GET", "/api/settings/agent"),
  saveAgentTuning: (body: AgentTuningInput) =>
    request<AgentTuning>("PUT", "/api/settings/agent", body),
  blacklist: () => request<BlacklistStatus>("GET", "/api/monitoring/blacklist"),
  saveBlacklist: (body: BlacklistSettings) =>
    request<BlacklistStatus>("PUT", "/api/monitoring/blacklist", body),
  checkBlacklist: () =>
    request<BlacklistStatus>("POST", "/api/monitoring/blacklist/check"),
  mailQueue: () => request<MailQueueAgent[]>("GET", "/api/monitoring/queue"),
  mailTest: (agent_id: string) =>
    request<{ id: string }>("POST", "/api/monitoring/mail-test", { agent_id }),
  certRenew: (agent_id: string) =>
    request<{ id: string }>("POST", "/api/monitoring/cert-renew", { agent_id }),
  pauseAutostart: (minutes: number) =>
    request<{ paused_until: string | null }>(
      "POST",
      "/api/monitoring/autostart/pause",
      { minutes },
    ),
  mailQueueAction: (body: MailQueueActionInput) =>
    request<{ id: string }>("POST", "/api/monitoring/queue/actions", body),
  telegram: () => request<TelegramSettings>("GET", "/api/settings/telegram"),
  saveTelegram: (body: TelegramSettingsInput) =>
    request<TelegramSettings>("PUT", "/api/settings/telegram", body),
  testTelegram: () =>
    request<{ ok: boolean; error: string | null }>(
      "POST",
      "/api/settings/telegram/test",
    ),
  telegramChats: () =>
    request<{ id: string; title: string; type: string }[]>(
      "GET",
      "/api/settings/telegram/chats",
    ),
  restoreTests: () =>
    request<RestoreTests>("GET", "/api/monitoring/restore-tests"),
  saveRestoreTests: (body: RestoreTestSettings) =>
    request<RestoreTests>("PUT", "/api/monitoring/restore-tests", body),
  runRestoreTest: () =>
    request<RestoreTests>("POST", "/api/monitoring/restore-tests/run"),
  serverLog: (process: "api" | "scheduler", lines: number) =>
    request<{
      process: string;
      available: boolean;
      lines: Record<string, unknown>[];
    }>("GET", "/api/logs/server", undefined, { process, lines }),
  askAgentLog: (source: "agent" | "mail" | "mailbox", lines: number) =>
    request<{ job_id: string }>("POST", "/api/logs/agent", { source, lines }),
  agentLog: (jobId: string) =>
    request<{ status: JobStatus; error: string | null; text: string | null }>(
      "GET",
      `/api/logs/agent/${jobId}`,
    ),
  monitoring: () =>
    request<MonitoringOverview>("GET", "/api/monitoring/overview"),
  saveMonitoringSettings: (body: MonitoringSettings) =>
    request<MonitoringSettings>("PUT", "/api/monitoring/settings", body),
  verifyNow: (target: "primary" | "replica", sample: number) =>
    request<{ queued: boolean; job_id: string | null }>(
      "POST",
      "/api/monitoring/verify",
      { target, sample },
    ),
  mailSecurity: (period: MailPeriod, onlyBlocked = false, limit = 100) =>
    request<LoginSecurity>("GET", "/api/mail/security", undefined, {
      period,
      only_blocked: onlyBlocked ? "true" : undefined,
      limit,
    }),
  loginFilter: () => request<LoginFilter>("GET", "/api/mail/login-filter"),
  ipAction: (action: "trust" | "untrust", ip: string) =>
    request<IpAction>("POST", `/api/mail/login-filter/${action}`, { ip }),
  blocklist: () => request<Blocklist>("GET", "/api/mail/blocklist"),
  blockSender: (value: string, reason?: string, force = false) =>
    request<Blocklist>("POST", "/api/mail/blocklist", {
      value,
      reason: reason || null,
      force,
    }),
  unblockSender: (value: string) =>
    request<Blocklist>("POST", "/api/mail/blocklist/remove", { value }),
  trustlist: () => request<Blocklist>("GET", "/api/mail/trustlist"),
  trustSender: (value: string, reason?: string, force = false) =>
    request<Blocklist>("POST", "/api/mail/trustlist", {
      value,
      reason: reason || null,
      force,
    }),
  untrustSender: (value: string) =>
    request<Blocklist>("POST", "/api/mail/trustlist/remove", { value }),
  spamSources: (period: MailPeriod) =>
    request<SpamSources>("GET", "/api/mail/spam-sources", undefined, {
      period,
    }),
  dashboardLayout: () =>
    request<{ widgets: Widget[] | null }>("GET", "/api/me/dashboard"),
  saveDashboardLayout: (widgets: Widget[] | null) =>
    request<{ widgets: Widget[] | null }>("PUT", "/api/me/dashboard", {
      widgets,
    }),
  mailOverview: (period: MailPeriod) =>
    request<MailOverview>("GET", "/api/mail/overview", undefined, { period }),
  saveMailLimits: (body: MailLimits) =>
    request<MailLimits>("PUT", "/api/mail/limits", body),
  ackMailAlert: (id: string) =>
    request<undefined>("POST", `/api/mail/alerts/${id}/ack`),
  accountLock: (
    accountId: string,
    action: "lock" | "unlock",
    reason?: string,
  ) =>
    request<{ id: string | null; action: string; queued: boolean }>(
      "POST",
      `/api/mail/accounts/${accountId}/${action}`,
      { reason: reason ?? null },
    ),
  storageSettings: () =>
    request<StorageSettings>("GET", "/api/settings/storage"),
  saveStorageSettings: (body: StorageSettingsInput) =>
    request<StorageSettings>("PUT", "/api/settings/storage", body),
  replicateNow: () =>
    request<{ job_id: string }>("POST", "/api/settings/storage/replicate"),
  backupAll: (backupType: BackupType, domainId?: string) =>
    request<BulkBackupResult>("POST", "/api/backups/all", {
      backup_type: backupType,
      domain_id: domainId || null,
    }),
  runPolicy: (id: string) =>
    request<BulkBackupResult>("POST", `/api/policies/${id}/run`, {}),
  startBackup: (accountId: string, backupType: BackupType) =>
    request<Job>("POST", "/api/backups", {
      account_id: accountId,
      backup_type: backupType,
    }),
};
