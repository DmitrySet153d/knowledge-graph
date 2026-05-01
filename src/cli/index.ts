import { Command } from 'commander';
import { mkdirSync } from 'fs';
import { resolveConfig } from '../lib/config.js';
import { Store } from '../lib/store.js';
import { Embedder } from '../lib/embedder.js';
import { IndexPipeline } from '../lib/index-pipeline.js';
import { KnowledgeGraph } from '../lib/graph.js';
import { Search } from '../lib/search.js';
import { resolveNodeName } from '../lib/resolve.js';

const program = new Command();

program
  .name('kg')
  .description('Knowledge graph tools for Obsidian vaults')
  .version('0.1.0')
  .option('--vault-path <path>', 'Path to Obsidian vault')
  .option('--data-dir <path>', 'Path to data directory');

function getConfig() {
  const opts = program.opts();
  return resolveConfig({
    vaultPath: opts.vaultPath,
    dataDir: opts.dataDir,
  });
}

function getStore() {
  const config = getConfig();
  mkdirSync(config.dataDir, { recursive: true });
  return new Store(config.dbPath);
}

function output(data: unknown) {
  console.log(JSON.stringify(data, null, 2));
}

function requireSingleMatch(name: string, store: Store): string {
  const matches = resolveNodeName(name, store);
  if (matches.length === 0) {
    console.error(`No node found matching "${name}"`);
    process.exit(1);
  }
  if (matches.length > 1 && matches[0].matchType !== 'exact' && matches[0].matchType !== 'id') {
    output({ ambiguous: true, hint: 'Use the full node ID to disambiguate', candidates: matches });
    process.exit(1);
  }
  return matches[0].nodeId;
}

program
  .command('index')
  .description('Parse vault and build/update the knowledge graph')
  .option('--resolution <number>', 'Louvain resolution parameter', '1.0')
  .option('--force', 'Force full re-index (ignore sync state)')
  .action(async (opts) => {
    const config = getConfig();
    mkdirSync(config.dataDir, { recursive: true });
    const store = new Store(config.dbPath);
    if (opts.force) {
      store.db.prepare('DELETE FROM sync').run();
    }
    const embedder = new Embedder();
    await embedder.init();
    const pipeline = new IndexPipeline(store, embedder);
    const stats = await pipeline.index(config.vaultPath, parseFloat(opts.resolution));
    output(stats);
    await embedder.dispose();
    store.close();
  });

program
  .command('node <name>')
  .description('Get a node with its content and connections')
  .option('--full', 'Return full content and edge context (default is brief)')
  .option('--max-content <n>', 'Truncate content to N chars in full mode', '2000')
  .action((name, opts) => {
    const store = getStore();
    const nodeId = requireSingleMatch(name, store);
    const node = store.getNode(nodeId);
    if (!node) { console.error(`Node not found`); process.exit(1); }

    if (opts.full) {
      const limit = parseInt(opts.maxContent);
      const truncatedContent = node.content.length > limit
        ? node.content.slice(0, limit) + `\n\n... [truncated, ${node.content.length} chars total]`
        : node.content;
      const outgoing = store.getEdgesFrom(nodeId).map(e => ({
        ...e, context: e.context.length > 200 ? e.context.slice(0, 200) + '...' : e.context,
      }));
      const incoming = store.getEdgesTo(nodeId).map(e => ({
        ...e, context: e.context.length > 200 ? e.context.slice(0, 200) + '...' : e.context,
      }));
      output({ ...node, content: truncatedContent, outgoing, incoming });
    } else {
      const outgoing = store.getEdgeSummariesFrom(nodeId);
      const incoming = store.getEdgeSummariesTo(nodeId);
      output({
        id: node.id, title: node.title, frontmatter: node.frontmatter,
        outgoingCount: store.countEdgesFrom(nodeId),
        incomingCount: store.countEdgesTo(nodeId),
        outgoing, incoming,
      });
    }
    store.close();
  });

program
  .command('neighbors <name>')
  .description('Get connected nodes')
  .option('--depth <n>', 'Hop depth', '1')
  .action((name, opts) => {
    const store = getStore();
    const nodeId = requireSingleMatch(name, store);
    const kg = KnowledgeGraph.fromStore(store);
    const neighbors = kg.neighbors(nodeId, parseInt(opts.depth));
    output(neighbors);
    store.close();
  });

program
  .command('search <query>')
  .description('Search the knowledge graph')
  .option('--fulltext', 'Use full-text search instead of semantic')
  .option('--limit <n>', 'Max results', '20')
  .action(async (query, opts) => {
    const store = getStore();
    if (opts.fulltext) {
      const results = store.searchFullText(query).slice(0, parseInt(opts.limit));
      output(results);
    } else {
      const embedder = new Embedder();
      await embedder.init();
      const search = new Search(store, embedder);
      const results = await search.semantic(query, parseInt(opts.limit));
      output(results);
      await embedder.dispose();
    }
    store.close();
  });

program
  .command('paths <from> <to>')
  .description('Find connecting paths between two nodes')
  .option('--max-depth <n>', 'Maximum path depth', '3')
  .action((from, to, opts) => {
    const store = getStore();
    const fromId = requireSingleMatch(from, store);
    const toId = requireSingleMatch(to, store);
    const kg = KnowledgeGraph.fromStore(store);
    const paths = kg.findPaths(fromId, toId, parseInt(opts.maxDepth));
    output(paths);
    store.close();
  });

program
  .command('common <nodeA> <nodeB>')
  .description('Find shared connections between two nodes')
  .action((nodeA, nodeB) => {
    const store = getStore();
    const idA = requireSingleMatch(nodeA, store);
    const idB = requireSingleMatch(nodeB, store);
    const kg = KnowledgeGraph.fromStore(store);
    const common = kg.commonNeighbors(idA, idB);
    output(common);
    store.close();
  });

program
  .command('subgraph <name>')
  .description('Extract a local neighborhood')
  .option('--depth <n>', 'Hop depth', '1')
  .action((name, opts) => {
    const store = getStore();
    const nodeId = requireSingleMatch(name, store);
    const kg = KnowledgeGraph.fromStore(store);
    const sub = kg.subgraph(nodeId, parseInt(opts.depth));
    output(sub);
    store.close();
  });

program
  .command('communities')
  .description('List detected communities')
  .action(() => {
    const store = getStore();
    const communities = store.getAllCommunities();
    output(communities.map(c => ({
      id: c.id,
      label: c.label,
      summary: c.summary,
      memberCount: c.nodeIds.length,
    })));
    store.close();
  });

program
  .command('community <id>')
  .description('Get a specific community')
  .action((id) => {
    const store = getStore();
    const communities = store.getAllCommunities();
    const numId = /^\d+$/.test(id) ? parseInt(id) : NaN;
    const community = communities.find(c => c.id === numId || c.label === id);
    if (!community) {
      console.error(`Community "${id}" not found`);
      process.exit(1);
    }
    output(community);
    store.close();
  });

program
  .command('bridges')
  .description('Find bridge nodes (high betweenness centrality)')
  .option('--limit <n>', 'Max results', '20')
  .action((opts) => {
    const store = getStore();
    const kg = KnowledgeGraph.fromStore(store);
    const bridges = kg.bridges(parseInt(opts.limit));
    output(bridges);
    store.close();
  });

program
  .command('probe')
  .description('Emit health-probe JSON of the indexed graph (nodes/edges/stubs/communities/sections)')
  .action(() => {
    const store = getStore();
    const db = store.db;
    const one = (sql: string, p: unknown[] = []) =>
      (db.prepare(sql).get(...p) as { c: number } | undefined)?.c ?? 0;

    const nodesTotal = one('SELECT COUNT(*) AS c FROM nodes');
    const nodesReal = one(
      "SELECT COUNT(*) AS c FROM nodes WHERE id NOT LIKE '_stub/%'",
    );
    const nodesStub = one(
      "SELECT COUNT(*) AS c FROM nodes WHERE id LIKE '_stub/%'",
    );
    const edgesTotal = one('SELECT COUNT(*) AS c FROM edges');
    const communitiesTotal = one('SELECT COUNT(*) AS c FROM communities');

    const singletonDetails: Array<{ id: number; label: string; members: string[] }> = [];
    let communitiesMalformed = 0;
    const commRows = db
      .prepare('SELECT id, label, node_ids FROM communities ORDER BY id')
      .all() as Array<{ id: number; label: string; node_ids: string }>;
    for (const r of commRows) {
      let members: string[] | null = null;
      try {
        const parsed = JSON.parse(r.node_ids);
        if (Array.isArray(parsed)) members = parsed.map(String);
      } catch {
        // fall through
      }
      if (members === null) {
        communitiesMalformed++;
        continue;
      }
      if (members.length <= 1) {
        singletonDetails.push({ id: r.id, label: r.label, members });
      }
    }

    const topStubsRows = db
      .prepare(
        `SELECT n.id AS id, COUNT(*) AS inbound FROM nodes n
         JOIN edges e ON e.target_id = n.id
         WHERE n.id LIKE '_stub/%'
         GROUP BY n.id ORDER BY inbound DESC LIMIT 10`,
      )
      .all() as Array<{ id: string; inbound: number }>;

    const leakedPrefixes = ['output/', 'scripts/', 'vault_backup_', '_FileOrganizer2000/'];
    const leaked: Record<string, number> = {};
    for (const prefix of leakedPrefixes) {
      const n = one('SELECT COUNT(*) AS c FROM nodes WHERE id LIKE ?', [prefix + '%']);
      if (n) leaked[prefix.replace(/\/$/, '')] = n;
    }

    const sectionRows = db
      .prepare(
        `SELECT CASE WHEN id LIKE '%/%' THEN substr(id, 1, instr(id, '/')-1) ELSE id END AS section,
                COUNT(*) AS n
         FROM nodes WHERE id NOT LIKE '_stub/%'
         GROUP BY section`,
      )
      .all() as Array<{ section: string; n: number }>;
    const sections: Record<string, number> = {};
    for (const r of sectionRows) sections[r.section] = r.n;

    const lastIndexed = db
      .prepare('SELECT MAX(indexed_at) AS m FROM sync')
      .get() as { m: number | null } | undefined;

    const probe: Record<string, unknown> = {
      nodes_total: nodesTotal,
      nodes_real: nodesReal,
      nodes_stub: nodesStub,
      edges_total: edgesTotal,
      communities_total: communitiesTotal,
      communities_singleton: singletonDetails.length,
      singleton_details: singletonDetails,
      top_stubs: topStubsRows.map((r) => ({ id: r.id, inbound: r.inbound })),
      leaked_artifact_nodes: leaked,
      sections,
      last_indexed_at_ms: lastIndexed?.m ?? null,
    };
    if (communitiesMalformed > 0) probe.communities_malformed = communitiesMalformed;

    output(probe);
    store.close();
  });

program
  .command('central')
  .description('Find central nodes (PageRank)')
  .option('--community <id>', 'Restrict to a community')
  .option('--limit <n>', 'Max results', '20')
  .action((opts) => {
    const store = getStore();
    const kg = KnowledgeGraph.fromStore(store);
    let communityNodeIds: string[] | undefined;
    if (opts.community) {
      const communities = store.getAllCommunities();
      const c = communities.find(c => c.id === parseInt(opts.community));
      communityNodeIds = c?.nodeIds;
    }
    const central = kg.centralNodes(parseInt(opts.limit), communityNodeIds);
    output(central);
    store.close();
  });

program.parse();
