


export function lruEvict<K, V>(cache: Map<K, V>, keepRatio: number): void {
  if (keepRatio <= 0) {
    return cache.clear()
  }

  const target = Math.floor(cache.size * keepRatio)

  while (cache.size > target) {
    cache.delete(cache.keys().next().value!)
  }
}
