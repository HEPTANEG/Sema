




import type {
  BackgroundStartResponse,
  ConfigGetValueResponse,
  ConfigSetResponse,
  ImageAttachResponse,
  SessionBranchResponse,
  SessionCompressResponse,
  SessionExportResponse,
  SessionUsageResponse,
  VoiceToggleResponse
} from '../../../gatewayTypes.js'
import type { PanelSection } from '../../../types.js'
import type { SlashCommand } from '../types.js'

import { attachedImageNotice, introMsg, toTranscriptMessages } from '../../../domain/messages.js'
import { formatVoiceRecordKey, parseVoiceRecordKey } from '../../../lib/platform.js'
import { fmtK } from '../../../lib/text.js'
import { DEFAULT_INDICATOR_STYLE, INDICATOR_STYLES, type IndicatorStyle } from '../../interfaces.js'
import { patchOverlayState } from '../../overlayStore.js'
import { patchUiState } from '../../uiStore.js'












const parseModelArg = (arg: string): { provider?: string; value: string } => {
  const flagged = arg.trim().match(/^(.*?)\s+--provider\s+(\S+)\s*$/)

  if (flagged) {
    return { provider: flagged[2], value: flagged[1]!.trim() }
  }

  const words = arg.trim().split(/\s+/).filter(Boolean)

  if (words.length === 2) {
    return { provider: words[0], value: words[1]! }
  }

  return { value: arg.trim() }
}



const normalizeSessionId = (id: string) => (id.includes(':') ? id : `tui:${id}`)

export const sessionCommands: SlashCommand[] = [
  {
    aliases: ['bg', 'btw'],
    help: 'launch a background prompt',
    name: 'background',
    supported: false,
    run: (arg, ctx) => {
      if (!arg) {
        return ctx.transcript.sys('/background <prompt>')
      }

      ctx.gateway
        .rpc<BackgroundStartResponse>('prompt.background', { session_id: ctx.sid, text: arg })
        .then(
          ctx.guarded<BackgroundStartResponse>(r => {
            if (!r.task_id) {
              return
            }

            patchUiState(state => ({ ...state, bgTasks: new Set(state.bgTasks).add(r.task_id!) }))
            ctx.transcript.sys(`bg ${r.task_id} started`)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'change model: <provider> <id>, or nothing to pick; --default sets new sessions',
    name: 'model',
    run: (arg, ctx) => {






      const raw = arg.trim()
      const asDefault = /(^|\s)--default(\s|$)/.test(raw)
      const rest = raw.replace(/(^|\s)--default(?=\s|$)/g, '').trim()
      if (!rest) {




        return patchOverlayState({ modelPicker: asDefault ? 'default' : true })
      }

      const { provider, value } = parseModelArg(rest)

      if (!provider) {





        return ctx.transcript.sys(
          `/model needs a provider. Run /model${asDefault ? ' --default' : ''} to pick one, ` +
            `or: /model <provider> ${value}${asDefault ? ' --default' : ''}`
        )
      }

      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', {
          key: 'model',
          session_id: ctx.sid,
          scope: asDefault ? 'default' : 'session',
          value,
          provider
        })
        .then(
          ctx.guarded<ConfigSetResponse>(r => {
            if (!r.value) {
              return ctx.transcript.sys('error: invalid response: model switch')
            }
            if (!r.applied) {


              return ctx.transcript.sys(`error: model switch was not applied: ${r.value}`)
            }

            ctx.transcript.sys(asDefault ? `default model → ${r.value}` : `model → ${r.value}`)
            ctx.local.maybeWarn(r)





            if (r.applies_to_session !== false) {
              patchUiState(state => ({
                ...state,
                info: state.info ? { ...state.info, model: r.value! } : { model: r.value!, tools: {} }
              }))
            }
          })
        )
        // Without this the rejection is unhandled, and setupGracefulExit writes
        // it raw to stderr over the Ink render: `/model` typed before the first
        // session.create resolves is refused by the server (session scope with
        // no session), and the transcript would show nothing at all.
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'browse/manage sessions: list, new [title], resume [id], delete [id|current]',
    name: 'sessions',
    run: (arg, ctx) => {
      const parts = arg.trim().split(/\s+/)
      const sub = parts[0]?.toLowerCase() ?? ''
      const rest = parts.slice(1).join(' ').trim()

      if (!sub || sub === 'list') {
        if (ctx.session.guardBusySessionSwitch('switch sessions')) {
          return
        }

        return patchOverlayState({ picker: true })
      }

      if (sub === 'new') {
        if (ctx.session.guardBusySessionSwitch('switch sessions')) {
          return
        }

        ctx.session.newSession('New session started', rest || undefined)

        return
      }

      if (sub === 'resume') {
        if (ctx.session.guardBusySessionSwitch('switch sessions')) {
          return
        }

        if (rest) {
          ctx.session.resumeById(rest)
        } else {
          patchOverlayState({ picker: true })
        }

        return
      }

      if (sub === 'delete') {
        const targetId = rest === 'current' || !rest ? ctx.sid : normalizeSessionId(rest)

        if (!targetId) {
          return ctx.transcript.sys('no active session to delete')
        }



        const isActive = targetId === ctx.sid

        if (isActive && ctx.session.guardBusySessionSwitch('delete the active session')) {
          return
        }

        void ctx.session
          .deleteSessionWithFallback(targetId)
          .then(removed => {


            if (!isActive) {
              ctx.transcript.sys(removed ? `deleted session: ${targetId}` : `no such session: ${targetId}`)
            }
          })
          .catch((e: unknown) => {
            const msg = e instanceof Error ? e.message : String(e)
            ctx.transcript.sys(`error: ${msg}`)
          })

        return
      }

      ctx.transcript.sys('usage: /sessions [list|new [title]|resume [id]|delete [id|current]]')
    }
  },

  {
    help: 'attach an image',
    name: 'image',
    supported: false,
    run: (arg, ctx) => {
      ctx.gateway
        .rpc<ImageAttachResponse>('image.attach', { path: arg, session_id: ctx.sid })
        .then(
          ctx.guarded<ImageAttachResponse>(r => {
            ctx.transcript.sys(attachedImageNotice(r))

            if (r.remainder) {
              ctx.composer.setInput(r.remainder)
            }
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'switch personality for this session',
    name: 'personality',
    supported: false,
    run: (arg, ctx) => {
      if (!arg) {
        return
      }

      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'personality', session_id: ctx.sid, value: arg })
        .then(
          ctx.guarded<ConfigSetResponse>(r => {
            if (r.history_reset) {
              ctx.session.resetVisibleHistory(r.info ?? null)
            }

            ctx.transcript.sys(`personality: ${r.value || 'default'}${r.history_reset ? ' · transcript cleared' : ''}`)
            ctx.local.maybeWarn(r)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'compress transcript',
    name: 'compress',
    supported: false,
    run: (arg, ctx) => {
      ctx.gateway
        .rpc<SessionCompressResponse>('session.compress', {
          session_id: ctx.sid,
          ...(arg ? { focus_topic: arg } : {})
        })
        .then(
          ctx.guarded<SessionCompressResponse>(r => {
            if (Array.isArray(r.messages)) {
              const rows = toTranscriptMessages(r.messages)

              ctx.transcript.setHistoryItems(r.info ? [introMsg(r.info), ...rows] : rows)
            }

            if (r.info) {
              patchUiState({ info: r.info })
            }

            if (r.usage) {
              patchUiState(state => ({ ...state, usage: { ...state.usage, ...r.usage } }))
            }

            if (r.summary?.headline) {
              const prefix = r.summary.noop ? '' : '✓ '

              ctx.transcript.sys(`${prefix}${r.summary.headline}`)

              if (r.summary.token_line) {
                ctx.transcript.sys(`  ${r.summary.token_line}`)
              }

              if (r.summary.note) {
                ctx.transcript.sys(`  ${r.summary.note}`)
              }

              return
            }

            if ((r.removed ?? 0) <= 0) {
              return ctx.transcript.sys('nothing to compress')
            }

            ctx.transcript.sys(
              `compressed ${r.removed} messages${r.usage?.total ? ` · ${fmtK(r.usage.total)} tok` : ''}`
            )
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    aliases: ['fork'],
    help: 'branch the session',
    name: 'branch',
    run: (arg, ctx) => {
      const prevSid = ctx.sid

      ctx.gateway
        .rpc<SessionBranchResponse>('session.branch', { name: arg, session_id: ctx.sid })
        .then(
          ctx.guarded<SessionBranchResponse>(r => {
            if (!r.session_id) {
              return
            }

            void ctx.session.closeSession(prevSid)
            patchUiState({ sid: r.session_id })
            ctx.session.setSessionStartedAt(Date.now())


            const bareSessionId = (k: string) => (k.includes(':') ? k.slice(k.indexOf(':') + 1) : k)
            const messageCount = r.message_count ?? 0
            const title = r.title || '(untitled)'
            ctx.transcript.sys(`⑂ Forked "${title}" · ${messageCount} message${messageCount === 1 ? '' : 's'} carried`)
            ctx.transcript.sys(`   parent  ${prevSid ? bareSessionId(prevSid) : '(none)'}`)
            ctx.transcript.sys(`   forked  ${bareSessionId(r.session_id)}`)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'export the session transcript to a markdown file',
    name: 'export',
    usage: '/export [id]',
    run: (arg, ctx) => {


      const target = arg.trim() || ctx.sid

      if (!target) {
        return ctx.transcript.sys('no active session to export')
      }

      ctx.gateway
        .rpc<SessionExportResponse>('session.export', { session_id: target })
        .then(
          ctx.guarded<SessionExportResponse>(r => {
            if (r.exported && r.path) {
              return ctx.transcript.sys(`✓ exported to ${r.path}`)
            }

            if (r.reason === 'ambiguous') {
              return ctx.transcript.sys(`ambiguous session id — candidates: ${(r.candidates ?? []).join(', ')}`)
            }

            if (r.reason === 'write_failed') {
              return ctx.transcript.sys('error: failed to write export file')
            }

            return ctx.transcript.sys(`no such session: ${arg.trim() || target}`)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'voice mode: [on|off|tts|status]',
    name: 'voice',
    supported: false,
    run: (arg, ctx) => {
      const normalized = (arg ?? '').trim().toLowerCase()

      const action =
        normalized === 'on' || normalized === 'off' || normalized === 'tts' || normalized === 'status'
          ? normalized
          : 'status'

      ctx.gateway
        .rpc<VoiceToggleResponse>('voice.toggle', { action })
        .then(
          ctx.guarded<VoiceToggleResponse>(r => {
            ctx.voice.setVoiceEnabled(!!r.enabled)

















            const parsed = r.record_key ? parseVoiceRecordKey(r.record_key) : undefined

            if (parsed) {
              ctx.voice.setVoiceRecordKey(parsed)
            }

            const recordKeyLabel = formatVoiceRecordKey(parsed ?? parseVoiceRecordKey('ctrl+b'))




            if (action === 'status') {
              const mode = r.enabled ? 'ON' : 'OFF'
              const tts = r.tts ? 'ON' : 'OFF'
              ctx.transcript.sys('Voice Mode Status')
              ctx.transcript.sys(`  Mode:       ${mode}`)
              ctx.transcript.sys(`  TTS:        ${tts}`)
              ctx.transcript.sys(`  Record key: ${recordKeyLabel}`)




              if (r.details) {
                ctx.transcript.sys('')
                ctx.transcript.sys('  Requirements:')

                for (const line of r.details.split('\n')) {
                  if (line.trim()) {
                    ctx.transcript.sys(`    ${line}`)
                  }
                }
              }

              return
            }

            if (action === 'tts') {
              ctx.transcript.sys(`Voice TTS ${r.tts ? 'enabled' : 'disabled'}.`)

              return
            }


            if (r.enabled) {
              const tts = r.tts ? ' (TTS enabled)' : ''
              ctx.transcript.sys(`Voice mode enabled${tts}`)
              ctx.transcript.sys(`  ${recordKeyLabel} to start/stop recording`)
              ctx.transcript.sys('  /voice tts  to toggle speech output')
              ctx.transcript.sys('  /voice off  to disable voice mode')
            } else {
              ctx.transcript.sys('Voice mode disabled.')
            }
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'switch theme skin (fires skin.changed)',
    name: 'skin',
    supported: false,
    run: (arg, ctx) => {
      if (!arg) {
        return ctx.gateway
          .rpc<ConfigGetValueResponse>('config.get', { key: 'skin' })
          .then(ctx.guarded<ConfigGetValueResponse>(r => ctx.transcript.sys(`skin: ${r.value || 'default'}`)))
          .catch(ctx.guardedErr)
      }

      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'skin', value: arg })
        .then(ctx.guarded<ConfigSetResponse>(r => r.value && ctx.transcript.sys(`skin → ${r.value}`)))
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'pick the busy indicator: kaomoji (default), emoji, unicode (braille), or ascii',
    name: 'indicator',
    supported: false,
    usage: `/indicator [${INDICATOR_STYLES.join('|')}]`,
    run: (arg, ctx) => {
      const value = arg.trim().toLowerCase()

      if (!value) {
        return ctx.gateway
          .rpc<ConfigGetValueResponse>('config.get', { key: 'indicator' })
          .then(
            ctx.guarded<ConfigGetValueResponse>(r =>
              ctx.transcript.sys(`indicator: ${r.value || DEFAULT_INDICATOR_STYLE}`)
            )
          )
          .catch(ctx.guardedErr)
      }

      if (!(INDICATOR_STYLES as readonly string[]).includes(value)) {
        return ctx.transcript.sys(`usage: /indicator [${INDICATOR_STYLES.join('|')}]`)
      }

      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'indicator', value })
        .then(
          ctx.guarded<ConfigSetResponse>(r => {
            if (!r.value) {
              return
            }




            patchUiState({ indicatorStyle: value as IndicatorStyle })
            ctx.transcript.sys(`indicator → ${r.value}`)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'toggle yolo mode (per-session approvals)',
    name: 'yolo',
    supported: false,
    run: (_arg, ctx) => {
      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'yolo', session_id: ctx.sid })
        .then(ctx.guarded<ConfigSetResponse>(r => ctx.transcript.sys(`yolo ${r.value === '1' ? 'on' : 'off'}`)))
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'inspect or set reasoning effort (updates live agent)',
    name: 'reasoning',
    supported: false,
    run: (arg, ctx) => {
      if (!arg) {
        return ctx.gateway
          .rpc<ConfigGetValueResponse>('config.get', { key: 'reasoning' })
          .then(
            ctx.guarded<ConfigGetValueResponse>(
              r => r.value && ctx.transcript.sys(`reasoning: ${r.value} · display ${r.display || 'hide'}`)
            )
          )
          .catch(ctx.guardedErr)
      }

      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'reasoning', session_id: ctx.sid, value: arg })
        .then(
          ctx.guarded<ConfigSetResponse>(r => {
            if (!r.value) {
              return
            }

            if (r.value === 'hide') {
              patchUiState(state => ({
                ...state,
                sections: { ...state.sections, thinking: 'hidden' },
                showReasoning: false
              }))
            } else if (r.value === 'show') {
              patchUiState(state => ({
                ...state,
                sections: { ...state.sections, thinking: 'expanded' },
                showReasoning: true
              }))
            }

            ctx.transcript.sys(`reasoning: ${r.value}`)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'toggle fast mode [normal|fast|status|on|off|toggle]',
    name: 'fast',
    supported: false,
    run: (arg, ctx) => {
      const mode = arg.trim().toLowerCase()
      const valid = new Set(['', 'status', 'normal', 'fast', 'on', 'off', 'toggle'])

      if (!valid.has(mode)) {
        return ctx.transcript.sys('usage: /fast [normal|fast|status|on|off|toggle]')
      }

      if (!mode || mode === 'status') {
        return ctx.gateway
          .rpc<ConfigGetValueResponse>('config.get', { key: 'fast', session_id: ctx.sid })
          .then(
            ctx.guarded<ConfigGetValueResponse>(r =>
              ctx.transcript.sys(`fast mode: ${r.value === 'fast' ? 'fast' : 'normal'}`)
            )
          )
          .catch(ctx.guardedErr)
      }

      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'fast', session_id: ctx.sid, value: mode })
        .then(
          ctx.guarded<ConfigSetResponse>(r => {
            const next = r.value === 'fast' ? 'fast' : 'normal'
            ctx.transcript.sys(`fast mode: ${next}`)
            patchUiState(state => ({
              ...state,
              info: state.info
                ? {
                    ...state.info,
                    fast: next === 'fast',
                    service_tier: next === 'fast' ? 'priority' : ''
                  }
                : state.info
            }))
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'control busy enter mode [queue|steer|interrupt|status]',
    name: 'busy',
    supported: false,
    run: (arg, ctx) => {
      const mode = arg.trim().toLowerCase()
      const valid = new Set(['', 'status', 'queue', 'steer', 'interrupt'])

      if (!valid.has(mode)) {
        return ctx.transcript.sys('usage: /busy [queue|steer|interrupt|status]')
      }

      if (!mode || mode === 'status') {
        return ctx.gateway
          .rpc<ConfigGetValueResponse>('config.get', { key: 'busy' })
          .then(
            ctx.guarded<ConfigGetValueResponse>(r => {
              const current = r.value || 'interrupt'
              ctx.transcript.sys(`busy input mode: ${current}`)
            })
          )
          .catch(ctx.guardedErr)
      }

      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'busy', value: mode })
        .then(
          ctx.guarded<ConfigSetResponse>(r => {
            const next = r.value || mode
            ctx.transcript.sys(`busy input mode: ${next}`)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'cycle verbose tool-output mode (updates live agent)',
    name: 'verbose',
    supported: false,
    run: (arg, ctx) => {
      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'verbose', session_id: ctx.sid, value: arg || 'cycle' })
        .then(ctx.guarded<ConfigSetResponse>(r => r.value && ctx.transcript.sys(`verbose: ${r.value}`)))
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'session usage (live counts — worker sees zeros)',
    name: 'usage',
    supported: false,
    run: (_arg, ctx) => {
      ctx.gateway
        .rpc<SessionUsageResponse>('session.usage', { session_id: ctx.sid })
        .then(r => {
          if (ctx.stale()) {
            return
          }

          if (r) {
            patchUiState({
              usage: { calls: r.calls ?? 0, input: r.input ?? 0, output: r.output ?? 0, total: r.total ?? 0 }
            })
          }

          if (!r?.calls) {
            return ctx.transcript.sys('no API calls yet')
          }

          const f = (v: number | undefined) => (v ?? 0).toLocaleString()
          const cost =
            r.cost_usd != null ? `${r.cost_status === 'estimated' ? '~' : ''}$${r.cost_usd.toFixed(4)}` : null

          const rows: [string, string][] = [
            ['Model', r.model ?? ''],
            ['Input tokens', f(r.input)],
            ['Cache read tokens', f(r.cache_read)],
            ['Cache write tokens', f(r.cache_write)],
            ['Output tokens', f(r.output)],
            ['Total tokens', f(r.total)],
            ['API calls', f(r.calls)]
          ]

          if (cost) {
            rows.push(['Cost', cost])
          }

          const sections: PanelSection[] = [{ rows }]

          if (r.context_max) {
            sections.push({ text: `Context: ${f(r.context_used)} / ${f(r.context_max)} (${r.context_percent}%)` })
          }

          if (r.compressions) {
            sections.push({ text: `Compressions: ${r.compressions}` })
          }

          ctx.transcript.panel('Usage', sections)
        })
        .catch(ctx.guardedErr)
    }
  }
]
