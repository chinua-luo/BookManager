// Validate with the same naming rules used for persistence, keeping the form open.
export async function submitNameForm({ read, validate, controls, errors, accept }) {
  for (const [key, input] of Object.entries(controls)) {
    input.removeAttribute('aria-invalid')
    errors[key].textContent = ''
  }
  const values = read()
  const result = await validate(values.nameParts)
  // A user edit or background metadata response must be validated again.
  if (JSON.stringify(values) !== JSON.stringify(read())) return false
  if (!result.valid) {
    const key = Object.hasOwn(controls, result.field) ? result.field : 'mainTitle'
    errors[key].textContent = result.message
    controls[key].setAttribute('aria-invalid', 'true')
    controls[key].focus()
    return false
  }
  accept(values)
  return true
}
