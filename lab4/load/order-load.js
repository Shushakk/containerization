import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:18000';

export const options = {
  stages: [
    { duration: '30s', target: 1 },
    { duration: '1m',  target: 3 },
    { duration: '1m',  target: 5 },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<300'],
  },
};

export default function () {
  const response = http.post(
    `${BASE_URL}/order`,
    JSON.stringify({
      item: 'sla-load-test',
      quantity: 1,
    }),
    {
      headers: {
        'Content-Type': 'application/json',
      },
      tags: {
        endpoint: 'create_order',
      },
    },
  );

  check(response, {
    'order created successfully (HTTP 200)': (r) => r.status === 200,
  });
}
