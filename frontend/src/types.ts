export type User = { name: string; role: 'admin' | 'member'; emoji: string; must_change?: boolean }
export type Me =
  | { logged_in: false; users: { name: string; emoji: string }[]; first_run?: boolean }
  | { logged_in: true; user: User }

export type ProviderModel = { id: string; label: string; context_window: number }
export type Provider = {
  id: string; label: string; models: ProviderModel[]; default_model: string
  efforts: string[]; auth_modes: string[]; key_help_url: string; key_help: string; connected: boolean
}

export type Project = {
  id: string; name: string; emoji: string; descr: string; accent: string
  provider: string; model: string; effort: string
  live_port: number | null; live_url: string | null; live_ready: boolean
  mine?: boolean; threads?: number; active?: boolean; created_by?: string
}

export type ThreadSummary = { id: number; title: string; status: string; snippet: string; created_at: string }
export type TimelineEntry = { id: string; type: string; author: string; text: string; created_at: string }
export type Session = { alive: boolean; ctx_pct: number; pending: number; compacting: boolean } | null
export type ThreadDetail = {
  id: number; title: string; status: string; timeline: TimelineEntry[]; session: Session
  project_id: string; live_port: number | null; live_url: string | null; live_ready: boolean
}

export type FileEntry = { name: string; dir: boolean; size: number; mtime: number }
export type FileListing = { available: boolean; reason?: string; path: string; entries: FileEntry[] }
