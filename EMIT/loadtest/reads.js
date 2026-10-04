// Read-path baseline: availability at a fixed arrival rate, nothing else running.
//   docker compose --profile loadtest run --rm --entrypoint k6 k6 run -e RPS=500 reads.js
import http from 'k6/http'

const BASE = __ENV.BASE_URL || 'http://localhost:8080'
const EVENT = __ENV.EVENT || '00000000-0000-4000-8000-000000000001'

export const options = {
  scenarios: {
    reads: { executor: 'constant-arrival-rate', rate: Number(__ENV.RPS || 500), timeUnit: '1s', duration: __ENV.DURATION || '20s', preAllocatedVUs: 100, maxVUs: 500 },
  },
  thresholds: { http_req_duration: ['p(95)<50', 'p(99)<150'], http_req_failed: ['rate==0'] },
  summaryTrendStats: ['med', 'p(95)', 'p(99)', 'max', 'count'],
}

export default function () {
  http.get(`${BASE}/api/events/${EVENT}/availability`)
}
