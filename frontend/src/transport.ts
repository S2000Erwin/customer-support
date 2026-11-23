export type TransportEvent = { type: string; data: Record<string, unknown> }

export interface ChatTransport {
  send(conversationId: string | null, message: string, accessToken?: string | null, resumeLogin?: boolean): AsyncGenerator<TransportEvent>
}

// Text transport today. A Live API transport can later implement a related
// session interface while keeping chat state, tools, and transcript rendering shared.
export class HttpChatTransport implements ChatTransport {
  constructor(private readonly apiUrl: string) {}

  async *send(conversationId: string | null, message: string, accessToken?: string | null, resumeLogin = false): AsyncGenerator<TransportEvent> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`
    const response = await fetch(`${this.apiUrl}/api/chat`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ conversation_id: conversationId, message, resume_login: resumeLogin }),
    })
    if (!response.ok || !response.body) {
      if (response.status === 401) throw new Error('Your sign-in session expired. Sign in again to continue with account support.')
      throw new Error('Could not connect to support.')
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() || ''
      for (const block of blocks) {
        const type = block.match(/^event: (.+)$/m)?.[1]
        const raw = block.match(/^data: (.+)$/m)?.[1]
        if (type && raw) yield { type, data: JSON.parse(raw) as Record<string, unknown> }
      }
    }
  }
}

export type VoiceSessionEvent = {
  type: 'connected' | 'input_transcript' | 'output_transcript' | 'tool_call' | 'tool_result' | 'guardrail' | 'turn_complete' | 'interrupted' | 'level' | 'error'
  data: Record<string, unknown>
}

type LiveToolCall = { id: string; name: string; args?: Record<string, unknown> }
type LiveConfig = { responseModalities?: string[]; [key: string]: unknown }

/** Browser-side Gemini Live transport. Only a short-lived token is exposed to the client. */
export class GeminiLiveVoiceSession {
  private socket: WebSocket | null = null
  private microphone: MediaStream | null = null
  private audioContext: AudioContext | null = null
  private processor: ScriptProcessorNode | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private analyser: AnalyserNode | null = null
  private animationFrame = 0
  private playbackSources = new Set<AudioBufferSourceNode>()
  private receivedAudioChunks = 0
  private onFirstOutputAudio: (() => void) | null = null
  private nextPlayTime = 0
  private listeners = new Set<(event: VoiceSessionEvent) => void>()
  private setupReady: Promise<void> | null = null
  private currentUserText = ''
  private microphonePaused = false
  private recentAssistantText = ''
  private modelSpeaking = false
  private lastBargeInAudioAt = 0
  private intentionalClose = false
  // Playback routing: sources -> outputBus -> outputAnalyser -> (loopback | speakers)
  private outputBus: GainNode | null = null
  private outputAnalyser: AnalyserNode | null = null
  private loopbackConnections: RTCPeerConnection[] = []
  private playbackElement: HTMLAudioElement | null = null
  // Echo-aware barge-in detection.
  private echoCoupling = 0.3
  private previousOutputRms = 0
  private speechFrames = 0
  private preRoll: Uint8Array[] = []
  private discardModelAudio = false
  private interruptEmitted = false

  constructor(
    private readonly apiUrl: string,
    private readonly conversationId: string,
    private accessToken: string | null,
    private readonly history: { role: 'user' | 'assistant'; content: string }[],
  ) {}

  onEvent(handler: (event: VoiceSessionEvent) => void) {
    this.listeners.add(handler)
    return () => this.listeners.delete(handler)
  }

  async connect() {
    this.intentionalClose = false
    // Create/resume audio devices in the click's user-activation window before
    // the token HTTP request and WebSocket setup add network latency.
    await this.prepareAudio()
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (this.accessToken) headers.Authorization = `Bearer ${this.accessToken}`
    const hasHistory = this.history.some(item => item.content.trim())
    const response = await fetch(`${this.apiUrl}/api/voice/session`, { method: 'POST', headers, body: JSON.stringify({ has_history: hasHistory }) })
    let session: Record<string, any>
    try { session = await response.json() as Record<string, any> }
    catch { throw new Error(`Voice setup endpoint returned HTTP ${response.status}. Restart the FastAPI backend and check VITE_API_URL.`) }
    if (!response.ok) throw new Error(`Voice token request failed (HTTP ${response.status}): ${session.detail || 'Could not create a Gemini Live session.'}`)

    this.setupReady = new Promise<void>((resolve, reject) => {
      const token = encodeURIComponent(String(session.token))
      this.socket = new WebSocket(`wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained?access_token=${token}`)
      this.socket.binaryType = 'arraybuffer'
      let setupSettled = false
      const failSetup = (error: Error) => {
        if (!setupSettled) {
          setupSettled = true
          window.clearTimeout(timeout)
          reject(error)
        }
        this.emit('error', { message: error.message })
      }
      const timeout = window.setTimeout(() => failSetup(new Error('Gemini Live did not finish setup in 15 seconds. Check the model name, API access, and browser network connection.')), 15000)
      this.socket.onopen = () => this.send({ setup: { model: `models/${session.model}`, ...session.config } })
      this.socket.onmessage = event => {
        void this.handleSocketMessage(event.data, failSetup, () => {
          if (!setupSettled) { setupSettled = true; window.clearTimeout(timeout); resolve() }
          this.sendHistory()
          this.emit('connected', {})
        })
      }
      this.socket.onerror = () => {
        failSetup(new Error('Gemini Live WebSocket connection failed before setup completed. Check internet access, proxy rules, and model access.'))
      }
      this.socket.onclose = event => {
        window.clearTimeout(timeout)
        if (this.intentionalClose) return
        const detail = event.reason
          ? `Gemini Live connection closed (${event.code || 'unknown'}): ${event.reason}`
          : `Gemini Live connection closed (${event.code || 'unknown'}).`
        if (!setupSettled) failSetup(new Error(`Gemini Live closed before setup completed: ${detail}`))
        else this.emit('error', { message: detail })
      }
    })
    await this.setupReady
    this.startMicrophone()
  }

  private async handleSocketMessage(
    frame: string | Blob | ArrayBuffer,
    failSetup: (error: Error) => void,
    onSetupComplete: () => void,
  ) {
    try {
      const raw = typeof frame === 'string'
        ? frame
        : frame instanceof Blob
          ? await frame.text()
          : new TextDecoder().decode(frame)
      const message = JSON.parse(raw) as Record<string, any>
      if (message.error) {
        const detail = message.error.message || JSON.stringify(message.error)
        failSetup(new Error(`Gemini Live rejected the session setup: ${detail}`))
        this.socket?.close(1000, 'Gemini setup rejected')
        return
      }
      if (message.setupComplete) {
        onSetupComplete()
        return
      }
      this.handleMessage(message)
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'Unknown frame parsing error.'
      const message = new Error(`Could not decode a Gemini Live WebSocket frame (${frame instanceof Blob ? 'Blob' : frame instanceof ArrayBuffer ? 'ArrayBuffer' : 'text'}): ${detail}`)
      failSetup(message)
      this.emit('error', { message: message.message })
    }
  }

  async invokeTool(call: LiveToolCall) {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (this.accessToken) headers.Authorization = `Bearer ${this.accessToken}`
    const response = await fetch(`${this.apiUrl}/api/voice/tool`, {
      method: 'POST', headers,
      body: JSON.stringify({ conversation_id: this.conversationId, tool_name: call.name, arguments: call.args || {}, current_user_message: this.currentUserText }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || `Could not run ${call.name}.`)
    if (data.result?.login_required && !this.currentUserText.trim()) await this.waitForUserTranscript(1200)
    this.emit('tool_result', { id: call.id, name: call.name, arguments: call.args || {}, result: data.result, current_user_message: this.currentUserText })
    if (data.result?.low_confidence && data.result?.escalation_required) {
      const reference = data.result?.escalation?.reference
      this.emit('guardrail', { text: reference
        ? `I couldn’t find a reliable answer in the support manual, so I’ve sent your question to a support specialist. Your reference number is ${reference}.`
        : 'I couldn’t find a reliable answer in the support manual. A support specialist should review this, but the ticket queue is unavailable right now. Please contact support directly.' })
      void this.disconnect()
      return
    }
    this.send({ toolResponse: { functionResponses: [{ id: call.id, name: call.name, response: data.result }] } })
  }

  private waitForUserTranscript(timeoutMs: number) {
    if (this.currentUserText.trim()) return Promise.resolve()
    return new Promise<void>(resolve => {
      let finished = false
      let timeout = 0
      let unsubscribe = () => {}
      const finish = () => {
        if (finished) return
        finished = true
        window.clearTimeout(timeout)
        unsubscribe()
        resolve()
      }
      unsubscribe = this.onEvent(event => {
        if (event.type === 'input_transcript' && String(event.data.text || '').trim()) finish()
      })
      timeout = window.setTimeout(finish, timeoutMs)
    })
  }

  setCurrentUserText(text: string) { this.currentUserText = text }

  getCurrentUserText() { return this.currentUserText }

  setAccessToken(token: string) { this.accessToken = token }

  getAccessToken() { return this.accessToken }

  pause() { this.pauseInput() }

  pauseInput() { this.microphonePaused = true }

  resume() { this.microphonePaused = false }

  async speakText(text: string, onAudioStarted?: () => void): Promise<boolean> {
    if (!text.trim()) return false
    if (this.socket?.readyState !== WebSocket.OPEN) throw new Error('Voice session is not connected.')
    const audioChunksBefore = this.receivedAudioChunks
    this.onFirstOutputAudio = onAudioStarted || null
    try {
      await new Promise<void>((resolve, reject) => {
      let settled = false
      let timeout = 0
      let unsubscribe = () => {}
      const finish = (error?: Error) => {
        if (settled) return
        settled = true
        window.clearTimeout(timeout)
        unsubscribe()
        if (error) reject(error)
        else resolve()
      }
      unsubscribe = this.onEvent(event => {
        if (event.type === 'turn_complete') finish()
        else if (event.type === 'interrupted') finish(new Error('Voice message was interrupted.'))
        else if (event.type === 'error') finish(new Error(String(event.data.message || 'Voice message failed.')))
      })
      timeout = window.setTimeout(() => finish(new Error('Voice message did not finish.')), 30000)
      // Live models only accept clientContent for seeding initial history; a
      // mid-session clientContent turn is ignored, so nothing would be spoken.
      // Realtime text input is the supported way to trigger a spoken reply.
      this.send({ realtimeInput: { text: `Speak this customer-facing message exactly as written. Do not add details or use tools: ${JSON.stringify(text)}` } })
      })
    } finally {
      this.onFirstOutputAudio = null
    }
    await this.waitForPlayback()
    return this.receivedAudioChunks > audioChunksBefore
  }

  private async waitForPlayback() {
    if (!this.audioContext) return
    while (this.playbackSources.size || this.audioContext.currentTime < this.nextPlayTime) {
      await new Promise(resolve => window.setTimeout(resolve, 50))
      if (!this.audioContext) return
    }
  }

  syncTextExchange(userText: string, assistantText: string, turnComplete = true) {
    if (this.socket?.readyState !== WebSocket.OPEN) return
    this.send({ clientContent: { turns: [
      { role: 'user', parts: [{ text: userText }] },
      { role: 'model', parts: [{ text: assistantText }] },
    ], turnComplete } })
  }

  stopPlayback() { this.clearPlayback() }

  async disconnect() {
    this.intentionalClose = true
    cancelAnimationFrame(this.animationFrame)
    this.processor?.disconnect(); this.source?.disconnect(); this.analyser?.disconnect()
    this.microphone?.getTracks().forEach(track => track.stop())
    this.microphone = null
    this.clearPlayback()
    if (this.socket && this.socket.readyState < WebSocket.CLOSING) this.socket.close(1000, 'Voice session ended')
    this.socket = null
    for (const connection of this.loopbackConnections) connection.close()
    this.loopbackConnections = []
    if (this.playbackElement) { this.playbackElement.pause(); this.playbackElement.srcObject = null; this.playbackElement = null }
    this.outputBus = null; this.outputAnalyser = null
    if (this.audioContext) await this.audioContext.close().catch(() => {})
    this.audioContext = null
  }

  private send(message: Record<string, unknown>) {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(message))
  }

  private sendHistory() {
    const turns = this.history.filter(item => item.content.trim()).slice(-20).map(item => ({
      role: item.role === 'assistant' ? 'model' : 'user', parts: [{ text: item.content }],
    }))
    // History is context only. Completing this as a user turn makes Gemini
    // answer the last historical prompt again, duplicating the spoken reply.
    if (turns.length) this.send({ clientContent: { turns, turnComplete: false } })
  }

  private emit(type: VoiceSessionEvent['type'], data: Record<string, unknown>) {
    for (const handler of this.listeners) handler({ type, data })
  }

  private handleMessage(message: Record<string, any>) {
    const content = message.serverContent || {}
    const outputTranscript = content.outputTranscription?.text
    if (outputTranscript) {
      this.modelSpeaking = true
      this.recentAssistantText = (this.recentAssistantText + String(outputTranscript)).slice(-2000)
      this.emit('output_transcript', { text: String(outputTranscript) })
    }

    const inputTranscript = content.inputTranscription?.text
    let acceptedInputText = ''
    if (inputTranscript && !this.microphonePaused) {
      const text = String(inputTranscript)
      const normalize = (value: string) => value.toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim()
      const heard = normalize(text)
      const assistant = normalize(this.recentAssistantText)
      const echoedAssistant = heard.length >= 8 && assistant.length >= 8 && (assistant.includes(heard) || heard.includes(assistant))
      const recentVoiceActivity = this.lastBargeInAudioAt > 0 && performance.now() - this.lastBargeInAudioAt < 2500
      if (!echoedAssistant && (!this.modelSpeaking || recentVoiceActivity)) acceptedInputText = text
    }

    // The Live API can mark speaker echo as an interruption before its input
    // transcript arrives. Only a recent, non-echo transcript is allowed to stop
    // local playback; the server interruption signal alone is not sufficient.
    const recentVoiceActivity = this.lastBargeInAudioAt > 0 && performance.now() - this.lastBargeInAudioAt < 2500
    if (acceptedInputText) {
      if ((this.modelSpeaking || this.playbackSources.size) && recentVoiceActivity) this.interruptModel()
      this.lastBargeInAudioAt = 0
      this.currentUserText += acceptedInputText
      this.recentAssistantText = ''
      this.emit('input_transcript', { text: this.currentUserText })
    }

    // Mic audio reaches Gemini during playback only after passing the echo
    // gate, so its server-side interruption reflects real customer speech.
    if (content.interrupted) {
      this.discardModelAudio = false
      this.interruptModel()
    }
    const parts = content.interrupted || this.discardModelAudio ? [] : (content.modelTurn?.parts || [])
    for (const part of parts) {
      const inline = part.inlineData || part.inline_data
      if (inline?.data) {
        if (!this.modelSpeaking) this.interruptEmitted = false
        this.modelSpeaking = true
        this.playAudio(String(inline.data))
      }
    }
    if (message.toolCall?.functionCalls) {
      for (const call of message.toolCall.functionCalls as LiveToolCall[]) {
        this.emit('tool_call', { id: call.id, name: call.name, arguments: call.args || {} })
        void this.invokeTool(call).catch(error => {
          const result = { ok: false, error: error instanceof Error ? error.message : 'The support tool failed.' }
          this.emit('tool_result', { id: call.id, name: call.name, arguments: call.args || {}, result })
          this.send({ toolResponse: { functionResponses: [{ id: call.id, name: call.name, response: result }] } })
        })
      }
    }
    if (content.turnComplete) {
      this.modelSpeaking = false
      this.discardModelAudio = false
      this.interruptEmitted = false
      this.emit('turn_complete', { user_text: this.currentUserText, usage: message.usageMetadata || {} })
      this.currentUserText = ''
    }
  }

  private interruptModel() {
    if (this.interruptEmitted || (!this.modelSpeaking && !this.playbackSources.size)) return
    this.interruptEmitted = true
    this.modelSpeaking = false
    this.clearPlayback()
    this.currentUserText = ''
    this.emit('interrupted', {})
  }

  /** Local barge-in: stop playback now and ignore the rest of the old reply. */
  private bargeIn() {
    if (this.modelSpeaking || this.playbackSources.size) this.discardModelAudio = true
    this.interruptModel()
  }

  private outputLevel() {
    if (!this.outputAnalyser) return 0
    const samples = new Float32Array(this.outputAnalyser.fftSize)
    this.outputAnalyser.getFloatTimeDomainData(samples)
    let energy = 0
    for (const sample of samples) energy += sample * sample
    return Math.sqrt(energy / samples.length)
  }

  private toPcm(input: Float32Array) {
    const ratio = this.audioContext!.sampleRate / 16000
    const sampleCount = Math.floor(input.length / ratio)
    const pcm = new Uint8Array(sampleCount * 2)
    const view = new DataView(pcm.buffer)
    for (let index = 0; index < sampleCount; index++) {
      const sample = input[Math.min(Math.floor(index * ratio), input.length - 1)]
      view.setInt16(index * 2, Math.max(-1, Math.min(1, sample)) * (sample < 0 ? 32768 : 32767), true)
    }
    return pcm
  }

  private sendPcm(pcm: Uint8Array) {
    let binary = ''
    for (let offset = 0; offset < pcm.length; offset += 8192) binary += String.fromCharCode(...pcm.subarray(offset, Math.min(offset + 8192, pcm.length)))
    this.send({ realtimeInput: { audio: { data: btoa(binary), mimeType: 'audio/pcm;rate=16000' } } })
  }

  private async startMicrophone() {
    if (!this.microphone || !this.audioContext) throw new Error('Microphone audio was not initialized.')
    this.source = this.audioContext.createMediaStreamSource(this.microphone)
    this.analyser = this.audioContext.createAnalyser()
    this.analyser.fftSize = 256
    this.source.connect(this.analyser)
    // 2048 samples at 16 kHz = 128 ms per frame, for responsive barge-in.
    this.processor = this.audioContext.createScriptProcessor(2048, 1, 1)
    const muted = this.audioContext.createGain()
    muted.gain.value = 0
    this.source.connect(this.processor)
    this.processor.connect(muted)
    muted.connect(this.audioContext.destination)
    this.processor.onaudioprocess = event => {
      if (this.microphonePaused || this.socket?.readyState !== WebSocket.OPEN) {
        this.speechFrames = 0; this.preRoll = []
        return
      }
      const input = event.inputBuffer.getChannelData(0)
      const pcm = this.toPcm(input)
      const playbackActive = this.modelSpeaking || this.playbackSources.size > 0 || this.audioContext!.currentTime < this.nextPlayTime
      if (!playbackActive) {
        this.speechFrames = 0; this.preRoll = []
        this.sendPcm(pcm)
        return
      }
      // Double-talk detection. Browser AEC (fed by the loopback playback route)
      // removes most speaker echo; the residual is estimated as a learned
      // fraction of the current output level. Only input well above that
      // residual, sustained for two frames, counts as the customer talking.
      let energy = 0
      for (const sample of input) energy += sample * sample
      const micRms = Math.sqrt(energy / input.length)
      const currentOutput = this.outputLevel()
      // Speaker-to-mic delay: the echo in this frame may come from the previous one.
      const outputRms = Math.max(currentOutput, this.previousOutputRms)
      this.previousOutputRms = currentOutput
      const expectedEcho = this.echoCoupling * outputRms
      const isSpeech = micRms > Math.max(0.02, expectedEcho * 3)
      if (!isSpeech && outputRms > 0.01) {
        // Learn echo coupling only from echo-only frames: adapt down quickly, up slowly.
        const ratio = micRms / outputRms
        const rate = ratio < this.echoCoupling ? 0.3 : 0.05
        this.echoCoupling = Math.min(2, Math.max(0.01, this.echoCoupling + (ratio - this.echoCoupling) * rate))
      }
      this.preRoll.push(pcm)
      if (this.preRoll.length > 3) this.preRoll.shift()
      if (!isSpeech) { this.speechFrames = 0; return }
      this.speechFrames += 1
      if (this.speechFrames < 2) return
      // Customer is talking over the agent: stop playback and forward the
      // buffered start of the utterance so Gemini hears it from the beginning.
      this.lastBargeInAudioAt = performance.now()
      this.bargeIn()
      for (const frame of this.preRoll) this.sendPcm(frame)
      this.preRoll = []
      this.speechFrames = 0
    }
    const samples = new Uint8Array(this.analyser.fftSize)
    const draw = () => {
      if (!this.analyser) return
      this.analyser.getByteTimeDomainData(samples)
      let energy = 0
      for (const sample of samples) energy += ((sample - 128) / 128) ** 2
      this.emit('level', { value: Math.min(1, Math.sqrt(energy / samples.length) * 4) })
      this.animationFrame = requestAnimationFrame(draw)
    }
    draw()
  }

  private async prepareAudio() {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('This browser cannot access a microphone. Use HTTPS or localhost and allow microphone access.')
    const streamRequest = navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: false } })
    this.audioContext = new AudioContext({ sampleRate: 16000 })
    await this.audioContext.resume()
    this.microphone = await streamRequest
    await this.setupPlaybackRoute()
  }

  /**
   * Chrome's echo canceller reliably uses WebRTC-received audio as its echo
   * reference, but not always Web Audio output. Sending playback through a
   * local RTCPeerConnection loopback into an <audio> element lets the
   * microphone's echoCancellation subtract the agent's voice. Falls back to
   * direct output when the loopback is unavailable.
   */
  private async setupPlaybackRoute() {
    const context = this.audioContext!
    this.outputBus = context.createGain()
    this.outputAnalyser = context.createAnalyser()
    this.outputAnalyser.fftSize = 1024
    this.outputBus.connect(this.outputAnalyser)
    try {
      if (typeof RTCPeerConnection === 'undefined') throw new Error('WebRTC unavailable')
      const destination = context.createMediaStreamDestination()
      this.outputAnalyser.connect(destination)
      const sender = new RTCPeerConnection()
      const receiver = new RTCPeerConnection()
      this.loopbackConnections = [sender, receiver]
      sender.onicecandidate = event => { if (event.candidate) void receiver.addIceCandidate(event.candidate).catch(() => {}) }
      receiver.onicecandidate = event => { if (event.candidate) void sender.addIceCandidate(event.candidate).catch(() => {}) }
      const remoteStream = new Promise<MediaStream>(resolve => {
        receiver.ontrack = event => resolve(event.streams[0] || new MediaStream([event.track]))
      })
      for (const track of destination.stream.getAudioTracks()) sender.addTrack(track, destination.stream)
      const offer = await sender.createOffer()
      await sender.setLocalDescription(offer)
      await receiver.setRemoteDescription(offer)
      const answer = await receiver.createAnswer()
      await receiver.setLocalDescription(answer)
      await sender.setRemoteDescription(answer)
      const stream = await Promise.race([
        remoteStream,
        new Promise<never>((_, reject) => window.setTimeout(() => reject(new Error('Loopback timed out')), 3000)),
      ])
      const element = new Audio()
      element.autoplay = true
      element.srcObject = stream
      await element.play()
      this.playbackElement = element
    } catch {
      for (const connection of this.loopbackConnections) connection.close()
      this.loopbackConnections = []
      this.outputAnalyser.disconnect()
      this.outputAnalyser.connect(context.destination)
    }
  }

  private playAudio(encoded: string) {
    if (!this.audioContext) return
    this.receivedAudioChunks += 1
    const onAudioStarted = this.onFirstOutputAudio
    this.onFirstOutputAudio = null
    onAudioStarted?.()
    const binary = atob(encoded)
    const samples = new Int16Array(Math.floor(binary.length / 2))
    for (let index = 0; index < samples.length; index++) {
      const low = binary.charCodeAt(index * 2)
      const high = binary.charCodeAt(index * 2 + 1)
      const value = low | (high << 8)
      samples[index] = value > 32767 ? value - 65536 : value
    }
    const buffer = this.audioContext.createBuffer(1, samples.length, 24000)
    const channel = buffer.getChannelData(0)
    for (let index = 0; index < samples.length; index++) channel[index] = samples[index] / 32768
    const source = this.audioContext.createBufferSource()
    source.buffer = buffer
    source.connect(this.outputBus || this.audioContext.destination)
    source.onended = () => this.playbackSources.delete(source)
    this.playbackSources.add(source)
    this.nextPlayTime = Math.max(this.nextPlayTime, this.audioContext.currentTime + 0.03)
    source.start(this.nextPlayTime)
    this.nextPlayTime += buffer.duration
  }

  private clearPlayback() {
    for (const source of this.playbackSources) {
      try { source.stop() } catch { /* already ended */ }
    }
    this.playbackSources.clear()
    this.nextPlayTime = this.audioContext?.currentTime || 0
  }
}
