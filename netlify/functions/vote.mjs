// Голосование за «цитату дня».
// GET  /api/vote?cid=<clientId>  -> результаты сегодня/вчера + за что голосовал этот клиент
// POST /api/vote {id, cid}       -> записать голос (один голос в день на клиента)
import { getStore } from "@netlify/blobs";

export const config = { path: "/api/vote" };

const TZ = "Europe/Moscow";      // по какому часовому поясу меняется день
const MAX_PER_IP = 5;            // сколько разных клиентов с одного IP могут голосовать в день
const SALT = Netlify.env.get("VOTE_SALT") || "mzlff-default-salt";

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

function dayKey(offsetDays = 0) {
  const d = new Date(Date.now() + offsetDays * 86400000);
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
  }).format(d); // YYYY-MM-DD
}

async function sha(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(SALT + text));
  return [...new Uint8Array(buf)].slice(0, 12).map(b => b.toString(16).padStart(2, "0")).join("");
}

async function listKeys(store, prefix) {
  const keys = [];
  for await (const page of store.list({ prefix, paginate: true })) {
    for (const b of page.blobs) keys.push(b.key);
  }
  return keys;
}

// ключи голосов: votes/<день>/<id цитаты>/<хэш клиента>
async function tally(store, day) {
  const keys = await listKeys(store, `votes/${day}/`);
  const counts = {};
  for (const k of keys) {
    const id = k.split("/")[2];
    counts[id] = (counts[id] || 0) + 1;
  }
  return Object.entries(counts)
    .map(([id, votes]) => ({ id, votes }))
    .sort((a, b) => b.votes - a.votes || a.id.localeCompare(b.id))
    .slice(0, 5);
}

// итоги прошедшего дня считаем один раз и запоминаем
async function finalTop(store, day) {
  const cached = await store.get(`final/${day}`, { type: "json" });
  if (cached) return cached;
  const top = await tally(store, day);
  await store.setJSON(`final/${day}`, top);
  return top;
}

export default async (req, context) => {
  const store = getStore("quote-votes");
  const today = dayKey(0);
  const yesterday = dayKey(-1);
  const url = new URL(req.url);

  if (req.method === "GET") {
    const cid = url.searchParams.get("cid") || "";
    let voted = null;
    if (/^[a-zA-Z0-9-]{8,64}$/.test(cid)) {
      voted = await store.get(`voter/${today}/${await sha(cid)}`);
    }
    return json({
      day: today,
      today: await tally(store, today),
      yesterday: { day: yesterday, top: await finalTop(store, yesterday) },
      voted,
    });
  }

  if (req.method === "POST") {
    let body;
    try { body = await req.json(); } catch { return json({ error: "bad json" }, 400); }
    const { id, cid } = body || {};
    if (!/^[a-z0-9]{1,10}$/.test(id || "")) return json({ error: "bad id" }, 400);
    if (!/^[a-zA-Z0-9-]{8,64}$/.test(cid || "")) return json({ error: "bad cid" }, 400);

    const voter = await sha(cid);
    if (await store.get(`voter/${today}/${voter}`)) {
      return json({ error: "already voted" }, 409);
    }

    const ipHash = await sha("ip:" + (context.ip || "unknown"));
    const ipKeys = await listKeys(store, `ip/${today}/${ipHash}/`);
    if (ipKeys.length >= MAX_PER_IP) return json({ error: "too many votes from this network" }, 429);

    await store.set(`voter/${today}/${voter}`, id);
    await store.set(`ip/${today}/${ipHash}/${voter}`, "1");
    await store.set(`votes/${today}/${id}/${voter}`, "1");

    return json({ ok: true, day: today, today: await tally(store, today), voted: id });
  }

  return json({ error: "method not allowed" }, 405);
};
