



import { describe, expect, it } from 'vitest'

import { formatLtmMaintenanceStageStatus } from '../components/appLayout.js'

describe('formatLtmMaintenanceStageStatus', () => {
  it('formats the active stage for the bottom status bar', () => {
    expect(formatLtmMaintenanceStageStatus({
      elapsed: '8m 05s',
      runId: 'run-1',
      stage: 'global_duplicate_review',
      stageIndex: 1,
      status: 'running'
    })).toBe('maintaining LTM · stage 2/5: global duplicate review · 8m 05s · run-1')
  })

  it('shows preparing before the first stage event', () => {
    expect(formatLtmMaintenanceStageStatus({ status: 'running' })).toBe('maintaining LTM · preparing')
  })
})
