import type { APIRoute } from 'astro';
import { getCollection } from 'astro:content';
import { SITE, METADATA } from 'astrowind:config';

export const prerender = true;

const getSlug = (id: string) => id.replace(/^.*[/\\]/, '').replace(/\.[^.]+$/, '');
const isoDate = (date: Date) => date.toISOString().slice(0, 10);

export const GET: APIRoute = async () => {
  const entries = await getCollection('geoPractices');
  entries.sort((a, b) => b.data.published.valueOf() - a.data.published.valueOf());

  const summary =
    METADATA?.description ||
    'Practical GEO (Generative Engine Optimization) practices for getting cited by AI search engines.';

  const parts = [`# ${SITE.name} — Full Content`, '', `> ${summary}`, '', `Generated: ${isoDate(new Date())}`, ''];

  for (const entry of entries) {
    const url = new URL('/learn/' + getSlug(entry.id), SITE.site).href;
    parts.push(
      '---',
      '',
      `# ${entry.data.title}`,
      `URL: ${url}`,
      `Published: ${isoDate(entry.data.published)}  Updated: ${isoDate(entry.data.updated)}`,
      '',
      entry.body ?? '',
      ''
    );
  }

  return new Response(parts.join('\n'), {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
};
