// services/spa/src/admin.jsx
//
// Administration screen (FSD §9), gated to ops_admin. Manage the Keycloak roles a
// process references, and the users who hold them — create/delete roles, and
// create / edit / delete users — via the admin-only API (/v1/admin/*). Authors
// only REFERENCE roles in the Builder; admins manage them here.
import { useEffect, useState } from 'react'
import { get, post, put, del } from './api'

const CORE_ROLES = ['ops_admin', 'process_author']

// Reusable checkbox picker for selecting a set of roles.
function RolePicker({ roles, selected, onToggle }) {
  if (!roles.length) return <span className="muted">No roles yet — create one above.</span>
  return (
    <div className="chip-picker">
      {roles.map((r) => (
        <label key={r} className={`chip-choice ${selected.includes(r) ? 'chip-on' : ''}`}>
          <input type="checkbox" checked={selected.includes(r)} onChange={() => onToggle(r)} />
          <span>{r}</span>
        </label>
      ))}
    </div>
  )
}

export function AdminPanel() {
  const [roles, setRoles] = useState([])
  const [processes, setProcesses] = useState([])
  const [users, setUsers] = useState([])
  const [newRole, setNewRole] = useState('')
  const [nu, setNu] = useState({ username: '', email: '', password: '12345', roles: [], processes: [] })
  const [editing, setEditing] = useState(null) // username being edited
  const [edit, setEdit] = useState({ email: '', password: '', roles: [], processes: [], new_username: '' })
  const [feedback, setFeedback] = useState(null)
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      const [r, u, d] = await Promise.all([get('/v1/admin/roles'), get('/v1/admin/users'),
        get('/v1/definitions').catch(() => [])])
      setProcesses((d || []).map((x) => x.process_key).filter(Boolean))
      setRoles(r || [])
      setUsers(u || [])
    } catch (e) {
      setFeedback({ type: 'error', message: e.message || String(e) })
    }
  }
  useEffect(() => { load() }, [])

  function toggle(list, setter, role) {
    setter(list.includes(role) ? list.filter((x) => x !== role) : [...list, role])
  }

  async function run(fn, okMsg) {
    setBusy(true)
    setFeedback(null)
    try {
      await fn()
      await load()
      if (okMsg) setFeedback({ type: 'success', message: okMsg })
    } catch (e) {
      setFeedback({ type: 'error', message: e.message || String(e) })
    } finally {
      setBusy(false)
    }
  }

  const createRole = () => {
    if (!newRole.trim()) return
    const name = newRole.trim()
    run(async () => { await post('/v1/admin/roles', { name }); setNewRole('') }, `Role '${name}' saved.`)
  }
  const deleteRole = (name) => run(() => del(`/v1/admin/roles/${encodeURIComponent(name)}`), `Role '${name}' deleted.`)

  const createUser = () => {
    if (!nu.username.trim()) return
    run(async () => {
      await post('/v1/admin/users', {
        username: nu.username.trim(),
        email: nu.email.trim() || null,
        password: nu.password || '12345',
        roles: nu.roles,
        processes: nu.processes,
      })
      setNu({ username: '', email: '', password: '12345', roles: [], processes: [] })
    }, `User '${nu.username.trim()}' saved.`)
  }
  const deleteUser = (username) => run(() => del(`/v1/admin/users/${encodeURIComponent(username)}`), `User '${username}' deleted.`)

  function startEdit(u) {
    setEditing(u.username)
    setEdit({ email: u.email || '', password: '', roles: u.roles || [],
      processes: u.processes || [], new_username: u.username })
  }
  const saveEdit = (username) => run(async () => {
    await put(`/v1/admin/users/${encodeURIComponent(username)}`, {
      email: edit.email || null,
      password: edit.password || null,
      roles: edit.roles,
      processes: edit.processes,
      new_username: edit.new_username && edit.new_username !== username ? edit.new_username : null,
    })
    setEditing(null)
  }, `User '${username}' updated.`)

  return (
    <section className="view" aria-labelledby="admin-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Administration</p>
          <h2 id="admin-heading">Admin — roles &amp; users</h2>
          <p>Create the roles your processes reference, and the people who hold them.</p>
        </div>
      </div>

      {feedback && (
        <p className={`feedback ${feedback.type}`} role={feedback.type === 'error' ? 'alert' : 'status'}>
          {feedback.message}
        </p>
      )}

      <div className="admin-grid">
        {/* Roles */}
        <section className="panel">
          <h3>Roles</h3>
          <div className="chip-picker">
            {roles.map((r) => (
              <span key={r} className="role-tag">
                {r}
                {!CORE_ROLES.includes(r) && (
                  <button type="button" className="tag-x" title={`Delete ${r}`} disabled={busy} onClick={() => deleteRole(r)}>×</button>
                )}
              </span>
            ))}
            {roles.length === 0 && <span className="muted">No roles yet.</span>}
          </div>
          <form className="inline-form" onSubmit={(e) => { e.preventDefault(); createRole() }}>
            <input type="text" placeholder="new role (e.g. manager)" value={newRole} onChange={(e) => setNewRole(e.target.value)} />
            <button className="primary-button" type="submit" disabled={busy}>Add role</button>
          </form>
        </section>

        {/* Create user */}
        <section className="panel">
          <h3>Add user</h3>
          <form className="stack-form" onSubmit={(e) => { e.preventDefault(); createUser() }}>
            <label className="field"><span>Username</span>
              <input value={nu.username} onChange={(e) => setNu({ ...nu, username: e.target.value })} /></label>
            <label className="field"><span>Email</span>
              <input value={nu.email} onChange={(e) => setNu({ ...nu, email: e.target.value })} /></label>
            <label className="field"><span>Password</span>
              <input value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} /></label>
            <div className="field"><span>Roles</span>
              <RolePicker roles={roles} selected={nu.roles} onToggle={(r) => toggle(nu.roles, (v) => setNu({ ...nu, roles: v }), r)} /></div>
            <div className="config-field"><span>Workflows this person works on</span>
              <RolePicker roles={processes} selected={nu.processes} onToggle={(p) => toggle(nu.processes, (v) => setNu({ ...nu, processes: v }), p)} />
              <span className="muted" style={{ fontSize: '0.75rem' }}>Tick none and they see no tasks. A person can be in several workflows.</span></div>
            <button className="primary-button" type="submit" disabled={busy}>Add user</button>
          </form>
        </section>
      </div>

      {/* Users table */}
      <section className="panel">
        <h3>Users</h3>
        <div className="table-scroll">
          <table className="admin-table">
            <thead>
              <tr><th>Username</th><th>Email</th><th>Roles</th><th>Workflows</th><th></th></tr>
            </thead>
            <tbody>
              {users.map((u) => (
                editing === u.username ? (
                  <tr key={u.username} className="editing-row">
                    <td><input value={edit.new_username} onChange={(e) => setEdit({ ...edit, new_username: e.target.value })} /></td>
                    <td><input value={edit.email} onChange={(e) => setEdit({ ...edit, email: e.target.value })} /></td>
                    <td colSpan={2}>
                      <RolePicker roles={roles} selected={edit.roles} onToggle={(r) => toggle(edit.roles, (v) => setEdit({ ...edit, roles: v }), r)} />
                      <div className="mini-label" style={{ marginTop: 8 }}>Workflows</div>
                      <RolePicker roles={processes} selected={edit.processes} onToggle={(p) => toggle(edit.processes, (v) => setEdit({ ...edit, processes: v }), p)} />
                      <input className="pw-input" placeholder="new password (optional)" value={edit.password} onChange={(e) => setEdit({ ...edit, password: e.target.value })} />
                    </td>
                    <td className="row-actions">
                      <button className="primary-button" type="button" disabled={busy} onClick={() => saveEdit(u.username)}>Save</button>
                      <button className="secondary-button" type="button" onClick={() => setEditing(null)}>Cancel</button>
                    </td>
                  </tr>
                ) : (
                  <tr key={u.username}>
                    <td>{u.username}</td>
                    <td>{u.email || '—'}</td>
                    <td>{(u.roles || []).join(', ') || '—'}</td>
                    <td>{(u.processes || []).join(', ') || <span className="muted">none — sees no tasks</span>}</td>
                    <td className="row-actions">
                      <button className="secondary-button" type="button" onClick={() => startEdit(u)}>Edit</button>
                      {u.username !== 'admin1' && (
                        <button className="danger-button" type="button" disabled={busy} onClick={() => deleteUser(u.username)}>Delete</button>
                      )}
                    </td>
                  </tr>
                )
              ))}
              {users.length === 0 && <tr><td colSpan="4" className="muted">No users yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
    </section>
  )
}
