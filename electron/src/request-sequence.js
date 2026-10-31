// Monotonic request tokens so late responses from superseded loads are ignored.
export function createRequestSequence() {
  let current = 0
  return {
    begin() { current += 1; return current },
    isCurrent(token) { return token === current },
    invalidate() { current += 1 }
  }
}
