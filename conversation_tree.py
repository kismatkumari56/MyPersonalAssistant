"""Conversation Message Tree & DAG Branching Engine for JUGNU (#62).

Implements:
- Hierarchical message tree with multiple child branches per node.
- Active path resolution from root to leaf for Ollama context compilation.
- Alternative response pagination and sibling navigation.
- Checkpoint forking to spawn independent conversations from any historical turn.
- Seamless backward compatibility with flat linear message lists.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


def _generate_node_id() -> str:
    return f"node_{uuid.uuid4().hex[:12]}"


@dataclass
class MessageNode:
    """A single message node within the conversation DAG."""

    id: str = field(default_factory=_generate_node_id)
    role: str = "user"                       # "user" | "assistant" | "system"
    content: str = ""
    parent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)
    active_child_index: int = 0              # Index into children_ids for the active branch
    timestamp: str = ""
    is_error: bool = False
    metrics: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_leaf(self) -> bool:
        return len(self.children_ids) == 0

    @property
    def active_child_id(self) -> str | None:
        if 0 <= self.active_child_index < len(self.children_ids):
            return self.children_ids[self.active_child_index]
        return None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
            "active_child_index": self.active_child_index,
            "timestamp": self.timestamp,
            "is_error": self.is_error,
        }
        if self.metrics:
            data["metrics"] = self.metrics
        if self.metadata:
            data["metadata"] = self.metadata
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageNode:
        return cls(
            id=data.get("id") or _generate_node_id(),
            role=data.get("role", "user"),
            content=data.get("content", ""),
            parent_id=data.get("parent_id"),
            children_ids=list(data.get("children_ids") or []),
            active_child_index=int(data.get("active_child_index", 0) or 0),
            timestamp=data.get("timestamp", ""),
            is_error=bool(data.get("is_error", False)),
            metrics=data.get("metrics"),
            metadata=dict(data.get("metadata") or {}),
        )


class ConversationTree:
    """Manages tree-structured conversation branches with active path resolution."""

    def __init__(
        self,
        root_ids: list[str] | None = None,
        nodes: dict[str, MessageNode] | None = None,
        active_root_index: int = 0,
    ):
        self.root_ids: list[str] = list(root_ids or [])
        self.nodes: dict[str, MessageNode] = dict(nodes or {})
        self.active_root_index: int = max(0, active_root_index)

    def add_node(
        self,
        role: str,
        content: str,
        parent_id: str | None = None,
        timestamp: str = "",
        is_error: bool = False,
        metrics: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> MessageNode:
        """Add a new message node. If parent already has children, creates an alternative branch."""
        new_id = node_id or _generate_node_id()
        node = MessageNode(
            id=new_id,
            role=role,
            content=content,
            parent_id=parent_id,
            timestamp=timestamp,
            is_error=is_error,
            metrics=metrics,
        )
        self.nodes[new_id] = node

        if parent_id is None or parent_id not in self.nodes:
            self.root_ids.append(new_id)
            self.active_root_index = len(self.root_ids) - 1
            node.parent_id = None
        else:
            parent = self.nodes[parent_id]
            if new_id not in parent.children_ids:
                parent.children_ids.append(new_id)
            parent.active_child_index = len(parent.children_ids) - 1

        return node

    def get_node(self, node_id: str | None) -> MessageNode | None:
        if not node_id:
            return None
        return self.nodes.get(node_id)

    def get_active_path(self) -> list[MessageNode]:
        """Traverse down from active root along active_child_index pointers to current leaf."""
        if not self.root_ids:
            return []

        if not (0 <= self.active_root_index < len(self.root_ids)):
            self.active_root_index = 0

        curr_id = self.root_ids[self.active_root_index]
        path: list[MessageNode] = []
        visited: set[str] = set()

        while curr_id and curr_id in self.nodes and curr_id not in visited:
            visited.add(curr_id)
            node = self.nodes[curr_id]
            path.append(node)
            curr_id = node.active_child_id

        return path

    def get_active_messages(self) -> list[dict[str, Any]]:
        """Return the active path as a list of message dicts matching standard history format."""
        path = self.get_active_path()
        messages: list[dict[str, Any]] = []
        for node in path:
            msg: dict[str, Any] = {
                "role": node.role,
                "content": node.content,
                "node_id": node.id,
            }
            if node.timestamp:
                msg["time"] = node.timestamp
            if node.is_error:
                msg["is_error"] = True
            if node.metrics:
                msg["metrics"] = node.metrics
            messages.append(msg)
        return messages

    def get_siblings(self, node_id: str) -> tuple[int, int, list[str]]:
        """Return (current_index, total_siblings_count, sibling_ids_list)."""
        node = self.nodes.get(node_id)
        if not node:
            return (0, 0, [])

        if node.parent_id and node.parent_id in self.nodes:
            parent = self.nodes[node.parent_id]
            siblings = parent.children_ids
        else:
            siblings = self.root_ids

        try:
            idx = siblings.index(node_id)
        except ValueError:
            idx = 0

        return (idx, len(siblings), list(siblings))

    def switch_branch(self, node_id: str, target_index: int) -> bool:
        """Switch the active sibling index for the given node's sibling group."""
        node = self.nodes.get(node_id)
        if not node:
            return False

        if node.parent_id and node.parent_id in self.nodes:
            parent = self.nodes[node.parent_id]
            if 0 <= target_index < len(parent.children_ids):
                parent.active_child_index = target_index
                return True
        else:
            if 0 <= target_index < len(self.root_ids):
                self.active_root_index = target_index
                return True

        return False

    def fork_tree(self, checkpoint_node_id: str) -> ConversationTree:
        """Create a fresh independent ConversationTree containing ancestor nodes up to checkpoint."""
        if checkpoint_node_id not in self.nodes:
            return ConversationTree()

        # Trace path from checkpoint back to root
        lineage: list[MessageNode] = []
        curr: MessageNode | None = self.nodes[checkpoint_node_id]
        while curr:
            lineage.append(curr)
            curr = self.nodes.get(curr.parent_id) if curr.parent_id else None

        lineage.reverse()  # Root down to checkpoint

        # Reconstruct clean linear tree
        new_tree = ConversationTree()
        prev_parent_id: str | None = None
        for old_node in lineage:
            new_node = new_tree.add_node(
                role=old_node.role,
                content=old_node.content,
                parent_id=prev_parent_id,
                timestamp=old_node.timestamp,
                is_error=old_node.is_error,
                metrics=dict(old_node.metrics) if old_node.metrics else None,
            )
            prev_parent_id = new_node.id

        return new_tree

    @classmethod
    def from_linear_messages(cls, messages: list[dict[str, Any]]) -> ConversationTree:
        """Reconstruct a ConversationTree from legacy 1D linear message lists."""
        tree = cls()
        parent_id: str | None = None
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "user")
            content = msg.get("content", "")
            time_val = msg.get("time", "")
            is_error = bool(msg.get("is_error", False))
            metrics = msg.get("metrics")
            node = tree.add_node(
                role=role,
                content=content,
                parent_id=parent_id,
                timestamp=time_val,
                is_error=is_error,
                metrics=metrics,
            )
            parent_id = node.id
        return tree

    def to_dict(self) -> dict[str, Any]:
        """Serialize tree to a JSON-serializable dictionary."""
        return {
            "root_ids": list(self.root_ids),
            "active_root_index": self.active_root_index,
            "nodes": {nid: node.to_dict() for nid, node in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ConversationTree:
        """Deserialize tree from a dictionary."""
        if not data or not isinstance(data, dict):
            return cls()
        root_ids = list(data.get("root_ids") or [])
        active_root_index = int(data.get("active_root_index", 0) or 0)
        raw_nodes = data.get("nodes") or {}
        nodes: dict[str, MessageNode] = {}
        for nid, ndict in raw_nodes.items():
            if isinstance(ndict, dict):
                nodes[nid] = MessageNode.from_dict(ndict)
        return cls(root_ids=root_ids, nodes=nodes, active_root_index=active_root_index)
