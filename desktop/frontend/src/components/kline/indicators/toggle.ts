export function makeToggle(refVal, syncFn) {
  return () => {
    refVal.value = !refVal.value
    syncFn()
  }
}
