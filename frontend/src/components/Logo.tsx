import { useId } from "react";

/** CarboRewind mark: a rewind arrow around an envelope (also public/favicon.svg). */
export function Logo({
  size = 32,
  withName = false,
}: {
  size?: number;
  withName?: boolean;
}) {
  const gradient = `cr-logo-${useId().replace(/:/g, "")}`;
  return (
    <span className="logo">
      <svg
        width={size}
        height={size}
        viewBox="0 0 64 64"
        aria-hidden={withName ? true : undefined}
        role={withName ? undefined : "img"}
        aria-label={withName ? undefined : "CarboRewind"}
        focusable="false"
      >
        <defs>
          <linearGradient id={gradient} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#1d4ed8" />
            <stop offset="1" stopColor="#0891b2" />
          </linearGradient>
        </defs>
        <rect width="64" height="64" rx="14" fill={`url(#${gradient})`} />
        <path
          d="M14.8 40 A19 19 0 1 0 14.8 24"
          fill="none"
          stroke="#fff"
          strokeWidth="4"
          strokeLinecap="round"
        />
        <path d="M12.7 28.5 L19.2 23.8 L12 20.5 Z" fill="#fff" />
        <rect
          x="22"
          y="25"
          width="20"
          height="14"
          rx="2.5"
          fill="none"
          stroke="#fff"
          strokeWidth="2.6"
        />
        <path
          d="M22.8 26.3 L32 33 L41.2 26.3"
          fill="none"
          stroke="#fff"
          strokeWidth="2.6"
          strokeLinejoin="round"
        />
      </svg>
      {withName && (
        <span className="logo-name">
          Carbo<span className="logo-accent">Rewind</span>
        </span>
      )}
    </span>
  );
}
