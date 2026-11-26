import { useEffect, useId, useState } from "react";

import { api } from "../api/endpoints";
import type { AccountItem } from "../api/types";

/** E-mail field suggesting the matching mailboxes (address, name or alias) as one types. */
export function AccountPicker({
  label,
  value,
  onChange,
  required = false,
}: {
  label: string;
  value: string;
  onChange: (email: string) => void;
  required?: boolean;
}) {
  const id = useId();
  const [matches, setMatches] = useState<AccountItem[]>([]);

  useEffect(() => {
    const q = value.trim();
    if (q.length < 2) {
      setMatches([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      api
        .accountList({
          q,
          filters: [],
          sort: "email",
          order: "asc",
          limit: 15,
          offset: 0,
        })
        .then((page) => !cancelled && setMatches(page.items))
        .catch(() => !cancelled && setMatches([]));
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [value]);

  return (
    <label className="grow">
      {label}
      <input
        type="email"
        list={`${id}-accounts`}
        autoComplete="off"
        required={required}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <datalist id={`${id}-accounts`}>
        {matches.map((a) => (
          <option key={a.id} value={a.email}>
            {a.display_name ?? a.email}
          </option>
        ))}
      </datalist>
    </label>
  );
}
