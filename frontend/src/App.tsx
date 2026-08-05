import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  Me, User, Provider, Project, ThreadSummary, ThreadDetail, FileEntry, FileListing,
  UsageSummary, LimitStatus, UpdateInfo, GithubStatus, ActivityItem, ScanResult, PublishStatus,
  LiveEntry,
} from './types'
import * as api from './api'

// ---------- helpers ----------
function relTime(iso: string): string {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (s < 60) return 'just now'
  const m = Math.round(s / 60); if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60); if (h < 24) return `${h} h ago`
  return `${Math.round(h / 24)} d ago`
}
const STATUS: Record<string, { label: string; color: string }> = {
  working: { label: 'working', color: 'var(--warn)' },
  queued: { label: 'queued', color: 'var(--muted)' },
  waiting: { label: 'waiting for limit', color: 'var(--warn)' },
  done: { label: 'done', color: 'var(--good)' },
  stopped: { label: 'stopped', color: 'var(--bad)' },
  failed: { label: 'failed', color: 'var(--bad)' },
}
function Pill({ status }: { status: string }) {
  const s = STATUS[status] || STATUS.done
  return <span className="pill" style={{ color: s.color, background: 'currentColor' }}>
    <span style={{ color: '#0f1020', mixBlendMode: 'normal' }}>{s.label}</span></span>
}
function Working({ color }: { color: string }) {
  return <span className="dots" style={{ color }}><span /><span /><span /></span>
}

// ---------- Login ----------
function Login({ users, onDone, version }: { users: { name: string; emoji: string }[]; onDone: () => void; version?: string }) {
  const [name, setName] = useState<string | null>(users.length === 1 ? users[0].name : null)
  const [pin, setPin] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const submit = async (p: string) => {
    if (!name) return
    try { await api.login(name, p); onDone() } catch (e) { setErr(String((e as Error).message)); setPin('') }
  }
  return (
    <div className="center">
      <div className="card pad stack fadein" style={{ width: 340 }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '2.2rem' }}>🛠️</div>
          <div className="brand" style={{ fontSize: '1.3rem' }}>AIWerkstatt{version ? <small style={{ fontWeight: 500, color: 'var(--muted)' }}> v{version}</small> : null}</div>
          <div className="muted" style={{ fontSize: '.85rem' }}>Sign in</div>
        </div>
        {!name ? (
          <div className="tiles">
            {users.map((u) => (
              <button key={u.name} className="tile" onClick={() => setName(u.name)}>
                <span style={{ fontSize: '1.6rem' }}>{u.emoji}</span><span>{u.name}</span>
              </button>
            ))}
          </div>
        ) : (
          <>
            <div className="between"><b>{name}</b><button className="chip" onClick={() => { setName(null); setPin('') }}>change</button></div>
            <div style={{ textAlign: 'center', letterSpacing: '.4rem', fontSize: '1.4rem', minHeight: '1.6rem' }}>
              {'•'.repeat(pin.length)}
            </div>
            <div className="pinpad">
              {['1', '2', '3', '4', '5', '6', '7', '8', '9'].map((k) => (
                <button key={k} className="btn key" onClick={() => setPin((p) => (p + k).slice(0, 12))}>{k}</button>
              ))}
              <button className="btn key" onClick={() => setPin((p) => p.slice(0, -1))}>⌫</button>
              <button className="btn key" onClick={() => setPin((p) => (p + '0').slice(0, 12))}>0</button>
              <button className="btn key primary" onClick={() => submit(pin)}>→</button>
            </div>
          </>
        )}
        {err && <div style={{ color: 'var(--bad)', fontSize: '.85rem', textAlign: 'center' }}>{err}</div>}
        <div className="muted" style={{ fontSize: '.72rem', textAlign: 'center' }}>First run? Default is admin / PIN 1234.</div>
      </div>
    </div>
  )
}

// ---------- Providers modal ----------
function ProvidersModal({ onClose, isAdmin }: { onClose: () => void; isAdmin: boolean }) {
  const [list, setList] = useState<Provider[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const load = useCallback(() => api.getProviders().then((r) => setList(r.providers)).catch(() => {}), [])
  useEffect(() => { load() }, [load])
  const connect = async (id: string, mode: string) => {
    setBusy(id)
    try { await api.connectProvider(id, mode, drafts[id] || ''); setDrafts((d) => ({ ...d, [id]: '' })); await load() }
    catch (e) { alert(String((e as Error).message)) } finally { setBusy(null) }
  }
  return (
    <div className="scrim" onClick={onClose}>
      <div className="card modal pad stack fadein" onClick={(e) => e.stopPropagation()}>
        <div className="between"><b>AI providers</b><button className="chip" onClick={onClose}>✕ close</button></div>
        <div className="muted" style={{ fontSize: '.85rem' }}>Connect at least one. Keys are stored privately on this machine, never in the repo.</div>
        {list.filter((p) => p.id !== 'demo' || p.available).map((p) => (
          <div key={p.id} className="card pad stack" style={{ background: 'var(--surface-2)' }}>
            <div className="between">
              <b>{p.label}</b>
              <span className="chip" style={{ color: p.connected ? 'var(--good)' : 'var(--muted)', borderColor: p.connected ? '#34d39955' : 'var(--border)' }}>
                {p.connected ? '● connected' : '○ not connected'}
              </span>
            </div>
            {p.auth_modes.length === 0 ? (
              <div className="muted" style={{ fontSize: '.85rem' }}>{p.key_help}</div>
            ) : isAdmin ? (
              <>
                <input className="input" type="password" placeholder={p.auth_modes.includes('api_key') ? 'Paste API key' : 'Paste token'}
                  value={drafts[p.id] || ''} onChange={(e) => setDrafts((d) => ({ ...d, [p.id]: e.target.value }))} />
                <div className="row">
                  {p.auth_modes.includes('api_key') &&
                    <button className="btn primary" disabled={busy === p.id} onClick={() => connect(p.id, 'api_key')}>Connect key</button>}
                  {p.auth_modes.includes('oauth') &&
                    <button className="btn" disabled={busy === p.id} onClick={() => connect(p.id, 'oauth')}>Connect token</button>}
                  {p.key_help_url && <a className="chip" href={p.key_help_url} target="_blank" rel="noreferrer">Where do I get a key? ↗</a>}
                </div>
                <div className="muted" style={{ fontSize: '.78rem' }}>{p.key_help}</div>
              </>
            ) : <div className="muted" style={{ fontSize: '.82rem' }}>Ask an admin to connect this provider.</div>}
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------- File browser ----------
const IMG = /\.(png|jpe?g|gif|webp|svg|ico|avif)$/i
const TXT = /\.(txt|md|py|js|ts|tsx|jsx|json|css|scss|html?|xml|ya?ml|toml|ini|cfg|sh|env|sql|csv|vue|svelte|rs|go|rb|php|gitignore|dockerignore)$/i
function icon(e: FileEntry) {
  if (e.dir) return '📁'; const n = e.name.toLowerCase()
  if (IMG.test(n)) return '🖼️'; if (/\.(html?|css|jsx?|tsx?)$/.test(n)) return '🌐'
  if (/\.(py|rs|go|rb|php|sh)$/.test(n)) return '📜'; return '📄'
}
function FileBrowser({ id, name, onClose }: { id: string; name: string; onClose: () => void }) {
  const [path, setPath] = useState(''); const [ls, setLs] = useState<FileListing | null>(null)
  const [preview, setPreview] = useState<{ name: string; path: string } | null>(null); const [text, setText] = useState<string | null>(null)
  const load = useCallback((p: string) => api.getFiles(id, p).then((d) => { setLs(d); setPath(d.path || '') }).catch(() => {}), [id])
  useEffect(() => { load('') }, [load])
  const open = (e: FileEntry) => {
    const child = path ? `${path}/${e.name}` : e.name
    if (e.dir) return load(child)
    if (IMG.test(e.name)) { setPreview({ name: e.name, path: child }); setText(null); return }
    if (TXT.test(e.name)) { setPreview({ name: e.name, path: child }); setText(null); fetch(api.fileRawUrl(id, child)).then((r) => r.text()).then(setText).catch(() => setText('—')); return }
    window.open(api.fileRawUrl(id, child), '_blank', 'noopener')
  }
  const crumbs = path ? path.split('/') : []
  return (
    <div className="scrim" onClick={onClose}>
      <div className="card modal pad stack fadein" onClick={(e) => e.stopPropagation()}>
        <div className="between"><b>📁 Files · {name}</b><button className="chip" onClick={onClose}>✕ close</button></div>
        <div className="row" style={{ flexWrap: 'wrap', fontSize: '.85rem' }}>
          <button className="chip" onClick={() => load('')}>🏠 root</button>
          {crumbs.map((c, i) => <button key={i} className="chip" onClick={() => load(crumbs.slice(0, i + 1).join('/'))}>{c}</button>)}
        </div>
        <div className="stack" style={{ gap: '.1rem' }}>
          {!ls?.available && <div className="muted pad">{ls?.reason || 'Loading…'}</div>}
          {ls?.available && ls.entries.length === 0 && <div className="muted pad">Empty.</div>}
          {ls?.entries.map((e) => (
            <div key={e.name} className="filerow" onClick={() => open(e)}>
              <span>{icon(e)}</span><span style={{ flex: 1, fontWeight: e.dir ? 700 : 400 }}>{e.name}</span>
              {!e.dir && <a className="chip" href={api.fileRawUrl(id, path ? `${path}/${e.name}` : e.name)} target="_blank" rel="noreferrer" onClick={(ev) => ev.stopPropagation()}>↗</a>}
            </div>
          ))}
        </div>
      </div>
      {preview && (
        <div className="scrim" onClick={() => setPreview(null)}>
          <div className="card modal pad stack" onClick={(e) => e.stopPropagation()}>
            <div className="between"><b>{preview.name}</b>
              <span className="row"><a className="chip" href={api.fileRawUrl(id, preview.path)} target="_blank" rel="noreferrer">↗ open</a>
                <button className="chip" onClick={() => setPreview(null)}>✕</button></span></div>
            {IMG.test(preview.name)
              ? <img src={api.fileRawUrl(id, preview.path)} alt={preview.name} style={{ maxWidth: '100%' }} />
              : <pre className="code">{text ?? 'Loading…'}</pre>}
          </div>
        </div>
      )}
    </div>
  )
}

// ---------- Live link ----------
function LiveLink({ project, building }: { project: { live_port: number | null; live_ready: boolean }; building: boolean }) {
  const url = api.liveUrl(project.live_port)
  if (project.live_ready && url) return <a className="btn primary" href={url} target="_blank" rel="noreferrer">▶ Open live</a>
  if (building) return <span className="chip" style={{ color: 'var(--warn)' }}><Working color="var(--warn)" /> building…</span>
  return <span className="chip muted">preview appears after the first build</span>
}

// ---------- Live agent feed (watch what it's doing, step by step) ----------
function LiveRow({ e }: { e: LiveEntry }) {
  if (e.act === 'tool') {
    const bash = e.text.startsWith('$ ')
    return <div className={`liverow tool${bash ? ' bash' : ''}`}>{bash ? e.text : `🔧 ${e.text}`}</div>
  }
  if (e.act === 'think') return <div className="liverow think">💭 {e.text}</div>
  if (e.act === 'result') return <div className="liverow result">↳ {e.text}</div>
  return <div className="liverow say">💬 {e.text}</div>
}
function AgentLive({ tid }: { tid: number }) {
  const [entries, setEntries] = useState<LiveEntry[]>([])
  const [live, setLive] = useState(false)
  const offset = useRef(0)
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => { setEntries([]); offset.current = 0 }, [tid])
  useEffect(() => {
    let alive = true; let timer: ReturnType<typeof setTimeout>
    const tick = () => api.getLive(tid, offset.current).then((d) => {
      if (!alive) return
      setLive(d.live)
      if (d.entries.length) { offset.current = d.offset; setEntries((e) => [...e, ...d.entries].slice(-250)) }
      timer = setTimeout(tick, d.live ? 1200 : 5000)
    }).catch(() => { timer = setTimeout(tick, 5000) })
    tick(); return () => { alive = false; clearTimeout(timer) }
  }, [tid])
  useEffect(() => { box.current?.scrollTo({ top: box.current.scrollHeight }) }, [entries.length])
  if (entries.length === 0) return null
  return (
    <div className="card pad stack live">
      <div className="between" style={{ fontSize: '.78rem' }}>
        <span className="row">{live ? <Working color="var(--accent)" /> : <span>🔎</span>}
          <b>What the agent is doing</b></span>
        <span className="muted">{entries.length} steps</span>
      </div>
      <div className="livebox" ref={box}>{entries.map((e, i) => <LiveRow key={i} e={e} />)}</div>
    </div>
  )
}

// ---------- Thread view ----------
function ThreadView({ tid, accent, onBack }: { tid: number; accent: string; onBack: () => void }) {
  const [t, setT] = useState<ThreadDetail | null>(null)
  const [text, setText] = useState(''); const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null); const [files, setFiles] = useState(false)
  const bottom = useRef<HTMLDivElement>(null)
  const working = t ? (t.status === 'working' || t.status === 'queued') : false
  useEffect(() => {
    let alive = true; let timer: ReturnType<typeof setTimeout>
    const tick = () => api.getThread(tid).then((d) => { if (alive) { setT(d); setErr(null) } }).catch((e) => { if (alive) setErr(String(e.message)) })
      .finally(() => { if (alive) { const fast = t ? (t.status === 'working' || t.status === 'queued' || !!t.session?.alive) : true; timer = setTimeout(tick, fast ? 3000 : 10000) } })
    tick(); return () => { alive = false; clearTimeout(timer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tid, t?.status, t?.session?.alive])
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }) }, [t?.timeline.length])
  useEffect(() => { if (t) api.markSeen(tid).catch(() => {}) }, [tid, t?.timeline.length])
  const send = async () => {
    if (!text.trim()) return; setBusy(true); setErr(null)
    try { await api.addComment(tid, text.trim()); setText(''); setT(await api.getThread(tid)) }
    catch (e) { setErr(String((e as Error).message)) } finally { setBusy(false) }
  }
  const stop = async () => {
    if (!confirm('Stop this task? The running agent is terminated.')) return
    setBusy(true); try { await api.stopThread(tid); setT(await api.getThread(tid)) } catch (e) { setErr(String((e as Error).message)) } finally { setBusy(false) }
  }
  const compact = async () => { setBusy(true); try { await api.compactThread(tid); setT(await api.getThread(tid)) } catch (e) { setErr(String((e as Error).message)) } finally { setBusy(false) } }
  if (!t) return <div className="wrap"><div className="muted pad">{err ? `Error: ${err}` : 'Loading…'}</div></div>
  const s = t.session
  return (
    <div className="wrap fadein">
      <div className="row" style={{ marginBottom: '.8rem' }}>
        <button className="chip" onClick={onBack}>← back</button>
        <Pill status={t.status} />
        <div className="spacer" />
        <LiveLink project={t} building={working} />
      </div>
      <h2 style={{ color: accent, margin: '.2rem 0 1rem' }}>{t.title}</h2>
      {err && <div className="b-bad bub" style={{ maxWidth: '100%' }}>Error: {err}</div>}
      <div className="bubbles">
        {t.timeline.map((e) => {
          if (e.type === 'user') return <div key={e.id} className="bub b-user">{e.text}</div>
          if (e.type === 'ack') return <div key={e.id} className="bub b-sys">🛠️ {e.text}</div>
          if (e.type === 'failed') return <div key={e.id} className="bub b-bad">{e.text}</div>
          if (e.type === 'stopped') return <div key={e.id} className="bub b-sys" style={{ color: 'var(--bad)' }}>⏹️ {e.text}</div>
          if (e.type === 'limited') return <div key={e.id} className="bub b-sys" style={{ color: 'var(--warn)' }}>⏸️ {e.text}</div>
          return <div key={e.id}><div className="b-label">🤖 Agent</div><div className="bub b-agent">{e.text}</div></div>
        })}
        {working && <div className="bub b-agent"><Working color={STATUS[t.status].color} /> <span className="muted">Agent {STATUS[t.status].label}…</span></div>}
        <div ref={bottom} />
      </div>

      <div className="stack" style={{ position: 'sticky', bottom: '.6rem', marginTop: '1rem' }}>
        <AgentLive tid={tid} />
        {s?.alive && (
          <div className="card pad" style={{ background: '#12162a' }}>
            <div className="between" style={{ fontSize: '.78rem' }}>
              <span style={{ color: 'var(--good)', fontWeight: 700 }}>● session open — type to add to it</span>
              <span className="row">
                <span style={{ color: s.ctx_pct >= 80 ? 'var(--bad)' : s.ctx_pct >= 50 ? 'var(--warn)' : 'var(--good)' }}>
                  {s.compacting ? 'summarizing…' : `context ${s.ctx_pct}%`}</span>
                {s.ctx_pct >= 50 && <button className="chip" disabled={busy || s.compacting} onClick={compact}>🧹 summarize</button>}
              </span>
            </div>
            <div className="bar" style={{ marginTop: '.5rem' }}>
              <div style={{ width: `${s.ctx_pct}%`, background: s.ctx_pct >= 80 ? 'var(--bad)' : s.ctx_pct >= 50 ? 'var(--warn)' : 'var(--good)' }} />
            </div>
          </div>
        )}
        <div className="card pad stack">
          <textarea className="area" rows={2} placeholder="Follow up or add a new point…" value={text} onChange={(e) => setText(e.target.value)} />
          <div className="between">
            <button className="chip" onClick={() => setFiles(true)}>📁 Files</button>
            {(working || s?.alive) && !text.trim()
              ? <button className="btn danger" disabled={busy} onClick={stop}>⏹️ Stop</button>
              : <button className="btn primary" disabled={busy || !text.trim()} onClick={send}>{busy ? 'sending…' : 'Send'}</button>}
          </div>
        </div>
      </div>
      {files && <FileBrowser id={t.project_id} name={t.title} onClose={() => setFiles(false)} />}
    </div>
  )
}

// ---------- Project view ----------
function ProjectView({ id, isAdmin, onBack, onOpenThread }: { id: string; isAdmin: boolean; onBack: () => void; onOpenThread: (tid: number) => void }) {
  const [project, setProject] = useState<Project | null>(null)
  const [threads, setThreads] = useState<ThreadSummary[]>([])
  const [providers, setProviders] = useState<Provider[]>([])
  const [title, setTitle] = useState(''); const [body, setBody] = useState(''); const [composing, setComposing] = useState(false)
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null); const [files, setFiles] = useState(false)
  const [pub, setPub] = useState<PublishStatus>({})
  useEffect(() => {
    let alive = true; let timer: ReturnType<typeof setTimeout>
    const tick = () => api.getProject(id).then((d) => { if (!alive) return; setProject(d.project); setThreads(d.threads); setPub(d.publish || {}); const busyP = !d.project.live_ready || d.threads.some((t) => t.status === 'working' || t.status === 'queued'); timer = setTimeout(tick, busyP ? 3500 : 10000) }).catch(() => { timer = setTimeout(tick, 10000) })
    tick(); return () => { alive = false; clearTimeout(timer) }
  }, [id])
  useEffect(() => { api.getProviders().then((r) => setProviders(r.providers)).catch(() => {}) }, [])
  const submit = async () => {
    if (!title.trim() || !body.trim()) return; setBusy(true); setErr(null)
    try { const r = await api.createThread(id, title.trim(), body.trim()); setTitle(''); setBody(''); setComposing(false); onOpenThread(r.id) }
    catch (e) { setErr(String((e as Error).message)) } finally { setBusy(false) }
  }
  const changeSettings = async (provider: string, model: string, effort: string) => {
    try { const r = await api.setProjectSettings(id, provider, model, effort); setProject(r.project) } catch (e) { alert(String((e as Error).message)) }
  }
  const del = async () => {
    if (!project || !confirm(`Delete “${project.name}” and its live app?`)) return
    try { await api.deleteProject(id); onBack() } catch (e) { setErr(String((e as Error).message)) }
  }
  if (!project) return <div className="wrap"><div className="muted pad">{err ? `Error: ${err}` : 'Loading…'}</div></div>
  const active = threads.some((t) => t.status === 'working' || t.status === 'queued')
  const spec = providers.find((p) => p.id === project.provider)
  return (
    <div className="wrap fadein">
      <button className="chip" onClick={onBack}>← all projects</button>
      <div className="card pad" style={{ margin: '.8rem 0 1.2rem', borderColor: `${project.accent}55` }}>
        <div className="between">
          <div className="row"><span className="emoji">{project.emoji}</span>
            <div><div style={{ fontWeight: 800, fontSize: '1.2rem' }}>{project.name}</div>
              <div className="muted" style={{ fontSize: '.85rem' }}>{project.descr}</div></div></div>
          <LiveLink project={project} building={active || !project.live_ready} />
        </div>
      </div>

      <ActivityStrip id={id} />

      {(isAdmin || project.mine) && (
        <div className="card pad stack" style={{ marginBottom: '1rem' }}>
          <div className="row" style={{ flexWrap: 'wrap' }}>
            <label className="lbl">Provider</label>
            <select className="input" style={{ width: 'auto' }} value={project.provider}
              onChange={(e) => { const p = providers.find((x) => x.id === e.target.value); changeSettings(e.target.value, p?.default_model || '', '') }}>
              {providers.filter((p) => p.id !== 'demo' || p.available || p.id === project.provider)
                .map((p) => <option key={p.id} value={p.id} disabled={!p.connected}>{p.label}{p.connected ? '' : ' (not connected)'}</option>)}
            </select>
            {spec && spec.models.length > 0 && (
              <select className="input" style={{ width: 'auto' }} value={project.model} onChange={(e) => changeSettings(project.provider, e.target.value, project.effort)}>
                {spec.models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
              </select>
            )}
            {spec && spec.efforts.length > 0 && (
              <select className="input" style={{ width: 'auto' }} value={project.effort} onChange={(e) => changeSettings(project.provider, project.model, e.target.value)}>
                <option value="">default effort</option>
                {spec.efforts.map((x) => <option key={x} value={x}>{x}</option>)}
              </select>
            )}
            <div className="spacer" />
            <button className="chip" onClick={() => setFiles(true)}>📁 Files</button>
          </div>
        </div>
      )}

      {!composing
        ? <button className="btn primary" style={{ width: '100%', padding: '.9rem' }} onClick={() => setComposing(true)}>✏️ New task</button>
        : (
          <div className="card pad stack">
            <input className="input" placeholder="Short title (e.g. “a habit tracker”)" value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} />
            <textarea className="area" rows={4} placeholder="Describe what you'd like, in plain words…" value={body} onChange={(e) => setBody(e.target.value)} />
            {err && <div style={{ color: 'var(--bad)', fontSize: '.85rem' }}>{err}</div>}
            <div className="row">
              <button className="btn primary" disabled={busy || !title.trim() || !body.trim()} onClick={submit}>{busy ? 'sending…' : '🚀 Send task'}</button>
              <button className="btn ghost" onClick={() => setComposing(false)}>Cancel</button>
            </div>
          </div>
        )}

      <div className="between" style={{ margin: '1.4rem 0 .6rem' }}><b className="muted">Tasks</b></div>
      {threads.length === 0 && <div className="card pad muted" style={{ textAlign: 'center' }}>No tasks yet — send the first one. 🎈</div>}
      <div className="stack">
        {threads.map((t) => (
          <button key={t.id} className="card pad proj" onClick={() => onOpenThread(t.id)}>
            <div className="between"><b>{t.title}</b>
              <span className="row">{!!t.unread && <span className="badge">{t.unread}</span>}<Pill status={t.status} /></span></div>
            {t.snippet && <div className="muted" style={{ fontSize: '.88rem', marginTop: '.2rem' }}>{t.snippet}</div>}
            <div className="muted" style={{ fontSize: '.75rem', marginTop: '.4rem' }}>{relTime(t.created_at)}</div>
          </button>
        ))}
      </div>

      {(isAdmin || project.mine) && <PublishPanel id={id} initial={pub} isAdmin={isAdmin} />}

      {(isAdmin || project.mine) && (
        <div style={{ textAlign: 'center', marginTop: '2.5rem' }}>
          <button className="btn danger" onClick={del}>🗑️ Delete this project</button>
        </div>
      )}
      {files && <FileBrowser id={id} name={project.name} onClose={() => setFiles(false)} />}
    </div>
  )
}

// ---------- Gallery ----------
function Gallery({ onOpen }: { onOpen: (id: string) => void }) {
  const [projects, setProjects] = useState<Project[]>([])
  const [providers, setProviders] = useState<Provider[]>([])
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState(''); const [emoji, setEmoji] = useState('✨'); const [idea, setIdea] = useState('')
  const [provider, setProvider] = useState('demo'); const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null)
  const load = useCallback(() => {
    api.getProjects().then((r) => setProjects(r.projects)).catch(() => {})
    api.getProviders().then((r) => setProviders(r.providers)).catch(() => {})
  }, [])
  useEffect(() => {
    load(); const t = setInterval(load, 6000); return () => clearInterval(t)
  }, [load])
  // Default the new-project provider to a connected one — never a hidden demo.
  useEffect(() => {
    if (!providers.length) return
    const visible = providers.filter((p) => p.id !== 'demo' || p.available)
    if (!visible.some((p) => p.id === provider && p.connected)) {
      const best = visible.find((p) => p.connected) || visible[0]
      if (best && best.id !== provider) setProvider(best.id)
    }
  }, [providers, provider])
  const create = async () => {
    if (!name.trim()) return; setBusy(true); setErr(null)
    try { const r = await api.createProject({ name: name.trim(), emoji, desc: '', idea: idea.trim(), provider }); setCreating(false); setName(''); setIdea(''); onOpen(r.project.id) }
    catch (e) { setErr(String((e as Error).message)) } finally { setBusy(false) }
  }
  return (
    <div className="wrap fadein">
      <button className="btn primary" style={{ width: '100%', padding: '.9rem', marginBottom: '1.2rem' }} onClick={() => setCreating((v) => !v)}>➕ New project</button>
      {creating && (
        <div className="card pad stack" style={{ marginBottom: '1.2rem' }}>
          <div className="row">
            <input className="input" style={{ width: '4rem', textAlign: 'center' }} value={emoji} maxLength={2} onChange={(e) => setEmoji(e.target.value)} />
            <input className="input" placeholder="Project name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <select className="input" value={provider} onChange={(e) => setProvider(e.target.value)}>
            {providers.filter((p) => p.id !== 'demo' || p.available)
              .map((p) => <option key={p.id} value={p.id} disabled={!p.connected}>{p.label}{p.connected ? '' : ' (connect first)'}</option>)}
          </select>
          <textarea className="area" rows={3} placeholder="First idea (optional) — the agent starts on it right away" value={idea} onChange={(e) => setIdea(e.target.value)} />
          {err && <div style={{ color: 'var(--bad)', fontSize: '.85rem' }}>{err}</div>}
          <div className="row"><button className="btn primary" disabled={busy || !name.trim()} onClick={create}>{busy ? 'creating…' : 'Create'}</button>
            <button className="btn ghost" onClick={() => setCreating(false)}>Cancel</button></div>
        </div>
      )}
      {projects.length === 0 && <div className="card pad muted" style={{ textAlign: 'center' }}>No projects yet. Create one to get started 🎈</div>}
      <div className="grid">
        {projects.map((p) => (
          <button key={p.id} className="card pad proj" style={{ borderColor: `${p.accent}44` }} onClick={() => onOpen(p.id)}>
            <div className="top"><span className="emoji">{p.emoji}</span>
              <div style={{ flex: 1 }}><div style={{ fontWeight: 800 }}>{p.name}</div><div className="muted" style={{ fontSize: '.8rem' }}>{p.descr || '—'}</div></div>
              {!!p.unread && <span className="badge" title="new agent replies">{p.unread}</span>}</div>
            <div className="row" style={{ marginTop: '.7rem', fontSize: '.78rem' }}>
              {p.active ? <span className="chip" style={{ color: 'var(--warn)' }}><Working color="var(--warn)" /> working</span>
                : p.live_ready ? <span className="chip" style={{ color: 'var(--good)' }}>● live</span> : <span className="chip muted">new</span>}
              <span className="muted">· {p.threads || 0} tasks</span>
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}

// ---------- Change PIN (also the forced first-run flow) ----------
function ChangePin({ forced, onDone, onClose }: { forced: boolean; onDone: () => void; onClose?: () => void }) {
  const [pin, setPin] = useState(''); const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null)
  const digits = (v: string) => v.replace(/\D/g, '').slice(0, 12)
  const save = async () => {
    if (pin.length < 4) { setErr('PIN must be 4–12 digits.'); return }
    if (pin !== confirm) { setErr("The two PINs don't match."); return }
    setBusy(true); setErr(null)
    try { await api.changePin(pin); onDone() } catch (e) { setErr(String((e as Error).message)) } finally { setBusy(false) }
  }
  return (
    <div className="scrim" onClick={forced ? undefined : onClose}>
      <div className="card modal pad stack fadein" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 360 }}>
        <div className="between"><b>{forced ? '🔒 Set your PIN' : '🔒 Change PIN'}</b>
          {!forced && <button className="chip" onClick={onClose}>✕ close</button>}</div>
        {forced && <div className="muted" style={{ fontSize: '.85rem' }}>You're on the default PIN. Choose your own to continue.</div>}
        <input className="input" type="password" inputMode="numeric" placeholder="New PIN (4–12 digits)"
          value={pin} onChange={(e) => setPin(digits(e.target.value))} />
        <input className="input" type="password" inputMode="numeric" placeholder="Repeat new PIN"
          value={confirm} onChange={(e) => setConfirm(digits(e.target.value))} />
        {err && <div style={{ color: 'var(--bad)', fontSize: '.85rem' }}>{err}</div>}
        <button className="btn primary" disabled={busy || !pin} onClick={save}>{busy ? 'saving…' : 'Save PIN'}</button>
      </div>
    </div>
  )
}

// ---------- Admin: user management ----------
function AdminUsersModal({ meName, onClose }: { meName: string; onClose: () => void }) {
  const [users, setUsers] = useState<User[]>([])
  const [name, setName] = useState(''); const [pin, setPin] = useState('')
  const [emoji, setEmoji] = useState('🙂'); const [role, setRole] = useState('member')
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null)
  const load = useCallback(() => api.getUsers().then((r) => setUsers(r.users)).catch(() => {}), [])
  useEffect(() => { load() }, [load])
  const add = async () => {
    setBusy(true); setErr(null)
    try { await api.addUser(name.trim(), pin, role, emoji); setName(''); setPin(''); setEmoji('🙂'); setRole('member'); await load() }
    catch (e) { setErr(String((e as Error).message)) } finally { setBusy(false) }
  }
  const remove = async (n: string) => {
    if (!confirm(`Remove ${n}?`)) return
    try { await api.removeUser(n); await load() } catch (e) { alert(String((e as Error).message)) }
  }
  return (
    <div className="scrim" onClick={onClose}>
      <div className="card modal pad stack fadein" onClick={(e) => e.stopPropagation()}>
        <div className="between"><b>👥 Users</b><button className="chip" onClick={onClose}>✕ close</button></div>
        <div className="stack" style={{ gap: '.25rem' }}>
          {users.map((u) => (
            <div key={u.name} className="between filerow">
              <span className="row"><span>{u.emoji}</span><b>{u.name}</b>
                <span className="chip muted">{u.role}</span>
                {u.must_change && <span className="chip" style={{ color: 'var(--warn)' }}>default PIN</span>}</span>
              {u.name.toLowerCase() !== meName.toLowerCase()
                ? <button className="chip" style={{ color: 'var(--bad)' }} onClick={() => remove(u.name)}>remove</button>
                : <span className="chip muted">you</span>}
            </div>
          ))}
        </div>
        <div className="card pad stack" style={{ background: 'var(--surface-2)' }}>
          <b style={{ fontSize: '.9rem' }}>Add a user</b>
          <div className="row">
            <input className="input" style={{ width: '3.6rem', textAlign: 'center' }} value={emoji} maxLength={2} onChange={(e) => setEmoji(e.target.value)} />
            <input className="input" placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="row">
            <input className="input" type="password" inputMode="numeric" placeholder="PIN (4–12 digits)"
              value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 12))} />
            <select className="input" style={{ width: 'auto' }} value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="member">member</option><option value="admin">admin</option>
            </select>
          </div>
          {err && <div style={{ color: 'var(--bad)', fontSize: '.85rem' }}>{err}</div>}
          <button className="btn primary" disabled={busy || !name.trim() || pin.length < 4} onClick={add}>{busy ? 'adding…' : 'Add user'}</button>
        </div>
      </div>
    </div>
  )
}

// ---------- Usage ----------
function UsageModal({ onClose }: { onClose: () => void }) {
  const [u, setU] = useState<UsageSummary | null>(null)
  useEffect(() => { api.getUsage().then(setU).catch(() => {}) }, [])
  const fmt = (n: number) => n.toLocaleString()
  const money = (n: number) => `$${n.toFixed(n < 1 ? 4 : 2)}`
  const Stat = ({ label, s }: { label: string; s: { tokens: number; cost: number; runs: number } }) => (
    <div className="card pad" style={{ flex: 1, background: 'var(--surface-2)' }}>
      <div className="muted" style={{ fontSize: '.72rem' }}>{label}</div>
      <div style={{ fontWeight: 800, fontSize: '1.2rem' }}>{money(s.cost)}</div>
      <div className="muted" style={{ fontSize: '.76rem' }}>{fmt(s.tokens)} tokens · {s.runs} runs</div>
    </div>
  )
  return (
    <div className="scrim" onClick={onClose}>
      <div className="card modal pad stack fadein" onClick={(e) => e.stopPropagation()}>
        <div className="between"><b>📊 Usage</b><button className="chip" onClick={onClose}>✕ close</button></div>
        {!u ? <div className="muted pad">Loading…</div> : (
          <>
            <div className="row" style={{ gap: '.6rem', alignItems: 'stretch' }}>
              <Stat label="Today" s={u.today} /><Stat label="All time" s={u.total} />
            </div>
            {u.by_provider.length > 0 && (
              <div className="stack" style={{ gap: '.2rem' }}>
                <div className="muted" style={{ fontSize: '.76rem' }}>By provider</div>
                {u.by_provider.map((p) => (
                  <div key={p.provider} className="between filerow">
                    <b>{p.provider}</b>
                    <span className="muted" style={{ fontSize: '.82rem' }}>{fmt(p.tokens)} tok · {money(p.cost)} · {p.runs} runs</span>
                  </div>
                ))}
              </div>
            )}
            <div className="muted" style={{ fontSize: '.72rem' }}>Output-token usage as reported by each provider. Demo runs are free.</div>
          </>
        )}
      </div>
    </div>
  )
}

// ---------- Banners ----------
function LimitBanner() {
  const [l, setL] = useState<LimitStatus | null>(null)
  useEffect(() => {
    let alive = true; const f = () => api.getLimit().then((d) => { if (alive) setL(d) }).catch(() => {})
    f(); const t = setInterval(f, 30000); return () => { alive = false; clearInterval(t) }
  }, [])
  if (!l?.active) return null
  return <div className="banner warn">⏸️ Usage limit reached — agents pause and resume automatically at {l.resumes_at}.</div>
}

function UpdateBanner() {
  const [u, setU] = useState<UpdateInfo | null>(null)
  const [hidden, setHidden] = useState(false)
  const [state, setState] = useState<'idle' | 'updating' | 'error'>('idle')
  useEffect(() => { api.getUpdate().then(setU).catch(() => {}) }, [])
  const doUpdate = async () => {
    if (!confirm('Update now? AIWerkstatt rebuilds and restarts — this takes about a minute and briefly interrupts running work. The page reloads when it’s done.')) return
    setState('updating')
    try { await api.installUpdate() } catch { setState('error'); return }
    const target = u?.latest
    let tries = 0
    const poll = () => api.getHealth().then((h) => {
      if (!target || h.version === target) { location.reload(); return }
      if (++tries > 140) { setState('error'); return }
      setTimeout(poll, 3000)
    }).catch(() => { if (++tries > 140) { setState('error'); return } setTimeout(poll, 3000) })
    setTimeout(poll, 6000)
  }
  if (hidden || !u?.update_available) return null
  if (state === 'updating')
    return <div className="banner info"><Working color="var(--accent)" /><span>Updating to {u.latest}… AIWerkstatt is rebuilding and will reload automatically (about a minute).</span></div>
  return (
    <div className="banner info">
      <span>🎉 AIWerkstatt {u.latest} is available (you have {u.current}).</span>
      <a href={u.url} target="_blank" rel="noreferrer">Release notes ↗</a>
      <button className="btn primary" style={{ padding: '.3rem .8rem' }} onClick={doUpdate}>⤴️ Update now</button>
      {state === 'error' && <span style={{ color: 'var(--bad)' }}>Update failed — try <code>git pull &amp;&amp; docker compose up -d --build</code>.</span>}
      <span className="spacer" />
      <button className="chip" onClick={() => setHidden(true)}>dismiss</button>
    </div>
  )
}

// ---------- Onboarding ----------
function Onboarding({ onClose }: { onClose: () => void }) {
  return (
    <div className="scrim" onClick={onClose}>
      <div className="card modal pad stack fadein" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 470 }}>
        <div className="between"><b>👋 Welcome to AIWerkstatt</b><button className="chip" onClick={onClose}>✕</button></div>
        <ol className="stack" style={{ paddingLeft: '1.1rem', gap: '.5rem', margin: 0 }}>
          <li><b>Connect a provider.</b> Open <b>⚙️ Providers</b> and add a Claude, OpenAI or Gemini key — or just use the zero-key <b>Demo</b>.</li>
          <li><b>Create a project.</b> Give it a name and a first idea; the agent starts building right away.</li>
          <li><b>Chat with it.</b> Follow up in plain words, watch the live activity, open the app it builds.</li>
          <li><b>Publish when ready.</b> Leak-scan a project and push it to GitHub — or download it as a zip — from its page.</li>
        </ol>
        <button className="btn primary" onClick={onClose}>Let's go 🚀</button>
      </div>
    </div>
  )
}

// ---------- Live activity strip (project view) ----------
function ActivityStrip({ id }: { id: string }) {
  const [items, setItems] = useState<ActivityItem[]>([])
  useEffect(() => {
    let alive = true
    const f = () => api.getActivity(id).then((r) => { if (alive) setItems(r.activity) }).catch(() => {})
    f(); const t = setInterval(f, 3000); return () => { alive = false; clearInterval(t) }
  }, [id])
  const live = items.filter((i) => i.status === 'working' || i.status === 'queued' || i.session?.alive)
  if (live.length === 0) return null
  return (
    <div className="card pad stack" style={{ marginBottom: '1rem', borderColor: '#fbbf2455', gap: '.4rem' }}>
      <div className="row" style={{ fontSize: '.8rem' }}><Working color="var(--warn)" /><b style={{ color: 'var(--warn)' }}>Live now</b></div>
      {live.map((i) => (
        <div key={i.thread_id} className="between" style={{ fontSize: '.82rem' }}>
          <span className="row"><b>{i.title}</b>
            {i.session?.alive && <span className="muted">· context {i.session.ctx_pct}%</span>}</span>
          <span className="muted ticker">{i.last ? i.last.text : (STATUS[i.status]?.label || '')}</span>
        </div>
      ))}
    </div>
  )
}

// ---------- Publish & release (project view) ----------
function PublishPanel({ id, initial, isAdmin }: { id: string; initial: PublishStatus; isAdmin: boolean }) {
  const [open, setOpen] = useState(false)
  const [gh, setGh] = useState<GithubStatus | null>(null)
  const [pub, setPub] = useState<PublishStatus>(initial || {})
  const [scan, setScan] = useState<ScanResult | null>(null)
  const [repo, setRepo] = useState(id)
  const [priv, setPriv] = useState(false)
  const [token, setToken] = useState('')
  const [version, setVersion] = useState('v1.0.0')
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const loadGh = useCallback(() => api.getGithub().then(setGh).catch(() => {}), [])
  useEffect(() => { if (open) loadGh() }, [open, loadGh])
  const wrap = (k: string, fn: () => Promise<void>) => async () => {
    setBusy(k); setErr(null); setMsg(null)
    try { await fn() } catch (e) { setErr(String((e as Error).message)) } finally { setBusy(null) }
  }
  const doScan = wrap('scan', async () => { const r = await api.scanPublish(id); setScan(r.scan) })
  const connect = wrap('gh', async () => { await api.connectGithub(token.trim()); setToken(''); await loadGh() })
  const publishNow = wrap('publish', async () => { const r = await api.publishProject(id, repo.trim(), priv); setPub(r.status); setMsg(`Published → ${r.html_url}`) })
  const releaseNow = wrap('release', async () => { const r = await api.releaseProject(id, version.trim(), ''); setPub(r.status); setMsg(`Released ${r.tag}`) })
  return (
    <div className="card pad stack" style={{ marginTop: '1.4rem' }}>
      <button className="between" style={{ background: 'transparent', border: 0, color: 'var(--text)', cursor: 'pointer', font: 'inherit', width: '100%', padding: 0 }} onClick={() => setOpen((v) => !v)}>
        <b>🌐 Publish &amp; release</b>
        <span className="chip muted">{pub.html_url ? `published · ${pub.visibility || ''}` : (open ? 'hide' : 'open')}</span>
      </button>
      {open && (
        <>
          <div className="muted" style={{ fontSize: '.82rem' }}>Share the app this project built — as a zip, or straight to GitHub. It's leak-scanned first so secrets never ship.</div>

          <div className="row" style={{ flexWrap: 'wrap' }}>
            <button className="btn" disabled={busy === 'scan'} onClick={doScan}>{busy === 'scan' ? 'scanning…' : '🔍 Scan for secrets'}</button>
            <a className="btn ghost" href={api.exportUrl(id)}>⬇️ Download .zip</a>
          </div>
          {scan && (
            <div className="card pad" style={{ background: 'var(--surface-2)', fontSize: '.82rem' }}>
              {scan.blocking === 0
                ? <span style={{ color: 'var(--good)' }}>✓ Clean — no blocking secrets found{scan.review ? ` (${scan.review} to review)` : ''}.</span>
                : <span style={{ color: 'var(--bad)' }}>✕ {scan.blocking} blocking issue(s) — fix before publishing:</span>}
              {scan.findings.slice(0, 6).map((f, i) => (
                <div key={i} className="muted" style={{ marginTop: '.2rem' }}>{f.severity === 'block' ? '🔴' : '🟡'} {f.file}:{f.line} · {f.rule}</div>
              ))}
            </div>
          )}

          <div className="card pad stack" style={{ background: 'var(--surface-2)' }}>
            <div className="between"><b style={{ fontSize: '.9rem' }}>GitHub</b>
              <span className="chip" style={{ color: gh?.connected ? 'var(--good)' : 'var(--muted)' }}>{gh?.connected ? `● ${gh.login}` : '○ not connected'}</span></div>
            {!gh?.connected ? (
              isAdmin ? (
                <>
                  <input className="input" type="password" placeholder="Paste a GitHub token (repo scope)" value={token} onChange={(e) => setToken(e.target.value)} />
                  <div className="row">
                    <button className="btn primary" disabled={busy === 'gh' || token.length < 20} onClick={connect}>{busy === 'gh' ? 'connecting…' : 'Connect GitHub'}</button>
                    <a className="chip" href="https://github.com/settings/tokens/new?scopes=repo&description=AIWerkstatt" target="_blank" rel="noreferrer">Create a token ↗</a>
                  </div>
                </>
              ) : <div className="muted" style={{ fontSize: '.82rem' }}>Ask an admin to connect a GitHub account, then you can publish here.</div>
            ) : (
              <>
                <div className="row" style={{ flexWrap: 'wrap' }}>
                  <input className="input" style={{ width: 'auto', flex: 1 }} placeholder="repository name" value={repo} onChange={(e) => setRepo(e.target.value)} />
                  <label className="row" style={{ fontSize: '.82rem' }}><input type="checkbox" checked={priv} onChange={(e) => setPriv(e.target.checked)} /> private</label>
                </div>
                <button className="btn primary" disabled={busy === 'publish'} onClick={publishNow}>{busy === 'publish' ? 'publishing…' : (pub.html_url ? '⤴️ Push update to GitHub' : '🚀 Publish to GitHub')}</button>
              </>
            )}
          </div>

          {pub.html_url && (
            <div className="card pad stack" style={{ background: 'var(--surface-2)' }}>
              <div className="between"><b style={{ fontSize: '.9rem' }}>Release</b>
                {pub.version && <span className="chip" style={{ color: 'var(--good)' }}>{pub.version}</span>}</div>
              <a className="chip" href={pub.html_url} target="_blank" rel="noreferrer">📦 {pub.repo} ↗</a>
              <div className="row">
                <input className="input" style={{ width: 'auto' }} value={version} onChange={(e) => setVersion(e.target.value)} />
                <button className="btn" disabled={busy === 'release'} onClick={releaseNow}>{busy === 'release' ? 'releasing…' : '🏷️ Tag a release'}</button>
              </div>
              {pub.release_url && <a className="chip" href={pub.release_url} target="_blank" rel="noreferrer">latest release ↗</a>}
            </div>
          )}

          {err && <div style={{ color: 'var(--bad)', fontSize: '.85rem' }}>{err}</div>}
          {msg && <div style={{ color: 'var(--good)', fontSize: '.85rem' }}>{msg}</div>}
          <div className="muted" style={{ fontSize: '.72rem' }}>The app you built is yours. Publishing pushes your project's files to a repository you own.</div>
        </>
      )}
    </div>
  )
}

// ---------- App shell ----------
type View = { v: 'gallery' } | { v: 'project'; id: string } | { v: 'thread'; tid: number; accent: string }

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [view, setView] = useState<View>({ v: 'gallery' })
  const [showProviders, setShowProviders] = useState(false)
  const [showUsage, setShowUsage] = useState(false)
  const [showUsers, setShowUsers] = useState(false)
  const [showAccount, setShowAccount] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [onboard, setOnboard] = useState(() => !localStorage.getItem('aiw_onboarded'))
  const [version, setVersion] = useState('')
  const refreshMe = useCallback(() => api.getMe().then(setMe).catch(() => setMe({ logged_in: false, users: [] })), [])
  useEffect(() => { refreshMe() }, [refreshMe])
  useEffect(() => { api.getHealth().then((h) => setVersion(h.version)).catch(() => {}) }, [])

  if (!me) return <div className="center muted">Loading…</div>
  if (!me.logged_in) return <Login users={me.users} onDone={refreshMe} version={version} />

  const user: User = me.user
  const isAdmin = user.role === 'admin'
  const closeOnboard = () => { setOnboard(false); setShowHelp(false); localStorage.setItem('aiw_onboarded', '1') }
  return (
    <>
      <div className="hdr">
        <span className="emoji" style={{ fontSize: '1.4rem', cursor: 'pointer' }} onClick={() => setView({ v: 'gallery' })}>🛠️</span>
        <div className="brand" style={{ cursor: 'pointer' }} onClick={() => setView({ v: 'gallery' })}>AIWerkstatt<small>self-hosted AI app workshop</small></div>
        <div className="spacer" />
        <button className="chip" onClick={() => setShowProviders(true)}>⚙️ Providers</button>
        <button className="chip" onClick={() => setShowUsage(true)}>📊 Usage</button>
        {isAdmin && <button className="chip" onClick={() => setShowUsers(true)}>👥 Users</button>}
        <button className="chip" title="Help" onClick={() => setShowHelp(true)}>❔</button>
        <button className="chip" title="Account · change PIN" onClick={() => setShowAccount(true)}>{user.emoji} {user.name}</button>
        {version && <span className="chip muted" title="Running version">v{version}</span>}
        <button className="chip" onClick={() => api.logout().then(refreshMe)}>Sign out</button>
      </div>
      {isAdmin && <UpdateBanner />}
      <LimitBanner />
      {view.v === 'gallery' && <Gallery onOpen={(id) => setView({ v: 'project', id })} />}
      {view.v === 'project' && <ProjectView id={view.id} isAdmin={isAdmin} onBack={() => setView({ v: 'gallery' })}
        onOpenThread={(tid) => setView({ v: 'thread', tid, accent: '#7c6cf0' })} />}
      {view.v === 'thread' && <ThreadView tid={view.tid} accent={view.accent} onBack={() => { const cur = view; setView({ v: 'gallery' }); void cur }} />}
      {showProviders && <ProvidersModal onClose={() => setShowProviders(false)} isAdmin={isAdmin} />}
      {showUsage && <UsageModal onClose={() => setShowUsage(false)} />}
      {showUsers && isAdmin && <AdminUsersModal meName={user.name} onClose={() => setShowUsers(false)} />}
      {showAccount && <ChangePin forced={false} onDone={() => { setShowAccount(false); refreshMe() }} onClose={() => setShowAccount(false)} />}
      {(onboard || showHelp) && !user.must_change && <Onboarding onClose={closeOnboard} />}
      {user.must_change && <ChangePin forced onDone={refreshMe} />}
    </>
  )
}
