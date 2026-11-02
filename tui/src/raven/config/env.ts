const truthy = (v?: string) => /^(?:1|true|yes|on)$/i.test((v ?? '').trim())

export const STARTUP_RESUME_ID = (process.env.RAVEN_TUI_RESUME ?? '').trim()
export const STARTUP_QUERY = (process.env.RAVEN_TUI_QUERY ?? '').trim()
export const STARTUP_IMAGE = (process.env.RAVEN_TUI_IMAGE ?? '').trim()
export const MOUSE_TRACKING = !truthy(process.env.RAVEN_TUI_DISABLE_MOUSE)
export const NO_CONFIRM_DESTRUCTIVE = truthy(process.env.RAVEN_TUI_NO_CONFIRM)




export const INLINE_MODE = truthy(process.env.SEMA_TUI_INLINE) || truthy(process.env.RAVEN_TUI_INLINE)



export const SHOW_FPS = truthy(process.env.RAVEN_TUI_FPS)
