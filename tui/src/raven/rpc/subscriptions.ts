









import type { EventNotificationParams } from './generated.js'

type AnyHandler = (event: unknown) => void

export class SubscriptionRegistry {
  private readonly handlers = new Map<string, AnyHandler>()


  register<E>(subscriptionId: string, handler: (event: E) => void): void {
    this.handlers.set(subscriptionId, handler as AnyHandler)
  }


  unregister(subscriptionId: string): boolean {
    return this.handlers.delete(subscriptionId)
  }


  has(subscriptionId: string): boolean {
    return this.handlers.has(subscriptionId)
  }


  size(): number {
    return this.handlers.size
  }






  dispatch(params: EventNotificationParams<unknown>): boolean {
    const handler = this.handlers.get(params.subscription_id)
    if (!handler) {
      process.stderr.write(`[rpc-subscriptions] event for unknown subscription_id=${params.subscription_id} dropped\n`)
      return false
    }
    try {
      handler(params.event)
    } catch (err) {

      process.stderr.write(`[rpc-subscriptions] handler for ${params.subscription_id} threw: ${String(err)}\n`)
    }
    return true
  }


  clear(): void {
    this.handlers.clear()
  }
}
