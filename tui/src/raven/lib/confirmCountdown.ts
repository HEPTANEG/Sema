




export const CONFIRM_COUNTDOWN_SECONDS = 30

export interface CountdownTick {
  autoCancel: boolean
  remaining: number
}






export const tickCountdown = (remaining: number): CountdownTick => {
  const next = Math.max(0, remaining - 1)

  return { autoCancel: next <= 0, remaining: next }
}

export const buildConfirmRespond = (requestId: string, answer: boolean) => ({
  answer,
  request_id: requestId
})
