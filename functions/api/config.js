export async function onRequestGet(context) {
  // Example: compute/merge your "engine room" config on the fly
  const defaultRules = { discounts: { "0-100": 0.3, "101-220": 0.2 } };
  const pageOverrides = { /* you could read from KV/D1 later */ };

  const cfg = { ...defaultRules, ...pageOverrides };

  return new Response(JSON.stringify(cfg), {
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      // IMPORTANT: Functions responses ignore _headers rules — set here
      "Cache-Control": "no-store, no-cache, must-revalidate",
    },
  });
}
