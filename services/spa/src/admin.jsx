// services/spa/src/admin.jsx
//
// Administration screen (FSD §9 "Configuration & Administration"), gated to
// ops_admin. Manage the Keycloak roles a process references, and the users who
// hold them — via the admin-only API (/v1/admin/*). Separation of duties: authors
// reference roles in the Builder; admins manage them here.
import { useEffect, useState } from 'react'
import { get, post } from './api'

export function AdminPanel() {
  const [roles, setRoles] = useState([])
  const [users, setUsers] = useState([])
  const [newRole, setNewRole] = useState('')
  const [nu, setNu] = useState({ username: '', email: '', password: '12345', roles: '' })
  const [feedback, setFeedback] = useState(null)
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      const [r, u] = await Promise.all([get('/v1/admin/roles'), get('/v1/admin/users')])
      setRoles(r || [])
      setUsers(u || [])
    } catch (e) {
      setFeedback({ type: 'error', message: e.message || String(e) })
    }
  }

  useEffect(() => { load() }, [])

  async function createRole(e) {
    e.preventDefault()
    if (!newRole.trim()) return
    setBusy(true)
    setFeedback(null)
    try {
      await post('/v1/admin/roles', { name: newRole.trim() })
      setNewRole('')
      await load()
      setFeedback({ type: 'success', message: 'Role saved.' })
    } catch (e) {
      setFeedback({ type: 'error', message: e.message || String(e) })
    } finally {
      setBusy(false)
    }
  }

  async function createUser(e) {
    e.preventDefault()
    if (!nu.username.trim()) return
    setBusy(true)
    setFeedback(null)
    try {
      await post('/v1/admin/users', {
        username: nu.username.trim(),
        email: nu.email.trim() || null,
        password: nu.password || '12345',
        roles: nu.roles.split(',').map((s) => s.trim()).filter(Boolean),
      })
      setNu({ username: '', email: '', password: '12345', roles: '' })
      await load()
      setFeedback({ type: 'success', message: 'User saved.' })
    } catch (e) {
      setFeedback({ type: 'error', message: e.message || String(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="view" aria-labelledby="admin-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Administration</p>
          <h2 id="admin-heading">Admin — roles &amp; users</h2>
          <p>Create the roles your processes reference, and the people who hold them.</p>
        </div>
      </div>

      {feedback && <p className={`feedback ${feedback.type}`}>{feedback.message}</p>}

      <div className="config-form">
        <section className="config-section">
          <div className="config-section-heading"><div><h3>Roles</h3></div></div>
          <p className="muted">{roles.length ? roles.join(', ') : 'No roles yet.'}</p>
          <form className="kv-row" onSubmit={createRole}>
            <input type="text" placeholder="new role (e.g. manager)" value={newRole} onChange={(e) => setNewRole(e.target.value)} />
            <button className="primary-button" type="submit" disabled={busy}>Add role</button>
          </form>
        </section>

        <section className="config-section">
          <div className="config-section-heading"><div><h3>Users</h3></div></div>
          <ul className="muted">
            {users.map((u) => <li key={u.username}>{u.username}{u.email ? ` — ${u.email}` : ''}</li>)}
            {users.length === 0 && <li>No users.</li>}
          </ul>
          <form className="config-field-grid three-columns" onSubmit={createUser}>
            <label className="config-field"><span>Username</span>
              <input value={nu.username} onChange={(e) => setNu({ ...nu, username: e.target.value })} /></label>
            <label className="config-field"><span>Email</span>
              <input value={nu.email} onChange={(e) => setNu({ ...nu, email: e.target.value })} /></label>
            <label className="config-field"><span>Password</span>
              <input value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} /></label>
            <label className="config-field"><span>Roles (comma-separated)</span>
              <input value={nu.roles} placeholder="manager,finance" onChange={(e) => setNu({ ...nu, roles: e.target.value })} /></label>
            <div className="config-field" style={{ justifyContent: 'flex-end' }}>
              <button className="primary-button" type="submit" disabled={busy}>Add user</button>
            </div>
          </form>
        </section>
      </div>
    </section>
  )
}
