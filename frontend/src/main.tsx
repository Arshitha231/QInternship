import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { initTracing } from './otel'

// Configured entirely by build-time environment (see otel.ts, including why
// the credential it uses is not — and cannot be — a secret in a browser).
// No-ops when unset, which is the normal state locally and in CI.
initTracing()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
