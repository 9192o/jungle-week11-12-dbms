from __future__ import annotations

import bisect


class _Node:
    __slots__: tuple[str, ...] = ("is_leaf", "keys", "children", "values", "next_leaf")

    def __init__(self, is_leaf: bool) -> None:
        self.is_leaf: bool = is_leaf
        self.keys: list[int] = []
        self.children: list[_Node] = []
        self.values: list[int] = []
        self.next_leaf: _Node | None = None


class BPlusTree:
    def __init__(self, order: int = 64) -> None:
        if order < 3:
            raise ValueError("order must be at least 3")
        self.order: int = order
        self.root: _Node | None = None

    def find(self, key: int) -> int | None:
        if self.root is None:
            return None
        node = self.root
        while not node.is_leaf:
            index = bisect.bisect_right(node.keys, key)
            node = node.children[index]
        index = bisect.bisect_left(node.keys, key)
        if index < len(node.keys) and node.keys[index] == key:
            return node.values[index]
        return None

    def insert(self, key: int, offset: int) -> None:
        if self.root is None:
            leaf = _Node(is_leaf=True)
            leaf.keys.append(key)
            leaf.values.append(offset)
            self.root = leaf
            return

        if self.find(key) is not None:
            raise ValueError(f"duplicate key: {key}")

        split = self._insert_recursive(self.root, key, offset)
        if split is not None:
            new_root = _Node(is_leaf=False)
            new_root.keys.append(split.separator)
            new_root.children.append(self.root)
            new_root.children.append(split.right)
            self.root = new_root

    class _SplitResult:
        __slots__: tuple[str, ...] = ("separator", "right")

        def __init__(self, separator: int, right: _Node) -> None:
            self.separator: int = separator
            self.right: _Node = right

    def _insert_recursive(
        self, node: _Node, key: int, offset: int
    ) -> _SplitResult | None:
        if node.is_leaf:
            index = bisect.bisect_left(node.keys, key)
            node.keys.insert(index, key)
            node.values.insert(index, offset)
            if len(node.keys) > self.order - 1:
                return self._split_leaf(node)
            return None

        index = bisect.bisect_right(node.keys, key)
        child = node.children[index]
        split = self._insert_recursive(child, key, offset)
        if split is None:
            return None

        node.keys.insert(index, split.separator)
        node.children.insert(index + 1, split.right)
        if len(node.keys) > self.order - 1:
            return self._split_internal(node)
        return None

    def _split_leaf(self, node: _Node) -> _SplitResult:
        split_point = len(node.keys) // 2
        right = _Node(is_leaf=True)
        right.keys = node.keys[split_point:]
        right.values = node.values[split_point:]
        right.next_leaf = node.next_leaf
        node.keys = node.keys[:split_point]
        node.values = node.values[:split_point]
        node.next_leaf = right
        separator = right.keys[0]
        return self._SplitResult(separator, right)

    def _split_internal(self, node: _Node) -> _SplitResult:
        split_point = len(node.keys) // 2
        separator = node.keys[split_point]
        right = _Node(is_leaf=False)
        right.keys = node.keys[split_point + 1 :]
        right.children = node.children[split_point + 1 :]
        node.keys = node.keys[:split_point]
        node.children = node.children[: split_point + 1]
        return self._SplitResult(separator, right)
