export interface Health {
  status: string;
  version: string;
}

/** Fetch the API liveness endpoint. Throws on network or HTTP error. */
export async function fetchHealth(signal?: AbortSignal): Promise<Health> {
  const response = await fetch("/healthz", { signal, credentials: "same-origin" });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return (await response.json()) as Health;
}
