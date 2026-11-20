import { Icon } from "./Icon";
import type { IconName } from "./Icon";

/** A small button showing only an icon; its full label is the tooltip and accessible name. */
export function IconButton({
  icon,
  label,
  title,
  onClick,
  tone = "secondary",
  pressed,
  disabled,
}: {
  icon: IconName;
  /** Short visible-on-hover label (e.g. "Edit"). */
  label: string;
  /** Full description, used as accessible name (e.g. "Edit policy Nightly"). */
  title: string;
  onClick: () => void;
  tone?: "primary" | "secondary" | "danger";
  pressed?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      className={`small icon-btn${tone === "primary" ? "" : " secondary"}${tone === "danger" ? " danger" : ""}`}
      title={`${label} — ${title}`}
      aria-label={title}
      aria-pressed={pressed}
      disabled={disabled}
      onClick={onClick}
    >
      <Icon name={icon} size={16} />
    </button>
  );
}
