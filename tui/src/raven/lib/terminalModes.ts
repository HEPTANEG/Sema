




import { writeSync } from 'node:fs'

export const TERMINAL_MODE_RESET =
  "\x1b[0'z" +
  "\x1b[0'{" +
  '\x1b[?2029l' +
  '\x1b[?1016l' +
  '\x1b[?1015l' +
  '\x1b[?1006l' +
  '\x1b[?1005l' +
  '\x1b[?1003l' +
  '\x1b[?1002l' +
  '\x1b[?1001l' +
  '\x1b[?1000l' +
  '\x1b[?9l' +
  '\x1b[?1004l' +
  '\x1b[?2004l' +
  '\x1b[?1049l' +
  '\x1b[<u' +
  '\x1b[>4m' +
  '\x1b[0m' +
  '\x1b]112\x07' +
  '\x1b[?25h'

type ResettableStream = Pick<NodeJS.WriteStream, 'isTTY' | 'write'> & {
  fd?: number
}

export function resetTerminalModes(stream: ResettableStream = process.stdout): boolean {
  if (!stream.isTTY) {
    return false
  }

  const fd = typeof stream.fd === 'number' ? stream.fd : stream === process.stdout ? 1 : undefined

  if (fd !== undefined) {
    try {
      writeSync(fd, TERMINAL_MODE_RESET)

      return true
    } catch {
      // Fall through to stream.write for mocked or unusual TTY streams.
    }
  }

  try {
    stream.write(TERMINAL_MODE_RESET)

    return true
  } catch {
    return false
  }
}
