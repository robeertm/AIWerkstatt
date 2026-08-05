import type { Me, User, Provider, Project, ThreadSummary, ThreadDetail, FileListing } from './types'

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
export const getProject = (id: string) => fetch(`/api/projects/${id}`).then(j<{ project: Project; threads: ThreadSummary[] }>)
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

// live URL from the current host + the project's port (never a hard-coded host).
export const liveUrl = (port: number | null | undefined): string | null =>
  port ? `${location.protocol}//${location.hostname}:${port}` : null
