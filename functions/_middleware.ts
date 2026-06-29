/**
 * Cloudflare Pages Function — AI-bot / AI-referrer telemetry (CiteLab Sprint 1, T2).
 *
 * What it does: runs on EVERY request, sniffs the User-Agent for known AI crawlers
 * and the Referer for known AI answer engines, and emits one structured console line
 * per match. A crawl is a leading indicator that a citation may follow, so this is an
 * early-warning signal that "the silence" (no AI traffic) is or isn't happening.
 *
 * Pass-through: this middleware NEVER blocks, rewrites, or throws. It always calls
 * context.next() and returns that Response unchanged. Detection failures are swallowed.
 *
 * No persistence in v1 — logs only. View them live with:
 *   npx wrangler pages deployment tail
 * (filter, e.g.:  npx wrangler pages deployment tail | grep AIBOT )
 *
 * No dependencies: a minimal local context type is used instead of
 * @cloudflare/workers-types (which is intentionally NOT installed).
 */

// Minimal local shape of the Pages Functions context (no external types).
type PagesContext = {
  request: Request;
  next: () => Promise<Response>;
};

// AI crawler User-Agent tokens (case-insensitive substring match).
// Kept aligned with public/robots.txt and broadened to current major AI crawlers.
const AI_BOT_TOKENS: string[] = [
  'GPTBot',
  'OAI-SearchBot',
  'ChatGPT-User',
  'PerplexityBot',
  'Perplexity-User',
  'ClaudeBot',
  'Claude-User',
  'anthropic-ai',
  'Google-Extended',
  'GoogleOther',
  'CCBot',
  'Bingbot',
  'Bytespider',
  'Amazonbot',
  'Applebot-Extended',
  'meta-externalagent',
  'cohere-ai',
  'Timpibot',
  'YouBot',
  'DuckAssistBot',
];

// Hosts of AI answer engines a real user might arrive FROM (Referer match).
const AI_REFERRER_HOSTS: string[] = [
  'chatgpt.com',
  'perplexity.ai',
  'gemini.google.com',
  'copilot.microsoft.com',
  'claude.ai',
  'bing.com/chat',
];

// Attacker-controlled strings (UA, Referer) go into logs verbatim. Strip CR/LF so a
// crafted header can't forge a fake [AIBOT]/[AIREF] line, and cap length. This keeps the
// crawl signal (the whole point of this telemetry) trustworthy if logs are later shipped
// to an aggregator.
const logSafe = (s: string): string => s.replace(/[\r\n]+/g, ' ').slice(0, 512);

export const onRequest = async (context: PagesContext): Promise<Response> => {
  const { request } = context;

  // Telemetry must never affect the response — guard everything.
  try {
    const url = new URL(request.url);
    const ua = request.headers.get('user-agent') ?? '';
    const referer = request.headers.get('referer') ?? '';
    const country = (request as { cf?: { country?: string } }).cf?.country ?? '-';
    const now = new Date().toISOString();

    const uaLower = ua.toLowerCase();
    const matchedBot = AI_BOT_TOKENS.find((token) => uaLower.includes(token.toLowerCase()));
    if (matchedBot) {
      console.log(`[AIBOT] ${now} ua="${logSafe(ua)}" bot=${matchedBot} path="${url.pathname}" country=${country}`);
    }

    if (referer) {
      const refLower = referer.toLowerCase();
      const matchedRef = AI_REFERRER_HOSTS.some((host) => refLower.includes(host));
      if (matchedRef) {
        console.log(`[AIREF] ${now} ref="${logSafe(referer)}" path="${url.pathname}"`);
      }
    }
  } catch {
    // Never let telemetry break a request.
  }

  return context.next();
};
