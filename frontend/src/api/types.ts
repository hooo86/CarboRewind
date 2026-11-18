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
