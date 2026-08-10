import type {
  Me, User, Provider, Project, ThreadSummary, ThreadDetail, FileListing,
  UsageSummary, LimitStatus, UpdateInfo, GithubStatus, ActivityItem, ScanResult, PublishStatus,
  LiveFeed,
} from './types'

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let msg = String(r.status)
    try { msg = (await r.json()).error || msg } catch { /* ignore */ }
    const e = new Error(msg) as Error & { status?: number }
    e.status = r.status
    throw e
  }
  return r.json() as Promise<T>
}

const req = (url: string, method: string, body?: unknown) =>
  fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })

export const getHealth = () => fetch('/api/health').then(j<{ ok: boolean; version: string; providers: string[] }>)

// auth
export const getMe = () => fetch('/api/me').then(j<Me>)
export const login = (name: string, pin: string) => req('/api/login', 'POST', { name, pin }).then(j<{ user: User }>)
export const logout = () => req('/api/logout', 'POST').then(j<{ ok: boolean }>)
export const changePin = (pin: string) => req('/api/me/pin', 'POST', { pin }).then(j<{ ok: boolean }>)

// users
export const getUsers = () => fetch('/api/users').then(j<{ users: User[] }>)
export const addUser = (name: string, pin: string, role: string, emoji: string) =>
  req('/api/users', 'POST', { name, pin, role, emoji }).then(j<{ user: User }>)
export const removeUser = (name: string) => req(`/api/users/${encodeURIComponent(name)}`, 'DELETE').then(j<{ ok: boolean }>)

// providers
export const getProviders = () => fetch('/api/providers').then(j<{ providers: Provider[] }>)
export const connectProvider = (id: string, mode: string, value: string) =>
  req(`/api/providers/${id}/credentials`, 'POST', { mode, value }).then(j<{ ok: boolean }>)
export const disconnectProvider = (id: string) => req(`/api/providers/${id}/credentials`, 'DELETE').then(j<{ ok: boolean }>)

// projects
export const getProjects = () => fetch('/api/projects').then(j<{ projects: Project[]; me: string }>)
export const getProject = (id: string) => fetch(`/api/projects/${id}`).then(j<{ project: Project; threads: ThreadSummary[]; publish: PublishStatus }>)
export const createProject = (p: { name: string; emoji: string; desc: string; idea: string; provider: string; model?: string; effort?: string }) =>
  req('/api/projects', 'POST', p).then(j<{ project: Project }>)
export const deleteProject = (id: string) => req(`/api/projects/${id}`, 'DELETE').then(j<{ ok: boolean }>)
export const setProjectSettings = (id: string, provider: string, model: string, effort: string) =>
  req(`/api/projects/${id}/settings`, 'POST', { provider, model, effort }).then(j<{ ok: boolean; project: Project }>)

// threads
export const createThread = (id: string, title: string, request: string) =>
  req(`/api/projects/${id}/threads`, 'POST', { title, request }).then(j<{ id: number }>)
export const getThread = (tid: number) => fetch(`/api/threads/${tid}`).then(j<ThreadDetail>)
export const addComment = (tid: number, text: string) => req(`/api/threads/${tid}/comment`, 'POST', { text }).then(j<{ ok: boolean }>)
export const compactThread = (tid: number) => req(`/api/threads/${tid}/compact`, 'POST').then(j<{ ok: boolean }>)
export const stopThread = (tid: number) => req(`/api/threads/${tid}/stop`, 'POST').then(j<{ ok: boolean }>)

// files
export const getFiles = (id: string, path: string) =>
  fetch(`/api/projects/${id}/files?path=${encodeURIComponent(path)}`).then(j<FileListing>)
export const fileRawUrl = (id: string, path: string) =>
  `/api/projects/${id}/files/raw?path=${encodeURIComponent(path)}`

export const markSeen = (tid: number) => req(`/api/threads/${tid}/seen`, 'POST').then(j<{ ok: boolean }>)
export const getLive = (tid: number, offset: number) => fetch(`/api/threads/${tid}/live?offset=${offset}`).then(j<LiveFeed>)

// visibility
export const getActivity = (id: string) => fetch(`/api/projects/${id}/activity`).then(j<{ activity: ActivityItem[] }>)
export const getUsage = () => fetch('/api/usage').then(j<UsageSummary>)
export const getPlanLimit = () => fetch('/api/planlimit').then(j<import('./types').PlanLimit>)
export const planLimitStart = () => req('/api/planlimit/connect/start', 'POST').then(j<{ url: string }>)
export const planLimitFinish = (code: string) => req('/api/planlimit/connect/finish', 'POST', { code }).then(j<{ ok: boolean }>)
export const planLimitDisconnect = () => req('/api/planlimit/connect', 'DELETE').then(j<{ ok: boolean }>)
export const getLimit = () => fetch('/api/limit').then(j<LimitStatus>)
export const getUpdate = (force?: boolean) => fetch('/api/update' + (force ? '?force=1' : '')).then(j<UpdateInfo>)
export const installUpdate = () => req('/api/update/install', 'POST').then(j<{ ok: boolean }>)

// github account (for publishing)
export const getGithub = () => fetch('/api/github').then(j<GithubStatus>)
export const connectGithub = (token: string) => req('/api/github', 'POST', { token }).then(j<{ ok: boolean; login: string }>)
export const disconnectGithub = () => req('/api/github', 'DELETE').then(j<{ ok: boolean }>)

// publish / release
export const scanPublish = (id: string) => req(`/api/projects/${id}/publish/scan`, 'POST').then(j<{ scan: ScanResult; has_files: boolean }>)
export const publishProject = (id: string, repo: string, priv: boolean) =>
  req(`/api/projects/${id}/publish`, 'POST', { repo, private: priv }).then(j<{ ok: boolean; full_name: string; html_url: string; visibility: string; status: PublishStatus }>)
export const releaseProject = (id: string, version: string, notes: string) =>
  req(`/api/projects/${id}/release`, 'POST', { version, notes }).then(j<{ ok: boolean; tag: string; html_url: string; status: PublishStatus }>)
export const exportUrl = (id: string) => `/api/projects/${id}/publish/export`

// live URL from the current host + the project's port (never a hard-coded host).
export const liveUrl = (port: number | null | undefined): string | null =>
  port ? `${location.protocol}//${location.hostname}:${port}` : null
