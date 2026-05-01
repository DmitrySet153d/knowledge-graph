import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { join } from 'path';
import { Store } from '../src/lib/store.js';
import { Embedder } from '../src/lib/embedder.js';
import { IndexPipeline } from '../src/lib/index-pipeline.js';

const FIXTURE_VAULT = join(import.meta.dirname, 'fixtures', 'vault');

describe('IndexPipeline', () => {
  let store: Store;
  let embedder: Embedder;
  let pipeline: IndexPipeline;

  beforeAll(async () => {
    store = new Store(':memory:');
    embedder = new Embedder();
    await embedder.init();
    pipeline = new IndexPipeline(store, embedder);
  }, 60000);

  afterAll(async () => {
    store.close();
    await embedder.dispose();
  });

  it('indexes the fixture vault', async () => {
    const stats = await pipeline.index(FIXTURE_VAULT);
    expect(stats.nodesIndexed).toBeGreaterThan(0);
    expect(stats.edgesIndexed).toBeGreaterThan(0);

    const alice = store.getNode('People/Alice Smith.md');
    expect(alice).toBeDefined();
    expect(alice!.title).toBe('Alice Smith');

    const edges = store.getEdgesFrom('People/Alice Smith.md');
    expect(edges.length).toBeGreaterThan(0);
  });

  it('creates stub nodes for broken links', async () => {
    // Store retains state from the first test's index() call
    const edges = store.getEdgesFrom('Ideas/Acme Project.md');
    const stubEdge = edges.find(e => e.targetId.includes('Nonexistent'));
    expect(stubEdge).toBeDefined();
  });

  it('detects communities', async () => {
    // Communities were detected during the first test's index() call
    const communities = store.getAllCommunities();
    expect(communities.length).toBeGreaterThan(0);
  });

  it('is incremental (skips unchanged files)', async () => {
    // Use a fresh store/pipeline so the first call indexes everything
    const freshStore = new Store(':memory:');
    const freshPipeline = new IndexPipeline(freshStore, embedder);

    const first = await freshPipeline.index(FIXTURE_VAULT);
    expect(first.nodesIndexed).toBeGreaterThan(0);
    expect(first.nodesDeleted).toBe(0);

    const second = await freshPipeline.index(FIXTURE_VAULT);
    expect(second.nodesIndexed).toBe(0);
    expect(second.nodesSkipped).toBe(first.nodesIndexed);
    expect(second.nodesDeleted).toBe(0);

    freshStore.close();
  });

  it('cleans FTS5 + sqlite-vec rows when a node is deleted', async () => {
    // Regression: deleteNode() must remove the node's rowid from BOTH the
    // FTS5 contentless mirror (nodes_fts) and the sqlite-vec virtual table
    // (nodes_vec). Bare DELETE on `nodes` does not cascade to either; FTS5
    // requires the explicit `INSERT INTO nodes_fts(...)VALUES('delete',...)`
    // and sqlite-vec needs a separate DELETE on its rowid.
    const freshStore = new Store(':memory:');
    const freshPipeline = new IndexPipeline(freshStore, embedder);
    await freshPipeline.index(FIXTURE_VAULT);

    const targetId = 'People/Alice Smith.md';
    const before = freshStore.db
      .prepare('SELECT rowid FROM nodes WHERE id = ?')
      .get(targetId) as { rowid: number } | undefined;
    expect(before).toBeDefined();
    const rowid = before!.rowid;

    // Sanity: Alice should be in both FTS5 and sqlite-vec before deletion.
    const ftsBefore = freshStore.db
      .prepare('SELECT rowid FROM nodes_fts WHERE rowid = ?')
      .get(rowid);
    expect(ftsBefore).toBeDefined();
    const vecBefore = freshStore.db
      .prepare('SELECT rowid FROM nodes_vec WHERE rowid = ?')
      .get(BigInt(rowid));
    expect(vecBefore).toBeDefined();

    freshStore.deleteNode(targetId);

    // Both shadow tables MUST be cleaned, otherwise full-text and vector
    // search would return stale matches pointing at a node that no longer
    // exists in `nodes`.
    const nodeAfter = freshStore.db
      .prepare('SELECT id FROM nodes WHERE id = ?')
      .get(targetId);
    expect(nodeAfter).toBeUndefined();
    const ftsAfter = freshStore.db
      .prepare('SELECT rowid FROM nodes_fts WHERE rowid = ?')
      .get(rowid);
    expect(ftsAfter).toBeUndefined();
    const vecAfter = freshStore.db
      .prepare('SELECT rowid FROM nodes_vec WHERE rowid = ?')
      .get(BigInt(rowid));
    expect(vecAfter).toBeUndefined();

    freshStore.close();
  });

  it('recomputes communities when nodes are deleted', async () => {
    // Regression: prior to this fix, index() only re-ran community detection on
    // additions/stubs. Pure-deletion runs left orphan singleton communities
    // pointing at gone nodes.
    const freshStore = new Store(':memory:');
    const freshPipeline = new IndexPipeline(freshStore, embedder);

    await freshPipeline.index(FIXTURE_VAULT);
    const initialCommunities = freshStore.getAllCommunities().length;
    expect(initialCommunities).toBeGreaterThan(0);

    // Inject a phantom path into sync — simulates a file that existed last run
    // but is gone this run. parseVault won't see it; deletion path will fire.
    freshStore.upsertSync('People/_Phantom.md', Date.now());
    expect(freshStore.getAllSyncPaths()).toContain('People/_Phantom.md');

    const stats = await freshPipeline.index(FIXTURE_VAULT);
    expect(stats.nodesDeleted).toBe(1);
    expect(stats.communitiesDetected).toBeGreaterThan(0);
    expect(freshStore.getAllSyncPaths()).not.toContain('People/_Phantom.md');

    freshStore.close();
  });
});
