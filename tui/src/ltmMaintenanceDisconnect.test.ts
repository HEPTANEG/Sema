import {beforeEach, describe, expect, it} from 'vitest'

import {failLtmMaintenanceFromBackendDisconnect} from './raven/app/ltmMaintenanceEvents.js'
import {getLtmMaintenanceState, openLtmMaintenanceView, resetLtmMaintenanceState} from './raven/app/ltmMaintenanceStore.js'

describe('maintenance backend disconnect', () => {
  beforeEach(() => resetLtmMaintenanceState())

  it('marks an active maintenance view interrupted', () => {
    openLtmMaintenanceView('/memory ltm maintain')
    failLtmMaintenanceFromBackendDisconnect()
    expect(getLtmMaintenanceState().status).toBe('failed')
    expect(getLtmMaintenanceState().terminal).toBe(true)
    expect(getLtmMaintenanceState().summary).toContain('Backend connection was lost')
  })

  it('does not create a maintenance failure when no maintenance is active', () => {
    failLtmMaintenanceFromBackendDisconnect()
    expect(getLtmMaintenanceState().status).toBe('idle')
  })
})
