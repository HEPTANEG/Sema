


export const APPROVAL_OPTIONS = [
  { choice: 'allow', label: 'Allow once' },
  { choice: 'deny', label: 'Deny' }
] as const

export const buildApprovalRespond = (approvalId: string, sessionId: string, choice: string) => {


  return {
    approval_id: approvalId,
    choice,
    session_id: sessionId
  }
}



export const approvalResponseAccepted = (response: null | { ok?: boolean }) => response?.ok === true



export const approvalRemainingSeconds = (expiresAt: number, now = Date.now()) =>
  Math.max(0, Math.ceil((expiresAt - now) / 1000))
