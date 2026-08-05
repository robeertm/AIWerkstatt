export type User = { name: string; role: 'admin' | 'member'; emoji: string; must_change?: boolean }
export type Me =
  | { logged_in: false; users: { name: string; emoji: string }[]; first_run?: boolean }
  | { logged_in: true; user: User }

export type ProviderModel = { id: string; label: string; context_window: number }
export type Provider = {
  id: string; label: string; models: ProviderModel[]; default_model: string
  efforts: string[]; auth_modes: string[]; key_help_url: string; key_help: string
  connected: boolean; available: boolean
}

export type Project = {
  id: string; name: string; emoji: string; descr: string; accent: string
  provider: string; model: string; effort: string
  live_port: number | null; live_url: string | null; live_ready: boolean
  mine?: boolean; threads?: number; active?: boolean; created_by?: string; unread?: number
}

export type ThreadSummary = { id: number; title: string; status: string; snippet: string; created_at: string; unread?: number }

// visibility / self-management
export type UsageSummary = {
  total: { tokens: number; cost: number; runs: number }
  today: { tokens: number; cost: number; runs: number }
  by_provider: { provider: string; tokens: number; cost: number; runs: number }[]
}
export type PlanWindow = { pct: number | null; resets_at: string | null }
export type PlanLimit = { connected: boolean; available?: boolean; session?: PlanWindow; weekly?: PlanWindow }
export type LimitStatus = { active: boolean; until?: number; resumes_at?: string }
export type UpdateInfo = { current: string; latest: string | null; update_available: boolean; url: string }
export type GithubStatus = { connected: boolean; login: string | null }
export type ActivityItem = {
  thread_id: number; title: string; status: string; session: Session
  last: { type: string; text: string; created_at: string } | null
}
export type LiveEntry = { seq?: number; act: string; text: string; at: number }
export type LiveFeed = { entries: LiveEntry[]; offset: number; live: boolean }

// publish / release
export type ScanFinding = { file: string; line: number; severity: string; rule: string; snippet: string }
export type ScanResult = { ok: boolean; blocking: number; review: number; findings: ScanFinding[] }
export type PublishStatus = {
  project_id?: string; repo?: string; html_url?: string; visibility?: string
  version?: string; release_url?: string; published_at?: string; released_at?: string
}
export type TimelineEntry = { id: string; type: string; author: string; text: string; created_at: string }
export type Session = { alive: boolean; ctx_pct: number; pending: number; compacting: boolean } | null
export type ThreadDetail = {
  id: number; title: string; status: string; timeline: TimelineEntry[]; session: Session
  project_id: string; live_port: number | null; live_url: string | null; live_ready: boolean
}

export type FileEntry = { name: string; dir: boolean; size: number; mtime: number }
export type FileListing = { available: boolean; reason?: string; path: string; entries: FileEntry[] }
