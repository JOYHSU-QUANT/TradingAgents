"""Cross-layer shared utilities — the bottom of the import graph.

Home for the pure helpers that several layers (domains, persistence, paper,
live, audit) import but none owns: the enum guard, the YAML-coercion seam, the
pinned decimal context, the network vocabulary, and the atomic text write.
Nothing here may import from any other ``hyperliquid_perp`` package, so any
module can depend on ``common`` without creating a cycle.
"""
