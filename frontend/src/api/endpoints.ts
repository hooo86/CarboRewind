import { request } from "./client";
import type {
  AccountDetail,
  AccountFacets,
  AccountItem,
  BulkBackupResult,
  AccountRow,
  BackupType,
  Dashboard,
  Domain,
  Health,
  Job,
  JobStatus,
  NotificationSettings,
  NotificationSettingsInput,
  OutboxEntry,
  Page,
  Policy,
  PolicyInput,
  RestoreRequest,
  SessionInfo,
  Snapshot,
  SnapshotFolder,
  SnapshotItem,
  User,
} from "./types";

export const api = {
  health: () => request<Health>("GET", "/healthz"),
  login: (username: string, password: string) =>
    request<SessionInfo>("POST", "/api/auth/login", { username, password }),
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
  startBackup: (accountId: string, backupType: BackupType) =>
    request<Job>("POST", "/api/backups", {
      account_id: accountId,
      backup_type: backupType,
    }),
};
