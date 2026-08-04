"use client"

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Meter } from '../components/Meters'

type Task = { id: string; goal: string; status: string; current_state_json: any; budget_json: any; workspace_path: string; created_at: string; updated_at: string }
type TaskEvent = { id: string; event_type: string; payload_json: any; created_at: string }
type Artifact = { id: string; task_id: string; path: string; kind: string; metadata_json: any; created_at: string }
type ArtifactContent = { id: string; path: string; kind: string; mime: string; content?: string; binary?: boolean; message?: string }
type Snapshot = { id: string; iteration: number; confidence: number; created_at: string }
type MCPServer = { name: string; url: string; transport: string; enabled: boolean }
type ToolConfig = { llm: boolean; filesystem: boolean; code_interpreter: boolean; headless_browser: boolean; mcp: boolean; dangerous_actions: boolean; mcp_servers: MCPServer[] }
type AgentSettings = Record<string, string | number | boolean>

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''
const STATUS_LABELS: Record<string, string> = { PENDING: 'В очереди', RUNNING: 'В работе', PAUSED: 'Пауза', AWAITING_USER: 'Ждет человека', SLEEPING: 'Спит / лимит', FAILED: 'Ошибка', COMPLETED: 'Завершено', ROLLED_BACK: 'Откат выполнен' }
const TOOL_LABELS: Record<keyof Omit<ToolConfig, 'mcp_servers'>, { title: string; description: string }> = {
  llm: { title: 'LLM', description: 'Планирование, исполнение, критика и summaries.' },
  filesystem: { title: 'Файловая система', description: 'Чтение/запись scratchpad и артефактов.' },
  code_interpreter: { title: 'Code Interpreter', description: 'Python sandbox для проверок и вычислений.' },
  headless_browser: { title: 'Headless Browser', description: 'Веб-серфинг. Runtime еще в разработке.' },
  mcp: { title: 'MCP', description: 'Внешние Model Context Protocol серверы.' },
  dangerous_actions: { title: 'Опасные действия', description: 'Удаление, публикация, платежи и production-операции.' },
}

function statusLabel(status?: string) { return status ? STATUS_LABELS[status] || status : 'НЕТ ЗАДАЧИ' }
function isActive(status?: string) { return status === 'PENDING' || status === 'RUNNING' }

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } })
  if (!response.ok) {
    const body = await response.text().catch(() => '')
    throw new Error(`API ${response.status}: ${body || response.statusText}`)
  }
  return response.json() as Promise<T>
}

export default function Home() {
  const [isDark, setIsDark] = useState(true)
  const [tasks, setTasks] = useState<Task[]>([])
  const [selected, setSelected] = useState<Task | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [artifactContent, setArtifactContent] = useState<ArtifactContent | null>(null)
  const [tools, setTools] = useState<ToolConfig | null>(null)
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [settings, setSettings] = useState<AgentSettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [isLaunching, setIsLaunching] = useState(false)
  const [intervention, setIntervention] = useState('')
  const [rollbackIteration, setRollbackIteration] = useState('')
  const [mcpName, setMcpName] = useState('search')
  const [mcpUrl, setMcpUrl] = useState('http://your-mcp-server:8001/sse')
  const [goal, setGoal] = useState('Подготовьте самостоятельный исследовательский отчет об архитектуре AetherMind.')

  const theme = isDark
    ? { page: 'bg-gradient-to-br from-[#070b16] to-[#111a33] text-slate-100', card: 'border-slate-800 bg-slate-950/70', soft: 'bg-slate-900 text-slate-300 border-slate-700', text: 'text-slate-300', title: 'text-white', input: 'border-slate-700 bg-slate-900 text-white', code: 'bg-slate-900 text-emerald-200' }
    : { page: 'bg-gradient-to-br from-sky-50 to-slate-100 text-slate-950', card: 'border-slate-200 bg-white/85 shadow-sm', soft: 'bg-slate-100 text-slate-700 border-slate-200', text: 'text-slate-600', title: 'text-slate-950', input: 'border-slate-300 bg-white text-slate-950', code: 'bg-slate-100 text-slate-900' }

  const load = useCallback(async () => {
    setIsLoading(true)
    try {
      const [data, appSettings] = await Promise.all([apiFetch<Task[]>('/api/tasks'), apiFetch<AgentSettings>('/api/settings')])
      setTasks(data); setSettings(appSettings); setError(null); setSelected((current) => current ?? data[0] ?? null)
    } catch (err) { setError(err instanceof Error ? err.message : 'Backend недоступен') }
    finally { setIsLoading(false) }
  }, [])

  const refreshSelected = useCallback(async (id: string) => {
    try {
      const [task, taskEvents, taskArtifacts, taskTools, taskSnapshots] = await Promise.all([
        apiFetch<Task>(`/api/tasks/${id}`), apiFetch<TaskEvent[]>(`/api/tasks/${id}/events`), apiFetch<Artifact[]>(`/api/tasks/${id}/artifacts`), apiFetch<ToolConfig>(`/api/tasks/${id}/tools`), apiFetch<Snapshot[]>(`/api/tasks/${id}/snapshots`),
      ])
      setSelected(task); setEvents(taskEvents); setArtifacts(taskArtifacts); setTools(taskTools); setSnapshots(taskSnapshots); setError(null)
      if (!isActive(task.status)) setIsLaunching(false)
    } catch (err) { setError(err instanceof Error ? err.message : 'Backend недоступен') }
  }, [])

  async function createTask() {
    if (isLaunching || !goal.trim()) return
    setIsLaunching(true)
    try {
      const task = await apiFetch<Task>('/api/tasks', { method: 'POST', body: JSON.stringify({ goal }) })
      setSelected(task); setArtifactContent(null); setEvents([]); setArtifacts([]); await load(); await refreshSelected(task.id)
    } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось создать задачу'); setIsLaunching(false) }
  }

  async function action(name: string) { if (!selected) return; try { await apiFetch<Task>(`/api/tasks/${selected.id}/${name}`, { method: 'POST' }); await refreshSelected(selected.id) } catch (err) { setError(err instanceof Error ? err.message : `Не удалось выполнить действие ${name}`) } }
  async function viewArtifact(artifact: Artifact) { if (!selected) return; try { setArtifactContent(await apiFetch<ArtifactContent>(`/api/tasks/${selected.id}/artifacts/${artifact.id}/content`)) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось открыть артефакт') } }
  async function toggleTool(key: keyof Omit<ToolConfig, 'mcp_servers'>) { if (!selected || !tools) return; const next = { ...tools, [key]: !tools[key] }; try { const saved = await apiFetch<ToolConfig>(`/api/tasks/${selected.id}/tools`, { method: 'PUT', body: JSON.stringify(next) }); setTools(saved); await refreshSelected(selected.id) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось обновить инструменты') } }
  async function sendIntervention(resume = true) { if (!selected || !intervention.trim()) return; try { await apiFetch<Task>(`/api/tasks/${selected.id}/intervene`, { method: 'POST', body: JSON.stringify({ message: intervention, resume }) }); setIntervention(''); await refreshSelected(selected.id) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось отправить вмешательство') } }
  async function rollback() { if (!selected) return; try { await apiFetch<Task>(`/api/tasks/${selected.id}/rollback`, { method: 'POST', body: JSON.stringify({ iteration: rollbackIteration ? Number(rollbackIteration) : undefined, new_instruction: intervention || undefined }) }); await refreshSelected(selected.id) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось выполнить rollback') } }
  async function addMcpServer() { if (!selected || !mcpName.trim() || !mcpUrl.trim()) return; try { const saved = await apiFetch<ToolConfig>(`/api/tasks/${selected.id}/mcp`, { method: 'POST', body: JSON.stringify({ name: mcpName, url: mcpUrl, transport: 'sse', enabled: true }) }); setTools(saved); await refreshSelected(selected.id) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось добавить MCP сервер') } }
  async function deleteMcpServer(name: string) { if (!selected) return; try { const saved = await apiFetch<ToolConfig>(`/api/tasks/${selected.id}/mcp/${encodeURIComponent(name)}`, { method: 'DELETE' }); setTools(saved); await refreshSelected(selected.id) } catch (err) { setError(err instanceof Error ? err.message : 'Не удалось удалить MCP сервер') } }

  useEffect(() => { load() }, [load])
  useEffect(() => { if (!selected) return; refreshSelected(selected.id); const timer = setInterval(() => refreshSelected(selected.id), isActive(selected.status) ? 1000 : 3000); return () => clearInterval(timer) }, [selected?.id, selected?.status, refreshSelected])
  useEffect(() => {
    if (!selected) return
    const source = new EventSource(`${API_BASE}/api/tasks/${selected.id}/stream`)
    source.addEventListener('task_event', () => refreshSelected(selected.id))
    source.onerror = () => source.close()
    return () => source.close()
  }, [selected?.id, refreshSelected])

  const plan = selected?.current_state_json?.plan || []
  const activeStep = plan.find((node: any) => node.status === 'running')
  const confidence = (selected?.current_state_json?.confidence ?? 1) * 100
  const budget = selected?.budget_json || {}; const iter = selected?.current_state_json?.iteration || 0
  const llmCalls = selected?.current_state_json?.llm_usage?.calls || budget.llm_calls || 0
  const tokensUsed = selected?.current_state_json?.llm_usage?.tokens_used || budget.tokens_used || 0
  const contextFill = useMemo(() => Math.min(100, ((iter % 5) / 5) * 100), [iter])
  const launchDisabled = isLaunching || !goal.trim()

  return <main className={`min-h-screen p-6 transition-colors ${theme.page}`}>
    <header className="mb-6 flex flex-wrap items-center justify-between gap-3"><div><h1 className={`text-3xl font-bold ${theme.title}`}>AetherMind: Центр управления</h1><p className={theme.text}>Автономный итерационный движок</p></div><div className="flex items-center gap-3"><button onClick={() => setIsDark(v => !v)} className={`rounded-full border px-4 py-2 text-sm ${theme.soft}`}>{isDark ? '☀️ День' : '🌙 Ночь'}</button><div className="rounded-full border border-sky-400/50 px-4 py-2 text-sky-500">{isLoading ? 'СИНХРОНИЗАЦИЯ' : statusLabel(selected?.status)}</div></div></header>
    {error && <div className="mb-6 rounded-2xl border border-red-500/40 bg-red-500/10 p-4 text-red-500"><div className="font-semibold">Проблема соединения или выполнения</div><div className="mt-1 text-sm opacity-90">{error}</div></div>}
    {(isLaunching || isActive(selected?.status)) && <div className={`mb-6 rounded-2xl border p-4 ${theme.card}`}><div className="flex items-center gap-3"><span className="h-3 w-3 animate-ping rounded-full bg-sky-400" /><div><div className="font-semibold">Агент работает: LLM выполняет шаги автономного цикла</div><div className={`text-sm ${theme.text}`}>Текущий шаг: {activeStep?.title || 'постановка в очередь / ожидание worker'} · итерация {iter} · LLM вызовы {llmCalls}</div></div></div></div>}
    {selected?.status === 'AWAITING_USER' && <section className="mb-6 rounded-2xl border border-amber-400/50 bg-amber-400/10 p-4"><h2 className="font-semibold text-amber-500">Требуется вмешательство человека</h2><p className={`mt-1 text-sm ${theme.text}`}>Агент остановлен из-за низкой уверенности, ошибки LLM/runtime или отключенного инструмента. Дайте инструкцию и продолжите, либо выполните rollback.</p><textarea value={intervention} onChange={e => setIntervention(e.target.value)} placeholder="Например: включи LLM, продолжай с более строгой проверкой источников..." className={`mt-3 h-24 w-full rounded-xl border p-3 ${theme.input}`} /><div className="mt-3 flex flex-wrap gap-2"><button onClick={() => sendIntervention(true)} className="rounded-lg bg-emerald-400 px-4 py-2 font-semibold text-slate-950">Отправить и продолжить</button><button onClick={() => sendIntervention(false)} className={`rounded-lg border px-4 py-2 ${theme.soft}`}>Сохранить без запуска</button><input value={rollbackIteration} onChange={e => setRollbackIteration(e.target.value)} placeholder="итерация rollback" className={`w-40 rounded-lg border px-3 py-2 ${theme.input}`} /><button onClick={rollback} className="rounded-lg bg-red-500/20 px-4 py-2 text-red-500">Rollback</button></div></section>}
    <section className={`mb-6 grid gap-3 rounded-2xl border p-4 md:grid-cols-[1fr_auto] ${theme.card}`}><input value={goal} onChange={e => setGoal(e.target.value)} className={`rounded-xl border px-4 py-3 outline-none focus:border-sky-400 ${theme.input}`} /><button disabled={launchDisabled} onClick={createTask} className={`rounded-xl px-5 py-3 font-semibold text-slate-950 ${launchDisabled ? 'cursor-not-allowed bg-slate-400 opacity-60' : 'bg-sky-400'}`}>{isLaunching ? 'Запускаю...' : 'Запустить агента'}</button></section>
    <div className="grid gap-6 lg:grid-cols-[280px_1fr_440px]"><aside className={`rounded-2xl border p-4 ${theme.card}`}><h2 className="mb-3 font-semibold">Задачи</h2><div className="space-y-2">{tasks.map(task => <button key={task.id} onClick={() => { setSelected(task); setArtifactContent(null) }} className={`block w-full rounded-xl border p-3 text-left text-sm ${selected?.id === task.id ? 'border-sky-400 bg-sky-400/15 text-sky-500' : theme.soft}`}><div className="truncate">{task.goal}</div><div className="text-xs opacity-70">{statusLabel(task.status)}</div></button>)}</div></aside>
      <section className="space-y-6"><div className={`rounded-2xl border p-4 ${theme.card}`}><h2 className="mb-4 font-semibold">Дерево стратегии</h2><div className="grid gap-3 md:grid-cols-4">{plan.map((node: any) => <div key={node.id} className={`rounded-xl border p-4 ${node.status === 'done' ? 'border-emerald-400 bg-emerald-400/10' : node.status === 'running' ? 'border-sky-400 bg-sky-400/10' : theme.soft}`}><div className="text-xs uppercase opacity-60">{node.status}</div><div className="font-medium">{node.title}</div></div>)}{!plan.length && <div className={`text-sm ${theme.text}`}>Дерево стратегии пока не сформировано.</div>}</div></div>
        <div className={`rounded-2xl border p-4 ${theme.card}`}><div className="mb-4 flex items-center justify-between"><h2 className="font-semibold">Артефакты</h2><span className={`text-xs ${theme.text}`}>{artifacts.length} файлов</span></div><div className="grid gap-3 md:grid-cols-[260px_1fr]"><div className="space-y-2">{artifacts.map(artifact => <div key={artifact.id} className={`rounded-xl border p-3 text-sm ${theme.soft}`}><div className="truncate font-medium">{artifact.path}</div><div className="mb-2 text-xs opacity-70">{artifact.kind}</div><div className="flex gap-2"><button onClick={() => viewArtifact(artifact)} className="rounded-lg bg-sky-400/20 px-3 py-1 text-sky-500">Просмотр</button>{selected && <a href={`${API_BASE}/api/tasks/${selected.id}/artifacts/${artifact.id}/download`} className="rounded-lg bg-emerald-400/20 px-3 py-1 text-emerald-500">Скачать</a>}</div></div>)}{!artifacts.length && <div className={`text-sm ${theme.text}`}>Артефактов пока нет.</div>}</div><div className={`min-h-64 overflow-auto rounded-xl p-4 text-sm ${theme.code}`}>{artifactContent ? artifactContent.binary ? <div>{artifactContent.message}</div> : <pre className="whitespace-pre-wrap">{artifactContent.content}</pre> : <div className={theme.text}>Выберите артефакт для просмотра.</div>}</div></div></div>
        <div className={`rounded-2xl border p-4 ${theme.card}`}><h2 className="mb-3 font-semibold">Настройки агента</h2><pre className={`max-h-80 overflow-auto rounded-xl p-4 text-xs ${theme.code}`}>{JSON.stringify({ app: settings, task_budget: budget, task_state: selected?.current_state_json, tools }, null, 2)}</pre></div></section>
      <aside className="space-y-6"><div className={`rounded-2xl border p-4 ${theme.card}`}><h2 className="mb-4 font-semibold">Контроль и ограничения</h2><div className="space-y-4"><Meter label="Уверенность" value={confidence} color={confidence < 50 ? 'bg-red-500' : 'bg-emerald-400'} /><Meter label="Заполнение контекста" value={contextFill} color="bg-amberMind" /><Meter label="Бюджет итераций" value={(iter / (budget.max_iterations || 25)) * 100} /><div className={`rounded-xl border p-3 text-xs ${theme.soft}`}>LLM вызовы: {llmCalls} · токены: {tokensUsed}</div><div className="grid grid-cols-2 gap-2"><button onClick={() => action('pause')} className="rounded-lg bg-amberMind/20 px-3 py-2 text-amberMind">Пауза</button><button onClick={() => action('resume')} className="rounded-lg bg-emerald-400/20 px-3 py-2 text-emerald-500">Продолжить</button></div></div></div>
        <div className={`rounded-2xl border p-4 ${theme.card}`}><h2 className="mb-4 font-semibold">Инструменты агента</h2><div className="space-y-3">{tools && (Object.keys(TOOL_LABELS) as Array<keyof Omit<ToolConfig, 'mcp_servers'>>).map(key => <div key={key} className={`rounded-xl border p-3 ${theme.soft}`}><div className="flex items-center justify-between gap-3"><div><div className="font-medium">{TOOL_LABELS[key].title}</div><div className="text-xs opacity-70">{TOOL_LABELS[key].description}</div></div><button onClick={() => toggleTool(key)} className={`rounded-full px-3 py-1 text-xs font-semibold ${tools[key] ? 'bg-emerald-400 text-slate-950' : 'bg-slate-500/30 text-slate-400'}`}>{tools[key] ? 'Вкл' : 'Выкл'}</button></div></div>)}<div className={`rounded-xl border p-3 ${theme.soft}`}><div className="mb-2 font-medium">Добавить внешний MCP сервер</div><div className="grid gap-2"><input value={mcpName} onChange={e => setMcpName(e.target.value)} className={`rounded-lg border px-3 py-2 ${theme.input}`} placeholder="имя" /><input value={mcpUrl} onChange={e => setMcpUrl(e.target.value)} className={`rounded-lg border px-3 py-2 ${theme.input}`} placeholder="http://server:8001/sse" /><button onClick={addMcpServer} className="rounded-lg bg-sky-400 px-3 py-2 font-semibold text-slate-950">Подключить MCP</button></div>{tools?.mcp_servers?.map(server => <div key={server.name} className="mt-2 flex items-center justify-between text-xs"><span>{server.name}: {server.url}</span><button onClick={() => deleteMcpServer(server.name)} className="text-red-500">удалить</button></div>)}</div>{!tools && <div className={`text-sm ${theme.text}`}>Выберите задачу для управления инструментами.</div>}</div></div>
        <div className={`rounded-2xl border p-4 ${theme.card}`}><h2 className="mb-4 font-semibold">Checkpoint / rollback</h2><div className="max-h-40 space-y-2 overflow-auto">{snapshots.slice(-8).reverse().map(s => <button key={s.id} onClick={() => setRollbackIteration(String(s.iteration))} className={`block w-full rounded-lg border p-2 text-left text-xs ${theme.soft}`}>Итерация {s.iteration} · confidence {Number(s.confidence).toFixed(2)}</button>)}{!snapshots.length && <div className={`text-sm ${theme.text}`}>Снапшотов пока нет.</div>}</div></div>
        <div className={`rounded-2xl border p-4 ${theme.card}`}><h2 className="mb-4 font-semibold">Живой trace</h2><div className="max-h-[520px] space-y-2 overflow-auto">{events.map(event => <div key={event.id} className={`rounded-lg border p-3 text-sm ${theme.soft}`}><div className="text-xs text-sky-500">{event.event_type}</div><div>{event.payload_json?.message || JSON.stringify(event.payload_json)}</div></div>)}{!events.length && <div className={`text-sm ${theme.text}`}>Событий пока нет.</div>}</div></div></aside></div>
  </main>
}
