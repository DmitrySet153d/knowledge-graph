import { stat } from 'fs/promises';
import { join } from 'path';
import { parseVault } from './parser.js';
import type { Store } from './store.js';
import { Embedder } from './embedder.js';
import { KnowledgeGraph } from './graph.js';

export interface IndexStats {
  nodesIndexed: number;
  nodesSkipped: number;
  nodesDeleted: number;
  edgesIndexed: number;
  communitiesDetected: number;
  stubNodesCreated: number;
}

export class IndexPipeline {
  constructor(
    private store: Store,
    private embedder: Embedder,
  ) {}

  async index(vaultPath: string, resolution = 1.0): Promise<IndexStats> {
    const stats: IndexStats = {
      nodesIndexed: 0,
      nodesSkipped: 0,
      nodesDeleted: 0,
      edgesIndexed: 0,
      communitiesDetected: 0,
      stubNodesCreated: 0,
    };

    const { nodes, edges, stubIds } = await parseVault(vaultPath);
    const previousPaths = this.store.getAllSyncPaths();

    // Detect deleted files
    const currentPaths = new Set(nodes.map(n => n.id));
    for (const oldPath of previousPaths) {
      if (!currentPaths.has(oldPath)) {
        this.store.deleteNode(oldPath);
        stats.nodesDeleted++;
      }
    }

    // Index nodes (incremental)
    for (const node of nodes) {
      const fileStat = await stat(join(vaultPath, node.id));
      const mtime = fileStat.mtimeMs;
      const prevMtime = this.store.getSyncMtime(node.id);

      if (prevMtime !== undefined && prevMtime >= mtime) {
        stats.nodesSkipped++;
        continue;
      }

      this.store.upsertNode(node);

      // Compute and store embedding
      const tags = Array.isArray(node.frontmatter.tags) ? node.frontmatter.tags : [];
      const text = Embedder.buildEmbeddingText(node.title, tags as string[], node.content);
      const embedding = await this.embedder.embed(text);
      this.store.upsertEmbedding(node.id, embedding);

      // Re-index edges from this node
      this.store.deleteAllEdgesFrom(node.id);
      for (const edge of edges.filter(e => e.sourceId === node.id)) {
        this.store.insertEdge(edge);
        stats.edgesIndexed++;
      }

      this.store.upsertSync(node.id, mtime);
      stats.nodesIndexed++;
    }

    // Create stub nodes
    for (const stubId of stubIds) {
      if (!this.store.getNode(stubId)) {
        this.store.upsertNode({
          id: stubId,
          title: stubId.replace('_stub/', '').replace('.md', ''),
          content: '',
          frontmatter: { _stub: true },
        });
        stats.stubNodesCreated++;
      }
    }

    // Re-run community detection on any structural change (add / delete / stub).
    // Without this, deletions leave singleton communities pointing at gone nodes.
    if (stats.nodesIndexed > 0 || stats.stubNodesCreated > 0 || stats.nodesDeleted > 0) {
      const kg = KnowledgeGraph.fromStore(this.store);
      const communities = kg.detectCommunities(resolution);
      this.store.clearCommunities();
      for (const c of communities) {
        this.store.upsertCommunity(c);
      }
      stats.communitiesDetected = communities.length;
    }

    return stats;
  }
}
