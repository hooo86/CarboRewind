import { request } from "./client";
import type {
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
  bulkBackup: (accountIds: string[], backupType: BackupType) =>
    request<BulkBackupResult>("POST", "/api/backups/bulk", {
      account_ids: accountIds,
      backup_type: backupType,
    }),
  account: (id: string) => request<AccountDetail>("GET", `/api/accounts/${id}`),
  snapshots: (id: string) =>
    request<Page<Snapshot>>("GET", `/api/accounts/${id}/snapshots`, undefined, {
      limit: 100,
    }),
  jobs: (
    filters: { status?: JobStatus; account_id?: string; offset?: number } = {},
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
  testNotification: (to?: string) =>
    request<{ ok: boolean; error: string | null }>(
      "POST",
      "/api/settings/notifications/test",
      to ? { to } : {},
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
  legalHolds: () => request<LegalHold[]>("GET", "/api/archives/legal-holds"),
  startLegalHold: (accountId: string, note: string) =>
    request<Job>("POST", `/api/archives/accounts/${accountId}/legal-hold`, {
      note,
    }),
  releaseLegalHold: (accountId: string) =>
    request<undefined>(
      "DELETE",
      `/api/archives/accounts/${accountId}/legal-hold`,
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
