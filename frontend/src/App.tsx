import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Activity, ArrowDown, ArrowLeft, ArrowUpRight, AudioLines, BookOpen, Bot, Check, ChevronDown, ChevronRight, CircleHelp, Clock3, Command, Database, Headphones, LifeBuoy, MessageSquare, Mic, MicOff, MoreHorizontal, Plus, Search, Send, Settings2, ShieldCheck, Sparkles, UserRound, Users, X } from 'lucide-react'
import { GeminiLiveVoiceSession, HttpChatTransport, type VoiceSessionEvent } from './transport'

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000'
const transport = new HttpChatTransport(API)
type ChatMessage = { id?: string; role: 'user' | 'assistant'; content: string }
type Trace = { id: string; parent_id?: string | null; type: string; label: string; detail?: string; duration_ms: number; metadata?: Record<string, unknown>; started_at?: string }
type Conversation = { id: string; title: string; created_at: string; updated_at: string; message_count: number; duration_ms: number; status: string; events: Trace[]; messages: ChatMessage[] }
type Overview = { conversations: Conversation[]; usage: { conversations: number; messages: number; tool_calls: number; input_tokens?: number; output_tokens?: number; avg_latency_ms: number } }
type SupportTicket = { id: string; reference: string; created_at: string; case_id: string; subject: string; conversation_id: string; customer_id: string; customer_email: string; status: string }
type EscalationTicket = { id: string; reference: string; created_at: string; category: string; reason: string; conversation_id: string; customer_id: string; customer_email: string; amount: string; order_id: string; status: string }

const samples = ['Where is my order?', 'What is your return policy?', 'I need help with shipping']

function liveTokenCost(detailsValue: unknown, fallbackTokens: number, textRate: number, audioRate: number) {
  if (!Array.isArray(detailsValue) || !detailsValue.length) return fallbackTokens * audioRate / 1_000_000
  return detailsValue.reduce((total, entry) => {
    if (!entry || typeof entry !== 'object') return total
    const item = entry as { modality?: string; tokenCount?: number; token_count?: number }
    const modality = String(item.modality || 'AUDIO').toUpperCase()
    const rate = modality === 'TEXT' ? textRate : modality === 'IMAGE' || modality === 'VIDEO' ? 1 : audioRate
    return total + Number(item.tokenCount || item.token_count || 0) * rate / 1_000_000
  }, 0)
}

function App() {
  const [page, setPage] = useState<'chat' | 'analytics' | 'queue' | 'tickets'>('chat')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [isSending, setIsSending] = useState(false)
  const [status, setStatus] = useState('Online · Typically replies instantly')
  const [trace, setTrace] = useState<Trace[]>([])
  const [activePromptId, setActivePromptId] = useState<string | null>(null)
  const [liveExpanded, setLiveExpanded] = useState<string[]>([])
  const [overview, setOverview] = useState<Overview | null>(null)
  const [selected, setSelected] = useState<Conversation | null>(null)
  const [expanded, setExpanded] = useState<string[]>([])
  const [query, setQuery] = useState('')
  const [provider, setProvider] = useState('demo')
  const [database, setDatabase] = useState('memory-demo')
  const [escalationQueue, setEscalationQueue] = useState('unavailable')
  const [adminKey, setAdminKey] = useState<string>(() => sessionStorage.getItem('support-admin-key') || '')
  const [tickets, setTickets] = useState<EscalationTicket[]>([])
  const [ticketCount, setTicketCount] = useState(0)
  const [supportTickets, setSupportTickets] = useState<SupportTicket[]>([])
  const [supportTicketCount, setSupportTicketCount] = useState(0)
  const [queueError, setQueueError] = useState('')
  const [queueBusy, setQueueBusy] = useState(false)
  const [companyName, setCompanyName] = useState('ErwinCo')
  const [authToken, setAuthToken] = useState<string | null>(() => sessionStorage.getItem('support-session-token'))
  const [authenticatedEmail, setAuthenticatedEmail] = useState<string | null>(() => sessionStorage.getItem('support-session-email'))
  const [authenticatedName, setAuthenticatedName] = useState<string | null>(() => sessionStorage.getItem('support-session-name'))
  const [loginOpen, setLoginOpen] = useState(false)
  const [loginEmail, setLoginEmail] = useState('')
  const [loginPassword, setLoginPassword] = useState('')
  const [loginError, setLoginError] = useState('')
  const [isLoggingIn, setIsLoggingIn] = useState(false)
  const [pendingLoginMessage, setPendingLoginMessage] = useState<string | null>(null)
  const pendingLoginMessageRef = useRef<string | null>(null)
  const loginDialogFallbackRef = useRef<number | null>(null)
  const [voiceState, setVoiceState] = useState<'off' | 'connecting' | 'listening' | 'speaking' | 'error'>('off')
  const [voiceLevel, setVoiceLevel] = useState(0)
  const [voiceError, setVoiceError] = useState('')
  const voiceClientRef = useRef<GeminiLiveVoiceSession | null>(null)
  const messageAreaRef = useRef<HTMLDivElement | null>(null)
  const stickToBottomRef = useRef(true)
  const traceTreeRef = useRef<HTMLDivElement | null>(null)
  const traceStickToBottomRef = useRef(true)
  const voiceConnectionEstablishedRef = useRef(false)
  const voiceReconnectInProgressRef = useRef(false)
  const voiceReconnectAttemptsRef = useRef(0)
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const voiceConversationIdRef = useRef<string | null>(null)
  const voiceEndChatRef = useRef(false)
  const voiceLogoutPendingRef = useRef(false)
  const pendingVoiceLoginRef = useRef(false)
  const voiceLoginPersistenceRef = useRef<Promise<void>>(Promise.resolve())
  const lastVoiceUserTextRef = useRef('')
  const latestVoiceUserBubbleRef = useRef<{ id: string; content: string } | null>(null)
  const voiceSyntheticSpeechRef = useRef(false)
  const voiceTurnRef = useRef<{ id: string; promptId: string; userText: string; assistantText: string; steps: Record<string, unknown>[]; started: number; persisted?: boolean } | null>(null)

  const loadOverview = async () => {
    try {
      const response = await fetch(`${API}/api/admin/overview`)
      const data = await response.json() as Overview
      setOverview(data)
    } catch { /* API may start after the frontend */ }
  }
  const showLoginDialog = () => {
    if (loginDialogFallbackRef.current !== null) window.clearTimeout(loginDialogFallbackRef.current)
    loginDialogFallbackRef.current = null
    setLoginOpen(true)
  }
  const scheduleLoginDialogFallback = () => {
    if (loginDialogFallbackRef.current !== null) window.clearTimeout(loginDialogFallbackRef.current)
    loginDialogFallbackRef.current = window.setTimeout(showLoginDialog, 5000)
  }
  // Silent background refresh for the sidebar count (and the queue list):
  // no busy/error UI. Uses a ref so long-lived voice handlers see the current key.
  const adminKeyRef = useRef(adminKey)
  adminKeyRef.current = adminKey
  const refreshEscalationCount = async () => {
    const key = adminKeyRef.current
    if (!key) return
    const fetchTopic = async (path: string) => {
      try {
        const response = await fetch(`${API}${path}`, { headers: { 'X-Admin-Key': key } })
        return response.ok ? await response.json() as { tickets?: unknown[]; count?: number } : null
      } catch { return null /* keep the last known count */ }
    }
    const [escalations, support] = await Promise.all([fetchTopic('/api/admin/escalations'), fetchTopic('/api/admin/support-tickets')])
    if (escalations) { setTickets((escalations.tickets || []) as EscalationTicket[]); setTicketCount(escalations.count || 0) }
    if (support) { setSupportTickets((support.tickets || []) as SupportTicket[]); setSupportTicketCount(support.count || 0) }
  }
  const loadSupportTickets = async (key = adminKey) => {
    if (!key) return
    setQueueBusy(true); setQueueError('')
    try {
      const response = await fetch(`${API}/api/admin/support-tickets`, { headers: { 'X-Admin-Key': key } })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Could not load support tickets.')
      setSupportTickets(data.tickets || []); setSupportTicketCount(data.count || 0)
    } catch (error) {
      setQueueError(error instanceof Error ? error.message : 'Could not load support tickets.')
    } finally { setQueueBusy(false) }
  }
  const clearSupportTickets = async () => {
    if (!window.confirm('Clear all support tickets from the Redis queue? Support cases in the database are not affected. This cannot be undone.')) return
    setQueueBusy(true); setQueueError('')
    try {
      const response = await fetch(`${API}/api/admin/support-tickets`, { method: 'DELETE', headers: { 'X-Admin-Key': adminKey } })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Could not clear support tickets.')
      setSupportTickets([]); setSupportTicketCount(0)
    } catch (error) {
      setQueueError(error instanceof Error ? error.message : 'Could not clear support tickets.')
    } finally { setQueueBusy(false) }
  }
  const loadEscalations = async (key = adminKey) => {
    if (!key) return
    setQueueBusy(true); setQueueError('')
    try {
      const response = await fetch(`${API}/api/admin/escalations`, { headers: { 'X-Admin-Key': key } })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Could not load escalation tickets.')
      setTickets(data.tickets || []); setTicketCount(data.count || 0)
    } catch (error) {
      setQueueError(error instanceof Error ? error.message : 'Could not load escalation tickets.')
    } finally { setQueueBusy(false) }
  }
  const clearEscalations = async () => {
    if (!window.confirm('Clear all escalation tickets from the Redis queue? This cannot be undone.')) return
    setQueueBusy(true); setQueueError('')
    try {
      const response = await fetch(`${API}/api/admin/escalations`, { method: 'DELETE', headers: { 'X-Admin-Key': adminKey } })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Could not clear escalation tickets.')
      setTickets([]); setTicketCount(0)
    } catch (error) {
      setQueueError(error instanceof Error ? error.message : 'Could not clear escalation tickets.')
    } finally { setQueueBusy(false) }
  }
  useEffect(() => {
    fetch(`${API}/api/health`).then(r => r.json()).then(d => { setProvider(d.provider); setDatabase(d.database); setEscalationQueue(d.escalation_queue); if (d.company_name) setCompanyName(d.company_name) }).catch(() => {})
    loadOverview()
  }, [])
  useEffect(() => {
    if (page !== 'analytics') return
    const timer = window.setInterval(loadOverview, 4000)
    return () => window.clearInterval(timer)
  }, [page])
  useEffect(() => () => { void voiceClientRef.current?.disconnect() }, [])
  useEffect(() => {
    if (!adminKey) return
    void refreshEscalationCount()
    // Background poll is only a fallback: counts already refresh right after
    // each chat turn and ticket-creating voice tool, and on window focus.
    // 60 s keeps Redis usage low (each poll is 4 commands; matters on Upstash's free tier).
    const timer = window.setInterval(() => { if (!document.hidden) void refreshEscalationCount() }, 60_000)
    const onFocus = () => { void refreshEscalationCount() }
    window.addEventListener('focus', onFocus)
    return () => { window.clearInterval(timer); window.removeEventListener('focus', onFocus) }
  }, [adminKey])
  // Follow new and streaming messages unless the customer scrolled up to read
  // earlier ones; their own new message always jumps back to the bottom.
  useEffect(() => {
    const area = messageAreaRef.current
    if (!area) return
    if (messages[messages.length - 1]?.role === 'user') stickToBottomRef.current = true
    if (stickToBottomRef.current) area.scrollTop = area.scrollHeight
  }, [messages, page])
  // Same behavior for the live Prompts & Steps list: follow new steps unless
  // the user scrolled up; a new customer prompt jumps back to the bottom.
  useEffect(() => {
    const tree = traceTreeRef.current
    if (!tree) return
    if (trace[trace.length - 1]?.type === 'user_prompt') traceStickToBottomRef.current = true
    if (traceStickToBottomRef.current) tree.scrollTop = tree.scrollHeight
  }, [trace, page])

  const updateTranscriptBubble = (id: string, role: ChatMessage['role'], content: string) => {
    setMessages(current => {
      const index = current.findIndex(item => item.id === id)
      if (index < 0) return [...current, { id, role, content }]
      return current.map(item => item.id === id ? { ...item, content } : item)
    })
  }

  const updateVoiceUserBubble = (id: string, content: string) => {
    setMessages(current => {
      const userId = `${id}-user`
      const existing = current.findIndex(item => item.id === userId)
      if (existing >= 0) return current.map(item => item.id === userId ? { ...item, content } : item)
      const assistantIndex = current.findIndex(item => item.id === `${id}-assistant`)
      const next = [...current]
      next.splice(assistantIndex >= 0 ? assistantIndex : next.length, 0, { id: userId, role: 'user', content })
      return next
    })
  }

  const beginVoiceTurn = (initialText = '') => {
    if (voiceTurnRef.current) return voiceTurnRef.current
    const id = crypto.randomUUID()
    const turn = { id, promptId: `voice-prompt-${id}`, userText: initialText, assistantText: '', steps: [] as Record<string, unknown>[], started: performance.now(), persisted: false }
    voiceTurnRef.current = turn
    if (initialText.trim()) {
      latestVoiceUserBubbleRef.current = { id: `${id}-user`, content: initialText }
      setMessages(current => [...current, { id: `${id}-user`, role: 'user', content: initialText }])
    }
    const prompt: Trace = { id: turn.promptId, type: 'user_prompt', label: 'Voice prompt', detail: initialText, duration_ms: 0, metadata: { content: initialText, input_mode: 'voice' }, started_at: new Date().toISOString() }
    setTrace(current => [...current, prompt]); setActivePromptId(turn.promptId)
    setLiveExpanded(current => current.includes(turn.promptId) ? current : [...current, turn.promptId])
    return turn
  }

  const persistVoiceTurn = (turn: NonNullable<typeof voiceTurnRef.current>, usageValue?: unknown): Promise<void> => {
    if (!turn.userText.trim() || turn.persisted) return Promise.resolve()
    turn.persisted = true
    if (usageValue && typeof usageValue === 'object') {
      const usage = usageValue as Record<string, unknown>
      const inputTokens = Number(usage.promptTokenCount || 0)
      const outputTokens = Number(usage.candidatesTokenCount || usage.responseTokenCount || 0)
      const inputCost = liveTokenCost(usage.promptTokensDetails || usage.prompt_tokens_details, inputTokens, 0.75, 3)
      const outputCost = liveTokenCost(usage.responseTokensDetails || usage.candidatesTokensDetails || usage.response_tokens_details, outputTokens, 4.5, 12)
      const modelUsage = { model: 'Gemini Live', input_tokens: inputTokens, output_tokens: outputTokens, input_cost_usd: inputCost, output_cost_usd: outputCost, estimated_cost_usd: inputCost + outputCost, input_usd_per_million: 3, output_usd_per_million: 12 }
      const elapsed = Math.round(performance.now() - turn.started)
      turn.steps.push({ type: 'model_call', label: 'Gemini Live response', detail: modelUsage, duration_ms: 0 })
      turn.steps.push({ type: 'turn', label: 'Voice turn latency', detail: {}, duration_ms: elapsed })
      setTrace(current => [...current,
        { id: crypto.randomUUID(), parent_id: turn.promptId, type: 'model_call', label: 'Gemini Live response', detail: JSON.stringify(modelUsage), duration_ms: 0, metadata: modelUsage },
        { id: crypto.randomUUID(), parent_id: turn.promptId, type: 'turn', label: 'Voice turn latency', detail: 'Speech transcript to response completion', duration_ms: elapsed },
      ])
    }
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (authToken) headers.Authorization = `Bearer ${authToken}`
    setConversationId(voiceConversationIdRef.current || conversationId)
    setActivePromptId(turn.promptId)
    return fetch(`${API}/api/voice/turn`, { method: 'POST', headers, body: JSON.stringify({ conversation_id: voiceConversationIdRef.current, user_text: turn.userText, assistant_text: turn.assistantText, steps: turn.steps }) })
      .then(response => { if (!response.ok) throw new Error('Could not save voice transcript'); loadOverview() })
      .catch(() => { setStatus('Voice reply received · Transcript could not be saved') })
  }

  const handleVoiceEvent = (event: VoiceSessionEvent) => {
    if (voiceSyntheticSpeechRef.current && ['output_transcript', 'turn_complete'].includes(event.type)) return
    // A real barge-in takes ownership from the synthetic login announcement.
    if (event.type === 'input_transcript' || event.type === 'interrupted') voiceSyntheticSpeechRef.current = false
    if (pendingVoiceLoginRef.current) {
      if (event.type === 'turn_complete') { showLoginDialog(); return }
      if (event.type !== 'error') return
    }
    const data = event.data
    if (event.type === 'connected') { voiceConnectionEstablishedRef.current = true; setVoiceState('listening'); setStatus('Listening · Speak when you’re ready'); return }
    if (event.type === 'level') { setVoiceLevel(Number(data.value || 0)); return }
    if (event.type === 'input_transcript') {
      voiceReconnectAttemptsRef.current = 0
      const text = String(data.text || '')
      lastVoiceUserTextRef.current = text
      const turn = voiceTurnRef.current || beginVoiceTurn(text)
      turn.userText = text
      voiceClientRef.current?.setCurrentUserText(text)
      latestVoiceUserBubbleRef.current = { id: `${turn.id}-user`, content: text }
      updateVoiceUserBubble(turn.id, text)
      const prompt = { id: turn.promptId, parent_id: turn.promptId, type: 'user_prompt', label: 'Voice prompt', detail: text, duration_ms: 0, metadata: { content: text, input_mode: 'voice' } } as Trace
      setTrace(current => current.map(item => item.id === turn.promptId ? prompt : item))
      setStatus('Hearing you…')
      return
    }
    if (event.type === 'output_transcript') {
      const turn = voiceTurnRef.current || beginVoiceTurn()
      turn.assistantText += String(data.text || '')
      updateTranscriptBubble(`${turn.id}-assistant`, 'assistant', turn.assistantText)
      setVoiceState('speaking'); setStatus('Speaking · You can interrupt at any time')
      return
    }
    if (event.type === 'tool_call') {
      const turn = voiceTurnRef.current || beginVoiceTurn()
      const step = { type: 'tool_call', label: String(data.name || 'Support tool'), detail: data, duration_ms: 0 }
      turn.steps.push(step)
      const item: Trace = { id: String(data.id || crypto.randomUUID()), parent_id: turn.promptId, type: 'tool_call', label: String(data.name || 'Support tool'), detail: JSON.stringify(data), duration_ms: 0, metadata: data }
      setTrace(current => [...current, item]); setStatus(`Working · ${item.label.replace(/_/g, ' ')}`)
      return
    }
    if (event.type === 'tool_result') {
      const activeVoiceTurn = voiceTurnRef.current
      const turn = activeVoiceTurn || beginVoiceTurn()
      const result = data.result && typeof data.result === 'object' ? data.result as Record<string, unknown> : {}
      const item: Trace = { id: crypto.randomUUID(), parent_id: turn.promptId, type: 'tool_result', label: String(data.name || 'Tool result'), detail: JSON.stringify(data), duration_ms: Number(result.duration_ms || 0), metadata: data }
      turn.steps.push({ type: 'tool_result', label: item.label, detail: data, duration_ms: item.duration_ms })
      setTrace(current => [...current, item])
      if (result.login_required) {
        pendingVoiceLoginRef.current = true
        const pendingQuestion = turn.userText.trim() || String(data.current_user_message || voiceClientRef.current?.getCurrentUserText() || lastVoiceUserTextRef.current || '').trim()
        if (pendingQuestion) turn.userText = pendingQuestion
        const notice = String(result.message || 'Please sign in to continue with account-specific support.')
        turn.assistantText = notice
        const latestUserMessage = [...messagesRef.current].reverse().find(item => item.role === 'user')
        const promptAlreadyVisible = latestUserMessage?.content.trim() === pendingQuestion
          || latestVoiceUserBubbleRef.current?.content.trim() === pendingQuestion
        if (pendingQuestion && !promptAlreadyVisible) {
          latestVoiceUserBubbleRef.current = { id: `${turn.id}-user`, content: pendingQuestion }
          updateVoiceUserBubble(turn.id, pendingQuestion)
        }
        updateTranscriptBubble(`${turn.id}-assistant`, 'assistant', notice)
        // A completed Live turn may already have persisted this exact prompt.
        if (activeVoiceTurn || !promptAlreadyVisible) voiceLoginPersistenceRef.current = persistVoiceTurn(turn)
        voiceTurnRef.current = null
        voiceClientRef.current?.pause()
        setVoiceState('off'); setVoiceLevel(0); setStatus('Voice paused · Sign in to continue')
        pendingLoginMessageRef.current = pendingQuestion
        setPendingLoginMessage(pendingQuestion); setLoginError('')
        scheduleLoginDialogFallback()
      }
      if (data.name === 'end_chat') voiceEndChatRef.current = true
      if (data.name === 'create_escalation_ticket' || data.name === 'create_support_case' || result.escalation) void refreshEscalationCount()
      if (result.session_revoked) {
        sessionStorage.removeItem('support-session-token'); sessionStorage.removeItem('support-session-email'); sessionStorage.removeItem('support-session-name')
        setAuthToken(null); setAuthenticatedEmail(null); setAuthenticatedName(null); setLoginOpen(false); setPendingLoginMessage(null)
        voiceLogoutPendingRef.current = true
      }
      return
    }
    if (event.type === 'guardrail') {
      const turn = voiceTurnRef.current
      const text = String(data.text || 'I’m sending this to a support specialist for review.')
      if (turn) {
        turn.assistantText = text
        updateTranscriptBubble(`${turn.id}-assistant`, 'assistant', text)
        turn.steps.push({ type: 'escalation_required', label: 'Low-confidence manual result', detail: { text }, duration_ms: 0 })
        persistVoiceTurn(turn)
      }
      voiceTurnRef.current = null
      voiceEndChatRef.current = false
      void voiceClientRef.current?.disconnect(); voiceClientRef.current = null
      setVoiceState('off'); setStatus('Human review required · Voice session ended')
      if ('speechSynthesis' in window) window.speechSynthesis.speak(new SpeechSynthesisUtterance(text))
      return
    }
    if (event.type === 'interrupted') {
      const turn = voiceTurnRef.current
      if (turn) {
        turn.steps.push({ type: 'interrupted', label: 'Voice response interrupted', detail: {}, duration_ms: Math.round(performance.now() - turn.started) })
        persistVoiceTurn(turn)
        voiceTurnRef.current = null
      }
      setVoiceState('listening'); setStatus('Interrupted · Listening to you'); return
    }
    if (event.type === 'turn_complete') {
      const turn = voiceTurnRef.current
      if (!turn) { setVoiceState('listening'); setStatus('Listening · Speak when you’re ready'); return }
      turn.userText = turn.userText.trim() || String(data.user_text || '')
      if (turn.userText.trim()) persistVoiceTurn(turn, data.usage)
      if (voiceLogoutPendingRef.current || voiceEndChatRef.current) {
        const wasLogout = voiceLogoutPendingRef.current
        voiceLogoutPendingRef.current = false
        voiceEndChatRef.current = false
        void voiceClientRef.current?.disconnect(); voiceClientRef.current = null; setVoiceState('off'); setVoiceLevel(0); setStatus(wasLogout ? 'Signed out' : 'Conversation ended')
      } else {
        setVoiceState('listening'); setStatus('Listening · Speak when you’re ready')
      }
      voiceTurnRef.current = null
      return
    }
    if (event.type === 'error') {
      const message = String(data.message || 'Voice session failed. You can continue by typing.')
      setVoiceError(message); setVoiceState('error'); setStatus(message)
      const failedClient = voiceClientRef.current
      const reconnectable = /closed|websocket connection failed/i.test(message)
      if (voiceConnectionEstablishedRef.current && reconnectable && failedClient && !voiceReconnectInProgressRef.current && voiceReconnectAttemptsRef.current < 3) {
        voiceReconnectInProgressRef.current = true
        voiceReconnectAttemptsRef.current += 1
        setVoiceState('connecting'); setStatus('Voice connection lost · Reconnecting…')
        void (async () => {
          await new Promise(resolve => window.setTimeout(resolve, 700))
          if (voiceClientRef.current !== failedClient) { voiceReconnectInProgressRef.current = false; return }
          const token = failedClient.getAccessToken()
          const id = voiceConversationIdRef.current
          await failedClient.disconnect()
          if (!token || !id) { voiceClientRef.current = null; voiceReconnectInProgressRef.current = false; setVoiceState('error'); return }
          const replacement = new GeminiLiveVoiceSession(API, id, token, messagesRef.current.slice(-20))
          voiceClientRef.current = replacement
          voiceConnectionEstablishedRef.current = false
          replacement.onEvent(handleVoiceEvent)
          try { await replacement.connect() }
          catch (error) {
            await replacement.disconnect()
            if (voiceClientRef.current === replacement) voiceClientRef.current = null
            setVoiceState('error'); setVoiceError(error instanceof Error ? error.message : 'Could not reconnect voice.')
          } finally { voiceReconnectInProgressRef.current = false }
        })()
      } else if (failedClient) {
        void failedClient.disconnect()
        if (voiceClientRef.current === failedClient) voiceClientRef.current = null
        voiceConnectionEstablishedRef.current = false
      }
    }
  }

  const toggleVoice = async () => {
    if (voiceClientRef.current) {
      if (voiceTurnRef.current) persistVoiceTurn(voiceTurnRef.current)
      await voiceClientRef.current.disconnect(); voiceClientRef.current = null
      voiceTurnRef.current = null; voiceEndChatRef.current = false; voiceConnectionEstablishedRef.current = false; voiceReconnectAttemptsRef.current = 0; setVoiceState('off'); setVoiceLevel(0); setVoiceError(''); setStatus('Online · Typically replies instantly')
      return
    }
    setVoiceState('connecting'); setVoiceError(''); setStatus('Connecting to voice…')
    const id = conversationId || `conv_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`
    voiceConversationIdRef.current = id
    setConversationId(id)
    const client = new GeminiLiveVoiceSession(API, id, authToken, messages)
    voiceClientRef.current = client
    client.onEvent(handleVoiceEvent)
    try { await client.connect() }
    catch (error) {
      await client.disconnect(); voiceClientRef.current = null; setVoiceState('error')
      const message = error instanceof Error ? error.message : 'Could not start voice. You can continue by typing.'
      setVoiceError(message); setStatus(message)
    }
  }

  const send = async (text = draft, options: { appendUser?: boolean; token?: string | null; resumeLogin?: boolean } = {}): Promise<string> => {
    const message = text.trim()
    if (!message || isSending) return ''
    setDraft(''); setIsSending(true); setStatus('Thinking…')
    const promptId = options.appendUser === false ? activePromptId : crypto.randomUUID()
    if (options.appendUser !== false) {
      setMessages(current => [...current, { role: 'user', content: message }])
      const promptEvent: Trace = { id: promptId!, type: 'user_prompt', label: 'Customer prompt', detail: message, duration_ms: 0, metadata: { content: message }, started_at: new Date().toISOString() }
      setTrace(current => [...current, promptEvent])
      setActivePromptId(promptId!)
      setLiveExpanded(current => current.includes(promptId!) ? current : [...current, promptId!])
    }
    const started = performance.now()
    let responseText = ''
    let loggedOut = false
    let loginRequired = false
    const responseMessageId = crypto.randomUUID()
    setMessages(current => [...current, { id: responseMessageId, role: 'assistant', content: '' }])
    try {
      voiceClientRef.current?.stopPlayback()
      const requestToken = options.token === undefined ? authToken : options.token
      for await (const event of transport.send(conversationId, message, requestToken, options.resumeLogin)) {
          const eventName = event.type
          const data = event.data
          if (eventName === 'conversation') setConversationId(String(data.conversation_id))
          if (eventName === 'status') setStatus(String(data.text || 'Working…'))
          if (eventName === 'message') {
            responseText += String(data.text || '')
            updateTranscriptBubble(responseMessageId, 'assistant', responseText)
          }
          if (eventName === 'login_required') {
            loginRequired = true
            responseText = String(data.message || 'Sign in to continue with account-specific support.')
            updateTranscriptBubble(responseMessageId, 'assistant', responseText)
            pendingVoiceLoginRef.current = Boolean(voiceClientRef.current)
            pendingLoginMessageRef.current = message
            setPendingLoginMessage(message)
            setLoginError('')
            if (voiceClientRef.current) {
              const client = voiceClientRef.current
              client.pauseInput(); voiceSyntheticSpeechRef.current = true
              void client.speakText(responseText).catch(() => {}).finally(() => {
                voiceSyntheticSpeechRef.current = false; showLoginDialog()
              })
              scheduleLoginDialogFallback()
            } else setLoginOpen(true)
          }
          if (eventName === 'tool_call' || eventName === 'tool_result' || eventName === 'citation' || eventName === 'model_call' || eventName === 'escalation_required' || eventName === 'session_logout') {
            const item: Trace = { id: String(data.id || crypto.randomUUID()), parent_id: promptId, type: eventName, label: String(data.name || data.title || eventName), detail: JSON.stringify(data), duration_ms: Number(data.duration_ms || 0), metadata: data }
            setTrace(current => [...current, item])
          }
          if (eventName === 'escalation_required') setStatus('Human review required')
          if (eventName === 'session_logout') { loggedOut = true; signOut(); setStatus('Signed out') }
          if (eventName === 'error') throw new Error(String(data.message || 'Something went wrong.'))
          if (eventName === 'done') setStatus(data.logout ? 'Signed out' : data.end_chat ? 'Conversation ended' : 'Online · Typically replies instantly')
      }
    } catch (error) {
      responseText = error instanceof Error ? error.message : 'Something went wrong.'
      updateTranscriptBubble(responseMessageId, 'assistant', responseText)
      if (responseText.includes('Sign in again')) {
        sessionStorage.removeItem('support-session-token')
        sessionStorage.removeItem('support-session-email')
        sessionStorage.removeItem('support-session-name')
        setAuthToken(null); setAuthenticatedEmail(null); setAuthenticatedName(null)
        pendingLoginMessageRef.current = message
        setPendingLoginMessage(message); setLoginError('Your session expired. Please sign in again.')
        setLoginOpen(true)
      }
    } finally {
      const elapsed = Math.round(performance.now() - started)
      setTrace(current => [...current, { id: crypto.randomUUID(), type: 'turn', label: 'Assistant turn', detail: 'End-to-end request', duration_ms: elapsed }])
      if (responseText && !options.resumeLogin && !loginRequired) voiceClientRef.current?.syncTextExchange(message, responseText)
      void refreshEscalationCount()
      setIsSending(false); setStatus(loggedOut ? 'Signed out' : voiceClientRef.current ? 'Voice paused · Finishing sign-in' : 'Online · Typically replies instantly'); loadOverview()
    }
    return responseText
  }

  const connectAuthenticatedVoice = async (token: string, pending: string): Promise<GeminiLiveVoiceSession | null> => {
    const id = voiceConversationIdRef.current || conversationId || `conv_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`
    // This session is opened only to speak the completed HTTP continuation.
    // Seeding an unfinished user-only history creates consecutive user turns.
    const history: { role: 'user' | 'assistant'; content: string }[] = []
    const previous = voiceClientRef.current
    if (previous) await previous.disconnect()
    voiceClientRef.current = null
    voiceConnectionEstablishedRef.current = false
    setVoiceState('connecting'); setVoiceError(''); setStatus('Starting your signed-in voice session…')
    const client = new GeminiLiveVoiceSession(API, id, token, history)
    voiceClientRef.current = client
    client.onEvent(handleVoiceEvent)
    client.pauseInput()
    try {
      await client.connect()
      setConversationId(id)
      setVoiceState('listening'); setStatus('Voice ready · Continuing your request…')
      return client
    } catch (error) {
      await client.disconnect()
      if (voiceClientRef.current === client) voiceClientRef.current = null
      voiceConnectionEstablishedRef.current = false
      setVoiceState('error')
      const message = error instanceof Error ? error.message : 'Could not resume voice after sign-in. You can continue by typing.'
      setVoiceError(message); setStatus(message)
      return null
    }
  }

  const speakAuthenticatedReply = async (client: GeminiLiveVoiceSession, pending: string, welcome: string, answer: string) => {
    setVoiceState('speaking'); setStatus('Speaking…')
    voiceSyntheticSpeechRef.current = true
    const spokenText = [welcome, answer].filter(Boolean).join(' ')
    let audioStarted = false
    const speakWithBrowser = () => new Promise<void>(resolve => {
      if (!spokenText || !('speechSynthesis' in window)) { resolve(); return }
      const utterance = new SpeechSynthesisUtterance(spokenText)
      utterance.onend = () => resolve()
      utterance.onerror = () => resolve()
      window.speechSynthesis.speak(utterance)
    })
    try {
      // The speech request itself puts the exchange into the Live session's
      // context, so no separate history sync is needed afterwards.
      const receivedLiveAudio = await client.speakText(spokenText, () => { audioStarted = true; client.resume() })
      if (!receivedLiveAudio) await speakWithBrowser()
      client.resume()
      setVoiceState('listening'); setStatus('Listening · Speak when you’re ready')
    } catch {
      // Live audio never started (timeout or error): still read the reply aloud.
      if (!audioStarted) await speakWithBrowser()
      client.resume()
      setVoiceState('listening'); setStatus('Listening · Speak when you’re ready')
    } finally { voiceSyntheticSpeechRef.current = false }
  }

  const submitLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setIsLoggingIn(true); setLoginError('')
    try {
      const response = await fetch(`${API}/api/auth/mock-login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: loginEmail, password: loginPassword }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Could not sign in.')
      sessionStorage.setItem('support-session-token', data.access_token)
      sessionStorage.setItem('support-session-email', data.email)
      sessionStorage.setItem('support-session-name', data.name || '')
      setAuthToken(data.access_token); setAuthenticatedEmail(data.email); setAuthenticatedName(data.name || null)
      if (loginDialogFallbackRef.current !== null) window.clearTimeout(loginDialogFallbackRef.current)
      loginDialogFallbackRef.current = null
      setLoginOpen(false); setLoginPassword('')
      const pending = pendingLoginMessageRef.current || pendingLoginMessage
      pendingLoginMessageRef.current = null
      setPendingLoginMessage(null)
      const voiceLogin = pendingVoiceLoginRef.current || Boolean(voiceClientRef.current)
      // Inherit the live Gemini session: it keeps the conversation context and
      // audio pipeline, and now runs its tools with the signed-in token.
      const inheritedVoiceClient = voiceLogin ? voiceClientRef.current : null
      if (inheritedVoiceClient) {
        inheritedVoiceClient.pauseInput()
        inheritedVoiceClient.setAccessToken(data.access_token)
        setVoiceState('listening'); setStatus('Voice ready · Continuing your request…')
      }
      await voiceLoginPersistenceRef.current
      pendingVoiceLoginRef.current = false
      const welcome = `Welcome back, ${data.name || 'there'}. I’ll continue with your request.`
      const continueRequest = async () => {
        lastVoiceUserTextRef.current = ''
        // Only open a new session if the previous one was lost during sign-in.
        const voiceClient = inheritedVoiceClient
          || (voiceLogin ? await connectAuthenticatedVoice(data.access_token, pending || '') : null)
        setMessages(current => [...current, { id: crypto.randomUUID(), role: 'assistant', content: welcome }])
        const answer = pending
          ? await send(pending, { appendUser: false, token: data.access_token, resumeLogin: true })
          : ''
        if (voiceClient) await speakAuthenticatedReply(voiceClient, pending || '', welcome, answer)
      }
      void continueRequest()
    } catch (error) {
      setLoginError(error instanceof Error ? error.message : 'Could not sign in.')
    } finally {
      setIsLoggingIn(false)
    }
  }

  const closeLogin = () => {
    const wasVoiceLogin = pendingVoiceLoginRef.current
    pendingVoiceLoginRef.current = false
    if (loginDialogFallbackRef.current !== null) window.clearTimeout(loginDialogFallbackRef.current)
    loginDialogFallbackRef.current = null
    lastVoiceUserTextRef.current = ''
    setLoginOpen(false); setPendingLoginMessage(null); pendingLoginMessageRef.current = null; setLoginError('')
    const message = 'Log in canceled. You can continue with general questions or sign in later for account support.'
    setMessages(current => [...current, { id: crypto.randomUUID(), role: 'assistant', content: message }])
    if (wasVoiceLogin) {
      const client = voiceClientRef.current
      if (client) {
        client.pauseInput(); voiceSyntheticSpeechRef.current = true
        void client.speakText(message).catch(() => {
          if ('speechSynthesis' in window) window.speechSynthesis.speak(new SpeechSynthesisUtterance(message))
        }).finally(() => {
          voiceSyntheticSpeechRef.current = false; client.resume(); setVoiceState('listening'); setStatus('Listening · Speak when you’re ready')
        })
      }
    } else if (voiceClientRef.current) {
      voiceClientRef.current.resume(); setVoiceState('listening'); setStatus('Listening · Speak when you’re ready')
    }
  }
  const signOut = () => {
    if (voiceTurnRef.current) persistVoiceTurn(voiceTurnRef.current)
    void voiceClientRef.current?.disconnect(); voiceClientRef.current = null; voiceTurnRef.current = null; setVoiceState('off'); setVoiceLevel(0)
    sessionStorage.removeItem('support-session-token')
    sessionStorage.removeItem('support-session-email')
    sessionStorage.removeItem('support-session-name')
    setAuthToken(null); setAuthenticatedEmail(null); setAuthenticatedName(null)
    setLoginOpen(false); setPendingLoginMessage(null); pendingLoginMessageRef.current = null; pendingVoiceLoginRef.current = false; setLoginPassword(''); setLoginError('')
  }

  const startNew = () => { void voiceClientRef.current?.disconnect(); voiceClientRef.current = null; voiceTurnRef.current = null; voiceConversationIdRef.current = null; voiceEndChatRef.current = false; voiceLogoutPendingRef.current = false; pendingVoiceLoginRef.current = false; setVoiceState('off'); setVoiceLevel(0); signOut(); setConversationId(null); setMessages([]); setTrace([]); setActivePromptId(null); setLiveExpanded([]); setSelected(null); setPage('chat') }
  const toggleLiveTrace = (id: string) => setLiveExpanded(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id])
  const filtered = useMemo(() => (overview?.conversations || []).filter(c => `${c.title} ${c.id}`.toLowerCase().includes(query.toLowerCase())), [overview, query])

  return <>
    <div className="shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><LifeBuoy size={19} /></div><div><strong>{companyName}</strong><small>SUPPORT DESK</small></div><button className="icon-button mobile-hide"><MoreHorizontal size={18} /></button></div>
      <div className="nav-label">WORKSPACE</div>
      <button className={`nav-item ${page === 'chat' ? 'active' : ''}`} onClick={startNew}><MessageSquare size={17} /> Conversations <span className="nav-count">{overview?.usage.conversations || 0}</span></button>
      <button className={`nav-item ${page === 'analytics' ? 'active' : ''}`} onClick={() => { setPage('analytics'); setSelected(null); loadOverview() }}><Activity size={17} /> Live analytics</button>
      <button className={`nav-item ${page === 'queue' ? 'active' : ''}`} onClick={() => { setPage('queue'); setSelected(null); if (adminKey) void loadEscalations(adminKey) }}><Database size={17} /> Escalation queue <span className="nav-count">{ticketCount}</span></button>
      <button className={`nav-item ${page === 'tickets' ? 'active' : ''}`} onClick={() => { setPage('tickets'); setSelected(null); if (adminKey) void loadSupportTickets(adminKey) }}><LifeBuoy size={17} /> Support tickets <span className="nav-count">{supportTicketCount}</span></button>
      <div className="sidebar-rule" />
      <div className="history-head"><span>RECENT CONVERSATIONS</span><button className="mini-action" onClick={loadOverview}><ArrowUpRight size={14} /></button></div>
      <div className="history-list">{(overview?.conversations || []).slice(0, 8).map(c => <button className="history-item" key={c.id} onClick={() => { setPage('analytics'); setSelected(c) }}><span className="history-dot" /><span className="history-title">{c.title || 'New conversation'}</span><small>{timeAgo(c.updated_at)}</small></button>)}{!overview?.conversations.length && <div className="empty-history">Your conversations will appear here.</div>}</div>
      <div className="sidebar-bottom"><button className="nav-item"><Settings2 size={17} /> Settings</button></div>
    </aside>

    <main className="main">
      <header className="topbar"><div className="breadcrumbs"><span>Workspace</span><ChevronRight size={14} /><b>{page === 'chat' ? 'Conversations' : page === 'queue' ? 'Escalation queue' : page === 'tickets' ? 'Support tickets' : selected ? 'Conversation details' : 'Live analytics'}</b></div><div className="top-actions">{authToken && <button className="session-chip" onClick={signOut}>Signed in · {authenticatedEmail || 'Account'} <X size={12} /></button>}<div className="environment"><i className={provider === 'gemini' ? 'green-dot' : 'amber-dot'} /> {provider === 'gemini' ? 'Gemini connected' : 'Demo mode'}</div><div className="environment"><i className={database === 'postgres' ? 'green-dot' : 'amber-dot'} /> {database === 'postgres' ? 'PostgreSQL connected' : 'In-memory fallback'}</div><div className="environment"><i className={escalationQueue === 'redis' ? 'green-dot' : 'amber-dot'} /> {escalationQueue === 'redis' ? 'Redis connected' : 'Redis unavailable'}</div><button className="icon-button"><CircleHelp size={17} /></button></div></header>
      {page === 'chat' ? <section className="chat-layout">
        <div className="chat-center">
          <div className="chat-heading"><div><div className="eyebrow">CUSTOMER CARE <span className="live-dot" /> LIVE</div><h1>How can we help?</h1><p>Our support team is here for you, whenever you need us.</p></div><button className="new-chat" onClick={startNew}><Plus size={16} /> New conversation</button></div>
          <div className="chat-panel">
            <div className="support-banner"><div className="support-icon"><Headphones size={20} /></div><div><b>{companyName} Support</b><small><span className="online-dot" /> {status}</small></div><button className="icon-button"><MoreHorizontal size={18} /></button></div>
            <div className="message-area" ref={messageAreaRef} onScroll={event => { const area = event.currentTarget; stickToBottomRef.current = area.scrollHeight - area.scrollTop - area.clientHeight < 80 }}>
              {messages.length === 0 ? <div className="welcome"><div className="welcome-icon"><Sparkles size={22} /></div><h2>Hi there <span>✳</span></h2><p>I'm your {companyName} support assistant. Ask general questions without signing in; for personal account details, I'll open a secure sign-in window.</p><div className="suggestions">{samples.map(s => <button key={s} onClick={() => send(s)}>{s}<ArrowUpRight size={14} /></button>)}</div></div> : <div className="messages">{messages.map((m, i) => <div className={`message-row ${m.role}`} key={m.id || i}>{m.role === 'assistant' && <div className="assistant-avatar"><Sparkles size={14} /></div>}<div className="message-bubble">{m.content || <span className="typing"><i /><i /><i /></span>}</div>{m.role === 'user' && <div className="user-avatar">{authToken && authenticatedName ? getInitials(authenticatedName) : <UserRound size={14} />}</div>}</div>)}{isSending && <div className="working-label"><span className="pulse" /> Assistant is working</div>}</div>}
            </div>
            <div className="composer-wrap"><div className="composer"><textarea value={draft} onChange={e => setDraft(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} placeholder="Write a message…" rows={1} /><div className="composer-actions"><span><Command size={12} /> Enter to send</span><button className="send-button" disabled={!draft.trim() || isSending} onClick={() => send()}><Send size={15} /></button></div></div><div className="voice-composer-row"><button className={`voice-button ${voiceState}`} onClick={() => void toggleVoice()} disabled={voiceState === 'connecting' || (provider !== 'gemini' && voiceState === 'off')} aria-label={voiceClientRef.current ? 'Stop voice conversation' : 'Start voice conversation'}>{voiceClientRef.current ? <MicOff size={17} /> : <Mic size={17} />}{voiceState === 'connecting' ? 'Connecting…' : voiceClientRef.current ? 'Stop voice' : 'Talk'}</button><div className={`voice-wave ${voiceState}`} aria-label={voiceState === 'listening' ? 'Microphone waveform' : 'Voice waveform'}>{Array.from({ length: 17 }, (_, index) => <i key={index} style={{ height: `${5 + voiceLevel * (7 + ((index * 7) % 16))}px` }} />)}</div><span className="voice-mode-label">{voiceState === 'listening' ? 'Listening · you can type anytime' : voiceState === 'speaking' ? 'Speaking · interrupt by talking' : voiceState === 'connecting' ? 'Starting secure voice…' : voiceState === 'error' ? voiceError || 'Voice unavailable · type instead' : provider === 'gemini' ? 'Optional voice' : 'Voice requires Gemini'}</span></div><div className="composer-foot"><span>Powered by {companyName} AI</span><span><ShieldCheck size={12} /> Your conversations are private</span></div></div>
          </div>
          <div className="chat-foot"><span><span className="online-dot" /> All systems operational</span><span>Average response time <b>~2 sec</b></span></div>
        </div>
        <aside className="context-panel"><div className="context-top"><div><div className="eyebrow">SESSION OVERVIEW</div><h3>Conversation insights</h3></div><button className="icon-button"><MoreHorizontal size={17} /></button></div><div className="context-card"><div className="card-label"><span>SESSION STATUS</span><span className="status-pill">{isSending ? 'Active' : 'Ready'}</span></div><div className="session-row"><span className="session-icon"><MessageSquare size={17} /></span><div><b>{conversationId ? conversationId.slice(0, 18) : 'New conversation'}</b><small>{messages.length} messages · just now</small></div></div></div><div className="context-card"><div className="card-label"><span>ASSISTANT ACTIVITY</span><span className="activity-value"><i className="green-dot" /> Live</span></div><div className="metric-line"><span>Agent steps</span><b>{trace.length}</b></div><div className="metric-line"><span>Tools used</span><b>{trace.filter(t => t.type === 'tool_call').length}</b></div><div className="metric-line"><span>Avg. response time</span><b>{trace.filter(t => t.type === 'turn').slice(-1)[0]?.duration_ms ?? '—'}{trace.length ? ' ms' : ''}</b></div></div><div className="context-card activity-card live-trace-card"><div className="card-label"><span>PROMPTS &amp; STEPS</span><button className="text-link" onClick={() => setPage('analytics')}>View all</button></div>{trace.length ? <div className="trace-tree" ref={traceTreeRef} onScroll={event => { const tree = event.currentTarget; traceStickToBottomRef.current = tree.scrollHeight - tree.scrollTop - tree.clientHeight < 60 }}><TraceTree events={trace} expanded={liveExpanded} toggle={toggleLiveTrace}/></div> : <div className="activity-empty"><div className="activity-empty-icon"><Activity size={17} /></div><b>Nothing happening yet</b><small>Prompt steps will appear here</small></div>}</div><div className="privacy-note"><ShieldCheck size={15} /><span><b>Privacy protected</b><small>Customer data is handled securely and never used to train models.</small></span></div></aside>
      </section> : page === 'analytics' ? <Analytics overview={overview} selected={selected} onSelect={setSelected} filtered={filtered} query={query} setQuery={setQuery} expanded={expanded} setExpanded={setExpanded} database={database} provider={provider} /> : page === 'queue' ? <EscalationQueuePage kind="escalations" adminKey={adminKey} setAdminKey={setAdminKey} tickets={tickets} ticketCount={ticketCount} busy={queueBusy} error={queueError} load={loadEscalations} clear={clearEscalations} /> : <EscalationQueuePage kind="support" adminKey={adminKey} setAdminKey={setAdminKey} tickets={supportTickets} ticketCount={supportTicketCount} busy={queueBusy} error={queueError} load={loadSupportTickets} clear={clearSupportTickets} />}
    </main>
    </div>
    {loginOpen && <div className="login-backdrop" role="presentation">
      <section className="login-dialog" role="dialog" aria-modal="true" aria-labelledby="login-title">
        <button className="login-close" type="button" aria-label="Close sign in" onClick={closeLogin}><X size={18} /></button>
        <div className="login-icon"><ShieldCheck size={21} /></div>
        <div className="eyebrow">MOCK ACCOUNT SIGN-IN</div>
        <h2 id="login-title">Sign in to continue</h2>
        <p>General support is public. Sign in only when you need personal account information.</p>
        <form onSubmit={submitLogin}>
          <label>Email address<input type="email" autoComplete="email" value={loginEmail} onChange={e => setLoginEmail(e.target.value)} required /></label>
          <label>Password<input type="password" autoComplete="current-password" value={loginPassword} onChange={e => setLoginPassword(e.target.value)} required /></label>
          {loginError && <div className="login-error" role="alert">{loginError}</div>}
          <div className="login-actions"><button type="button" className="login-cancel" onClick={closeLogin}>Cancel</button><button type="submit" className="login-submit" disabled={isLoggingIn || isSending}>{isLoggingIn ? 'Signing in…' : 'Sign in securely'}</button></div>
        </form>
        <small className="login-demo-note">Demo sign-in checks the salted password hash stored for each mock customer.</small>
      </section>
    </div>}
  </>
}

function EscalationQueuePage({ kind, adminKey, setAdminKey, tickets, ticketCount, busy, error, load, clear }: { kind: 'escalations' | 'support'; adminKey: string; setAdminKey: (key: string) => void; tickets: EscalationTicket[] | SupportTicket[]; ticketCount: number; busy: boolean; error: string; load: (key?: string) => Promise<void>; clear: () => Promise<void> }) {
  const [keyDraft, setKeyDraft] = useState(adminKey)
  const unlock = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    sessionStorage.setItem('support-admin-key', keyDraft)
    setAdminKey(keyDraft)
    void load(keyDraft)
  }
  const lock = () => {
    sessionStorage.removeItem('support-admin-key')
    setAdminKey('')
    setKeyDraft('')
  }
  const support = kind === 'support'
  const copy = support
    ? { title: 'Support tickets', stream: 'support:tickets', intro: 'Support cases opened for signed-in customers. Clearing this queue does not delete the cases in the database.', noun: 'support tickets', empty: 'There are no support tickets.', button: 'Open tickets' }
    : { title: 'Escalation queue', stream: 'support:escalations', intro: 'Review pending support tickets. Clearing tickets is separate from starting a new conversation.', noun: 'escalation tickets', empty: 'The escalation queue is empty.', button: 'Open queue' }
  return <section className="analytics-page">
    <div className="analytics-title"><div><div className="eyebrow">OPERATIONS <span className="live-dot" /> REDIS STREAM · {copy.stream}</div><h1>{copy.title}</h1><p>{copy.intro}</p></div>{adminKey && <div style={{ display: 'flex', gap: 8 }}><button className="new-chat" disabled={busy} onClick={() => void load()}>{busy ? 'Loading…' : 'Refresh'}</button><button className="new-chat" disabled={busy || ticketCount === 0} onClick={() => void clear()}>Clear queue</button></div>}</div>
    {error && <div className="login-error" role="alert">{error}</div>}
    {!adminKey ? <div className="panel" style={{ maxWidth: 480 }}><div className="panel-heading"><div><h3>Admin access required</h3><p>Enter ADMIN_API_KEY from backend/.env to view and manage {copy.noun}.</p></div></div><form onSubmit={unlock} style={{ display: 'grid', gap: 12, marginTop: 16 }}><input type="password" autoComplete="current-password" value={keyDraft} onChange={event => setKeyDraft(event.target.value)} placeholder="Admin API key" required /><button className="login-submit" disabled={busy}>{busy ? 'Loading…' : copy.button}</button></form></div> : <div className="panel">
      <div className="panel-heading"><div><h3>{ticketCount} queued {ticketCount === 1 ? 'ticket' : 'tickets'}</h3><p>Tickets remain here until an administrator clears the queue.</p></div><button className="text-link" onClick={lock}>Sign out admin</button></div>
      {tickets.length === 0 ? <div className="table-empty">{copy.empty}</div> : support ? <div style={{ marginTop: 16, display: 'grid', gap: 10 }}>{(tickets as SupportTicket[]).map(ticket => <article className="context-card" key={ticket.id} style={{ margin: 0 }}><div className="card-label"><span>SUPPORT CASE · {ticket.case_id}</span><span className="status-pill">{ticket.status}</span></div><div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', gap: 8 }}><strong>{ticket.reference}</strong><small>{new Date(ticket.created_at).toLocaleString()}</small></div><p style={{ fontSize: 12, lineHeight: 1.5 }}>{ticket.subject}</p><small>{ticket.customer_email || 'Unknown customer'}</small><small style={{ display: 'block', marginTop: 5 }}>Conversation: {ticket.conversation_id}</small></article>)}</div> : <div style={{ marginTop: 16, display: 'grid', gap: 10 }}>{(tickets as EscalationTicket[]).map(ticket => <article className="context-card" key={ticket.id} style={{ margin: 0 }}><div className="card-label"><span>{ticket.category.replace(/_/g, ' ').toUpperCase()}</span><span className="status-pill">{ticket.status}</span></div><div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', gap: 8 }}><strong>{ticket.reference}</strong><small>{new Date(ticket.created_at).toLocaleString()}</small></div><p style={{ fontSize: 12, lineHeight: 1.5 }}>{ticket.reason}</p><small>{ticket.customer_email || 'Unauthenticated'}{ticket.order_id ? ` · ${ticket.order_id}` : ''}{ticket.amount ? ` · $${ticket.amount}` : ''}</small><small style={{ display: 'block', marginTop: 5 }}>Conversation: {ticket.conversation_id}</small></article>)}</div>}
    </div>}
  </section>
}

function Analytics({ overview, selected, onSelect, filtered, query, setQuery, expanded, setExpanded, database, provider }: { overview: Overview | null; selected: Conversation | null; onSelect: (c: Conversation | null) => void; filtered: Conversation[]; query: string; setQuery: (q: string) => void; expanded: string[]; setExpanded: (x: string[]) => void; database: string; provider: string }) {
  const usage = overview?.usage || { conversations: 0, messages: 0, tool_calls: 0, avg_latency_ms: 0 }
  const toggle = (id: string) => setExpanded(expanded.includes(id) ? expanded.filter(x => x !== id) : [...expanded, id])
  const times = overview?.conversations.slice(0, 7).map(c => c.duration_ms) || []
  return <section className="analytics-page">
    <div className="analytics-title"><div><div className="eyebrow">OPERATIONS <span className="live-dot" /> REAL TIME</div><h1>{selected ? 'Conversation trace' : 'Live analytics'}</h1><p>{selected ? selected.title : 'Understand how your support agent is performing.'}</p></div>{selected && <button className="new-chat" onClick={() => onSelect(null)}><ArrowLeft size={15} /> Back to analytics</button>}</div>
    {!selected && <div className="usage-inline"><span>Usage snapshot</span><b>{((usage.input_tokens || 0) + (usage.output_tokens || 0)).toLocaleString()} model tokens</b><small>{(usage.input_tokens || 0).toLocaleString()} input · {(usage.output_tokens || 0).toLocaleString()} output · {usage.messages} messages</small></div>}
    {!selected ? <><div className="stat-grid"><Stat icon={<MessageSquare size={17}/>} label="Conversations" value={usage.conversations} trend="Across all sessions"/><Stat icon={<Activity size={17}/>} label="Messages processed" value={usage.messages} trend="Customer + assistant"/><Stat icon={<Database size={17}/>} label="Tool calls" value={usage.tool_calls} trend="Agent actions"/><Stat icon={<Clock3 size={17}/>} label="Average latency" value={`${usage.avg_latency_ms} ms`} trend="Across recorded events"/></div><div className="analytics-grid"><div className="panel chart-panel"><div className="panel-heading"><div><h3>Response latency</h3><p>End-to-end processing time by conversation</p></div><button className="range-select">Last 7 conversations <ChevronDown size={14}/></button></div><div className="chart-area"><div className="chart-y"><span>4s</span><span>3s</span><span>2s</span><span>1s</span><span>0</span></div><div className="chart"><div className="chart-grid"><i/><i/><i/><i/><i/></div><div className="bars">{Array.from({length:7}, (_, i) => { const val = times[i] || [1.3, 2.1, 1.7, 2.8, 1.2, 3.1, 2.3][i]; const h = Math.min(94, 16 + val / 4000 * 80); return <div className="bar-slot" key={i}><div className="bar" style={{height:`${h}%`}}/><small>{i === 6 ? 'Now' : `${6-i}h ago`}</small></div>})}</div></div></div></div><div className="panel provider-panel"><div className="panel-heading"><div><h3>System status</h3><p>Connected services</p></div><span className="all-good"><i className="green-dot"/> Operational</span></div><Service icon={<Sparkles size={16}/>} name="Gemini API" detail="Agent model · Flash" state="Connected"/><Service icon={<Database size={16}/>} name="PostgreSQL" detail="Customer and trace data" state={database === 'postgres' ? 'Connected' : 'Demo mode'}/><Service icon={<BookOpen size={16}/>} name="Qdrant" detail="Manual vector search" state="Configured"/><Service icon={<AudioLines size={16}/>} name="Voice channel" detail="Gemini Live API" state={provider === 'gemini' ? 'Configured' : 'Not configured'}/></div></div><div className="panel history-panel"><div className="panel-heading"><div><h3>Conversation history</h3><p>Inspect prompts, tool calls, and timing by session</p></div><label className="search-box"><Search size={15}/><input value={query} onChange={e => setQuery(e.target.value)} placeholder="Search conversations"/></label></div><div className="table-head"><span>CONVERSATION</span><span>MESSAGES</span><span>TRACE EVENTS</span><span>EST. COST</span><span>LATENCY</span><span>LAST ACTIVE</span><span/></div>{filtered.map(c=><button className="table-row" key={c.id} onClick={()=>onSelect(c)}><span className="conv-title"><div className="conversation-avatar"><MessageSquare size={14}/></div><span><b>{c.title || 'New conversation'}</b><small>{c.id}</small></span></span><span>{c.message_count}</span><span>{c.events.length}</span><span className="conversation-cost">{summarizeModelUsage(c.events) ? formatEstimatedCost(summarizeModelUsage(c.events)!.totalCost) : '—'}</span><span>{c.duration_ms} ms</span><span>{timeAgo(c.updated_at)}</span><ChevronRight size={15}/></button>)}{!filtered.length && <div className="table-empty">No conversations recorded yet. Send a message in the chat to see live traces.</div>}</div></> : <div className="detail-grid"><div className="panel trace-panel"><div className="panel-heading"><div><h3>Prompt and tool history</h3><p>Expandable execution tree for {selected.id}</p></div><span className="status-pill">{selected.status}</span></div><div className="trace-tree"><TraceTree events={selected.events} expanded={expanded} toggle={toggle}/></div></div><RagConfidencePanel events={selected.events}/><div className="panel timing-panel"><div className="panel-heading"><div><h3>Timing breakdown</h3><p>Recorded event durations</p></div></div><div className="latency-total"><span>Total recorded latency</span><b>{selected.duration_ms} <small>ms</small></b></div>{selected.events.map(e=><div className="timing-row" key={e.id}><div><span className="timing-dot"/><b>{e.label}</b></div><span>{e.duration_ms} ms</span></div>)}{!selected.events.length && <div className="table-empty">No event timings recorded for this conversation.</div>}<div className="session-meta"><span>Messages</span><b>{selected.message_count}</b></div><div className="session-meta"><span>Storage</span><b>{database === 'postgres' ? 'PostgreSQL' : 'Demo memory'}</b></div></div><div className="panel transcript-panel"><div className="panel-heading"><div><h3>Conversation transcript</h3><p>User prompts and assistant replies</p></div></div>{selected.messages.map((m,i)=><div className={`transcript-row ${m.role}`} key={i}><span className="transcript-role">{m.role === 'user' ? 'CUSTOMER' : 'ASSISTANT'}</span><p>{m.content}</p></div>)}{!selected.messages.length && <div className="table-empty">No saved transcript.</div>}</div></div>}
  </section>
}
function RagConfidencePanel({events}: {events: Trace[]}) {
  const searches = events
    .filter(event => event.type === 'tool_result' && event.metadata?.name === 'search_manual')
    .map(event => {
      const result = event.metadata?.result as Record<string, unknown> | undefined
      return {event, result}
    })
    .filter((item): item is {event: Trace; result: Record<string, unknown>} => Boolean(item.result && typeof item.result.confidence_score === 'number'))
  return <div className="panel rag-confidence-panel">
    <div className="panel-heading"><div><h3>Manual search confidence</h3><p>Retrieval score compared with the escalation threshold</p></div></div>
    {searches.length === 0 ? <div className="table-empty">No manual search confidence was recorded for this conversation.</div> : searches.map(({event, result}) => {
      const score = Number(result.confidence_score)
      const threshold = Number(result.confidence_threshold)
      const lowConfidence = Boolean(result.low_confidence)
      const details = (result.confidence_details || {}) as Record<string, unknown>
      const percent = Math.max(0, Math.min(100, score * 100))
      return <article className="rag-confidence-result" key={event.id}>
        <div className="rag-confidence-heading"><b>{String(result.retrieval || 'retrieval').replace(/-/g, ' ')}</b><span className={lowConfidence ? 'confidence-badge low' : 'confidence-badge'}>{lowConfidence ? 'Escalated' : 'Passed'}</span></div>
        <div className="confidence-score-line"><span>Confidence</span><b>{score.toFixed(3)} <small>/ threshold {threshold.toFixed(3)}</small></b></div>
        <div className="confidence-meter" role="img" aria-label={`Confidence ${score.toFixed(3)}, threshold ${threshold.toFixed(3)}`}><i style={{width: `${percent}%`}}/><span style={{left: `${Math.max(0, Math.min(100, threshold * 100))}%`}}/></div>
        <small className="confidence-formula">{String(details.formula || (result.retrieval === 'qdrant' ? 'Cosine similarity (legacy trace; raw filtered scores unavailable)' : 'Confidence score returned by retrieval'))}</small>
        {Array.isArray(details.top_scores) && <small className="confidence-formula">Top chunk scores: {(details.top_scores as number[]).map(value => Number(value).toFixed(3)).join(' / ') || 'No chunks returned'}</small>}
        {Array.isArray(details.query_terms) && <small className="confidence-formula">Matched terms: {((details.matched_terms as string[]) || []).join(', ') || 'none'} of {((details.query_terms as string[]) || []).join(', ') || 'no searchable terms'}</small>}
        <small className="confidence-formula">{String(event.metadata?.duration_ms ?? event.duration_ms)} ms - {String(result.matches ? (result.matches as unknown[]).length : 0)} qualifying manual chunks</small>
      </article>
    })}
  </div>
}

function TraceTree({events, expanded, toggle}: {events: Trace[]; expanded: string[]; toggle: (id:string)=>void}) {
  const groups: { prompt: Trace; steps: Trace[] }[] = []
  const orphanEvents: Trace[] = []
  for (const event of events) {
    if (event.type === 'user_prompt') {
      groups.push({ prompt: event, steps: [] })
      continue
    }
    const parentGroup = event.parent_id ? groups.find(group => group.prompt.id === event.parent_id) : groups[groups.length - 1]
    if (parentGroup) parentGroup.steps.push(event)
    else orphanEvents.push(event)
  }
  return <>
    {groups.map(({ prompt, steps }) => <div className="trace-node" key={prompt.id}>
      <button className="trace-event" style={{ background: '#f8faf7', padding: '12px 9px', borderRadius: 7 }} onClick={() => toggle(prompt.id)}>
        <span className="trace-chevron">{expanded.includes(prompt.id) ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}</span>
        <span className="trace-icon"><MessageSquare size={14}/></span>
        <span className="trace-event-label"><b>{prompt.detail || prompt.label}</b><small>Customer prompt · {steps.length} steps · {new Date(prompt.started_at || Date.now()).toLocaleTimeString()}</small>{summarizeModelUsage(steps) && <small className="cost-summary">Agent API estimate (standard paid rates): {formatEstimatedCost(summarizeModelUsage(steps)!.inputCost)} input + {formatEstimatedCost(summarizeModelUsage(steps)!.outputCost)} output = {formatEstimatedCost(summarizeModelUsage(steps)!.totalCost)} · {summarizeModelUsage(steps)!.inputTokens.toLocaleString()} in / {summarizeModelUsage(steps)!.outputTokens.toLocaleString()} out tokens</small>}</span>
      </button>
      {expanded.includes(prompt.id) && <>
        <div className="trace-detail"><pre>{prompt.detail || JSON.stringify(prompt.metadata || {}, null, 2)}</pre></div>
        <div style={{ marginLeft: 20, paddingLeft: 10, borderLeft: '1px solid #dfe5de' }}>
          {steps.map((step, index) => <TraceStep key={step.id} event={step} expanded={expanded.includes(step.id)} toggle={() => toggle(step.id)} last={index === steps.length - 1}/>) }
        </div>
      </>}
    </div>)}
    {orphanEvents.map((event, index) => <TraceStep key={event.id} event={event} expanded={expanded.includes(event.id)} toggle={() => toggle(event.id)} last={index === orphanEvents.length - 1}/>) }
  </>
}

function TraceStep({event, expanded, toggle, last}: {event: Trace; expanded: boolean; toggle: () => void; last: boolean}) {
  const meta = event.metadata || {}
  const hasModelCost = event.type === 'model_call' && typeof meta.estimated_cost_usd === 'number'
  return <div className="trace-node">
    <button className="trace-event" onClick={toggle}>
      <span className="trace-chevron">{expanded ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}</span>
      <span className={"trace-icon " + event.type}><Activity size={14}/></span>
      <span className="trace-event-label"><b>{event.label}</b><small>{event.type.replace(/_/g,' ')} · {new Date(event.started_at || Date.now()).toLocaleTimeString()}</small>{hasModelCost && <small className="cost-summary">{Number(meta.input_tokens || 0).toLocaleString()} in ({formatEstimatedCost(Number(meta.input_cost_usd || 0))}) · {Number(meta.output_tokens || 0).toLocaleString()} out ({formatEstimatedCost(Number(meta.output_cost_usd || 0))}) · total {formatEstimatedCost(Number(meta.estimated_cost_usd))}</small>}</span>
      <span className="trace-duration">{event.duration_ms} ms</span>
    </button>
    {expanded && <div className="trace-detail"><pre>{event.detail || JSON.stringify(event.metadata || {}, null, 2)}</pre></div>}
    {!last && <div className="trace-connector"/>}
  </div>
}
function summarizeModelUsage(events: Trace[]) {
  const calls = events.filter(event => event.type === 'model_call' && typeof event.metadata?.estimated_cost_usd === 'number')
  if (!calls.length) return null
  const sum = (key: string) => calls.reduce((total, event) => total + Number(event.metadata?.[key] || 0), 0)
  return {
    inputTokens: sum('input_tokens'),
    outputTokens: sum('output_tokens'),
    inputCost: sum('input_cost_usd'),
    outputCost: sum('output_cost_usd'),
    totalCost: sum('estimated_cost_usd'),
  }
}
function formatEstimatedCost(value: number) { return '$' + value.toFixed(6) }
function Stat({icon,label,value,trend}: {icon:React.ReactNode;label:string;value:string|number;trend:string}) {return <div className="stat-card"><div className="stat-top"><span>{label}</span><div>{icon}</div></div><strong>{value}</strong><small>{trend}</small></div>}
function Service({icon,name,detail,state}: {icon:React.ReactNode;name:string;detail:string;state:string}) {return <div className="service-row"><span className="service-icon">{icon}</span><span><b>{name}</b><small>{detail}</small></span><span className={`service-state ${state==='Connected'?'connected':''}`}>{state}</span></div>}
function timeAgo(value: string) { const diff=Math.max(0,Math.floor((Date.now()-new Date(value).getTime())/60000)); return diff<1?'now':diff<60?`${diff}m ago`:`${Math.floor(diff/60)}h ago` }
function getInitials(name: string) { return name.trim().split(/\s+/).filter(Boolean).slice(0, 2).map(part => part.charAt(0).toUpperCase()).join('') }
export default App
