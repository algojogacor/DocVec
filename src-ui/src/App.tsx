import {
  ArrowUpDown,
  BatteryCharging,
  Bot,
  ChevronRight,
  Clock,
  Copy,
  Database,
  ExternalLink,
  FileText,
  Folder,
  FolderOpen,
  HardDrive,
  Home,
  LayoutGrid,
  List,
  Pause,
  RefreshCw,
  Rows3,
  Search,
  Sparkles,
  Terminal,
  Trash2,
  X,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  ApiStatus,
  SavedSearch,
  ScanSummary,
  SearchResult,
  SearchFilters,
  clearLogs,
  fetchContext,
  fetchLogs,
  fetchSavedSearches,
  fetchStatus,
  openDocVecPath,
  pauseScan,
  resumeScan,
  scanRoots,
  searchDocVec,
  saveSavedSearch,
} from "./api";

const scanProfiles = {
  focused: {
    label: "Focused",
    roots: [
      "D:\\hermes\\brain",
      "D:\\hermes\\gbrain",
      "D:\\hermes\\projects",
      "D:\\hermes\\.hermes\\state.db",
      "D:\\hermes\\.hermes\\memory_store.db",
      "D:\\hermes\\.hermes\\memories",
      "C:\\Users\\Arya Rizky\\.codex\\sessions",
    ],
  },
  full: {
    label: "Full PC",
    roots: ["C:\\", "D:\\", "E:\\"],
  },
} as const;

type SidebarKey = "quick" | "this-pc" | "projects" | "sessions" | "brain" | "index";

const sidebar: Array<{ key: SidebarKey; label: string; icon: typeof Home }> = [
  { key: "quick", label: "Quick Access", icon: Home },
  { key: "this-pc", label: "This PC", icon: HardDrive },
  { key: "projects", label: "Projects", icon: Folder },
  { key: "sessions", label: "AI Sessions", icon: Bot },
  { key: "brain", label: "Hermes Brain", icon: Sparkles },
  { key: "index", label: "DocVec Index", icon: Database },
];

const drives = [
  { name: "OS (C:)", free: "27.5 GB free", fill: 72, drive: "C:" },
  { name: "DATA (D:)", free: "26.9 GB free", fill: 68, drive: "D:" },
  { name: "Dokumen Penting (E:)", free: "14.2 GB free", fill: 58, drive: "E:" },
];

const sourceFilterOptions = [
  { label: "All sources", value: "" },
  { label: "Files", value: "normal_file" },
  { label: "Projects", value: "project_source" },
  { label: "AI sessions", value: "ai_session" },
  { label: "AI memory", value: "ai_memory" },
];

const fileTypeFilterOptions = [
  { label: "Any type", value: "" },
  { label: "Code", value: "code" },
  { label: "Document", value: "document" },
  { label: "Config", value: "config" },
  { label: "Transcript", value: "transcript" },
  { label: "Session", value: "session" },
  { label: "Memory", value: "memory" },
];

type ViewMode = "details" | "list" | "tiles";
type SortKey = "name" | "source" | "type" | "score";
type SortDirection = "asc" | "desc";

function formatBytes(value: number): string {
  if (value < 1024 * 1024) {
    return `${Math.round(value / 1024)} KB`;
  }
  if (value < 1024 ** 3) {
    return `${(value / 1024 ** 2).toFixed(1)} MB`;
  }
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

function sourceLabel(kind: string): string {
  return kind.replace(/_/g, " ");
}

function lineRangeLabel(metadata: Record<string, string>): string | null {
  const start = metadata.start_line;
  const end = metadata.end_line;
  if (!start) {
    return null;
  }
  return end && end !== start ? `Lines ${start}-${end}` : `Line ${start}`;
}

function formatContextText(
  chunk: { text: string; ordinal: number; chunk_id: number },
  neighbors: Array<{ text: string; ordinal: number; chunk_id: number }>,
): string {
  return [...neighbors, chunk]
    .sort((left, right) =>
      left.ordinal === right.ordinal
        ? left.chunk_id - right.chunk_id
        : left.ordinal - right.ordinal,
    )
    .map((item) => item.text)
    .join("\n\n");
}

function resultTypeLabel(result: SearchResult): string {
  const extension = result.metadata.extension || extensionFromPath(result.source_path);
  if (result.source_kind === "ai_session") {
    return "Session";
  }
  if (result.source_kind === "ai_memory") {
    return "Memory";
  }
  return extension ? extension.replace(".", "").toUpperCase() : "File";
}

function extensionFromPath(path: string): string {
  const cleanPath = path.split("#", 1)[0];
  const lastSegment = cleanPath.split(/[\\/]/).pop() ?? cleanPath;
  const dotIndex = lastSegment.lastIndexOf(".");
  return dotIndex >= 0 ? lastSegment.slice(dotIndex).toLowerCase() : "";
}

function compareResults(
  left: SearchResult,
  right: SearchResult,
  sortKey: SortKey,
  direction: SortDirection,
): number {
  const multiplier = direction === "asc" ? 1 : -1;
  if (sortKey === "score") {
    return (left.score - right.score) * multiplier;
  }
  const leftValue =
    sortKey === "name"
      ? left.title
      : sortKey === "source"
        ? sourceLabel(left.source_kind)
        : resultTypeLabel(left);
  const rightValue =
    sortKey === "name"
      ? right.title
      : sortKey === "source"
        ? sourceLabel(right.source_kind)
        : resultTypeLabel(right);
  return leftValue.localeCompare(rightValue, undefined, { sensitivity: "base" }) * multiplier;
}

function storageLabel(status: ApiStatus | null): string {
  if (!status) {
    return "Storage unknown";
  }
  const prefix =
    status.storage_state === "hard_stop"
      ? "Storage stop"
      : status.storage_state === "warning"
        ? "Storage warning"
        : "Storage";
  return `${prefix}: ${formatBytes(status.storage_usage_bytes)}`;
}

function logLevelClass(line: string): string {
  const match = line.match(/\s(DEBUG|INFO|WARNING|ERROR)\s/);
  return match ? `log-entry ${match[1].toLowerCase()}` : "log-entry";
}

function autoScanLabel(status: ApiStatus | null): string {
  const autoScan = status?.auto_scan;
  if (!autoScan) {
    return "Auto: unknown";
  }
  if (!autoScan.enabled) {
    return "Auto: off";
  }
  const intervalHours = autoScan.interval_seconds / 3600;
  const interval =
    Number.isInteger(intervalHours) && intervalHours >= 1
      ? `${intervalHours}h`
      : `${Math.round(autoScan.interval_seconds / 60)}m`;
  const guard = autoScan.require_charging ? "charging/idle" : "idle";
  return `Auto: ${interval} ${guard}`;
}

export function App() {
  const [status, setStatus] = useState<ApiStatus | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [query, setQuery] = useState("DocVec Hermes project notes");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [driveFilter, setDriveFilter] = useState("");
  const [sourceKindFilter, setSourceKindFilter] = useState("");
  const [extensionFilter, setExtensionFilter] = useState("");
  const [pathFilter, setPathFilter] = useState("");
  const [projectFilter, setProjectFilter] = useState("");
  const [fileTypeFilter, setFileTypeFilter] = useState("");
  const [dateFromFilter, setDateFromFilter] = useState("");
  const [dateToFilter, setDateToFilter] = useState("");
  const [scanProfile, setScanProfile] = useState<keyof typeof scanProfiles>("full");
  const [scanSummary, setScanSummary] = useState<ScanSummary | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [isScanning, setIsScanning] = useState(false);
  const [previewText, setPreviewText] = useState<string | null>(null);
  const [previewMessage, setPreviewMessage] = useState<string | null>(null);
  const [savedSearches, setSavedSearches] = useState<SavedSearch[]>([]);
  const [selectedSavedSearchId, setSelectedSavedSearchId] = useState("");
  const [viewMode, setViewMode] = useState<ViewMode>("details");
  const [sortKey, setSortKey] = useState<SortKey>("score");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [logsOpen, setLogsOpen] = useState(false);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [logError, setLogError] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState<SidebarKey>("this-pc");

  const sortedResults = useMemo(
    () => [...results].sort((left, right) => compareResults(left, right, sortKey, sortDirection)),
    [results, sortDirection, sortKey],
  );
  const selected = useMemo(
    () =>
      sortedResults.find((result) => result.chunk_id === selectedId) ??
      sortedResults[0] ??
      null,
    [selectedId, sortedResults],
  );
  const activeRoots = scanProfiles[scanProfile].roots;
  const backgroundJob =
    status?.background_scan && status.background_scan.status !== "idle"
      ? status.background_scan
      : null;
  const visibleJob = backgroundJob ?? scanSummary ?? status?.latest_job ?? null;
  const isPaused = visibleJob?.status === "paused";
  const isScanRunning = visibleJob?.status === "running" || isScanning;
  const activeSearchFilters: SearchFilters = {
    drive: driveFilter,
    source_kind: sourceKindFilter,
    extension: extensionFilter,
    path_contains: pathFilter,
    project: projectFilter,
    file_type: fileTypeFilter,
    date_from: dateFromFilter,
    date_to: dateToFilter,
  };

  async function refreshStatus() {
    try {
      const nextStatus = await fetchStatus();
      setStatus(nextStatus);
      if (nextStatus.background_scan.status === "running") {
        setIsScanning(true);
      } else if (nextStatus.background_scan.status !== "idle") {
        setIsScanning(false);
      }
      setApiError(null);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : "DocVec API offline");
    }
  }

  async function loadSavedSearches() {
    try {
      setSavedSearches(await fetchSavedSearches());
      setApiError(null);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : "Saved searches failed");
    }
  }

  async function loadLogs() {
    try {
      const payload = await fetchLogs(500);
      setLogLines(payload.lines);
      setLogError(null);
    } catch (error) {
      setLogError(error instanceof Error ? error.message : "Logs unavailable");
    }
  }

  async function runSearch(nextQuery = query, filters = activeSearchFilters) {
    if (!nextQuery.trim()) {
      return;
    }
    setIsSearching(true);
    try {
      const payload = await searchDocVec(nextQuery.trim(), 30, filters);
      setResults(payload.results);
      setSelectedId(payload.results[0]?.chunk_id ?? null);
      setApiError(null);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : "Search failed");
    } finally {
      setIsSearching(false);
    }
  }

  async function saveCurrentSearch() {
    const trimmedQuery = query.trim();
    if (!trimmedQuery) {
      return;
    }
    try {
      const saved = await saveSavedSearch(trimmedQuery, trimmedQuery, activeSearchFilters);
      await loadSavedSearches();
      setSelectedSavedSearchId(String(saved.id));
      setPreviewMessage("Search saved");
    } catch (error) {
      setApiError(error instanceof Error ? error.message : "Save search failed");
    }
  }

  function applyFilters(filters: SearchFilters) {
    setDriveFilter(filters.drive ?? "");
    setSourceKindFilter(filters.source_kind ?? "");
    setExtensionFilter(filters.extension ?? "");
    setPathFilter(filters.path_contains ?? "");
    setProjectFilter(filters.project ?? "");
    setFileTypeFilter(filters.file_type ?? "");
    setDateFromFilter(filters.date_from ?? "");
    setDateToFilter(filters.date_to ?? "");
  }

  function clearFilters() {
    applyFilters({});
  }

  function navigateSidebar(section: SidebarKey) {
    setActiveSection(section);
    if (section === "quick") {
      setScanProfile("focused");
      setSourceKindFilter("");
      setPathFilter("hermes");
      setProjectFilter("");
      return;
    }
    if (section === "this-pc") {
      setScanProfile("full");
      clearFilters();
      return;
    }
    if (section === "projects") {
      setScanProfile("focused");
      setSourceKindFilter("project_source");
      setPathFilter("D:\\hermes\\projects");
      return;
    }
    if (section === "sessions") {
      setScanProfile("focused");
      setSourceKindFilter("ai_session");
      setPathFilter("");
      return;
    }
    if (section === "brain") {
      setScanProfile("focused");
      setSourceKindFilter("ai_memory");
      setPathFilter("D:\\hermes\\brain");
      return;
    }
    clearFilters();
  }

  function selectRoot(root: string) {
    setActiveSection("index");
    setPathFilter(root);
    setDriveFilter(root.match(/^[A-Z]:/i)?.[0].toUpperCase() ?? "");
  }

  function selectDrive(drive: string) {
    setActiveSection("this-pc");
    setScanProfile("full");
    setDriveFilter(drive);
    setPathFilter("");
  }

  function applySavedSearch(id: string) {
    setSelectedSavedSearchId(id);
    const saved = savedSearches.find((item) => String(item.id) === id);
    if (!saved) {
      return;
    }
    setQuery(saved.query);
    applyFilters(saved.filters);
    void runSearch(saved.query, saved.filters);
  }

  async function runScan() {
    setIsScanning(true);
    try {
      const summary = await scanRoots([...activeRoots], true);
      setScanSummary(summary);
      setApiError(null);
      await refreshStatus();
    } catch (error) {
      setApiError(error instanceof Error ? error.message : "Scan failed");
      setIsScanning(false);
    }
  }

  useEffect(() => {
    if (visibleJob?.status !== "running") {
      return;
    }
    const timer = window.setInterval(() => {
      void refreshStatus();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [visibleJob?.status]);

  useEffect(() => {
    if (scanSummary?.status === "running" && backgroundJob?.status === "completed") {
      setScanSummary(backgroundJob);
      void runSearch(query);
    }
    if (scanSummary?.status === "running" && backgroundJob?.status === "error") {
      setScanSummary(backgroundJob);
    }
  }, [backgroundJob?.status]);

  async function runPause() {
    try {
      await pauseScan();
      setPreviewMessage("Scan pause requested");
      setScanSummary((current) =>
        current ? { ...current, status: "paused" } : current,
      );
    } catch (error) {
      setApiError(error instanceof Error ? error.message : "Pause failed");
    }
  }

  async function runResume() {
    try {
      await resumeScan();
      setPreviewMessage("Scan resume ready");
      setScanSummary((current) =>
        current ? { ...current, status: "running" } : current,
      );
    } catch (error) {
      setApiError(error instanceof Error ? error.message : "Resume failed");
    }
  }

  async function openSelected(mode: "file" | "folder") {
    if (!selected) {
      return;
    }
    try {
      await openDocVecPath(selected.source_path, mode);
      setPreviewMessage(mode === "folder" ? "Folder opened" : "File opened");
    } catch (error) {
      setPreviewMessage(error instanceof Error ? error.message : "Open failed");
    }
  }

  async function copySelectedPath() {
    if (!selected) {
      return;
    }
    try {
      await navigator.clipboard.writeText(selected.source_path);
      setPreviewMessage("Path copied");
    } catch {
      setPreviewMessage("Copy failed");
    }
  }

  async function copyVisibleLogs() {
    try {
      await navigator.clipboard.writeText(logLines.join("\n"));
      setPreviewMessage("Logs copied");
    } catch {
      setLogError("Copy failed");
    }
  }

  async function clearVisibleLogs() {
    try {
      await clearLogs();
      setLogLines([]);
      setLogError(null);
    } catch (error) {
      setLogError(error instanceof Error ? error.message : "Clear failed");
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void runSearch();
  }

  function setSort(nextKey: SortKey) {
    if (sortKey === nextKey) {
      setSortDirection((current) => (current === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(nextKey);
    setSortDirection(nextKey === "score" ? "desc" : "asc");
  }

  function sortLabel(key: SortKey): string {
    if (sortKey !== key) {
      return "";
    }
    return sortDirection === "asc" ? " up" : " down";
  }

  useEffect(() => {
    void refreshStatus();
    void loadSavedSearches();
  }, []);

  useEffect(() => {
    if (!logsOpen) {
      return;
    }
    void loadLogs();
    const timer = window.setInterval(() => {
      void loadLogs();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [logsOpen]);

  useEffect(() => {
    if (!selected) {
      setPreviewText(null);
      setPreviewMessage(null);
      return;
    }

    let cancelled = false;
    setPreviewText(null);
    setPreviewMessage(null);
    void fetchContext(selected.chunk_id)
      .then((payload) => {
        if (!cancelled) {
          setPreviewText(formatContextText(payload.chunk, payload.neighbors ?? []));
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setPreviewMessage(error instanceof Error ? error.message : "Preview failed");
        }
      });

    return () => {
      cancelled = true;
    };
  }, [selected]);

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">D</div>
          <div>
            <strong>DocVec</strong>
            <span>Local semantic search</span>
          </div>
        </div>
        <nav className="nav-list">
          {sidebar.map((item) => (
            <button
              className={activeSection === item.key ? "nav-item active" : "nav-item"}
              key={item.label}
              onClick={() => navigateSidebar(item.key)}
              type="button"
            >
              <item.icon size={17} />
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-section">
          <span className="section-title">Pinned roots</span>
          {activeRoots.map((root) => (
            <button className="root-row" key={root} onClick={() => selectRoot(root)} type="button">
              <Folder size={15} />
              <span>{root}</span>
            </button>
          ))}
        </div>
      </aside>

      <section className="main-pane">
        <header className="titlebar">
          <div className="breadcrumb">
            <HardDrive size={18} />
            <ChevronRight size={16} />
            <span>This PC</span>
            <ChevronRight size={16} />
            <strong>Semantic Index</strong>
          </div>
          <form className="search-box" onSubmit={onSubmit}>
            <button title="Search" type="submit">
              <Search size={17} />
            </button>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search DocVec semantically"
            />
          </form>
        </header>

        <div className="toolbar">
          <button onClick={runScan} disabled={isScanning}>
            <RefreshCw size={16} />
            <span>{isScanning ? "Refreshing" : "Refresh Index"}</span>
          </button>
          <button
            onClick={isPaused ? runResume : runPause}
            disabled={!isScanRunning && !isPaused}
          >
            <Pause size={16} />
            <span>{isPaused ? "Resume" : "Pause"}</span>
          </button>
          <button onClick={refreshStatus}>
            <RefreshCw size={16} />
            <span>Refresh</span>
          </button>
          <button onClick={() => void saveCurrentSearch()}>
            <Database size={16} />
            <span>Save</span>
          </button>
          <button onClick={() => setLogsOpen((current) => !current)}>
            <Terminal size={16} />
            <span>Logs</span>
          </button>
          <select
            className="saved-search-select"
            value={selectedSavedSearchId}
            onChange={(event) => applySavedSearch(event.target.value)}
          >
            <option value="">Saved searches</option>
            {savedSearches.map((saved) => (
              <option key={saved.id} value={saved.id}>
                {saved.name}
              </option>
            ))}
          </select>
          <div className="view-toggle" aria-label="View mode">
            <button
              className={viewMode === "details" ? "active" : ""}
              onClick={() => setViewMode("details")}
              title="Details view"
            >
              <Rows3 size={15} />
              <span>Details</span>
            </button>
            <button
              className={viewMode === "list" ? "active" : ""}
              onClick={() => setViewMode("list")}
              title="List view"
            >
              <List size={15} />
              <span>List</span>
            </button>
            <button
              className={viewMode === "tiles" ? "active" : ""}
              onClick={() => setViewMode("tiles")}
              title="Tiles view"
            >
              <LayoutGrid size={15} />
              <span>Tiles</span>
            </button>
          </div>
          <div className="profile-toggle" aria-label="Scan profile">
            {Object.entries(scanProfiles).map(([key, profile]) => (
              <button
                className={scanProfile === key ? "active" : ""}
                key={key}
                onClick={() => setScanProfile(key as keyof typeof scanProfiles)}
              >
                {profile.label}
              </button>
            ))}
          </div>
          <div className="toolbar-spacer" />
          <span className="api-state">
            <Clock size={14} />
            {autoScanLabel(status)}
          </span>
          <span className="api-state">
            <BatteryCharging size={14} />
            {status?.auto_scan?.power_idle
              ? status.auto_scan.power_idle.is_charging
                ? "Charging"
                : "Battery"
              : "Power"}
          </span>
          <span className={apiError ? "api-state offline" : "api-state"}>
            {apiError ? "API offline" : "API connected"}
          </span>
        </div>

        <div className="filterbar">
          <label>
            <span>Drive</span>
            <select value={driveFilter} onChange={(event) => setDriveFilter(event.target.value)}>
              <option value="">All</option>
              <option value="C:">C:</option>
              <option value="D:">D:</option>
              <option value="E:">E:</option>
            </select>
          </label>
          <label>
            <span>Source</span>
            <select
              value={sourceKindFilter}
              onChange={(event) => setSourceKindFilter(event.target.value)}
            >
              {sourceFilterOptions.map((option) => (
                <option key={option.label} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Ext</span>
            <input
              value={extensionFilter}
              onChange={(event) => setExtensionFilter(event.target.value)}
              placeholder=".md"
            />
          </label>
          <label>
            <span>Type</span>
            <select
              value={fileTypeFilter}
              onChange={(event) => setFileTypeFilter(event.target.value)}
            >
              {fileTypeFilterOptions.map((option) => (
                <option key={option.label} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Project</span>
            <input
              value={projectFilter}
              onChange={(event) => setProjectFilter(event.target.value)}
              placeholder="warung"
            />
          </label>
          <label>
            <span>From</span>
            <input
              value={dateFromFilter}
              onChange={(event) => setDateFromFilter(event.target.value)}
              placeholder="2026-06-01"
            />
          </label>
          <label>
            <span>To</span>
            <input
              value={dateToFilter}
              onChange={(event) => setDateToFilter(event.target.value)}
              placeholder="2026-06-30"
            />
          </label>
          <label className="path-filter">
            <span>Path</span>
            <input
              value={pathFilter}
              onChange={(event) => setPathFilter(event.target.value)}
              placeholder="hermes, school, project"
            />
          </label>
        </div>

        <div className="content-grid">
          <section className="content-list">
            <div className="drive-strip">
              {drives.map((drive) => (
                <button
                  className={driveFilter === drive.drive ? "drive-tile active" : "drive-tile"}
                  key={drive.name}
                  onClick={() => selectDrive(drive.drive)}
                  type="button"
                >
                  <HardDrive size={24} />
                  <div className="drive-meta">
                    <strong>{drive.name}</strong>
                    <div className="drive-bar">
                      <span style={{ width: `${drive.fill}%` }} />
                    </div>
                    <small>{drive.free}</small>
                  </div>
                </button>
              ))}
            </div>

            {viewMode === "details" ? (
              <div className="list-header">
                <button onClick={() => setSort("name")}>
                  <span>Name{sortLabel("name")}</span>
                  <ArrowUpDown size={13} />
                </button>
                <button onClick={() => setSort("source")}>
                  <span>Source{sortLabel("source")}</span>
                  <ArrowUpDown size={13} />
                </button>
                <button onClick={() => setSort("type")}>
                  <span>Type{sortLabel("type")}</span>
                  <ArrowUpDown size={13} />
                </button>
                <button onClick={() => setSort("score")}>
                  <span>Score{sortLabel("score")}</span>
                  <ArrowUpDown size={13} />
                </button>
              </div>
            ) : null}

            {results.length === 0 ? (
              <div className="empty-state">
                <FileText size={34} />
                <strong>{isSearching ? "Searching..." : "No semantic results yet"}</strong>
                <span>Start the local API, scan a root, or search indexed files.</span>
              </div>
            ) : (
              <div className={`result-list ${viewMode}`}>
                {sortedResults.map((result) => (
                  <button
                    className={
                      selected?.chunk_id === result.chunk_id ? "result-row selected" : "result-row"
                    }
                    key={result.chunk_id}
                    onClick={() => setSelectedId(result.chunk_id)}
                  >
                    <span className="result-name">
                      <FileText size={18} />
                      <span className="result-main">
                        <strong>{result.title}</strong>
                        <small>{result.source_path}</small>
                      </span>
                    </span>
                    <span>{sourceLabel(result.source_kind)}</span>
                    <span>{resultTypeLabel(result)}</span>
                    <span>{result.score.toFixed(3)}</span>
                  </button>
                ))}
              </div>
            )}
          </section>

          <aside className="preview-pane">
            {selected ? (
              <>
                <div className="preview-title">
                  <FileText size={22} />
                  <div>
                    <strong>{selected.title}</strong>
                    <span>{sourceLabel(selected.source_kind)}</span>
                  </div>
                </div>
                <div className="preview-actions">
                  <button onClick={() => void openSelected("file")}>
                    <ExternalLink size={15} />
                    <span>Open</span>
                  </button>
                  <button onClick={() => void openSelected("folder")}>
                    <FolderOpen size={15} />
                    <span>Folder</span>
                  </button>
                  <button onClick={() => void copySelectedPath()}>
                    <Copy size={15} />
                    <span>Copy</span>
                  </button>
                </div>
                {previewMessage ? <p className="action-message">{previewMessage}</p> : null}
                <p className="snippet">{selected.snippet}</p>
                <pre className="context-text">{previewText ?? "Loading context..."}</pre>
                <dl>
                  <dt>Path</dt>
                  <dd>{selected.source_path}</dd>
                  <dt>Rank source</dt>
                  <dd>{selected.rank_source}</dd>
                  {lineRangeLabel(selected.metadata) ? (
                    <>
                      <dt>Line</dt>
                      <dd>{lineRangeLabel(selected.metadata)}</dd>
                    </>
                  ) : null}
                  <dt>Score</dt>
                  <dd>{selected.score.toFixed(4)}</dd>
                </dl>
              </>
            ) : (
              <div className="preview-empty">No preview available.</div>
            )}
          </aside>
        </div>

        <footer className="statusbar">
          <span>{results.length} results</span>
          <span>{storageLabel(status)}</span>
          <span>{visibleJob ? `${visibleJob.status}: ${visibleJob.indexed_count} indexed` : "Ready"}</span>
        </footer>
      </section>

      {logsOpen ? (
        <aside className="logs-drawer" aria-label="Logs">
          <div className="logs-header">
            <div>
              <strong>Logs</strong>
              <span>{logLines.length} entries</span>
            </div>
            <div className="logs-actions">
              <button onClick={() => void copyVisibleLogs()} title="Copy logs">
                <Copy size={15} />
              </button>
              <button onClick={() => void clearVisibleLogs()} title="Clear logs">
                <Trash2 size={15} />
              </button>
              <button onClick={() => setLogsOpen(false)} title="Close logs">
                <X size={15} />
              </button>
            </div>
          </div>
          {logError ? <p className="logs-error">{logError}</p> : null}
          <div className="logs-list">
            {logLines.length === 0 ? (
              <div className="logs-empty">No log entries.</div>
            ) : (
              logLines.map((line, index) => (
                <pre className={logLevelClass(line)} key={`${index}-${line}`}>
                  {line}
                </pre>
              ))
            )}
          </div>
        </aside>
      ) : null}
    </main>
  );
}
