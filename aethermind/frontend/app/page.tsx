"use client"

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Meter } from '../components/Meters'

type Task = {
  id: string
  goal: string
  status: string
  current_state_json: any
  budget_json: any
  workspace_path: string
  created_at: string
  updated_at: string
}

type TaskEvent = { id: string; event_type: string; payload_json: any; created_at: string }
type Artifact = { id: string; task_id: string; path: string; kind: string; metadata_json: any; created_at: string }
type ArtifactContent = { id: string; path: string; kind: string; mime: string; content?: string; binary?: boolean; message?: string }
type Snapshot = { id: string; iteration: number; confidence: number; created_at: string }
type MCPServer = { name: string; url: string; transport: string; enabled: boolean }
type ToolConfig = {
  llm: boolean
  filesystem: boolean
  code_interpreter: boolean
  headless_browser: boolean
  mcp: boolean
  dangerous_actions: boolean
  mcp_servers: MCPServer[]
}
type MCPTool = {
  server_name: string
  server_url?: string
  name?: string
  title?: string
  description?: string
  input_schema?: any
  status: 'ok' | 'error'
  error?: string
  internal?: boolean
}
type MCPToolsResponse = { enabled: boolean; tools: MCPTool[]; message?: string }
type MCPCallResponse = { result: any; artifact?: { id: string; path: string; kind: string } }
type AgentSettings = Record<string, string | number | boolean>

type Theme = {
  page: string
  card: string
  soft: string
  text: string
  title: string
  input: string
  code: string
  mutedPanel: string
}

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

const STATUS_LABELS: Record<string, string> = {
  PENDING: 'В очереди',
  RUNNING: 'В работе',
  PAUSED: 'Пауза',
  AWAITING_USER: 'Ждет человека',
  SLEEPING: 'Спит / лимит',
  FAILED: 'Ошибка',
  COMPLETED: 'Завершено',
  ROLLED_BACK: 'Откат выполнен',
}

const TOOL_LABELS: Record<keyof Omit<ToolConfig, 'mcp_servers'>, { title: string; description: string }> = {
  llm: { title: 'LLM', description: 'Планирование, исполнение, критика и summaries.' },
  filesystem: { title: 'Файловая система', description: 'Чтение/запись scratchpad и артефактов.' },
  code_interpreter: { title: 'Code Interpreter', description: 'Python sandbox для проверок и вычислений.' },
  headless_browser: { title: 'Headless Browser', description: 'Веб-серфинг. Runtime еще в разработке.' },
  mcp: { title: 'MCP', description: 'Внешние Model Context Protocol серверы.' },
  dangerous_actions: { title: 'Опасные действия', description: 'Удаление, публикация, платежи и production-операции.' },
}

const DEFAULT_TOOLS: ToolConfig = {
  llm: true,
  filesystem: true,
  code_interpreter: true,
  headless_browser: false,
  mcp: true,
  dangerous_actions: false,
  mcp_servers: [],
}

function statusLabel(status?: string) {
  return status ? STATUS_LABELS[status] || status : 'НЕТ ЗАДАЧИ'
}

function isAgentActive(status?: string) {
  return status === 'PENDING' || status === 'RUNNING'
}

function compactJson(value: unknown) {
  return JSON.stringify(value, null, 2)
}

function templateFromSchema(schema: any): Record<string, unknown> {
  const props = schema?.properties || {}
  const required: string[] = schema?.required || Object.keys(props)
  const template: Record<string, unknown> = {}
  for (const key of required.length ? required : Object.keys(props)) {
    const prop = props[key] || {}
    if (prop.default !== undefined) template[key] = prop.default
    else if (prop.type === 'integer' || prop.type === 'number') template[key] = key === 'max_chars' ? 12000 : 1
    else if (prop.type === 'boolean') template[key] = true
    else if (prop.type === 'array') template[key] = []
    else if (prop.type === 'object') template[key] = {}
    else if (key.toLowerCase().includes('url')) template[key] = 'https://example.com'
    else if (key.toLowerCase().includes('query')) template[key] = 'AetherMind autonomous agent'
    else if (key.toLowerCase().includes('code')) template[key] = 'print(2 + 2)'
    else template[key] = ''
  }
  return template
}

function missingRequiredArgs(schema: any, args: any): string[] {
  const required: string[] = schema?.required || []
  return required.filter((key) => args?.[key] === undefined || args?.[key] === '')
}

function escapeHtml(value: string) {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
}

function renderInlineMarkdown(value: string) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\[(.*?)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
}

function isMarkdownTableSeparator(line: string) {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line)
}

function splitMarkdownTableRow(line: string) {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

function markdownToHtml(markdown: string) {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n')
  const html: string[] = []
  let inCode = false
  let codeBuffer: string[] = []
  let inList = false

  const closeList = () => {
    if (inList) {
      html.push('</ul>')
      inList = false
    }
  }

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]

    if (line.trim().startsWith('```')) {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeBuffer.join('\n'))}</code></pre>`)
        codeBuffer = []
        inCode = false
      } else {
        closeList()
        inCode = true
      }
      continue
    }

    if (inCode) {
      codeBuffer.push(line)
      continue
    }

    if (index + 1 < lines.length && line.includes('|') && isMarkdownTableSeparator(lines[index + 1])) {
      closeList()
      const headers = splitMarkdownTableRow(line)
      const rows: string[][] = []
      index += 2
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        rows.push(splitMarkdownTableRow(lines[index]))
        index += 1
      }
      index -= 1
      html.push('<div class="overflow-auto"><table><thead><tr>')
      html.push(headers.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join(''))
      html.push('</tr></thead><tbody>')
      for (const row of rows) {
        html.push('<tr>')
        html.push(headers.map((_, cellIndex) => `<td>${renderInlineMarkdown(row[cellIndex] || '')}</td>`).join(''))
        html.push('</tr>')
      }
      html.push('</tbody></table></div>')
      continue
    }

    if (!line.trim()) {
      closeList()
      html.push('<br />')
      continue
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/)
    if (heading) {
      closeList()
      const level = heading[1].length
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`)
      continue
    }

    const listItem = line.match(/^\s*[-*]\s+(.*)$/)
    if (listItem) {
      if (!inList) {
        html.push('<ul>')
        inList = true
      }
      html.push(`<li>${renderInlineMarkdown(listItem[1])}</li>`)
      continue
    }

    closeList()
    html.push(`<p>${renderInlineMarkdown(line)}</p>`)
  }
  closeList()
  if (inCode) html.push(`<pre><code>${escapeHtml(codeBuffer.join('\n'))}</code></pre>`)
  return html.join('\n')
}

type HumanFeedback = {
  title: string
  currentStep: string
  confidence: number | null
  criticReason: string
  observation: string
  recovery: string
  lastProblems: string[]
  suggestions: Array<{ label: string; text: string; rollback?: boolean }>
}

function stringifyShort(value: unknown, max = 1200) {
  if (value === undefined || value === null) return 'нет данных'
  const text = typeof value === 'string' ? value : compactJson(value)
  return text.length > max ? `${text.slice(0, max)}…` : text
}

function buildHumanFeedback(task: Task | null, events: TaskEvent[]): HumanFeedback {
  const state = task?.current_state_json || {}
  const reflection = state.reflection || {}
  const observation = state.observation || {}
  const humanRequest = state.human_request || {}
  const currentStep = humanRequest.current_step || state.current_step?.title || state.current_step?.id || 'шаг не определен'
  const criticReason = humanRequest.reason || reflection.reason || observation.reason || observation.error || 'Критик не передал подробную причину. Посмотрите observation и последние события ниже.'
  const confidence = typeof humanRequest.confidence === 'number' ? humanRequest.confidence : (typeof state.confidence === 'number' ? state.confidence : null)
  const problemEvents = events
    .filter((event) => ['error', 'mcp_error', 'auto_recovery', 'reflection', 'tools'].includes(event.event_type))
    .slice(0, 5)
    .map((event) => event.payload_json?.message || event.payload_json?.reason || event.payload_json?.error || stringifyShort(event.payload_json, 300))
  const recoveryAttempts = state.auto_recovery_attempts || state.current_step?.retry_count || 0
  const recovery = recoveryAttempts
    ? `Автовосстановление уже пробовало исправить ситуацию: ${recoveryAttempts} попытк(и).`
    : 'Автовосстановление еще не исчерпано или не запускалось.'

  const lowerReason = criticReason.toLowerCase()
  const wantsFiles = /файл|код|excel|word|pdf|csv|json|артефакт|директор/.test(lowerReason)
  const wantsSources = /url|ссыл|источник|данн|поиск|http|fetch/.test(lowerReason)
  const suggestions = [
    wantsFiles
      ? {
          label: 'Создать недостающие файлы',
          text: `Продолжай автономно. Для текущего шага «${currentStep}» создай реальные файлы через MCP_CALL_JSON с __internal__.write_file: минимум artifacts/result.md и при необходимости code/ или data/ файлы. Затем проверь их через __internal__.list_dir и __internal__.read_file. Замечание критика: ${criticReason}.`,
        }
      : {
          label: 'Повторить шаг строже',
          text: `Продолжай автономно и повтори текущий шаг «${currentStep}». Учти замечание критика: ${criticReason}. Создай проверяемый артефакт через __internal__.write_file и не возвращайся к человеку без фатальной ошибки инструмента.`,
        },
    wantsSources
      ? {
          label: 'Проверить источники',
          text: `Продолжай автономно. Используй MCP/fetch для проверки источников или URL, затем сохрани найденные данные в artifacts/sources.md или data/sources.json через __internal__.write_file. Замечание критика: ${criticReason}.`,
        }
      : {
          label: 'Перепланировать локально',
          text: `Не спрашивай человека повторно. Перепланируй текущий шаг «${currentStep}» на 2-3 внутренних действия, выполни их и создай артефакт результата. Причина: ${criticReason}.`,
        },
    {
      label: 'Принять риск и идти дальше',
      text: `Принять текущий риск как допустимый, зафиксировать допущение в scratchpad, пометить текущий шаг выполненным и продолжить выполнение следующего шага. Не блокируй этот же шаг повторно. Причина риска: ${criticReason}.`,
    },
    {
      label: 'Проверить инструменты',
      text: `Продолжай автономно. Проверь доступность инструментов: __internal__.list_dir, __internal__.write_file, __internal__.run_python, MCP discovery/call. Если внешний MCP недоступен, используй внутренние инструменты. Затем повтори шаг «${currentStep}».`,
    },
    {
      label: 'Откатиться на шаг назад',
      rollback: true,
      text: `Выполни rollback на предыдущую устойчивую итерацию и запусти альтернативный план. Ошибка/сомнение критика: ${criticReason}.`,
    },
  ]

  return {
    title: task?.status === 'AWAITING_USER' ? 'Критик просит решение человека' : 'Обратная связь критика',
    currentStep,
    confidence,
    criticReason,
    observation: stringifyShort(observation),
    recovery,
    lastProblems: problemEvents,
    suggestions,
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers })
  if (!response.ok) {
    const body = await response.text().catch(() => '')
    throw new Error(`API ${response.status}: ${body || response.statusText}`)
  }
  return response.json() as Promise<T>
}

function getTheme(isDark: boolean): Theme {
  return isDark
    ? {
        page: 'bg-gradient-to-br from-[#070b16] to-[#111a33] text-slate-100',
        card: 'border-slate-800 bg-slate-950/75 shadow-[0_0_0_1px_rgba(15,23,42,0.6)]',
        soft: 'bg-slate-900/95 text-slate-300 border-slate-700',
        text: 'text-slate-300',
        title: 'text-white',
        input: 'border-slate-700 bg-slate-900 text-white placeholder:text-slate-500',
        code: 'bg-slate-950 text-emerald-200 border-slate-800',
        mutedPanel: 'bg-slate-900/50 border-slate-800',
      }
    : {
        page: 'bg-gradient-to-br from-sky-50 to-slate-100 text-slate-950',
        card: 'border-slate-200 bg-white/90 shadow-sm',
        soft: 'bg-slate-50 text-slate-700 border-slate-200',
        text: 'text-slate-600',
        title: 'text-slate-950',
        input: 'border-slate-300 bg-white text-slate-950 placeholder:text-slate-400',
        code: 'bg-slate-50 text-slate-900 border-slate-200',
        mutedPanel: 'bg-white/60 border-slate-200',
      }
}

export default function Home() {
  const [isDark, setIsDark] = useState(true)
  const [tasks, setTasks] = useState<Task[]>([])
  const [selected, setSelected] = useState<Task | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [artifactContent, setArtifactContent] = useState<ArtifactContent | null>(null)
  const [selectedArtifact, setSelectedArtifact] = useState<Artifact | null>(null)
  const [artifactViewMode, setArtifactViewMode] = useState<'markdown' | 'html'>('markdown')
  const [tools, setTools] = useState<ToolConfig>(DEFAULT_TOOLS)
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [mcpTools, setMcpTools] = useState<MCPTool[]>([])
  const [mcpCallArgs, setMcpCallArgs] = useState('{\n  "url": "https://example.com",\n  "max_chars": 12000\n}')
  const [mcpCallResult, setMcpCallResult] = useState<any>(null)
  const [settings, setSettings] = useState<AgentSettings | null>(null)
  const [goalDraft, setGoalDraft] = useState('')
  const [budgetDraft, setBudgetDraft] = useState('{}')
  const [stateDraft, setStateDraft] = useState('{}')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [isLaunching, setIsLaunching] = useState(false)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [intervention, setIntervention] = useState('')
  const [rollbackIteration, setRollbackIteration] = useState('')
  const [mcpName, setMcpName] = useState('search')
  const [mcpUrl, setMcpUrl] = useState('http://your-mcp-server:8001/sse')
  const [goal, setGoal] = useState('Подготовьте самостоятельный исследовательский отчет об архитектуре AetherMind.')
  const [pendingImages, setPendingImages] = useState<File[]>([])

  const theme = getTheme(isDark)

  useEffect(() => {
    const saved = window.localStorage.getItem('aethermind.theme')
    if (saved === 'light') setIsDark(false)
    if (saved === 'dark') setIsDark(true)
  }, [])

  function setThemePersistent(nextDark: boolean) {
    setIsDark(nextDark)
    window.localStorage.setItem('aethermind.theme', nextDark ? 'dark' : 'light')
    document.cookie = `aethermind.theme=${nextDark ? 'dark' : 'light'}; path=/; max-age=31536000; SameSite=Lax`
  }

  const refreshTasks = useCallback(async () => {
    const [taskList, appSettings] = await Promise.all([
      apiFetch<Task[]>('/api/tasks'),
      apiFetch<AgentSettings>('/api/settings'),
    ])
    setTasks(taskList)
    setSettings(appSettings)
    setSelected((current) => {
      if (!current) return taskList[0] ?? null
      return taskList.find((task) => task.id === current.id) ?? current
    })
  }, [])

  const refreshSelected = useCallback(async (id: string) => {
    const [task, taskEvents, taskArtifacts, taskTools, taskSnapshots, taskMcpTools] = await Promise.all([
      apiFetch<Task>(`/api/tasks/${id}`),
      apiFetch<TaskEvent[]>(`/api/tasks/${id}/events`),
      apiFetch<Artifact[]>(`/api/tasks/${id}/artifacts`),
      apiFetch<ToolConfig>(`/api/tasks/${id}/tools`),
      apiFetch<Snapshot[]>(`/api/tasks/${id}/snapshots`),
      apiFetch<MCPToolsResponse>(`/api/tasks/${id}/mcp/tools`).catch(() => ({ enabled: false, tools: [] })),
    ])
    setSelected(task)
      setEvents([...taskEvents].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)))
      setArtifacts([...taskArtifacts].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)))
    setTools({ ...DEFAULT_TOOLS, ...taskTools, mcp_servers: taskTools.mcp_servers || [] })
    setSnapshots(taskSnapshots)
    setMcpTools(taskMcpTools.tools || [])
  }, [])

  const safeRefreshSelected = useCallback(async (id: string) => {
    try {
      await refreshSelected(id)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Backend недоступен')
    }
  }, [refreshSelected])

  useEffect(() => {
    let cancelled = false
    async function initialLoad() {
      setIsLoading(true)
      try {
        await refreshTasks()
        if (!cancelled) setError(null)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Backend недоступен')
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    initialLoad()
    return () => { cancelled = true }
  }, [refreshTasks])

  useEffect(() => {
    if (!selected) return
    safeRefreshSelected(selected.id)
    const interval = window.setInterval(
      () => safeRefreshSelected(selected.id),
      isAgentActive(selected.status) ? 1000 : 3000,
    )
    return () => window.clearInterval(interval)
  }, [selected?.id, selected?.status, safeRefreshSelected])

  useEffect(() => {
    if (!selected) return
    setGoalDraft(selected.goal)
    setBudgetDraft(compactJson(selected.budget_json || {}))
    setStateDraft(compactJson(selected.current_state_json || {}))
  }, [selected?.id])

  async function uploadImageToTask(taskId: string, file: File) {
    const form = new FormData()
    form.append('file', file)
    const response = await fetch(`${API_BASE}/api/tasks/${taskId}/attachments`, { method: 'POST', body: form })
    if (!response.ok) throw new Error(await response.text())
    return response.json()
  }

  async function createTask() {
    if (isLaunching || !goal.trim()) return
    setIsLaunching(true)
    setError(null)
    setNotice('Задача создается и ставится в очередь Celery...')
    try {
      const task = await apiFetch<Task>('/api/tasks', { method: 'POST', body: JSON.stringify({ goal }) })
      for (const image of pendingImages) {
        await uploadImageToTask(task.id, image)
      }
      setPendingImages([])
      setSelected(task)
      setEvents([])
      setArtifacts([])
      setArtifactContent(null)
      setNotice(pendingImages.length ? `Задача запущена, изображений прикреплено: ${pendingImages.length}.` : 'Задача запущена. Следите за индикатором работы и Live Trace.')
      await refreshTasks()
      await refreshSelected(task.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось создать задачу')
    } finally {
      setIsLaunching(false)
    }
  }

  async function runTaskAction(name: string) {
    if (!selected) return
    setBusyAction(name)
    setError(null)
    try {
      await apiFetch<Task>(`/api/tasks/${selected.id}/${name}`, { method: 'POST' })
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : `Не удалось выполнить действие ${name}`)
    } finally {
      setBusyAction(null)
    }
  }

  async function deleteTask(task: Task) {
    if (!window.confirm(`Удалить задачу «${task.goal}»? Это удалит события, снапшоты и записи артефактов.`)) return
    setBusyAction(`delete_task:${task.id}`)
    try {
      await apiFetch<{ ok: boolean }>(`/api/tasks/${task.id}`, { method: 'DELETE' })
      setNotice('Задача удалена.')
      if (selected?.id === task.id) {
        setSelected(null)
        setEvents([])
        setArtifacts([])
        setArtifactContent(null)
      }
      await refreshTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось удалить задачу')
    } finally {
      setBusyAction(null)
    }
  }

  async function saveTaskSettings() {
    if (!selected) return
    setBusyAction('save_settings')
    try {
      let parsedBudget: any
      let parsedState: any
      try {
        parsedBudget = JSON.parse(budgetDraft)
        parsedState = JSON.parse(stateDraft)
      } catch {
        throw new Error('Budget и State должны быть валидными JSON объектами')
      }
      const updated = await apiFetch<Task>(`/api/tasks/${selected.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ goal: goalDraft, budget_json: parsedBudget, current_state_json: parsedState }),
      })
      setSelected(updated)
      setNotice('Настройки агента и состояние сохранены.')
      await refreshSelected(updated.id)
      await refreshTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось сохранить настройки')
    } finally {
      setBusyAction(null)
    }
  }

  async function sendIntervention(resume: boolean) {
    if (!selected || !intervention.trim()) return
    setBusyAction('intervene')
    try {
      await apiFetch<Task>(`/api/tasks/${selected.id}/intervene`, {
        method: 'POST',
        body: JSON.stringify({ message: intervention, resume }),
      })
      setIntervention('')
      setNotice(resume ? 'Инструкция отправлена, агент продолжит работу.' : 'Инструкция сохранена без запуска.')
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось отправить вмешательство')
    } finally {
      setBusyAction(null)
    }
  }

  async function rollback() {
    if (!selected) return
    setBusyAction('rollback')
    try {
      await apiFetch<Task>(`/api/tasks/${selected.id}/rollback`, {
        method: 'POST',
        body: JSON.stringify({
          iteration: rollbackIteration ? Number(rollbackIteration) : undefined,
          new_instruction: intervention || undefined,
        }),
      })
      setNotice('Rollback выполнен. При необходимости нажмите «Продолжить».')
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось выполнить rollback')
    } finally {
      setBusyAction(null)
    }
  }

  async function viewArtifact(artifact: Artifact) {
    if (!selected) return
    setBusyAction(`artifact:${artifact.id}`)
    try {
      const content = await apiFetch<ArtifactContent>(`/api/tasks/${selected.id}/artifacts/${artifact.id}/content`)
      setSelectedArtifact(artifact)
      setArtifactContent(content)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось открыть артефакт')
    } finally {
      setBusyAction(null)
    }
  }

  async function uploadImageAttachment(file: File) {
    if (!selected) return
    setBusyAction('upload_image')
    try {
      await uploadImageToTask(selected.id, file)
      setNotice(`Изображение прикреплено к контексту: ${file.name}`)
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось прикрепить изображение')
    } finally {
      setBusyAction(null)
    }
  }

  async function saveTools(nextTools: ToolConfig) {
    if (!selected) return
    setBusyAction('tools')
    try {
      const saved = await apiFetch<ToolConfig>(`/api/tasks/${selected.id}/tools`, {
        method: 'PUT',
        body: JSON.stringify(nextTools),
      })
      setTools({ ...DEFAULT_TOOLS, ...saved, mcp_servers: saved.mcp_servers || [] })
      setNotice('Настройки инструментов сохранены.')
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось обновить инструменты')
    } finally {
      setBusyAction(null)
    }
  }

  async function toggleTool(key: keyof Omit<ToolConfig, 'mcp_servers'>) {
    await saveTools({ ...tools, [key]: !tools[key] })
  }

  async function addMcpServer() {
    if (!selected || !mcpName.trim() || !mcpUrl.trim()) return
    if (!/^https?:\/\//.test(mcpUrl.trim())) {
      setError('URL MCP сервера должен начинаться с http:// или https://')
      return
    }
    setBusyAction('mcp')
    setError(null)
    try {
      const saved = await apiFetch<ToolConfig>(`/api/tasks/${selected.id}/mcp`, {
        method: 'POST',
        body: JSON.stringify({ name: mcpName.trim(), url: mcpUrl.trim(), transport: 'sse', enabled: true }),
      })
      setTools({ ...DEFAULT_TOOLS, ...saved, mcp_servers: saved.mcp_servers || [] })
      setNotice(`MCP сервер «${mcpName.trim()}» подключен к задаче.`)
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось добавить MCP сервер')
    } finally {
      setBusyAction(null)
    }
  }

  async function deleteMcpServer(name: string) {
    if (!selected) return
    setBusyAction('mcp')
    try {
      const saved = await apiFetch<ToolConfig>(`/api/tasks/${selected.id}/mcp/${encodeURIComponent(name)}`, { method: 'DELETE' })
      setTools({ ...DEFAULT_TOOLS, ...saved, mcp_servers: saved.mcp_servers || [] })
      setNotice(`MCP сервер «${name}» удален.`)
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось удалить MCP сервер')
    } finally {
      setBusyAction(null)
    }
  }

  async function refreshMcpTools() {
    if (!selected) return
    setBusyAction('mcp_tools')
    try {
      const data = await apiFetch<MCPToolsResponse>(`/api/tasks/${selected.id}/mcp/tools`)
      setMcpTools(data.tools || [])
      setNotice(data.enabled ? `Найдено MCP инструментов: ${(data.tools || []).filter((tool) => tool.status === 'ok').length}` : data.message || 'MCP выключен')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось получить MCP tools')
    } finally {
      setBusyAction(null)
    }
  }

  async function callMcpTool(tool: MCPTool) {
    if (!selected || !tool.name) return
    setBusyAction(`mcp_call:${tool.server_name}:${tool.name}`)
    try {
      let args: any = {}
      try {
        args = mcpCallArgs.trim() ? JSON.parse(mcpCallArgs) : {}
      } catch {
        throw new Error('Аргументы MCP должны быть валидным JSON объектом')
      }
      const missing = missingRequiredArgs(tool.input_schema, args)
      if (missing.length) {
        const template = templateFromSchema(tool.input_schema)
        setMcpCallArgs(compactJson(template))
        throw new Error(`Для ${tool.server_name}.${tool.name} не хватает обязательных аргументов: ${missing.join(', ')}. Я подставил шаблон — проверьте и нажмите «Выполнить» еще раз.`)
      }
      const response = await apiFetch<MCPCallResponse>(`/api/tasks/${selected.id}/mcp/call`, {
        method: 'POST',
        body: JSON.stringify({ server_name: tool.server_name, tool_name: tool.name, arguments: args }),
      })
      setMcpCallResult(response)
      setNotice(`MCP инструмент выполнен: ${tool.server_name}.${tool.name}`)
      await refreshSelected(selected.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось выполнить MCP инструмент')
    } finally {
      setBusyAction(null)
    }
  }

  const plan = selected?.current_state_json?.plan || []
  const activeStep = plan.find((node: any) => node.status === 'running')
  const budget = selected?.budget_json || {}
  const iter = selected?.current_state_json?.iteration || 0
  const confidence = (selected?.current_state_json?.confidence ?? 1) * 100
  const llmCalls = selected?.current_state_json?.llm_usage?.calls || budget.llm_calls || 0
  const tokensUsed = selected?.current_state_json?.llm_usage?.tokens_used || budget.tokens_used || 0
  const contextFill = useMemo(() => Math.min(100, ((iter % 5) / 5) * 100), [iter])
  const humanFeedback = useMemo(() => buildHumanFeedback(selected, events), [selected, events])

  return (
    <main className={`min-h-screen overflow-x-hidden p-4 transition-colors sm:p-6 ${theme.page}`}>
      <div className="mx-auto grid w-full max-w-[1800px] min-w-0 gap-6">
        <header className="flex min-w-0 flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <h1 className={`truncate text-2xl font-bold sm:text-3xl ${theme.title}`}>AetherMind: Центр управления</h1>
            <p className={`${theme.text} truncate`}>Автономный итерационный движок</p>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <button onClick={() => setThemePersistent(!isDark)} className={`rounded-full border px-4 py-2 text-sm ${theme.soft}`}>
              {isDark ? '☀️ День' : '🌙 Ночь'}
            </button>
            <div className="rounded-full border border-sky-400/50 px-4 py-2 text-sky-500">
              {isLoading ? 'СИНХРОНИЗАЦИЯ' : statusLabel(selected?.status)}
            </div>
          </div>
        </header>

        {error && <Banner kind="error" message={error} onClose={() => setError(null)} />}
        {notice && <Banner kind="notice" message={notice} onClose={() => setNotice(null)} />}

        {(isLaunching || isAgentActive(selected?.status)) && (
          <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
            <div className="flex min-w-0 items-center gap-3">
              <span className="relative flex h-3 w-3 shrink-0">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-sky-400 opacity-75" />
                <span className="relative inline-flex h-3 w-3 rounded-full bg-sky-500" />
              </span>
              <div className="min-w-0">
                <div className="font-semibold">Агент работает: LLM выполняет автономный цикл</div>
                <div className={`truncate text-sm ${theme.text}`}>
                  Текущий шаг: {activeStep?.title || 'постановка в очередь / ожидание worker'} · итерация {iter} · LLM вызовы {llmCalls}
                </div>
              </div>
            </div>
          </div>
        )}

        {selected?.status === 'AWAITING_USER' && (
          <HumanGatePanel theme={theme} feedback={humanFeedback} intervention={intervention} setIntervention={setIntervention} rollbackIteration={rollbackIteration} setRollbackIteration={setRollbackIteration} busyAction={busyAction} onSend={sendIntervention} onRollback={rollback} />
        )}

        <section className={`grid min-w-0 gap-3 rounded-2xl border p-4 ${theme.card}`}>
          <div className="grid min-w-0 gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
            <input value={goal} onChange={(event) => setGoal(event.target.value)} className={`min-w-0 rounded-xl border px-4 py-3 outline-none focus:border-sky-400 ${theme.input}`} />
            <button disabled={isLaunching || !goal.trim()} onClick={createTask} className={`rounded-xl px-5 py-3 font-semibold text-slate-950 ${isLaunching || !goal.trim() ? 'cursor-not-allowed bg-slate-400 opacity-60' : 'bg-sky-400 hover:bg-sky-300'}`}>
              {isLaunching ? 'Запускаю...' : 'Запустить агента'}
            </button>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <label className="cursor-pointer rounded-lg bg-sky-400/20 px-3 py-2 text-xs text-sky-500">
              📎 Изображения к стартовому контексту
              <input type="file" accept="image/*" multiple className="hidden" onChange={(event) => { const files = Array.from(event.target.files || []); setPendingImages((current) => [...current, ...files]); event.currentTarget.value = '' }} />
            </label>
            {pendingImages.map((file, index) => (
              <span key={`${file.name}-${index}`} className={`rounded-lg border px-2 py-1 text-xs ${theme.soft}`}>
                {file.name}
                <button onClick={() => setPendingImages((current) => current.filter((_, itemIndex) => itemIndex !== index))} className="ml-2 text-red-500">×</button>
              </span>
            ))}
            {!pendingImages.length && <span className={`text-xs ${theme.text}`}>Можно прикрепить изображения до запуска задачи — они попадут в контекст агента.</span>}
          </div>
        </section>

        <div className="grid min-w-0 gap-6 xl:grid-cols-[260px_minmax(0,1fr)_380px] 2xl:grid-cols-[280px_minmax(0,1fr)_420px]">
          <TasksPanel tasks={tasks} selected={selected} theme={theme} busyAction={busyAction} onSelect={(task) => { setSelected(task); setArtifactContent(null) }} onDelete={deleteTask} />
          <section className="grid min-w-0 content-start gap-6">
            <StrategyPanel plan={plan} theme={theme} />
            <ArtifactsPanel artifacts={artifacts} artifactContent={artifactContent} selectedArtifact={selectedArtifact} artifactViewMode={artifactViewMode} selected={selected} theme={theme} busyAction={busyAction} onView={viewArtifact} onViewModeChange={setArtifactViewMode} onUploadImage={uploadImageAttachment} />
            <SettingsPanel settings={settings} selected={selected} budget={budget} tools={tools} theme={theme} goalDraft={goalDraft} budgetDraft={budgetDraft} stateDraft={stateDraft} busyAction={busyAction} setGoalDraft={setGoalDraft} setBudgetDraft={setBudgetDraft} setStateDraft={setStateDraft} onSave={saveTaskSettings} />
          </section>
          <aside className="grid min-w-0 content-start gap-6">
            <ControlPanel theme={theme} confidence={confidence} contextFill={contextFill} iter={iter} budget={budget} llmCalls={llmCalls} tokensUsed={tokensUsed} busyAction={busyAction} onPause={() => runTaskAction('pause')} onResume={() => runTaskAction('resume')} />
            <TracePanel theme={theme} events={events} />
            <ToolsPanel theme={theme} tools={tools} mcpTools={mcpTools} mcpCallArgs={mcpCallArgs} mcpCallResult={mcpCallResult} busyAction={busyAction} mcpName={mcpName} mcpUrl={mcpUrl} setMcpName={setMcpName} setMcpUrl={setMcpUrl} setMcpCallArgs={setMcpCallArgs} onToggle={toggleTool} onAddMcp={addMcpServer} onDeleteMcp={deleteMcpServer} onRefreshMcpTools={refreshMcpTools} onCallMcpTool={callMcpTool} />
            <SnapshotsPanel theme={theme} snapshots={snapshots} setRollbackIteration={setRollbackIteration} />
          </aside>
        </div>
      </div>
    </main>
  )
}

function Banner({ kind, message, onClose }: { kind: 'error' | 'notice'; message: string; onClose: () => void }) {
  const cls = kind === 'error' ? 'border-red-500/40 bg-red-500/10 text-red-500' : 'border-sky-400/40 bg-sky-400/10 text-sky-500'
  return (
    <div className={`flex min-w-0 items-start justify-between gap-3 rounded-2xl border p-4 ${cls}`}>
      <div className="min-w-0 break-words text-sm"><span className="font-semibold">{kind === 'error' ? 'Проблема: ' : 'Инфо: '}</span>{message}</div>
      <button onClick={onClose} className="shrink-0 text-xs opacity-70 hover:opacity-100">закрыть</button>
    </div>
  )
}

function HumanGatePanel({
  theme,
  feedback,
  intervention,
  setIntervention,
  rollbackIteration,
  setRollbackIteration,
  busyAction,
  onSend,
  onRollback,
}: {
  theme: Theme
  feedback: HumanFeedback
  intervention: string
  setIntervention: (value: string) => void
  rollbackIteration: string
  setRollbackIteration: (value: string) => void
  busyAction: string | null
  onSend: (resume: boolean) => void
  onRollback: () => void
}) {
  return (
    <section className="min-w-0 rounded-2xl border border-amber-400/50 bg-amber-400/10 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-amber-500">{feedback.title}</h2>
          <p className={`mt-1 text-sm ${theme.text}`}>Агент остановился не молча: ниже видно, что именно не понравилось критику и какие действия можно выбрать.</p>
        </div>
        <div className="rounded-xl border border-amber-400/40 bg-amber-400/10 px-3 py-2 text-xs text-amber-500">
          confidence: {feedback.confidence === null ? 'n/a' : `${Math.round(feedback.confidence * 100)}%`}
        </div>
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-2">
        <div className={`rounded-xl border p-3 text-sm ${theme.soft}`}>
          <div className="text-xs uppercase opacity-60">Текущий шаг</div>
          <div className="mt-1 font-medium">{feedback.currentStep}</div>
        </div>
        <div className={`rounded-xl border p-3 text-sm ${theme.soft}`}>
          <div className="text-xs uppercase opacity-60">Автовосстановление</div>
          <div className="mt-1">{feedback.recovery}</div>
        </div>
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-700">
          <div className="font-semibold text-red-600">Что не понравилось критику</div>
          <div className="mt-2 whitespace-pre-wrap break-words">{feedback.criticReason}</div>
        </div>
        <details className={`rounded-xl border p-3 text-sm ${theme.soft}`}>
          <summary className="cursor-pointer font-semibold">Observation / технические детали</summary>
          <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words text-xs">{feedback.observation}</pre>
        </details>
      </div>

      {!!feedback.lastProblems.length && (
        <div className={`mt-3 rounded-xl border p-3 text-sm ${theme.soft}`}>
          <div className="mb-2 font-semibold">Последние проблемные события</div>
          <ul className="grid gap-2">
            {feedback.lastProblems.map((item, index) => <li key={index} className="break-words text-xs opacity-85">• {item}</li>)}
          </ul>
        </div>
      )}

      <div className="mt-4">
        <div className="mb-2 text-sm font-semibold">Быстрый выбор ответа человеку</div>
        <div className="flex flex-wrap gap-2">
          {feedback.suggestions.map((suggestion) => (
            <button
              key={suggestion.label}
              onClick={() => setIntervention(suggestion.text)}
              className={`rounded-lg border px-3 py-2 text-sm ${suggestion.rollback ? 'border-red-500/40 bg-red-500/10 text-red-400' : 'border-sky-400/40 bg-sky-400/10 text-sky-400'}`}
              title={suggestion.text}
            >
              {suggestion.rollback ? '↩ ' : '✦ '}{suggestion.label}
            </button>
          ))}
        </div>
      </div>

      <textarea
        value={intervention}
        onChange={(event) => setIntervention(event.target.value)}
        placeholder="Выберите вариант выше или напишите свою инструкцию: что принять, что перепроверить, куда откатиться, какие источники/инструменты использовать..."
        className={`mt-3 h-32 w-full min-w-0 resize-y rounded-xl border p-3 ${theme.input}`}
      />
      <div className="mt-3 flex min-w-0 flex-wrap gap-2">
        <button disabled={busyAction === 'intervene' || !intervention.trim()} onClick={() => onSend(true)} className="rounded-lg bg-emerald-400 px-4 py-2 font-semibold text-slate-950 disabled:opacity-50">Отправить и продолжить</button>
        <button disabled={busyAction === 'intervene' || !intervention.trim()} onClick={() => onSend(false)} className={`rounded-lg border px-4 py-2 disabled:opacity-50 ${theme.soft}`}>Сохранить без запуска</button>
        <input value={rollbackIteration} onChange={(event) => setRollbackIteration(event.target.value)} placeholder="итерация rollback" className={`w-40 rounded-lg border px-3 py-2 ${theme.input}`} />
        <button disabled={busyAction === 'rollback'} onClick={onRollback} className="rounded-lg bg-red-500/20 px-4 py-2 text-red-500 disabled:opacity-50">Rollback</button>
      </div>
    </section>
  )
}

function TasksPanel({ tasks, selected, theme, busyAction, onSelect, onDelete }: { tasks: Task[]; selected: Task | null; theme: Theme; busyAction: string | null; onSelect: (task: Task) => void; onDelete: (task: Task) => void }) {
  return (
    <aside className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-3 font-semibold">Задачи</h2>
      <div className="max-h-[70vh] space-y-2 overflow-auto pr-1">
        {tasks.map((task) => (
          <div key={task.id} className={`min-w-0 rounded-xl border p-3 text-sm ${selected?.id === task.id ? 'border-sky-400 bg-sky-400/15 text-sky-500' : theme.soft}`}>
            <button onClick={() => onSelect(task)} className="block w-full min-w-0 text-left">
              <div className="truncate">{task.goal}</div>
              <div className="text-xs opacity-70">{statusLabel(task.status)}</div>
            </button>
            <button disabled={busyAction === `delete_task:${task.id}`} onClick={() => onDelete(task)} className="mt-2 rounded-lg bg-red-500/10 px-2 py-1 text-xs text-red-500 disabled:opacity-50">
              🗑 Удалить
            </button>
          </div>
        ))}
        {!tasks.length && <div className={`text-sm ${theme.text}`}>Задач пока нет.</div>}
      </div>
    </aside>
  )
}

function StrategyPanel({ plan, theme }: { plan: any[]; theme: Theme }) {
  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Дерево стратегии</h2>
      <div className="grid min-w-0 grid-cols-[repeat(auto-fit,minmax(190px,1fr))] gap-3">
        {plan.map((node: any) => (
          <div key={node.id} className={`min-w-0 rounded-xl border p-4 ${node.status === 'done' ? 'border-emerald-400 bg-emerald-400/10' : node.status === 'running' ? 'border-sky-400 bg-sky-400/10' : theme.soft}`}>
            <div className="text-xs uppercase opacity-60">{node.status}</div>
            <div className="break-words font-medium leading-snug">{node.title}</div>
          </div>
        ))}
        {!plan.length && <div className={`text-sm ${theme.text}`}>Дерево стратегии пока не сформировано.</div>}
      </div>
    </div>
  )
}

function ArtifactsPanel({
  artifacts,
  artifactContent,
  selectedArtifact,
  artifactViewMode,
  selected,
  theme,
  busyAction,
  onView,
  onViewModeChange,
  onUploadImage,
}: {
  artifacts: Artifact[]
  artifactContent: ArtifactContent | null
  selectedArtifact: Artifact | null
  artifactViewMode: 'markdown' | 'html'
  selected: Task | null
  theme: Theme
  busyAction: string | null
  onView: (artifact: Artifact) => void
  onViewModeChange: (mode: 'markdown' | 'html') => void
  onUploadImage: (file: File) => void
}) {
  const html = artifactContent?.content ? markdownToHtml(artifactContent.content) : ''
  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="font-semibold">Артефакты</h2>
          <div className={`text-xs ${theme.text}`}>Последние файлы сверху · {artifacts.length} всего</div>
        </div>
        <label className="cursor-pointer rounded-lg bg-sky-400/20 px-3 py-2 text-xs text-sky-500">
          📎 Прикрепить изображение
          <input type="file" accept="image/*" className="hidden" disabled={!selected || busyAction === 'upload_image'} onChange={(event) => { const file = event.target.files?.[0]; if (file) onUploadImage(file); event.currentTarget.value = '' }} />
        </label>
      </div>
      <div className="grid min-w-0 gap-3 lg:grid-cols-[300px_minmax(0,1fr)]">
        <div className="max-h-[30rem] min-w-0 space-y-2 overflow-auto pr-1">
          {artifacts.map((artifact) => (
            <div key={artifact.id} className={`min-w-0 rounded-xl border p-3 text-sm ${selectedArtifact?.id === artifact.id ? 'border-sky-400 bg-sky-400/10' : theme.soft}`}>
              <div className="truncate font-medium" title={artifact.path}>{artifact.path}</div>
              <div className="mb-2 text-xs opacity-70">{artifact.kind} · {new Date(artifact.created_at).toLocaleString()}</div>
              <div className="flex flex-wrap gap-2">
                <button disabled={busyAction === `artifact:${artifact.id}`} onClick={() => onView(artifact)} className="rounded-lg bg-sky-400/20 px-3 py-1 text-sky-500 disabled:opacity-50">Просмотр</button>
                {selected && <a href={`${API_BASE}/api/tasks/${selected.id}/artifacts/${artifact.id}/download`} className="rounded-lg bg-emerald-400/20 px-3 py-1 text-emerald-500">Скачать</a>}
              </div>
            </div>
          ))}
          {!artifacts.length && <div className={`text-sm ${theme.text}`}>Артефактов пока нет.</div>}
        </div>
        <div className="min-w-0">
          <div className={`mb-2 rounded-xl border p-3 text-xs ${theme.soft}`}>
            {selectedArtifact ? <span>Открыт: <strong>{selectedArtifact.path}</strong></span> : 'Выберите артефакт для просмотра.'}
            <div className="mt-2 flex gap-2">
              <button onClick={() => onViewModeChange('markdown')} className={`rounded-lg px-2 py-1 ${artifactViewMode === 'markdown' ? 'bg-sky-400 text-slate-950' : 'bg-sky-400/10 text-sky-500'}`}>Markdown</button>
              <button onClick={() => onViewModeChange('html')} className={`rounded-lg px-2 py-1 ${artifactViewMode === 'html' ? 'bg-sky-400 text-slate-950' : 'bg-sky-400/10 text-sky-500'}`}>HTML</button>
            </div>
          </div>
          <div className={`min-h-80 min-w-0 overflow-auto rounded-xl border p-4 text-sm ${theme.code}`}>
            {artifactContent ? artifactContent.binary ? (
              <div>{artifactContent.message}</div>
            ) : artifactViewMode === 'html' ? (
              <div className="prose prose-invert max-w-none" dangerouslySetInnerHTML={{ __html: html }} />
            ) : (
              <pre className="max-w-full whitespace-pre-wrap break-words">{artifactContent.content}</pre>
            ) : <div className={theme.text}>Выберите артефакт для просмотра.</div>}
          </div>
        </div>
      </div>
    </div>
  )
}

function SettingsPanel({
  settings,
  selected,
  budget,
  tools,
  theme,
  goalDraft,
  budgetDraft,
  stateDraft,
  busyAction,
  setGoalDraft,
  setBudgetDraft,
  setStateDraft,
  onSave,
}: {
  settings: AgentSettings | null
  selected: Task | null
  budget: any
  tools: ToolConfig
  theme: Theme
  goalDraft: string
  budgetDraft: string
  stateDraft: string
  busyAction: string | null
  setGoalDraft: (value: string) => void
  setBudgetDraft: (value: string) => void
  setStateDraft: (value: string) => void
  onSave: () => void
}) {
  const statePreview = { app: settings, task_budget: budget, task_state: selected?.current_state_json, tools }
  return (
    <details className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <summary className="cursor-pointer font-semibold">Настройки агента и состояние</summary>
      <div className="mt-4 grid gap-3">
        <label className="grid gap-1 text-xs font-medium">
          Цель задачи
          <input value={goalDraft} onChange={(event) => setGoalDraft(event.target.value)} className={`rounded-lg border px-3 py-2 text-sm ${theme.input}`} disabled={!selected} />
        </label>
        <label className="grid gap-1 text-xs font-medium">
          Budget JSON
          <textarea value={budgetDraft} onChange={(event) => setBudgetDraft(event.target.value)} className={`h-36 rounded-lg border p-3 font-mono text-xs ${theme.input}`} disabled={!selected} />
        </label>
        <label className="grid gap-1 text-xs font-medium">
          State JSON
          <textarea value={stateDraft} onChange={(event) => setStateDraft(event.target.value)} className={`h-64 rounded-lg border p-3 font-mono text-xs ${theme.input}`} disabled={!selected} />
        </label>
        <div className="flex flex-wrap gap-2">
          <button disabled={!selected || busyAction === 'save_settings'} onClick={onSave} className="rounded-lg bg-emerald-400 px-4 py-2 text-sm font-semibold text-slate-950 disabled:opacity-50">💾 Сохранить настройки</button>
          <button disabled={!selected} onClick={() => { setGoalDraft(selected?.goal || ''); setBudgetDraft(compactJson(selected?.budget_json || {})); setStateDraft(compactJson(selected?.current_state_json || {})) }} className={`rounded-lg border px-4 py-2 text-sm ${theme.soft}`}>↩ Сбросить</button>
        </div>
      </div>
      <details className="mt-4">
        <summary className="cursor-pointer text-sm opacity-80">Текущий read-only preview</summary>
        <pre className={`mt-3 max-h-96 min-w-0 overflow-auto rounded-xl border p-4 text-xs ${theme.code}`}><code className="break-words">{compactJson(statePreview)}</code></pre>
      </details>
    </details>
  )
}

function ControlPanel({ theme, confidence, contextFill, iter, budget, llmCalls, tokensUsed, busyAction, onPause, onResume }: { theme: Theme; confidence: number; contextFill: number; iter: number; budget: any; llmCalls: number; tokensUsed: number; busyAction: string | null; onPause: () => void; onResume: () => void }) {
  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Контроль и ограничения</h2>
      <div className="space-y-4">
        <Meter label="Уверенность" value={confidence} color={confidence < 50 ? 'bg-red-500' : 'bg-emerald-400'} />
        <Meter label="Заполнение контекста" value={contextFill} color="bg-amberMind" />
        <Meter label="Бюджет итераций" value={(iter / (budget.max_iterations || 25)) * 100} />
        <div className={`min-w-0 rounded-xl border p-3 text-xs ${theme.soft}`}>LLM вызовы: {llmCalls} · токены: {tokensUsed}</div>
        <div className="grid grid-cols-2 gap-2">
          <button disabled={busyAction === 'pause'} onClick={onPause} className="rounded-lg bg-amberMind/20 px-3 py-2 text-amberMind disabled:opacity-50">Пауза</button>
          <button disabled={busyAction === 'resume'} onClick={onResume} className="rounded-lg bg-emerald-400/20 px-3 py-2 text-emerald-500 disabled:opacity-50">Продолжить</button>
        </div>
      </div>
    </div>
  )
}

function TracePanel({ theme, events }: { theme: Theme; events: TaskEvent[] }) {
  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Живой trace</h2>
      <div className="max-h-[420px] min-w-0 space-y-2 overflow-auto pr-1">
        {events.map((event) => (
          <div key={event.id} className={`min-w-0 rounded-lg border p-3 text-sm ${theme.soft}`}>
            <div className="text-xs text-sky-500">{event.event_type}</div>
            <div className="break-words">{event.payload_json?.message || compactJson(event.payload_json)}</div>
          </div>
        ))}
        {!events.length && <div className={`text-sm ${theme.text}`}>Событий пока нет.</div>}
      </div>
    </div>
  )
}

function ToolsPanel({
  theme,
  tools,
  mcpTools,
  mcpCallArgs,
  mcpCallResult,
  busyAction,
  mcpName,
  mcpUrl,
  setMcpName,
  setMcpUrl,
  setMcpCallArgs,
  onToggle,
  onAddMcp,
  onDeleteMcp,
  onRefreshMcpTools,
  onCallMcpTool,
}: {
  theme: Theme
  tools: ToolConfig
  mcpTools: MCPTool[]
  mcpCallArgs: string
  mcpCallResult: any
  busyAction: string | null
  mcpName: string
  mcpUrl: string
  setMcpName: (value: string) => void
  setMcpUrl: (value: string) => void
  setMcpCallArgs: (value: string) => void
  onToggle: (key: keyof Omit<ToolConfig, 'mcp_servers'>) => void
  onAddMcp: () => void
  onDeleteMcp: (name: string) => void
  onRefreshMcpTools: () => void
  onCallMcpTool: (tool: MCPTool) => void
}) {
  const okTools = mcpTools.filter((tool) => tool.status === 'ok' && tool.name)
  const errorTools = mcpTools.filter((tool) => tool.status === 'error')

  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <div className="mb-4 flex items-center justify-between gap-2">
        <h2 className="font-semibold">Инструменты агента</h2>
        <button disabled={busyAction === 'mcp_tools'} onClick={onRefreshMcpTools} className="rounded-lg bg-sky-400/20 px-3 py-1 text-xs text-sky-500 disabled:opacity-50">
          Обновить tools
        </button>
      </div>
      <div className="space-y-3">
        {(Object.keys(TOOL_LABELS) as Array<keyof Omit<ToolConfig, 'mcp_servers'>>).map((key) => (
          <div key={key} className={`min-w-0 rounded-xl border p-3 ${theme.soft}`}>
            <div className="flex min-w-0 items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="font-medium">{TOOL_LABELS[key].title}</div>
                <div className="break-words text-xs opacity-70">{TOOL_LABELS[key].description}</div>
              </div>
              <button disabled={busyAction === 'tools'} onClick={() => onToggle(key)} className={`shrink-0 rounded-full px-3 py-1 text-xs font-semibold disabled:opacity-50 ${tools[key] ? 'bg-emerald-400 text-slate-950' : 'bg-slate-500/30 text-slate-400'}`}>
                {tools[key] ? 'Вкл' : 'Выкл'}
              </button>
            </div>
          </div>
        ))}

        <div className={`min-w-0 rounded-xl border p-3 ${theme.soft}`}>
          <div className="mb-2 font-medium">MCP серверы</div>
          <div className="grid min-w-0 gap-2">
            <input value={mcpName} onChange={(event) => setMcpName(event.target.value)} className={`min-w-0 rounded-lg border px-3 py-2 ${theme.input}`} placeholder="имя, например search" />
            <input value={mcpUrl} onChange={(event) => setMcpUrl(event.target.value)} className={`min-w-0 rounded-lg border px-3 py-2 ${theme.input}`} placeholder="http://server:8001/sse" />
            <button disabled={busyAction === 'mcp' || !mcpName.trim() || !mcpUrl.trim()} onClick={onAddMcp} className="rounded-lg bg-sky-400 px-3 py-2 font-semibold text-slate-950 disabled:opacity-50">Подключить MCP</button>
          </div>
          <div className="mt-3 space-y-2">
            {(tools.mcp_servers || []).map((server) => (
              <div key={server.name} className="min-w-0 rounded-lg border border-current/10 p-2 text-xs">
                <div className="font-medium">{server.name} · {server.transport} · {server.enabled ? 'enabled' : 'disabled'}</div>
                <div className="break-all opacity-70">{server.url}</div>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button type="button" disabled={busyAction === 'mcp_tools'} onClick={onRefreshMcpTools} title="Обновить список инструментов этого сервера" className="rounded-lg bg-sky-400/20 px-2 py-1 text-sky-500 disabled:opacity-50">🔄 tools</button>
                  <button type="button" disabled={busyAction === 'mcp'} onClick={() => onDeleteMcp(server.name)} title="Удалить MCP сервер" className="rounded-lg bg-red-500/10 px-2 py-1 text-red-500 disabled:opacity-50">🗑 удалить</button>
                </div>
              </div>
            ))}
            {!tools.mcp_servers?.length && <div className="text-xs opacity-70">Внешние MCP серверы еще не подключены. Встроенный fetch доступен после включения MCP.</div>}
          </div>
        </div>

        <div className={`min-w-0 rounded-xl border p-3 ${theme.soft}`}>
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="font-medium">MCP tools discovery</div>
            <div className="text-xs opacity-70">ok: {okTools.length} · errors: {errorTools.length}</div>
          </div>
          <textarea value={mcpCallArgs} onChange={(event) => setMcpCallArgs(event.target.value)} className={`h-28 w-full min-w-0 rounded-lg border p-2 font-mono text-xs ${theme.input}`} />
          <div className="mt-3 max-h-80 min-w-0 space-y-2 overflow-auto pr-1">
            {okTools.map((tool) => (
              <div key={`${tool.server_name}:${tool.name}`} className="min-w-0 rounded-lg border border-current/10 p-2 text-xs">
                <div className="font-medium">{tool.server_name}.{tool.name}</div>
                <div className="break-words opacity-70">{tool.description || tool.title}</div>
                <details className="mt-1">
                  <summary className="cursor-pointer opacity-70">input schema</summary>
                  <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words">{compactJson(tool.input_schema || {})}</pre>
                </details>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button type="button" onClick={() => setMcpCallArgs(compactJson(templateFromSchema(tool.input_schema)))} className="rounded-lg bg-sky-400/20 px-3 py-1 text-sky-500">Подставить JSON</button>
                  <button disabled={busyAction === `mcp_call:${tool.server_name}:${tool.name}`} onClick={() => onCallMcpTool(tool)} className="rounded-lg bg-emerald-400/20 px-3 py-1 text-emerald-500 disabled:opacity-50">Выполнить с JSON выше</button>
                </div>
              </div>
            ))}
            {errorTools.map((tool) => (
              <div key={`${tool.server_name}:error`} className="min-w-0 rounded-lg border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-500">
                <div className="font-medium">{tool.server_name}</div>
                <div className="break-words">{tool.error}</div>
              </div>
            ))}
            {!mcpTools.length && <div className="text-xs opacity-70">Нажмите «Обновить tools», чтобы получить список инструментов. Если MCP выключен — включите переключатель MCP выше.</div>}
          </div>
          {mcpCallResult && (
            <details className="mt-3" open>
              <summary className="cursor-pointer text-xs font-medium">Последний результат MCP call</summary>
              <pre className={`mt-2 max-h-64 overflow-auto rounded-lg border p-2 text-xs ${theme.code}`}>{compactJson(mcpCallResult)}</pre>
            </details>
          )}
        </div>
      </div>
    </div>
  )
}

function SnapshotsPanel({ theme, snapshots, setRollbackIteration }: { theme: Theme; snapshots: Snapshot[]; setRollbackIteration: (value: string) => void }) {
  return (
    <div className={`min-w-0 rounded-2xl border p-4 ${theme.card}`}>
      <h2 className="mb-4 font-semibold">Checkpoint / rollback</h2>
      <div className="max-h-40 min-w-0 space-y-2 overflow-auto pr-1">
        {snapshots.slice(-8).reverse().map((snapshot) => (
          <button key={snapshot.id} onClick={() => setRollbackIteration(String(snapshot.iteration))} className={`block w-full rounded-lg border p-2 text-left text-xs ${theme.soft}`}>
            Итерация {snapshot.iteration} · confidence {Number(snapshot.confidence).toFixed(2)}
          </button>
        ))}
        {!snapshots.length && <div className={`text-sm ${theme.text}`}>Снапшотов пока нет.</div>}
      </div>
    </div>
  )
}
