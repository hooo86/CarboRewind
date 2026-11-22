import { useMemo } from "react";
import { useTranslation } from "react-i18next";

/** Used when the browser cannot list the zones (very old browsers). */
const FALLBACK = [
  "UTC",
  "Africa/Algiers",
  "Africa/Casablanca",
  "Africa/Tunis",
  "Africa/Cairo",
  "Europe/Paris",
  "Europe/London",
  "Europe/Berlin",
  "America/New_York",
  "Asia/Dubai",
];
/** Shown first: the zones people here pick. */
const COMMON = ["Africa/Algiers", "UTC", "Europe/Paris"];

function allZones(): string[] {
  const intl = Intl as unknown as {
    supportedValuesOf?: (key: "timeZone") => string[];
  };
  try {
    const zones = intl.supportedValuesOf?.("timeZone") ?? FALLBACK;
    return zones.includes("UTC") ? zones : ["UTC", ...zones];
  } catch {
    return FALLBACK;
  }
}

/** A list of the IANA time zones, grouped by region, instead of a text field. */
export function TimezoneSelect({
  value,
  onChange,
  required,
}: {
  value: string;
  onChange: (zone: string) => void;
  required?: boolean;
}) {
  const { t } = useTranslation();
  const groups = useMemo(() => {
    const zones = allZones();
    const known = new Set(zones);
    const byRegion = new Map<string, string[]>();
    for (const zone of zones) {
      if (zone === "UTC") continue;
      const region = zone.split("/")[0] ?? "Etc";
      byRegion.set(region, [...(byRegion.get(region) ?? []), zone]);
    }
    return { known, byRegion: [...byRegion.entries()].sort() };
  }, []);
  const current = value && !groups.known.has(value) ? [value] : [];
  return (
    <select
      value={value}
      required={required}
      onChange={(e) => onChange(e.target.value)}
    >
      {!value && <option value="">{t("timezone.choose")}</option>}
      {current.map((zone) => (
        <option key={zone} value={zone}>
          {zone}
        </option>
      ))}
      <optgroup label={t("timezone.common")}>
        {COMMON.filter((z) => groups.known.has(z)).map((zone) => (
          <option key={`c-${zone}`} value={zone}>
            {zone.replace(/_/g, " ")}
          </option>
        ))}
      </optgroup>
      {groups.byRegion.map(([region, zones]) => (
        <optgroup key={region} label={region}>
          {zones.map((zone) => (
            <option key={zone} value={zone}>
              {zone.replace(/_/g, " ")}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}
