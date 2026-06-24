"""Blueprint graph model."""
from __future__ import annotations

import re
from collections import deque
from typing import Optional

from pydantic import BaseModel, Field

from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus


_LEAN_IDENT = re.compile(r'^[a-zA-Z_α-ωΑ-Ω][a-zA-Z0-9_\'α-ωΑ-Ω.]*$')


def _is_valid_lean_ident(name: str) -> bool:
    """Check if name is a plausible Lean identifier (snake_case, unicode ok)."""
    return bool(_LEAN_IDENT.match(name)) if name else False


class Blueprint(BaseModel):
    """A dependency graph of blueprint nodes."""
    nodes: dict[str, BlueprintNode] = Field(default_factory=dict)
    target_id: str = ""

    # ── adjacency helpers ────────────────────────────────────────────────

    def parents_of(self, node_id: str) -> list[str]:
        """Return the parent ids of a node."""
        node = self.nodes.get(node_id)
        return list(node.parents) if node else []

    def children_of(self, node_id: str) -> list[str]:
        """Return ids of nodes that list node_id as a parent."""
        return [nid for nid, n in self.nodes.items() if node_id in n.parents]

    # ── topological sort ─────────────────────────────────────────────────

    def topo_sort(self) -> list[str]:
        """Kahn's algorithm; tie-break by sorted id. Raises on cycle."""
        in_degree: dict[str, int] = {nid: 0 for nid in self.nodes}
        for nid, node in self.nodes.items():
            for pid in node.parents:
                if pid in in_degree:
                    in_degree[nid] = in_degree.get(nid, 0) + 1

        # Recompute properly
        in_degree = {nid: 0 for nid in self.nodes}
        for nid, node in self.nodes.items():
            for pid in node.parents:
                if pid in self.nodes:
                    in_degree[nid] += 1

        queue: list[str] = sorted(
            [nid for nid, deg in in_degree.items() if deg == 0]
        )
        result: list[str] = []

        while queue:
            nid = queue.pop(0)
            result.append(nid)
            children = sorted(self.children_of(nid))
            for child in children:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    # Insert in sorted order
                    inserted = False
                    for i, q in enumerate(queue):
                        if child < q:
                            queue.insert(i, child)
                            inserted = True
                            break
                    if not inserted:
                        queue.append(child)

        if len(result) != len(self.nodes):
            raise ValueError("Blueprint graph contains a cycle")
        return result

    # ── cycle detection ──────────────────────────────────────────────────

    def detect_cycle(self) -> Optional[list[str]]:
        """DFS cycle detection. Returns a cycle path or None."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {nid: WHITE for nid in self.nodes}
        parent: dict[str, Optional[str]] = {nid: None for nid in self.nodes}

        def dfs(v: str) -> Optional[list[str]]:
            color[v] = GRAY
            for child in self.children_of(v):
                if child not in color:
                    continue
                if color[child] == GRAY:
                    # Reconstruct cycle
                    cycle = [child, v]
                    cur = v
                    while cur != child and parent[cur] is not None:
                        cur = parent[cur]  # type: ignore[assignment]
                        cycle.append(cur)
                    return list(reversed(cycle))
                if color[child] == WHITE:
                    parent[child] = v
                    result = dfs(child)
                    if result:
                        return result
            color[v] = BLACK
            return None

        for nid in sorted(self.nodes):
            if color[nid] == WHITE:
                cycle = dfs(nid)
                if cycle:
                    return cycle
        return None

    # ── reachability ─────────────────────────────────────────────────────

    def reachable_from_target(self) -> set[str]:
        """BFS over parent edges starting from target_id."""
        if not self.target_id or self.target_id not in self.nodes:
            return set()
        visited: set[str] = set()
        queue: deque[str] = deque([self.target_id])
        while queue:
            nid = queue.popleft()
            if nid in visited:
                continue
            visited.add(nid)
            for pid in self.nodes[nid].parents:
                if pid in self.nodes and pid not in visited:
                    queue.append(pid)
        return visited

    def prune_unreachable(self) -> list[str]:
        """Remove nodes not reachable from target. Returns removed ids."""
        reachable = self.reachable_from_target()
        removed = [nid for nid in list(self.nodes) if nid not in reachable]
        for nid in removed:
            del self.nodes[nid]
        return removed

    # ── auto-satisfy definitions ─────────────────────────────────────────

    def auto_satisfy_definitions(self) -> list[str]:
        """Mark all DEFINITION nodes as PROVED (they need no proof).

        Definitions are "given" declarations (def/abbrev/structure/instance).
        They carry a real Lean body, not a proposition to prove, so they are
        automatically considered satisfied.  Call this after loading or
        updating a blueprint to ensure DEFINITION nodes never reach the prover.

        Returns the list of node ids that were auto-satisfied.
        """
        satisfied: list[str] = []
        for nid, node in self.nodes.items():
            if node.kind == NodeKind.DEFINITION and node.status != NodeStatus.PROVED:
                node.status = NodeStatus.PROVED
                satisfied.append(nid)
        return satisfied

    # ── ready nodes ──────────────────────────────────────────────────────

    def ready_nodes(self) -> list[str]:
        """Nodes with status PENDING or UNPROVED whose every parent is PROVED.

        DEFINITION nodes are never included — they are auto-satisfied and need
        no proof.  Returns remaining provable nodes in topological order.
        """
        try:
            order = self.topo_sort()
        except ValueError:
            return []

        result = []
        for nid in order:
            node = self.nodes[nid]
            # DEFINITION nodes are never sent to the prover
            if node.kind == NodeKind.DEFINITION:
                continue
            if node.status not in (NodeStatus.PENDING, NodeStatus.UNPROVED):
                continue
            all_parents_proved = all(
                self.nodes[pid].status == NodeStatus.PROVED
                for pid in node.parents
                if pid in self.nodes
            )
            if all_parents_proved:
                result.append(nid)
        return result

    # ── solved ───────────────────────────────────────────────────────────

    def is_solved(self) -> bool:
        """True if target node status == PROVED."""
        node = self.nodes.get(self.target_id)
        return node is not None and node.status == NodeStatus.PROVED

    # ── validate ─────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """Return list of human-readable errors (empty = valid)."""
        errors: list[str] = []

        # Exactly one TARGET node
        target_nodes = [nid for nid, n in self.nodes.items() if n.kind == NodeKind.TARGET]
        if len(target_nodes) == 0:
            errors.append("No TARGET node found in blueprint")
        elif len(target_nodes) > 1:
            errors.append(f"Multiple TARGET nodes: {target_nodes}")
        else:
            if target_nodes[0] != self.target_id:
                errors.append(
                    f"TARGET node id '{target_nodes[0]}' != target_id '{self.target_id}'"
                )

        # target_id exists
        if self.target_id and self.target_id not in self.nodes:
            errors.append(f"target_id '{self.target_id}' not found in nodes")

        # Every parent id exists
        for nid, node in self.nodes.items():
            for pid in node.parents:
                if pid not in self.nodes:
                    errors.append(f"Node '{nid}' references unknown parent '{pid}'")

        # Acyclic
        cycle = self.detect_cycle()
        if cycle:
            errors.append(f"Cycle detected: {' -> '.join(cycle)}")

        # All nodes reachable from target
        reachable = self.reachable_from_target()
        for nid in self.nodes:
            if nid not in reachable:
                errors.append(f"Node '{nid}' is not reachable from target '{self.target_id}' (dead node)")

        # Unique lean_name
        seen_names: dict[str, str] = {}
        for nid, node in self.nodes.items():
            if node.lean_name in seen_names:
                errors.append(
                    f"Duplicate lean_name '{node.lean_name}' in nodes '{seen_names[node.lean_name]}' and '{nid}'"
                )
            else:
                seen_names[node.lean_name] = nid

        # Valid lean identifiers
        for nid, node in self.nodes.items():
            if not _is_valid_lean_ident(node.lean_name):
                errors.append(f"Node '{nid}' has invalid lean_name '{node.lean_name}'")

        return errors

    # ── serialisation ─────────────────────────────────────────────────────

    def to_json(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, data: dict) -> "Blueprint":
        """Deserialize from a dict (as produced by to_json)."""
        return cls.model_validate(data)
