





import { useEffect, useLayoutEffect } from 'react'
import { useEventCallback } from 'usehooks-ts'

import type { InputEvent, Key } from '../events/input-event.js'

import useStdin from './use-stdin.js'

type Handler = (input: string, key: Key, event: InputEvent) => void

type Options = {






  isActive?: boolean
}

























const useInput = (inputHandler: Handler, options: Options = {}) => {
  const { setRawMode, exitOnCtrlC, inputEmitter } = useStdin()






  useLayoutEffect(() => {
    if (options.isActive === false) {
      return
    }

    setRawMode(true)

    return () => {
      setRawMode(false)
    }
  }, [options.isActive, setRawMode])








  const handleData = useEventCallback((event: InputEvent) => {
    if (options.isActive === false) {
      return
    }

    const { input, key } = event




    if (!(input === 'c' && key.ctrl) || !exitOnCtrlC) {
      inputHandler(input, key, event)
    }
  })

  useEffect(() => {
    inputEmitter?.on('input', handleData)

    return () => {
      inputEmitter?.removeListener('input', handleData)
    }
  }, [inputEmitter, handleData])
}

export default useInput
