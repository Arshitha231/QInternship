import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// --- 1. Import OpenTelemetry modules ---
import { WebTracerProvider } from '@opentelemetry/sdk-trace-web';
import { getWebAutoInstrumentations } from '@opentelemetry/auto-instrumentations-web';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { registerInstrumentations } from '@opentelemetry/instrumentation';
import { ZoneContextManager } from '@opentelemetry/context-zone';


// --- 2. Initialize the Web Exporter ---
const exporter = new OTLPTraceExporter({
  // Use the endpoint Grafana gave you, and append /v1/traces
  url: 'https://otlp-gateway-prod-us-west-0.grafana.net/otlp/v1/traces',
  headers: {
    // Replace %20 with a space, and do not include the "Authorization=" prefix
    Authorization: 'Basic MTc5NTg4NzpnbGNfZXlKdklqb2lNVGc0TVRnME1TSXNJbTRpT2lKeGRXRmtjbUZ1ZEMxdmRHVnNMWFJ2YTJWdUlpd2lheUk2SW1vek9VdFpValphWXpaaE0ydDFSalZFTnpaSFJqUXpSU0lzSW0waU9uc2ljaUk2SW5CeWIyUXRkWE10ZDJWemRDMHdJbjE5',
  },
});

const provider = new WebTracerProvider({
  spanProcessors: [new BatchSpanProcessor(exporter)],
});
provider.register({
  contextManager: new ZoneContextManager()
});

// --- 3. Register Auto-Instrumentations ---
// This automatically captures document load times, button clicks, and API fetch latencies
registerInstrumentations({
  instrumentations: [
    getWebAutoInstrumentations({
      '@opentelemetry/instrumentation-document-load': {},
      '@opentelemetry/instrumentation-user-interaction': {},
      '@opentelemetry/instrumentation-fetch': {
        clearTimingResources: true,
      },
    }),
  ],
});

// --- 4. Existing React Render ---
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)