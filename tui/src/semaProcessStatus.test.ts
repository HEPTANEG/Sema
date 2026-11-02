import {describe, expect, it} from 'vitest'

import {processStatusDisplay} from './raven/components/appLayout.js'

const colors = {
  primary: 'ansi:cyanBright',
  text: 'ansi:white'
}

describe('Sema process status display', () => {
  it('uses the normal text color for the idle placeholder', () => {
    expect(processStatusDisplay('', colors)).toEqual({
      line: 'Nothing is happening',
      color: colors.text
    })
  })

  it('keeps an explicit idle placeholder in the normal text color', () => {
    expect(processStatusDisplay('Nothing is happening', colors)).toEqual({
      line: 'Nothing is happening',
      color: colors.text
    })
  })

  it('uses the brand accent for real activity text', () => {
    expect(processStatusDisplay('running... | Focus Detector is working', colors)).toEqual({
      line: 'running... | Focus Detector is working',
      color: colors.primary
    })
  })
})
