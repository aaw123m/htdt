import { useState } from 'react'

// Identifier chip that copies the full value to the clipboard on click —
// cards only render a truncated prefix, so `display` carries the visible text.
export function CopyCode({ value, display }: { value: string; display?: string }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value)
      } else {
        const field = document.createElement('textarea')
        field.value = value
        field.style.position = 'fixed'
        field.style.opacity = '0'
        document.body.appendChild(field)
        field.select()
        document.execCommand('copy')
        field.remove()
      }
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch { /* clipboard denied — leave the chip unchanged */ }
  }

  return <button type="button" className={copied ? 'copy-chip copied' : 'copy-chip'}
    title={`${value}\nクリックでコピー`} aria-label={`${display ?? value} をコピー`}
    onClick={() => void copy()}>
    <code>{display ?? value}</code>{copied && <span className="copy-flag">copied</span>}
  </button>
}
