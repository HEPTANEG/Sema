






import type { JsonRpcErrorObject } from './generated.js'


export class RpcError extends Error {
  readonly code: number
  readonly data: unknown

  constructor(frame: JsonRpcErrorObject) {
    super(`[rpc ${frame.code}] ${frame.message}`)
    this.name = 'RpcError'
    this.code = frame.code
    this.data = frame.data
  }
}



export class SessionNotFoundError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'SessionNotFoundError'
  }
}
export class SessionLockedError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'SessionLockedError'
  }
}
export class TurnInProgressError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'TurnInProgressError'
  }
}
export class ModelNotAvailableError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'ModelNotAvailableError'
  }
}
export class ConfigFieldReadonlyError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'ConfigFieldReadonlyError'
  }
}
export class ConfigValidationError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'ConfigValidationError'
  }
}
export class NotSupportedInV01Error extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'NotSupportedInV01Error'
  }
}
export class CliCommandFailedError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'CliCommandFailedError'
  }
}
export class CliCommandTimeoutError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'CliCommandTimeoutError'
  }
}
export class NotDispatchCompatibleError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'NotDispatchCompatibleError'
  }
}



const CODE_TO_CTOR: Record<number, new (f: JsonRpcErrorObject) => RpcError> = {
  [-32001]: SessionNotFoundError,
  [-32002]: SessionLockedError,
  [-32003]: TurnInProgressError,
  [-32008]: ModelNotAvailableError,
  [-32010]: ConfigFieldReadonlyError,
  [-32011]: ConfigValidationError,
  [-32012]: NotSupportedInV01Error,
  [-32013]: CliCommandFailedError,
  [-32014]: CliCommandTimeoutError,
  [-32015]: NotDispatchCompatibleError
}


export function rpcErrorFromFrame(frame: JsonRpcErrorObject): RpcError {
  const Ctor = CODE_TO_CTOR[frame.code]
  return Ctor ? new Ctor(frame) : new RpcError(frame)
}
