const API_BASE = "http://127.0.0.1:8765";

export type ApiStatus = {
  ok: boolean;
  database_path: string;
  database_exists: boolean;
  storage_usage_bytes: number;
  storage_breakdown: {
    sqlite_bytes: number;
    vector_bytes: number;
    log_bytes: number;
    other_bytes: number;
  };
  storage_warning_bytes: number;
  storage_hard_stop_bytes: number;
  storage_state: "ok" | "warning" | "hard_stop";
  latest_job: ScanSummary | null;
  background_scan: ScanSummary;
  auto_scan: AutoScanStatus;
};

export type AutoScanStatus = {
  enabled: boolean;
  interval_seconds: number;
  idle_seconds: number;
  require_charging: boolean;
  roots: string[];
  check_interval_seconds: number;
  last_checked_at: string;
  last_triggered_at: string;
  next_due_at: string;
  last_skip_reason: string;
  power_idle: {
    is_charging: boolean;
    idle_seconds: number;
    source: string;
  } | null;
};

export type SearchResult = {
  chunk_id: number;
  source_path: string;
  source_kind: string;
  title: string;
  snippet: string;
  score: number;
  rank_source: string;
  metadata: Record<string, string>;
};

export type SearchPayload = {
  ok: boolean;
  results: SearchResult[];
};

export type SearchFilters = {
  drive?: string;
  extension?: string;
  source_kind?: string;
  path_contains?: string;
  project?: string;
  file_type?: string;
  date_from?: string;
  date_to?: string;
};

export type SavedSearch = {
  id: number;
  name: string;
  query: string;
  filters: SearchFilters;
};

export type ChunkContext = {
  chunk_id: number;
  source_path: string;
  source_kind: string;
  title: string;
  text: string;
  metadata: Record<string, string>;
  ordinal: number;
  content_hash: string;
};

export type ChunkContextPayload = {
  ok: boolean;
  chunk: ChunkContext;
  neighbors: ChunkContext[];
};

export type ScanSummary = {
  status: string;
  discovered_count: number;
  indexed_count: number;
  skipped_count: number;
  error_count: number;
  job_id: number | null;
};

export async function fetchStatus(): Promise<ApiStatus> {
  const response = await fetch(`${API_BASE}/status`);
  if (!response.ok) {
    throw new Error(`status failed: ${response.status}`);
  }
  return response.json();
}

export async function searchDocVec(
  query: string,
  limit = 30,
  filters: SearchFilters = {},
): Promise<SearchPayload> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  Object.entries(filters).forEach(([key, value]) => {
    if (value?.trim()) {
      params.set(key, value.trim());
    }
  });
  const response = await fetch(`${API_BASE}/search?${params}`);
  if (!response.ok) {
    throw new Error(`search failed: ${response.status}`);
  }
  return response.json();
}

export async function fetchContext(chunkId: number): Promise<ChunkContextPayload> {
  const params = new URLSearchParams({ id: String(chunkId) });
  const response = await fetch(`${API_BASE}/context?${params}`);
  if (!response.ok) {
    throw new Error(`context failed: ${response.status}`);
  }
  return response.json();
}

export async function fetchSavedSearches(): Promise<SavedSearch[]> {
  const response = await fetch(`${API_BASE}/saved-searches`);
  if (!response.ok) {
    throw new Error(`saved searches failed: ${response.status}`);
  }
  const payload = await response.json();
  return payload.saved_searches;
}

export async function saveSavedSearch(
  name: string,
  query: string,
  filters: SearchFilters,
): Promise<SavedSearch> {
  const response = await fetch(`${API_BASE}/saved-searches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, query, filters }),
  });
  if (!response.ok) {
    throw new Error(`save search failed: ${response.status}`);
  }
  const payload = await response.json();
  return payload.saved_search;
}

export async function scanRoots(
  roots: string[],
  background = false,
): Promise<ScanSummary> {
  const response = await fetch(`${API_BASE}/scan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ roots, background }),
  });
  if (!response.ok) {
    throw new Error(`scan failed: ${response.status}`);
  }
  const payload = await response.json();
  return payload.summary;
}

export async function pauseScan(): Promise<void> {
  const response = await fetch(`${API_BASE}/pause`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`pause failed: ${response.status}`);
  }
}

export async function resumeScan(): Promise<void> {
  const response = await fetch(`${API_BASE}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    throw new Error(`resume failed: ${response.status}`);
  }
}

export async function openDocVecPath(
  path: string,
  mode: "file" | "folder" = "file",
): Promise<void> {
  const response = await fetch(`${API_BASE}/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, mode }),
  });
  if (!response.ok) {
    throw new Error(`open failed: ${response.status}`);
  }
}
