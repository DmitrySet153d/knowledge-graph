import { readdir, readFile } from 'fs/promises';
import { join, basename } from 'path';
import matter from 'gray-matter';
import {
  extractWikiLinks,
  buildStemLookup,
  resolveLink,
} from './wiki-links.js';
import type { ParsedNode, ParsedEdge } from './types.js';

const EXCLUDED_DIRS = new Set(['.obsidian', '_FileOrganizer2000', 'attachments', 'output', 'scripts']);

export interface ParseResult {
  nodes: ParsedNode[];
  edges: ParsedEdge[];
  stubIds: Set<string>;
}

export async function parseVault(vaultPath: string): Promise<ParseResult> {
  const mdPaths = await collectMarkdownFiles(vaultPath);
  const stemLookup = buildStemLookup(mdPaths);
  const allPathsSet = new Set(mdPaths);
  const nodes: ParsedNode[] = [];
  const edges: ParsedEdge[] = [];
  const stubIds = new Set<string>();

  for (const relPath of mdPaths) {
    const absPath = join(vaultPath, relPath);
    const raw = await readFile(absPath, 'utf-8');

    let fm: Record<string, unknown>;
    let content: string;
    try {
      const parsed = matter(raw);
      fm = parsed.data;
      content = parsed.content;
    } catch {
      // Malformed YAML frontmatter — treat entire file as content
      console.warn(`Malformed frontmatter in ${relPath}, treating as plain markdown`);
      fm = {};
      content = raw;
    }

    const title = (fm.title as string)
      ?? basename(relPath, '.md');

    const inlineTags = extractInlineTags(content);
    const frontmatter = { ...fm };
    if (inlineTags.length > 0) {
      frontmatter.inline_tags = inlineTags;
    }

    nodes.push({ id: relPath, title, content, frontmatter });

    const links = extractWikiLinks(content);
    const paragraphs = content.split(/\n\n+/);

    for (const link of links) {
      const targetId = resolveLink(link.raw, stemLookup, allPathsSet);
      const resolvedTarget = targetId ?? `_stub/${link.raw}.md`;

      if (!targetId) {
        stubIds.add(resolvedTarget);
      }

      const context = paragraphs.find(p => p.includes(`[[${link.raw}`))
        ?? paragraphs.find(p => p.includes(link.display ?? link.raw))
        ?? '';

      edges.push({
        sourceId: relPath,
        targetId: resolvedTarget,
        context: context.trim(),
      });
    }
  }

  return { nodes, edges, stubIds };
}

function extractInlineTags(content: string): string[] {
  const tags = new Set<string>();
  const pattern = /(?<!\w)#([a-zA-Z][\w-\/]*)/g;
  let match;
  while ((match = pattern.exec(content)) !== null) {
    tags.add(match[1]);
  }
  return [...tags];
}

async function collectMarkdownFiles(
  vaultPath: string,
  subdir = '',
): Promise<string[]> {
  const results: string[] = [];
  const dirPath = join(vaultPath, subdir);
  const entries = await readdir(dirPath, { withFileTypes: true });

  for (const entry of entries) {
    if (entry.name.startsWith('.')) continue;
    if (entry.isDirectory() && EXCLUDED_DIRS.has(entry.name)) continue;

    const relPath = subdir ? `${subdir}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      results.push(...await collectMarkdownFiles(vaultPath, relPath));
    } else if (entry.name.endsWith('.md')) {
      results.push(relPath);
    }
  }

  return results;
}
