import { useTranslation } from "react-i18next";

import { formatDate, formatRelative } from "../utils/format";

const DAY = 86_400_000;

/** Password expiry of a mailbox: red once expired, orange within a week. */
export function PasswordExpiry({
  at,
  mustChange = false,
}: {
  at: string | null;
  mustChange?: boolean;
}) {
  const { t } = useTranslation();
  const left = at ? new Date(at).getTime() - Date.now() : null;
  return (
    <span title={at ? formatDate(at) : undefined}>
      {left === null ? (
        <span className="muted">{t("password.never")}</span>
      ) : left <= 0 ? (
        <span className="bad-text">
          {t("password.expired", { when: formatRelative(at) })}
        </span>
      ) : (
        <span className={left < 7 * DAY ? "warn-text" : undefined}>
          {formatRelative(at)}
        </span>
      )}
      {mustChange && (
        <span className="muted small"> · {t("password.mustChangeShort")}</span>
      )}
    </span>
  );
}
