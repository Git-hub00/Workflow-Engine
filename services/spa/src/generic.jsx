// services/spa/src/generic.jsx
//
// Generic, PDD-driven UI building blocks (P5, slice 1). These render from a
// process definition instead of hardcoding invoice, so ANY published process can
// be launched from the catalog. Reuses the existing App.css classes for styling.
import { useEffect, useState } from 'react'
import { get, post, upload } from './api'

// data_schema is {fieldName: "string"|"number"} in the PDD. Turn it into the
// fields[] shape DynamicForm renders.
function schemaToFields(dataSchema) {
  if (!dataSchema || typeof dataSchema !== 'object') return []
  return Object.entries(dataSchema).map(([key, type]) => ({
    key,
    type: type === 'number' ? 'number' : 'string',
  }))
}

// Render a set of inputs from a fields spec: [{key, type, values?, required?}].
// Shared by the Start form now and (slice 2) the task form.
export function DynamicForm({ fields, values, onChange, disabled = false }) {
  return (
    <div className="config-field-grid three-columns">
      {fields.map((field) => (
        <label className="config-field" key={field.key}>
          <span>{(field.label || field.key)}{field.required ? ' *' : ''}</span>
          {field.type === 'enum' ? (
            <select
              value={values[field.key] ?? ''}
              disabled={disabled}
              onChange={(event) => onChange(field.key, event.target.value)}
            >
              <option value="">—</option>
              {(field.values || []).map((v) => (
                <option key={v} value={v}>{v}</option>
              ))}
            </select>
          ) : (
            <input
              type={field.type === 'number' ? 'number' : 'text'}
              value={values[field.key] ?? ''}
              disabled={disabled}
              onChange={(event) => onChange(field.key, event.target.value)}
            />
          )}
        </label>
      ))}
    </div>
  )
}

// Catalog + generic launcher: pick a published process, fill the form built from
// its data_schema (optionally auto-filled from a PDF), and start a transaction.
export function StartProcess() {
  const [processes, setProcesses] = useState([])
  const [selected, setSelected] = useState('')
  const [pdd, setPdd] = useState(null)
  const [values, setValues] = useState({})
  const [busy, setBusy] = useState(false)
  const [extracting, setExtracting] = useState(false)
  const [feedback, setFeedback] = useState(null)

  useEffect(() => {
    get('/v1/definitions')
      .then((rows) => {
        const list = rows || []
        setProcesses(list)
        setSelected((current) => current || (list[0] ? list[0].process_key : ''))
      })
      .catch((error) => setFeedback({ type: 'error', message: error.message || String(error) }))
  }, [])

  useEffect(() => {
    if (!selected) return
    setPdd(null)
    setValues({})
    setFeedback(null)
    get(`/v1/definitions/${encodeURIComponent(selected)}`)
      .then(setPdd)
      .catch((error) => setFeedback({ type: 'error', message: error.message || String(error) }))
  }, [selected])

  const fields = pdd ? schemaToFields(pdd.data_schema) : []

  function updateValue(key, value) {
    setValues((current) => ({ ...current, [key]: value }))
  }

  async function handleFile(file) {
    if (!file) return
    setExtracting(true)
    setFeedback(null)
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('process_key', selected)
      const result = await upload('/v1/extract', form)
      const extracted = result.fields || {}
      setValues((current) => {
        const next = { ...current }
        for (const key of Object.keys(extracted)) {
          if (extracted[key] !== null && extracted[key] !== undefined) next[key] = String(extracted[key])
        }
        return next
      })
      if (result.error) setFeedback({ type: 'error', message: `Auto-fill partial: ${result.error}` })
    } catch (error) {
      setFeedback({ type: 'error', message: error.message || String(error) })
    } finally {
      setExtracting(false)
    }
  }

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setFeedback(null)
    try {
      const data = { ...values }
      for (const field of fields) {
        if (field.type === 'number' && data[field.key] !== undefined && data[field.key] !== '') {
          data[field.key] = Number(data[field.key])
        }
      }
      const result = await post('/v1/transactions', { process_key: selected, data })
      setFeedback({ type: 'success', message: `Started. Transaction ${result.transaction_id}.` })
      setValues({})
    } catch (error) {
      setFeedback({ type: 'error', message: error.message || String(error) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="view" aria-labelledby="start-process-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Any process</p>
          <h2 id="start-process-heading">Start a Process</h2>
          <p>Pick a published process and start it — the form is built from its definition.</p>
        </div>
      </div>

      <div className="toolbar">
        <label className="config-field">
          <span>Process</span>
          <select value={selected} onChange={(event) => setSelected(event.target.value)}>
            {processes.length === 0 && <option value="">No published processes</option>}
            {processes.map((p) => (
              <option key={p.process_key} value={p.process_key}>
                {p.process_key} (v{p.latest_version})
              </option>
            ))}
          </select>
        </label>
      </div>

      {selected && !pdd && <div className="loading-panel">Loading definition…</div>}

      {pdd && (
        <form className="config-form" onSubmit={submit}>
          <section className="config-section">
            <div className="config-section-heading">
              <div>
                <h3>Details</h3>
                <p>Fields for <span className="mono">{selected}</span>.</p>
              </div>
            </div>
            {fields.length ? (
              <DynamicForm fields={fields} values={values} onChange={updateValue} />
            ) : (
              <p className="muted">This process defines no data_schema fields.</p>
            )}
          </section>

          <section className="config-section">
            <div className="config-section-heading">
              <div>
                <h3>Upload PDF (optional)</h3>
                <p>Auto-fill the fields from a document, then review before starting.</p>
              </div>
            </div>
            <label className="pdf-dropzone">
              <input
                type="file"
                accept="application/pdf"
                className="pdf-file-input"
                onChange={(event) => handleFile(event.target.files?.[0])}
              />
              {extracting ? 'Extracting fields…' : 'Click to choose a PDF'}
            </label>
          </section>

          <div className="form-footer">
            {feedback && (
              <p className={`feedback ${feedback.type}`} role={feedback.type === 'error' ? 'alert' : 'status'}>
                {feedback.message}
              </p>
            )}
            <button className="primary-button" type="submit" disabled={busy || !selected}>
              {busy ? 'Starting…' : 'Start'}
            </button>
          </div>
        </form>
      )}

      {!selected && feedback && (
        <p className={`feedback ${feedback.type}`}>{feedback.message}</p>
      )}
    </section>
  )
}
