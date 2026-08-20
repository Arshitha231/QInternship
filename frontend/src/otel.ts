// Browser tracing setup, split out of main.tsx so the render path stays
// readable and this can say what it needs to say.
//
// READ THIS BEFORE TREATING THE TOKEN AS A SECRET
// -----------------------------------------------
// The credential this file uses ships to the browser. That is not a flaw in
// how it is configured here -- it is what a browser-side OTLP exporter IS.
// The bundle is downloaded by every visitor, so anything baked into it at
// build time can be read out of devtools in about five seconds, whether it
// arrived as a hardcoded string or through import.meta.env.
//
// So moving it to an environment variable buys exactly one thing, and it is
// worth having: the value stops living in source control, where it is
// permanent, greppable, and copied into every clone and fork. It does NOT
// make the value private at runtime.
//
// What follows from that:
//   - The token must be WRITE-ONLY and scoped to trace ingestion, so the
//     worst a reader can do is send you junk spans.
//   - It should be rotated on a schedule, and immediately if it has ever
//     been committed (this one was -- see the PR that introduced this file).
//   - If it genuinely needs protecting, the browser cannot hold it: traces
//     have to go to our own API and be forwarded server-side with a
//     credential that never leaves the App Service.
//
// Unset means OFF, deliberately: local dev and CI builds have no collector,
// and a tracer pointed at nothing produces a console full of failed exports
// and retry noise on every page load.

import { WebTracerProvider } from "@opentelemetry/sdk-trace-web";
import { getWebAutoInstrumentations } from "@opentelemetry/auto-instrumentations-web";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { BatchSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { ZoneContextManager } from "@opentelemetry/context-zone";

/** OTLP's own header encoding: `key=value,key2=value2`, per the spec's
 *  OTEL_EXPORTER_OTLP_HEADERS. Parsed rather than assumed to be a bare
 *  Authorization value so the SAME repository secret already feeding the
 *  backend can feed this, instead of a second secret that can drift out of
 *  step with the first.
 *
 *  Splits on the FIRST `=` only: a base64 Basic credential routinely ends in
 *  padding `=`, and splitting on all of them truncates the token into
 *  something that fails auth with no useful error. */
function parseHeaders(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const pair of raw.split(",")) {
    const at = pair.indexOf("=");
    if (at <= 0) continue;
    const key = pair.slice(0, at).trim();
    const value = pair.slice(at + 1).trim();
    if (key && value) out[key] = value;
  }
  return out;
}

/** Accepts either the signal-specific traces URL or the base OTLP endpoint,
 *  because the two conventions are both common and the backend's own
 *  variable holds the base. Appending blindly would produce
 *  `/v1/traces/v1/traces` for anyone who set the full URL. */
function tracesUrl(endpoint: string): string {
  const trimmed = endpoint.replace(/\/+$/, "");
  return trimmed.endsWith("/v1/traces") ? trimmed : `${trimmed}/v1/traces`;
}

export function initTracing(): void {
  const endpoint = import.meta.env.VITE_OTEL_EXPORTER_OTLP_ENDPOINT as string | undefined;
  const headers = import.meta.env.VITE_OTEL_EXPORTER_OTLP_HEADERS as string | undefined;

  // No endpoint, no tracing. Silent rather than warned: this is the normal
  // state for every local dev server and every CI build, and a warning that
  // fires on every ordinary run is a warning people learn to ignore.
  if (!endpoint) return;

  const exporter = new OTLPTraceExporter({
    url: tracesUrl(endpoint),
    headers: headers ? parseHeaders(headers) : undefined,
  });

  // Span processors are constructor arguments; WebTracerProvider's
  // addSpanProcessor() was removed in the v2 SDK this project is pinned to.
  const provider = new WebTracerProvider({
    spanProcessors: [new BatchSpanProcessor(exporter)],
  });
  provider.register({ contextManager: new ZoneContextManager() });

  registerInstrumentations({
    instrumentations: [
      getWebAutoInstrumentations({
        "@opentelemetry/instrumentation-document-load": {},
        "@opentelemetry/instrumentation-user-interaction": {},
        "@opentelemetry/instrumentation-fetch": { clearTimingResources: true },
      }),
    ],
  });
}
