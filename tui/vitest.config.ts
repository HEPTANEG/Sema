import {defineConfig} from 'vitest/config'

export default defineConfig({
  test: {
    exclude: [
      'node_modules/**',
      'dist/**',
      'src/raven/**/*.test.*',
      'src/raven/**/__tests__/**',
      'packages/**'
    ]
  }
})
